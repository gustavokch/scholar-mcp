from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.pediatrics import PediatricsEngine
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

BF_URL = "https://brightfutures.aap.org/Search"
AAP_URL = "https://publications.aap.org/pediatrics/search"


async def _engine(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = PediatricsEngine(
        http_client=http_client, cache=cache, settings=settings, jitter_range=None
    )
    return engine, cache, http_client


def _install_fake_camoufox(monkeypatch, rendered_html=""):
    """Fake camoufox.async_api; returns (attempts, captured_urls)."""
    import sys
    import types

    attempts: list[bool] = []
    captured_urls: list[str] = []

    class _FakePage:
        async def goto(self, url, *a, **k):
            captured_urls.append(url)
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
    return attempts, captured_urls


def _install_fake_playwright(monkeypatch):
    """Fake playwright.async_api that records async_playwright() attempts."""
    import sys
    import types

    attempts: list[bool] = []

    class _FakeContext:
        async def __aenter__(self):
            attempts.append(True)
            return None

        async def __aexit__(self, *exc):
            return False

    def _fake_async_playwright():
        return _FakeContext()

    api_mod = types.ModuleType("playwright.async_api")
    api_mod.async_playwright = _fake_async_playwright
    pw_mod = types.ModuleType("playwright")
    pw_mod.async_api = api_mod
    monkeypatch.setitem(sys.modules, "playwright", pw_mod)
    monkeypatch.setitem(sys.modules, "playwright.async_api", api_mod)
    return attempts


@respx.mock
async def test_search_bright_futures_html(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    respx.get(BF_URL).respond(
        html="""
    <html><body>
      <div class="search-result">
        <h3 class="title"><a href="/guidelines/infant-nutrition">
          Infant Nutrition Guidelines (0-12 months)</a></h3>
        <p class="description">Recommendations on breastfeeding and complementary feeding.</p>
      </div>
      <div class="search-result">
        <h3 class="title"><a href="/x">No</a></h3>
      </div>
    </body></html>
    """
    )

    guidelines, meta = await engine.search_bright_futures("nutrition")
    assert len(guidelines) == 1  # short-title item dropped
    assert "Infant Nutrition" in guidelines[0].title
    assert guidelines[0].source == "bright-futures"
    assert guidelines[0].organization == "American Academy of Pediatrics"
    assert guidelines[0].category == "Preventive Care"
    assert "0-12 months" in guidelines[0].age_group
    assert guidelines[0].url.startswith("https://brightfutures.aap.org/")
    assert len(guidelines[0].description) <= 300
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_aap_policy_html(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    respx.get(AAP_URL).respond(
        html="""
    <html><body>
      <article class="publication-item">
        <h2><a href="/pediatrics/article/1">AAP Policy Statement on Asthma 2023</a></h2>
        <p>Policy summary.</p>
      </article>
    </body></html>
    """
    )

    guidelines, meta = await engine.search_aap_policy("asthma")
    assert len(guidelines) == 1
    assert guidelines[0].source == "aap-policy"
    assert guidelines[0].year == "2023"
    assert guidelines[0].category == "Policy Statement"
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_aap_guidelines_combines_and_dedups(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    shared = "<h3><a href='/a'>Guideline on Nutrition 2023</a></h3><p>Different text.</p>"
    respx.get(BF_URL).respond(html=f"<div class='search-result'>{shared}</div>")
    respx.get(AAP_URL).respond(
        html=f"<div class='search-result'>{shared}</div>"  # identical normalized title
    )

    guidelines, meta = await engine.search_aap_guidelines("nutrition")
    assert len(guidelines) == 1  # exact normalized-title dedup
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_literature_composes_journal_query(tmp_path: Path):
    from unittest.mock import AsyncMock

    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    engine, cache, http_client = await _engine(tmp_path)
    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = ([], CacheMetadata(cached=False, cache_age=0))
    engine.pubmed = mock_pubmed

    await engine.search_pediatric_literature("asthma", max_results=5)
    term = mock_pubmed.search_articles.await_args.args[0]
    assert "asthma" in term
    assert '"Pediatrics"[Journal]' in term
    assert '"JAMA Pediatrics"[Journal]' in term
    assert "European Journal of Pediatrics" in term
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_bright_futures_marks_error_and_skips_cache_on_failure(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(BF_URL).mock(side_effect=httpx.ConnectError("boom"))

        guidelines, meta = await engine.search_bright_futures("nutrition")
        assert guidelines == []
        assert meta.error is True

        after_first = route.call_count
        await engine.search_bright_futures("nutrition")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_aap_policy_marks_error_on_failure(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(AAP_URL).mock(side_effect=httpx.ConnectError("boom"))

        guidelines, meta = await engine.search_aap_policy("nutrition")
        assert guidelines == []
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_aap_guidelines_marks_error_when_one_source_fails(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(BF_URL).respond(
            html="""
        <html><body>
          <div class="search-result">
            <h3 class="title"><a href="/guidelines/infant-nutrition">
              Infant Nutrition Guidelines (0-12 months)</a></h3>
            <p class="description">Recommendations on complementary feeding.</p>
          </div>
        </body></html>
        """
        )
        respx.get(AAP_URL).mock(side_effect=httpx.ConnectError("boom"))

        guidelines, meta = await engine.search_aap_guidelines("nutrition")
        # Partial results are still returned, but flagged as incomplete.
        assert guidelines
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_direct_scrapes_drop_items_unrelated_to_query(tmp_path: Path):
    """server.py routes to search_bright_futures / search_aap_policy directly;
    those paths must apply the same query-overlap filter as the combined
    search so SPA navigation junk cannot reach callers."""
    engine, cache, http_client = await _engine(tmp_path)
    junk_html = """
    <html><body>
      <div class="search-result">
        <h3 class="title"><a href="/practice-management/bright-futures/quality">Quality Improvement</a></h3>
        <p>Site navigation.</p>
      </div>
    </body></html>
    """
    respx.get(BF_URL).respond(html=junk_html)
    respx.get(AAP_URL).respond(html=junk_html)

    bf, bf_meta = await engine.search_bright_futures("nutrition")
    assert bf == []
    assert bf_meta.error is False

    aap, aap_meta = await engine.search_aap_policy("nutrition")
    assert aap == []
    assert aap_meta.error is False

    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_aap_guidelines_falls_back_to_pubmed_on_scrape_failure(tmp_path: Path):
    """Both AAP scrapes failing (e.g. Cloudflare 403) falls back to a PubMed
    publication-type search filtered to AAP instead of returning nothing."""
    from unittest.mock import AsyncMock

    from scholar_mcp.medical.models import MedicalArticle
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    engine, cache, http_client = await _engine(tmp_path)
    engine.settings.enable_browser_fallback = False
    respx.get(BF_URL).respond(status_code=403)
    respx.get(AAP_URL).respond(status_code=403)

    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        [
            MedicalArticle(
                title=(
                    "American Academy of Pediatrics guideline: "
                    "ibuprofen use in infants under 6 months"
                ),
                abstract=(
                    "Recommendations and best practice for ibuprofen dosing "
                    "and contraindications in children under 6 months."
                ),
                pmid="12345",
                year="2024",
            )
        ],
        CacheMetadata(cached=False, cache_age=0),
    )
    engine.pubmed = mock_pubmed

    guidelines, meta = await engine.search_aap_guidelines("ibuprofen children")
    assert len(guidelines) == 1
    assert guidelines[0].source == "pubmed-aap"
    assert guidelines[0].organization == "American Academy of Pediatrics"
    assert meta.error is False
    term = mock_pubmed.search_articles.await_args.args[0]
    assert "ibuprofen" in term
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_aap_guidelines_ignores_scrape_items_unrelated_to_query(tmp_path: Path):
    """BF/AAP search pages are SPAs that render static nav items regardless of
    the query. Items whose title shares no word with the query are junk and
    must not block the PubMed fallback."""
    from unittest.mock import AsyncMock

    from scholar_mcp.medical.models import MedicalArticle
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    engine, cache, http_client = await _engine(tmp_path)
    engine.settings.enable_browser_fallback = False
    respx.get(BF_URL).respond(
        html="""
    <html><body>
      <div class="search-result">
        <h3 class="title"><a href="/practice-management/bright-futures/quality">Quality Improvement</a></h3>
        <p>Site navigation.</p>
      </div>
      <div class="search-result">
        <h3 class="title"><a href="/practice-management/bright-futures/stories">Implementation Stories</a></h3>
        <p>Site news.</p>
      </div>
    </body></html>
    """
    )
    respx.get(AAP_URL).respond(status_code=403)

    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        [
            MedicalArticle(
                title=(
                    "American Academy of Pediatrics guideline: "
                    "ibuprofen use in infants under 6 months"
                ),
                abstract=(
                    "Recommendations and best practice for ibuprofen dosing "
                    "and contraindications in children under 6 months."
                ),
                pmid="12345",
                year="2024",
            )
        ],
        CacheMetadata(cached=False, cache_age=0),
    )
    engine.pubmed = mock_pubmed

    guidelines, meta = await engine.search_aap_guidelines("ibuprofen children")
    assert len(guidelines) == 1
    assert guidelines[0].source == "pubmed-aap"
    assert meta.error is False
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_aap_guidelines_browser_is_last_resort(tmp_path: Path, monkeypatch):
    """The camoufox browser fallback must run only after the PubMed fallback
    also found nothing."""
    import sys
    import types
    from unittest.mock import AsyncMock

    from scholar_mcp.medical.models import MedicalArticle
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    engine, cache, http_client = await _engine(tmp_path)
    respx.get(BF_URL).respond(status_code=403)
    respx.get(AAP_URL).respond(status_code=403)

    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        [],
        CacheMetadata(cached=False, cache_age=0),
    )
    engine.pubmed = mock_pubmed

    rendered_html = """
    <html><body>
      <div class="search-result">
        <h3 class="title"><a href="/pediatrics/article/9">
          Ibuprofen Safety in Infants 2024</a></h3>
        <p class="description">Policy summary for infant dosing.</p>
      </div>
    </body></html>
    """

    attempts, _urls = _install_fake_camoufox(monkeypatch, rendered_html)
    # Block the legacy playwright path so the pre-camoufox source cannot open
    # a real browser during this test.
    _install_fake_playwright(monkeypatch)

    try:
        guidelines, meta = await engine.search_aap_guidelines("ibuprofen")
        assert attempts, "browser fallback never attempted"
        assert guidelines
        assert guidelines[0].title.startswith("Ibuprofen Safety")
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()



@respx.mock
async def test_last_resort_browser_scrape_uses_camoufox_and_encodes_query(
    tmp_path: Path, monkeypatch
):
    """The last-resort browser scrape must drive camoufox (not playwright)
    and URL-encode the query it appends as ?q= so '&' or '#' in an
    agent-supplied query cannot misroute the request."""
    from unittest.mock import AsyncMock

    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    engine, cache, http_client = await _engine(tmp_path)
    respx.get(BF_URL).respond(status_code=403)
    respx.get(AAP_URL).respond(status_code=403)

    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        [],
        CacheMetadata(cached=False, cache_age=0),
    )
    engine.pubmed = mock_pubmed

    camoufox_attempts, captured = _install_fake_camoufox(monkeypatch)
    pw_attempts = _install_fake_playwright(monkeypatch)

    try:
        await engine.search_aap_guidelines("ibuprofen & children")
        assert captured, "browser fallback never attempted"
        assert "ibuprofen+%26+children" in captured[0]
        assert not pw_attempts, "playwright path still reachable"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_browser_fallback_skipped_when_pubmed_yields_results(tmp_path: Path, monkeypatch):
    """The camoufox browser must never launch when the PubMed fallback
    already returned results."""
    from unittest.mock import AsyncMock

    from scholar_mcp.medical.models import MedicalArticle
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    engine, cache, http_client = await _engine(tmp_path)
    respx.get(BF_URL).respond(status_code=403)
    respx.get(AAP_URL).respond(status_code=403)

    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        [
            MedicalArticle(
                title=(
                    "American Academy of Pediatrics guideline: "
                    "ibuprofen use in infants under 6 months"
                ),
                abstract=(
                    "Recommendations and best practice for ibuprofen dosing "
                    "and contraindications in children under 6 months."
                ),
                pmid="12345",
                year="2024",
            )
        ],
        CacheMetadata(cached=False, cache_age=0),
    )
    engine.pubmed = mock_pubmed

    camoufox_attempts, _urls = _install_fake_camoufox(monkeypatch)

    try:
        guidelines, meta = await engine.search_aap_guidelines("ibuprofen children")
        assert not camoufox_attempts, "browser launched despite PubMed results"
        assert len(guidelines) == 1
        assert guidelines[0].source == "pubmed-aap"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()
