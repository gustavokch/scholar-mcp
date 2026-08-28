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


EPMC_CIT = "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/12345/citations"
ELINK_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


@respx.mock
async def test_europe_pmc_fetch_citations(client):
    respx.get(url__startswith=EPMC_CIT).mock(
        return_value=httpx.Response(
            200,
            json={
                "citationList": {
                    "citation": [
                        {
                            "title": "Citing Paper Alpha",
                            "authorString": "Doe J",
                            "pubYear": "2022",
                            "journalTitle": "Cell",
                            "doi": "10.1016/j.cell.2022.01",
                            "pmid": "35000001",
                            "citedByCount": 12,
                        }
                    ]
                }
            },
        )
    )
    provider = EuropePMCProvider(client)
    cits = await provider.fetch_citations(IdentifierMap(pmid="12345"), limit=10)
    assert len(cits) == 1
    assert cits[0].title == "Citing Paper Alpha"
    assert cits[0].citation_count == 12
    assert cits[0].doi == "10.1016/j.cell.2022.01"


@respx.mock
async def test_pubmed_fetch_related_papers(client):
    respx.get(url__startswith=ELINK_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "linksets": [
                    {
                        "linksetdbs": [
                            {
                                "linkname": "pubmed_pubmed",
                                "links": [
                                    {"id": "31000001", "score": "95000000"},
                                    {"id": "31000002", "score": "82000000"},
                                ],
                            }
                        ]
                    }
                ]
            },
        )
    )
    respx.get(url__startswith=ESUMMARY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "uids": ["31000001", "31000002"],
                    "31000001": {
                        "title": "Related Paper 1",
                        "authors": [{"name": "Author One"}],
                        "pubdate": "2021",
                        "fulljournalname": "Genetics",
                        "elocationid": "doi: 10.1000/1",
                    },
                    "31000002": {
                        "title": "Related Paper 2",
                        "authors": [{"name": "Author Two"}],
                        "pubdate": "2021",
                        "fulljournalname": "Genomics",
                        "elocationid": "doi: 10.1000/2",
                    },
                }
            },
        )
    )
    from scholar_mcp.providers.pubmed import PubMedProvider

    provider = PubMedProvider(client, Settings())
    related = await provider.fetch_related_papers("12345", limit=2)
    assert len(related) == 2
    assert related[0].title == "Related Paper 1"
    assert related[0].score == 95.0
    assert related[0].doi == "10.1000/1"

