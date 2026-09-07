from pathlib import Path

from scholar_mcp.config import Settings


def test_medical_settings_defaults():
    settings = Settings.load()
    assert settings.cache_ttl_fda == 86400
    assert settings.cache_ttl_pubmed == 3600
    assert settings.cache_ttl_who == 604800
    assert settings.cache_ttl_rxnorm == 2592000
    assert settings.cache_ttl_guidelines == 604800
    assert settings.cache_ttl_bright_futures == 2592000
    assert settings.cache_ttl_aap_policy == 604800
    assert settings.cache_ttl_pediatric_journals == 3600
    assert settings.cache_ttl_child_health == 604800
    assert settings.cache_ttl_pediatric_drugs == 86400
    assert settings.cache_ttl_clinical_trials == 86400
    assert settings.cache_ttl_brazil_moh == 2592000
    assert settings.cache_max_entries == 1000
    assert settings.enable_browser_fallback is True
    assert settings.enable_medical_tools is True
    assert isinstance(settings.cache_db_path, Path)
    assert settings.cache_db_path == Path("~/.cache/scholar_mcp/cache.db").expanduser()


def test_medical_settings_env_override(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_FDA", "12345")
    monkeypatch.setenv("CACHE_TTL_PUBMED", "90")
    monkeypatch.setenv("CACHE_TTL_PEDIATRIC_JOURNALS", "120")
    monkeypatch.setenv("CACHE_MAX_SIZE", "50")
    monkeypatch.setenv("ENABLE_MEDICAL_TOOLS", "false")
    monkeypatch.setenv("ENABLE_PLAYWRIGHT_FALLBACK", "no")
    monkeypatch.setenv("SCHOLAR_CACHE_DB", "/tmp/custom_cache.db")
    settings = Settings.load()
    assert settings.cache_ttl_fda == 12345
    assert settings.cache_ttl_pubmed == 90
    assert settings.cache_ttl_pediatric_journals == 120
    assert settings.cache_max_entries == 50
    assert settings.enable_medical_tools is False
    assert settings.enable_browser_fallback is False
    assert settings.cache_db_path == Path("/tmp/custom_cache.db")


def test_medical_settings_legacy_playwright_env_alias(monkeypatch):
    """ENABLE_PLAYWRIGHT_FALLBACK remains honored after the browser fallback
    moved from playwright to camoufox."""
    monkeypatch.setenv("ENABLE_PLAYWRIGHT_FALLBACK", "false")
    settings = Settings.load()
    assert settings.enable_browser_fallback is False


def test_brazil_moh_ttl_env_override(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_BRAZIL_MOH", "600")
    settings = Settings.load()
    assert settings.cache_ttl_brazil_moh == 600


def test_brazil_moh_cache_source_uses_its_own_ttl(tmp_path):
    from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    assert cache._ttl_for("brazil_moh", None) == 2592000

