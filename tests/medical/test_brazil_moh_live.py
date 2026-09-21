from pathlib import Path

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import BrazilMoHEngine
from scholar_mcp.medical.ranking import normalize_portuguese
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

pytestmark = pytest.mark.network


@pytest.mark.asyncio
async def test_live_bvs_dengue_classification_risk_returns_ms_manual(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client, cache, settings)
    try:
        records, meta = await engine.search_guidelines("dengue classificação risco", limit=5)
        assert not meta.error
        assert any(
            "dengue" in r.title.lower() and "classificação de risco" in r.title.lower()
            for r in records
        )
    finally:
        await cache.close()
        await http_client.aclose()


@pytest.mark.asyncio
async def test_live_bvs_cervical_cancer_screening_returns_guideline(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client, cache, settings)
    try:
        records, meta = await engine.search_guidelines(
            "diretrizes rastreamento câncer colo", limit=15
        )
        assert not meta.error
        assert any(
            "diretrizes brasileiras para o rastreamento do câncer do colo do útero" in r.title.lower()
            for r in records
        )
    finally:
        await cache.close()
        await http_client.aclose()


@pytest.mark.asyncio
async def test_live_scenario_query_surfaces_the_dengue_manual(tmp_path: Path):
    """The spec's q26 acceptance criterion.

    This is the exact query the exam pipeline issued in run 10. Before the
    OR-title stage every AND conjunction returned zero and the all-field
    fallback returned topically adjacent journal articles instead.
    """
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client, cache, settings)
    try:
        records, meta = await engine.search_guidelines(
            "dengue grupo B manejo hidratação parenteral observação", limit=10
        )
        assert meta.error is False, "BVS flaps 502; re-run before treating as a failure"
        titles = [normalize_portuguese(r.title) for r in records]
        assert any(
            "dengue" in t and "manejo" in t for t in titles
        ), titles
    finally:
        await cache.close()
        await http_client.aclose()
