import httpx
import pytest
import respx

from pdf_fixtures import make_text_pdf
from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.arxiv import ARXIV_API, ARXIV_PDF, ArxivProvider
from scholar_mcp.utils.http import AsyncHttpClient

PDF_BODY = "We propose a transformer model for language understanding."

ATOM_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2305.18290v2</id>
    <title>Attention Is All You Need in Deep Learning</title>
    <summary>We propose a model.</summary>
    <published>2023-05-29T00:00:00Z</published>
    <author><name>Smith, Jane</name></author>
    <author><name>Doe, John</name></author>
    <arxiv:journal_ref>Nature 640, 2025</arxiv:journal_ref>
    <arxiv:doi>10.1038/example</arxiv:doi>
  </entry>
</feed>
"""


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_arxiv_fetch_metadata(client):
    respx.get(url__startswith=ARXIV_API).mock(
        return_value=httpx.Response(200, text=ATOM_FEED)
    )
    provider = ArxivProvider(client)
    meta = await provider.fetch_metadata("2305.18290v2")
    assert meta is not None
    assert meta.title == "Attention Is All You Need in Deep Learning"
    assert meta.authors == ["Smith, Jane", "Doe, John"]
    assert meta.year == "2023"
    assert meta.abstract == "We propose a model."
    assert meta.venue == "Nature 640, 2025"
    assert meta.doi == "10.1038/example"


@respx.mock
async def test_arxiv_fetch_metadata_empty_feed(client):
    respx.get(url__startswith=ARXIV_API).mock(
        return_value=httpx.Response(200, text='<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>')
    )
    provider = ArxivProvider(client)
    assert await provider.fetch_metadata("9999.99999") is None


@respx.mock
async def test_arxiv_fetch_metadata_error_feed(client):
    """arXiv error feeds contain an entry with an errors ID and title 'Error'."""
    error_feed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format_for_9999.99999</id>
    <title>Error</title>
    <summary>incorrect id format for 9999.99999</summary>
    <published>2023-01-01T00:00:00Z</published>
  </entry>
</feed>
"""
    respx.get(url__startswith=ARXIV_API).mock(
        return_value=httpx.Response(200, text=error_feed)
    )
    provider = ArxivProvider(client)
    assert await provider.fetch_metadata("9999.99999") is None


@respx.mock
async def test_arxiv_fetch_full_text_no_id(client):
    provider = ArxivProvider(client)
    assert await provider.fetch_full_text(IdentifierMap(doi="10.1038/x")) is None
    assert provider.last_skip_reason == "NO_ARXIV_ID"


@respx.mock
async def test_arxiv_fetch_full_text(client):
    route = respx.get(url__startswith=f"{ARXIV_PDF}/2305.18290").mock(
        return_value=httpx.Response(200, content=make_text_pdf(PDF_BODY))
    )
    provider = ArxivProvider(client)
    res = await provider.fetch_full_text(IdentifierMap(arxiv="2305.18290", doi="10.48550/arXiv.2305.18290"))
    assert route.called
    assert res is not None
    assert res.status == "full_text"
    assert res.source == "arxiv"
    assert PDF_BODY in res.content
    assert res.total_chars == len(res.content)
    assert res.url == "https://arxiv.org/abs/2305.18290"
    assert res.doi == "10.48550/arXiv.2305.18290"


@respx.mock
async def test_arxiv_skip_reason_is_cleared_on_next_request(client):
    """last_skip_reason is per-request state on a provider reused across calls."""
    provider = ArxivProvider(client)

    # First request has no arXiv ID, so the tier legitimately self-skips.
    assert await provider.fetch_full_text(IdentifierMap(doi="10.1038/x")) is None
    assert provider.last_skip_reason == "NO_ARXIV_ID"

    # Second request does have an arXiv ID: the tier runs and misses. The waterfall
    # reads last_skip_reason after every miss, so a stale value mislabels this as a skip.
    respx.get(url__startswith=f"{ARXIV_PDF}/2305.18290").mock(
        return_value=httpx.Response(404)
    )
    assert await provider.fetch_full_text(IdentifierMap(arxiv="2305.18290")) is None
    assert provider.last_skip_reason == ""
