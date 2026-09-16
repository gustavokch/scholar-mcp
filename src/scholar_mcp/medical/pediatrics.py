import asyncio
import logging
import random
import re
from urllib.parse import urlencode
from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import MedicalArticle, PediatricGuideline
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

AAP_BASE = "https://publications.aap.org"
# AAP moved search to a Solr-backed /search-results page; the old
# /pediatrics/search endpoint 404s.
AAP_URL = "https://publications.aap.org/pediatrics/search-results"

AAP_ITEM_SELECTORS = ".item-container, .search-result, .result-item, .article-item, article, .publication-item"
# select_one() with a comma list matches in document order, not selector
# order, so priority has to be expressed by trying one selector at a time.
TITLE_SELECTORS: tuple[str, ...] = (
    ".sri-title h4",
    "h4",
    "h2",
    "h3",
    ".title",
    "a.title",
)


def _select_title_el(item):
    for selector in TITLE_SELECTORS:
        el = item.select_one(selector)
        if el is not None:
            return el
    return None


def _first_href_anchor(*scopes):
    """First anchor that actually carries an href, in scope order."""
    for scope in scopes:
        if scope is None:
            continue
        for anchor in scope.find_all("a"):
            if anchor.get("href"):
                return anchor
    return None


def _looks_like_challenge(content: str) -> bool:
    lowered = content.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)
DESC_SELECTORS = ".description, .summary, .abstract, p"

AGE_RANGE_RE = re.compile(
    r"(\d+\s*(?:-|\s*to\s*)\s*\d+\s*(?:months?|years?|days?))",
    re.IGNORECASE,
)
AGE_TERM_RE = re.compile(
    r"(infant|toddler|preschool|school-age|adolescent|under-five|under 5|neonatal|newborn)",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# Browser-fallback budget. The AAP host sits behind Cloudflare, so the
# challenge needs time to settle, but search_aap_guidelines is an MCP tool
# with no caller-side ceiling — mirror scihub.py's bounded camoufox block.
_NAV_TIMEOUT_MS = 15000
_CHALLENGE_SETTLE_MS = 5000
_POST_RENAV_SETTLE_MS = 3000
_CAMOUFOX_TOTAL_TIMEOUT_S = 45.0

# Cloudflare interstitial markers. The title text is localised; the body
# carries stable platform divs.
_CHALLENGE_MARKERS = (
    "just a moment",
    "challenge-platform",
    "cf-browser-verification",
)

logger = logging.getLogger(__name__)


def _extract_age_group(text: str) -> str:
    if not text:
        return ""
    m_range = AGE_RANGE_RE.search(text)
    if m_range:
        return m_range.group(0)
    m_term = AGE_TERM_RE.search(text)
    if m_term:
        return m_term.group(0)
    return ""

PEDIATRIC_JOURNALS = [
    "Pediatrics",
    "JAMA Pediatrics",
    "The Journal of Pediatrics",
    "Pediatric Research",
    "Archives of Disease in Childhood",
    "European Journal of Pediatrics",
    "Pediatric Clinics of North America",
]

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


class PediatricsEngine:
    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
        pubmed: MedicalPubMedClient | None = None,
        jitter_range: tuple[float, float] | None = (1.0, 3.0),
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings
        self.pubmed = pubmed or MedicalPubMedClient(http_client, cache, settings)
        self.jitter_range = jitter_range

    @staticmethod
    def _matches_query(title: str, query_tokens: set[str]) -> bool:
        """True when the title shares at least one word with the query."""
        return bool(query_tokens & set(re.findall(r"\w+", title.lower())))

    def _filter_matches(
        self, results: list[PediatricGuideline], query: str
    ) -> list[PediatricGuideline]:
        """Drop SPA navigation junk whose title shares no word with the query."""
        query_tokens = set(re.findall(r"\w+", query.lower()))
        return [g for g in results if self._matches_query(g.title, query_tokens)]

    def _parse_guideline_items(
        self,
        html_text: str,
        item_selectors: str,
        base_url: str,
        source: str,
    ) -> list[PediatricGuideline]:
        soup = BeautifulSoup(html_text, "html.parser")
        guidelines: list[PediatricGuideline] = []

        for item in soup.select(item_selectors):
            title_el = _select_title_el(item)
            # Raw get_text() keeps the document whitespace between nested
            # Solr highlight nodes (so <strong>Oppositional</strong>
            # <strong>Defiant</strong> stays separated) without inventing a
            # space where a highlight splits a word mid-token
            # (<strong>Oppo</strong>sitional); the collapse below tidies
            # newlines and indentation.
            title = (
                re.sub(r"\s+", " ", title_el.get_text()).strip()
                if title_el
                else ""
            )
            if not title or len(title) <= 10:
                continue

            link = _first_href_anchor(title_el, item)
            href = link.get("href", "") if link else ""
            if href:
                item_url = href if href.startswith("http") else (base_url.rstrip("/") + "/" + href.lstrip("/"))
            else:
                item_url = base_url

            desc_el = item.select_one(DESC_SELECTORS)
            description = (desc_el.get_text(" ", strip=True) if desc_el else "")[:300]

            age_group = _extract_age_group(title) or _extract_age_group(description)

            m_year = YEAR_RE.search(title) or (YEAR_RE.search(description) if description else None)
            year = m_year.group(0) if m_year else ""

            category = "Preventive Care" if source == "bright-futures" else "Policy Statement"

            guidelines.append(
                PediatricGuideline(
                    title=title,
                    organization="American Academy of Pediatrics",
                    url=item_url,
                    source=source,
                    year=year,
                    description=description,
                    age_group=age_group,
                    category=category,
                )
            )
        return guidelines

    async def _scrape_html(
        self,
        url: str,
        params: dict[str, str],
        item_selectors: str,
        base_url: str,
        source: str,
    ) -> tuple[list[PediatricGuideline], bool]:
        """Scrape one guideline source.

        Returns the guidelines and whether the fetch failed, so callers can
        distinguish an unreachable source from one that genuinely has no match.
        """
        if self.jitter_range:
            await asyncio.sleep(random.uniform(*self.jitter_range))

        try:
            resp = await self.http_client.get(
                url,
                params=params,
                headers={"User-Agent": BROWSER_UA},
            )
            if resp is None:
                raise FetchError("guideline page request failed")
            html_text = resp.text
        except Exception:
            logger.warning("Pediatric guideline scrape failed for %s", url, exc_info=True)
            return [], True

        return self._parse_guideline_items(html_text, item_selectors, base_url, source), False

    @staticmethod
    async def _settle(page, result_selector: str, timeout_ms: int) -> None:
        """Block until result items render.

        wait_for_selector already blocks for up to timeout_ms, so a miss needs
        no extra sleep: it means a challenge page or markup we do not
        recognise, which the caller detects from the content itself."""
        try:
            await page.wait_for_selector(result_selector, timeout=timeout_ms)
        except Exception:
            logger.debug(
                "No %s within %dms; treating the page as unrendered",
                result_selector,
                timeout_ms,
                exc_info=True,
            )

    async def _camoufox_scrape(
        self,
        url: str,
        query: str,
        item_selectors: str,
        base_url: str,
        source: str,
    ) -> list[PediatricGuideline]:
        """Last-resort rendered-HTML scrape for one guideline source.

        Camoufox (anti-detection Firefox) because the AAP hosts sit behind
        Cloudflare and 403 plain HTTP clients. It manages its own coherent
        fingerprint, so no custom user agent is sent."""
        from camoufox.async_api import AsyncCamoufox

        target = f"{url}?{urlencode({'q': query})}"

        async def _run() -> list[PediatricGuideline]:
            async with AsyncCamoufox(headless=True) as browser:
                page = await browser.new_page()
                await page.goto(
                    target, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
                )
                # The Cloudflare interstitial auto-redirects to a mangled URL
                # ("?autologincheck=redirected" appended to the query) that
                # 404s. Once the challenge clears, its cookie is set and a
                # clean re-navigation reaches the real results page.
                await self._settle(page, item_selectors, _CHALLENGE_SETTLE_MS)
                content = await page.content()
                first = self._parse_guideline_items(
                    content, item_selectors, base_url, source
                )
                if page.url == target and not _looks_like_challenge(content):
                    return first
                await page.goto(
                    target, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
                )
                await self._settle(page, item_selectors, _POST_RENAV_SETTLE_MS)
                content = await page.content()
                second = self._parse_guideline_items(
                    content, item_selectors, base_url, source
                )
            return second or first

        return await asyncio.wait_for(_run(), timeout=_CAMOUFOX_TOTAL_TIMEOUT_S)

    async def _pubmed_guidelines(self, query: str) -> list[PediatricGuideline]:
        """AAP-filtered guideline search over PubMed publication types.

        Fallback for the AAP-hosted scrapes, which sit behind Cloudflare and
        fail with 403 for plain HTTP clients."""
        from scholar_mcp.medical.guidelines import GuidelinesEngine

        engine = GuidelinesEngine(self.pubmed, self.cache, self.settings)
        found, _meta = await engine.search_clinical_guidelines(
            query, organization="AAP"
        )
        out: list[PediatricGuideline] = []
        for g in found:
            out.append(
                PediatricGuideline(
                    title=g.title,
                    organization=g.organization or "American Academy of Pediatrics",
                    url=g.url,
                    source="pubmed-aap",
                    year=str(g.year or ""),
                    description=g.description,
                    age_group=_extract_age_group(f"{g.title} {g.description}"),
                    category="Policy Statement",
                )
            )
        return out

    async def search_bright_futures(
        self,
        query: str,
    ) -> tuple[list[PediatricGuideline], CacheMetadata]:
        cache_key = f"bright_futures:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [PediatricGuideline.from_dict(d) for d in cached_data], meta

        # The BF ?q= endpoint is dead: it ignores the query and returns
        # static nav HTML. Go straight to the PubMed organization=AAP
        # search instead of hitting an endpoint that never searches.
        try:
            results = await self._pubmed_guidelines(query)
        except Exception:
            logger.warning(
                "PubMed bright-futures fallback failed for %r", query,
                exc_info=True,
            )
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [g.to_dict() for g in results],
            source="bright_futures",
        )
        return results, CacheMetadata(cached=False, cache_age=0)

    async def search_aap_policy(
        self,
        query: str,
    ) -> tuple[list[PediatricGuideline], CacheMetadata]:
        cache_key = f"aap_policy:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [PediatricGuideline.from_dict(d) for d in cached_data], meta

        results, errored = await self._scrape_html(
            AAP_URL,
            {"q": query},
            AAP_ITEM_SELECTORS,
            AAP_BASE,
            "aap-policy",
        )
        results = self._filter_matches(results, query)

        if errored:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [g.to_dict() for g in results],
            source="aap_policy",
        )
        return results, CacheMetadata(cached=False, cache_age=0)

    async def search_aap_guidelines(
        self,
        query: str,
    ) -> tuple[list[PediatricGuideline], CacheMetadata]:
        cache_key = f"aap_guidelines:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [PediatricGuideline.from_dict(d) for d in cached_data], meta

        bf_res, aap_res = await asyncio.gather(
            self.search_bright_futures(query),
            self.search_aap_policy(query),
            return_exceptions=True,
        )

        all_items: list[PediatricGuideline] = []
        errored = False
        for res in (bf_res, aap_res):
            if isinstance(res, tuple):
                all_items.extend(res[0])
                errored = errored or res[1].error
            else:
                # gather returned the exception instead of a result
                logger.warning("Pediatric guideline sub-search raised", exc_info=res)
                errored = True

        seen: set[str] = set()
        deduped: list[PediatricGuideline] = []
        for g in all_items:
            norm = re.sub(r"[^\w\s]", "", g.title.lower())
            if norm not in seen:
                seen.add(norm)
                deduped.append(g)

        # The AAP-hosted search pages are SPAs: with or without JS they render
        # static navigation items regardless of the query. Drop items that
        # share no word with the query so they neither pollute results nor
        # block the PubMed fallback below.
        deduped = self._filter_matches(deduped, query)

        # Fallback chain when both scrapes came up empty: PubMed
        # publication-type search filtered to AAP first (reliable, no
        # anti-bot wall), Playwright last resort.
        if not deduped:
            try:
                deduped = await self._pubmed_guidelines(query)
                if deduped:
                    errored = False
            except Exception:
                logger.warning(
                    "PubMed pediatric guideline fallback failed for %r", query,
                    exc_info=True,
                )

        if not deduped and self.settings.enable_browser_fallback:
            try:
                browser_items = await self._camoufox_scrape(
                    AAP_URL, query, AAP_ITEM_SELECTORS, AAP_BASE, "aap-policy"
                )
            except Exception:
                logger.warning(
                    "Browser fallback failed for %s", AAP_URL, exc_info=True
                )
                browser_items = []
            if browser_items:
                deduped = []
                browser_seen: set[str] = set()
                for g in self._filter_matches(browser_items, query):
                    norm = re.sub(r"[^\w\s]", "", g.title.lower())
                    if norm not in browser_seen:
                        browser_seen.add(norm)
                        deduped.append(g)
                errored = False

        if deduped and all(g.source == "pubmed-aap" for g in deduped):
            # Everything came from PubMed, which never touches the
            # Cloudflare-walled AAP host: the scrape error is stale.
            errored = False

        if errored and not deduped:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [g.to_dict() for g in deduped],
            source="guidelines",
        )
        return deduped, CacheMetadata(cached=False, cache_age=0, error=errored)

    async def search_pediatric_literature(
        self,
        query: str,
        max_results: int = 10,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        cache_key = f"pediatric_journals:{query}:{max_results}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [MedicalArticle.from_dict(d) for d in cached_data], meta

        journal_filters = " OR ".join(f'"{j}"[Journal]' for j in PEDIATRIC_JOURNALS)
        term = f"({query}) AND ({journal_filters})"
        articles, pubmed_meta = await self.pubmed.search_articles(term, max_results=max_results)

        if pubmed_meta.error and not articles:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [a.to_dict() for a in articles],
            source="pediatric_journals",
        )
        return articles, CacheMetadata(cached=False, cache_age=0, error=pubmed_meta.error)
