import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap, PaperMetadata
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import annotate_oa_status
from scholar_mcp.providers.pubmed import PubMedProvider
from scholar_mcp.providers.scihub import SciHubProvider
from scholar_mcp.utils.http import AsyncHttpClient

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
CROSSREF = "https://api.crossref.org/works"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


def test_pubmed_query_builder_applies_filters():
    q = PubMedProvider.build_query(
        "crispr", author="Doudna J", journal="Nature", year_start=2015, year_end=2020
    )
    assert "crispr" in q
    assert '"Doudna J"[Author]' in q
    assert '"Nature"[Journal]' in q
    assert "2015" in q and "2020" in q and "[PDAT]" in q


@respx.mock
async def test_pubmed_search_returns_metadata(client):
    respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["32000000"]}})
    )
    respx.get(url__startswith=ESUMMARY).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "uids": ["32000000"],
                    "32000000": {
                        "title": "A PubMed Paper",
                        "authors": [{"name": "Doudna J"}],
                        "pubdate": "2020 Mar",
                        "fulljournalname": "Nature",
                        "elocationid": "doi: 10.1038/nature123",
                    },
                }
            },
        )
    )
    results = await PubMedProvider(client, Settings()).search("crispr", num_results=5)
    assert len(results) == 1
    assert results[0].title == "A PubMed Paper"
    assert results[0].pmid == "32000000"
    assert results[0].doi == "10.1038/nature123"


@respx.mock
async def test_crossref_search_returns_metadata(client):
    respx.get(url__startswith=CROSSREF).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1038/xref1",
                            "title": ["A CrossRef Paper"],
                            "author": [{"given": "Ada", "family": "Lovelace"}],
                            "container-title": ["Science"],
                            "issued": {"date-parts": [[2019]]},
                        }
                    ]
                }
            },
        )
    )
    results = await CrossRefProvider(client).search("crispr", num_results=5)
    assert results[0].doi == "10.1038/xref1"
    assert "Ada Lovelace" in results[0].authors


@respx.mock
async def test_oa_status_annotated_in_one_batched_call(client):
    """oa_status must cost one request for the whole page, not one per paper."""
    route = respx.get(url__startswith=EPMC).mock(
        return_value=httpx.Response(
            200,
            json={
                "resultList": {
                    "result": [
                        {"doi": "10.1/a", "isOpenAccess": "Y"},
                        {"doi": "10.1/b", "isOpenAccess": "N"},
                    ]
                }
            },
        )
    )
    papers = [
        PaperMetadata(title="A", doi="10.1/a"),
        PaperMetadata(title="B", doi="10.1/b"),
        PaperMetadata(title="C", doi=None),
    ]
    await annotate_oa_status(papers, client)
    assert route.call_count == 1
    assert papers[0].oa_status == "oa"
    assert papers[1].oa_status == "closed"
    assert papers[2].oa_status == "unknown"


@respx.mock
async def test_scihub_mirror_fallback(client, monkeypatch):
    respx.get(url__startswith="https://mirror1.org").mock(return_value=httpx.Response(500))
    respx.get(url__startswith="https://mirror2.org").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="//cyber.sci-hub.se/tree/10.1038/test.pdf#view=fitH"></iframe></html>',
        )
    )
    respx.get(url__regex=r"https://cyber\.sci-hub\.se/.*\.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-scihub-data")
    )
    monkeypatch.setattr(
        "scholar_mcp.providers.scihub.pdf_bytes_to_text", lambda b: "SciHub Extracted Content"
    )
    provider = SciHubProvider(client, mirrors=["https://mirror1.org", "https://mirror2.org"])
    res = await provider.fetch_full_text(IdentifierMap(doi="10.1038/test"))
    assert res is not None and res.source == "scihub"
    assert "SciHub Extracted Content" in res.content


@respx.mock
async def test_scihub_all_mirrors_down_is_miss(client):
    respx.get(url__regex=r"https://mirror\d\.org.*").mock(return_value=httpx.Response(503))
    provider = SciHubProvider(client, mirrors=["https://mirror1.org", "https://mirror2.org"])
    assert await provider.fetch_full_text(IdentifierMap(doi="10.1038/test")) is None


async def test_scihub_without_doi_is_miss(client):
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"])
    assert await provider.fetch_full_text(IdentifierMap(pmid="123")) is None
