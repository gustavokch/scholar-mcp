"""Passage-targeted full-text retrieval (ENAMED misses plan B3).

A 100k synthetic body carries a distinctive target sentence at offset
60k: a topic query must surface that window with its offset, and an
explicit offset must page the body exactly.
"""

import dataclasses
from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import (
    BVS_SEARCH_URL,
    CACHE_SCHEMA,
    MAX_FULL_TEXT_CHARS,
    BrazilMoHEngine,
)
from scholar_mcp.medical.models import BrazilGuideline
from scholar_mcp.medical.passages import serve_body, split_windows
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

FI_ADMIN_URL = "https://fi-admin.bvsalud.org/document/view/cfpaj"

TARGET = "Tempo necessário para início de proteção com uso diário consecutivo"


def _synthetic_body(target_offset: int = 60000, total: int = 100000) -> str:
    filler = "palavra de preenchimento "
    prefix_reps = target_offset // len(filler)
    prefix = filler * prefix_reps
    assert len(prefix) == target_offset
    suffix_reps = (total - target_offset - len(TARGET) - 2) // len("complemento final ")
    body = prefix + TARGET + "\n\n" + "complemento final " * suffix_reps
    assert len(body) >= total - 100
    return body


async def _engine(tmp_path: Path):
    settings = dataclasses.replace(Settings.load(), brazil_browser_fallback=False)
    http_client = AsyncHttpClient(settings)
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
        "ur": ["https://fi-admin.bvsalud.org/document/view/cfpaj"],
    }
    doc.update(extra)
    return doc


def _bvs_response(docs):
    return {
        "diaServerResponse": [
            {
                "responseHeader": {"status": 0},
                "response": {"numFound": len(docs), "docs": docs},
            }
        ]
    }


@respx.mock
async def test_query_returns_passage_with_offset(tmp_path: Path, monkeypatch):
    body = _synthetic_body()
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: body
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        payload, meta = await engine.get_full_text(
            "biblio-1", query="tempo necessário início proteção"
        )
        assert payload["status"] == "success"
        assert payload["total_chars"] == len(body)
        assert TARGET in payload["content"]
        assert payload["passages"], "the 60k window must be served"
        offsets = [p["offset"] for p in payload["passages"]]
        assert any(abs(o - 60000) < 1500 for o in offsets)
        assert all(p["score"] >= 1 for p in payload["passages"])
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_offset_pages_body_exactly(tmp_path: Path, monkeypatch):
    body = _synthetic_body()
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: body
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        payload, _ = await engine.get_full_text(
            "biblio-1", max_chars=5000, offset=60000
        )
        assert payload["content"] == body[60000:65000]
        assert TARGET in payload["content"]
        assert payload["truncated"] is True
        assert payload["passages"] == []
        assert payload["total_chars"] == len(body)
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_cache_holds_full_body(tmp_path: Path, monkeypatch):
    body = _synthetic_body()
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: body
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        await engine.get_full_text("biblio-1")
        cached, _ = await cache.get(f"brazil_moh_fulltext:{CACHE_SCHEMA}:biblio-1")
        assert cached["content"] == body
        assert cached["total_chars"] == len(body)
        # A cached full body serves passages without refetching.
        respx.clear()
        payload, meta = await engine.get_full_text(
            "biblio-1", query="tempo necessário início proteção"
        )
        assert TARGET in payload["content"]
        assert meta.cached is True
    finally:
        await cache.close()
        await http_client.aclose()


async def test_local_path_supports_query_and_offset(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        record = BrazilGuideline(
            title="Manual",
            record_id="local-test",
            document_url="local:guidelines/manual_tuberculose_2019.txt",
        )
        base = {
            "source": "brazil-moh",
            "record_id": "local-test",
            "document_url": record.document_url,
            "truncated": False,
        }
        payload, meta = await engine._serve_local_text(
            "brazil_moh_fulltext:local-test",
            base,
            record,
            50000,
            query="rifampicina esquema basico tratamento",
            offset=0,
        )
        assert payload["status"] == "success"
        # The manual is ~745k chars: storage caps at the ceiling while
        # total_chars reports the true length.
        assert payload["total_chars"] > MAX_FULL_TEXT_CHARS
        assert len(payload["content"]) <= 50000 + 5000
        assert payload["passages"], "a real manual must yield scored windows"
        assert meta.error is False

        payload_page, _ = await engine._serve_local_text(
            "brazil_moh_fulltext:local-test",
            base,
            record,
            5000,
            query=None,
            offset=500000,
        )
        assert payload_page["truncated"] is True
        assert payload_page["passages"] == []
        assert len(payload_page["content"]) == 5000
    finally:
        await cache.close()
        await http_client.aclose()


def test_split_windows_cover_body_contiguously():
    body = ("paragrafo sobre tratamento " * 200 + "\n\n") * 30
    windows = split_windows(body)
    assert windows[0][0] == 0
    for (off_a, text_a), (off_b, _) in zip(windows, windows[1:]):
        assert off_a + len(text_a) == off_b
    assert windows[-1][0] + len(windows[-1][1]) == len(body)
    assert all(len(t) <= 1500 for _, t in windows)


def test_serve_body_query_wins_over_offset():
    body = "head " * 1000 + "unico termoalvo " + "tail " * 3000
    served = serve_body(body, len(body), 50000, query="termoalvo", offset=99999)
    assert "termoalvo" in served["content"]
    assert served["passages"]


def test_serve_body_offset_past_end_serves_empty_page():
    served = serve_body("abc", 3, 100, offset=999)
    assert served["content"] == ""
    assert served["truncated"] is False
