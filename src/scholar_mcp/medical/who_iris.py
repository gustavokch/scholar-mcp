import logging
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import WHOGuideline
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

IRIS_API_BASE = "https://iris.who.int/server/api"
IRIS_BROWSE_TITLE_URL = f"{IRIS_API_BASE}/discover/browses/title/items"
IRIS_SEARCH_URL = f"{IRIS_API_BASE}/discover/search/objects"
IRIS_HANDLE_BASE = "https://iris.who.int/handle"
IRIS_ITEM_TYPE_FILTER = "f.itemtype=Publications,equals"
MAX_PAGE_SIZE = 100

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
        item_type="Publications",
    )


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
        normalized = query.strip().lower()
        cache_key = f"who_iris:{mode}:{normalized}:{limit}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [WHOGuideline.from_dict(d) for d in cached_data], meta

        if mode == "fulltext":
            url = IRIS_SEARCH_URL
            params = {"query": query.strip(), "f.itemtype": "Publications,equals"}
            extract = _extract_search_page
        else:
            url = IRIS_BROWSE_TITLE_URL
            params = {"startsWith": query.strip(), "filter": IRIS_ITEM_TYPE_FILTER}
            extract = _extract_browse_page

        raw_items, errored = await self._fetch_paginated(url, params, extract, limit)

        records = [_build_record(item) for item in raw_items if item]

        if errored and not records:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(cache_key, [r.to_dict() for r in records], source="who_iris")
        return records, CacheMetadata(cached=False, cache_age=0, error=errored)
