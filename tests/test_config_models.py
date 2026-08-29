import pytest
from scholar_mcp.config import Settings
from scholar_mcp.models import (
    PaperMetadata,
    FullTextResponse,
    FullTextSummary,
    DownloadResult,
    IdentifierMap,
    FetchAttempt,
)


def test_settings_defaults(monkeypatch):
    for var in (
        "PUBMED_API_KEY",
        "ENABLE_SCIHUB",
        "PREFER_SCIHUB_OVER_UNPAYWALL",
        "SCHOLAR_DOWNLOAD_DIR",
        "SCHOLAR_MAX_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.load()
    assert settings.enable_scihub is True
    assert settings.prefer_scihub_over_unpaywall is False
    assert settings.pubmed_api_key is None
    assert settings.max_chars == 50_000
    assert settings.total_budget_seconds == 45
    assert settings.max_concurrency == 5
    assert settings.cache_ttl_seconds == 3600
    assert settings.title_match_threshold == 80.0
    assert settings.download_dir.name == "downloads"
    assert len(settings.scihub_mirrors) > 0


def test_settings_custom_env(monkeypatch):
    monkeypatch.setenv("PREFER_SCIHUB_OVER_UNPAYWALL", "true")
    monkeypatch.setenv("ENABLE_SCIHUB", "false")
    monkeypatch.setenv("PUBMED_API_KEY", "test-key")
    monkeypatch.setenv("SCHOLAR_MAX_CHARS", "1000")
    settings = Settings.load()
    assert settings.prefer_scihub_over_unpaywall is True
    assert settings.enable_scihub is False
    assert settings.pubmed_api_key == "test-key"
    assert settings.max_chars == 1000


def test_scihub_disabled_beats_preference(monkeypatch):
    """ENABLE_SCIHUB=false wins even when the preference flag is set."""
    monkeypatch.setenv("ENABLE_SCIHUB", "false")
    monkeypatch.setenv("PREFER_SCIHUB_OVER_UNPAYWALL", "true")
    settings = Settings.load()
    assert settings.scihub_tier_enabled() is False


def test_ncbi_rate_limit_depends_on_api_key(monkeypatch):
    monkeypatch.delenv("PUBMED_API_KEY", raising=False)
    assert Settings.load().ncbi_rate_limit == 3.0
    monkeypatch.setenv("PUBMED_API_KEY", "k")
    assert Settings.load().ncbi_rate_limit == 10.0


def test_paper_metadata_serialization():
    meta = PaperMetadata(
        title="Sample Paper",
        authors=["Alice Doe", "Bob Smith"],
        year="2023",
        venue="Nature",
        doi="10.1038/s41586-020-2003-7",
        pmid="32000000",
        pmcid="PMC7000000",
        abstract="This is a test abstract.",
        oa_status="gold",
    )
    d = meta.to_dict()
    assert d["title"] == "Sample Paper"
    assert d["pmid"] == "32000000"


def test_full_text_response_carries_truncation_and_trace():
    resp = FullTextResponse(
        status="full_text",
        source="pmc",
        format="markdown",
        title="Sample Paper",
        doi="10.1038/s41586-020-2003-7",
        pmid="32000000",
        pmcid="PMC7000000",
        content="# Sample Paper\n\nFull text body...",
        url="https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7000000/",
        truncated=False,
        total_chars=32,
        sections_available=["Abstract", "Introduction"],
        attempts=[FetchAttempt(tier="pmc", outcome="hit")],
    )
    d = resp.to_dict()
    assert d["truncated"] is False
    assert d["attempts"][0]["tier"] == "pmc"
    assert d["sections_available"] == ["Abstract", "Introduction"]


def test_fetch_attempt_records_skip_reason():
    a = FetchAttempt(tier="unpaywall", outcome="skipped", reason="UNPAYWALL_EMAIL not configured")
    assert a.to_dict()["reason"] == "UNPAYWALL_EMAIL not configured"


def test_settings_s2_and_openalex_env(monkeypatch):
    monkeypatch.setenv("S2_API_KEY", "secret")
    monkeypatch.setenv("OPENALEX_MAILTO", "me@example.com")
    monkeypatch.setenv("ENABLE_S2", "false")
    s = Settings.load()
    assert s.s2_api_key == "secret"
    assert s.openalex_email == "me@example.com"
    assert s.enable_s2 is False
    assert s.enable_openalex is True


def test_settings_openalex_mailto_fallback(monkeypatch):
    monkeypatch.delenv("OPENALEX_MAILTO", raising=False)
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    monkeypatch.setenv("PUBMED_EMAIL", "pubmed@example.com")
    s = Settings.load()
    assert s.openalex_email == "pubmed@example.com"


async def test_s2_rate_limit_depends_on_api_key(monkeypatch):
    from scholar_mcp.utils.http import AsyncHttpClient

    monkeypatch.delenv("S2_API_KEY", raising=False)
    c1 = AsyncHttpClient(settings=Settings.load(), max_retries=1)
    try:
        assert c1._limiter_for("api.semanticscholar.org").rate_per_sec == 1.0
    finally:
        await c1.aclose()

    monkeypatch.setenv("S2_API_KEY", "k")
    c2 = AsyncHttpClient(settings=Settings.load(), max_retries=1)
    try:
        assert c2._limiter_for("api.semanticscholar.org").rate_per_sec == 5.0
    finally:
        await c2.aclose()


def test_paper_metadata_ranking_fields():
    meta = PaperMetadata(
        title="Sample Paper",
        score=1.42,
        ranking_metrics={"z_citation": 0.8, "z_recency": 0.6},
    )
    d = meta.to_dict()
    assert d["score"] == 1.42
    assert d["ranking_metrics"] == {"z_citation": 0.8, "z_recency": 0.6}


def test_settings_ranking_defaults(monkeypatch):
    monkeypatch.delenv("RANKING_ENABLED", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_RELEVANCE", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_CITATIONS", raising=False)
    monkeypatch.delenv("RANKING_WEIGHT_RECENCY", raising=False)
    monkeypatch.delenv("RANKING_RECENCY_HALF_LIFE_YEARS", raising=False)
    monkeypatch.delenv("RANKING_CANDIDATE_MULTIPLIER", raising=False)
    monkeypatch.delenv("RANKING_MIN_CANDIDATES", raising=False)
    monkeypatch.delenv("RANKING_MAX_CANDIDATES", raising=False)
    monkeypatch.delenv("RANKING_ENRICHMENT_TIMEOUT", raising=False)

    s = Settings.load()
    assert s.ranking_enabled is True
    assert s.ranking_weight_relevance == 0.4
    assert s.ranking_weight_citations == 0.3
    assert s.ranking_weight_recency == 0.3
    assert s.ranking_recency_half_life_years == 7.0
    assert s.ranking_candidate_multiplier == 3
    assert s.ranking_min_candidates == 20
    assert s.ranking_max_candidates == 50
    assert s.ranking_enrichment_timeout == 1.5


def test_settings_ranking_custom_env(monkeypatch):
    monkeypatch.setenv("RANKING_ENABLED", "0")
    monkeypatch.setenv("RANKING_WEIGHT_RELEVANCE", "0.5")
    monkeypatch.setenv("RANKING_WEIGHT_CITATIONS", "0.2")
    monkeypatch.setenv("RANKING_WEIGHT_RECENCY", "0.3")
    monkeypatch.setenv("RANKING_RECENCY_HALF_LIFE_YEARS", "5.0")
    monkeypatch.setenv("RANKING_CANDIDATE_MULTIPLIER", "4")
    monkeypatch.setenv("RANKING_MIN_CANDIDATES", "15")
    monkeypatch.setenv("RANKING_MAX_CANDIDATES", "40")
    monkeypatch.setenv("RANKING_ENRICHMENT_TIMEOUT", "2.5")

    s = Settings.load()
    assert s.ranking_enabled is False
    assert s.ranking_weight_relevance == 0.5
    assert s.ranking_weight_citations == 0.2
    assert s.ranking_weight_recency == 0.3
    assert s.ranking_recency_half_life_years == 5.0
    assert s.ranking_candidate_multiplier == 4
    assert s.ranking_min_candidates == 15
    assert s.ranking_max_candidates == 40
    assert s.ranking_enrichment_timeout == 2.5

