from pathlib import Path
from unittest.mock import AsyncMock

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.databases import MedicalDatabasesEngine
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

COCHRANE_URL = "https://www.cochranelibrary.com/search"


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
    respx.get(COCHRANE_URL).respond(
        html="""
    <html><body>
      <div class="search-result-item">
        <h3><a href="/cd/1">DIABETES management</a></h3>
        <div class="abstract">Short.</div>
        <div class="journal">Cochrane Database</div>
      </div>
    </body></html>
    """
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
        respx.get(COCHRANE_URL).respond(html="<html><body></body></html>")
        articles, _ = await engine.search_medical_databases("diabetes")
        assert len(articles) == 20
        # The highly relevant item must survive the truncation; it would not if unique[:20] ran first.
        assert any("Diabetes breakthrough" in a.title for a in articles)
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_medical_databases_survives_source_failure(tmp_path: Path):
    engine, cache, http_client, _ = await _engine(tmp_path)
    respx.get(COCHRANE_URL).mock(side_effect=Exception("cochrane down"))

    articles, meta = await engine.search_medical_databases("diabetes")
    assert len(articles) == 2  # PubMed + ClinicalTrials still returned
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
