import asyncio
import random
from typing import Any
from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.medical.ranking import rank_medical_articles
from scholar_mcp.utils.deduplication import deduplicate_papers
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

COCHRANE_BASE = "https://www.cochranelibrary.com"
COCHRANE_URL = "https://www.cochranelibrary.com/search"

COCHRANE_ITEM_SELECTORS = ".search-result-item, .result-item, .search-result"
TITLE_SELECTORS = "h3 a, .title a, .result-title a, h3, .title"
DESC_SELECTORS = ".abstract, .snippet, .summary, p"
JOURNAL_SELECTORS = ".journal, .source, .publication"

TOP_JOURNALS = [
    "New England Journal of Medicine",
    "JAMA",
    "Lancet",
    "BMJ",
    "Nature Medicine",
]

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
)


class MedicalDatabasesEngine:
    def __init__(
        self,
        pubmed: MedicalPubMedClient,
        clinical_trials: ClinicalTrialsClient,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
        jitter_range: tuple[float, float] | None = (1.0, 3.0),
    ) -> None:
        self.pubmed = pubmed
        self.clinical_trials = clinical_trials
        self.http_client = http_client
        self.cache = cache
        self.settings = settings
        self.jitter_range = jitter_range

    async def _search_cochrane(
        self,
        query: str,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        if self.jitter_range:
            await asyncio.sleep(random.uniform(*self.jitter_range))

        articles: list[MedicalArticle] = []
        try:
            resp = await self.http_client.get(
                COCHRANE_URL,
                params={"q": query},
                headers={"User-Agent": BROWSER_UA},
            )
            html_text = resp.text
            soup = BeautifulSoup(html_text, "html.parser")
            for item in soup.select(COCHRANE_ITEM_SELECTORS):
                title_el = item.select_one(TITLE_SELECTORS)
                title = title_el.get_text(strip=True) if title_el else ""
                if not title or len(title) <= 10:
                    continue

                link = item.find("a")
                href = link.get("href", "") if link else ""
                url = href if href.startswith("http") else (COCHRANE_BASE.rstrip("/") + "/" + href.lstrip("/"))

                desc_el = item.select_one(DESC_SELECTORS)
                abstract = (desc_el.get_text(strip=True) if desc_el else "")[:300]

                journal_el = item.select_one(JOURNAL_SELECTORS)
                journal = journal_el.get_text(strip=True) if journal_el else "Cochrane Database"

                articles.append(
                    MedicalArticle(
                        title=title,
                        abstract=abstract,
                        journal=journal,
                        url=url,
                        source_database="Cochrane",
                    )
                )
        except Exception:
            return [], CacheMetadata(cached=False, cache_age=0)

        # Fallback to Playwright if enabled and no items found
        if not articles and self.settings.enable_playwright_fallback:
            try:
                from playwright.async_api import async_playwright

                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    page = await browser.new_page(user_agent=BROWSER_UA)
                    full_url = f"{COCHRANE_URL}?q={query}"
                    await page.goto(full_url, wait_until="domcontentloaded")
                    content = await page.content()
                    await browser.close()

                pw_soup = BeautifulSoup(content, "html.parser")
                for item in pw_soup.select(COCHRANE_ITEM_SELECTORS):
                    title_el = item.select_one(TITLE_SELECTORS)
                    title = title_el.get_text(strip=True) if title_el else ""
                    if not title or len(title) <= 10:
                        continue

                    link = item.find("a")
                    href = link.get("href", "") if link else ""
                    url = href if href.startswith("http") else (COCHRANE_BASE.rstrip("/") + "/" + href.lstrip("/"))

                    desc_el = item.select_one(DESC_SELECTORS)
                    abstract = (desc_el.get_text(strip=True) if desc_el else "")[:300]

                    journal_el = item.select_one(JOURNAL_SELECTORS)
                    journal = journal_el.get_text(strip=True) if journal_el else "Cochrane Database"

                    articles.append(
                        MedicalArticle(
                            title=title,
                            abstract=abstract,
                            journal=journal,
                            url=url,
                            source_database="Cochrane",
                        )
                    )
            except Exception:
                pass

        return articles, CacheMetadata(cached=False, cache_age=0)

    async def search_medical_databases(
        self,
        query: str,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        cache_key = f"medical_databases:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [MedicalArticle.from_dict(d) for d in cached_data], meta

        tasks = [
            self.pubmed.search_articles(query, max_results=5),
            self.clinical_trials.search_clinical_trials(query),
            self._search_cochrane(query),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        papers: list[dict[str, Any]] = []
        for res in results:
            if isinstance(res, BaseException) or not res or not res[0]:
                continue
            papers.extend(a.to_dict() for a in res[0])

        unique, _ = deduplicate_papers(papers)
        final_articles = rank_medical_articles(
            [MedicalArticle.from_dict(p) for p in unique[:20]], query
        )

        await self.cache.set(
            cache_key,
            [a.to_dict() for a in final_articles],
            source="pubmed",
        )
        return final_articles, CacheMetadata(cached=False, cache_age=0)

    async def search_medical_journals(
        self,
        query: str,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        cache_key = f"medical_journals:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [MedicalArticle.from_dict(d) for d in cached_data], meta

        journal_filters = " OR ".join(f'"{j}"[Journal]' for j in TOP_JOURNALS)
        term = f"({query}) AND ({journal_filters})"
        articles, _ = await self.pubmed.search_articles(term, max_results=15)

        deduped, _ = deduplicate_papers([a.to_dict() for a in articles])
        final_articles = [MedicalArticle.from_dict(p) for p in deduped[:15]]

        await self.cache.set(
            cache_key,
            [a.to_dict() for a in final_articles],
            source="pubmed",
        )
        return final_articles, CacheMetadata(cached=False, cache_age=0)
