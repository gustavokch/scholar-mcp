import asyncio
import logging
import re
from typing import Any
from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.medical.ranking import rank_medical_articles
from scholar_mcp.utils.deduplication import deduplicate_papers
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

logger = logging.getLogger(__name__)

# Cochrane Library's HTML search sits behind a Cloudflare bot wall that blocks
# the plain HTTP fetch and even headless browsers. Europe PMC mirrors Cochrane
# systematic reviews via an open REST API, so we route the "Cochrane" search
# through Europe PMC and label results as Cochrane records.
EUROPE_PMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

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

    @staticmethod
    def _europe_pmc_to_articles(payload: dict[str, Any]) -> list[MedicalArticle]:
        """Map Europe PMC search result records to MedicalArticle shape,
        tagging them as Cochrane records so the agent sees them under the
        right source_database label.
        """
        articles: list[MedicalArticle] = []
        for rec in payload.get("resultList", {}).get("result", []) or []:
            title = (rec.get("title") or "").strip()
            if not title:
                continue
            authors = []
            author_string = rec.get("authorString")
            if author_string:
                authors = [a.strip() for a in author_string.split(",") if a.strip()]
            pmid = rec.get("pmid") or rec.get("id") or ""
            url = ""
            if rec.get("pmcid"):
                url = f"https://europepmc.org/article/PMC/{rec['pmcid']}"
            elif pmid:
                url = f"https://europepmc.org/article/MED/{pmid}"
            articles.append(
                MedicalArticle(
                    title=title,
                    authors=authors,
                    year=str(rec.get("pubYear") or ""),
                    journal=rec.get("journalTitle") or "Cochrane Database Syst Rev",
                    abstract=(rec.get("abstractText") or "")[:300],
                    url=url,
                    pmid=pmid,
                    doi=rec.get("doi") or "",
                    source_database="Cochrane",
                )
            )
        return articles

    async def _search_cochrane(
        self,
        query: str,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        # Cochrane's HTML site is behind a Cloudflare bot wall; route the
        # "Cochrane" search through Europe PMC's open REST API, which mirrors
        # Cochrane systematic reviews. The pub_type filter keeps the result
        # set close to what the user would have found on cochranelibrary.com.
        # Neutralize query-language punctuation first: a stray quote or paren
        # in an agent-supplied query is Europe PMC syntax, not part of the
        # topic, and would turn the whole search into a 400.
        clean_query = re.sub(r'["()]', " ", query)
        clean_query = re.sub(r"\s+", " ", clean_query).strip()
        europe_pmc_query = f"({clean_query}) AND (PUB_TYPE:\"systematic review\" OR PUB_TYPE:\"meta-analysis\")"
        try:
            resp = await self.http_client.get(
                EUROPE_PMC_URL,
                params={
                    "query": europe_pmc_query,
                    "format": "json",
                    # "lite" omits abstractText; only "core" returns it.
                    "resultType": "core",
                    "pageSize": "10",
                },
            )
            if resp is None:
                raise FetchError("europe pmc request failed")
            payload = resp.json()
        except Exception:
            logger.warning(
                "Europe PMC (Cochrane) search failed for %r", query, exc_info=True
            )
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        articles = self._europe_pmc_to_articles(payload)
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
        errored = False
        for res in results:
            if isinstance(res, BaseException):
                # gather returned the exception instead of a result
                logger.warning("Medical database sub-search raised", exc_info=res)
                errored = True
                continue
            if not res or not res[0]:
                errored = errored or res[1].error
                continue
            errored = errored or res[1].error
            papers.extend(a.to_dict() for a in res[0])

        unique, _ = deduplicate_papers(papers)
        ranked = rank_medical_articles(
            [MedicalArticle.from_dict(p) for p in unique], query
        )
        final_articles = ranked[:20]

        # A failed fetch that produced nothing must reach the caller as a
        # failure rather than being cached as a genuine absence.
        if errored and not final_articles:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [a.to_dict() for a in final_articles],
            source="pubmed",
        )
        return final_articles, CacheMetadata(cached=False, cache_age=0, error=errored)

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
        articles, pubmed_meta = await self.pubmed.search_articles(term, max_results=15)

        deduped, _ = deduplicate_papers([a.to_dict() for a in articles])
        # Rank on the raw user query, not `term`: the journal filters would
        # otherwise contribute their own tokens ("medicine", "lancet") as query
        # terms. Rank before slicing so the cap keeps the best 15, not the
        # first 15.
        ranked = rank_medical_articles(
            [MedicalArticle.from_dict(p) for p in deduped], query
        )
        final_articles = ranked[:15]

        # A failed fetch that produced nothing must reach the caller as a
        # failure rather than being cached as a genuine absence.
        if pubmed_meta.error and not final_articles:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [a.to_dict() for a in final_articles],
            source="pubmed",
        )
        return final_articles, CacheMetadata(cached=False, cache_age=0, error=pubmed_meta.error)
