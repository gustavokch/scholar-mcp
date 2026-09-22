"""Resolver-style total_chars reporting with ceiling kept (Task 1)."""

import dataclasses
from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import (
    BVS_SEARCH_URL,
    MAX_FULL_TEXT_CHARS,
    BrazilMoHEngine,
)
from scholar_mcp.medical.passages import DEFAULT_SERVING_CHARS
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

FI_ADMIN_URL = "https://fi-admin.bvsalud.org/document/view/cfpaj"


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
async def test_over_ceiling_reports_total_chars_with_max_chars(tmp_path: Path, monkeypatch):
    full = "x" * 62000
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: full
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
        payload, _ = await engine.get_full_text("biblio-1", max_chars=5000)
        assert payload["total_chars"] == 62000
        assert payload["truncated"] is True
        # Served slice capped at max_chars plus the truncation marker.
        assert len(payload["content"]) <= 5000 + 100
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_default_serves_serving_budget_not_ceiling(
    tmp_path: Path, monkeypatch
):
    full = "x" * 620000
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: full
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
        payload, _ = await engine.get_full_text("biblio-1", max_chars=None)
        assert payload["total_chars"] == 620000
        # The default serves the serving budget, not the storage ceiling:
        # a plain get_full_text call must not return 600k chars.
        assert payload["truncated"] is True
        assert len(payload["content"]) <= DEFAULT_SERVING_CHARS + 100
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_under_ceiling_reports_full_length_untruncated(
    tmp_path: Path, monkeypatch
):
    full = "y" * 18000
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: full
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
        payload, _ = await engine.get_full_text("biblio-1", max_chars=None)
        assert payload["total_chars"] == 18000
        assert payload["truncated"] is False
        assert payload["content"] == full
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_abstract_path_total_chars_matches_abstract(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-1", ab=["Resumo apenas."])
        doc["ur"] = ["https://www.sciencedirect.com/science/article/pii/S123"]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        payload, _ = await engine.get_full_text("biblio-1")
        assert payload["content_type"] == "abstract"
        assert payload["total_chars"] == len("Resumo apenas.")
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_cache_stores_capped_content_with_full_total_chars(
    tmp_path: Path, monkeypatch
):
    full = "z" * 620000
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: full
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
        cached, _ = await cache.get("brazil_moh_fulltext:biblio-1")
        assert len(cached["content"]) <= MAX_FULL_TEXT_CHARS
        assert cached["total_chars"] == 620000
    finally:
        await cache.close()
        await http_client.aclose()


def _local_record(document_url: str):
    from scholar_mcp.medical.models import BrazilGuideline

    return BrazilGuideline(
        title="Local", record_id="local-test", document_url=document_url
    )


def _local_base(document_url: str) -> dict:
    return {
        "source": "brazil-moh",
        "record_id": "local-test",
        "document_url": document_url,
        "truncated": False,
    }


async def test_local_text_invalid_path_reports_not_found(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, meta = await engine._serve_local_text(
            "brazil_moh_fulltext:local-test",
            _local_base("local:../../etc/passwd"),
            _local_record("local:../../etc/passwd"),
            None,
        )
        assert payload["status"] == "not_found"
        assert payload["error"] == "invalid local corpus path"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


async def test_local_text_missing_file_reports_missing(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, meta = await engine._serve_local_text(
            "brazil_moh_fulltext:local-test",
            _local_base("local:guidelines/nope.txt"),
            _local_record("local:guidelines/nope.txt"),
            None,
        )
        assert payload["status"] == "not_found"
        assert payload["error"] == "local corpus file missing"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()
