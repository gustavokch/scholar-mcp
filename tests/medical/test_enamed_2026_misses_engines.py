"""ENAMED 2026 misses, track B: engine-side contract tests (S0/S1/S2).

Covers the §2 contract (error kinds, budgets, stable record_id) and the
phase acceptance criteria: diagnostics emission, BVS root-cause behavior,
and result-quality guarantees. BVS HTTP is mocked with respx; the browser
tier stays off unless a test installs its own fake.
"""

import asyncio
import dataclasses
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import (
    ABSTRACT_MAX_CHARS,
    BVS_SEARCH_URL,
    BrazilMoHEngine,
    _SearchState,
    bvs_budget_contract,
)
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager


async def _engine(tmp_path: Path, stub_local: bool = True, **settings_overrides):
    settings = dataclasses.replace(
        Settings.load(), brazil_browser_fallback=False, **settings_overrides
    )
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)

    async def _empty(*args, **kwargs):
        return [], CacheMetadata(cached=False, cache_age=0, error=False)

    if stub_local:
        engine.pcdt_engine.search = _empty
        engine.az_engine.search = _empty
    else:
        # S2.5 corpus tests need the real PCDT engine (extended catalog);
        # only the network-backed A-Z stage is stubbed.
        engine.az_engine.search = _empty
    return engine, cache, http_client


def _bvs_doc(record_id="biblio-1", title="Protocolo", country="^iBrazil^eBrasil", **extra):
    doc = {
        "id": record_id,
        "ti": [title],
        "la": ["pt"],
        "da": "202609",
        "pais_publicacao": [country],
        "ur": ["https://fi-admin.bvsalud.org/document/view/cfpaj"],
    }
    doc.update(extra)
    return doc


def _bvs_response(docs):
    return {
        "diaServerResponse": [
            {"responseHeader": {"status": 0}, "response": {"numFound": len(docs), "docs": docs}}
        ]
    }


# S0.1 — diagnostics ------------------------------------------------------


@respx.mock
async def test_success_meta_carries_contract_fields(tmp_path: Path, caplog):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        import logging

        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        with caplog.at_level(logging.INFO, logger="scholar_mcp.medical.brazil_moh"):
            records, meta = await engine.search_guidelines("dengue", limit=10)
        assert len(records) == 1
        assert meta.error is False
        assert meta.error_kind == "ok"
        assert meta.http_status == 200
        assert meta.challenge_hit is False
        assert meta.timeout is False
        diagnostics = [
            r for r in caplog.records if "rerank_in=" in (r.getMessage() or "")
        ]
        assert diagnostics, "expected one S0.1 per-call diagnostics line"
        line = diagnostics[0].getMessage()
        for field in ("elapsed=", "http_status=", "challenge_hit=", "cache_hit=",
                      "timeout=", "overfetch_window=", "rerank_in=", "rerank_out="):
            assert field in line
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_empty_success_is_successful_empty_not_error(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        records, meta = await engine.search_guidelines("dengue", limit=10)
        assert records == []
        assert meta.error is False
        assert meta.error_kind == "successful_empty"
    finally:
        await cache.close()
        await http_client.aclose()


# S1 — error taxonomy ------------------------------------------------------


@respx.mock
async def test_403_challenge_classified_and_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        # Challenge-HTML 403 (Bunny shield page) fails fast: no plain-HTTP
        # retry can pass it, so the stage burns one call, not the ladder.
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                403,
                headers={"content-type": "text/html"},
                text='<iframe src="https://shield-templates-prod.b-cdn.net/x/block.html">',
            )
        )
        records, meta = await engine.search_guidelines("dengue", limit=10)
        assert records == []
        assert meta.error is True
        assert meta.error_kind == "cdn_challenge"
        assert meta.http_status == 403
        assert meta.challenge_hit is True
        first_calls = len(route.calls)
        assert first_calls == 1
        # A challenge verdict must not be pinned: replay re-hits the network.
        records2, meta2 = await engine.search_guidelines("dengue", limit=10)
        assert records2 == [] and meta2.error_kind == "cdn_challenge"
        assert len(route.calls) == first_calls + 1
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_plain_403_retried_then_still_challenge(tmp_path: Path):
    """A burst 403 without challenge markers retries like a 429, then lands
    on the same cdn_challenge verdict (not a generic backend_error)."""
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(403, text="blocked")
        )
        records, meta = await engine.search_guidelines("dengue", limit=10)
        assert records == []
        assert meta.error_kind == "cdn_challenge"
        assert meta.challenge_hit is True
        assert len(route.calls) > 1  # burst-retry ladder ran
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_500_origin_outage_classified_and_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(500, text="Erro 504 - Gateway Timeout")
        )
        records, meta = await engine.search_guidelines("dengue", limit=10)
        assert records == []
        assert meta.error is True
        assert meta.error_kind == "origin_outage"
        assert meta.http_status == 500
        assert meta.challenge_hit is False
        first_calls = len(route.calls)
        assert first_calls == 1  # fail fast: no 5xx retry ladder
        records2, meta2 = await engine.search_guidelines("dengue", limit=10)
        assert records2 == [] and meta2.error_kind == "origin_outage"
        assert len(route.calls) == first_calls + 1
    finally:
        await cache.close()
        await http_client.aclose()


async def test_timeout_state_maps_to_timeout_kind(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        state = _SearchState()
        engine._mark_bvs_timed_out(state)
        meta = engine._search_meta(
            error=True, state=state, records=[],
            elapsed_s=1.0, rerank_in=0, rerank_out=0,
        )
        assert meta.error_kind == "timeout"
        assert meta.timeout is True
    finally:
        await cache.close()
        await http_client.aclose()


# S1.1 — published budgets --------------------------------------------------


def test_budget_contract_publishes_search_vs_fulltext():
    settings = Settings.load()
    contract = bvs_budget_contract(settings)
    assert contract["search_chain_ceiling_s"] == 90.0
    assert contract["search_stage_ceiling_s"] == 20.0
    assert contract["browser_tier_ceiling_s"] == 45.0
    assert contract["browser_nav_ceiling_s"] == 30.0
    assert contract["fulltext_ceiling_s"] == 30.0
    # The nav ceiling can never silently exceed the tier ceiling.
    assert contract["browser_nav_ceiling_s"] <= contract["browser_tier_ceiling_s"]


async def test_camoufox_nav_timeout_clamped_to_small_ceiling(tmp_path, monkeypatch):
    engine, cache, http_client = await _engine(tmp_path)
    captured = {}

    class _FakePage:
        async def goto(self, url, *a, **k):
            captured["timeout"] = k.get("timeout")
            return None

        async def content(self):
            return "<html>not json</html>"

    class _FakeBrowser:
        async def new_page(self, *a, **k):
            return _FakePage()

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeBrowser()

        async def __aexit__(self, *exc):
            return False

    api_mod = types.ModuleType("camoufox.async_api")
    api_mod.AsyncCamoufox = lambda **kw: _FakeCtx()
    camoufox_mod = types.ModuleType("camoufox")
    camoufox_mod.async_api = api_mod
    monkeypatch.setitem(sys.modules, "camoufox", camoufox_mod)
    monkeypatch.setitem(sys.modules, "camoufox.async_api", api_mod)
    try:
        await engine._camoufox_search("dengue", 10, ceiling=5.0)
        assert captured["timeout"] == 5000
    finally:
        await cache.close()
        await http_client.aclose()


# S1.2 — session/cache contention --------------------------------------------


async def test_sqlite_cache_survives_concurrent_writers(tmp_path: Path):
    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    try:
        async def _write(i: int):
            await cache.set(f"key-{i}", {"v": i}, source="brazil_moh")
            data, meta = await cache.get(f"key-{i}")
            assert meta.cached and data == {"v": i}

        await asyncio.gather(*[_write(i) for i in range(16)])
        stats = await cache.get_stats()
        assert stats["total_entries"] >= 16
    finally:
        await cache.close()


# S1.4 — adaptive overfetch --------------------------------------------------


async def test_overfetch_full_window_when_chain_fresh(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        assert engine._overfetch_count(10, time.monotonic()) == 100
        assert engine._overfetch_count(50, time.monotonic()) == 200
    finally:
        await cache.close()
        await http_client.aclose()


async def test_overfetch_shrinks_when_chain_half_burned(tmp_path: Path):
    engine, cache, http_client = await _engine(
        tmp_path, brazil_chain_timeout_s=10.0
    )
    try:
        stale_start = time.monotonic() - 6.0  # past half of a 10 s chain
        assert engine._overfetch_count(10, stale_start) == 30
    finally:
        await cache.close()
        await http_client.aclose()


# S2.1 — abstract guarantee ---------------------------------------------------


def test_build_record_caps_long_abstract():
    from scholar_mcp.medical.brazil_moh import _build_record

    record = _build_record(_bvs_doc(ab=["x" * 5000]))
    assert len(record.abstract) == ABSTRACT_MAX_CHARS == 2000


def test_build_record_mesh_fallback_when_ab_missing():
    from scholar_mcp.medical.brazil_moh import _build_record

    record = _build_record(_bvs_doc(mh=["Tuberculose", "Atenção Primária"]))
    assert record.abstract, "expected a decidable body from DeCS descriptors"
    assert "Tuberculose" in record.abstract


def test_build_record_title_en_fallback_without_mesh():
    from scholar_mcp.medical.brazil_moh import _build_record

    doc = _bvs_doc()
    doc.pop("ur", None)
    doc["ti_en"] = ["Clinical protocol in English"]
    record = _build_record(doc)
    assert record.abstract == "Clinical protocol in English"


def test_build_record_genuinely_textless_stays_empty():
    from scholar_mcp.medical.brazil_moh import _build_record

    record = _build_record({"id": "biblio-x"})
    assert record.abstract == ""


# S2.2 — recency ---------------------------------------------------------------


@respx.mock
async def test_since_year_filters_pre_2015_pool(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        docs = [
            _bvs_doc(record_id="biblio-old", title="Guia antigo", da="201205"),
            _bvs_doc(record_id="biblio-new", title="Guia atual", da="202501"),
        ]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response(docs))
        )
        records, meta = await engine.search_guidelines(
            "guia", limit=10, since_year=2024
        )
        assert [r.record_id for r in records] == ["biblio-new"]
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_since_year_keeps_yearless_records(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-noyear", title="Guia sem data")
        doc.pop("da", None)
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        records, _ = await engine.search_guidelines("guia", limit=10, since_year=2024)
        assert [r.record_id for r in records] == ["biblio-noyear"]
    finally:
        await cache.close()
        await http_client.aclose()


# S2.3 — metadata enrichment ----------------------------------------------------


def test_extract_doi_from_record_links():
    from scholar_mcp.medical.brazil_moh import _build_record

    doc = _bvs_doc(
        ur=["https://fi-admin.bvsalud.org/document/view/cfpaj",
            "https://doi.org/10.1016/j.lana.2024.100123."]
    )
    assert _build_record(doc).doi == "10.1016/j.lana.2024.100123"


def test_record_id_survives_roundtrip_for_brmoh_fold():
    from scholar_mcp.medical.models import BrazilGuideline

    record = BrazilGuideline(record_id="biblio-1701387", doi="10.1/abc")
    restored = BrazilGuideline.from_dict(record.to_dict())
    assert restored.record_id == "biblio-1701387"
    assert restored.doi == "10.1/abc"


# S2.4 — abstract fallback flag --------------------------------------------------


@respx.mock
async def test_fulltext_offsite_abstract_is_flagged_not_silent(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(
            record_id="biblio-off",
            title="Documento externo",
            ab=["Resumo disponível no registro."],
            ur=["https://www.sciencedirect.com/science/article/pii/S123"],
        )
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        payload, meta = await engine.get_full_text("biblio-off")
        assert payload["status"] == "success"
        assert payload["content_type"] == "abstract"
        assert payload["abstract_fallback"] is True
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_fulltext_timeout_maps_to_timeout_kind(tmp_path: Path):
    engine, cache, http_client = await _engine(
        tmp_path, brazil_fulltext_timeout_s=0.05
    )
    try:
        async def _slow_lookup(record_id):
            await asyncio.sleep(5.0)
            return None, False

        engine._lookup_record = _slow_lookup  # type: ignore[method-assign]
        payload, meta = await engine.get_full_text("biblio-slow")
        assert payload["status"] == "error"
        assert payload["abstract_fallback"] is False
        assert meta.error_kind == "timeout"
        assert meta.timeout is True
    finally:
        await cache.close()
        await http_client.aclose()


# S2.5 — corpus additions ---------------------------------------------------------


async def test_2025_aps_indicators_retrievable_via_title(tmp_path: Path):
    # No respx: the bundled corpus path must make zero network calls, so
    # any live request would fail loudly instead of hiding behind a mock.
    engine, cache, http_client = await _engine(tmp_path, stub_local=False)
    try:
        records, _ = await engine.search_guidelines(
            "aps cofinanciamento indicadores 2025", collection="pcdt"
        )
        assert "ms-portaria-aps-cofinanciamento-2025" in [r.record_id for r in records]
        payload, _ = await engine.get_full_text(
            "ms-portaria-aps-cofinanciamento-2025", max_chars=10000
        )
        assert payload["status"] == "success"
        assert "cofinanciamento" in payload["content"].lower()
    finally:
        await cache.close()
        await http_client.aclose()


async def test_rs_flood_doctrine_retrievable_via_title(tmp_path: Path):
    # No respx: see above -- the corpus path is offline by construction.
    engine, cache, http_client = await _engine(tmp_path, stub_local=False)
    try:
        records, _ = await engine.search_guidelines(
            "enchentes vigilancia rio grande sul", collection="pcdt"
        )
        assert "ms-vigilancia-enchentes-rs-2025" in [r.record_id for r in records]
        payload, _ = await engine.get_full_text(
            "ms-vigilancia-enchentes-rs-2025", max_chars=10000
        )
        assert payload["status"] == "success"
        assert "leptospirose" in payload["content"].lower()
    finally:
        await cache.close()
        await http_client.aclose()


def test_new_corpus_files_carry_provenance_and_length():
    data_dir = Path(__file__).resolve().parents[2] / "src" / "scholar_mcp" / "data"
    for rel in (
        "guidelines/indicadores_aps_cofinanciamento_2025.txt",
        "guidelines/vigilancia_enchentes_rs_2025.txt",
    ):
        text = (data_dir / rel).read_text(encoding="utf-8")
        assert len(text) > 1000
        for marker in ("Fonte:", "URL:", "Extraído em:", "Licença:"):
            assert marker in text, f"{rel} missing provenance marker {marker}"


def test_config_fulltext_ceiling_defaults_and_env(monkeypatch):
    from scholar_mcp.config import Settings

    assert Settings.load().brazil_fulltext_timeout_s == 30.0
    monkeypatch.setenv("BRAZIL_FULLTEXT_TIMEOUT_S", "12.5")
    assert Settings.load().brazil_fulltext_timeout_s == 12.5


@respx.mock
async def test_fulltext_pdf_phase_gets_remaining_budget(tmp_path, monkeypatch):
    from scholar_mcp.medical.brazil_moh import _build_record

    engine, cache, http_client = await _engine(tmp_path, brazil_fulltext_timeout_s=0.4)
    try:
        doc = _bvs_doc(record_id="biblio-rem", ab=["Resumo."])
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )

        async def _slow_lookup(record_id):
            await asyncio.sleep(0.3)
            return _build_record(doc), False

        async def _fast_pdf(url):
            return "texto do pdf", False

        engine._lookup_record = _slow_lookup  # type: ignore[method-assign]
        engine._extract_pdf_text = _fast_pdf  # type: ignore[method-assign]

        from scholar_mcp.medical import brazil_moh as bm

        real_wait_for = bm.asyncio.wait_for
        seen: list[float] = []

        async def _spy(awaitable, timeout=None, **kw):
            seen.append(timeout)
            return await real_wait_for(awaitable, timeout=timeout, **kw)

        monkeypatch.setattr(bm.asyncio, "wait_for", _spy)
        payload, meta = await engine.get_full_text("biblio-rem")
        assert payload["status"] == "success"
        assert seen[-1] <= 0.15, f"PDF phase must get only the remainder, got {seen[-1]}"
    finally:
        await cache.close()
        await http_client.aclose()
