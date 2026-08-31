from pathlib import Path
from unittest.mock import AsyncMock

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.databases import MedicalDatabasesEngine
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

EUROPE_PMC_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


async def _engine(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        [
            MedicalArticle(
                title="Diabetes Management",
                authors=["Smith J"],
                year="2021",
                doi="10.1000/1",
                journal="NEJM",
                abstract="Long detailed abstract.",
            )
        ],
        CacheMetadata(cached=False, cache_age=0),
    )
    mock_ct = AsyncMock()
    mock_ct.search_clinical_trials.return_value = (
        [
            MedicalArticle(
                title="Diabetes Clinical Trial",
                journal="ClinicalTrials.gov",
                url="https://clinicaltrials.gov/study/NCT123",
            )
        ],
        CacheMetadata(cached=False, cache_age=0),
    )

    engine = MedicalDatabasesEngine(
        pubmed=mock_pubmed,
        clinical_trials=mock_ct,
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )
    return engine, cache, http_client, mock_pubmed


@respx.mock
async def test_search_medical_databases_combines_and_deduplicates(tmp_path: Path):
    engine, cache, http_client, _ = await _engine(tmp_path)
    # Cochrane returns the same paper under a case-variant title + same year: dedup keeps the PubMed record (has DOI).
    respx.get(EUROPE_PMC_URL).respond(
        json={
            "hitCount": 1,
            "resultList": {
                "result": [
                    {
                        "title": "DIABETES management",
                        "authorString": "Cochrane Collaboration",
                        "journalTitle": "Cochrane Database Syst Rev",
                        "pubYear": "2021",
                        "pmid": "99999",
                        "doi": "10.1000/1",
                        "pubType": "systematic review",
                    }
                ]
            },
        }
    )

    articles, meta = await engine.search_medical_databases("diabetes")
    titles = [a.title for a in articles]
    assert "Diabetes Management" in titles
    assert "Diabetes Clinical Trial" in titles
    assert len(articles) == 2  # Cochrane duplicate removed
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_medical_databases_ranks_by_relevance(tmp_path: Path):
    engine, cache, http_client, mock_pubmed = await _engine(tmp_path)
    try:
        # PubMed article does not mention the query; trial title does. Relevance must win over source order.
        mock_pubmed.search_articles.return_value = (
            [MedicalArticle(title="General practice survey", year="2020")],
            CacheMetadata(cached=False, cache_age=0),
        )

        articles, meta = await engine.search_medical_databases("diabetes")
        assert articles[0].title == "Diabetes Clinical Trial"
        assert articles[0].score is not None
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_medical_databases_ranks_before_truncation(tmp_path: Path):
    """When the merged pool exceeds 20, ranking must happen before slicing so high-relevance items from
    secondary sources are not dropped just because they appear after the cap."""
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    # Distinct titles + distinct years + distinct DOIs so dedup keeps each one.
    filler = [
        MedicalArticle(
            title=f"Cardiology review {i} on arrhythmia management",
            year=str(2000 + i),
            doi=f"10.1000/cardio{i}",
        )
        for i in range(25)
    ]
    filler.append(
        MedicalArticle(
            title="Diabetes breakthrough: highly relevant study",
            year="2024",
            doi="10.1000/diab1",
        )
    )
    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        filler,
        CacheMetadata(cached=False, cache_age=0),
    )
    mock_ct = AsyncMock()
    mock_ct.search_clinical_trials.return_value = (
        [],
        CacheMetadata(cached=False, cache_age=0),
    )

    engine = MedicalDatabasesEngine(
        pubmed=mock_pubmed,
        clinical_trials=mock_ct,
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )
    try:
        respx.get(EUROPE_PMC_URL).respond(
            json={"hitCount": 0, "resultList": {"result": []}}
        )
        articles, _ = await engine.search_medical_databases("diabetes")
        assert len(articles) == 20
        # The highly relevant item must survive the truncation; it would not if unique[:20] ran first.
        assert any("Diabetes breakthrough" in a.title for a in articles)
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_cochrane_routes_through_europe_pmc(tmp_path: Path):
    """The Cochrane Library HTML site is behind a Cloudflare bot wall that
    blocks the plain HTTP fetch. Rather than fight the wall, _search_cochrane
    routes through Europe PMC's open API (which mirrors Cochrane systematic
    reviews) and reports those results as Cochrane records.
    """
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    engine = MedicalDatabasesEngine(
        pubmed=AsyncMock(),
        clinical_trials=AsyncMock(),
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )

    europe_pmc_payload = {
        "hitCount": 2,
        "resultList": {
            "result": [
                {
                    "title": "Ibuprofen for acute postoperative pain in children",
                    "authorString": "Smith J, Doe A",
                    "journalTitle": "Cochrane Database Syst Rev",
                    "pubYear": "2024",
                    "pmid": "12345",
                    "pmcid": "PMC12345",
                    "doi": "10.1000/cochrane.12345",
                    "pubType": "systematic review",
                    "abstractText": "Systematic review of pediatric NSAID data.",
                },
                {
                    "title": "NSAIDs for pediatric fever: a systematic review",
                    "authorString": "Lee K",
                    "journalTitle": "Cochrane Database Syst Rev",
                    "pubYear": "2023",
                    "pmid": "67890",
                    "pubType": "systematic review",
                },
            ]
        },
    }

    respx.get(EUROPE_PMC_URL).respond(json=europe_pmc_payload)

    articles, meta = await engine._search_cochrane("ibuprofen children")
    assert len(articles) == 2
    assert articles[0].title == "Ibuprofen for acute postoperative pain in children"
    assert articles[0].source_database == "Cochrane"
    assert articles[0].journal == "Cochrane Database Syst Rev"
    assert articles[0].year == "2024"
    assert "Smith J" in articles[0].authors
    assert articles[1].year == "2023"
    assert meta.error is False
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_cochrane_marks_error_when_europe_pmc_also_fails(tmp_path: Path):
    """Both the local cache check and the Europe PMC fetch fail -> meta.error=True
    so the parent engine propagates the failure rather than caching an empty
    absence that hides a real outage.
    """
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    engine = MedicalDatabasesEngine(
        pubmed=AsyncMock(),
        clinical_trials=AsyncMock(),
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )

    respx.get(EUROPE_PMC_URL).respond(status_code=503)

    articles, meta = await engine._search_cochrane("ibuprofen children")
    assert articles == []
    assert meta.error is True
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_cochrane_filters_by_publication_type_when_results_present(
    tmp_path: Path,
):
    """When Europe PMC returns more than the soft cap, the engine must request
    the systematic-review publication type filter so the parent engine does
    not drown the gather in non-Cochrane material.
    """
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    engine = MedicalDatabasesEngine(
        pubmed=AsyncMock(),
        clinical_trials=AsyncMock(),
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )

    respx.get(EUROPE_PMC_URL).respond(
        json={"hitCount": 0, "resultList": {"result": []}}
    )

    await engine._search_cochrane("ibuprofen children")
    request = respx.get(EUROPE_PMC_URL).calls.last.request
    query = str(request.url.params.get("query", ""))
    assert "ibuprofen children" in query
    assert "systematic" in query.lower() or "review" in query.lower()
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_cochrane_neutralizes_query_punctuation(tmp_path: Path):
    """Europe PMC treats quotes and parens as query syntax; an agent-supplied
    natural-language query containing them must be neutralized before it is
    spliced into the PUB_TYPE-filtered query, or the whole search 400s."""
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    engine = MedicalDatabasesEngine(
        pubmed=AsyncMock(),
        clinical_trials=AsyncMock(),
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )

    respx.get(EUROPE_PMC_URL).respond(
        json={"hitCount": 0, "resultList": {"result": []}}
    )

    try:
        articles, meta = await engine._search_cochrane(
            'what is "ibuprofen" (for children)'
        )
        assert articles == []
        assert meta.error is False

        request = respx.get(EUROPE_PMC_URL).calls.last.request
        query = str(request.url.params.get("query", ""))
        # The only remaining quotes/parens are the engine's own PUB_TYPE filter.
        assert query == (
            '(what is ibuprofen for children) AND '
            '(PUB_TYPE:"systematic review" OR PUB_TYPE:"meta-analysis")'
        )
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_cochrane_leaves_pmid_empty_for_non_med_record(tmp_path: Path):
    """Europe PMC's `id` is a source-local record id, not a PMID. Only fall
    back to it when the record's source is MED, or the article gets a bogus
    pmid and a bogus europepmc.org/article/MED/{id} link."""
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    engine = MedicalDatabasesEngine(
        pubmed=AsyncMock(),
        clinical_trials=AsyncMock(),
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )

    respx.get(EUROPE_PMC_URL).respond(
        json={
            "hitCount": 1,
            "resultList": {
                "result": [
                    {
                        "id": "777",
                        "source": "CBA",
                        "title": "Systematic review of pediatric NSAID data",
                        "pubYear": "2024",
                        "pubType": "systematic review",
                    }
                ]
            },
        }
    )

    try:
        articles, meta = await engine._search_cochrane("ibuprofen children")
        assert len(articles) == 1
        assert articles[0].pmid == ""
        assert articles[0].url == ""
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_medical_databases_survives_source_failure(tmp_path: Path):
    engine, cache, http_client, _ = await _engine(tmp_path)
    respx.get(EUROPE_PMC_URL).mock(side_effect=Exception("europe pmc down"))

    articles, meta = await engine.search_medical_databases("diabetes")
    assert len(articles) == 2  # PubMed + ClinicalTrials still returned
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_medical_databases_marks_error_when_all_sources_fail(tmp_path: Path):
    """Empty + errored must reach the caller and must not be cached."""
    engine, cache, http_client, mock_pubmed = await _engine(tmp_path)
    mock_pubmed.search_articles.return_value = ([], CacheMetadata(cached=False, cache_age=0, error=True))
    mock_ct = AsyncMock()
    mock_ct.search_clinical_trials.return_value = ([], CacheMetadata(cached=False, cache_age=0, error=True))
    engine.clinical_trials = mock_ct
    respx.get(EUROPE_PMC_URL).mock(side_effect=Exception("europe pmc down"))

    articles, meta = await engine.search_medical_databases("diabetes")
    assert articles == []
    assert meta.error is True

    # The failure must not be cached: a second call re-issues the request.
    # Exact counts are retry-dependent, so only require fresh traffic.
    after_first = respx.get(EUROPE_PMC_URL).call_count
    await engine.search_medical_databases("diabetes")
    assert respx.get(EUROPE_PMC_URL).call_count > after_first
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_medical_databases_marks_partial_source_failure(tmp_path: Path):
    """Cochrane down while PubMed/ClinicalTrials succeed: results still return,
    but the caller must learn the set is incomplete."""
    engine, cache, http_client, _ = await _engine(tmp_path)
    respx.get(EUROPE_PMC_URL).mock(side_effect=Exception("europe pmc down"))

    articles, meta = await engine.search_medical_databases("diabetes")
    assert len(articles) == 2
    assert meta.error is True
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_medical_journals_propagates_pubmed_error(tmp_path: Path):
    engine, cache, http_client, mock_pubmed = await _engine(tmp_path)
    mock_pubmed.search_articles.return_value = ([], CacheMetadata(cached=False, cache_age=0, error=True))

    articles, meta = await engine.search_medical_journals("diabetes")
    assert articles == []
    assert meta.error is True

    after_first = mock_pubmed.search_articles.await_count
    await engine.search_medical_journals("diabetes")
    assert mock_pubmed.search_articles.await_count > after_first
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_medical_journals_composes_query(tmp_path: Path):
    engine, cache, http_client, mock_pubmed = await _engine(tmp_path)
    mock_pubmed.search_articles.return_value = (
        [MedicalArticle(title="NEJM diabetes study", journal="NEJM")],
        CacheMetadata(cached=False, cache_age=0),
    )

    articles, meta = await engine.search_medical_journals("diabetes")
    term = mock_pubmed.search_articles.await_args.args[0]
    assert "New England Journal of Medicine" in term
    assert "Nature Medicine" in term
    assert "diabetes" in term
    assert len(articles) == 1
    await cache.close()
    await http_client.aclose()


async def test_search_medical_journals_ranks_on_user_query_before_truncation(tmp_path: Path):
    """The journal search sends `(query) AND ("NEJM"[Journal] OR ...)` to PubMed.

    Ranking must use the raw user query -- not that composed term, whose journal
    names would score as query terms -- and must run before the [:15] slice.
    """
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    # Filler titles echo the journal-name tokens in the composed PubMed term.
    filler = [
        MedicalArticle(
            title=f"Nature Medicine journal commentary {i}",
            year=str(2000 + i),
            doi=f"10.1000/filler{i}",
        )
        for i in range(20)
    ]
    on_topic = MedicalArticle(
        title="Metformin diabetes outcomes",
        year="2015",
        doi="10.1000/ontopic",
    )
    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        [*filler, on_topic],
        CacheMetadata(cached=False, cache_age=0),
    )

    engine = MedicalDatabasesEngine(
        pubmed=mock_pubmed,
        clinical_trials=AsyncMock(),
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )
    try:
        articles, _ = await engine.search_medical_journals("metformin diabetes")
        assert len(articles) == 15
        assert articles[0].title == "Metformin diabetes outcomes"
    finally:
        await cache.close()
        await http_client.aclose()
