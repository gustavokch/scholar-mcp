"""Shared primitives for the gov.br (Plone) scrapers.

Both the PCDT tree (``/assuntos/pcdt``) and the publication trees behind
"Saúde de A a Z" (``/centrais-de-conteudo/publicacoes/...``) are the same
Plone install, so headers, text folding, scoring, and URL derivation are
shared here rather than duplicated per engine.
"""

import re
import unicodedata

SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60  # 604,800 seconds

GOVBR_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9",
}

PORTUGUESE_STOPWORDS = frozenset(
    {
        "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do",
        "dos", "e", "em", "entre", "na", "nao", "nas", "no", "nos", "o",
        "os", "ou", "para", "pela", "pelo", "por", "que", "se", "sem",
        "sob", "sobre", "um", "uma", "umas", "uns",
    }
)

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_LOGIN_MARKER = "credentials_cookie_auth/require_login"


def normalize_text(text: str | None) -> str:
    """Normalize text by folding accents, stripping non-ASCII characters, and lowercasing."""
    if not text:
        return ""
    folded = (
        unicodedata.normalize("NFKD", text)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return folded.strip()


def tokenize_portuguese(text: str | None) -> list[str]:
    """Tokenize Portuguese text into substantive search terms."""
    norm = normalize_text(text)
    if not norm:
        return []
    return [
        tok
        for tok in _WORD_SPLIT_RE.split(norm)
        if len(tok) >= 2 and tok not in PORTUGUESE_STOPWORDS
    ]


def score_item(
    query_tokens: list[str],
    query_norm: str,
    primary: str,
    secondary: str = "",
    extra: str = "",
) -> float:
    """Score a catalog item against a query.

    ``primary`` is the title, ``secondary`` the slug (hyphens read as
    spaces), ``extra`` any alias text (A-Z abbreviations, topic names).
    Tiers: 1.0 exact, 0.90 substring, 0.80 all query tokens present,
    else 0.50 x token ratio.
    """
    if not query_tokens:
        return 0.0

    primary_norm = normalize_text(primary)
    secondary_norm = normalize_text(secondary).replace("-", " ")
    extra_norm = normalize_text(extra).replace("-", " ")

    if query_norm in {primary_norm, secondary_norm}:
        return 1.0

    # Prefix matches are a subset of substring matches, so a separate
    # startswith tier would be unreachable.
    for haystack in (primary_norm, secondary_norm, extra_norm):
        if haystack and query_norm in haystack:
            return 0.90

    item_tokens: set[str] = set()
    for field in (primary, secondary.replace("-", " "), extra.replace("-", " ")):
        item_tokens |= set(tokenize_portuguese(field))

    if not item_tokens:
        return 0.0

    matching_tokens = [tok for tok in query_tokens if tok in item_tokens]
    if not matching_tokens:
        return 0.0

    token_ratio = len(matching_tokens) / len(query_tokens)
    if token_ratio == 1.0:
        return 0.80
    return 0.50 * token_ratio


def derive_item_urls(href: str) -> tuple[str, str]:
    """Return ``(view_url, download_url)`` for a Plone item URL."""
    clean = href.strip().split("?")[0].rstrip("/")
    base = clean[: -len("/view")] if clean.endswith("/view") else clean
    return f"{base}/view", f"{base}/@@download/file"


def is_login_redirect(html: str) -> bool:
    """True when a 200 response is really Plone's login gate.

    Login-gated folders under gov.br answer 200 and redirect in the body,
    so status codes alone cannot detect them.
    """
    return _LOGIN_MARKER in (html or "")
