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


import logging
from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical import govbr_az
from scholar_mcp.medical.govbr_az import GovBrAZEngine
from scholar_mcp.medical.govbr_common import CACHE_SCHEMA, SEVEN_DAYS_SECONDS
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager


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


async def _prime(engine):
    """Install a crawled fixture catalog as the in-memory catalog.

    refresh_catalog is offline-only and never sets it; search tests need a
    catalog built from the fixture pages, not the bundled seed.
    """
    catalog, complete = await engine.refresh_catalog()
    assert complete
    engine._memory_catalog = catalog


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
        catalog, complete = await engine.refresh_catalog()

        assert complete is True
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


async def test_refresh_catalog_caches_and_keeps_nothing(tmp_path, responses):
    """Offline-only: even a complete crawl is neither cached nor kept."""
    engine, _ = _make_engine(tmp_path, responses)
    try:
        _, complete = await engine.refresh_catalog()

        assert complete is True
        _, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is False
        assert engine._memory_catalog is None
    finally:
        await engine.cache.close()


def _warned_about(caplog, url: str) -> bool:
    """The seed writer exits with "see the warnings above": every page the
    crawl loses must be named at WARNING, not silently dropped."""
    return any(
        r.levelno >= logging.WARNING and url in r.getMessage() for r in caplog.records
    )


async def test_refresh_catalog_failed_folder_is_incomplete(tmp_path, responses, caplog):
    folder = f"https://www.gov.br{SVSA}/tuberculose"
    responses[folder] = FakeResponse("", status_code=503)
    engine, _ = _make_engine(tmp_path, responses)
    try:
        with caplog.at_level(logging.WARNING, logger="scholar_mcp.medical.govbr_az"):
            catalog, complete = await engine.refresh_catalog()

        assert "govbr-svsa-dengue-dengue-manejo-clinico" in catalog
        assert complete is False
        assert _warned_about(caplog, folder)
    finally:
        await engine.cache.close()


async def test_refresh_catalog_login_gated_folder_is_incomplete(tmp_path, responses, caplog):
    folder = f"https://www.gov.br{SVSA}/tuberculose"
    responses[folder] = FakeResponse(LOGIN_GATE)
    engine, _ = _make_engine(tmp_path, responses)
    try:
        with caplog.at_level(logging.WARNING, logger="scholar_mcp.medical.govbr_az"):
            catalog, complete = await engine.refresh_catalog()

        assert not any(key.startswith("govbr-svsa-tuberculose") for key in catalog)
        assert complete is False
        assert _warned_about(caplog, folder)
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
        catalog, complete = await engine.refresh_catalog()

        assert complete is True
        assert "govbr-svsa-dengue-dengue-boletim" in catalog
        urls = [call.args[0] for call in http.get.await_args_list]
        assert len(urls) == len(set(urls))
    finally:
        await engine.cache.close()


async def test_search_ranks_exact_topic_match_first(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await _prime(engine)
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
        await _prime(engine)
        results, meta = await engine.search("   ", limit=5)
        assert results == []
        assert meta.error is False
    finally:
        await engine.cache.close()


async def test_search_respects_limit(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await _prime(engine)
        results, _ = await engine.search("dengue", limit=1)
        assert len(results) == 1
    finally:
        await engine.cache.close()


async def test_search_uses_cache_on_second_call(tmp_path, responses):
    engine, http = _make_engine(tmp_path, responses)
    try:
        await _prime(engine)
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
        await _prime(engine)
        record = await engine.get_guideline("govbr-svsa-dengue-dengue-manejo-clinico")
        assert record is not None
        assert record.title.startswith("Dengue")
    finally:
        await engine.cache.close()


async def test_get_guideline_by_slug(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await _prime(engine)
        record = await engine.get_guideline("dengue-manejo-clinico")
        assert record is not None
        assert record.record_id == "govbr-svsa-dengue-dengue-manejo-clinico"
    finally:
        await engine.cache.close()


async def test_get_guideline_unknown_returns_none(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await _prime(engine)
        assert await engine.get_guideline("nao-existe") is None
    finally:
        await engine.cache.close()


async def test_guias_records_carry_year(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await _prime(engine)
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
        assert meta.error_kind != "successful_empty"
        assert meta.error_kind == "backend_error"
        _, cache_meta = await engine.cache.get("govbr_az_search:5:dengue")
        assert cache_meta.cached is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_refuses_a_shrunken_crawl(tmp_path, responses):
    """A parser break is not a smaller site: a crawl below the retention
    floor of the incumbent is incomplete even when every page answered."""
    engine, _ = _make_engine(tmp_path, responses)
    try:
        incumbent = {f"row-{i}": {"record_id": f"row-{i}"} for i in range(100)}

        catalog, complete = await engine.refresh_catalog(incumbent=incumbent)

        assert catalog, "the crawl is still returned to the caller"
        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_accepts_a_crawl_that_holds_its_size(tmp_path, responses):
    engine, _ = _make_engine(tmp_path, responses)
    try:
        incumbent = {"row-0": {"record_id": "row-0"}, "row-1": {"record_id": "row-1"}}

        _, complete = await engine.refresh_catalog(incumbent=incumbent)

        assert complete is True
    finally:
        await engine.cache.close()


def test_compile_alias_patterns_is_reused_across_titles(monkeypatch):
    """The alias vocabulary is compiled once per crawl, not once per title.

    refresh_catalog scores hundreds of items against hundreds of aliases,
    which overruns re's internal pattern cache and recompiles almost every
    time when the patterns are built inline.
    """
    import scholar_mcp.medical.govbr_az as az

    compiled: list[str] = []
    original = az._alias_pattern
    monkeypatch.setattr(
        az, "_alias_pattern", lambda term: (compiled.append(term), original(term))[1]
    )

    patterns = az.compile_alias_patterns({"dengue": "Dengue", "dtha": "DTHA"})
    after_compile = len(compiled)

    for _ in range(50):
        az.build_alias_text_compiled("Manual da Dengue", patterns)

    assert len(compiled) == after_compile, "alias patterns recompiled per title"


def test_build_alias_text_compiled_matches_the_uncompiled_helper():
    aliases = {"dtha": "Doenças de Transmissão Hídrica e Alimentar", "dengue": "Dengue"}
    title = "Doenças de Transmissão Hídrica e Alimentar"
    from scholar_mcp.medical.govbr_az import (
        build_alias_text_compiled,
        compile_alias_patterns,
    )

    assert build_alias_text_compiled(
        title, compile_alias_patterns(aliases)
    ) == build_alias_text(title, aliases)


async def test_az_search_pre_v1_cache_row_is_not_served(tmp_path, responses):
    """Same rule as the PCDT engine: an un-versioned row predates
    ``has_full_text`` and must not be served as current."""
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await _prime(engine)
        stale_row = [
            {
                "title": "Manual Tuberculose",
                "record_id": "govbr-svsa-tuberculose-manual-tuberculose",
                "document_url": "https://www.gov.br/x/manual-tuberculose/@@download/file",
            }
        ]
        await engine.cache.set(
            f"govbr_az_search:5:{normalize_text('tuberculose')}",
            stale_row,
            source="govbr_az",
        )

        results, meta = await engine.search("tuberculose", limit=5)

        assert meta.cached is False
        assert results[0].record_id == "govbr-svsa-tuberculose-manual-tuberculose"
        assert results[0].has_full_text is True
    finally:
        await engine.cache.close()


async def test_get_catalog_serves_seed_without_network_or_cache_row(tmp_path, responses):
    """The bundled seed is the catalog: no crawl and no SQLite copy of it."""
    engine, http = _make_engine(tmp_path, responses)
    try:
        catalog = await engine.get_catalog()

        assert catalog == load_seed_catalog()
        http.get.assert_not_awaited()
        _, meta = await engine.cache.get("govbr_az:catalog")
        assert meta.cached is False, "the seed must not be copied into SQLite"
    finally:
        await engine.cache.close()


async def test_get_catalog_seed_beats_a_leftover_cache_row(tmp_path, responses):
    """A catalog row written by an older release must not shadow the seed."""
    engine, _ = _make_engine(tmp_path, responses)
    try:
        await engine.cache.set(
            "govbr_az:catalog",
            {"old": {"record_id": "old"}},
            source="govbr_az",
            ttl=SEVEN_DAYS_SECONDS,
        )

        catalog = await engine.get_catalog()

        assert "old" not in catalog
        assert catalog == load_seed_catalog()
    finally:
        await engine.cache.close()


async def test_missing_seed_is_an_outage_and_is_not_kept(tmp_path, responses, monkeypatch):
    monkeypatch.setattr(govbr_az, "load_seed_catalog", lambda: {})
    engine, http = _make_engine(tmp_path, responses)
    try:
        catalog = await engine.get_catalog()

        assert catalog == {}
        assert engine._memory_catalog is None
        http.get.assert_not_awaited()

        results, meta = await engine.search("dengue", limit=5)
        assert results == []
        assert meta.error_kind == "backend_error"
        key = f"govbr_az_search:{CACHE_SCHEMA}:5:dengue"
        _, cache_meta = await engine.cache.get(key)
        assert cache_meta.cached is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_failed_second_page_is_incomplete(tmp_path, responses):
    """One loaded page is not a loaded folder."""
    folder = f"https://www.gov.br{SVSA}/dengue"
    responses[f"{folder}?b_start:int=20"] = FakeResponse("", status_code=503)
    responses[folder] = FakeResponse(
        _listing("dengue-manejo-clinico", "Dengue: manejo", f"{SVSA}/dengue")
        + f'<a href="{folder}?b_start:int=20">2</a>'
    )
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog, complete = await engine.refresh_catalog()

        assert "govbr-svsa-dengue-dengue-manejo-clinico" in catalog
        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_page_cap_is_incomplete(tmp_path, responses, monkeypatch):
    """A folder cut off by MAX_PAGES_PER_FOLDER with pages still queued is
    a truncated folder, not a complete one."""
    monkeypatch.setattr(govbr_az, "MAX_PAGES_PER_FOLDER", 2)
    folder = f"https://www.gov.br{SVSA}/dengue"
    pages = [folder] + [f"{folder}?b_start:int={20 * i}" for i in range(1, 4)]
    for i, url in enumerate(pages[:-1]):
        responses[url] = FakeResponse(
            _listing(f"dengue-{i}", f"Dengue parte {i}", f"{SVSA}/dengue")
            + f'<a href="{pages[i + 1]}">{i + 2}</a>'
        )
    engine, _ = _make_engine(tmp_path, responses)
    try:
        _, complete = await engine.refresh_catalog()

        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_failed_alias_index_is_incomplete(tmp_path, responses):
    """Rows crawled without the alias vocabulary lose their alias text."""
    responses["https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z"] = FakeResponse(
        "", status_code=503
    )
    engine, _ = _make_engine(tmp_path, responses)
    try:
        catalog, complete = await engine.refresh_catalog()

        assert catalog
        assert complete is False
    finally:
        await engine.cache.close()


async def test_refresh_catalog_failed_alias_letter_is_incomplete(tmp_path, responses):
    responses["https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z/t"] = FakeResponse(
        "", status_code=503
    )
    engine, _ = _make_engine(tmp_path, responses)
    try:
        _, complete = await engine.refresh_catalog()

        assert complete is False
    finally:
        await engine.cache.close()
