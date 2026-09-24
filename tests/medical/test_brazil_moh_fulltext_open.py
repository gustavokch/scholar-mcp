"""Brazilian MoH full-text open design (D6-D9): dead-host attribution,
``timeout_phase``, PDF parse isolation, and the per-id record cache.

Design: docs/superpowers/specs/2026-09-23-brazil-moh-fulltext-open-design.md

The motivating failure was one log line -- ``full text fetch for
'biblio-935743' exceeded its 30.0s budget`` -- against a host whose recorded
URL is dead: a single hopeless connect consumed the whole ceiling because the
per-attempt timeout was a scalar bounded only by the budget left.
"""

import asyncio
import dataclasses
import threading
import time
from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical import brazil_moh as bm
from scholar_mcp.medical.brazil_moh import (
    BVS_SEARCH_URL,
    CACHE_SCHEMA,
    BrazilMoHEngine,
)
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.rate_limit import AsyncRateLimiter
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

FI_ADMIN_URL = "https://fi-admin.bvsalud.org/document/view/cfpaj"


def _pin_fast_limiter(http_client, rate_per_sec: float = 50.0) -> None:
    """Private, full, fast token bucket (see test_brazil_moh.py)."""
    limiter = AsyncRateLimiter(rate_per_sec=rate_per_sec)
    http_client._limiter_for_url = lambda url: limiter


async def _engine(tmp_path: Path, backoff_base: float = 0.5, **settings_overrides):
    settings = dataclasses.replace(
        Settings.load(),
        brazil_browser_fallback=False,
        **settings_overrides,
    )
    http_client = AsyncHttpClient(settings, backoff_base=backoff_base)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)

    async def _empty(*args, **kwargs):
        return [], CacheMetadata(cached=False, cache_age=0, error=False)

    engine.pcdt_engine.search = _empty
    engine.az_engine.search = _empty
    return engine, cache, http_client


def _bvs_doc(record_id="biblio-1", title="Protocolo", country="^iBrazil^eBrasil", **extra):
    doc = {
        "id": record_id,
        "ti": [title],
        "la": ["pt"],
        "da": "202609",
        "pais_publicacao": [country],
        "ur": [FI_ADMIN_URL],
    }
    doc.update(extra)
    return doc


def _bvs_response(docs, num_found=None):
    return {
        "diaServerResponse": [
            {
                "responseHeader": {"status": 0},
                "response": {
                    "numFound": len(docs) if num_found is None else num_found,
                    "docs": docs,
                },
            }
        ]
    }


def _pdf_response(content: bytes) -> httpx.Response:
    return httpx.Response(
        200, content=content, headers={"content-type": "application/pdf"}
    )


# D9b + D7 ---------------------------------------------------------------------


@respx.mock
async def test_dead_host_fails_fast_and_attributed(tmp_path: Path):
    """An unreachable document host is an ``origin_outage`` attributed to the
    fetch phase -- not a bare timeout that consumed the whole ceiling."""
    engine, cache, http_client = await _engine(tmp_path, backoff_base=0.01)
    _pin_fast_limiter(http_client)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        respx.get(FI_ADMIN_URL).mock(
            side_effect=httpx.ConnectTimeout("Connection timed out")
        )
        started = time.monotonic()
        payload, meta = await engine.get_full_text("biblio-1")
        elapsed = time.monotonic() - started
        assert payload["status"] == "error"
        # The origin is not serving: an outage by the taxonomy's own meaning,
        # exempt from breaker counting. Charged as a timeout it would count.
        assert meta.error_kind == "origin_outage"
        assert payload["timeout_phase"] == "fetch"
        # The ceiling is 30 s; a bounded connect plus one backoff must not
        # consume it.
        assert elapsed < 10.0
    finally:
        await cache.close()
        await http_client.aclose()


# D7 ---------------------------------------------------------------------------


@respx.mock
async def test_timeout_phase_lookup_when_lookup_exceeds_budget(tmp_path: Path):
    """A lookup-phase timeout and a fetch-phase timeout are told apart: the
    slow lookup reports ``timeout_phase == "lookup"``."""
    engine, cache, http_client = await _engine(tmp_path, brazil_fulltext_timeout_s=0.05)
    try:
        async def _slow_lookup(record_id, deadline=None):
            await asyncio.sleep(5.0)
            return None, None

        engine._lookup_record = _slow_lookup  # type: ignore[method-assign]
        payload, meta = await engine.get_full_text("biblio-slow")
        assert payload["status"] == "error"
        assert meta.error_kind == "timeout"
        assert payload["timeout_phase"] == "lookup"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_timeout_phase_none_on_clean_result(tmp_path: Path, monkeypatch):
    """``timeout_phase`` is always present -- null on a clean result, matching
    ``abstract_fallback``'s convention."""
    monkeypatch.setattr(bm, "pdf_bytes_to_text", lambda _: "Texto integral.")
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        respx.get(FI_ADMIN_URL).mock(return_value=_pdf_response(b"%PDF-1.4 fake"))
        payload, _ = await engine.get_full_text("biblio-1")
        assert payload["status"] == "success"
        assert payload["timeout_phase"] is None
    finally:
        await cache.close()
        await http_client.aclose()


# D8 ---------------------------------------------------------------------------


@respx.mock
async def test_pdf_byte_cap_boundary(tmp_path: Path, monkeypatch):
    """At the cap the body is parsed; one byte over is ``backend_error`` and
    the parser never runs."""
    engine, cache, http_client = await _engine(tmp_path)
    engine.settings.brazil_pdf_max_bytes = 100
    parsed: list[int] = []

    def _parser(body: bytes) -> str:
        parsed.append(len(body))
        return "Texto."

    monkeypatch.setattr(bm, "pdf_bytes_to_text", _parser)
    try:
        docs = [
            _bvs_doc(record_id="biblio-at", ur=["https://fi-admin.bvsalud.org/document/view/at"]),
            _bvs_doc(record_id="biblio-over", ur=["https://fi-admin.bvsalud.org/document/view/over"]),
        ]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response(docs))
        )
        respx.get("https://fi-admin.bvsalud.org/document/view/at").mock(
            return_value=_pdf_response(b"x" * 100)
        )
        respx.get("https://fi-admin.bvsalud.org/document/view/over").mock(
            return_value=_pdf_response(b"x" * 101)
        )

        payload, _ = await engine.get_full_text("biblio-at")
        assert payload["status"] == "success"
        assert parsed == [100]

        payload2, meta2 = await engine.get_full_text("biblio-over")
        assert payload2["status"] == "error"
        assert meta2.error_kind == "backend_error"
        assert payload2["content"] == ""
        assert parsed == [100]  # the parser never saw the over-cap body
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_pdf_parse_does_not_block_the_event_loop(tmp_path: Path, monkeypatch):
    """The parse runs off the loop: a CPU-bound extraction cannot be made
    cancellable, or kept off the loop's critical path, any other way. Fails
    while ``pdf_bytes_to_text`` is called inline."""
    parse_started = threading.Event()

    def _slow_parser(_body: bytes) -> str:
        parse_started.set()
        time.sleep(0.5)
        return "Texto."

    monkeypatch.setattr(bm, "pdf_bytes_to_text", _slow_parser)
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        respx.get(FI_ADMIN_URL).mock(return_value=_pdf_response(b"%PDF-1.4 fake"))

        fetch = asyncio.create_task(engine.get_full_text("biblio-1"))
        # Wait until the parse is actually in flight, then prove the loop is
        # still free while it runs.
        while not parse_started.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert not fetch.done(), "parse blocked the event loop"
        payload, _ = await fetch
        assert payload["status"] == "success"
    finally:
        await cache.close()
        await http_client.aclose()


# D6 ---------------------------------------------------------------------------


@respx.mock
async def test_record_cache_row_serves_cold_open_without_live_lookup(tmp_path: Path):
    """Search writes per-id record rows at the standard TTL; a later open
    resolves from the row instead of re-paying a live ``id:"..."`` Solr
    query."""
    engine, cache, http_client = await _engine(tmp_path)
    _pin_fast_limiter(http_client)
    try:
        doc = _bvs_doc(
            record_id="biblio-1",
            ab=["Resumo."],
            ur=["https://www.sciencedirect.com/science/article/pii/S123"],
        )
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        records, _ = await engine.search_guidelines("dengue", limit=5)
        assert [r.record_id for r in records] == ["biblio-1"]

        row, row_meta = await cache.get(f"brazil_moh_record:{CACHE_SCHEMA}:biblio-1")
        assert row_meta.cached
        assert row["record_id"] == "biblio-1"
        assert row["document_url"].startswith("https://www.sciencedirect.com")

        calls_after_search = len(route.calls)
        payload, _ = await engine.get_full_text("biblio-1")
        # Off-site document: no PDF fetch, abstract fallback.
        assert payload["content_type"] == "abstract"
        assert len(route.calls) == calls_after_search  # no live id:"..." lookup
    finally:
        await cache.close()
        await http_client.aclose()
