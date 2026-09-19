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
