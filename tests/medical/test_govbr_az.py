# tests/medical/test_govbr_az.py
from scholar_mcp.medical.govbr_az import (
    build_alias_text,
    parse_az_index,
    parse_az_letter_page,
)

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


from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical.govbr_az import GovBrAZEngine
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
    return GovBrAZEngine(http, cache, settings), http


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
