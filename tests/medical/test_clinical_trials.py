from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

CT_URL = "https://clinicaltrials.gov/api/v2/studies"


@respx.mock
async def test_search_clinical_trials(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(CT_URL).respond(
        json={
            "studies": [
                {
                    "protocolSection": {
                        "identificationModule": {
                            "nctId": "NCT01234567",
                            "briefTitle": "Evaluation of Drug X in Asthma",
                            "leadSponsor": {"name": "National Institute of Health"},
                        },
                        "descriptionModule": {"briefSummary": "This study evaluates safety and efficacy."},
                        "statusModule": {"startDateStruct": {"date": "2021-01"}},
                    }
                }
            ]
        }
    )

    articles, meta = await client.search_clinical_trials("asthma", limit=5)
    assert len(articles) == 1
    assert articles[0].title == "Evaluation of Drug X in Asthma"
    assert articles[0].authors == ["National Institute of Health"]
    assert articles[0].journal == "ClinicalTrials.gov"
    assert articles[0].year == "2021-01"
    assert articles[0].url == "https://clinicaltrials.gov/study/NCT01234567"
    assert articles[0].source_database == "ClinicalTrials.gov"
    assert articles[0].abstract == "This study evaluates safety and efficacy."

    # Cache check
    articles2, meta2 = await client.search_clinical_trials("asthma", limit=5)
    assert meta2.cached is True
    assert articles2[0].to_dict() == articles[0].to_dict()

    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_clinical_trials_handles_missing_fields(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(CT_URL).respond(
        json={"studies": [{"protocolSection": {"identificationModule": {"nctId": "NCT0000001"}}}]}
    )

    articles, meta = await client.search_clinical_trials("asthma")
    assert len(articles) == 1
    assert articles[0].title == "Clinical Trial"  # fallback title
    assert articles[0].authors == []
    await cache.close()
    await http_client.aclose()
