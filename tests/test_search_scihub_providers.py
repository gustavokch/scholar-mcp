import asyncio
import time
from typing import NamedTuple

import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap, PaperMetadata
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import annotate_oa_status
from scholar_mcp.providers.pubmed import PubMedProvider
from scholar_mcp.providers.scihub import SciHubProvider, _extract_pdf_url
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
async def test_pubmed_search_surfaces_relaxed_variant(client):
    """finding 3: relax=True defaults on the scholar path with no metadata
    channel, so a relaxed answer is indistinguishable from an exact one.
    The full query returns no hits; a relaxed variant does; the caller must
    be able to see which variant actually answered.
    """
    respx.get(url__startswith=ESEARCH).mock(
        side_effect=[
            httpx.Response(200, json={"esearchresult": {"idlist": []}}),
            httpx.Response(200, json={"esearchresult": {"idlist": ["32000000"]}}),
        ]
    )
    respx.get(url__startswith=ESUMMARY).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "uids": ["32000000"],
                    "32000000": {
                        "title": "A Relaxed Paper",
                        "authors": [{"name": "Doudna J"}],
                        "pubdate": "2020 Mar",
                        "fulljournalname": "Nature",
                    },
                }
            },
        )
    )
    provider = PubMedProvider(client, Settings())
    results = await provider.search(
        "novel therapeutic approaches for treating diabetes mellitus type",
        num_results=5,
    )
    assert len(results) == 1
    assert provider.last_relaxed_query is not None
    assert provider.last_relaxed_query != "novel therapeutic approaches for treating diabetes mellitus type"


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
    route = respx.get(url__startswith=CROSSREF).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1038/xref1",
                            "type": "journal-article",
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
    # Hygiene (ENAMED misses B2): journal-article filter, parsed doc_type,
    # and the source field the search envelope reports.
    assert "type:journal-article" in route.calls.last.request.url.params.get("filter", "")
    assert results[0].doc_type == "journal-article"
    assert results[0].source == "crossref"
    assert results[0].to_dict()["source"] == "crossref"


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
    pdf_route = respx.get(url__regex=r"https://cyber\.sci-hub\.se/.*\.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-scihub-data")
    )
    monkeypatch.setattr(
        "scholar_mcp.providers.scihub.pdf_bytes_to_text", lambda b: "SciHub Extracted Content"
    )
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org", "https://mirror2.org"], settings=settings)
    res = await provider.fetch_full_text(IdentifierMap(doi="10.1038/test"))
    assert res is not None and res.source == "scihub"
    assert "SciHub Extracted Content" in res.content
    assert pdf_route.called
    assert pdf_route.calls.last.request.headers.get("referer") == "https://mirror2.org/10.1038/test"


@respx.mock
async def test_scihub_passes_referer_header_from_redirected_url(client, monkeypatch):
    """Upstream PDF hosts (like sci.bban.top) block requests lacking a Referer header."""
    respx.get("https://mirror1.org/10.1038/redirected").mock(
        return_value=httpx.Response(
            301,
            headers={"Location": "https://landing-page.org/10.1038/redirected"},
        )
    )
    respx.get("https://landing-page.org/10.1038/redirected").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="https://upstream.org/paper.pdf"></iframe></html>',
        )
    )
    pdf_route = respx.get("https://upstream.org/paper.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-redirected-data")
    )
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)
    pdf_bytes, pdf_url = await provider.fetch_pdf_bytes(IdentifierMap(doi="10.1038/redirected"))
    assert pdf_bytes == b"%PDF-redirected-data"
    assert pdf_url == "https://upstream.org/paper.pdf"
    assert pdf_route.called
    assert pdf_route.calls.last.request.headers.get("referer") == "https://landing-page.org/10.1038/redirected"


@respx.mock
async def test_scihub_retries_without_referer_when_hotlink_protected(client):
    """Hosts configured with `valid_referers none …` reject a foreign Referer but
    serve a bare request, so a blocked fetch must be retried without the header."""
    seen: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("referer"))
        if request.headers.get("referer"):
            return httpx.Response(403, text="<html>Hotlink denied</html>")
        return httpx.Response(200, content=b"%PDF-hotlink-data")

    respx.get(url__startswith="https://mirror1.org").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="https://pdf-host.org/paper.pdf"></iframe></html>',
        )
    )
    respx.get("https://pdf-host.org/paper.pdf").mock(side_effect=_handler)
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)

    pdf_bytes, pdf_url = await provider.fetch_pdf_bytes(IdentifierMap(doi="10.1038/test"))

    assert pdf_bytes == b"%PDF-hotlink-data"
    assert pdf_url == "https://pdf-host.org/paper.pdf"
    assert seen == ["https://mirror1.org/10.1038/test", None]


@respx.mock
async def test_scihub_does_not_retry_bare_when_host_fails(client):
    """A 5xx is not a Referer problem. Paying a second fetch for it burns the
    resolver budget that the remaining mirrors need."""
    seen: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("referer"))
        return httpx.Response(500, text="upstream boom")

    respx.get(url__startswith="https://mirror1.org").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="https://pdf-host.org/paper.pdf"></iframe></html>',
        )
    )
    respx.get("https://pdf-host.org/paper.pdf").mock(side_effect=_handler)
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)

    pdf_bytes, pdf_url = await provider.fetch_pdf_bytes(IdentifierMap(doi="10.1038/test"))

    assert pdf_bytes is None and pdf_url is None
    assert seen == ["https://mirror1.org/10.1038/test"]


@respx.mock
async def test_scihub_retries_bare_when_referer_gets_bot_challenge(client):
    """A challenge page is a refusal, not a failure — the header is still the
    plausible cause, so the bare retry must still fire."""
    seen: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("referer"))
        if request.headers.get("referer"):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html>Just a moment...</html>",
            )
        return httpx.Response(200, content=b"%PDF-challenge-cleared")

    respx.get(url__startswith="https://mirror1.org").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="https://pdf-host.org/paper.pdf"></iframe></html>',
        )
    )
    respx.get("https://pdf-host.org/paper.pdf").mock(side_effect=_handler)
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)

    pdf_bytes, _ = await provider.fetch_pdf_bytes(IdentifierMap(doi="10.1038/test"))

    assert pdf_bytes == b"%PDF-challenge-cleared"
    assert seen == ["https://mirror1.org/10.1038/test", None]


class _FakeCamoufox(NamedTuple):
    """State captured by the fake browser; a bare tuple hid the third field."""

    attempts: list[bool]
    urls: list[str]
    headers: list[dict]


def _install_fake_camoufox(
    monkeypatch,
    rendered_html="",
    pdf_bytes=b"%PDF-1.5-fake-data",
    final_url=None,
    browser_status=200,
):
    import sys
    import types

    attempts: list[bool] = []
    captured_urls: list[str] = []
    captured_headers: list[dict] = []

    class _FakeResponse:
        status = browser_status

        async def body(self):
            return pdf_bytes

    class _FakeRequest:
        async def get(self, url, headers=None, *a, **k):
            captured_headers.append(headers or {})
            return _FakeResponse()

    class _FakePage:
        def __init__(self):
            self.request = _FakeRequest()
            self.url = ""

        async def goto(self, url, *a, **k):
            captured_urls.append(url)
            # A real page reports the URL it landed on after redirects.
            self.url = final_url or url
            return None

        async def content(self):
            return rendered_html

    class _FakeBrowser:
        async def new_page(self, *a, **k):
            return _FakePage()

    class _FakeCamoufoxContext:
        async def __aenter__(self):
            attempts.append(True)
            return _FakeBrowser()

        async def __aexit__(self, *exc):
            return False

    def _fake_async_camoufox(**launch_options):
        return _FakeCamoufoxContext()

    api_mod = types.ModuleType("camoufox.async_api")
    api_mod.AsyncCamoufox = _fake_async_camoufox
    camoufox_mod = types.ModuleType("camoufox")
    camoufox_mod.async_api = api_mod
    monkeypatch.setitem(sys.modules, "camoufox", camoufox_mod)
    monkeypatch.setitem(sys.modules, "camoufox.async_api", api_mod)
    return _FakeCamoufox(attempts, captured_urls, captured_headers)


def test_landing_url_falls_back_for_non_http_urls():
    """`page.url` is "about:blank" when navigation lands nowhere; that must not
    become a Referer, and must not become the base for relative PDF paths."""
    from scholar_mcp.providers.scihub import _landing_url

    assert _landing_url("https://mirror1.org/10.1038/test", "https://m/x") == (
        "https://mirror1.org/10.1038/test"
    )
    assert _landing_url("http://mirror1.org/10.1038/test", "https://m/x") == (
        "http://mirror1.org/10.1038/test"
    )
    assert _landing_url("about:blank", "https://m/x") == "https://m/x"
    assert _landing_url("", "https://m/x") == "https://m/x"
    assert _landing_url(None, "https://m/x") == "https://m/x"


def test_scihub_extract_pdf_url_resolves_relative_path():
    html = '<html><iframe src="/storage/10.1038/test.pdf#view=fitH"></iframe></html>'
    res = _extract_pdf_url(html, base_url="https://sci-hub.se/10.1038/test")
    assert res == "https://sci-hub.se/storage/10.1038/test.pdf"

    html_embed = '<html><embed src="/tree/10.1038/test.pdf"/></html>'
    res_embed = _extract_pdf_url(html_embed, base_url="https://sci-hub.se/10.1038/test")
    assert res_embed == "https://sci-hub.se/tree/10.1038/test.pdf"


@respx.mock
async def test_failing_mirror_deprioritized_on_next_call(client):
    """A mirror that fails gets a penalty; the next call tries healthy
    mirrors first (stable sort keeps config order among zero penalties)."""
    respx.get(url__startswith="https://m1.org").mock(side_effect=httpx.ConnectError("down"))
    respx.get(url__startswith="https://m2.org").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="https://cyber.sci-hub.se/deprio.pdf"></iframe></html>',
        )
    )
    respx.get(url__regex=r"https://cyber\.sci-hub\.se/.*\.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-deprio")
    )
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(
        client, mirrors=["https://m1.org", "https://m2.org"], settings=settings
    )
    ids = IdentifierMap(doi="10.1/deprio")
    b1, _ = await provider.fetch_pdf_bytes(ids)
    assert b1
    first_call_calls = len(respx.calls)
    b2, _ = await provider.fetch_pdf_bytes(ids)
    assert b2
    later_hosts = [str(c.request.url.host) for c in respx.calls[first_call_calls:]]
    assert later_hosts[0] == "m2.org"
    if "m1.org" in later_hosts:
        assert later_hosts.index("m2.org") < later_hosts.index("m1.org")


@respx.mock
async def test_mirror_tier_respects_tier_deadline(client):
    """The whole mirror tier is bounded, not just each mirror: 7 mirrors x a
    slow per-mirror timeout must not add up past scihub_tier_timeout_s."""
    async def _slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(503)

    mirrors = [f"https://t{i}.org" for i in range(7)]
    for m in mirrors:
        respx.get(url__startswith=m).mock(side_effect=_slow)
    settings = Settings(
        enable_browser_fallback=False,
        scihub_mirror_timeout_s=1.0,
        scihub_tier_timeout_s=2.0,
    )
    provider = SciHubProvider(client, mirrors=mirrors, settings=settings)
    start = time.monotonic()
    b, _ = await provider.fetch_pdf_bytes(IdentifierMap(doi="10.1/tier"))
    elapsed = time.monotonic() - start
    assert b is None
    assert elapsed < 5.0, f"mirror tier outlived its deadline: {elapsed:.1f}s"


@respx.mock
async def test_mirror_attempt_respects_per_mirror_timeout(client):
    """Each mirror gets at most scihub_mirror_timeout_s; a slow mirror must
    not burn the whole waterfall budget before the next mirror is tried."""
    async def _slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, text="too late")

    respx.get(url__startswith="https://slow.org").mock(side_effect=_slow)
    respx.get(url__startswith="https://fast.org").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="https://cyber.sci-hub.se/fast.pdf"></iframe></html>',
        )
    )
    respx.get(url__regex=r"https://cyber\.sci-hub\.se/.*\.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-fast")
    )
    settings = Settings(enable_browser_fallback=False, scihub_mirror_timeout_s=0.2)
    provider = SciHubProvider(
        client, mirrors=["https://slow.org", "https://fast.org"], settings=settings
    )
    start = time.monotonic()
    b, _ = await provider.fetch_pdf_bytes(IdentifierMap(doi="10.1/slow"))
    elapsed = time.monotonic() - start
    assert b
    assert elapsed < 2.0


@respx.mock
async def test_scihub_all_mirrors_down_is_miss(client, monkeypatch):
    respx.get(url__regex=r"https://mirror\d\.org.*").mock(return_value=httpx.Response(503))
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org", "https://mirror2.org"], settings=settings)
    assert await provider.fetch_full_text(IdentifierMap(doi="10.1038/test")) is None


@respx.mock
async def test_scihub_fetch_pdf_bytes_ignores_non_pdf_content(client):
    """When a mirror returns HTML/error page instead of PDF bytes, it should be ignored."""
    respx.get(url__startswith="https://mirror1.org").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="https://mirror1.org/paper.pdf"></iframe></html>',
        )
    )
    respx.get("https://mirror1.org/paper.pdf").mock(
        return_value=httpx.Response(200, content=b"<html>Cloudflare error</html>")
    )
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)
    pdf_bytes, pdf_url = await provider.fetch_pdf_bytes(IdentifierMap(doi="10.1038/test"))
    assert pdf_bytes is None
    assert pdf_url is None


@respx.mock
async def test_scihub_camoufox_fallback_when_http_blocked(client, monkeypatch):
    respx.get(url__regex=r"https://mirror\d\.org.*").mock(return_value=httpx.Response(403))
    rendered_html = '<html><embed src="https://sci-pdf.org/paper.pdf" type="application/pdf"/></html>'
    fake = _install_fake_camoufox(monkeypatch, rendered_html=rendered_html)
    monkeypatch.setattr(
        "scholar_mcp.providers.scihub.pdf_bytes_to_text", lambda b: "Camoufox SciHub Content"
    )
    settings = Settings(enable_browser_fallback=True)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)
    res = await provider.fetch_full_text(IdentifierMap(doi="10.1038/test"))
    assert res is not None and res.source == "scihub"
    assert "Camoufox SciHub Content" in res.content
    assert len(fake.attempts) == 1
    assert "https://mirror1.org/10.1038/test" in fake.urls
    assert len(fake.headers) == 1
    assert fake.headers[0].get("Referer") == "https://mirror1.org/10.1038/test"


@respx.mock
async def test_scihub_camoufox_uses_final_page_url_as_referer(client, monkeypatch):
    """A mirror that redirects must be referenced by the page it landed on, not by
    the requested URL — and relative PDF paths must resolve against it."""
    respx.get(url__regex=r"https://mirror\d\.org.*").mock(return_value=httpx.Response(403))
    rendered_html = '<html><embed src="paper.pdf" type="application/pdf"/></html>'
    fake = _install_fake_camoufox(
        monkeypatch,
        rendered_html=rendered_html,
        final_url="https://landed.org/10.1038/test",
    )
    settings = Settings(enable_browser_fallback=True)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)

    pdf_bytes, pdf_url = await provider._fetch_via_camoufox("10.1038/test")

    assert pdf_bytes == b"%PDF-1.5-fake-data"
    assert pdf_url == "https://landed.org/10.1038/paper.pdf"
    assert fake.headers[0].get("Referer") == "https://landed.org/10.1038/test"


@respx.mock
async def test_scihub_camoufox_falls_through_to_http_without_bare_retry(client, monkeypatch):
    """When the browser fetch is refused, the httpx fall-through still sends the
    Referer, but must not spend a third, bare request: a real browser session was
    already turned away, so the barest request has strictly less to offer."""
    respx.get(url__regex=r"https://mirror\d\.org.*").mock(return_value=httpx.Response(403))
    seen: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("referer"))
        return httpx.Response(403, text="<html>denied</html>")

    respx.get("https://sci-pdf.org/paper.pdf").mock(side_effect=_handler)
    rendered_html = (
        '<html><embed src="https://sci-pdf.org/paper.pdf" type="application/pdf"/></html>'
    )
    _install_fake_camoufox(monkeypatch, rendered_html=rendered_html, browser_status=403)
    settings = Settings(enable_browser_fallback=True)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)

    pdf_bytes, pdf_url = await provider._fetch_via_camoufox("10.1038/test")

    assert pdf_bytes is None and pdf_url is None
    assert seen == ["https://mirror1.org/10.1038/test"]


@respx.mock
async def test_scihub_browser_fallback_disabled_skips_camoufox(client, monkeypatch):
    respx.get(url__regex=r"https://mirror\d\.org.*").mock(return_value=httpx.Response(403))
    fake = _install_fake_camoufox(monkeypatch, rendered_html="<html></html>")
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)
    res = await provider.fetch_full_text(IdentifierMap(doi="10.1038/test"))
    assert res is None
    assert len(fake.attempts) == 0


async def test_scihub_camoufox_import_error_gracefully_handled(client, monkeypatch):
    import builtins

    _real_import = builtins.__import__

    def _block_camoufox(name, *args, **kwargs):
        if "camoufox" in name:
            raise ImportError("no camoufox")
        return _real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _block_camoufox)
    settings = Settings(enable_browser_fallback=True)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)
    bytes_res, url_res = await provider._fetch_via_camoufox("10.1038/test")
    assert bytes_res is None
    assert url_res is None


async def test_scihub_without_doi_is_miss(client):
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)
    assert await provider.fetch_full_text(IdentifierMap(pmid="123")) is None


@respx.mock
async def test_scihub_whitespace_doi_is_miss(client):
    route = respx.get(url__startswith="https://mirror1.org").mock(
        return_value=httpx.Response(200, text="<html>home</html>")
    )
    settings = Settings(enable_browser_fallback=False)
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"], settings=settings)
    assert await provider.fetch_full_text(IdentifierMap(doi="   ")) is None
    pdf_bytes, pdf_url = await provider.fetch_pdf_bytes(IdentifierMap(doi="   "))
    assert pdf_bytes is None
    assert pdf_url is None
    assert route.call_count == 0


@respx.mock
async def test_scihub_camoufox_caps_mirror_attempts(client, monkeypatch):
    """Camoufox fallback must try at most _CAMOUFOX_MAX_MIRRORS mirrors,
    not all 5 provided."""
    mirrors = [f"https://m{i}.org" for i in range(5)]
    for m in mirrors:
        respx.get(url__startswith=m).mock(return_value=httpx.Response(403))
    # Camoufox returns no PDF from any mirror (empty HTML)
    fake = _install_fake_camoufox(monkeypatch, rendered_html="<html></html>")
    settings = Settings(enable_browser_fallback=True)
    provider = SciHubProvider(client, mirrors=mirrors, settings=settings)
    await provider._fetch_via_camoufox("10.1038/test")
    from scholar_mcp.providers.scihub import _CAMOUFOX_MAX_MIRRORS
    assert len(fake.urls) == _CAMOUFOX_MAX_MIRRORS


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


@respx.mock
async def test_pubmed_fetch_abstract_ignores_reference_list_dois(client):
    """The EFetch document nests one ArticleIdList per cited reference under
    ReferenceList. The article's own DOI must come from the record's own
    ArticleIdList/ELocationID, never from a cited reference."""
    efetch_xml = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>39770434</PMID>
      <Article>
        <Journal><Title>Pharmaceuticals</Title></Journal>
        <ELocationID EIdType="doi" ValidYN="Y">10.3390/ph17121592</ELocationID>
        <ArticleTitle>Do Major Pharmacovigilance Databases Support Evidence of \
Fetotoxicity?</ArticleTitle>
        <Abstract><AbstractText>NSAIDs are fetotoxic.</AbstractText></Abstract>
        <AuthorList>
          <Author><LastName>Dathe</LastName><ForeName>Katarina</ForeName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">39770434</ArticleId>
        <ArticleId IdType="doi">10.3390/ph17121592</ArticleId>
        <ArticleId IdType="pmc">PMC11676342</ArticleId>
      </ArticleIdList>
      <ReferenceList>
        <Reference>
          <Citation>Earlier related work (2015)</Citation>
          <ArticleIdList>
            <ArticleId IdType="pubmed">25645319</ArticleId>
            <ArticleId IdType="doi">10.1007/s00404-015-3648-7</ArticleId>
          </ArticleIdList>
        </Reference>
        <Reference>
          <Citation>Another cited paper (2014)</Citation>
          <ArticleIdList>
            <ArticleId IdType="doi">10.1111/1471-0528.12653</ArticleId>
          </ArticleIdList>
        </Reference>
      </ReferenceList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""

    respx.get(url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=efetch_xml)
    )

    provider = PubMedProvider(client, Settings())
    meta = await provider.fetch_abstract(IdentifierMap(pmid="39770434"))

    assert meta is not None
    assert meta.doi == "10.3390/ph17121592"
    assert meta.doi != "10.1007/s00404-015-3648-7"
    assert meta.doi != "10.1111/1471-0528.12653"


def _efetch_xml(article_ids: str, elocation: str = "", reference_ids: str = "") -> str:
    """Build a minimal one-record EFetch document.

    ``article_ids`` populates the record's own PubmedData/ArticleIdList,
    ``reference_ids`` populates a single cited reference's ArticleIdList.
    """
    references = ""
    if reference_ids:
        references = f"""
      <ReferenceList>
        <Reference>
          <Citation>Cited work</Citation>
          <ArticleIdList>{reference_ids}</ArticleIdList>
        </Reference>
      </ReferenceList>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>39770434</PMID>
      <Article>
        <Journal><Title>Pharmaceuticals</Title></Journal>{elocation}
        <ArticleTitle>Do Major Pharmacovigilance Databases Support Evidence?</ArticleTitle>
        <Abstract><AbstractText>NSAIDs are fetotoxic.</AbstractText></Abstract>
        <AuthorList>
          <Author><LastName>Dathe</LastName><ForeName>Katarina</ForeName></Author>
        </AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>{article_ids}</ArticleIdList>{references}
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>"""


def _mock_efetch(xml: str) -> None:
    respx.get(url__startswith="https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi").mock(
        return_value=httpx.Response(200, text=xml)
    )


@respx.mock
async def test_pubmed_fetch_abstract_uses_elocationid_when_no_own_doi_id(client):
    """Some records carry the DOI only in Article/ELocationID. The record's own
    ArticleIdList then holds no doi entry, and the DOI must still be found there
    rather than falling through to a cited reference or to nothing."""
    _mock_efetch(
        _efetch_xml(
            article_ids=(
                '<ArticleId IdType="pubmed">39770434</ArticleId>'
                '<ArticleId IdType="pii">ph17121592</ArticleId>'
            ),
            elocation='<ELocationID EIdType="doi" ValidYN="Y">10.3390/ph17121592</ELocationID>',
            reference_ids='<ArticleId IdType="doi">10.1007/s00404-015-3648-7</ArticleId>',
        )
    )

    provider = PubMedProvider(client, Settings())
    meta = await provider.fetch_abstract(IdentifierMap(pmid="39770434"))

    assert meta is not None
    assert meta.doi == "10.3390/ph17121592"


@respx.mock
async def test_pubmed_fetch_abstract_skips_empty_own_doi_id(client):
    """An empty <ArticleId IdType="doi"/> in the record's own list must not end
    the scan; a later non-empty own entry is still the record's DOI."""
    _mock_efetch(
        _efetch_xml(
            article_ids=(
                '<ArticleId IdType="doi"></ArticleId>'
                '<ArticleId IdType="doi">10.3390/ph17121592</ArticleId>'
            ),
            reference_ids='<ArticleId IdType="doi">10.1007/s00404-015-3648-7</ArticleId>',
        )
    )

    provider = PubMedProvider(client, Settings())
    meta = await provider.fetch_abstract(IdentifierMap(pmid="39770434"))

    assert meta is not None
    assert meta.doi == "10.3390/ph17121592"


@respx.mock
async def test_pubmed_fetch_abstract_parses_own_pmcid(client):
    """The record's own PMC id sits in the same ArticleIdList as its DOI and is
    worth returning; a cited reference's PMC id is not the record's."""
    _mock_efetch(
        _efetch_xml(
            article_ids=(
                '<ArticleId IdType="pubmed">39770434</ArticleId>'
                '<ArticleId IdType="doi">10.3390/ph17121592</ArticleId>'
                '<ArticleId IdType="pmc">PMC11676342</ArticleId>'
            ),
            reference_ids=(
                '<ArticleId IdType="doi">10.1007/s00404-015-3648-7</ArticleId>'
                '<ArticleId IdType="pmc">PMC4321000</ArticleId>'
            ),
        )
    )

    provider = PubMedProvider(client, Settings())
    meta = await provider.fetch_abstract(IdentifierMap(pmid="39770434"))

    assert meta is not None
    assert meta.pmcid == "PMC11676342"



async def test_provider_last_error_is_per_request():
    """Providers are module-level singletons in server.py. A failing search
    running concurrently with a successful one must not leave its error on the
    attribute the successful call reads."""
    started = asyncio.Event()
    failed_done = asyncio.Event()

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"message": {"items": []}}

    async def slow_ok(url, **kwargs):
        started.set()
        await asyncio.sleep(0.05)
        return _Resp()

    async def fast_fail(url, **kwargs):
        return None

    async def run_ok():
        # Each task gets its own client: reassigning client.get from two
        # concurrent tasks races one patch over the other.
        client = AsyncHttpClient(
            settings=Settings(), max_retries=1, backoff_base=0.01
        )
        try:
            client.get = slow_ok
            provider = CrossRefProvider(client)
            await provider.search("ok query")
            await failed_done.wait()
            return provider.last_error
        finally:
            await client.aclose()

    async def run_fail():
        await started.wait()
        client = AsyncHttpClient(
            settings=Settings(), max_retries=1, backoff_base=0.01
        )
        try:
            client.get = fast_fail
            provider = CrossRefProvider(client)
            await provider.search("fail query")
            return provider.last_error
        finally:
            failed_done.set()

    ok_err, fail_err = await asyncio.gather(run_ok(), run_fail())
    assert fail_err == "transport"
    assert ok_err is None


async def test_mirror_penalty_is_capped(client, monkeypatch):
    """Penalties order the mirror list; they must not grow without bound as a
    long-lived process keeps retrying a dead mirror."""
    from scholar_mcp.providers.scihub import MAX_MIRROR_PENALTY

    provider = SciHubProvider(
        client, mirrors=["https://m1.example"], settings=Settings(enable_browser_fallback=False)
    )

    async def always_none(url, **kwargs):
        return None

    monkeypatch.setattr(client, "get", always_none)
    for _ in range(MAX_MIRROR_PENALTY + 5):
        await provider.fetch_pdf_bytes(IdentifierMap(doi="10.1/x"))
    assert provider._mirror_penalties["https://m1.example"] == MAX_MIRROR_PENALTY


_ESUMMARY_RECORD = {
    "result": {
        "uids": ["32000000"],
        "32000000": {
            "title": "A Relaxed PubMed Paper",
            "authors": [{"name": "Doudna J"}],
            "pubdate": "2020 Mar",
            "fulljournalname": "Nature",
            "elocationid": "doi: 10.1038/relaxed",
        },
    }
}


@respx.mock
async def test_pubmed_search_sends_ncbi_credentials(client):
    """The provider was missing api_key/email/tool entirely (2.8 req/s
    instead of 9 with a key). Both esearch and esummary must carry them."""
    esearch_route = respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["32000000"]}})
    )
    esummary_route = respx.get(url__startswith=ESUMMARY).mock(
        return_value=httpx.Response(200, json=_ESUMMARY_RECORD)
    )
    settings = Settings(pubmed_api_key="KEY123", pubmed_email="a@b.c", pubmed_tool="T")
    results = await PubMedProvider(client, settings).search("crispr", num_results=5)
    assert len(results) == 1
    for route in (esearch_route, esummary_route):
        params = route.calls.last.request.url.params
        assert params.get("api_key") == "KEY123"
        assert params.get("email") == "a@b.c"
        assert params.get("tool") == "T"


@respx.mock
async def test_pubmed_search_relaxes_long_query_preserving_filters(client):
    """An 8-token query ANDs to zero hits; the ladder rebuilds the
    author filter around the relaxed prefix and stops at the first hit."""
    def _router(request: httpx.Request) -> httpx.Response:
        term = request.url.params.get("term", "")
        if "theta" in term:
            return httpx.Response(200, json={"esearchresult": {"idlist": []}})
        return httpx.Response(200, json={"esearchresult": {"idlist": ["32000000"]}})

    esearch_route = respx.get(url__startswith=ESEARCH).mock(side_effect=_router)
    respx.get(url__startswith=ESUMMARY).mock(
        return_value=httpx.Response(200, json=_ESUMMARY_RECORD)
    )
    results = await PubMedProvider(client, Settings()).search(
        "alpha beta gamma delta epsilon zeta eta theta", author="Doudna J"
    )
    assert len(results) == 1
    assert results[0].pmid == "32000000"
    terms = [c.request.url.params.get("term", "") for c in esearch_route.calls]
    assert len(terms) == 2
    assert "theta" in terms[0] and '"Doudna J"[Author]' in terms[0]
    assert terms[1].startswith("alpha beta gamma delta epsilon")
    assert "zeta" not in terms[1]
    assert '"Doudna J"[Author]' in terms[1]
    assert "theta" not in terms[1]


@respx.mock
async def test_pubmed_search_does_not_relax_on_esearch_error(client):
    esearch_route = respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(500)
    )
    provider = PubMedProvider(client, Settings())
    results = await provider.search("alpha beta gamma delta epsilon zeta eta theta")
    assert results == []
    assert provider.last_error is not None
    assert len(esearch_route.calls) == 1
