from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.fda import FDAClient, is_valid_drug_query
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

FDA_URL = "https://api.fda.gov/drug/label.json"


def _label_payload(brand, generic=None, ndc="50580-488", dosage=None):
    return {
        "results": [
            {
                "openfda": {
                    "brand_name": [brand],
                    "generic_name": [generic] if generic else [],
                    "manufacturer_name": ["Johnson & Johnson"],
                    "product_ndc": [ndc],
                },
                "effective_time": "20230101",
                "purpose": ["Pain reliever/fever reducer"],
                "dosage_and_administration": [dosage] if dosage else [],
            }
        ]
    }


async def _make_client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = FDAClient(http_client=http_client, cache=cache, settings=settings)
    return client, cache, http_client


def test_is_valid_drug_query():
    assert is_valid_drug_query("medication") is False
    assert is_valid_drug_query("pill") is False
    assert is_valid_drug_query("ab") is False
    assert is_valid_drug_query("aspirin") is True


@respx.mock
async def test_search_drugs_success(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(json=_label_payload("Tylenol", "Acetaminophen"))

    drugs, meta = await client.search_drugs("tylenol", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name == ["Tylenol"]
    assert drugs[0].openfda.generic_name == ["Acetaminophen"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drugs_invalid_query_returns_empty(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    drugs, meta = await client.search_drugs("medication")
    assert drugs == []
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_get_drug_by_ndc(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(json=_label_payload("Advil", ndc="0573-0164"))

    drug, meta = await client.get_drug_by_ndc("0573-0164")
    assert drug is not None
    assert drug.openfda.brand_name == ["Advil"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_drugs(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload(
            "Children's Motrin",
            generic="Ibuprofen",
            ndc="50580-601",
            dosage="Pediatric dosing: 10mg/kg every 6-8 hours for children",
        )
    )

    drugs, meta = await client.search_pediatric_drugs("motrin", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name == ["Children's Motrin"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_drugs_filters_adult_labels(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload("Adult Formula", dosage="Take one tablet with water")
    )

    drugs, meta = await client.search_pediatric_drugs("adult formula", limit=5)
    assert drugs == []
    await cache.close()
    await http_client.aclose()
