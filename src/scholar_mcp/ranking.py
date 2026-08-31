import asyncio
from dataclasses import asdict, dataclass
import datetime
from functools import lru_cache
import json
import math
import re
from pathlib import Path
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.models import PaperMetadata
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.providers.openalex import OpenAlexProvider, _strip_doi_url
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient


_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_TITLE_WEIGHT = 2.0
_ABSTRACT_WEIGHT = 1.0
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "is",
    "of", "on", "or", "the", "to", "with",
}

PUBTYPE_TO_GRADE: dict[str, str] = {
    "meta-analysis": "1a",
    "systematic review": "1a",
    "randomized controlled trial": "1b",
    "observational study": "2b",
    "comparative study": "2b",
    "multicenter study": "2b",
    "case-control studies": "3b",
    "case reports": "4",
    "review": "5",
    "editorial": "5",
    "comment": "5",
    "practice guideline": "5",
}

EVIDENCE_GRADE_RANK: dict[str, int] = {
    "1a": 1,
    "1b": 2,
    "2b": 3,
    "3b": 4,
    "4": 5,
    "5": 6,
}


def classify_evidence_grade(pubtypes: list[str] | None) -> str | None:
    """Map raw PubMed PublicationType strings to an Oxford CEBM-style grade.

    Picks the single best (lowest-rank) grade when a paper carries multiple
    publication types (e.g. both "Multicenter Study" and "Randomized
    Controlled Trial" -> the RCT grade wins).
    """
    if not pubtypes:
        return None
    best_grade: str | None = None
    best_rank: int | None = None
    for pt in pubtypes:
        grade = PUBTYPE_TO_GRADE.get(pt.strip().lower())
        if grade is None:
            continue
        rank = EVIDENCE_GRADE_RANK[grade]
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_grade = grade
    return best_grade


_SCIMAGO_DATA_PATH = Path(__file__).parent / "data" / "scimago_sjr.json"


def _normalize_issn(issn: str) -> str:
    return re.sub(r"[^0-9Xx]", "", issn).upper()


def _normalize_journal_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


@lru_cache(maxsize=1)
def _load_scimago_table() -> dict[str, dict[str, float]]:
    try:
        with open(_SCIMAGO_DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "issn": {k: float(v) for k, v in data.get("issn", {}).items()},
            "name": {k: float(v) for k, v in data.get("name", {}).items()},
        }
    except Exception:
        return {"issn": {}, "name": {}}


def lookup_journal_impact(issn: str | None, venue: str | None) -> float | None:
    table = _load_scimago_table()
    if issn:
        value = table["issn"].get(_normalize_issn(issn))
        if value is not None:
            return value
    if venue:
        value = table["name"].get(_normalize_journal_name(venue))
        if value is not None:
            return value
    return None


def parse_scimago_csv(rows: list[dict[str, str]]) -> dict[str, dict[str, float]]:
    """Parse Scimago Journal Rank CSV export rows (dict-per-row, semicolon-delimited
    source) into the {"issn": {...}, "name": {...}} table format used by
    scimago_sjr.json. Shared with scripts/update_scimago_data.py."""
    issn_table: dict[str, float] = {}
    name_table: dict[str, float] = {}

    for row in rows:
        title = (row.get("Title") or "").strip()
        sjr_raw = (row.get("SJR") or "").strip()
        issn_raw = (row.get("Issn") or "").strip()
        if not title or not sjr_raw:
            continue

        try:
            sjr_value = float(sjr_raw.replace(",", "."))
        except ValueError:
            continue

        name_table[_normalize_journal_name(title)] = sjr_value

        for issn in issn_raw.split(","):
            issn = issn.strip()
            if issn:
                issn_table[_normalize_issn(issn)] = sjr_value

    return {"issn": issn_table, "name": name_table}



@dataclass
class RankingWeights:
    relevance: float = 0.30
    citations: float = 0.20
    recency: float = 0.15
    evidence_grade: float = 0.20
    journal_impact: float = 0.10
    author_authority: float = 0.05
    recency_half_life_years: float = 7.0
    position_weight: float = 0.25


@dataclass
class ScoringMetrics:
    initial_rank: int
    citation_count: int
    pub_year: int | None
    raw_relevance: float
    raw_citation: float
    raw_recency: float
    raw_evidence: float
    raw_impact: float
    raw_authority: float
    z_relevance: float
    z_citation: float
    z_recency: float
    z_evidence: float
    z_impact: float
    z_authority: float
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
    def tokenize(text: str | None) -> list[str]:
        if not text:
            return []
        return [
            t for t in _WORD_SPLIT_RE.split(text.lower())
            if len(t) >= 2 and t not in _STOPWORDS
        ]

    @staticmethod
    def text_coverage(query_terms: list[str], title: str | None, abstract: str | None) -> float:
        if not query_terms:
            return 0.0
        term_count = len(query_terms)
        title_terms = set(ScoringEngine.tokenize(title))
        abstract_terms = set(ScoringEngine.tokenize(abstract))
        title_coverage = sum(1 for t in query_terms if t in title_terms) / term_count
        abstract_coverage = sum(1 for t in query_terms if t in abstract_terms) / term_count
        abstract_ratio = _ABSTRACT_WEIGHT / _TITLE_WEIGHT
        return min(1.0, title_coverage + abstract_ratio * abstract_coverage)

    @staticmethod
    def best_matching_sentence(query_terms: list[str], text: str) -> tuple[str, float]:
        if not text or not query_terms:
            return "", 0.0
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if not sentences:
            return "", 0.0
        term_count = len(query_terms)
        best_sentence = ""
        best_score = 0.0
        for sentence in sentences:
            sentence_terms = set(ScoringEngine.tokenize(sentence))
            score = sum(1 for t in query_terms if t in sentence_terms) / term_count
            if score > best_score:
                best_score = score
                best_sentence = sentence
        return best_sentence, best_score

    @staticmethod
    def calculate_relevance(rank_idx: int) -> float:
        return 1.0 / math.sqrt(rank_idx + 1)

    @staticmethod
    def calculate_query_relevance(
        rank_idx: int,
        query_terms: list[str],
        title: str | None,
        abstract: str | None,
        position_weight: float,
    ) -> float:
        lexical = ScoringEngine.text_coverage(query_terms, title, abstract)
        position = ScoringEngine.calculate_relevance(rank_idx)
        return (1.0 - position_weight) * lexical + position_weight * position

    @staticmethod
    def calculate_citation_feature(citations: int | None) -> float:
        count = max(0, citations if citations is not None else 0)
        return math.log(1.0 + count)

    @staticmethod
    def calculate_evidence_feature(evidence_grade: str | None) -> float:
        if evidence_grade is None:
            return 0.0
        rank = EVIDENCE_GRADE_RANK.get(evidence_grade)
        if rank is None:
            return 0.0
        return 1.0 / rank

    @staticmethod
    def calculate_impact_feature(sjr: float | None) -> float:
        if sjr is None or sjr <= 0:
            return 0.0
        return math.log(1.0 + sjr)

    @staticmethod
    def calculate_authority_feature(h_index: int | None) -> float:
        if h_index is None or h_index <= 0:
            return 0.0
        return math.log(1.0 + h_index)

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
        query: str,
        current_year: int | None = None,
    ) -> list[PaperMetadata]:
        if not papers:
            return []

        now_year = current_year if current_year is not None else datetime.datetime.now().year
        query_terms = cls.tokenize(query)

        raw_rel_list: list[float] = []
        raw_cit_list: list[float] = []
        raw_rec_list: list[float] = []
        raw_evidence_list: list[float] = []
        raw_impact_list: list[float] = []
        raw_authority_list: list[float] = []
        parsed_years: list[int | None] = []
        cit_counts: list[int] = []

        for idx, p in enumerate(papers):
            raw_rel = cls.calculate_query_relevance(
                idx, query_terms, p.title, p.abstract, weights.position_weight
            )
            raw_cit = cls.calculate_citation_feature(p.citation_count)
            raw_rec, p_year = cls.calculate_recency_feature(
                p.year,
                current_year=now_year,
                half_life_years=weights.recency_half_life_years,
            )
            raw_evidence = cls.calculate_evidence_feature(p.evidence_grade)
            raw_impact = cls.calculate_impact_feature(lookup_journal_impact(p.issn, p.venue))
            raw_authority = cls.calculate_authority_feature(p.last_author_h_index)

            raw_rel_list.append(raw_rel)
            raw_cit_list.append(raw_cit)
            raw_rec_list.append(raw_rec)
            raw_evidence_list.append(raw_evidence)
            raw_impact_list.append(raw_impact)
            raw_authority_list.append(raw_authority)
            parsed_years.append(p_year)
            cit_counts.append(p.citation_count if p.citation_count is not None else 0)

        z_rel_list = cls.calculate_z_scores(raw_rel_list)
        z_cit_list = cls.calculate_z_scores(raw_cit_list)
        z_rec_list = cls.calculate_z_scores(raw_rec_list)
        z_evidence_list = cls.calculate_z_scores(raw_evidence_list)
        z_impact_list = cls.calculate_z_scores(raw_impact_list)
        z_authority_list = cls.calculate_z_scores(raw_authority_list)

        scored_papers: list[PaperMetadata] = []
        for idx, p in enumerate(papers):
            final_score = (
                weights.relevance * z_rel_list[idx]
                + weights.citations * z_cit_list[idx]
                + weights.recency * z_rec_list[idx]
                + weights.evidence_grade * z_evidence_list[idx]
                + weights.journal_impact * z_impact_list[idx]
                + weights.author_authority * z_authority_list[idx]
            )

            metrics = ScoringMetrics(
                initial_rank=idx,
                citation_count=cit_counts[idx],
                pub_year=parsed_years[idx],
                raw_relevance=raw_rel_list[idx],
                raw_citation=raw_cit_list[idx],
                raw_recency=raw_rec_list[idx],
                raw_evidence=raw_evidence_list[idx],
                raw_impact=raw_impact_list[idx],
                raw_authority=raw_authority_list[idx],
                z_relevance=z_rel_list[idx],
                z_citation=z_cit_list[idx],
                z_recency=z_rec_list[idx],
                z_evidence=z_evidence_list[idx],
                z_impact=z_impact_list[idx],
                z_authority=z_authority_list[idx],
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

        # 1. OpenAlex Batch lookup (citation count + last-author ID)
        oa_details: dict[str, dict[str, Any]] = {}
        if self.settings.enable_openalex:
            try:
                oa_details = await self.openalex.fetch_work_details_batch(
                    dois=missing_dois,
                    pmids=missing_pmids,
                )
            except Exception:
                oa_details = {}

        last_author_ids: dict[int, str] = {}
        still_missing: list[int] = []
        for i in missing_indices:
            p = papers[i]
            clean_doi = (_strip_doi_url(p.doi) or p.doi.strip()).lower() if p.doi else None
            entry = None
            if clean_doi and clean_doi in oa_details:
                entry = oa_details[clean_doi]
            elif p.pmid and p.pmid.strip() in oa_details:
                entry = oa_details[p.pmid.strip()]

            if entry is not None:
                count = entry["citation_count"]
                p.citation_count = count
                if p.pmid:
                    await self.cache.set(self._cache_key(f"pmid:{p.pmid.strip()}"), count)
                if clean_doi:
                    await self.cache.set(self._cache_key(f"doi:{clean_doi}"), count)
                if entry.get("last_author_id"):
                    last_author_ids[i] = entry["last_author_id"]
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

        # 3. Author authority: batch-fetch last-author h-index for papers
        # resolved via OpenAlex (same precondition as citation enrichment).
        if self.settings.enable_openalex and last_author_ids:
            unique_author_ids = list({aid for aid in last_author_ids.values()})
            try:
                h_index_map = await self.openalex.fetch_author_h_indices_batch(unique_author_ids)
            except Exception:
                h_index_map = {}
            for idx, author_id in last_author_ids.items():
                h_index = h_index_map.get(author_id)
                if h_index is not None:
                    papers[idx].last_author_h_index = h_index

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

