"""Extended MoH catalog search and offline full-text (Task 3)."""

import dataclasses
from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import BrazilMoHEngine
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager


async def _engine(tmp_path: Path):
    settings = dataclasses.replace(Settings.load(), brazil_browser_fallback=False)
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)
    return engine, cache, http_client


async def test_pcdt_search_finds_tb_manual(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        records, _ = await engine.search_guidelines(
            "tuberculose acolhimento ubs", collection="pcdt"
        )
        assert "ms-manual-tuberculose-2019" in [r.record_id for r in records]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_tb_full_text_served_from_disk_offline(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, _ = await engine.get_full_text(
            "ms-manual-tuberculose-2019", max_chars=10000
        )
        assert payload["status"] == "success"
        assert payload["content_type"] == "text"
        assert payload["total_chars"] > 1000
        # Task 1 contract: over-ceiling source sliced to the caller limit.
        assert payload["truncated"] is True
        assert len(payload["content"]) <= 10000 + 100
        # The anchor phrase sits past the 10k slice; the ceiling slice holds it.
        full_slice, _ = await engine.get_full_text(
            "ms-manual-tuberculose-2019", max_chars=50000
        )
        assert "porta de entrada" in full_slice["content"].lower()
        assert respx.calls.call_count == 0
    finally:
        await cache.close()
        await http_client.aclose()


async def test_pcdt_search_finds_trauma_manual(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        records, _ = await engine.search_guidelines(
            "trauma pelvico instavel", collection="pcdt"
        )
        assert "sbait-trauma-pelvico-2020" in [r.record_id for r in records]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_trauma_full_text_contains_fixador_externo_offline(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, _ = await engine.get_full_text("sbait-trauma-pelvico-2020")
        assert payload["status"] == "success"
        assert payload["content_type"] == "text"
        assert payload["total_chars"] > 1000
        assert "fixador externo" in payload["content"].lower()
        assert respx.calls.call_count == 0
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_pnab_full_text_served_offline(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, _ = await engine.get_full_text("ms-pnab-portaria-2436-2017")
        assert payload["status"] == "success"
        assert payload["content_type"] == "text"
        assert payload["total_chars"] > 1000
        assert respx.calls.call_count == 0
    finally:
        await cache.close()
        await http_client.aclose()
