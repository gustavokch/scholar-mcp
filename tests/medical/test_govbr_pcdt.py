import json
from unittest.mock import AsyncMock, patch

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical.govbr_pcdt import (
    GovBrPCDTEngine,
    load_seed_catalog,
    parse_letter_page,
)
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager


def test_load_seed_catalog():
    catalog = load_seed_catalog()
    assert len(catalog) >= 170
    assert "pcdt-acromegalia" in catalog
    item = catalog["pcdt-acromegalia"]
    assert item["slug"] == "acromegalia"
    assert item["letter"] == "a"
    assert "acromegalia" in item["download_url"]
    assert item["download_url"].endswith("@@download/file")


def test_parse_letter_page():
    html = """
    <div id="content-core">
        <a href="https://www.gov.br/saude/pt-br/assuntos/pcdt/a/acromegalia.pdf/view">Acromegalia</a>
        <a href="https://www.gov.br/saude/pt-br/assuntos/pcdt/a/acidentes-ofidicos/view">Acidentes Ofídicos</a>
        <a href="https://www.gov.br/saude/pt-br/assuntos/pcdt/a?b_start:int=30">Next</a>
    </div>
    """
    items, next_urls = parse_letter_page(html, "a", "https://www.gov.br/saude/pt-br/assuntos/pcdt/a")
    assert len(items) == 2
    assert "pcdt-acromegalia" in items
    assert items["pcdt-acromegalia"]["title"] == "Acromegalia"
    assert items["pcdt-acromegalia"]["download_url"] == (
        "https://www.gov.br/saude/pt-br/assuntos/pcdt/a/acromegalia.pdf/@@download/file"
    )
    assert len(next_urls) == 1
    assert "b_start:int=30" in next_urls[0]


@pytest.mark.asyncio
async def test_pcdt_search_exact_and_accent_insensitive(tmp_path):
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    engine = GovBrPCDTEngine(http_client=AsyncMock(), cache=cache, settings=settings)

    try:
        # Search exact match
        results, meta = await engine.search("acromegalia", limit=5)
        assert len(results) > 0
        assert results[0].record_id == "pcdt-acromegalia"
        assert results[0].source == "brazil-moh"
        assert results[0].country == "Brasil"
        assert "gov.br" in results[0].document_url
        assert results[0].document_url.endswith("@@download/file")

        # Search accent-insensitive match
        results_accent, _ = await engine.search("dor cronica", limit=5)
        assert len(results_accent) > 0
        assert "dor" in results_accent[0].title.lower()
        assert results_accent[0].record_id == "pcdt-dor-cronica"
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_pcdt_search_partial_terms(tmp_path):
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    engine = GovBrPCDTEngine(http_client=AsyncMock(), cache=cache, settings=settings)

    try:
        results, _ = await engine.search("melanoma cutaneo", limit=5)
        assert len(results) > 0
        assert any("melanoma" in r.title.lower() for r in results)
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_pcdt_get_guideline_by_id(tmp_path):
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    engine = GovBrPCDTEngine(http_client=AsyncMock(), cache=cache, settings=settings)

    try:
        # With pcdt- prefix
        item = await engine.get_guideline("pcdt-hanseniase")
        assert item is not None
        assert "hanseniase" in item.title.lower() or "hanseníase" in item.title.lower()
        assert "gov.br" in item.document_url

        # Bare slug
        item2 = await engine.get_guideline("hanseniase")
        assert item2 is not None
        assert item2.record_id == "pcdt-hanseniase"

        # Non-existent
        item3 = await engine.get_guideline("non-existent-condition")
        assert item3 is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_pcdt_7_day_cache_refresh(tmp_path):
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    mock_http = AsyncMock()
    engine = GovBrPCDTEngine(http_client=mock_http, cache=cache, settings=settings)

    try:
        # Pre-populate cache with an old timestamp (8 days old)
        old_catalog = {"pcdt-old": {"record_id": "pcdt-old", "slug": "old", "title": "Old"}}
        await cache.set("govbr_pcdt:catalog", old_catalog, source="govbr_pcdt", ttl=864000)

        # Mock refresh_catalog to return updated catalog
        fresh_catalog = {"pcdt-fresh": {"record_id": "pcdt-fresh", "slug": "fresh", "title": "Fresh"}}
        with patch.object(engine, "refresh_catalog", AsyncMock(return_value=fresh_catalog)) as mock_refresh:
            # Patch cache.get to report cache_age >= 7 days (604,800s)
            with patch.object(
                cache,
                "get",
                AsyncMock(return_value=(old_catalog, type("Meta", (), {"cached": True, "cache_age": 700000})())),
            ):
                catalog = await engine.get_catalog()
                mock_refresh.assert_called_once()
                assert "pcdt-fresh" in catalog
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_total_crawl_failure_caches_nothing(tmp_path):
    """All letter pages fail: catalog stays empty and nothing is cached."""
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    mock_http = AsyncMock()
    mock_http.get.return_value = None
    engine = GovBrPCDTEngine(http_client=mock_http, cache=cache, settings=settings)
    try:
        catalog = await engine.refresh_catalog()
        assert catalog == {}
        _, meta = await cache.get("govbr_pcdt:catalog")
        assert meta.cached is False
        assert engine._memory_catalog is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_partial_crawl_not_cached(tmp_path):
    """One letter page OK, the rest fail: items returned but NOT cached.

    A partial crawl cached with the full 7-day TTL would pin an incomplete
    catalog for a week whenever gov.br is flaky.
    """
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    mock_http = AsyncMock()
    ok_page = (
        '<div id="content-core">'
        '<a href="https://www.gov.br/saude/pt-br/assuntos/pcdt/a/acromegalia/view">Acromegalia</a>'
        "</div>"
    )

    async def get(url, **kwargs):
        if "/pcdt/a" in url:
            return type("R", (), {"status_code": 200, "text": ok_page})()
        return None

    mock_http.get.side_effect = get
    engine = GovBrPCDTEngine(http_client=mock_http, cache=cache, settings=settings)
    try:
        catalog = await engine.refresh_catalog()
        assert "pcdt-acromegalia" in catalog
        _, meta = await cache.get("govbr_pcdt:catalog")
        assert meta.cached is False, "partial crawl must not be cached"
        assert engine._memory_catalog is None, "partial crawl must not become the in-memory catalog"
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_brazil_moh_engine_pcdt_integration(tmp_path, monkeypatch):
    import httpx
    import respx
    from scholar_mcp.medical.brazil_moh import BVS_SEARCH_URL, BrazilMoHEngine
    from scholar_mcp.utils.http import AsyncHttpClient

    settings = Settings()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)

    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text",
        lambda _: "Texto integral do PCDT da Acromegalia.",
    )

    try:
        # 1. Collection = 'pcdt'
        records, meta = await engine.search_guidelines("acromegalia", limit=5, collection="pcdt")
        assert len(records) > 0
        assert records[0].record_id == "pcdt-acromegalia"
        assert meta.error is False

        # 2. BVS 502 failure -> fallback to PCDT in 'all' collection
        with respx.mock:
            respx.get(url__startswith=BVS_SEARCH_URL).mock(
                return_value=httpx.Response(502, text="Bad Gateway")
            )
            records_fallback, meta_fallback = await engine.search_guidelines(
                "acromegalia", limit=5, collection="all"
            )
            assert len(records_fallback) > 0
            assert records_fallback[0].record_id == "pcdt-acromegalia"
            assert meta_fallback.error is False

        # 3. Full text retrieval for PCDT record ID
        with respx.mock:
            respx.get("https://www.gov.br/saude/pt-br/assuntos/pcdt/a/acromegalia.pdf/@@download/file").mock(
                return_value=httpx.Response(200, content=b"%PDF-1.5 test", headers={"Content-Type": "application/pdf"})
            )
            fulltext, ft_meta = await engine.get_full_text("pcdt-acromegalia")
            assert fulltext["status"] == "success"
            assert fulltext["content_type"] == "pdf"
            assert "Acromegalia" in fulltext["content"]
            assert ft_meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()
