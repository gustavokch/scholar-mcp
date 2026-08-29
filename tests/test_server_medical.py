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
    "search_medical_databases",
    "search_medical_journals",
    "get_medical_cache_stats",
}


def test_all_medical_tools_registered():
    for name in MEDICAL_TOOLS:
        assert callable(getattr(srv, name)), f"{name} is not exposed by scholar_mcp.server"


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
