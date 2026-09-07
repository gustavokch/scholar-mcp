"""Brazilian Ministry of Health technical publications via BVS/iAHx.

Discovery uses the BVS portal search API. Several of its behaviours are
counter-intuitive and are load-bearing for this module:

* ``fq`` is silently ignored, so every filter is composed into ``q``.
* The default boolean operator is OR, so user tokens are joined with AND.
* ``pais_publicacao`` is subfield-encoded and is neither exact-matchable
  nor wildcard-searchable, so Brazil scoping is ``la:"pt"`` server-side
  plus a client-side assertion on the parsed country.
* Records are duplicated across indexing collections at roughly 2.1-2.3x,
  so the engine over-fetches and trims after deduplication.

Results are served in the order BVS returns them. No re-ranking is applied:
``ScoringEngine`` does not fold accents or strip Portuguese stopwords, so
blending it against a Solr ordering tuned for this corpus would degrade it.
``BrazilGuideline.score`` is the seam for adding that later.
"""

import logging
import re
import urllib.parse
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import BrazilGuideline
from scholar_mcp.parsers.pdf import pdf_bytes_to_text
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager
from scholar_mcp.utils.text import truncate_content

BVS_SEARCH_URL = "https://pesquisa.bvsalud.org/portal/"

# The repo default User-Agent receives HTTP 403 from this host.
BVS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

FI_ADMIN_DOC_RE = re.compile(
    r"^https?://fi-admin\.bvsalud\.org/document/view/([A-Za-z0-9._-]+)/?$"
)

# The full-text fetch follows a URL taken from record content while the
# record id is caller-controlled. Without this allowlist the tool would act
# as a general-purpose request proxy.
FULLTEXT_ALLOWED_HOSTS = frozenset({"fi-admin.bvsalud.org", "docs.bvsalud.org"})

MAX_RESULTS = 50
OVERFETCH_FACTOR = 3
MAX_PAGE_SIZE = 200
MAX_FULL_TEXT_CHARS = 50_000

BASE_FILTER = 'type:"non-conventional" AND la:"pt"'
BRISA_FILTER = 'db:"BRISA"'
VALID_COLLECTIONS = frozenset({"all", "brisa"})

BRAZIL_COUNTRY = "Brasil"

logger = logging.getLogger(__name__)


def _first(value: Any) -> str:
    """First element of a multi-valued Solr field, or the scalar, as a string."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def _as_list(value: Any) -> list[str]:
    """Every non-empty value of a Solr field as a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def _parse_issued(value: Any) -> tuple[str, str]:
    """Split a Solr ``da`` value ("202609") into ("2026", "2026-09").

    Returns ("", "") when the value is absent or not year-shaped.
    """
    raw = _first(value).strip()
    if len(raw) >= 6 and raw[:6].isdigit():
        return raw[:4], f"{raw[:4]}-{raw[4:6]}"
    if len(raw) >= 4 and raw[:4].isdigit():
        return raw[:4], raw[:4]
    return "", ""


def _parse_country(value: Any) -> str:
    """Read the ``^e`` subfield out of a ``pais_publicacao`` value.

    The field is encoded as "^iBrazil^eBrasil^pBrasil^fBrésil"; ``^e`` holds
    the Portuguese name. Returns "" when no ``^e`` subfield is present.
    """
    for raw in _as_list(value):
        for part in raw.split("^"):
            if part[:1] == "e" and part[1:].strip():
                return part[1:].strip()
    return ""


def _derive_fulltext_id(url: str) -> str:
    """Short slug of a fi-admin document view URL, or "" for any other URL."""
    match = FI_ADMIN_DOC_RE.match(url or "")
    return match.group(1) if match else ""


def _build_query(query: str, collection: str) -> str:
    """Compose every filter into ``q``.

    ``fq`` is silently ignored by this API, and the default operator is OR,
    so user tokens are explicitly ANDed inside their own group.
    """
    clauses = [BASE_FILTER]
    if collection == "brisa":
        clauses.append(BRISA_FILTER)
    tokens = [token for token in (query or "").split() if token]
    if tokens:
        clauses.append("(" + " AND ".join(tokens) + ")")
    return " AND ".join(clauses)


def _extract_docs(data: Any) -> list[dict[str, Any]]:
    """Pull the document list out of the nested BVS envelope."""
    if not isinstance(data, dict):
        return []
    responses = data.get("diaServerResponse") or []
    if not isinstance(responses, list) or not responses:
        return []
    first = responses[0]
    if not isinstance(first, dict):
        return []
    response = first.get("response") or {}
    docs = response.get("docs") or []
    return [doc for doc in docs if isinstance(doc, dict)]


def _build_record(doc: dict[str, Any]) -> BrazilGuideline:
    """Map one Solr document onto a BrazilGuideline."""
    document_url = _first(doc.get("ur"))
    year, issued = _parse_issued(doc.get("da"))
    return BrazilGuideline(
        title=_first(doc.get("ti")),
        title_en=_first(doc.get("ti_en")),
        record_id=_first(doc.get("id")),
        document_url=document_url,
        fulltext_id=_derive_fulltext_id(document_url),
        abstract=_first(doc.get("ab")),
        year=year,
        issued=issued,
        country=_parse_country(doc.get("pais_publicacao")),
        authors=_as_list(doc.get("au")),
        languages=_as_list(doc.get("la")),
        collections=_as_list(doc.get("db")),
        mesh_subjects=_as_list(doc.get("mh")),
    )


def _is_brazilian(record: BrazilGuideline) -> bool:
    """Client-side Brazil assertion.

    ``pais_publicacao`` cannot be filtered server-side, so ``la:"pt"``
    narrows the pool and this drops the Portuguese-language records
    published elsewhere.
    """
    return record.country == BRAZIL_COUNTRY


def _dedupe_by_id(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exact deduplication on the Solr ``id``, keeping first occurrence.

    BVS emits one row per indexing collection, inflating results by roughly
    2.1-2.3x. Exact-key dedup is deliberate: the fuzzy title matching in
    utils/deduplication.py would collapse genuinely distinct guidelines with
    similar titles.
    """
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for doc in docs:
        record_id = str(doc.get("id") or "")
        if record_id:
            if record_id in seen:
                continue
            seen.add(record_id)
        unique.append(doc)
    return unique


class BrazilMoHEngine:
    """Search and full-text retrieval for Brazilian MoH publications."""

    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    async def search_guidelines(
        self,
        query: str,
        limit: int = 10,
        collection: str = "all",
    ) -> tuple[list[BrazilGuideline], CacheMetadata]:
        if collection not in VALID_COLLECTIONS:
            logger.warning("unknown brazil_moh collection %r", collection)
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        clamped = min(max(1, limit), MAX_RESULTS)
        cache_key = f"brazil_moh_search:{collection}:{clamped}:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [BrazilGuideline.from_dict(item) for item in cached_data], meta

        count = min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)
        resp = await self.http_client.get(
            BVS_SEARCH_URL,
            headers=BVS_HEADERS,
            params={
                "q": _build_query(query, collection),
                "output": "json",
                "count": count,
            },
        )
        if resp is None:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        try:
            data = resp.json()
        except ValueError:
            logger.warning("brazil_moh search returned non-JSON payload")
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        records = [_build_record(doc) for doc in _dedupe_by_id(_extract_docs(data))]
        records = [record for record in records if _is_brazilian(record)][:clamped]

        await self.cache.set(
            cache_key,
            [record.to_dict() for record in records],
            source="brazil_moh",
        )
        return records, CacheMetadata(cached=False, cache_age=0, error=False)


