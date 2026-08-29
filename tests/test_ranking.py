import math
import httpx
import pytest
import respx
from scholar_mcp.config import Settings
from scholar_mcp.models import PaperMetadata
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.providers.openalex import OPENALEX_BASE, OpenAlexProvider
from scholar_mcp.ranking import RankingPipeline, RankingWeights, ScoringEngine, ScoringMetrics
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient


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


@respx.mock
async def test_ranking_pipeline_enrich_and_rank():
    settings = Settings()
    client = AsyncHttpClient(settings)
    cache = TTLCache(maxsize=100, ttl_seconds=3600)
    openalex = OpenAlexProvider(client)
    europe_pmc = EuropePMCProvider(client)
    crossref = CrossRefProvider(client)

    pipeline = RankingPipeline(
        openalex=openalex,
        europe_pmc=europe_pmc,
        crossref=crossref,
        cache=cache,
        settings=settings,
    )

    # Mock OpenAlex batch works response
    respx.get(url__startswith=f"{OPENALEX_BASE}/works?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "doi": "https://doi.org/10.1001/paper1",
                        "ids": {"pmid": "111"},
                        "cited_by_count": 500,
                    },
                    {
                        "doi": "https://doi.org/10.1001/paper2",
                        "ids": {"pmid": "222"},
                        "cited_by_count": 10,
                    },
                ]
            },
        )
    )

    candidates = [
        PaperMetadata(title="Paper 1", doi="10.1001/paper1", pmid="111", year="2020"),
        PaperMetadata(title="Paper 2", doi="10.1001/paper2", pmid="222", year="2025"),
        PaperMetadata(title="Paper 3 (No OpenAlex)", doi="10.1001/paper3", pmid="333", year="2026"),
    ]

    # Mock Europe PMC search for Paper 3 fallback
    respx.get(url__startswith="https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "resultList": {
                    "result": [{"doi": "10.1001/paper3", "pmid": "333", "citedByCount": 25}]
                }
            },
        )
    )

    ranked = await pipeline.rank_papers(candidates, top_n=2)

    assert len(ranked) == 2
    assert ranked[0].score is not None
    assert ranked[1].score is not None
    assert ranked[0].score >= ranked[1].score

    # Verify cached values
    assert await cache.get("cit:pmid:111") == 500
    assert await cache.get("cit:pmid:222") == 10
    assert await cache.get("cit:doi:10.1001/paper3") == 25

    await client.aclose()

