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
async def test_pcdt_get_guideline_slug_case_insensitive(tmp_path):
    """Slug lookup must be case-insensitive in both directions."""
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    engine = GovBrPCDTEngine(http_client=AsyncMock(), cache=cache, settings=settings)
    try:
        # Uppercase-slug catalog item, lowercase input.
        engine._memory_catalog = {
            "pcdt-X": {
                "record_id": "pcdt-X",
                "slug": "X",
                "title": "X Condition",
                "download_url": "https://www.gov.br/saude/pt-br/assuntos/pcdt/x/X/@@download/file",
            }
        }
        item = await engine.get_guideline("x")
        assert item is not None and item.record_id == "pcdt-X"

        # Mixed-case input against the lowercase seed catalog.
        engine._memory_catalog = None
        item2 = await engine.get_guideline("Hanseniase")
        assert item2 is not None and item2.record_id == "pcdt-hanseniase"
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_brazil_moh_engine_pcdt_integration(tmp_path, monkeypatch):
    import httpx
    import respx
    from scholar_mcp.medical.brazil_moh import BVS_SEARCH_URL, BrazilMoHEngine
    from scholar_mcp.utils.http import AsyncHttpClient

    # Browser tier pinned off: step 2 drives every BVS stage to a 502, and with
    # the tier on that launches a REAL camoufox against the live BVS host. The
    # live records then outrank the PCDT record this test asserts on, so the
    # assertion silently becomes a statement about today's network.
    settings = Settings(brazil_browser_fallback=False)
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


def test_score_item_prefix_match_scores_substring_tier():
    """Prefix match is a subset of substring match, so it scores 0.90."""
    from scholar_mcp.medical.govbr_pcdt import _score_item, normalize_text, tokenize_portuguese

    item = {"title": "Acromegalia", "slug": "acromegalia"}
    query = "acromeg"
    score = _score_item(tokenize_portuguese(query), normalize_text(query), item)
    assert score == 0.90


def test_dict_to_guideline_sets_has_full_text():
    """PCDT rows with a download/local URL carry a body; rows with only a
    description fall back to it; rows with neither have nothing."""
    from scholar_mcp.medical.govbr_pcdt import _dict_to_guideline as pcdt_convert

    assert pcdt_convert(
        {"record_id": "pcdt-x", "title": "X", "download_url": "https://www.gov.br/x/@@download/file"}
    ).has_full_text is True
    assert pcdt_convert(
        {"record_id": "pcdt-y", "title": "Y", "download_url": "local:guidelines/y.txt"}
    ).has_full_text is True
    assert pcdt_convert(
        {"record_id": "pcdt-z", "title": "Z", "description": "Resumo."}
    ).has_full_text is True
    assert pcdt_convert({"record_id": "pcdt-w", "title": "W"}).has_full_text is False


def test_az_dict_to_guideline_sets_has_full_text():
    from scholar_mcp.medical.govbr_az import _dict_to_guideline as az_convert

    assert az_convert(
        {"record_id": "az-x", "title": "X", "download_url": "https://www.gov.br/x.pdf", "tree": "svsa"}
    ).has_full_text is True
    assert az_convert(
        {"record_id": "az-w", "title": "W", "tree": "svsa"}
    ).has_full_text is False


@pytest.mark.asyncio
async def test_pcdt_search_pre_v1_cache_row_is_not_served(tmp_path):
    """A row written under the un-versioned (pre-CACHE_SCHEMA) key must be a
    miss: it predates ``has_full_text`` and ``from_dict`` would default it to
    False, printing the off-site notice for a retrievable PCDT and sinking
    the record into the body-less tier in the merged brazil_moh ranking.
    """
    from scholar_mcp.medical.govbr_pcdt import normalize_text

    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    engine = GovBrPCDTEngine(http_client=AsyncMock(), cache=cache, settings=settings)
    try:
        stale_row = [
            {
                "title": "Acromegalia",
                "record_id": "pcdt-acromegalia",
                "document_url": "https://www.gov.br/x/@@download/file",
            }
        ]
        await cache.set(
            f"govbr_pcdt_search:5:{normalize_text('acromegalia')}",
            stale_row,
            source="govbr_pcdt",
        )

        results, meta = await engine.search("acromegalia", limit=5)

        assert meta.cached is False
        assert results[0].record_id == "pcdt-acromegalia"
        assert results[0].has_full_text is True
    finally:
        await cache.close()
