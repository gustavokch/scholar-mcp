import logging
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import WHOGuideline
from scholar_mcp.parsers.pdf import pdf_bytes_to_text
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager
from scholar_mcp.utils.text import truncate_content

IRIS_API_BASE = "https://iris.who.int/server/api"
IRIS_BROWSE_TITLE_URL = f"{IRIS_API_BASE}/discover/browses/title/items"
IRIS_SEARCH_URL = f"{IRIS_API_BASE}/discover/search/objects"
IRIS_HANDLE_BASE = "https://iris.who.int/handle"
IRIS_PID_FIND_URL = f"{IRIS_API_BASE}/pid/find"
IRIS_ITEM_BUNDLES_URL = f"{IRIS_API_BASE}/core/items"
IRIS_BUNDLE_BITSTREAMS_URL = f"{IRIS_API_BASE}/core/bundles"
IRIS_BITSTREAM_CONTENT_URL = f"{IRIS_API_BASE}/core/bitstreams"
IRIS_ITEM_TYPE_FILTER = "Publications,equals"
MAX_PAGE_SIZE = 100
MAX_RESULTS = 200
MAX_FULL_TEXT_CHARS = 50_000

logger = logging.getLogger(__name__)


def _first_meta(metadata: dict[str, list[dict[str, Any]]], key: str) -> str:
    """Return metadata[key][0]["value"], or "" if the key/value is absent."""
    values = metadata.get(key) or []
    if not values:
        return ""
    first = values[0]
    if not isinstance(first, dict):
        return ""
    return first.get("value") or ""


def _all_meta(metadata: dict[str, list[dict[str, Any]]], key: str) -> list[str]:
    """Return every non-empty value string for a multi-valued metadata entry."""
    return [
        v.get("value", "")
        for v in (metadata.get(key) or [])
        if isinstance(v, dict) and v.get("value")
    ]


def _extract_browse_page(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    embedded = data.get("_embedded") or {}
    items = embedded.get("items") or []
    page_info = data.get("page") or {}
    return items, page_info


def _extract_search_page(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    search_result = (data.get("_embedded") or {}).get("searchResult") or {}
    objects = (search_result.get("_embedded") or {}).get("objects") or []
    items = [
        (obj.get("_embedded") or {}).get("indexableObject") or {}
        for obj in objects
    ]
    page_info = search_result.get("page") or {}
    return items, page_info


def _build_record(item: dict[str, Any]) -> WHOGuideline:
    metadata = item.get("metadata") or {}
    handle = item.get("handle") or ""
    title = _first_meta(metadata, "dc.title") or item.get("name") or ""
    date_issued = _first_meta(metadata, "dc.date.issued")
    return WHOGuideline(
        title=title,
        handle=handle,
        url=f"{IRIS_HANDLE_BASE}/{handle}" if handle else "",
        year=date_issued[:4] if date_issued else "",
        description=_first_meta(metadata, "dc.description.abstract")
        or _first_meta(metadata, "dc.description"),
        authors=_all_meta(metadata, "dc.contributor.author"),
        languages=_all_meta(metadata, "dc.language.iso"),
        mesh_subjects=_all_meta(metadata, "dc.subject.mesh"),
        subjects=_all_meta(metadata, "dc.subject"),
        spatial_coverage=_all_meta(metadata, "dc.coverage.spatial"),
        isbn=_first_meta(metadata, "dc.identifier.isbn"),
        publisher=_first_meta(metadata, "dc.publisher"),
        item_type=_first_meta(metadata, "dc.type"),
    )


def _normalize_handle(handle: str) -> str:
    """Strip URL/"hdl:" decorations from an IRIS handle down to "12345/67890"."""
    h = handle.strip()
    if "/handle/" in h:
        h = h.split("/handle/", 1)[1]
    if h.lower().startswith("hdl:"):
        h = h[4:]
    return h.strip("/")


class WHOIRISEngine:
    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    async def _fetch_paginated(
        self,
        url: str,
        params: dict[str, str],
        extract: Any,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch pages until `limit` items are collected or pages run out.

        Returns the raw item dicts and whether a fetch failed partway through.
        """
        collected: list[dict[str, Any]] = []
        page = 0
        size = min(limit, MAX_PAGE_SIZE)

        while len(collected) < limit:
            page_params = {**params, "size": str(size), "page": str(page)}
            try:
                resp = await self.http_client.get(
                    url, params=page_params, headers={"Accept": "application/json"}
                )
                if resp is None:
                    raise FetchError("who iris request failed")
                data = resp.json()
            except Exception:
                logger.warning("WHO IRIS request failed for %s", url, exc_info=True)
                return collected, True

            items, page_info = extract(data)
            collected.extend(items)
            total_pages = page_info.get("totalPages", 0)
            page += 1
            if page >= total_pages or not items:
                break

        return collected[:limit], False

    async def search_guidelines(
        self,
        query: str,
        limit: int = 10,
        mode: str = "prefix",
    ) -> tuple[list[WHOGuideline], CacheMetadata]:
        # Clamp inside the engine too: the tool boundary clamp must not be the
        # only guard, or a direct call with a huge limit pages through the
        # whole repository.
        limit = min(max(1, limit), MAX_RESULTS)
        mode = mode.strip().lower()
        if mode not in ("prefix", "fulltext"):
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        normalized = query.strip().lower()
        cache_key = f"who_iris:{mode}:{normalized}:{limit}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [WHOGuideline.from_dict(d) for d in cached_data], meta

        if mode == "fulltext":
            url = IRIS_SEARCH_URL
            params = {"query": query.strip(), "f.itemtype": IRIS_ITEM_TYPE_FILTER}
            extract = _extract_search_page
        else:
            url = IRIS_BROWSE_TITLE_URL
            # No item-type filter here: the browse endpoint ignores it (the
            # search endpoint's f.itemtype is honoured). Item type is derived
            # from dc.type metadata instead.
            params = {"startsWith": query.strip()}
            extract = _extract_browse_page

        raw_items, errored = await self._fetch_paginated(url, params, extract, limit)

        records = [_build_record(item) for item in raw_items if item]

        # A failed page fetch must never be served from cache for the whole TTL:
        # skip the write whenever any page errored, even when earlier pages
        # returned a partial result set (same convention as fda.py).
        if errored:
            return records, CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(cache_key, [r.to_dict() for r in records], source="who_iris")
        return records, CacheMetadata(cached=False, cache_age=0, error=errored)

    async def get_full_text(
        self,
        handle: str,
        max_chars: int | None = None,
    ) -> tuple[dict[str, Any], CacheMetadata]:
        normalized = _normalize_handle(handle)
        base = {
            "source": "who-iris",
            "handle": normalized,
            "url": f"{IRIS_HANDLE_BASE}/{normalized}" if normalized else "",
            "truncated": False,
        }
        if not normalized:
            return (
                {**base, "status": "error", "error": "handle is required",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=True),
            )

        cache_key = f"who_iris_fulltext:{normalized}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return self._serve_full_text(cached_data, max_chars), meta

        # ok_statuses lets a 404 through so not-found is distinguishable from a
        # network failure (get() otherwise collapses both to None).
        resp = await self.http_client.get(
            IRIS_PID_FIND_URL,
            params={"id": f"hdl:{normalized}"},
            headers={"Accept": "application/json"},
            ok_statuses=frozenset({404}),
        )
        if resp is None:
            return (
                {**base, "status": "error", "error": "who iris request failed",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=True),
            )
        if resp.status_code == 404:
            return (
                {**base, "status": "not_found", "error": "no item for handle",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=False),
            )

        item = resp.json()
        metadata = item.get("metadata") or {}
        title = _first_meta(metadata, "dc.title") or item.get("name") or ""
        abstract = (
            _first_meta(metadata, "dc.description.abstract")
            or _first_meta(metadata, "dc.description")
        )

        # A failed bitstream fetch degrades the result to the abstract but
        # still marks it errored so it is never cached for the whole TTL.
        pdf_text, errored = await self._extract_pdf_text(item.get("uuid") or "")
        if pdf_text:
            result = {"content_type": "pdf", "content": pdf_text}
        elif abstract:
            result = {"content_type": "abstract", "content": abstract}
        else:
            return (
                {**base, "status": "not_found", "error": "no full text or abstract available",
                 "title": title, "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=errored),
            )

        payload = {**base, "status": "success", "title": title, **result}
        if not errored:
            await self.cache.set(cache_key, payload, source="who_iris")
        return self._serve_full_text(payload, max_chars), CacheMetadata(cached=False, cache_age=0, error=errored)

    async def _extract_pdf_text(self, item_uuid: str) -> tuple[str, bool]:
        """Extract the primary PDF's text. Returns (text, errored)."""
        if not item_uuid:
            return "", False
        try:
            bundles_resp = await self.http_client.get(
                f"{IRIS_ITEM_BUNDLES_URL}/{item_uuid}/bundles",
                params={"size": str(MAX_PAGE_SIZE)},
                headers={"Accept": "application/json"},
            )
            if bundles_resp is None:
                return "", True
            bundles = (bundles_resp.json().get("_embedded") or {}).get("bundles") or []
            original = next((b for b in bundles if b.get("name") == "ORIGINAL"), None)
            if original is None:
                return "", False

            bits_resp = await self.http_client.get(
                f"{IRIS_BUNDLE_BITSTREAMS_URL}/{original.get('uuid')}/bitstreams",
                params={"size": str(MAX_PAGE_SIZE)},
                headers={"Accept": "application/json"},
            )
            if bits_resp is None:
                return "", True
            bitstreams = (bits_resp.json().get("_embedded") or {}).get("bitstreams") or []
            # Live IRIS payloads leave bitstream mimeType null; the .pdf name
            # suffix is then the only marker, so accept either.
            pdfs = [
                b for b in bitstreams
                if (b.get("mimeType") or "").startswith("application/pdf")
                or (b.get("name") or "").lower().endswith(".pdf")
            ]
            if not pdfs:
                return "", False

            best = max(pdfs, key=lambda b: b.get("sizeBytes") or 0)
            pdf_bytes = await self.http_client.get_bytes(
                f"{IRIS_BITSTREAM_CONTENT_URL}/{best.get('uuid')}/content"
            )
            if pdf_bytes is None:
                return "", True
            return pdf_bytes_to_text(pdf_bytes), False
        except Exception:
            logger.warning("WHO IRIS full-text fetch failed for item %s", item_uuid, exc_info=True)
            return "", True

    @staticmethod
    def _serve_full_text(payload: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
        served = dict(payload)
        content, truncated = truncate_content(served.get("content") or "", max_chars or MAX_FULL_TEXT_CHARS)
        served["content"] = content
        served["truncated"] = truncated
        return served
