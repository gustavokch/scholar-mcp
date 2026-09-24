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
    CACHE_SCHEMA,
    GOVBR_HEADERS,
    MIN_CATALOG_RETENTION,
    is_login_redirect,
    normalize_text,
    parse_folder_index,
    parse_listing_page,
    score_item,
    tokenize_portuguese,
)
from scholar_mcp.medical.models import BrazilGuideline, has_retrievable_body
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

# Folders paginate 20 items per page. 25 pages is ~500 documents per
# folder: far above anything observed, and a hard stop against a
# pagination loop pointing back into itself.
MAX_PAGES_PER_FOLDER = 25

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


def compile_alias_patterns(
    aliases: dict[str, str],
) -> list[tuple[str, re.Pattern[str], str, re.Pattern[str] | None]]:
    """Compile the A-Z alias vocabulary once, for reuse across titles.

    A crawl scores hundreds of items against hundreds of aliases. Building
    the patterns inline overruns ``re``'s internal cache, so every title
    pays for a full recompile of the vocabulary.
    """
    compiled: list[tuple[str, re.Pattern[str], str, re.Pattern[str] | None]] = []
    for slug, disease in aliases.items():
        disease_norm = normalize_text(disease)
        if not disease_norm:
            continue
        slug_norm = normalize_text(slug)
        compiled.append(
            (
                disease_norm,
                _alias_pattern(disease_norm),
                slug_norm,
                _alias_pattern(slug_norm) if slug_norm else None,
            )
        )
    return compiled


def build_alias_text_compiled(
    title: str,
    patterns: list[tuple[str, re.Pattern[str], str, re.Pattern[str] | None]],
) -> str:
    """Return space-joined A-Z aliases that apply to ``title``.

    For each alias, the *counterpart* term is emitted: a title carrying the
    disease name gains the abbreviation, and a title carrying the
    abbreviation gains the disease name.
    """
    title_norm = normalize_text(title)
    if not title_norm:
        return ""
    matched: list[str] = []
    for disease_norm, disease_re, slug_norm, slug_re in patterns:
        disease_matched = bool(disease_re.search(title_norm))
        slug_matched = bool(slug_re.search(title_norm)) if slug_re else False

        if disease_matched:
            if slug_norm not in matched and not slug_matched:
                matched.append(slug_norm)
            elif slug_norm == disease_norm and slug_norm not in matched:
                matched.append(slug_norm)
        elif slug_matched and disease_norm not in matched:
            matched.append(disease_norm)
    return " ".join(matched)


def build_alias_text(title: str, aliases: dict[str, str]) -> str:
    """Compile ``aliases`` and apply them to a single ``title``.

    Convenience wrapper for one-off calls; crawls should compile once with
    ``compile_alias_patterns`` and call ``build_alias_text_compiled``.
    """
    return build_alias_text_compiled(title, compile_alias_patterns(aliases))


_COLLECTION_BY_TREE = {"svsa": "SVSA", "guias": "GUIAS-E-MANUAIS"}


def _dict_to_guideline(item: dict[str, Any], score: float | None = None) -> BrazilGuideline:
    """Convert a catalog row to a BrazilGuideline."""
    collection = _COLLECTION_BY_TREE.get(item.get("tree", ""), "GOVBR")
    document_url = item.get("download_url", "")
    return BrazilGuideline(
        title=item.get("title", ""),
        record_id=item.get("record_id", ""),
        document_url=document_url,
        fulltext_id=item.get("record_id", ""),
        # Same rule as the PCDT converter, via the shared helper
        # (medical.models.has_retrievable_body): a catalog download URL or
        # description text served as the abstract fallback.
        has_full_text=has_retrievable_body(
            document_url,
            fallback_text=item.get("description", ""),
            url_trusted=True,
        ),
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
        """GET ``url`` and return HTML, or None for errors and login gates.

        Every lost page is logged at WARNING: the seed writer refuses an
        incomplete crawl with "see the warnings above", so a silent miss
        would leave the operator nothing to act on.
        """
        try:
            resp = await self.http_client.get(url, headers=GOVBR_HEADERS)
        except Exception as exc:
            logger.warning("gov.br A-Z fetch failed for %s: %s", url, exc)
            return None
        if resp is None or resp.status_code != 200:
            logger.warning(
                "gov.br A-Z page %s answered %s",
                url,
                getattr(resp, "status_code", None),
            )
            return None
        if is_login_redirect(resp.text):
            logger.warning("gov.br A-Z folder is login-gated, skipping: %s", url)
            return None
        return resp.text

    async def _load_aliases(self) -> tuple[dict[str, str], bool]:
        """Build the disease alias vocabulary from the A-Z index.

        Returns ``(aliases, ok)``. ``ok`` is False when the index or any
        letter page failed, or the index listed no letters: rows crawled
        without the full vocabulary lose alias text, which is a partial
        catalog.
        """
        index_html = await self._fetch_html(AZ_INDEX_URL)
        if not index_html:
            return {}, False
        letter_urls = parse_az_index(index_html)
        ok = bool(letter_urls)
        aliases: dict[str, str] = {}
        for letter_url in letter_urls:
            letter_html = await self._fetch_html(letter_url)
            if not letter_html:
                ok = False
                continue
            aliases.update(parse_az_letter_page(letter_html))
        return aliases, ok

    async def _crawl_folder(
        self,
        tree: str,
        folder_url: str,
        patterns: list[tuple[str, re.Pattern[str], str, re.Pattern[str] | None]],
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Crawl one folder with pagination. Returns (rows, ok).

        ``ok`` means every visited page loaded and no page was left queued
        at the page cap. One loaded page is not a loaded folder.
        """
        topic = folder_url.rstrip("/").rsplit("/", 1)[-1]
        rows: dict[str, dict[str, Any]] = {}
        queue = [folder_url]
        visited: set[str] = set()
        failed = False

        while queue and len(visited) < MAX_PAGES_PER_FOLDER:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            html = await self._fetch_html(url)
            if html is None:
                failed = True
                continue
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
                    "aliases": build_alias_text_compiled(
                        f"{item['title']} {topic}", patterns
                    ),
                    "view_url": item["view_url"],
                    "download_url": item["download_url"],
                }
            for nurl in next_urls:
                if nurl not in visited and nurl not in queue:
                    queue.append(nurl)

        if queue:
            logger.warning(
                "gov.br A-Z folder %s stopped at the %d-page cap with %d pages queued",
                folder_url,
                MAX_PAGES_PER_FOLDER,
                len(queue),
            )
        return rows, not failed and not queue

    async def refresh_catalog(
        self, incumbent: dict[str, dict[str, Any]] | None = None
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Crawl both publication trees from gov.br. Offline use only.

        Returns ``(catalog, complete)``. ``complete`` is True only when the
        alias vocabulary, both tree indexes, and every page of every folder
        loaded, no folder stopped at the page cap, and the catalog holds at
        least ``MIN_CATALOG_RETENTION`` of ``incumbent`` (a crawl that finds
        a fraction of the known size is a parser break, not a smaller site).

        Nothing is cached or kept in memory here. The server never crawls:
        ``scripts/update_govbr_catalogs.py`` writes a complete crawl to the
        bundled seed and refuses anything else.
        """
        aliases, complete = await self._load_aliases()
        if not complete:
            logger.warning("gov.br A-Z alias vocabulary is incomplete")
        # Compiled once here and reused for every item in every folder.
        patterns = compile_alias_patterns(aliases)
        catalog: dict[str, dict[str, Any]] = {}

        for tree, tree_url, tree_path in _TREES:
            index_html = await self._fetch_html(tree_url)
            if index_html is None:
                logger.warning("gov.br A-Z tree index unavailable: %s", tree_url)
                complete = False
                continue
            folder_urls = parse_folder_index(index_html, tree_url, tree_path)
            if not folder_urls:
                logger.warning("gov.br A-Z tree index lists no folders: %s", tree_url)
                complete = False
            for folder_url in folder_urls:
                rows, ok = await self._crawl_folder(tree, folder_url, patterns)
                catalog.update(rows)
                complete = complete and ok

        size_floor = int(len(incumbent or {}) * MIN_CATALOG_RETENTION)
        if not catalog or len(catalog) < size_floor:
            logger.warning(
                "gov.br A-Z crawl returned %d rows against %d already held; "
                "incomplete (suspected parser break)",
                len(catalog),
                len(incumbent or {}),
            )
            complete = False
        return catalog, complete

    async def get_catalog(self) -> dict[str, dict[str, Any]]:
        """Get the catalog from memory or the bundled seed.

        The seed is the catalog: no search crawls gov.br, and nothing here
        touches the network or the SQLite cache.
        ``scripts/update_govbr_catalogs.py --catalog az`` regenerates the
        seed offline. A missing or empty seed returns ``{}`` and is not
        kept, so ``search`` reports the outage.
        """
        if self._memory_catalog:
            return self._memory_catalog

        seed = load_seed_catalog()
        if not seed:
            logger.error(
                "gov.br A-Z seed catalog is missing or empty; reporting an outage"
            )
            return {}
        self._memory_catalog = seed
        return seed

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

        cache_key = f"govbr_az_search:{CACHE_SCHEMA}:{limit}:{query_norm}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and isinstance(cached_data, list):
            return [BrazilGuideline.from_dict(d) for d in cached_data], meta

        catalog = await self.get_catalog()
        if not catalog:
            # An empty catalog means the bundled seed is missing or empty:
            # an outage, not a zero-result search. Caching it would pin a
            # false success for cache_ttl_brazil_moh and hide the failure
            # from errored_any in brazil_moh.
            logger.warning("gov.br A-Z catalog is empty; reporting search error")
            return [], CacheMetadata(
                cached=False, cache_age=0, error=True, error_kind="backend_error"
            )

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
