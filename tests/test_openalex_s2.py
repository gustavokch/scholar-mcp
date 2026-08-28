import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.providers.openalex import OPENALEX_BASE, OpenAlexProvider
from scholar_mcp.utils.http import AsyncHttpClient

WORK_URL = f"{OPENALEX_BASE}/works/https://doi.org/10.1038/nature123"

WORK_JSON = {
    "id": "https://openalex.org/W1",
    "doi": "https://doi.org/10.1038/nature123",
    "title": "OpenAlex Paper",
    "publication_year": 2021,
    "cited_by_count": 42,
    "open_access": {"is_oa": True, "oa_url": "https://example.com/paper.pdf"},
    "primary_location": {"source": {"display_name": "Nature"}},
    "authorships": [
        {
            "author": {"display_name": "Curie, Marie"},
            "institutions": [{"display_name": "Sorbonne"}],
        }
    ],
    "abstract_inverted_index": {"Hello": [0], "world": [1]},
}

CITING_JSON = {
    "results": [
        {
            "title": "Citing Paper",
            "publication_year": 2023,
            "cited_by_count": 7,
            "doi": "https://doi.org/10.1000/citing1",
            "primary_location": {"source": {"display_name": "Science"}},
            "authorships": [{"author": {"display_name": "Hopper, Grace"}, "institutions": []}],
        }
    ]
}


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_openalex_fetch_metadata(client):
    respx.get(WORK_URL).mock(return_value=httpx.Response(200, json=WORK_JSON))
    provider = OpenAlexProvider(client, email="me@example.com")
    meta = await provider.fetch_metadata("10.1038/nature123")
    assert meta is not None
    assert meta.title == "OpenAlex Paper"
    assert meta.year == "2021"
    assert meta.citation_count == 42
    assert meta.oa_url == "https://example.com/paper.pdf"
    assert meta.institutions == ["Sorbonne"]
    assert meta.abstract == "Hello world"
    assert meta.oa_status == "oa"


@respx.mock
async def test_openalex_fetch_citations(client):
    respx.get(WORK_URL).mock(return_value=httpx.Response(200, json=WORK_JSON))
    respx.get(url__startswith=f"{OPENALEX_BASE}/works?").mock(
        return_value=httpx.Response(200, json=CITING_JSON)
    )
    provider = OpenAlexProvider(client, email="me@example.com")
    cits = await provider.fetch_citations("10.1038/nature123", limit=10)
    assert len(cits) == 1
    assert cits[0].title == "Citing Paper"
    assert cits[0].doi == "10.1000/citing1"
    assert cits[0].citation_count == 7
    assert cits[0].authors == ["Hopper, Grace"]


@respx.mock
async def test_openalex_handles_missing_work(client):
    respx.get(WORK_URL).mock(return_value=httpx.Response(404))
    provider = OpenAlexProvider(client, email="me@example.com")
    assert await provider.fetch_metadata("10.1038/nature123") is None
    assert await provider.fetch_citations("10.1038/nature123", limit=10) == []


@respx.mock
async def test_resolver_get_citations_falls_back_to_openalex():
    from scholar_mcp.resolver import WaterfallResolver

    # Europe PMC has nothing for this DOI; OpenAlex answers.
    respx.get(url__startswith="https://www.ncbi.nlm.nih.gov/pmc/utils/idconv").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    respx.get(url__startswith=f"{OPENALEX_BASE}/works/https://doi.org/").mock(
        return_value=httpx.Response(200, json=WORK_JSON)
    )
    respx.get(url__startswith=f"{OPENALEX_BASE}/works?").mock(
        return_value=httpx.Response(200, json=CITING_JSON)
    )
    resolver = WaterfallResolver(settings=Settings())
    cits = await resolver.get_citations("10.1038/nature123", limit=10)
    assert len(cits) == 1
    assert cits[0].title == "Citing Paper"
    await resolver.http_client.aclose()
