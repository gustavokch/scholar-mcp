# tests/medical/test_govbr_az.py
from scholar_mcp.medical.govbr_az import (
    build_alias_text,
    load_seed_catalog,
    parse_az_index,
    parse_az_letter_page,
)
from scholar_mcp.medical.govbr_common import normalize_text

AZ_INDEX_HTML = """
<div id="content-core">
  <a href="/saude/pt-br/assuntos/saude-de-a-a-z/d">D</a>
  <a href="/saude/pt-br/assuntos/saude-de-a-a-z/t/">T</a>
  <a href="/saude/pt-br/assuntos/saude-de-a-a-z">Topo</a>
</div>
"""

AZ_LETTER_HTML = """
<div id="content-core">
  <a class="govbr-card-content" href="/saude/pt-br/assuntos/saude-de-a-a-z/d/dengue">
    <span class="titulo">Dengue</span>
  </a>
  <a class="govbr-card-content" href="/saude/pt-br/assuntos/saude-de-a-a-z/d/dtha">
    <span class="titulo">Doenças de Transmissão Hídrica e Alimentar</span>
  </a>
  <a class="govbr-card-content" href="/saude/pt-br/assuntos/saude-de-a-a-z/d/">
    <span class="titulo">Sem slug</span>
  </a>
</div>
"""


def test_parse_az_index_returns_letter_urls():
    assert parse_az_index(AZ_INDEX_HTML) == [
        "https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z/d",
        "https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z/t",
    ]


def test_parse_az_letter_page_maps_slug_to_title():
    assert parse_az_letter_page(AZ_LETTER_HTML) == {
        "dengue": "Dengue",
        "dtha": "Doenças de Transmissão Hídrica e Alimentar",
    }


def test_build_alias_text_matches_title_to_abbreviation():
    aliases = {"dtha": "Doenças de Transmissão Hídrica e Alimentar", "dengue": "Dengue"}
    assert build_alias_text("Doenças de Transmissão Hídrica e Alimentar", aliases) == "dtha"


def test_build_alias_text_matches_on_containment():
    aliases = {"tuberculose": "Tuberculose"}
    text = build_alias_text("Manual de Recomendações para o Controle da Tuberculose", aliases)
    assert "tuberculose" in text


def test_build_alias_text_empty_when_no_match():
    assert build_alias_text("Boletim epidemiológico", {"dengue": "Dengue"}) == ""


def test_build_alias_text_word_boundary():
    # "dtha" shouldn't match within "widthas"
    aliases = {"dtha": "doencas de transmissao hidrica e alimentar"}
    assert build_alias_text("widthas title", aliases) == ""
    assert "doencas de transmissao hidrica e alimentar" in build_alias_text("manual de dtha no brasil", aliases)


from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical.govbr_az import GovBrAZEngine
from scholar_mcp.medical.govbr_common import SEVEN_DAYS_SECONDS
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


SVSA_INDEX = """
<div id="content-core">
  <a href="/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue">Dengue</a>
  <a href="/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/tuberculose">Tuberculose</a>
</div>
"""

GUIAS_INDEX = """
<div id="content-core">
  <a href="/saude/pt-br/centrais-de-conteudo/publicacoes/guias-e-manuais/2024">2024</a>
</div>
"""


def _listing(slug: str, title: str, folder: str) -> str:
    return f"""
    <div id="content-core">
      <article class="tileItem tile-file">
        <h2 class="tileHeadline">
          <a class="summary url" href="https://www.gov.br{folder}/{slug}">{title}</a>
        </h2>
        <p class="tileBody"><span class="description">desc {slug}</span></p>
      </article>
    </div>
    """


LOGIN_GATE = "<html><body>/acl_users/credentials_cookie_auth/require_login</body></html>"


def _make_engine(tmp_path, responses):
    http = AsyncMock()

    async def fake_get(url, **kwargs):
        if url in responses:
            return responses[url]
        for key in sorted(responses.keys(), key=len, reverse=True):
            if url.startswith(key):
                return responses[key]
        return FakeResponse("", status_code=404)

    http.get = AsyncMock(side_effect=fake_get)
    settings = Settings()
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = GovBrAZEngine(http, cache, settings)
    return engine, http


SVSA = "/saude/pt-br/centrais-de-conteudo/publicacoes/svsa"
GUIAS = "/saude/pt-br/centrais-de-conteudo/publicacoes/guias-e-manuais"


@pytest.fixture
def responses():
    return {
        "https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z/d": FakeResponse(
            AZ_LETTER_HTML
        ),
        "https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z/t": FakeResponse(
            '<a class="govbr-card-content" href="/saude/pt-br/assuntos/saude-de-a-a-z/t/tuberculose">'
            '<span class="titulo">Tuberculose</span></a>'
        ),
        "https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z": FakeResponse(
            AZ_INDEX_HTML
        ),
        f"https://www.gov.br{SVSA}/dengue": FakeResponse(
            _listing("dengue-manejo-clinico", "Dengue: diagnóstico e manejo clínico", f"{SVSA}/dengue")
        ),
        f"https://www.gov.br{SVSA}/tuberculose": FakeResponse(
            _listing("manual-tuberculose", "Manual de Recomendações da Tuberculose", f"{SVSA}/tuberculose")
        ),
        f"https://www.gov.br{SVSA}": FakeResponse(SVSA_INDEX),
        f"https://www.gov.br{GUIAS}/2024": FakeResponse(
            _listing("guia-vigilancia", "Guia de Vigilância em Saúde", f"{GUIAS}/2024")
        ),
        f"https://www.gov.br{GUIAS}": FakeResponse(GUIAS_INDEX),
    }


async def test_refresh_catalog_indexes_both_trees(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog = await engine.refresh_catalog()

        assert "govbr-svsa-dengue-dengue-manejo-clinico" in catalog
        assert "govbr-svsa-tuberculose-manual-tuberculose" in catalog
        assert "govbr-guias-2024-guia-vigilancia" in catalog

        row = catalog["govbr-svsa-dengue-dengue-manejo-clinico"]
        assert row["download_url"].endswith("/@@download/file")
        assert row["tree"] == "svsa"
        assert row["topic"] == "dengue"
        assert "dengue" in row["aliases"]
        assert catalog["govbr-guias-2024-guia-vigilancia"]["year"] == "2024"
    finally:
        await engine.cache.close()


async def test_refresh_catalog_caches_full_crawl(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        cached, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is True
        assert "govbr-svsa-dengue-dengue-manejo-clinico" in cached
    finally:
        await engine.cache.close()


async def test_refresh_catalog_does_not_cache_partial_crawl(tmp_path, responses):
    responses[f"https://www.gov.br{SVSA}/tuberculose"] = FakeResponse("", status_code=503)
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog = await engine.refresh_catalog()

        assert "govbr-svsa-dengue-dengue-manejo-clinico" in catalog
        _, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_skips_login_gated_folders(tmp_path, responses):
    responses[f"https://www.gov.br{SVSA}/tuberculose"] = FakeResponse(LOGIN_GATE)
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog = await engine.refresh_catalog()

        assert not any(key.startswith("govbr-svsa-tuberculose") for key in catalog)
        _, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_follows_pagination_once_per_url(tmp_path, responses):
    folder = f"https://www.gov.br{SVSA}/dengue"
    page_two = _listing("dengue-boletim", "Boletim da Dengue", f"{SVSA}/dengue")
    responses[f"{folder}?b_start:int=20"] = FakeResponse(page_two)
    responses[folder] = FakeResponse(
        _listing("dengue-manejo-clinico", "Dengue: manejo", f"{SVSA}/dengue")
        + f'<a href="{folder}?b_start:int=20">2</a>'
    )
    engine, http = _make_engine(tmp_path, responses)
    try:
        catalog = await engine.refresh_catalog()

        assert "govbr-svsa-dengue-dengue-boletim" in catalog
        urls = [call.args[0] for call in http.get.await_args_list]
        assert len(urls) == len(set(urls))
    finally:
        await engine.cache.close()


async def test_search_ranks_exact_topic_match_first(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        results, meta = await engine.search("tuberculose", limit=5)

        assert meta.error is False
        assert results
        assert results[0].record_id == "govbr-svsa-tuberculose-manual-tuberculose"
        assert results[0].document_url.endswith("/@@download/file")
        assert results[0].source == "brazil-moh"
        assert results[0].collections == ["SVSA"]
        assert results[0].authors == ["Ministério da Saúde"]
        assert results[0].country == "Brasil"
        assert results[0].languages == ["pt"]
    finally:
        await engine.cache.close()


async def test_search_returns_empty_for_blank_query(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        results, meta = await engine.search("   ", limit=5)
        assert results == []
        assert meta.error is False
    finally:
        await engine.cache.close()


async def test_search_respects_limit(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        results, _ = await engine.search("dengue", limit=1)
        assert len(results) == 1
    finally:
        await engine.cache.close()


async def test_search_uses_cache_on_second_call(tmp_path, responses):
    engine, http = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        await engine.search("dengue", limit=5)
        calls_after_first = http.get.await_count
        results, meta = await engine.search("dengue", limit=5)
        assert meta.cached is True
        assert http.get.await_count == calls_after_first
        assert results
    finally:
        await engine.cache.close()


async def test_get_guideline_by_record_id(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        record = await engine.get_guideline("govbr-svsa-dengue-dengue-manejo-clinico")
        assert record is not None
        assert record.title.startswith("Dengue")
    finally:
        await engine.cache.close()


async def test_get_guideline_by_slug(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        record = await engine.get_guideline("dengue-manejo-clinico")
        assert record is not None
        assert record.record_id == "govbr-svsa-dengue-dengue-manejo-clinico"
    finally:
        await engine.cache.close()


async def test_get_guideline_unknown_returns_none(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        assert await engine.get_guideline("nao-existe") is None
    finally:
        await engine.cache.close()


async def test_guias_records_carry_year(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.refresh_catalog()
        record = await engine.get_guideline("govbr-guias-2024-guia-vigilancia")
        assert record is not None
        assert record.year == "2024"
        assert record.collections == ["GUIAS-E-MANUAIS"]
    finally:
        await engine.cache.close()


def test_seed_catalog_is_bundled_and_non_empty():
    seed = load_seed_catalog()
    assert len(seed) > 50


def test_seed_catalog_rows_have_required_fields():
    seed = load_seed_catalog()
    for record_id, row in seed.items():
        assert row["record_id"] == record_id
        assert row["title"]
        assert row["download_url"].endswith("/@@download/file")
        assert row["tree"] in {"svsa", "guias"}


def test_seed_catalog_contains_dengue_and_tuberculosis_manuals():
    titles = [normalize_text(row["title"]) for row in load_seed_catalog().values()]
    assert any("dengue" in title for title in titles)
    assert any("tuberculose" in title for title in titles)


async def test_get_catalog_memoizes_partial_refresh(tmp_path, responses):
    """A partial crawl must not re-crawl gov.br on the next get_catalog call.

    refresh_catalog only writes the cache for a complete crawl, so a partial
    result that is returned without being memoized makes every subsequent
    search re-crawl both publication trees for as long as gov.br is degraded.
    """
    engine, _ = _make_engine(tmp_path, responses)
    try:
        engine.cache.get = AsyncMock(
            return_value=(
                {"old": {"record_id": "old"}},
                CacheMetadata(
                    cached=True, cache_age=SEVEN_DAYS_SECONDS + 1, error=False
                ),
            )
        )
        calls: list[int] = []

        async def _partial_refresh(incumbent=None):
            calls.append(1)
            return {"new": {"record_id": "new"}}

        engine.refresh_catalog = _partial_refresh

        first = await engine.get_catalog()
        second = await engine.get_catalog()

        assert first == second == {"new": {"record_id": "new"}}
        assert len(calls) == 1, "partial crawl re-ran on the second get_catalog call"
    finally:
        await engine.cache.close()


def test_build_alias_text_matches_portuguese_plural():
    """Titles pluralize the disease name; the alias must still apply."""
    aliases = {"hepatite": "Hepatite"}
    assert "hepatite" in build_alias_text("Manual das Hepatites Virais", aliases)


def test_build_alias_text_plural_does_not_reopen_false_positives():
    aliases = {"dtha": "doencas de transmissao hidrica e alimentar"}
    assert build_alias_text("widthas title", aliases) == ""
    assert build_alias_text("largura dthas nao", aliases) != ""


async def test_search_reports_error_when_catalog_unavailable(tmp_path, responses):
    """An empty catalog is an outage, not a zero-result search.

    Caching it would pin a false success for cache_ttl_brazil_moh and hide
    the failure from errored_any in brazil_moh.
    """
    engine, _ = _make_engine(tmp_path, responses)
    try:
        engine.get_catalog = AsyncMock(return_value={})

        results, meta = await engine.search("dengue", limit=5)

        assert results == []
        assert meta.error is True
        _, cache_meta = await engine.cache.get("govbr_az_search:5:dengue")
        assert cache_meta.cached is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_refuses_to_pin_a_shrunken_crawl(tmp_path, responses):
    """A parser break must not overwrite a good catalog for seven days.

    Every folder answers 200, so folders_ok == folders_total and the old
    guard would have pinned the result; only the row count reveals that the
    listing parser stopped matching.
    """
    engine, _ = _make_engine(tmp_path, responses)
    try:
        incumbent = {f"row-{i}": {"record_id": f"row-{i}"} for i in range(100)}

        catalog = await engine.refresh_catalog(incumbent=incumbent)

        assert catalog, "the crawl is still returned to this caller"
        _, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_pins_a_crawl_that_holds_its_size(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        incumbent = {"row-0": {"record_id": "row-0"}, "row-1": {"record_id": "row-1"}}

        await engine.refresh_catalog(incumbent=incumbent)

        _, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is True
    finally:
        await engine.cache.close()
