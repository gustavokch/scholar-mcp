import json
from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical import govbr_pcdt
from scholar_mcp.medical.govbr_common import (
    CACHE_SCHEMA,
    SEVEN_DAYS_SECONDS,
    normalize_text,
)
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
async def test_search_reports_error_when_catalog_unavailable(tmp_path):
    """An empty catalog is an outage, not a zero-result search.

    A failed PCDT search must not report error=True alongside
    error_kind=successful_empty -- that pair reads as a genuine zero-match
    search rather than a backend outage.
    """
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    engine = GovBrPCDTEngine(http_client=AsyncMock(), cache=cache, settings=settings)
    try:
        engine.get_catalog = AsyncMock(return_value={})

        results, meta = await engine.search("acromegalia", limit=5)

        assert results == []
        assert meta.error is True
        assert meta.error_kind != "successful_empty"
        assert meta.error_kind == "backend_error"
        _, cache_meta = await engine.cache.get("govbr_pcdt_search:5:acromegalia")
        assert cache_meta.cached is False
    finally:
        await cache.close()


class _Resp:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


def _letter_page(letter: str, extra: str = "") -> _Resp:
    return _Resp(
        '<div id="content-core">'
        f'<a href="https://www.gov.br/saude/pt-br/assuntos/pcdt/{letter}/cond-{letter}/view">'
        f"Condicao {letter.upper()}</a>{extra}</div>"
    )


def _crawl_engine(tmp_path, get):
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    http = AsyncMock()
    http.get.side_effect = get
    return GovBrPCDTEngine(http_client=http, cache=cache, settings=settings), cache


def _letter_of(url: str) -> str:
    return url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


@pytest.mark.asyncio
async def test_refresh_total_crawl_failure_is_incomplete(tmp_path):
    async def get(url, **kwargs):
        return None

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        catalog, complete = await engine.refresh_catalog()
        assert catalog == {}
        assert complete is False
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_partial_crawl_is_incomplete(tmp_path):
    async def get(url, **kwargs):
        return _letter_page("a") if _letter_of(url) == "a" else None

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        catalog, complete = await engine.refresh_catalog()
        assert "pcdt-cond-a" in catalog
        assert complete is False
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_full_crawl_is_complete_and_keeps_nothing(tmp_path):
    """Offline-only: even a complete crawl is neither cached nor kept."""
    async def get(url, **kwargs):
        return _letter_page(_letter_of(url))

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        catalog, complete = await engine.refresh_catalog()
        assert complete is True
        assert len(catalog) == len(govbr_pcdt.PCDT_LETTERS)
        _, meta = await cache.get("govbr_pcdt:catalog")
        assert meta.cached is False
        assert engine._memory_catalog is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_failed_second_page_is_incomplete(tmp_path):
    """One loaded page is not a loaded letter."""
    page_two = "https://www.gov.br/saude/pt-br/assuntos/pcdt/a?b_start:int=20"

    async def get(url, **kwargs):
        if "b_start" in url:
            return _Resp("", status_code=503)
        letter = _letter_of(url)
        extra = f'<a href="{page_two}">2</a>' if letter == "a" else ""
        return _letter_page(letter, extra)

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        catalog, complete = await engine.refresh_catalog()
        assert "pcdt-cond-a" in catalog
        assert complete is False
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_refresh_shrunken_crawl_is_incomplete(tmp_path):
    async def get(url, **kwargs):
        return _letter_page(_letter_of(url))

    engine, cache = _crawl_engine(tmp_path, get)
    try:
        incumbent = {f"row-{i}": {"record_id": f"row-{i}"} for i in range(100)}
        catalog, complete = await engine.refresh_catalog(incumbent=incumbent)
        assert catalog
        assert complete is False
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


@pytest.mark.asyncio
async def test_brazil_moh_search_guidelines_pcdt_engine_failure_not_successful_empty(tmp_path):
    """A failed PCDT sub-engine search must not surface as error_kind=successful_empty.

    ``search_guidelines`` rebuilds sub-engine meta as
    ``error_kind=sub_meta.error_kind or ("ok" if records else "successful_empty")``.
    Before this fix, the PCDT engine never set ``error_kind`` on failure, so a
    genuinely failed search reported error=True *and* "genuinely nothing
    matching" at the same time.
    """
    from scholar_mcp.medical.brazil_moh import BrazilMoHEngine
    from scholar_mcp.utils.http import AsyncHttpClient

    settings = Settings()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)

    try:
        engine.pcdt_engine.get_catalog = AsyncMock(return_value={})

        records, meta = await engine.search_guidelines(
            "acromegalia", limit=5, collection="pcdt"
        )

        assert records == []
        assert meta.error is True
        assert meta.error_kind != "successful_empty"
        assert meta.error_kind == "backend_error"
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


def _pcdt_engine(tmp_path):
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "test.db", settings=settings)
    return GovBrPCDTEngine(http_client=AsyncMock(), cache=cache, settings=settings), cache


async def test_get_catalog_serves_seed_without_network_or_cache_row(tmp_path):
    """The bundled seed is the catalog: no crawl and no SQLite copy of it."""
    engine, cache = _pcdt_engine(tmp_path)
    try:
        catalog = await engine.get_catalog()

        assert "pcdt-acromegalia" in catalog
        engine.http_client.get.assert_not_awaited()
        _, meta = await cache.get("govbr_pcdt:catalog")
        assert meta.cached is False, "the seed must not be copied into SQLite"
    finally:
        await cache.close()


async def test_get_catalog_seed_beats_a_leftover_cache_row(tmp_path):
    """A catalog row written by an older release must not shadow the seed."""
    engine, cache = _pcdt_engine(tmp_path)
    try:
        await cache.set(
            "govbr_pcdt:catalog",
            {"pcdt-old": {"record_id": "pcdt-old", "slug": "old", "title": "Old"}},
            source="govbr_pcdt",
            ttl=SEVEN_DAYS_SECONDS,
        )

        catalog = await engine.get_catalog()

        assert "pcdt-old" not in catalog
        assert "pcdt-acromegalia" in catalog
    finally:
        await cache.close()


async def test_missing_seed_is_an_outage_not_a_partial_catalog(tmp_path, monkeypatch):
    """Extended rows alone are a partial catalog: report the outage instead."""
    monkeypatch.setattr(govbr_pcdt, "load_seed_catalog", dict)
    engine, cache = _pcdt_engine(tmp_path)
    try:
        catalog = await engine.get_catalog()

        assert catalog == {}
        assert engine._memory_catalog is None
        engine.http_client.get.assert_not_awaited()

        results, meta = await engine.search("acromegalia", limit=5)
        assert results == []
        assert meta.error is True
        assert meta.error_kind == "backend_error"
        key = f"govbr_pcdt_search:{CACHE_SCHEMA}:5:{normalize_text('acromegalia')}"
        _, cache_meta = await cache.get(key)
        assert cache_meta.cached is False
    finally:
        await cache.close()
