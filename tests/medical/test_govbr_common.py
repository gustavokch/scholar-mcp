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


def test_is_login_redirect_detects_plone_gate():
    html = "<html><body><script>window.location='/acl_users/credentials_cookie_auth/require_login?came_from=x'</script></body></html>"
    assert is_login_redirect(html) is True
    assert is_login_redirect("<html><body>ok</body></html>") is False
