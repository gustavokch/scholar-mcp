"""Scraper and search engine for official Brazilian SUS PCDT guidelines from gov.br.

Retrieves clinical guidelines (Protocolos Clínicos e Diretrizes Terapêuticas - PCDT)
directly from the Ministry of Health portal:
https://www.gov.br/saude/pt-br/assuntos/pcdt/[a-u]/[condition]
"""

from collections.abc import Callable
import functools
import json
import logging
from pathlib import Path
from typing import Any
import urllib.parse

from bs4 import BeautifulSoup
from scholar_mcp.config import Settings
from scholar_mcp.medical.govbr_common import (  # noqa: F401  (re-exported)
    CACHE_SCHEMA,
    GOVBR_HEADERS,
    MIN_CATALOG_RETENTION,
    PORTUGUESE_STOPWORDS,
    derive_item_urls,
    normalize_text,
    score_item,
    tokenize_portuguese,
)
from scholar_mcp.medical.models import BrazilGuideline, has_retrievable_body
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

logger = logging.getLogger(__name__)

PCDT_BASE_URL = "https://www.gov.br/saude/pt-br/assuntos/pcdt"
PCDT_LETTERS = (
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "l",
    "m", "n", "o", "p", "r", "s", "t", "u",
)

# Letter pages paginate 20 items per page; 25 pages is far above anything
# observed and a hard stop against a pagination chain that never ends. A
# letter cut off here with pages still queued is an incomplete crawl.
MAX_PAGES_PER_LETTER = 25

def load_seed_catalog() -> dict[str, dict[str, Any]]:
    """Load the bundled pre-scraped PCDT catalog."""
    seed_path = Path(__file__).resolve().parent.parent / "data" / "govbr_pcdt_catalog.json"
    if not seed_path.exists():
        logger.warning("PCDT seed catalog not found at %s", seed_path)
        return {}
    try:
        with seed_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to load PCDT seed catalog: %s", exc)
        return {}


@functools.lru_cache(maxsize=1)
def load_extended_catalog() -> dict[str, dict[str, Any]]:
    """Load the bundled full-text MoH manuals corpus, normalized to PCDT shape.

    Cached per process: the corpus is a bundled, immutable file, so every
    ``get_catalog`` call reuses one parsed dict instead of re-reading disk.
    """
    ext_path = (
        Path(__file__).resolve().parent.parent / "data" / "brazil_moh_extended_catalog.json"
    )
    if not ext_path.exists():
        return {}
    try:
        with ext_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load extended MoH catalog: %s", exc)
        return {}
    catalog: dict[str, dict[str, Any]] = {}
    for record_id, row in raw.items():
        if not isinstance(row, dict):
            continue
        file_path = row.get("file_path", "")
        slug = (
            Path(file_path).stem.replace("_", "-")
            if file_path
            else str(record_id).lower()
        )
        catalog[record_id] = {
            "record_id": record_id,
            "slug": slug,
            "title": row.get("title", ""),
            "description": row.get("description", ""),
            "keywords": row.get("keywords", ""),
            "source_url": row.get("source_url", ""),
            "file_path": file_path,
            "download_url": f"local:{file_path}",
            "authors": row.get("authors", []),
            "collections": row.get("collections", []),
        }
    return catalog


def _merge_extended(base: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return a NEW dict of ``base`` plus the extended corpus rows.

    The in-memory catalog stays base-only: the merged result is never
    stored, so the seed and the extended corpus stay separate sources.
    """
    return {**base, **load_extended_catalog()}


def parse_letter_page(
    html: str, letter: str, base_url: str
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Parse one letter index page from gov.br PCDT directory."""
    soup = BeautifulSoup(html, "html.parser")
    items: dict[str, dict[str, Any]] = {}
    next_urls: list[str] = []

    # Check for Plone pagination links
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if f"/pcdt/{letter}?" in href and "b_start:int=" in href:
            full_next = urllib.parse.urljoin(base_url, href)
            if full_next not in next_urls:
                next_urls.append(full_next)

    content_div = (
        soup.find("div", id="content-core")
        or soup.find("div", id="content")
        or soup
    )

    for a in content_div.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not text or len(text) < 3:
            continue

        clean_href = href.rstrip("/").split("?")[0]
        # Exclude directory / navigation links
        if (
            clean_href.endswith(f"/pcdt/{letter}")
            or clean_href.endswith("/pcdt")
            or "/pcdt/pcdt" in clean_href
        ):
            continue

        target_marker = f"/pcdt/{letter}/"
        if target_marker in clean_href:
            parts = clean_href.split(target_marker)
            if len(parts) > 1:
                raw_slug = parts[1].split("/")[0]
                slug = raw_slug.replace(".pdf", "")
                if not slug:
                    continue
                record_id = f"pcdt-{slug}"

                base_item_url = (
                    clean_href[:-5] if clean_href.endswith("/view") else clean_href
                )
                download_url = f"{base_item_url}/@@download/file"
                view_url = (
                    clean_href if clean_href.endswith("/view") else f"{clean_href}/view"
                )

                if record_id not in items or len(text) > len(items[record_id]["title"]):
                    items[record_id] = {
                        "record_id": record_id,
                        "slug": slug,
                        "title": text,
                        "letter": letter,
                        "view_url": view_url,
                        "download_url": download_url,
                    }

    return items, next_urls


def _score_item(query_tokens: list[str], query_norm: str, item: dict[str, Any]) -> float:
    """Compute matching score between query and catalog item."""
    extra = f"{item.get('description', '')} {item.get('keywords', '')}".strip()
    return score_item(
        query_tokens,
        query_norm,
        item.get("title", ""),
        item.get("slug", ""),
        extra,
    )


def _dict_to_guideline(item: dict[str, Any], score: float | None = None) -> BrazilGuideline:
    """Convert catalog dictionary to BrazilGuideline dataclass.

    ``authors``/``collections`` pass through when the catalog row declares
    them (extended corpus rows carry their real source); crawled and seed
    rows default to the MS/CONITEC PCDT shape.
    """
    document_url = item.get("download_url", "")
    return BrazilGuideline(
        title=item.get("title", ""),
        record_id=item.get("record_id", ""),
        document_url=document_url,
        fulltext_id=item.get("record_id", ""),
        # One shared rule (medical.models.has_retrievable_body): a catalog
        # download URL -- every one is a first-party Plone file URL -- or
        # description text served as the abstract fallback. ``fulltext_id``
        # here is the catalog id, not a fi-admin view, so it is not passed
        # as fulltext evidence.
        has_full_text=has_retrievable_body(
            document_url,
            fallback_text=item.get("description", ""),
            url_trusted=True,
        ),
        source="brazil-moh",
        abstract=item.get("description", ""),
        country="Brasil",
        languages=["pt"],
        collections=item.get("collections") or ["PCDT"],
        authors=item.get("authors") or ["Ministério da Saúde", "CONITEC"],
        doi=(item.get("doi") or "").strip().rstrip(".,;:)]}"),
        score=score,
    )


class GovBrPCDTEngine:
    """Catalog crawler and search engine for Brazilian MoH PCDT guidelines."""

    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings
        self._memory_catalog: dict[str, dict[str, Any]] | None = None

    async def get_catalog(self) -> dict[str, dict[str, Any]]:
        """Get the PCDT catalog from memory or the bundled seed.

        The seed is the catalog: no search crawls gov.br, and nothing here
        touches the network or the SQLite cache.
        ``scripts/update_govbr_catalogs.py --catalog pcdt`` regenerates the
        seed offline. Every path returns a NEW merged dict (base plus the
        extended corpus); the stored base is never mutated.

        A missing or empty seed returns ``{}``, without the extended corpus
        and without being kept: extended rows alone are a partial catalog,
        and ``search`` must report the outage instead of a success over them.
        """
        if self._memory_catalog:
            return _merge_extended(self._memory_catalog)

        seed = load_seed_catalog()
        if not seed:
            logger.error("PCDT seed catalog is missing or empty; reporting an outage")
            return {}
        self._memory_catalog = seed
        return _merge_extended(seed)

    async def refresh_catalog(
        self, incumbent: dict[str, dict[str, Any]] | None = None
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Crawl a fresh catalog from the gov.br portal. Offline use only.

        Returns ``(catalog, complete)``. ``complete`` is True only when every
        page of every letter loaded and the catalog holds at least
        ``MIN_CATALOG_RETENTION`` of ``incumbent`` (a crawl that finds a
        fraction of the known size is a parser break, not a smaller site).

        Nothing is cached or kept in memory here. The server never crawls:
        ``scripts/update_govbr_catalogs.py`` writes a complete crawl to the
        bundled seed and refuses anything else.
        """
        catalog: dict[str, dict[str, Any]] = {}
        complete = True
        for letter in PCDT_LETTERS:
            urls_to_visit = [f"{PCDT_BASE_URL}/{letter}"]
            visited: set[str] = set()
            while urls_to_visit and len(visited) < MAX_PAGES_PER_LETTER:
                curr_url = urls_to_visit.pop(0)
                if curr_url in visited:
                    continue
                visited.add(curr_url)
                try:
                    resp = await self.http_client.get(curr_url, headers=GOVBR_HEADERS)
                    if resp is None or resp.status_code != 200:
                        logger.warning(
                            "PCDT page %s answered %s",
                            curr_url,
                            getattr(resp, "status_code", None),
                        )
                        complete = False
                        continue
                    items, next_urls = parse_letter_page(resp.text, letter, curr_url)
                except Exception as exc:
                    logger.warning("Error crawling PCDT letter %s at %s: %s", letter, curr_url, exc)
                    complete = False
                    continue
                catalog.update(items)
                for nurl in next_urls:
                    if nurl not in visited and nurl not in urls_to_visit:
                        urls_to_visit.append(nurl)
            if urls_to_visit:
                logger.warning(
                    "PCDT letter %s stopped at the %d-page cap with %d pages queued",
                    letter,
                    MAX_PAGES_PER_LETTER,
                    len(urls_to_visit),
                )
                complete = False

        size_floor = int(len(incumbent or {}) * MIN_CATALOG_RETENTION)
        if not catalog or len(catalog) < size_floor:
            logger.warning(
                "PCDT crawl returned %d rows against %d already held; "
                "incomplete (suspected parser break)",
                len(catalog),
                len(incumbent or {}),
            )
            complete = False
        return catalog, complete

    async def search(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[list[BrazilGuideline], CacheMetadata]:
        """Search PCDT catalog by clinical condition or keyword."""
        query_norm = normalize_text(query)
        query_tokens = tokenize_portuguese(query)
        if not query_norm or not query_tokens:
            return [], CacheMetadata(cached=False, cache_age=0, error=False)

        cache_key = f"govbr_pcdt_search:{CACHE_SCHEMA}:{limit}:{query_norm}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and isinstance(cached_data, list):
            return [BrazilGuideline.from_dict(d) for d in cached_data], meta

        catalog = await self.get_catalog()
        if not catalog:
            # An empty catalog means the bundled seed is missing or empty
            # (the extended corpus alone is never served): an outage, not a
            # zero-result search. Caching it would pin a false success for
            # cache_ttl_brazil_moh and hide the failure from errored_any in
            # brazil_moh.
            logger.warning("PCDT catalog is empty; reporting search error")
            return [], CacheMetadata(
                cached=False, cache_age=0, error=True, error_kind="backend_error"
            )

        scored_items: list[tuple[float, dict[str, Any]]] = []

        for item in catalog.values():
            score = _score_item(query_tokens, query_norm, item)
            if score > 0.0:
                scored_items.append((score, item))

        # Sort by score descending, then title ascending
        scored_items.sort(key=lambda pair: (-pair[0], pair[1].get("title", "")))

        results = [
            _dict_to_guideline(item, score=score)
            for score, item in scored_items[:limit]
        ]

        await self.cache.set(
            cache_key,
            [g.to_dict() for g in results],
            source="govbr_pcdt",
            ttl=self.settings.cache_ttl_brazil_moh,
        )

        return results, CacheMetadata(cached=False, cache_age=0, error=False)

    async def get_guideline(self, record_id: str) -> BrazilGuideline | None:
        """Lookup guideline by record_id or slug."""
        normalized = (record_id or "").strip().lower()
        if not normalized:
            return None

        catalog = await self.get_catalog()

        # Direct match
        if normalized in catalog:
            return _dict_to_guideline(catalog[normalized])

        # Try with or without pcdt- prefix
        if normalized.startswith("pcdt-"):
            slug = normalized[5:]
        else:
            slug = normalized

        prefixed_id = f"pcdt-{slug}"
        if prefixed_id in catalog:
            return _dict_to_guideline(catalog[prefixed_id])

        # Search by slug (case-insensitive: gov.br slugs may carry case
        # that the lowercased input no longer has).
        for item in catalog.values():
            if item.get("slug", "").lower() == slug:
                return _dict_to_guideline(item)

        return None
