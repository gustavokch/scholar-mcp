"""Live gov.br tests. Run with: uv run --extra dev pytest -m network"""

from pathlib import Path
import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import BrazilMoHEngine
from scholar_mcp.medical.govbr_az import GovBrAZEngine
from scholar_mcp.medical.govbr_common import normalize_text
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

pytestmark = pytest.mark.network


@pytest.fixture
async def az_engine(tmp_path: Path):
    settings = Settings()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(tmp_path / "live.db", settings=settings)
    engine = GovBrAZEngine(http_client, cache, settings)
    try:
        yield engine
    finally:
        await cache.close()
        await http_client.aclose()


@pytest.fixture
async def brazil_engine_live(tmp_path: Path):
    settings = Settings()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(tmp_path / "live_brazil.db", settings=settings)
    engine = BrazilMoHEngine(http_client, cache, settings)
    try:
        yield engine
    finally:
        await cache.close()
        await http_client.aclose()


async def test_live_search_finds_dengue_clinical_manual(az_engine):
    results, meta = await az_engine.search("dengue manejo clinico", limit=10)
    assert meta.error is False
    titles = [normalize_text(r.title) for r in results]
    assert any("dengue" in t and "manejo" in t for t in titles), titles


async def test_live_search_finds_tuberculosis_control_manual(az_engine):
    results, meta = await az_engine.search("tuberculose", limit=10)
    assert meta.error is False
    titles = [normalize_text(r.title) for r in results]
    assert any("tuberculose" in t for t in titles), titles


async def test_live_full_text_extracts_dengue_pdf(brazil_engine_live, az_engine):
    results, _ = await az_engine.search("dengue manejo clinico", limit=5)
    assert results
    payload, meta = await brazil_engine_live.get_full_text(results[0].record_id)
    assert payload["status"] == "success", payload
    assert payload["content_type"] == "pdf"
    assert len(payload["content"]) > 1000


async def test_live_default_collection_surfaces_az_records(brazil_engine_live):
    results, meta = await brazil_engine_live.search_guidelines("tuberculose", limit=20)
    assert meta.error is False
    assert any(r.record_id.startswith("govbr-") for r in results), [
        r.record_id for r in results
    ]
