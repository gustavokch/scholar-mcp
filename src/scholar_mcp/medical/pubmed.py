import dataclasses
import hashlib
import logging
import re
from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.medical.ranking import SOURCE_POSITION_WEIGHT, rank_medical_articles
from scholar_mcp.models import PaperMetadata
from scholar_mcp.providers.pubmed import PubMedProvider
from scholar_mcp.query_relax import MAX_RELAX_EXTRA_CALLS, relax_ladder
from scholar_mcp.utils.deduplication import deduplicate_papers
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

logger = logging.getLogger(__name__)


def _to_medical_article(paper: PaperMetadata) -> MedicalArticle:
    return MedicalArticle(
        title=paper.title,
        authors=paper.authors,
        journal=paper.venue,
        year=paper.year,
        abstract=paper.abstract,
        pmid=paper.pmid,
        pmc_id=re.sub(r"^PMC", "", paper.pmcid, flags=re.IGNORECASE) if paper.pmcid else None,
        doi=paper.doi,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{paper.pmid}/",
        source_database="PubMed",
        study_type=paper.study_type,
        evidence_grade=paper.evidence_grade,
    )


def parse_pubmed_xml(xml_text: str) -> list[MedicalArticle]:
    """EFetch XML -> MedicalArticle via the core PubMed record parser, so the
    medical tools read the publication year from PubDate (not the first
    <Year>, which is DateCompleted) and the DOI/PMCID from the record's own
    ArticleIdList (not a cited reference's)."""
    soup = BeautifulSoup(xml_text, "lxml-xml")
    articles: list[MedicalArticle] = []
    for record in soup.find_all(["PubmedArticle", "PubmedBookArticle"]):
        paper = PubMedProvider.parse_record(record)
        if paper is not None:
            articles.append(_to_medical_article(paper))
    return articles


class MedicalPubMedClient:
    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    def _base_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.settings.pubmed_api_key:
            params["api_key"] = self.settings.pubmed_api_key
        if self.settings.pubmed_email:
            params["email"] = self.settings.pubmed_email
        if self.settings.pubmed_tool:
            params["tool"] = self.settings.pubmed_tool
        return params

    async def _esearch(self, term: str, max_results: int) -> tuple[list[str], bool]:
        """One esearch call. Returns (idlist, errored).

        Extracted so the relaxation walk cannot drift apart from the initial
        search in params or failure handling.
        """
        search_params = {
            **self._base_params(),
            "db": "pubmed",
            "term": term,
            "retmode": "json",
            "retmax": str(max_results),
            # NCBI's default (no sort param) is most-recent-first; ask for Best Match.
            "sort": "relevance",
        }

        try:
            resp = await self.http_client.get(ESEARCH_URL, params=search_params)
            # AsyncHttpClient.get returns None once retries are exhausted or on
            # a >=400 status; check it explicitly rather than letting the miss
            # surface as an AttributeError below.
            if resp is None:
                raise FetchError("esearch request failed")
            data = resp.json()
            return data.get("esearchresult", {}).get("idlist", []), False
        except Exception:
            logger.warning("PubMed esearch failed for %r", term, exc_info=True)
            return [], True

    async def search_articles(
        self,
        query: str,
        max_results: int = 10,
        relax: bool = True,
        filters: str | None = None,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        """Search PubMed for ``query``, optionally restricted by ``filters``.

        ``filters`` is a PubMed clause (e.g. ``"NEJM"[Journal] OR ...``)
        ANDed onto the topic on every esearch, the relaxed rungs included:
        the ladder only ever shortens ``query``. Composing the clause into
        ``query`` instead would hand it to ``relax_ladder``, which strips the
        quotes, parentheses and ``OR`` and turns the filter into free-text
        words or drops it. Callers rank the returned articles against
        ``query``, never against the composed term.
        """
        # The cache key stays the original query: a relaxed hit is still the
        # answer to what the caller asked, and the key must not fan out per
        # ladder step. v2: rows parsed before the core-parser delegation hold
        # DateCompleted years and cited references' DOIs.
        cache_key = f"pubmed:search:v2:{query}:{max_results}"
        if filters:
            filt_hash = hashlib.sha1(filters.encode()).hexdigest()[:12]
            cache_key = f"{cache_key}:f={filt_hash}"

        def _term(topic: str) -> str:
            return f"({topic}) AND ({filters})" if filters else topic

        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return (
                [MedicalArticle.from_dict(d) for d in cached_data.get("articles", [])],
                dataclasses.replace(meta, relaxed_query=cached_data.get("relaxed_query")),
            )

        idlist, errored = await self._esearch(_term(query), max_results)
        if errored:
            # A fetch failure is not a zero-hit: relaxing further would
            # mistake a transport error for an over-constrained query.
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        # PubMed ANDs every term, so a long natural-language query
        # over-constrains esearch to zero hits while a leading-token prefix
        # of the same query returns hits. Walk the ladder past the already
        # tried full query, stopping at the first non-empty step.
        relaxed_query: str | None = None
        if not idlist and relax:
            for variant in relax_ladder(query)[1 : 1 + MAX_RELAX_EXTRA_CALLS]:
                if variant == query:
                    continue
                idlist, errored = await self._esearch(_term(variant), max_results)
                if errored:
                    break
                if idlist:
                    relaxed_query = variant
                    break

        if errored:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        if not idlist:
            await self.cache.set(
                cache_key, {"articles": [], "relaxed_query": None}, source="pubmed"
            )
            return [], CacheMetadata(cached=False, cache_age=0)

        fetch_params = {
            **self._base_params(),
            "db": "pubmed",
            "id": ",".join(idlist),
            "retmode": "xml",
        }

        try:
            fetch_resp = await self.http_client.get(EFETCH_URL, params=fetch_params)
            if fetch_resp is None:
                raise FetchError("efetch request failed")
            articles = parse_pubmed_xml(fetch_resp.text)
        except Exception:
            logger.warning("PubMed efetch failed for %r", query, exc_info=True)
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        # Deduplicate
        deduped_dicts, _ = deduplicate_papers([a.to_dict() for a in articles])
        # esearch was asked for Best Match, so the incoming order carries NCBI's
        # own relevance signal; blend it in rather than discarding it.
        final_articles = rank_medical_articles(
            [MedicalArticle.from_dict(d) for d in deduped_dicts],
            query,
            position_weight=SOURCE_POSITION_WEIGHT,
        )

        await self.cache.set(
            cache_key,
            {
                "articles": [a.to_dict() for a in final_articles],
                "relaxed_query": relaxed_query,
            },
            source="pubmed",
        )
        return final_articles, CacheMetadata(
            cached=False, cache_age=0, relaxed_query=relaxed_query
        )
