from typing import Any, get_type_hints
from unittest.mock import AsyncMock

from scholar_mcp import server as srv
from scholar_mcp.medical.models import DrugLabel, OpenFDAData
from scholar_mcp.utils.sqlite_cache import CacheMetadata

MEDICAL_TOOLS = {
    "search_drugs",
    "get_drug_details",
    "search_pediatric_drugs",
    "search_drug_nomenclature",
    "get_health_statistics",
    "get_child_health_statistics",
    "search_clinical_guidelines",
    "search_pediatric_guidelines",
    "search_aap_guidelines",
    "search_pediatric_literature",
    "search_who_iris_guidelines",
    "get_who_iris_full_text",
    "search_brazil_moh_guidelines",
    "get_brazil_moh_full_text",
    "search_medical_databases",
    "search_medical_journals",
    "get_medical_cache_stats",
}


def test_all_medical_tools_registered():
    for name in MEDICAL_TOOLS:
        assert callable(getattr(srv, name)), f"{name} is not exposed by scholar_mcp.server"


def test_medical_tools_return_dict_type_annotation():
    for name in MEDICAL_TOOLS:
        fn = getattr(srv, name)
        hints = get_type_hints(fn)
        assert hints["return"] == dict[str, Any], f"{name} return type is not dict[str, Any]"


async def test_search_drugs_tool(monkeypatch):
    mock = AsyncMock()
    mock.search_drugs.return_value = (
        [DrugLabel(openfda=OpenFDAData(brand_name=["Advil"], generic_name=["Ibuprofen"]))],
        CacheMetadata(cached=False, cache_age=0),
    )
    monkeypatch.setattr(srv, "fda_client", mock)
    result = await srv.search_drugs("advil")
    assert result["data"][0]["openfda"]["brand_name"] == ["Advil"]
    assert "[Fresh response]" in result["markdown"]


async def test_get_drug_details_tool_handles_none(monkeypatch):
    mock = AsyncMock()
    mock.get_drug_by_ndc.return_value = (None, CacheMetadata(cached=False, cache_age=0))
    monkeypatch.setattr(srv, "fda_client", mock)
    result = await srv.get_drug_details("00-00-00")
    assert result["status"] == "not_found"


async def test_get_medical_cache_stats_tool(monkeypatch):
    mock = AsyncMock()
    mock.get_stats.return_value = {"total_entries": 0, "hits": 0, "misses": 0}
    monkeypatch.setattr(srv, "medical_cache", mock)
    result = await srv.get_medical_cache_stats()
    assert result["total_entries"] == 0


async def test_search_brazil_moh_guidelines_tool(monkeypatch):
    from scholar_mcp.medical.models import BrazilGuideline

    mock = AsyncMock(
        return_value=(
            [BrazilGuideline(title="Protocolo", record_id="biblio-1")],
            CacheMetadata(cached=False, cache_age=0),
        )
    )
    monkeypatch.setattr(srv.brazil_moh_engine, "search_guidelines", mock)
    result = await srv.search_brazil_moh_guidelines("tuberculose", limit=5)
    assert result["data"][0]["record_id"] == "biblio-1"
    assert mock.await_args.kwargs["limit"] == 5


async def test_search_brazil_moh_guidelines_clamps_limit(monkeypatch):
    mock = AsyncMock(return_value=([], CacheMetadata(cached=False, cache_age=0)))
    monkeypatch.setattr(srv.brazil_moh_engine, "search_guidelines", mock)
    await srv.search_brazil_moh_guidelines("x", limit=9999)
    assert mock.await_args.kwargs["limit"] == 50


async def test_search_brazil_moh_guidelines_rejects_unknown_collection(monkeypatch):
    mock = AsyncMock(return_value=([], CacheMetadata(cached=False, cache_age=0)))
    monkeypatch.setattr(srv.brazil_moh_engine, "search_guidelines", mock)
    result = await srv.search_brazil_moh_guidelines("x", collection="everything")
    assert result["status"] == "error"
    assert result["source"] == "brazil-moh"
    assert mock.await_count == 0


async def test_search_brazil_moh_guidelines_returns_error_envelope(monkeypatch):
    mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(srv.brazil_moh_engine, "search_guidelines", mock)
    result = await srv.search_brazil_moh_guidelines("x")
    assert result["status"] == "error"
    assert result["source"] == "brazil-moh"


async def test_get_brazil_moh_full_text_tool(monkeypatch):
    mock = AsyncMock(
        return_value=(
            {"status": "success", "content": "texto", "content_type": "pdf"},
            CacheMetadata(cached=True, cache_age=42),
        )
    )
    monkeypatch.setattr(srv.brazil_moh_engine, "get_full_text", mock)
    result = await srv.get_brazil_moh_full_text("biblio-1")
    assert result["content"] == "texto"
    assert result["cache"] == {"cached": True, "cache_age": 42}


async def test_get_brazil_moh_full_text_returns_error_envelope(monkeypatch):
    mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(srv.brazil_moh_engine, "get_full_text", mock)
    result = await srv.get_brazil_moh_full_text("biblio-1")
    assert result["status"] == "error"
    assert result["content"] == ""

