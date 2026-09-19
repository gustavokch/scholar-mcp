"""Scraper for Brazilian MoH manuals published behind "Saúde de A a Z".

The A-Z index at ``/assuntos/saude-de-a-a-z`` hosts no PDFs: its disease
pages cross-link with ``resolveuid`` URLs and their ``publicacoes`` folders
are login-gated. The manuals themselves (Dengue clinical management,
Tuberculosis control, and the rest of the surveillance set) are published
under ``/centrais-de-conteudo/publicacoes/svsa/<topic>`` and
``/centrais-de-conteudo/publicacoes/guias-e-manuais/<year>``. This module
crawls those trees, and uses the A-Z index only as a vocabulary that maps
official abbreviations (``dtha``, ``dcj``, ``dda``) to disease names.
"""

import json
import logging
from pathlib import Path
import re
from typing import Any

from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.govbr_common import (
    GOVBR_HEADERS,
    SEVEN_DAYS_SECONDS,
    is_login_redirect,
    normalize_text,
    parse_folder_index,
    parse_listing_page,
    score_item,
    tokenize_portuguese,
)
from scholar_mcp.medical.models import BrazilGuideline
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

logger = logging.getLogger(__name__)

GOVBR_ROOT = "https://www.gov.br"
AZ_INDEX_PATH = "/saude/pt-br/assuntos/saude-de-a-a-z"
AZ_INDEX_URL = f"{GOVBR_ROOT}{AZ_INDEX_PATH}"

SVSA_PATH = "/saude/pt-br/centrais-de-conteudo/publicacoes/svsa"
GUIAS_PATH = "/saude/pt-br/centrais-de-conteudo/publicacoes/guias-e-manuais"
SVSA_URL = f"{GOVBR_ROOT}{SVSA_PATH}"
GUIAS_URL = f"{GOVBR_ROOT}{GUIAS_PATH}"
CATALOG_CACHE_KEY = "govbr_az:catalog"

# Folders paginate 20 items per page. 25 pages is ~500 documents per
# folder: far above anything observed, and a hard stop against a
# pagination loop pointing back into itself.
MAX_PAGES_PER_FOLDER = 25

# A refreshed crawl that returns less than this fraction of the catalog
# already in hand is treated as a parser break, not as a smaller site, and
# is never pinned for the 7-day TTL. scripts/update_govbr_az_catalog.py
# applies the same idea with an absolute floor for the bundled seed.
MIN_CATALOG_RETENTION = 0.5

_TREES = (("svsa", SVSA_URL, SVSA_PATH), ("guias", GUIAS_URL, GUIAS_PATH))


def load_seed_catalog() -> dict[str, dict[str, Any]]:
    """Load the bundled pre-scraped A-Z publication catalog."""
    seed_path = Path(__file__).resolve().parent.parent / "data" / "govbr_az_catalog.json"
    if not seed_path.exists():
        logger.warning("gov.br A-Z seed catalog not found at %s", seed_path)
        return {}
    try:
        with seed_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("Failed to load gov.br A-Z seed catalog: %s", exc)
        return {}


def parse_az_index(html: str) -> list[str]:
    """Return the letter page URLs of the A-Z index."""
    return parse_folder_index(html, AZ_INDEX_URL, AZ_INDEX_PATH)


def parse_az_letter_page(html: str) -> dict[str, str]:
    """Map disease slug to display title for one A-Z letter page."""
    soup = BeautifulSoup(html, "html.parser")
    entries: dict[str, str] = {}
    for anchor in soup.find_all("a", class_="govbr-card-content", href=True):
        title_node = anchor.find("span", class_="titulo")
        title = (
            title_node.get_text(strip=True)
            if title_node
            else anchor.get_text(strip=True)
        )
        slug = anchor["href"].strip().split("?")[0].rstrip("/").rsplit("/", 1)[-1]
        if not title or not slug or slug == AZ_INDEX_PATH.rsplit("/", 1)[-1]:
            continue
        if len(slug) <= 1:
            continue
        entries[slug] = title
    return entries


def _alias_pattern(term: str) -> re.Pattern[str]:
    """Word-boundary match for ``term`` that tolerates a plural suffix.

    Anchoring on word boundaries keeps a short abbreviation such as "dtha"
    from matching inside "widthas". The optional trailing "s" keeps the
    match working against the pluralized titles gov.br actually publishes
    ("Manual das Hepatites Virais" for the alias "hepatite").
    """
    return re.compile(r"\b" + re.escape(term) + r"s?\b")


def build_alias_text(title: str, aliases: dict[str, str]) -> str:
    """Return space-joined A-Z aliases that apply to ``title``.

    An alias applies when the disease name appears in the document title,
    so "Manual ... da Tuberculose" also scores for the query "tuberculose"
    and for abbreviations such as "dtha".
    """
    title_norm = normalize_text(title)
    if not title_norm:
        return ""
    matched: list[str] = []
    for slug, disease in aliases.items():
        disease_norm = normalize_text(disease)
        slug_norm = normalize_text(slug)
        if not disease_norm:
            continue
        disease_matched = bool(_alias_pattern(disease_norm).search(title_norm))
        slug_matched = bool(_alias_pattern(slug_norm).search(title_norm)) if slug_norm else False

        if disease_matched:
            if slug_norm not in matched and not slug_matched:
                matched.append(slug_norm)
            elif slug_norm == disease_norm and slug_norm not in matched:
                matched.append(slug_norm)
        elif slug_matched:
            if disease_norm not in matched and not disease_matched:
                matched.append(disease_norm)
    return " ".join(matched)


_COLLECTION_BY_TREE = {"svsa": "SVSA", "guias": "GUIAS-E-MANUAIS"}


def _dict_to_guideline(item: dict[str, Any], score: float | None = None) -> BrazilGuideline:
    """Convert a catalog row to a BrazilGuideline."""
    collection = _COLLECTION_BY_TREE.get(item.get("tree", ""), "GOVBR")
    return BrazilGuideline(
        title=item.get("title", ""),
        record_id=item.get("record_id", ""),
        document_url=item.get("download_url", ""),
        fulltext_id=item.get("record_id", ""),
        source="brazil-moh",
        abstract=item.get("description", ""),
        year=item.get("year", ""),
        country="Brasil",
        languages=["pt"],
        collections=[collection],
        authors=["Ministério da Saúde"],
        score=score,
    )


class GovBrAZEngine:
    """Catalog crawler and search engine for gov.br publication trees."""

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

    async def _fetch_html(self, url: str) -> str | None:
        """GET ``url`` and return HTML, or None for errors and login gates."""
        try:
            resp = await self.http_client.get(url, headers=GOVBR_HEADERS)
        except Exception as exc:
            logger.warning("gov.br A-Z fetch failed for %s: %s", url, exc)
            return None
        if resp is None or resp.status_code != 200:
            return None
        if is_login_redirect(resp.text):
            logger.info("gov.br A-Z folder is login-gated, skipping: %s", url)
            return None
        return resp.text

    async def _load_aliases(self) -> dict[str, str]:
        """Build the disease alias vocabulary from the A-Z index."""
        index_html = await self._fetch_html(AZ_INDEX_URL)
        if not index_html:
            return {}
        aliases: dict[str, str] = {}
        for letter_url in parse_az_index(index_html):
            letter_html = await self._fetch_html(letter_url)
            if not letter_html:
                continue
            aliases.update(parse_az_letter_page(letter_html))
        return aliases

    async def _crawl_folder(
        self, tree: str, folder_url: str, aliases: dict[str, str]
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Crawl one folder with pagination. Returns (rows, ok)."""
        topic = folder_url.rstrip("/").rsplit("/", 1)[-1]
        rows: dict[str, dict[str, Any]] = {}
        queue = [folder_url]
        visited: set[str] = set()
        ok = False

        while queue and len(visited) < MAX_PAGES_PER_FOLDER:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            html = await self._fetch_html(url)
            if html is None:
                continue
            ok = True
            items, next_urls = parse_listing_page(html, url)
            for item in items:
                record_id = f"govbr-{tree}-{topic}-{item['slug']}"
                rows[record_id] = {
                    "record_id": record_id,
                    "slug": item["slug"],
                    "title": item["title"],
                    "description": item["description"],
                    "tree": tree,
                    "topic": topic,
                    "year": topic if tree == "guias" and topic.isdigit() else "",
                    "aliases": build_alias_text(
                        f"{item['title']} {topic}", aliases
                    ),
                    "view_url": item["view_url"],
                    "download_url": item["download_url"],
                }
            for nurl in next_urls:
                if nurl not in visited and nurl not in queue:
                    queue.append(nurl)

        return rows, ok

    async def refresh_catalog(
        self, incumbent: dict[str, dict[str, Any]] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Crawl both publication trees from gov.br.

        ``incumbent`` is the catalog already in hand, if any. It is used only
        as a size reference: a crawl that returns a fraction of it is a
        parser break rather than a smaller site, and must not be pinned.
        """
        aliases = await self._load_aliases()
        catalog: dict[str, dict[str, Any]] = {}
        folders_total = 0
        folders_ok = 0

        for tree, tree_url, tree_path in _TREES:
            index_html = await self._fetch_html(tree_url)
            if index_html is None:
                logger.warning("gov.br A-Z tree index unavailable: %s", tree_url)
                folders_total += 1  # a missing index is a failed unit of work
                continue
            folder_urls = parse_folder_index(index_html, tree_url, tree_path)
            for folder_url in folder_urls:
                folders_total += 1
                rows, ok = await self._crawl_folder(tree, folder_url, aliases)
                catalog.update(rows)
                if ok:
                    folders_ok += 1

        # Cache only a fully-crawled catalog: a partial crawl pinned with
        # the 7-day TTL would serve an incomplete catalog for a week while
        # gov.br is flaky. Partial results are returned for this call but
        # leave the cached and in-memory catalogs untouched.
        #
        # Folder-level success is not enough on its own: a DOM change makes
        # every folder answer 200 while the listing parser matches nothing,
        # which would otherwise pin a near-empty catalog over a good one.
        size_floor = int(len(incumbent or {}) * MIN_CATALOG_RETENTION)
        if (
            catalog
            and len(catalog) >= size_floor
            and folders_total > 0
            and folders_ok == folders_total
        ):
            self._memory_catalog = catalog
            await self.cache.set(
                CATALOG_CACHE_KEY,
                catalog,
                source="govbr_az",
                ttl=SEVEN_DAYS_SECONDS,
            )
        elif catalog and len(catalog) < size_floor:
            logger.warning(
                "gov.br A-Z crawl returned %d rows against %d already held; "
                "not caching (suspected parser break)",
                len(catalog),
                len(incumbent or {}),
            )
        return catalog

    async def get_catalog(self) -> dict[str, dict[str, Any]]:
        """Get the catalog from memory, cache, or the bundled seed."""
        if self._memory_catalog:
            return self._memory_catalog

        cached_data, meta = await self.cache.get(CATALOG_CACHE_KEY)
        if meta.cached and isinstance(cached_data, dict) and cached_data:
            if meta.cache_age < SEVEN_DAYS_SECONDS:
                self._memory_catalog = cached_data
                return self._memory_catalog
            try:
                refreshed = await self.refresh_catalog(incumbent=cached_data)
                if refreshed:
                    # refresh_catalog only writes the cache for a complete
                    # crawl. A partial crawl must still be memoized here, or
                    # every subsequent search re-crawls both publication trees
                    # for as long as gov.br is degraded.
                    self._memory_catalog = refreshed
                    return refreshed
            except Exception as exc:
                logger.warning("Failed to refresh gov.br A-Z catalog: %s", exc)
            self._memory_catalog = cached_data
            return self._memory_catalog

        seed = load_seed_catalog()
        if seed:
            self._memory_catalog = seed
            await self.cache.set(
                CATALOG_CACHE_KEY,
                seed,
                source="govbr_az",
                ttl=SEVEN_DAYS_SECONDS,
            )
            return self._memory_catalog

        try:
            crawled = await self.refresh_catalog()
            if crawled:
                # Same reasoning as the stale-cache path: memoize even a
                # partial crawl so a degraded gov.br is not re-crawled once
                # per search.
                self._memory_catalog = crawled
                return crawled
        except Exception as exc:
            logger.warning("Failed initial crawl of gov.br A-Z catalog: %s", exc)

        return {}

    async def search(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[list[BrazilGuideline], CacheMetadata]:
        """Search the publication catalog by disease, topic, or keyword."""
        query_norm = normalize_text(query)
        query_tokens = tokenize_portuguese(query)
        if not query_norm or not query_tokens:
            return [], CacheMetadata(cached=False, cache_age=0, error=False)

        cache_key = f"govbr_az_search:{limit}:{query_norm}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and isinstance(cached_data, list):
            return [BrazilGuideline.from_dict(d) for d in cached_data], meta

        catalog = await self.get_catalog()
        if not catalog:
            # An empty catalog means the seed is missing and every crawl
            # failed: an outage, not a zero-result search. Caching it would
            # pin a false success for cache_ttl_brazil_moh and hide the
            # failure from errored_any in brazil_moh.
            logger.warning("gov.br A-Z catalog is empty; reporting search error")
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        scored_items: list[tuple[float, dict[str, Any]]] = []
        for item in catalog.values():
            score = score_item(
                query_tokens,
                query_norm,
                item.get("title", ""),
                item.get("slug", ""),
                f"{item.get('topic', '')} {item.get('aliases', '')}",
            )
            if score > 0.0:
                scored_items.append((score, item))

        scored_items.sort(key=lambda pair: (-pair[0], pair[1].get("title", "")))
        results = [
            _dict_to_guideline(item, score=score)
            for score, item in scored_items[:limit]
        ]

        await self.cache.set(
            cache_key,
            [g.to_dict() for g in results],
            source="govbr_az",
            ttl=self.settings.cache_ttl_brazil_moh,
        )
        return results, CacheMetadata(cached=False, cache_age=0, error=False)

    async def get_guideline(self, record_id: str) -> BrazilGuideline | None:
        """Look up one publication by record_id or slug."""
        normalized = (record_id or "").strip().lower()
        if not normalized:
            return None

        catalog = await self.get_catalog()
        if normalized in catalog:
            return _dict_to_guideline(catalog[normalized])

        for item in catalog.values():
            if item.get("slug", "").lower() == normalized:
                return _dict_to_guideline(item)
            if item.get("record_id", "").lower() == normalized:
                return _dict_to_guideline(item)

        return None
