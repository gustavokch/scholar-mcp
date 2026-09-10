"""Scraper and search engine for official Brazilian SUS PCDT guidelines from gov.br.

Retrieves clinical guidelines (Protocolos Clínicos e Diretrizes Terapêuticas - PCDT)
directly from the Ministry of Health portal:
https://www.gov.br/saude/pt-br/assuntos/pcdt/[a-u]/[condition]
"""

from collections.abc import Callable
import json
import logging
from pathlib import Path
import re
from typing import Any
import unicodedata
import urllib.parse

from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import BrazilGuideline
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

logger = logging.getLogger(__name__)

PCDT_BASE_URL = "https://www.gov.br/saude/pt-br/assuntos/pcdt"
PCDT_LETTERS = (
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "l",
    "m", "n", "o", "p", "r", "s", "t", "u",
)

SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60  # 604,800 seconds

GOVBR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

PORTUGUESE_STOPWORDS = frozenset(
    {
        "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do",
        "dos", "e", "em", "entre", "na", "nao", "nas", "no", "nos", "o",
        "os", "ou", "para", "pela", "pelo", "por", "que", "se", "sem",
        "sob", "sobre", "um", "uma", "umas", "uns",
    }
)

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def normalize_text(text: str | None) -> str:
    """Normalize text by folding accents, stripping non-ASCII characters, and lowercasing."""
    if not text:
        return ""
    folded = (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return folded.strip()


def tokenize_portuguese(text: str | None) -> list[str]:
    """Tokenize Portuguese text into substantive search terms."""
    norm = normalize_text(text)
    if not norm:
        return []
    return [
        tok
        for tok in _WORD_SPLIT_RE.split(norm)
        if len(tok) >= 2 and tok not in PORTUGUESE_STOPWORDS
    ]


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
    if not query_tokens:
        return 0.0

    title_norm = normalize_text(item.get("title", ""))
    slug_norm = normalize_text(item.get("slug", "")).replace("-", " ")

    # Exact match
    if query_norm == title_norm or query_norm == slug_norm:
        return 1.0

    # Substring match
    if query_norm in title_norm or query_norm in slug_norm:
        return 0.90

    # Title or slug starts with query
    if title_norm.startswith(query_norm) or slug_norm.startswith(query_norm):
        return 0.85

    title_tokens = set(tokenize_portuguese(item.get("title", "")))
    slug_tokens = set(tokenize_portuguese(item.get("slug", "").replace("-", " ")))
    all_item_tokens = title_tokens | slug_tokens

    if not all_item_tokens:
        return 0.0

    matching_tokens = [tok for tok in query_tokens if tok in all_item_tokens]
    if not matching_tokens:
        return 0.0

    token_ratio = len(matching_tokens) / len(query_tokens)

    # All terms matched
    if token_ratio == 1.0:
        return 0.80

    return 0.50 * token_ratio


def _dict_to_guideline(item: dict[str, Any], score: float | None = None) -> BrazilGuideline:
    """Convert catalog dictionary to BrazilGuideline dataclass."""
    return BrazilGuideline(
        title=item.get("title", ""),
        record_id=item.get("record_id", ""),
        document_url=item.get("download_url", ""),
        fulltext_id=item.get("record_id", ""),
        source="brazil-moh",
        abstract=item.get("description", ""),
        country="Brasil",
        languages=["pt"],
        collections=["PCDT"],
        authors=["Ministério da Saúde", "CONITEC"],
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
        """Get PCDT catalog from memory, cache, or seed fallback.

        Refreshes catalog if cached entry is older than 7 days.
        """
        if self._memory_catalog:
            return self._memory_catalog

        cache_key = "govbr_pcdt:catalog"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and isinstance(cached_data, dict) and cached_data:
            if meta.cache_age < SEVEN_DAYS_SECONDS:
                self._memory_catalog = cached_data
                return self._memory_catalog
            # Cache entry is older than 7 days. Try refresh.
            try:
                refreshed = await self.refresh_catalog()
                if refreshed:
                    return refreshed
            except Exception as exc:
                logger.warning("Failed to refresh PCDT catalog: %s", exc)
            self._memory_catalog = cached_data
            return self._memory_catalog

        # Not cached in SQLite. Use bundled seed catalog on cold start.
        seed = load_seed_catalog()
        if seed:
            self._memory_catalog = seed
            await self.cache.set(
                cache_key,
                seed,
                source="govbr_pcdt",
                ttl=SEVEN_DAYS_SECONDS,
            )
            return self._memory_catalog

        # If no seed, try online crawl.
        try:
            crawled = await self.refresh_catalog()
            if crawled:
                return crawled
        except Exception as exc:
            logger.warning("Failed initial crawl of PCDT catalog: %s", exc)

        return {}

    async def refresh_catalog(self) -> dict[str, dict[str, Any]]:
        """Crawl fresh catalog from gov.br portal."""
        catalog: dict[str, dict[str, Any]] = {}
        letters_ok = 0
        for letter in PCDT_LETTERS:
            urls_to_visit = [f"{PCDT_BASE_URL}/{letter}"]
            visited: set[str] = set()
            letter_ok = False
            while urls_to_visit:
                curr_url = urls_to_visit.pop(0)
                if curr_url in visited:
                    continue
                visited.add(curr_url)
                try:
                    resp = await self.http_client.get(curr_url, headers=GOVBR_HEADERS)
                    if resp is None or resp.status_code != 200:
                        continue
                    letter_ok = True
                    items, next_urls = parse_letter_page(resp.text, letter, curr_url)
                    catalog.update(items)
                    for nurl in next_urls:
                        if nurl not in visited and nurl not in urls_to_visit:
                            urls_to_visit.append(nurl)
                except Exception as exc:
                    logger.warning("Error crawling PCDT letter %s at %s: %s", letter, curr_url, exc)
            if letter_ok:
                letters_ok += 1

        # Cache only a fully-crawled catalog: a partial crawl pinned with
        # the 7-day TTL would serve an incomplete catalog for a week while
        # gov.br is flaky. Partial results are returned for this call but
        # leave the cached and in-memory catalogs untouched.
        if catalog and letters_ok == len(PCDT_LETTERS):
            self._memory_catalog = catalog
            await self.cache.set(
                "govbr_pcdt:catalog",
                catalog,
                source="govbr_pcdt",
                ttl=SEVEN_DAYS_SECONDS,
            )
        return catalog

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

        cache_key = f"govbr_pcdt_search:{limit}:{query_norm}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and isinstance(cached_data, list):
            return [BrazilGuideline.from_dict(d) for d in cached_data], meta

        catalog = await self.get_catalog()
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
