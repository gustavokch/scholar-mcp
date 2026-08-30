from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.guidelines import (
    GuidelinesEngine,
    calculate_guideline_score,
    extract_organization,
)
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def test_calculate_guideline_score():
    article = MedicalArticle(
        title="American Heart Association Clinical Practice Guideline for Hypertension",
        authors=["Whelton PK"],
        journal="Journal of the American College of Cardiology",
        abstract="Evidence-based recommendation and consensus for blood pressure management.",
        pmid="12345",
    )
    score = calculate_guideline_score(article, has_publication_type=True)
    assert score.publication_type == 2.0
    assert score.title_keywords == 1.0  # "guideline" in title
    assert score.journal_reputation == 1.0  # "journal of the american"
    assert score.author_affiliation == 1.0  # org extracted
    assert score.abstract_keywords == 1.0  # 3 keywords hit, capped at 2 * 0.5
    assert score.mesh_terms == 0.0  # reserved weight
    assert score.total == 6.0


def test_calculate_guideline_score_rejects_low_scores():
    article = MedicalArticle(
        title="A random case report",
        journal="Some Journal",
        abstract="An interesting case.",
    )
    score = calculate_guideline_score(article, has_publication_type=False)
    assert score.total < 2.5


def test_extract_organization():
    article = MedicalArticle(
        title="Management of Asthma",
        journal="Pediatrics",
        abstract="Official statement from the American Academy of Pediatrics on pediatric asthma care.",
    )
    assert "American Academy of Pediatrics" in extract_organization(article)


@respx.mock
async def test_search_clinical_guidelines_layers(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    pubmed = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    engine = GuidelinesEngine(pubmed=pubmed, cache=cache, settings=settings)

    respx.get(ESEARCH_URL).respond(json={"esearchresult": {"idlist": ["999"]}})
    respx.get(EFETCH_URL).respond(
        content=(
            "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>999</PMID>"
            "<Article><Journal><Title>Lancet</Title></Journal>"
            "<ArticleTitle>Clinical practice guideline for asthma</ArticleTitle>"
            "<Abstract><AbstractText>Guideline recommendations.</AbstractText></Abstract>"
            "<ELocationID EIdType='doi'>10.1/g</ELocationID>"
            "</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
        ).encode()
    )

    guidelines, meta = await engine.search_clinical_guidelines("asthma")
    assert len(guidelines) >= 1
    assert guidelines[0].score >= 2.5
    assert guidelines[0].pmid == "999"

    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_clinical_guidelines_organization_expansion(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    pubmed = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    engine = GuidelinesEngine(pubmed=pubmed, cache=cache, settings=settings)

    respx.get(ESEARCH_URL).respond(json={"esearchresult": {"idlist": ["1000"]}})
    respx.get(EFETCH_URL).respond(
        content=(
            "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1000</PMID>"
            "<Article><Journal><Title>Circulation</Title></Journal>"
            "<ArticleTitle>AHA guideline for hypertension: consensus recommendation.</ArticleTitle>"
            "<Abstract><AbstractText>Expert consensus recommendation on blood pressure "
            "management.</AbstractText></Abstract>"
            "</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
        ).encode()
    )

    guidelines, meta = await engine.search_clinical_guidelines(
        "hypertension", organization="American Heart Association"
    )
    assert len(guidelines) >= 1
    assert guidelines[0].pmid == "1000"

    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_clinical_guidelines_marks_error_on_pubmed_failure(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    pubmed = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    engine = GuidelinesEngine(pubmed=pubmed, cache=cache, settings=settings)
    try:
        route = respx.get(ESEARCH_URL).mock(side_effect=httpx.ConnectError("boom"))

        guidelines, meta = await engine.search_clinical_guidelines("asthma")
        assert guidelines == []
        assert meta.error is True

        after_first = route.call_count
        await engine.search_clinical_guidelines("asthma")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()
