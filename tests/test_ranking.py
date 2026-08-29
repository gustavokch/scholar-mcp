import math
import pytest
from scholar_mcp.models import PaperMetadata
from scholar_mcp.ranking import RankingWeights, ScoringEngine, ScoringMetrics


def test_calculate_z_scores_basic():
    values = [10.0, 20.0, 30.0]
    z_scores = ScoringEngine.calculate_z_scores(values)
    assert len(z_scores) == 3
    # Mean = 20, Std = sqrt((100 + 0 + 100)/3) = sqrt(66.6667) = 8.1649658
    assert math.isclose(z_scores[0], (10 - 20) / math.sqrt(200 / 3), rel_tol=1e-5)
    assert math.isclose(z_scores[1], 0.0, abs_tol=1e-5)
    assert math.isclose(z_scores[2], (30 - 20) / math.sqrt(200 / 3), rel_tol=1e-5)


def test_calculate_z_scores_zero_variance():
    values = [5.0, 5.0, 5.0, 5.0]
    z_scores = ScoringEngine.calculate_z_scores(values)
    assert z_scores == [0.0, 0.0, 0.0, 0.0]


def test_calculate_z_scores_empty_and_single():
    assert ScoringEngine.calculate_z_scores([]) == []
    assert ScoringEngine.calculate_z_scores([42.0]) == [0.0]


def test_calculate_recency_feature():
    current_year = 2026
    half_life = 7.0

    # Age 0 -> exp(0) = 1.0
    r0, y0 = ScoringEngine.calculate_recency_feature("2026", current_year, half_life)
    assert math.isclose(r0, 1.0, rel_tol=1e-5)
    assert y0 == 2026

    # Age 7 -> exp(-ln(2)) = 0.5
    r7, y7 = ScoringEngine.calculate_recency_feature("2019", current_year, half_life)
    assert math.isclose(r7, 0.5, rel_tol=1e-5)
    assert y7 == 2019

    # Age 14 -> exp(-2*ln(2)) = 0.25
    r14, y14 = ScoringEngine.calculate_recency_feature("2012", current_year, half_life)
    assert math.isclose(r14, 0.25, rel_tol=1e-5)
    assert y14 == 2012

    # Invalid year fallback
    r_bad, y_bad = ScoringEngine.calculate_recency_feature(
        "unknown", current_year, half_life, default_age=10.0
    )
    expected_decay = math.exp(-(math.log(2) / 7.0) * 10.0)
    assert math.isclose(r_bad, expected_decay, rel_tol=1e-5)
    assert y_bad is None


def test_calculate_citation_feature():
    assert math.isclose(ScoringEngine.calculate_citation_feature(0), math.log(1), rel_tol=1e-5)
    assert math.isclose(ScoringEngine.calculate_citation_feature(None), math.log(1), rel_tol=1e-5)
    assert math.isclose(ScoringEngine.calculate_citation_feature(100), math.log(101), rel_tol=1e-5)


def test_score_candidates_ordering():
    papers = [
        # Paper 0: Old, massive citations
        PaperMetadata(title="Seminal Classic", year="2010", citation_count=5000, pmid="1"),
        # Paper 1: Recent, moderate citations
        PaperMetadata(title="Recent High Impact", year="2025", citation_count=100, pmid="2"),
        # Paper 2: Very recent, zero citations
        PaperMetadata(title="Brand New Paper", year="2026", citation_count=0, pmid="3"),
    ]

    weights = RankingWeights(
        relevance=0.2, citations=0.4, recency=0.4, recency_half_life_years=7.0
    )
    ranked = ScoringEngine.score_candidates(papers, weights=weights, current_year=2026)

    assert len(ranked) == 3
    # Verify all papers have score and ranking_metrics
    for p in ranked:
        assert p.score is not None
        assert p.ranking_metrics is not None
        assert "z_citation" in p.ranking_metrics
        assert "z_recency" in p.ranking_metrics
        assert "z_relevance" in p.ranking_metrics

    # Scores must be sorted descending
    assert ranked[0].score >= ranked[1].score >= ranked[2].score
