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
