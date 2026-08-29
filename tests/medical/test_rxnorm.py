from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.rxnorm import RxNormClient
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

RXNORM_URL = "https://rxnav.nlm.nih.gov/REST/drugs.json"


@respx.mock
async def test_search_drug_nomenclature(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = RxNormClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(RXNORM_URL).respond(
        json={
            "drugGroup": {
                "conceptGroup": [
                    {
                        "conceptProperties": [
                            {
                                "rxcui": "161",
                                "name": "Acetaminophen",
                                "synonym": "APAP",  # string, not list
                                "tty": "IN",
                                "language": "ENG",
                                "suppress": "N",
                                "umlscui": "C0000970",  # string, not list
                            }
                        ]
                    },
                    {"conceptProperties": []},  # empty group is skipped
                    {"noProperties": True},  # group without properties is skipped
                ]
            }
        }
    )

    drugs, meta = await client.search_drug_nomenclature("acetaminophen")
    assert len(drugs) == 1
    assert drugs[0].rxcui == "161"
    assert drugs[0].name == "Acetaminophen"
    assert drugs[0].tty == "IN"
    assert drugs[0].synonyms == ["APAP"]
    assert drugs[0].umlscui == ["C0000970"]

    # Second call hits cache
    drugs2, meta2 = await client.search_drug_nomenclature("acetaminophen")
    assert meta2.cached is True
    assert drugs2[0].to_dict() == drugs[0].to_dict()

    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drug_nomenclature_empty(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = RxNormClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(RXNORM_URL).respond(json={"drugGroup": {}})

    drugs, meta = await client.search_drug_nomenclature("unknown")
    assert drugs == []
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drug_nomenclature_filters_empty_concepts(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = RxNormClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(RXNORM_URL).respond(
        json={
            "drugGroup": {
                "conceptGroup": [
                    {
                        "conceptProperties": [
                            {"rxcui": "161", "name": "Acetaminophen"},
                            {"rxcui": "", "name": "Missing RxCUI"},
                            {"rxcui": "999", "name": ""},
                        ]
                    }
                ]
            }
        }
    )

    drugs, meta = await client.search_drug_nomenclature("acetaminophen")
    assert len(drugs) == 1
    assert drugs[0].rxcui == "161"
    await cache.close()
    await http_client.aclose()
