import asyncio
from dataclasses import asdict, dataclass
import datetime
import math
import re
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.models import PaperMetadata
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.providers.openalex import OpenAlexProvider, _strip_doi_url
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient



@dataclass
class RankingWeights:
    relevance: float = 0.4
    citations: float = 0.3
    recency: float = 0.3
    recency_half_life_years: float = 7.0


@dataclass
class ScoringMetrics:
    initial_rank: int
    citation_count: int
    pub_year: int | None
    raw_relevance: float
    raw_citation: float
    raw_recency: float
    z_relevance: float
    z_citation: float
    z_recency: float
    final_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScoringEngine:
    """Pure mathematical methods for search re-ranking and Z-score standardization."""

    @staticmethod
    def calculate_z_scores(values: list[float]) -> list[float]:
        k = len(values)
        if k <= 1:
            return [0.0] * k

        mean = sum(values) / k
        variance = sum((x - mean) ** 2 for x in values) / k
        std = math.sqrt(variance)

        if std < 1e-6:
            return [0.0] * k

        return [(x - mean) / std for x in values]

    @staticmethod
    def calculate_relevance(rank_idx: int) -> float:
        return 1.0 / math.sqrt(rank_idx + 1)

    @staticmethod
    def calculate_citation_feature(citations: int | None) -> float:
        count = max(0, citations if citations is not None else 0)
        return math.log(1.0 + count)

    @staticmethod
    def parse_year(year_str: str | None) -> int | None:
        if not year_str:
            return None
        match = re.search(r"\b(19\d\d|20\d\d)\b", str(year_str))
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
        return None

    @classmethod
    def calculate_recency_feature(
        cls,
        year_str: str | None,
        current_year: int,
        half_life_years: float,
        default_age: float = 10.0,
    ) -> tuple[float, int | None]:
        parsed_year = cls.parse_year(year_str)
        if parsed_year is not None:
            age = max(0.0, float(current_year - parsed_year))
        else:
            age = default_age

        half_life = max(0.1, half_life_years)
        decay_rate = math.log(2) / half_life
        recency_val = math.exp(-decay_rate * age)
        return recency_val, parsed_year

    @classmethod
    def score_candidates(
        cls,
        papers: list[PaperMetadata],
        weights: RankingWeights,
        current_year: int | None = None,
    ) -> list[PaperMetadata]:
        if not papers:
            return []

        now_year = current_year if current_year is not None else datetime.datetime.now().year

        raw_rel_list: list[float] = []
        raw_cit_list: list[float] = []
        raw_rec_list: list[float] = []
        parsed_years: list[int | None] = []
        cit_counts: list[int] = []

        for idx, p in enumerate(papers):
            raw_rel = cls.calculate_relevance(idx)
            raw_cit = cls.calculate_citation_feature(p.citation_count)
            raw_rec, p_year = cls.calculate_recency_feature(
                p.year,
                current_year=now_year,
                half_life_years=weights.recency_half_life_years,
            )

            raw_rel_list.append(raw_rel)
            raw_cit_list.append(raw_cit)
            raw_rec_list.append(raw_rec)
            parsed_years.append(p_year)
            cit_counts.append(p.citation_count if p.citation_count is not None else 0)

        z_rel_list = cls.calculate_z_scores(raw_rel_list)
        z_cit_list = cls.calculate_z_scores(raw_cit_list)
        z_rec_list = cls.calculate_z_scores(raw_rec_list)

        scored_papers: list[PaperMetadata] = []
        for idx, p in enumerate(papers):
            final_score = (
                weights.relevance * z_rel_list[idx]
                + weights.citations * z_cit_list[idx]
                + weights.recency * z_rec_list[idx]
            )

            metrics = ScoringMetrics(
                initial_rank=idx,
                citation_count=cit_counts[idx],
                pub_year=parsed_years[idx],
                raw_relevance=raw_rel_list[idx],
                raw_citation=raw_cit_list[idx],
                raw_recency=raw_rec_list[idx],
                z_relevance=z_rel_list[idx],
                z_citation=z_cit_list[idx],
                z_recency=z_rec_list[idx],
                final_score=final_score,
            )

            p.score = final_score
            p.ranking_metrics = metrics.to_dict()
            scored_papers.append(p)

        # Sort criteria: final_score DESC, citation_count DESC, year DESC, initial_rank ASC
        def sort_key(item: PaperMetadata) -> tuple[float, int, int, int]:
            m = item.ranking_metrics or {}
            score_val = item.score if item.score is not None else -float("inf")
            cit_val = m.get("citation_count", 0)
            yr_val = m.get("pub_year") or 0
            init_rank = m.get("initial_rank", 0)
            return (score_val, cit_val, yr_val, -init_rank)

        scored_papers.sort(key=sort_key, reverse=True)
        return scored_papers


class RankingPipeline:
    """Orchestrates candidate citation enrichment, feature extraction, and re-ranking."""

    def __init__(
        self,
        openalex: OpenAlexProvider,
        europe_pmc: EuropePMCProvider,
        crossref: CrossRefProvider,
        cache: TTLCache,
        settings: Settings | None = None,
    ) -> None:
        self.openalex = openalex
        self.europe_pmc = europe_pmc
        self.crossref = crossref
        self.cache = cache
        self.settings = settings or Settings.load()

    def _cache_key(self, identifier: str) -> str:
        return f"cit:{identifier.lower().strip()}"

    async def _fetch_epmc_citations(self, pmid: str | None, doi: str | None) -> int | None:
        query_part = f'EXT_ID:"{pmid}"' if pmid else f'DOI:"{doi}"'
        try:
            resp = await self.europe_pmc.http_client.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                params={"query": query_part, "format": "json", "resultType": "core"},
            )
            if resp is not None and resp.status_code == 200:
                results = resp.json().get("resultList", {}).get("result", [])
                if results and isinstance(results[0], dict):
                    c = results[0].get("citedByCount")
                    if isinstance(c, int):
                        return c
        except Exception:
            pass
        return None

    async def _fetch_crossref_citations(self, doi: str) -> int | None:
        try:
            meta = await self.crossref.fetch_metadata(doi)
            if meta and meta.citation_count is not None:
                return meta.citation_count
        except Exception:
            pass
        return None

    async def enrich_citations(self, papers: list[PaperMetadata]) -> list[PaperMetadata]:
        """Enrich candidates with citation counts via cache, OpenAlex batch, and fallback."""
        missing_indices: list[int] = []
        for idx, p in enumerate(papers):
            if p.citation_count is not None:
                continue

            # Check cache
            cached_count = None
            clean_doi = (_strip_doi_url(p.doi) or p.doi.strip()).lower() if p.doi else None
            if p.pmid:
                cached_count = await self.cache.get(self._cache_key(f"pmid:{p.pmid.strip()}"))
            if cached_count is None and clean_doi:
                cached_count = await self.cache.get(self._cache_key(f"doi:{clean_doi}"))

            if cached_count is not None:
                p.citation_count = cached_count
            else:
                missing_indices.append(idx)

        if not missing_indices:
            return papers

        missing_dois = [
            _strip_doi_url(papers[i].doi) or papers[i].doi.strip()
            for i in missing_indices
            if papers[i].doi
        ]
        missing_pmids = [papers[i].pmid for i in missing_indices if papers[i].pmid]

        # 1. OpenAlex Batch lookup
        oa_counts: dict[str, int] = {}
        if self.settings.enable_openalex:
            try:
                oa_counts = await self.openalex.fetch_citation_counts_batch(
                    dois=missing_dois,
                    pmids=missing_pmids,
                )
            except Exception:
                oa_counts = {}

        still_missing: list[int] = []
        for i in missing_indices:
            p = papers[i]
            clean_doi = (_strip_doi_url(p.doi) or p.doi.strip()).lower() if p.doi else None
            count = None
            if clean_doi and clean_doi in oa_counts:
                count = oa_counts[clean_doi]
            elif p.pmid and p.pmid.strip() in oa_counts:
                count = oa_counts[p.pmid.strip()]

            if count is not None:
                p.citation_count = count
                if p.pmid:
                    await self.cache.set(self._cache_key(f"pmid:{p.pmid.strip()}"), count)
                if clean_doi:
                    await self.cache.set(self._cache_key(f"doi:{clean_doi}"), count)
            else:
                still_missing.append(i)

        # 2. Parallel Fallback (Europe PMC / CrossRef)
        if still_missing:

            async def resolve_single_fallback(idx: int) -> tuple[int, int | None]:
                paper = papers[idx]
                c_val = await self._fetch_epmc_citations(paper.pmid, paper.doi)
                if c_val is None and paper.doi:
                    c_val = await self._fetch_crossref_citations(paper.doi)
                return idx, c_val

            tasks = [resolve_single_fallback(i) for i in still_missing]
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, tuple):
                        i, c_val = res
                        final_c = c_val if c_val is not None else 0
                        papers[i].citation_count = final_c
                        if papers[i].pmid:
                            await self.cache.set(
                                self._cache_key(f"pmid:{papers[i].pmid.strip()}"), final_c
                            )
                        c_doi = (
                            _strip_doi_url(papers[i].doi) or papers[i].doi.strip()
                        ).lower() if papers[i].doi else None
                        if c_doi:
                            await self.cache.set(
                                self._cache_key(f"doi:{c_doi}"), final_c
                            )
            except Exception:
                for i in still_missing:
                    if papers[i].citation_count is None:
                        papers[i].citation_count = 0

        # Guarantee non-None citation count
        for p in papers:
            if p.citation_count is None:
                p.citation_count = 0

        return papers

    async def rank_papers(
        self,
        papers: list[PaperMetadata],
        weights: RankingWeights | None = None,
        top_n: int = 10,
    ) -> list[PaperMetadata]:
        if not papers:
            return []

        w = weights or RankingWeights(
            relevance=self.settings.ranking_weight_relevance,
            citations=self.settings.ranking_weight_citations,
            recency=self.settings.ranking_weight_recency,
            recency_half_life_years=self.settings.ranking_recency_half_life_years,
        )

        try:
            # Enrich citations with timeout protection
            enriched = await asyncio.wait_for(
                self.enrich_citations(papers),
                timeout=self.settings.ranking_enrichment_timeout,
            )
        except Exception:
            for p in papers:
                if p.citation_count is None:
                    p.citation_count = 0
            enriched = papers

        scored = ScoringEngine.score_candidates(enriched, weights=w)
        return scored[:top_n]

