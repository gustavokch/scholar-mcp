import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.arxiv import ARXIV_API, ARXIV_PDF, ArxivProvider
from scholar_mcp.utils.http import AsyncHttpClient

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
async def test_arxiv_fetch_full_text_no_id(client):
    provider = ArxivProvider(client)
    assert await provider.fetch_full_text(IdentifierMap(doi="10.1038/x")) is None
    assert provider.last_skip_reason == "NO_ARXIV_ID"


@respx.mock
async def test_arxiv_fetch_full_text(client):
    from io import BytesIO

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    route = respx.get(url__startswith=f"{ARXIV_PDF}/2305.18290").mock(
        return_value=httpx.Response(200, content=pdf_bytes)
    )
    provider = ArxivProvider(client)
    # Blank page yields little text; assert the plumbing, not MIN_USEFUL_CHARS.
    res = await provider.fetch_full_text(IdentifierMap(arxiv="2305.18290"))
    assert route.called
    assert res is None or res.source == "arxiv"
