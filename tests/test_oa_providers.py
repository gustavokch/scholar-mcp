import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.providers.pmc import PMCProvider
from scholar_mcp.providers.unpaywall import UnpaywallProvider
from scholar_mcp.utils.http import AsyncHttpClient

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL = "https://api.unpaywall.org/v2"

PMC_XML = (
    b"<article><front><article-meta><title-group>"
    b"<article-title>Test</article-title></title-group></article-meta></front>"
    b"<body><sec><title>Results</title><p>Content body.</p></sec></body></article>"
)


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_pmc_provider_hit(client):
    respx.get(url__startswith=EFETCH).mock(return_value=httpx.Response(200, content=PMC_XML))
    res = await PMCProvider(client).fetch_full_text(IdentifierMap(pmcid="PMC123456"))
    assert res is not None
    assert res.status == "full_text" and res.source == "pmc"
    assert "Content body." in res.content
    assert "Results" in res.sections_available


@respx.mock
async def test_pmc_provider_without_pmcid_is_miss(client):
    assert await PMCProvider(client).fetch_full_text(IdentifierMap(doi="10.1/x")) is None


@respx.mock
async def test_pmc_provider_empty_body_is_miss(client):
    """PMC returns 200 with a metadata-only stub for non-OA records; that is a miss."""
    respx.get(url__startswith=EFETCH).mock(
        return_value=httpx.Response(200, content=b"<article><front/></article>")
    )
    assert await PMCProvider(client).fetch_full_text(IdentifierMap(pmcid="PMC1")) is None


@respx.mock
async def test_pmc_provider_upstream_error_is_miss_not_raise(client):
    respx.get(url__startswith=EFETCH).mock(return_value=httpx.Response(500))
    assert await PMCProvider(client).fetch_full_text(IdentifierMap(pmcid="PMC1")) is None


@respx.mock
async def test_europe_pmc_provider_hit(client):
    respx.get(url__regex=rf"{EPMC}/.*fullTextXML").mock(
        return_value=httpx.Response(200, content=PMC_XML)
    )
    res = await EuropePMCProvider(client).fetch_full_text(
        IdentifierMap(pmcid="PMC123456", doi="10.1/x")
    )
    assert res is not None and res.source == "europepmc"
    assert "Content body." in res.content


@respx.mock
async def test_unpaywall_skipped_without_email(client):
    provider = UnpaywallProvider(client, email=None)
    assert await provider.fetch_full_text(IdentifierMap(doi="10.1038/sample")) is None
    assert "UNPAYWALL_EMAIL" in provider.last_skip_reason
    assert respx.calls.call_count == 0  # no HTTP attempted at all


@respx.mock
async def test_unpaywall_provider_hit(client, monkeypatch):
    respx.get(url__startswith=UNPAYWALL).mock(
        return_value=httpx.Response(
            200,
            json={
                "is_oa": True,
                "title": "Unpaywall Title",
                "best_oa_location": {
                    "url_for_pdf": "https://oa.org/paper.pdf",
                    "url": "https://oa.org/paper",
                },
            },
        )
    )
    respx.get("https://oa.org/paper.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-sample")
    )
    monkeypatch.setattr(
        "scholar_mcp.providers.unpaywall.pdf_bytes_to_text", lambda b: "Extracted PDF Body"
    )
    res = await UnpaywallProvider(client, email="test@example.com").fetch_full_text(
        IdentifierMap(doi="10.1038/sample")
    )
    assert res is not None and res.source == "unpaywall"
    assert res.format == "text"
    assert "Extracted PDF Body" in res.content


@respx.mock
async def test_unpaywall_closed_access_is_miss(client):
    respx.get(url__startswith=UNPAYWALL).mock(
        return_value=httpx.Response(200, json={"is_oa": False, "best_oa_location": None})
    )
    res = await UnpaywallProvider(client, email="t@e.com").fetch_full_text(
        IdentifierMap(doi="10.1038/closed")
    )
    assert res is None


@respx.mock
async def test_unpaywall_pdf_yielding_no_text_is_miss(client, monkeypatch):
    """A PDF that extracts to nothing (scanned image) must fall through, not return empty text."""
    respx.get(url__startswith=UNPAYWALL).mock(
        return_value=httpx.Response(
            200,
            json={"is_oa": True, "best_oa_location": {"url_for_pdf": "https://oa.org/scan.pdf"}},
        )
    )
    respx.get("https://oa.org/scan.pdf").mock(return_value=httpx.Response(200, content=b"%PDF"))
    monkeypatch.setattr("scholar_mcp.providers.unpaywall.pdf_bytes_to_text", lambda b: "  ")
    res = await UnpaywallProvider(client, email="t@e.com").fetch_full_text(
        IdentifierMap(doi="10.1038/scan")
    )
    assert res is None
