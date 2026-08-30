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
