from dataclasses import asdict, dataclass
import datetime
import math
import re
from typing import Any

from scholar_mcp.models import PaperMetadata


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
