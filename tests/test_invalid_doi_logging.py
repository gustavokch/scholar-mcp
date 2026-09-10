import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.resolver import WaterfallResolver
from scholar_mcp.utils.http import AsyncHttpClient


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_fetch_abstract_missing_doi_emits_no_http_warning(client, caplog):
    doi = "10.1093/humupd/dmab061"
    # PubMed returns empty
    respx.get(url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": []}})
    )
    respx.get(url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi").mock(
        return_value=httpx.Response(200, json={"result": {"uids": []}})
    )
    # CrossRef 404
    respx.get(f"https://api.crossref.org/works/{doi}").mock(
        return_value=httpx.Response(404, text="Resource not found.")
    )
    # OpenAlex 404
    respx.get("https://api.openalex.org/works/https://doi.org/10.1093%2Fhumupd%2Fdmab061").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    settings = Settings(enable_openalex=True)
    resolver = WaterfallResolver(settings=settings, http_client=client)

    with caplog.at_level("WARNING", logger="scholar_mcp.utils.http"):
        meta = await resolver.fetch_abstract(IdentifierMap(doi=doi))

    assert meta is None
    assert len(caplog.records) == 0


@respx.mock
async def test_get_metadata_missing_doi_emits_no_http_warning(client, caplog):
    doi = "10.1093/humupd/dmab061"
    # NCBI ID conversion returns empty records
    respx.get(url__startswith="https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    # PubMed returns empty
    respx.get(url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi").mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": []}})
    )
    respx.get(url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi").mock(
        return_value=httpx.Response(200, json={"result": {"uids": []}})
    )
    # CrossRef 404
    respx.get(f"https://api.crossref.org/works/{doi}").mock(
        return_value=httpx.Response(404, text="Resource not found.")
    )
    # OpenAlex 404
    respx.get("https://api.openalex.org/works/https://doi.org/10.1093%2Fhumupd%2Fdmab061").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    settings = Settings(enable_openalex=True)
    resolver = WaterfallResolver(settings=settings, http_client=client)

    with caplog.at_level("WARNING", logger="scholar_mcp.utils.http"):
        meta = await resolver.get_metadata(doi)

    assert meta is None
    assert len(caplog.records) == 0
