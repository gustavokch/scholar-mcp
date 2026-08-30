import asyncio
import logging
import random
import re
from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import MedicalArticle, PediatricGuideline
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

BF_BASE = "https://brightfutures.aap.org"
BF_URL = "https://brightfutures.aap.org/Search"
AAP_BASE = "https://publications.aap.org"
AAP_URL = "https://publications.aap.org/pediatrics/search"

BF_ITEM_SELECTORS = ".search-result, .result-item, .guideline-item, article, .content-item"
AAP_ITEM_SELECTORS = ".search-result, .result-item, .article-item, article, .publication-item"
TITLE_SELECTORS = "h2, h3, .title, a.title"
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

        soup = BeautifulSoup(html_text, "html.parser")
        guidelines: list[PediatricGuideline] = []

        for item in soup.select(item_selectors):
            title_el = item.select_one(TITLE_SELECTORS)
            title = title_el.get_text(strip=True) if title_el else ""
            if not title or len(title) <= 10:
                continue

            link = item.find("a")
            href = link.get("href", "") if link else ""
            if href:
                item_url = href if href.startswith("http") else (base_url.rstrip("/") + "/" + href.lstrip("/"))
            else:
                item_url = base_url

            desc_el = item.select_one(DESC_SELECTORS)
            description = (desc_el.get_text(strip=True) if desc_el else "")[:300]

            age_group = _extract_age_group(title) or _extract_age_group(description)

            m_year = YEAR_RE.search(title) or (YEAR_RE.search(description) if description else None)
            year = m_year.group(0) if m_year else ""

            if source == "bright-futures":
                org = "American Academy of Pediatrics"
                category = "Preventive Care"
            else:
                org = "American Academy of Pediatrics"
                category = "Policy Statement"

            guidelines.append(
                PediatricGuideline(
                    title=title,
                    organization=org,
                    url=item_url,
                    source=source,
                    year=year,
                    description=description,
                    age_group=age_group,
                    category=category,
                )
            )

        # Fallback to Playwright if enabled and no items found
        if not guidelines and self.settings.enable_playwright_fallback:
            try:
                from playwright.async_api import async_playwright

                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    page = await browser.new_page(user_agent=BROWSER_UA)
                    query_str = params.get("q", "")
                    full_url = f"{url}?q={query_str}"
                    await page.goto(full_url, wait_until="domcontentloaded")
                    content = await page.content()
                    await browser.close()

                # Re-parse playwright rendered content
                pw_soup = BeautifulSoup(content, "html.parser")
                for item in pw_soup.select(item_selectors):
                    title_el = item.select_one(TITLE_SELECTORS)
                    title = title_el.get_text(strip=True) if title_el else ""
                    if not title or len(title) <= 10:
                        continue

                    link = item.find("a")
                    href = link.get("href", "") if link else ""
                    if href:
                        item_url = href if href.startswith("http") else (base_url.rstrip("/") + "/" + href.lstrip("/"))
                    else:
                        item_url = base_url

                    desc_el = item.select_one(DESC_SELECTORS)
                    description = (desc_el.get_text(strip=True) if desc_el else "")[:300]

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
            except Exception:
                logger.warning(
                    "Playwright fallback failed for %s", url, exc_info=True
                )

        return guidelines, False

    async def search_bright_futures(
        self,
        query: str,
    ) -> tuple[list[PediatricGuideline], CacheMetadata]:
        cache_key = f"bright_futures:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [PediatricGuideline.from_dict(d) for d in cached_data], meta

        results, errored = await self._scrape_html(
            BF_URL,
            {"q": query},
            BF_ITEM_SELECTORS,
            BF_BASE,
            "bright-futures",
        )

        if errored:
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
