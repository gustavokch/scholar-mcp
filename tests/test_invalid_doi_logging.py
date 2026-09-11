import httpx
import pytest
import respx
from log_helpers import HTTP_LOGGER, assert_no_http_warnings

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.resolver import WaterfallResolver
from scholar_mcp.utils.http import AsyncHttpClient

MISSING_DOI = "10.1093/humupd/dmab061"
CROSSREF_URL = f"https://api.crossref.org/works/{MISSING_DOI}"
OPENALEX_URL = "https://api.openalex.org/works/https://doi.org/10.1093%2Fhumupd%2Fdmab061"
ESEARCH_PREFIX = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_PREFIX = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
IDCONV_PREFIX = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


def _mock_empty_pubmed() -> None:
    respx.get(url__startswith=ESEARCH_PREFIX).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": []}})
    )
    respx.get(url__startswith=ESUMMARY_PREFIX).mock(
        return_value=httpx.Response(200, json={"result": {"uids": []}})
    )


def _mock_missing_doi_registries() -> tuple[respx.Route, respx.Route]:
    crossref = respx.get(CROSSREF_URL).mock(
        return_value=httpx.Response(404, text="Resource not found.")
    )
    openalex = respx.get(OPENALEX_URL).mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    return crossref, openalex


@respx.mock
async def test_fetch_abstract_missing_doi_emits_no_http_warning(client, caplog):
    _mock_empty_pubmed()
    crossref, openalex = _mock_missing_doi_registries()

    resolver = WaterfallResolver(
        settings=Settings(enable_openalex=True), http_client=client
    )
    with caplog.at_level("WARNING", logger=HTTP_LOGGER):
        meta = await resolver.fetch_abstract(IdentifierMap(doi=MISSING_DOI))

    # Both registries must actually be reached; a mistyped mock URL would
    # otherwise leave the 404 path untested.
    assert crossref.called
    assert openalex.called
    assert meta is None
    assert_no_http_warnings(caplog)


@respx.mock
async def test_get_metadata_missing_doi_emits_no_http_warning(client, caplog):
    respx.get(url__startswith=IDCONV_PREFIX).mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    _mock_empty_pubmed()
    crossref, openalex = _mock_missing_doi_registries()

    resolver = WaterfallResolver(
        settings=Settings(enable_openalex=True), http_client=client
    )
    with caplog.at_level("WARNING", logger=HTTP_LOGGER):
        meta = await resolver.get_metadata(MISSING_DOI)

    assert crossref.called
    assert openalex.called
    assert meta is None
    assert_no_http_warnings(caplog)
