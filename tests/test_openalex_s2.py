import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.openalex import OPENALEX_BASE, OpenAlexProvider
from scholar_mcp.providers.semantic_scholar import S2_BASE, S2_RECS_BASE, SemanticScholarProvider
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
async def test_openalex_enrichment_keeps_existing_oa_url():
    """A closed OpenAlex record must not erase an oa_url found by an earlier provider."""
    from unittest.mock import AsyncMock

    from scholar_mcp.models import PaperMetadata
    from scholar_mcp.resolver import WaterfallResolver

    closed_work = dict(WORK_JSON, open_access={"is_oa": False, "oa_url": None})
    respx.get(url__startswith=f"{OPENALEX_BASE}/works/https://doi.org/").mock(
        return_value=httpx.Response(200, json=closed_work)
    )
    resolver = WaterfallResolver(settings=Settings())
    resolver.pubmed.fetch_abstract = AsyncMock(
        return_value=PaperMetadata(
            title="Existing",
            abstract="An abstract.",
            oa_url="https://existing.example/paper.pdf",
        )
    )
    meta = await resolver.fetch_abstract(IdentifierMap(doi="10.1038/nature123"))
    assert meta is not None
    assert meta.citation_count == 42  # enrichment still applied
    assert meta.oa_url == "https://existing.example/paper.pdf"
    await resolver.http_client.aclose()


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


S2_SEARCH_JSON = {
    "data": [
        {
            "paperId": "abc123",
            "title": "S2 Found Paper",
            "year": 2022,
            "venue": "NeurIPS",
            "abstract": "An S2 abstract.",
            "citationCount": 100,
            "authors": [{"name": "LeCun, Yann"}],
            "externalIds": {"DOI": "10.5555/s2paper", "ArXiv": "2201.00001"},
            "openAccessPdf": {"url": "https://example.com/s2.pdf"},
        }
    ]
}

S2_RECS_JSON = {
    "recommendedPapers": [
        {
            "paperId": "def456",
            "title": "Recommended Paper",
            "year": 2023,
            "venue": "ICML",
            "authors": [{"name": "Bengio, Yoshua"}],
            "externalIds": {"DOI": "10.5555/rec1"},
        }
    ]
}


@respx.mock
async def test_s2_search(client):
    route = respx.get(url__startswith=f"{S2_BASE}/paper/search").mock(
        return_value=httpx.Response(200, json=S2_SEARCH_JSON)
    )
    provider = SemanticScholarProvider(client, api_key=None)
    papers = await provider.search("deep learning", num_results=5, year_start=2020, year_end=2023)
    assert len(papers) == 1
    p = papers[0]
    assert p.title == "S2 Found Paper"
    assert p.year == "2022"
    assert p.doi == "10.5555/s2paper"
    assert p.citation_count == 100
    assert p.oa_url == "https://example.com/s2.pdf"
    assert p.oa_status == "oa"
    assert "year=2020-2023" in str(route.calls[0].request.url)


@respx.mock
async def test_s2_search_sends_api_key_header(client):
    route = respx.get(url__startswith=f"{S2_BASE}/paper/search").mock(
        return_value=httpx.Response(200, json=S2_SEARCH_JSON)
    )
    provider = SemanticScholarProvider(client, api_key="secret")
    await provider.search("test", num_results=1)
    assert route.calls[0].request.headers.get("x-api-key") == "secret"


@respx.mock
async def test_s2_recommendations(client):
    route = respx.get(url__startswith="https://api.semanticscholar.org/recommendations/v1/papers/forpaper/").mock(
        return_value=httpx.Response(200, json=S2_RECS_JSON)
    )
    provider = SemanticScholarProvider(client, api_key=None)
    recs = await provider.fetch_recommendations("DOI:10.1038/nature123", limit=5)
    assert len(recs) == 1
    assert recs[0].title == "Recommended Paper"
    assert recs[0].doi == "10.5555/rec1"
    assert recs[0].authors == ["Bengio, Yoshua"]
    # Verify paper_id was URL-encoded to avoid splitting URL path segments
    assert "DOI%3A10.1038%2Fnature123" in str(route.calls[0].request.url)


@respx.mock
async def test_s2_search_post_filters_author_and_journal(client):
    """S2 graph search has no author/journal filter, so results must be filtered locally."""
    respx.get(url__startswith=f"{S2_BASE}/paper/search").mock(
        return_value=httpx.Response(200, json=S2_SEARCH_JSON)
    )
    provider = SemanticScholarProvider(client, api_key=None)

    assert await provider.search("dl", num_results=5, author="LeCun") != []
    assert await provider.search("dl", num_results=5, author="Curie") == []
    assert await provider.search("dl", num_results=5, journal="NeurIPS") != []
    assert await provider.search("dl", num_results=5, journal="Nature") == []


@respx.mock
async def test_s2_empty_result_set_returns_empty(client):
    respx.get(url__startswith=f"{S2_BASE}/paper/search").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    provider = SemanticScholarProvider(client, api_key=None)
    assert await provider.search("nothing", num_results=5) == []


@respx.mock
async def test_s2_upstream_error_returns_empty(client):
    respx.get(url__startswith=f"{S2_BASE}/paper/search").mock(
        return_value=httpx.Response(500)
    )
    respx.get(url__startswith=f"{S2_RECS_BASE}/papers/forpaper/").mock(
        return_value=httpx.Response(500)
    )
    provider = SemanticScholarProvider(client, api_key=None)
    assert await provider.search("boom", num_results=5) == []
    assert await provider.fetch_recommendations("DOI:10.1038/x", limit=5) == []


@respx.mock
async def test_s2_malformed_payload_returns_empty(client):
    respx.get(url__startswith=f"{S2_BASE}/paper/search").mock(
        return_value=httpx.Response(200, text="not json")
    )
    provider = SemanticScholarProvider(client, api_key=None)
    assert await provider.search("bad", num_results=5) == []


@respx.mock
async def test_resolver_search_source_s2():
    from scholar_mcp.resolver import WaterfallResolver

    respx.get(url__startswith=f"{S2_BASE}/paper/search").mock(
        return_value=httpx.Response(200, json=S2_SEARCH_JSON)
    )
    # annotate_oa_status batch call after search
    respx.get(url__startswith="https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(
        return_value=httpx.Response(200, json={"resultList": {"result": []}})
    )
    resolver = WaterfallResolver(settings=Settings())
    papers = await resolver.search("deep learning", source="s2", num_results=5)
    assert len(papers) == 1
    assert papers[0].title == "S2 Found Paper"
    await resolver.http_client.aclose()


@respx.mock
async def test_resolver_related_papers_falls_back_to_s2():
    from scholar_mcp.resolver import WaterfallResolver

    # No pmid anywhere (arXiv-only paper); PubMed path yields nothing, S2 answers.
    respx.get(url__startswith="https://www.ncbi.nlm.nih.gov/pmc/utils/idconv").mock(
        return_value=httpx.Response(200, json={"records": []})
    )
    respx.get(
        url__startswith="https://api.semanticscholar.org/recommendations/v1/papers/forpaper/"
    ).mock(return_value=httpx.Response(200, json=S2_RECS_JSON))
    resolver = WaterfallResolver(settings=Settings())
    recs = await resolver.get_related_papers("arXiv:2305.18290", limit=5)
    assert len(recs) == 1
    assert recs[0].title == "Recommended Paper"
    await resolver.http_client.aclose()
