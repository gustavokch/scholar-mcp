from pathlib import Path

import httpx
import respx

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical.clinical_trials import (
    CT_MAX_QUERY_TERMS,
    CT_URL,
    ClinicalTrialsClient,
    _cap_query_terms,
)
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager


@respx.mock
async def test_search_clinical_trials(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    try:
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
        assert articles[0].nct_id == "NCT01234567"
        assert articles[0].source_database == "ClinicalTrials.gov"
        assert articles[0].abstract == "This study evaluates safety and efficacy."

        # Cache check
        articles2, meta2 = await client.search_clinical_trials("asthma", limit=5)
        assert meta2.cached is True
        assert articles2[0].to_dict() == articles[0].to_dict()
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_clinical_trials_handles_missing_fields(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    try:
        respx.get(CT_URL).respond(
            json={"studies": [{"protocolSection": {"identificationModule": {"nctId": "NCT0000001"}}}]}
        )

        articles, meta = await client.search_clinical_trials("asthma")
        assert len(articles) == 1
        assert articles[0].title == "Clinical Trial"  # fallback title
        assert articles[0].authors == []
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_clinical_trials_marks_error_on_fetch_failure(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    try:
        route = respx.get(CT_URL).mock(side_effect=httpx.ConnectError("connection refused"))

        articles, meta = await client.search_clinical_trials("asthma")
        assert articles == []
        assert meta.cached is False
        assert meta.error is True

        # The failure must not be cached: a second call re-issues the request.
        # Exact counts are retry-dependent, so only require fresh traffic.
        after_first = route.call_count
        await client.search_clinical_trials("asthma")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_clinical_trials_caps_query_terms(tmp_path: Path):
    # The ClinicalTrials.gov Essie parser rejects over-complex free-text
    # queries with HTTP 400 ("Too complicated query"); ~13 plain terms is
    # enough to trip it. The client must cap the outbound query instead of
    # letting every long agent-composed query fail.
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    try:
        respx.get(CT_URL).respond(json={"studies": []})

        # The query from the field failure: 13 terms, parser rejects it verbatim.
        articles, meta = await client.search_clinical_trials(
            "ibuprofen pregnancy third trimester FDA pregnancy category D"
            " premature ductus arteriosus closure oligohydramnios",
            limit=10,
        )

        sent = respx.calls.last.request.url.params["query.term"]
        assert sent == "ibuprofen pregnancy third trimester FDA pregnancy category D premature ductus"
        assert meta.error is False
        assert articles == []
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_clinical_trials_shares_cache_for_overlong_queries(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    try:
        route = respx.get(CT_URL).respond(
            json={
                "studies": [
                    {
                        "protocolSection": {
                            "identificationModule": {
                                "nctId": "NCT01234567",
                                "briefTitle": "Evaluation of Drug X",
                            }
                        }
                    }
                ]
            }
        )

        query_13_terms = "one two three four five six seven eight nine ten eleven twelve thirteen"
        query_14_terms = "one two three four five six seven eight nine ten foo bar baz qux"

        articles1, meta1 = await client.search_clinical_trials(query_13_terms, limit=10)
        assert meta1.cached is False
        assert len(articles1) == 1
        assert route.call_count == 1

        # Second call with different trailing terms maps to identical capped query and must hit cache
        articles2, meta2 = await client.search_clinical_trials(query_14_terms, limit=10)
        assert meta2.cached is True
        assert len(articles2) == 1
        assert route.call_count == 1
        assert articles2[0].to_dict() == articles1[0].to_dict()
    finally:
        await cache.close()
        await http_client.aclose()


@pytest.mark.parametrize(
    ("input_query", "max_terms", "expected"),
    [
        ("", 10, ""),
        ("   ", 10, ""),
        ("asthma", 10, "asthma"),
        ("one two three four five six seven eight nine ten", 10, "one two three four five six seven eight nine ten"),
        ("one two three four five six seven eight nine ten eleven", 10, "one two three four five six seven eight nine ten"),
        ('treatment "for hypertension and heart failure" in elderly patients', 4, 'treatment "for hypertension and"'),
        ('"asthma', 10, '"asthma"'),
        ('asthma "treatment', 10, 'asthma "treatment"'),
        ('"asthma" and "copd"', 10, '"asthma" and "copd"'),
        ("one two three", 0, ""),
        ("one two three", -1, ""),
    ],
)
def test_cap_query_terms_matrix(input_query: str, max_terms: int, expected: str):
    assert _cap_query_terms(input_query, max_terms=max_terms) == expected

