import math
import httpx
import pytest
import respx
from scholar_mcp.config import Settings
from scholar_mcp.models import PaperMetadata
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.providers.openalex import OPENALEX_BASE, OpenAlexProvider
from scholar_mcp.ranking import (
    RankingPipeline,
    RankingWeights,
    ScoringEngine,
    ScoringMetrics,
    classify_evidence_grade,
)
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient


def test_tokenize_lowercases_and_drops_stopwords_and_short_tokens():
    tokens = ScoringEngine.tokenize("The Effects of Metformin on A1c")
    assert tokens == ["effects", "metformin", "a1c"]


def test_tokenize_handles_none_and_empty():
    assert ScoringEngine.tokenize(None) == []
    assert ScoringEngine.tokenize("") == []


def test_text_coverage_title_weighted_double_abstract():
    terms = ScoringEngine.tokenize("metformin diabetes")
    # Both terms in title -> full coverage
    assert math.isclose(
        ScoringEngine.text_coverage(terms, "Metformin for Diabetes", ""), 1.0
    )
    # Both terms in abstract only -> abstract counts half, so max 0.5
    assert math.isclose(
        ScoringEngine.text_coverage(terms, "", "Metformin and diabetes outcomes"), 0.5
    )
    # No terms anywhere -> 0
    assert ScoringEngine.text_coverage(terms, "Unrelated title", "Unrelated abstract") == 0.0
    # No query terms -> 0
    assert ScoringEngine.text_coverage([], "Metformin", "Diabetes") == 0.0


def test_best_matching_sentence_picks_highest_overlap():
    terms = ScoringEngine.tokenize("metformin renal outcomes")
    text = (
        "This study examines insulin resistance in cells. "
        "Metformin showed no significant renal outcomes in this cohort. "
        "Patients were followed for five years."
    )
    sentence, score = ScoringEngine.best_matching_sentence(terms, text)
    assert "Metformin showed no significant renal outcomes" in sentence
    assert score > 0.5


def test_best_matching_sentence_empty_inputs():
    assert ScoringEngine.best_matching_sentence([], "Some text.") == ("", 0.0)
    assert ScoringEngine.best_matching_sentence(["metformin"], "") == ("", 0.0)


def test_classify_evidence_grade_meta_analysis_and_systematic_review():
    assert classify_evidence_grade(["Meta-Analysis"]) == "1a"
    assert classify_evidence_grade(["Journal Article", "Systematic Review"]) == "1a"


def test_classify_evidence_grade_picks_best_of_multiple():
    # RCT (1b) outranks Multicenter Study (2b) when both are present
    assert classify_evidence_grade(["Multicenter Study", "Randomized Controlled Trial"]) == "1b"


def test_classify_evidence_grade_lower_tiers():
    assert classify_evidence_grade(["Case-Control Studies"]) == "3b"
    assert classify_evidence_grade(["Case Reports"]) == "4"
    assert classify_evidence_grade(["Review"]) == "5"


def test_classify_evidence_grade_none_when_unrecognized_or_empty():
    assert classify_evidence_grade(["Journal Article"]) is None
    assert classify_evidence_grade([]) is None
    assert classify_evidence_grade(None) is None


def test_calculate_evidence_feature():
    assert ScoringEngine.calculate_evidence_feature("1a") == 1.0
    assert math.isclose(ScoringEngine.calculate_evidence_feature("1b"), 1.0 / 2)
    assert math.isclose(ScoringEngine.calculate_evidence_feature("2b"), 1.0 / 3)
    assert ScoringEngine.calculate_evidence_feature(None) == 0.0


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
    ranked = ScoringEngine.score_candidates(papers, weights=weights, query="", current_year=2026)

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

    ranked = await pipeline.rank_papers(candidates, query="", top_n=2)

    assert len(ranked) == 2
    assert ranked[0].score is not None
    assert ranked[1].score is not None
    assert ranked[0].score >= ranked[1].score

    # Verify cached values
    assert await cache.get("cit:pmid:111") == 500
    assert await cache.get("cit:pmid:222") == 10
    assert await cache.get("cit:doi:10.1001/paper3") == 25

    await client.aclose()


@respx.mock
async def test_enrich_citations_warm_cache_keeps_authority():
    settings = Settings()
    client = AsyncHttpClient(settings)
    cache = TTLCache(maxsize=100, ttl_seconds=3600)

    pipeline = RankingPipeline(
        openalex=OpenAlexProvider(client),
        europe_pmc=EuropePMCProvider(client),
        crossref=CrossRefProvider(client),
        cache=cache,
        settings=settings,
    )

    works_route = respx.get(url__startswith=f"{OPENALEX_BASE}/works?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "doi": "https://doi.org/10.1001/auth1",
                        "ids": {"pmid": "777"},
                        "cited_by_count": 300,
                        "authorships": [
                            {"author": {"id": "https://openalex.org/A9999"}},
                            {"author": {"id": "https://openalex.org/A8888"}},
                        ],
                    }
                ]
            },
        )
    )
    authors_route = respx.get(url__startswith=f"{OPENALEX_BASE}/authors?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"id": "https://openalex.org/A8888", "summary_stats": {"h_index": 41}}
                ]
            },
        )
    )

    try:
        cold = [
            PaperMetadata(title="Authority Paper", doi="10.1001/auth1", pmid="777", year="2024")
        ]
        cold = await pipeline.enrich_citations(cold)
        assert cold[0].citation_count == 300
        assert cold[0].last_author_h_index == 41
        assert await cache.get("cit:ah:a8888") == 41

        warm = [
            PaperMetadata(title="Authority Paper", doi="10.1001/auth1", pmid="777", year="2024")
        ]
        warm = await pipeline.enrich_citations(warm)
        assert warm[0].citation_count == 300
        assert warm[0].last_author_h_index == 41

        # Warm run served entirely from cache: cold run made one /works call
        # per filter (doi + pmid) and one /authors call; warm added none.
        assert works_route.call_count == 2
        assert authors_route.call_count == 1
    finally:
        await client.aclose()


@respx.mock
async def test_ranking_pipeline_normalizes_doi_url():
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

    respx.get(url__startswith=f"{OPENALEX_BASE}/works?").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "doi": "https://doi.org/10.1038/s41586-020-0001",
                        "cited_by_count": 420,
                    }
                ]
            },
        )
    )

    # Candidate has full DOI URL and no PMID
    candidates = [
        PaperMetadata(
            title="DOI URL Paper",
            doi="https://doi.org/10.1038/s41586-020-0001",
            year="2020",
        )
    ]

    enriched = await pipeline.enrich_citations(candidates)
    assert enriched[0].citation_count == 420
    assert await cache.get("cit:doi:10.1038/s41586-020-0001") == 420

    await client.aclose()




def test_calculate_authority_feature():
    assert ScoringEngine.calculate_authority_feature(None) == 0.0
    assert ScoringEngine.calculate_authority_feature(0) == 0.0
    assert math.isclose(ScoringEngine.calculate_authority_feature(9), math.log(10.0))


def test_score_candidates_query_relevance_outranks_source_position():
    papers = [
        # High source rank (idx 0) but irrelevant to the query
        PaperMetadata(title="Unrelated topic entirely", year="2024", citation_count=10, pmid="1"),
        # Low source rank (idx 4) but a strong lexical match
        PaperMetadata(title="Metformin efficacy in type 2 diabetes", year="2024", citation_count=10, pmid="2"),
        PaperMetadata(title="Filler paper A", year="2024", citation_count=10, pmid="3"),
        PaperMetadata(title="Filler paper B", year="2024", citation_count=10, pmid="4"),
        PaperMetadata(title="Filler paper C", year="2024", citation_count=10, pmid="5"),
    ]
    weights = RankingWeights(
        relevance=0.7, citations=0.1, recency=0.1,
        evidence_grade=0.05, journal_impact=0.025, author_authority=0.025,
    )

    ranked = ScoringEngine.score_candidates(papers, weights=weights, query="metformin diabetes", current_year=2026)

    assert ranked[0].pmid == "2"


def test_score_candidates_new_signals_default_neutral():
    papers = [
        PaperMetadata(title="Paper A", year="2024", citation_count=5, pmid="1"),
        PaperMetadata(title="Paper B", year="2024", citation_count=5, pmid="2"),
    ]
    weights = RankingWeights()
    ranked = ScoringEngine.score_candidates(papers, weights=weights, query="", current_year=2026)

    for p in ranked:
        assert p.ranking_metrics["z_evidence"] == 0.0
        assert p.ranking_metrics["z_impact"] == 0.0
        assert p.ranking_metrics["z_authority"] == 0.0


def test_score_candidates_full_pipeline_favors_high_quality_paper():
    strong = PaperMetadata(
        title="Systematic Review of Metformin for Type 2 Diabetes",
        abstract="A systematic review and meta-analysis of metformin trials in type 2 diabetes.",
        year="2024",
        citation_count=50,
        pmid="1",
        issn="0028-4793",
        evidence_grade="1a",
        last_author_h_index=60,
    )
    weak = PaperMetadata(
        title="Systematic Review of Metformin for Type 2 Diabetes",
        abstract="A systematic review and meta-analysis of metformin trials in type 2 diabetes.",
        year="2024",
        citation_count=50,
        pmid="2",
        issn=None,
        evidence_grade=None,
        last_author_h_index=None,
    )

    import scholar_mcp.ranking as ranking_module
    ranking_module._load_scimago_table.cache_clear()

    # position_weight=0 isolates the quality signals this test checks: the two
    # papers have identical text, so any position prior would hand the whole
    # relevance z-spread to the weak paper (idx 0), outweighing evidence +
    # authority (0.25) with the relevance weight alone (0.30).
    weights = RankingWeights(position_weight=0.0)
    ranked = ScoringEngine.score_candidates(
        [weak, strong], weights=weights, query="metformin type 2 diabetes", current_year=2026
    )

    assert ranked[0].pmid == "1"
    assert ranked[0].score > ranked[1].score
