"""Shared primitives for the gov.br (Plone) scrapers.

Both the PCDT tree (``/assuntos/pcdt``) and the publication trees behind
"Saúde de A a Z" (``/centrais-de-conteudo/publicacoes/...``) are the same
Plone install, so headers, text folding, scoring, and URL derivation are
shared here rather than duplicated per engine.
"""

import re
import unicodedata
import urllib.parse

from bs4 import BeautifulSoup

SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60  # 604,800 seconds

# A crawl that returns less than this fraction of the catalog already in
# hand is treated as a parser break, not as a smaller site, and is never
# accepted as complete.
MIN_CATALOG_RETENTION = 0.5

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

# Catalog slugs keep their file extension because ``record_id`` derives from
# them ("govbr-guias-2001-manual-de-calibracao-de-examinadores.pdf"), so the
# extension would otherwise leak into scoring as a literal term: the query
# "pdf" scored 392 of the 569 bundled rows.
_DOCUMENT_EXTENSIONS = frozenset(
    {"pdf", "doc", "docx", "odt", "xls", "xlsx", "ods", "ppt", "pptx", "zip"}
)


def strip_document_extension(text: str) -> str:
    """Drop a trailing document file extension from ``text``.

    Only a known document extension is removed, so version suffixes such as
    "protocolo-v1.2" survive.
    """
    head, sep, tail = text.rpartition(".")
    if sep and tail.lower() in _DOCUMENT_EXTENSIONS:
        return head
    return text


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

    # The slug carries the source filename, extension included. That
    # extension is an artifact of the CMS, not a search term.
    secondary = strip_document_extension(secondary)

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
    clean = href.strip().split("#")[0].split("?")[0].rstrip("/")
    base = clean[: -len("/view")] if clean.endswith("/view") else clean
    return f"{base}/view", f"{base}/@@download/file"


def is_login_redirect(html: str) -> bool:
    """True when a 200 response is really Plone's login gate.

    Login-gated folders under gov.br answer 200 and redirect in the body,
    so status codes alone cannot detect them.
    """
    return _LOGIN_MARKER in (html or "")


_B_START_RE = re.compile(r"b_start(?::|%3A)int=(\d+)")


def parse_listing_page(html: str, base_url: str) -> tuple[list[dict[str, str]], list[str]]:
    """Parse one Plone folder listing page.

    Only ``tile-file`` rows are harvested: ``tile-link`` rows point at
    content pages whose ``@@download/file`` is a 404 and whose ``/view``
    redirects into the login-gated tree.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict[str, str]] = []
    seen_slugs: set[str] = set()

    for article in soup.find_all("article"):
        classes = article.get("class") or []
        if "tile-file" not in classes:
            continue
        anchor = article.find("a", class_="summary", href=True)
        if anchor is None:
            anchor = article.find("a", href=True)
        if anchor is None:
            continue
        title = anchor.get_text(strip=True)
        if not title:
            continue
        href = urllib.parse.urljoin(base_url, anchor["href"].strip())
        view_url, download_url = derive_item_urls(href)
        slug = view_url[: -len("/view")].rstrip("/").rsplit("/", 1)[-1]
        if not slug or slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        description_node = article.find("span", class_="description")
        items.append(
            {
                "slug": slug,
                "title": title,
                "description": (
                    description_node.get_text(strip=True) if description_node else ""
                ),
                "view_url": view_url,
                "download_url": download_url,
            }
        )

    next_urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not _B_START_RE.search(href):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        if absolute not in next_urls:
            next_urls.append(absolute)

    return items, next_urls


def parse_folder_index(html: str, base_url: str, parent_path: str) -> list[str]:
    """Return absolute URLs of the immediate child folders of ``parent_path``.

    Folder lists (A-Z letters, publication years, SVSA topics) change over
    time, so they are discovered rather than hardcoded.
    """
    soup = BeautifulSoup(html, "html.parser")
    prefix = parent_path.rstrip("/") + "/"
    folders: list[str] = []
    for a in soup.find_all("a", href=True):
        absolute = urllib.parse.urljoin(base_url, a["href"].strip()).split("?")[0]
        parsed = urllib.parse.urlparse(absolute)
        path = parsed.path.rstrip("/")
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix) :]
        if not remainder or "/" in remainder:
            continue
        normalized = f"{parsed.scheme}://{parsed.netloc}{path}"
        if normalized not in folders:
            folders.append(normalized)
    return folders


# Bumped whenever a cached gov.br search row's shape changes. Both engines
# store BrazilGuideline.to_dict() rows and read them back through
# from_dict(), which fills a missing field with its default rather than
# failing -- so an un-bumped key serves a pre-change row as if current:
# has_full_text silently False, for the full cache_ttl_brazil_moh (30 days).
# Declared here rather than reused from brazil_moh.CACHE_SCHEMA because
# brazil_moh imports both engines; the reverse import would be a cycle.
# v2: has_full_text (ENAMED misses plan B4).
CACHE_SCHEMA = "v2"
