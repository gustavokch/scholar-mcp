# tests/medical/test_govbr_common.py
from scholar_mcp.medical.govbr_common import (
    derive_item_urls,
    is_login_redirect,
    normalize_text,
    score_item,
    tokenize_portuguese,
)


def test_normalize_text_folds_accents():
    assert normalize_text("Tuberculose Ósseá") == "tuberculose ossea"


def test_tokenize_drops_stopwords_and_short_tokens():
    assert tokenize_portuguese("manual de dengue no brasil") == [
        "manual",
        "dengue",
        "brasil",
    ]


def test_score_item_tiers():
    tokens = tokenize_portuguese("dengue")
    assert score_item(tokens, "dengue", "Dengue") == 1.0
    assert score_item(tokens, "dengue", "Manual de dengue grave") == 0.90
    assert score_item(tokens, "dengue", "Arboviroses", "dengue-chikungunya") == 0.90
    assert score_item(tokens, "dengue", "Malaria") == 0.0


def test_score_item_uses_extra_alias_field():
    tokens = tokenize_portuguese("dtha")
    assert score_item(tokens, "dtha", "Doencas de transmissao hidrica", "", "dtha") == 0.90


def test_derive_item_urls_appends_view_and_download():
    view, download = derive_item_urls("https://www.gov.br/saude/x/item")
    assert view == "https://www.gov.br/saude/x/item/view"
    assert download == "https://www.gov.br/saude/x/item/@@download/file"


def test_derive_item_urls_strips_existing_view_suffix():
    view, download = derive_item_urls("https://www.gov.br/saude/x/item/view")
    assert view == "https://www.gov.br/saude/x/item/view"
    assert download == "https://www.gov.br/saude/x/item/@@download/file"


def test_derive_item_urls_strips_fragments():
    view, dl = derive_item_urls("https://www.gov.br/saude/pt-br/manual.pdf#page=2")
    assert view == "https://www.gov.br/saude/pt-br/manual.pdf/view"
    assert dl == "https://www.gov.br/saude/pt-br/manual.pdf/@@download/file"


def test_is_login_redirect_detects_plone_gate():
    html = "<html><body><script>window.location='/acl_users/credentials_cookie_auth/require_login?came_from=x'</script></body></html>"
    assert is_login_redirect(html) is True
    assert is_login_redirect("<html><body>ok</body></html>") is False


from scholar_mcp.medical.govbr_common import parse_folder_index, parse_listing_page

LISTING_HTML = """
<html><body><div id="content-core">
  <article class="tileItem visualIEFloatFix tile-file">
    <h2 class="tileHeadline">
      <a class="summary url" href="https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue/dengue-diagnostico-e-manejo-clinico-adulto-e-crianca">
        Dengue: diagnóstico e manejo clínico: adulto e criança
      </a>
    </h2>
    <p class="tileBody"><span class="description">6ª edição, 2024.</span></p>
  </article>
  <article class="tileItem visualIEFloatFix tile-link">
    <h2 class="tileHeadline">
      <a class="summary url" href="https://www.gov.br/saude/pt-br/assuntos/saude-de-a-a-z/d/dengue">Página da Dengue</a>
    </h2>
  </article>
  <a href="/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue?b_start:int=20">Próximo</a>
</div></body></html>
"""


def test_parse_listing_page_keeps_only_tile_file_rows():
    items, _ = parse_listing_page(
        LISTING_HTML,
        "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue",
    )
    assert len(items) == 1
    item = items[0]
    assert item["slug"] == "dengue-diagnostico-e-manejo-clinico-adulto-e-crianca"
    assert item["title"] == "Dengue: diagnóstico e manejo clínico: adulto e criança"
    assert item["description"] == "6ª edição, 2024."
    assert item["download_url"].endswith("/@@download/file")
    assert item["view_url"].endswith("/view")


def test_parse_listing_page_returns_absolute_pagination_urls():
    _, next_urls = parse_listing_page(
        LISTING_HTML,
        "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue",
    )
    assert next_urls == [
        "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue?b_start:int=20"
    ]


def test_parse_listing_page_handles_encoded_b_start():
    html = '<a href="/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue?b_start%3Aint=40">2</a>'
    _, next_urls = parse_listing_page(
        html, "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue"
    )
    assert len(next_urls) == 1
    assert "b_start" in next_urls[0]


def test_parse_folder_index_returns_child_folders_only():
    html = """
    <div id="content-core">
      <a href="/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue">Dengue</a>
      <a href="/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/tuberculose/">Tuberculose</a>
      <a href="/saude/pt-br/centrais-de-conteudo/publicacoes/svsa">Voltar</a>
      <a href="/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue/um-arquivo/view">Arquivo</a>
      <a href="https://www.gov.br/outro">Externo</a>
    </div>
    """
    folders = parse_folder_index(
        html,
        "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa",
        "/saude/pt-br/centrais-de-conteudo/publicacoes/svsa",
    )
    assert folders == [
        "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/dengue",
        "https://www.gov.br/saude/pt-br/centrais-de-conteudo/publicacoes/svsa/tuberculose",
    ]
