import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.utils.http import AsyncHttpClient

EPMC_REF = "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/12345/references"
CROSSREF_WORKS = "https://api.crossref.org/works/10.1038/nature123"


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_europe_pmc_fetch_references(client):
    respx.get(url__startswith=EPMC_REF).mock(
        return_value=httpx.Response(
            200,
            json={
                "referenceList": {
                    "reference": [
                        {
                            "id": "1",
                            "title": "Foundational Paper",
                            "authorString": "Smith J, Doe A",
                            "pubYear": "2020",
                            "journalTitle": "Nature",
                            "doi": "10.1038/ref1",
                            "pmid": "30000001",
                        }
                    ]
                }
            },
        )
    )
    provider = EuropePMCProvider(client)
    refs = await provider.fetch_references(IdentifierMap(pmid="12345"), limit=10)
    assert len(refs) == 1
    assert refs[0].title == "Foundational Paper"
    assert refs[0].doi == "10.1038/ref1"
    assert refs[0].pmid == "30000001"
    assert refs[0].year == "2020"
    assert refs[0].authors == ["Smith J", "Doe A"]


@respx.mock
async def test_crossref_fetch_references(client):
    respx.get(CROSSREF_WORKS).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "reference": [
                        {
                            "key": "ref1",
                            "article-title": "CrossRef Cited Article",
                            "author": "Lovelace A",
                            "year": "2019",
                            "journal-title": "Science",
                            "DOI": "10.1126/science.ref1",
                        }
                    ]
                }
            },
        )
    )
    provider = CrossRefProvider(client)
    refs = await provider.fetch_references("10.1038/nature123", limit=10)
    assert len(refs) == 1
    assert refs[0].title == "CrossRef Cited Article"
    assert refs[0].doi == "10.1126/science.ref1"
    assert refs[0].authors == ["Lovelace A"]
