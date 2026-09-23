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

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import (
    ABSTRACT_MAX_CHARS,
    BVS_SEARCH_URL,
    BrazilMoHEngine,
    _SearchState,
)
from scholar_mcp.medical.models import BrazilGuideline
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.rate_limit import AsyncRateLimiter
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager


def _pin_fast_limiter(http_client, rate_per_sec: float = 50.0) -> None:
    """Give ``http_client`` a private, full, fast token bucket.

    The real ``pesquisa.bvsalud.org`` bucket is 1 req/s and the limiter
    registry is process-global, so a test that walks the retry ladder
    otherwise pays ~1 s of wall clock per attempt AND inherits whatever debt
    an earlier test left on the shared bucket. Tests that assert attempt
    counts or failure classification care about neither.
    """
    limiter = AsyncRateLimiter(rate_per_sec=rate_per_sec)
    http_client._limiter_for_url = lambda url: limiter


async def _engine(
    tmp_path: Path,
    stub_local: bool = True,
    backoff_base: float = 0.5,
    **settings_overrides,
):
    # Browser tier stays off unless a test opts in: the default rides in
    # the same dict as the overrides so `brazil_browser_fallback=True`
    # wins instead of colliding with a hardcoded keyword.
    #
    # ``backoff_base`` is a named parameter rather than part of
    # ``settings_overrides``: it belongs to AsyncHttpClient, not to Settings,
    # and dataclasses.replace would raise on it. It defaults to the production
    # value so existing tests keep their behaviour; a test that walks the retry
    # ladder wants 0.01 here AND ``_pin_fast_limiter`` above -- the limiter, at
    # 1 req/s, is the larger of the two costs.
    defaults = {"brazil_browser_fallback": False}
    defaults.update(settings_overrides)
    settings = dataclasses.replace(Settings.load(), **defaults)
    http_client = AsyncHttpClient(settings, backoff_base=backoff_base)
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
    engine, cache, http_client = await _engine(tmp_path, backoff_base=0.01)
    _pin_fast_limiter(http_client)
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
        # A 5xx is retried now: the host answers per-request, not per-outage.
        assert first_calls == http_client.max_retries
        records2, meta2 = await engine.search_guidelines("dengue", limit=10)
        assert records2 == [] and meta2.error_kind == "origin_outage"
        # The outage itself is still never cached: the second search pays a
        # full second ladder rather than replaying a stored failure. Stated as
        # a sum, not as `first_calls * 2` -- a ratio moves on both sides when
        # the ladder length changes, so it can never fail for that reason.
        assert len(route.calls) == first_calls + http_client.max_retries
    finally:
        await cache.close()
        await http_client.aclose()


async def test_timeout_state_maps_to_timeout_kind(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        state = _SearchState()
        engine._mark_bvs_timed_out(state)
        meta = engine._search_meta(error=True, state=state, records=[])
        assert meta.error_kind == "timeout"
        assert meta.timeout is True
    finally:
        await cache.close()
        await http_client.aclose()


async def test_camoufox_nav_timeout_clamped_to_small_ceiling(tmp_path, monkeypatch):
    """The nav timeout is clamped to the ceiling minus whatever the launch
    itself already spent, so it lands at or just under the ceiling -- never
    over it."""
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
        await engine._camoufox_search("dengue", 10, ceiling=25.0)
        assert 24000 <= captured["timeout"] <= 25000
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
            return None, None

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
            return _build_record(doc), None

        async def _fast_pdf(url):
            return "texto do pdf", None

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


@respx.mock
async def test_fulltext_pdf_failure_with_abstract_is_success_not_error(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-pdf-fail", ab=["Resumo preservado."])
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        respx.get(url__startswith="https://fi-admin.bvsalud.org").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/html"}, text="<html>WAF</html>"
            )
        )
        payload, meta = await engine.get_full_text("biblio-pdf-fail")
        assert payload["status"] == "success"
        assert payload["content_type"] == "abstract"
        assert payload["abstract_fallback"] is True
        assert meta.error is False
        # error_kind still carries the real PDF-fetch failure so machine
        # consumers see the degradation, even though error itself is False.
        assert meta.error_kind == "backend_error"
        # Degraded but reachable: cached briefly, never for the 30-day TTL.
        payload2, meta2 = await engine.get_full_text("biblio-pdf-fail")
        assert meta2.cached is True
        assert payload2["content_type"] == "abstract"
        # The degradation travels with the cached row, so the second caller
        # reads the same kind as the first (see the cache-hit test below).
        assert meta2.error_kind == "backend_error"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_fulltext_cache_hit_keeps_degraded_error_kind(tmp_path: Path):
    """A hit inside the degraded TTL reports the kind the first caller saw.

    The degraded payload is held for 300 s. If the kind does not travel with
    the row, only the first request in that window reports ``degraded``, and a
    machine consumer polling behind it cannot tell "not degraded" from
    "degraded, but you asked second".
    """
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-outage-abs", ab=["Resumo preservado."])
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        respx.get(url__startswith="https://fi-admin.bvsalud.org").mock(
            return_value=httpx.Response(503, text="Erro 503 - Service Unavailable")
        )
        payload, meta = await engine.get_full_text("biblio-outage-abs")
        assert payload["content_type"] == "abstract"
        assert meta.cached is False
        assert meta.error_kind == "origin_outage"

        payload2, meta2 = await engine.get_full_text("biblio-outage-abs")
        assert meta2.cached is True
        assert payload2["content_type"] == "abstract"
        assert meta2.error_kind == "origin_outage"
        # The stored kind is an internal field of the cached row, never a key
        # the tool hands back.
        assert "_error_kind" not in payload
        assert "_error_kind" not in payload2
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_fulltext_origin_outage_without_abstract_keeps_its_kind(tmp_path: Path):
    """No PDF and no abstract must still report the classified kind.

    ``origin_outage`` must not count against a caller-side breaker (module
    docstring, AGENTS.md Decision 10), so reporting a sick document host as
    ``backend_error`` on the one branch that has nothing to fall back on
    defeats the taxonomy exactly where the caller needs it.
    """
    engine, cache, http_client = await _engine(tmp_path)
    try:
        # No ``ab``, no ``mh``, no ``ti_en``: the record carries no abstract,
        # synthetic or otherwise.
        doc = _bvs_doc(record_id="biblio-outage-bare")
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        respx.get(url__startswith="https://fi-admin.bvsalud.org").mock(
            return_value=httpx.Response(503, text="Erro 503 - Service Unavailable")
        )
        payload, meta = await engine.get_full_text("biblio-outage-bare")
        assert payload["status"] == "error"
        assert payload["content_type"] == "none"
        assert meta.error is True
        assert meta.error_kind == "origin_outage"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_fulltext_lookup_challenge_kind_propagates(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                403,
                headers={"content-type": "text/html"},
                text='<iframe src="https://shield-templates-prod.b-cdn.net/x/block.html">',
            )
        )
        payload, meta = await engine.get_full_text("biblio-x")
        assert payload["status"] == "error"
        assert meta.error is True
        assert meta.error_kind == "cdn_challenge"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_lookup_record_retries_5xx_then_reports_outage(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path, backoff_base=0.01)
    _pin_fast_limiter(http_client)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(500, text="Erro 504 - Gateway Timeout")
        )
        record, kind = await engine._lookup_record("biblio-x")
        assert record is None
        assert kind == "origin_outage"
        assert len(route.calls) == http_client.max_retries, (
            "a 5xx lookup retries like the search path, then reports the outage"
        )
    finally:
        await cache.close()
        await http_client.aclose()


async def test_pcdt_collection_applies_since_year(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path, stub_local=False)
    try:
        async def _pcdt_search(query, limit=10):
            return (
                [
                    BrazilGuideline(record_id="old", title="Guia antigo", year="2012", abstract="x"),
                    BrazilGuideline(record_id="new", title="Guia atual", year="2025", abstract="y"),
                ],
                CacheMetadata(cached=False, cache_age=0, error=False),
            )

        engine.pcdt_engine.search = _pcdt_search
        records, meta = await engine.search_guidelines("guia", collection="pcdt", since_year=2024)
        assert [r.record_id for r in records] == ["new"]
        assert meta.error_kind == "ok"
    finally:
        await cache.close()
        await http_client.aclose()


async def test_pcdt_collection_since_year_fills_the_requested_limit(tmp_path: Path):
    """The sub-engine slices to the limit it is handed, so the year filter
    cannot run after that slice: with ten 2012 rows ranked ahead of five 2025
    ones, a caller asking ``limit=3, since_year=2024`` must still receive
    three rows, not zero."""
    engine, cache, http_client = await _engine(tmp_path)
    try:
        catalog = [
            BrazilGuideline(record_id=f"old-{i}", title="Guia antigo", year="2012")
            for i in range(10)
        ] + [
            BrazilGuideline(record_id=f"new-{i}", title="Guia atual", year="2025")
            for i in range(5)
        ]
        asked: list[int] = []

        async def _pcdt_search(query, limit=10):
            asked.append(limit)
            # Mirrors the real engine: score, sort, then slice to `limit`.
            return catalog[:limit], CacheMetadata(cached=False, cache_age=0, error=False)

        engine.pcdt_engine.search = _pcdt_search
        records, _meta = await engine.search_guidelines(
            "guia", limit=3, collection="pcdt", since_year=2024
        )
        assert [r.record_id for r in records] == ["new-0", "new-1", "new-2"]
        assert asked[0] > 3, "the year filter needs a pool wider than the caller's limit"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_local_fallback_since_year_filters_before_slicing(tmp_path: Path):
    """BVS down, local gov.br rows survive: filter by year, then slice.

    The ranker puts the two exact-title 2012 rows first, so slicing to the
    caller's limit before dropping pre-2024 rows returns nothing while two
    matching 2025 rows sat in the pool. This is the engine's main degradation
    mode, so the ordering has to hold here too.
    """
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(500, text="Erro 504 - Gateway Timeout")
        )

        async def _pcdt_search(query, limit=10):
            return (
                [
                    BrazilGuideline(
                        record_id="old-1", title="dengue manejo clinico", year="2012"
                    ),
                    BrazilGuideline(
                        record_id="old-2", title="dengue manejo clinico adulto", year="2013"
                    ),
                    BrazilGuideline(
                        record_id="new-1", title="tuberculose diagnostico", year="2025"
                    ),
                    BrazilGuideline(
                        record_id="new-2", title="hanseniase tratamento", year="2025"
                    ),
                ],
                CacheMetadata(cached=False, cache_age=0, error=False),
            )

        engine.pcdt_engine.search = _pcdt_search
        records, meta = await engine.search_guidelines(
            "dengue manejo clinico", limit=2, since_year=2024
        )
        assert {r.record_id for r in records} == {"new-1", "new-2"}
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_browser_tier_recomputes_adaptive_overfetch(tmp_path, monkeypatch):
    engine, cache, http_client = await _engine(
        tmp_path, brazil_browser_fallback=True, enable_browser_fallback=True
    )
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(500, text="Erro 504 - Gateway Timeout")
        )
        calls: list[int] = []

        def _spy(clamped, chain_start=None):
            calls.append(clamped)
            return 7  # sentinel: any recompute is observable

        monkeypatch.setattr(engine, "_overfetch_count", _spy)
        seen: list[int] = []

        async def _fake_browser(composed, count, ceiling):
            seen.append(count)
            return []

        engine._camoufox_search = _fake_browser
        await engine.search_guidelines("dengue hidratacao", limit=10)
        assert calls == [10, 10], (
            "the browser tier must recompute the overfetch window, not reuse the chain-start count"
        )
        assert seen == [7]
    finally:
        await cache.close()
        await http_client.aclose()


async def test_camoufox_skips_when_ceiling_below_useful_floor(tmp_path, monkeypatch):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        constructed: list[int] = []

        class _Exploding:
            def __init__(self, **kw):
                constructed.append(1)
                raise AssertionError("camoufox must not launch under a tiny ceiling")

        api_mod = types.ModuleType("camoufox.async_api")
        api_mod.AsyncCamoufox = _Exploding
        camoufox_mod = types.ModuleType("camoufox")
        camoufox_mod.async_api = api_mod
        monkeypatch.setitem(sys.modules, "camoufox", camoufox_mod)
        monkeypatch.setitem(sys.modules, "camoufox.async_api", api_mod)

        assert await engine._camoufox_search("dengue", 10, ceiling=0.5) == []
        assert constructed == [], "camoufox must not launch under a tiny ceiling"
    finally:
        await cache.close()
        await http_client.aclose()


def test_build_record_marks_synthesized_abstract():
    from scholar_mcp.medical.brazil_moh import _build_record

    assert _build_record(_bvs_doc(ab=["Resumo real."])).abstract_synthetic is False
    assert _build_record(_bvs_doc(mh=["Tuberculose"])).abstract_synthetic is True


@respx.mock
async def test_fulltext_synthetic_abstract_not_served_as_abstract(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-decs", mh=["Tuberculose", "Atenção Primária"])
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        respx.get(url__startswith="https://fi-admin.bvsalud.org").mock(
            return_value=httpx.Response(
                200, headers={"content-type": "text/html"}, text="<html>WAF</html>"
            )
        )
        payload, _ = await engine.get_full_text("biblio-decs")
        assert payload["content_type"] == "none"
        assert payload["abstract_fallback"] is False
    finally:
        await cache.close()
        await http_client.aclose()


async def test_cache_stats_and_close_serialize_with_writers(tmp_path: Path):
    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    async def _writer(i: int):
        await cache.set(f"k{i}", {"v": i}, source="brazil_moh")
        await cache.get(f"k{i}")

    async def _stats():
        for _ in range(5):
            stats = await cache.get_stats()
            assert "total_entries" in stats

    await asyncio.gather(
        *[_writer(i) for i in range(16)],
        *[_stats() for _ in range(4)],
    )
    assert (await cache.get_stats())["total_entries"] >= 16
    await cache.close()


@respx.mock
async def test_cache_hit_emits_s0_1_diagnostics(tmp_path: Path, caplog):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        import logging

        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        await engine.search_guidelines("dengue", limit=10)
        with caplog.at_level(logging.INFO, logger="scholar_mcp.medical.brazil_moh"):
            records, meta = await engine.search_guidelines("dengue", limit=10)
        assert meta.cached is True
        lines = [r.getMessage() for r in caplog.records if "rerank_in=" in (r.getMessage() or "")]
        assert lines, "cache hit must still emit the S0.1 line"
        for field in ("http_status=", "challenge_hit=", "cache_hit=True", "overfetch_window="):
            assert field in lines[-1]
    finally:
        await cache.close()
        await http_client.aclose()


def test_config_garbage_brazil_timeout_falls_back(monkeypatch):
    monkeypatch.setenv("BRAZIL_FULLTEXT_TIMEOUT_S", "30s")
    assert Settings.load().brazil_fulltext_timeout_s == 30.0


def test_config_valid_brazil_timeout_env_wins(monkeypatch):
    monkeypatch.setenv("BRAZIL_FULLTEXT_TIMEOUT_S", "12.5")
    assert Settings.load().brazil_fulltext_timeout_s == 12.5


def test_extract_doi_strips_query_and_braces():
    from scholar_mcp.medical.brazil_moh import _extract_doi

    assert _extract_doi({"ur": ["https://doi.org/10.1016/j.lana.2024.100123?utm_source=x"]}) == (
        "10.1016/j.lana.2024.100123"
    )
    assert _extract_doi({"ur": ["https://doi.org/10.1590/abc}"]}) == "10.1590/abc"
    assert _extract_doi({"ur": ["http://site/v10.1234/5678/file.pdf"]}) == ""


def test_catalog_doi_is_normalized():
    from scholar_mcp.medical.govbr_pcdt import _dict_to_guideline

    assert _dict_to_guideline({"doi": "  10.1590/abc.  "}).doi == "10.1590/abc"
    assert _dict_to_guideline({}).doi == ""
