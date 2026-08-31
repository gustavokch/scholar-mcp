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
    esearch_route = respx.get(url__startswith=ESEARCH).mock(
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
    results = await PubMedProvider(client, Settings()).search("crispr", num_results=5, sort="relevance")
    assert len(results) == 1
    assert results[0].title == "A PubMed Paper"
    assert results[0].pmid == "32000000"
    assert results[0].doi == "10.1038/nature123"
    # Verify relevance sort is requested from NCBI (default esearch order is date, not relevance)
    request = esearch_route.calls.last.request
    assert request.url.params.get("sort") == "relevance"


@respx.mock
async def test_pubmed_search_sort_date(client):
    esearch_route = respx.get(url__startswith=ESEARCH).mock(
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
    results = await PubMedProvider(client, Settings()).search("crispr", num_results=5, sort="pub_date")
    assert len(results) == 1
    request = esearch_route.calls.last.request
    assert request.url.params.get("sort") == "pub_date"


@respx.mock
async def test_pubmed_search_captures_pubtype_and_issn(client):
    respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["111"]}})
    )
    respx.get(url__startswith=ESUMMARY).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "uids": ["111"],
                    "111": {
                        "title": "A Randomized Trial of X.",
                        "authors": [{"name": "Doe J"}],
                        "pubdate": "2024",
                        "fulljournalname": "New England Journal of Medicine",
                        "pubtype": ["Journal Article", "Randomized Controlled Trial"],
                        "issn": "0028-4793",
                        "essn": "1533-4406",
                    },
                }
            },
        )
    )

    results = await PubMedProvider(client, Settings()).search("x trial", num_results=5)

    assert len(results) == 1
    assert results[0].study_type == "Journal Article; Randomized Controlled Trial"
    assert results[0].evidence_grade == "1b"
    assert results[0].issn == "0028-4793"


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


@respx.mock
async def test_pubmed_fetch_abstract_structured_labels(client):
    efetch_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>99999999</PMID>
      <Article>
        <ArticleTitle>A Trial of Treatment</ArticleTitle>
        <Abstract>
          <AbstractText Label="BACKGROUND">Cancer is a complex disease.</AbstractText>
          <AbstractText Label="METHODS">We conducted a randomized trial.</AbstractText>
          <AbstractText Label="RESULTS">Survival improved significantly.</AbstractText>
          <AbstractText Label="CONCLUSIONS">Treatment was effective.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Smith</LastName><ForeName>John</ForeName></Author>
        </AuthorList>
        <Journal><Title>Journal of Clinical Medicine</Title></Journal>
        <ArticleIdList>
          <ArticleId IdType="doi">10.1000/182</ArticleId>
        </ArticleIdList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""

    respx.get(url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=efetch_xml)
    )

    provider = PubMedProvider(client, Settings())
    meta = await provider.fetch_abstract(IdentifierMap(pmid="99999999"))

    assert meta is not None
    assert meta.title == "A Trial of Treatment"
    assert "BACKGROUND: Cancer is a complex disease." in meta.abstract
    assert "METHODS: We conducted a randomized trial." in meta.abstract
    assert "RESULTS: Survival improved significantly." in meta.abstract
    assert "CONCLUSIONS: Treatment was effective." in meta.abstract

