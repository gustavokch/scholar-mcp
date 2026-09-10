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
from scholar_mcp.medical.govbr_pcdt import GOVBR_HEADERS, GovBrPCDTEngine
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
    r"^https?://fi-admin\.bvsalud\.org/document/view/([A-Za-z0-9._-]+)(?:[/?#]|$)"
)

# The full-text fetch follows a URL taken from record content while the
# record id is caller-controlled. Without this allowlist the tool would act
# as a general-purpose request proxy.
FULLTEXT_ALLOWED_HOSTS = frozenset(
    {"fi-admin.bvsalud.org", "docs.bvsalud.org", "www.gov.br", "gov.br", "bvsms.saude.gov.br"}
)

MAX_RESULTS = 50
OVERFETCH_FACTOR = 3
MAX_PAGE_SIZE = 200
MAX_FULL_TEXT_CHARS = 50_000

BASE_FILTER = 'type:"non-conventional" AND la:"pt"'
BRISA_FILTER = 'db:"BRISA"'
VALID_COLLECTIONS = frozenset({"all", "brisa", "pcdt"})

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
        return [s for v in value if v and (s := str(v).strip())]
    if value:
        s = str(value).strip()
        return [s] if s else []
    return []


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
    the Portuguese name. A value carrying no ``^`` is an unencoded name and
    is matched against Brazil only -- scanning it for subfields would read
    "Espanha" as the ``^e`` subfield "spanha". Returns "" when neither form
    yields a recognised name.
    """
    for raw in _as_list(value):
        if "^" not in raw:
            if raw.strip().lower() == BRAZIL_COUNTRY.lower():
                return BRAZIL_COUNTRY
            continue
        for part in raw.split("^"):
            if part[:1] == "e" and part[1:].strip():
                return part[1:].strip()
    return ""


def _derive_fulltext_id(url: str) -> str:
    """Short slug of a fi-admin document view URL, or "" for any other URL."""
    match = FI_ADMIN_DOC_RE.match((url or "").strip())
    return match.group(1) if match else ""


def _sanitize_token(token: str) -> str:
    """Strip Solr query syntax from one user token.

    The BVS endpoint receives every filter composed into ``q``, so a token
    carrying Solr syntax (``"`` ``()[]`` ``^`` ``~`` ``*`` ``:`` ``\\`` ``/``
    ``+`` ``!``) or a leading ``+``/``-`` operator would corrupt the query
    rather than match text. Characters are removed, not escaped, because the
    endpoint's escaping rules differ from Solr's own.
    """
    return _SOLR_SPECIALS_RE.sub("", token).lstrip("+-")


_SOLR_SPECIALS_RE = re.compile(r'[\[\]{}()^"~*?:\\/+!&|]')

# Boolean words are composed by this module itself; a user token of "AND"
# would otherwise surface as ``AND AND AND`` in the composed query.
_SOLR_BOOLEAN_WORDS = frozenset({"and", "or", "not", "to"})


def _usable_tokens(query: str) -> list[str]:
    """User tokens that survive sanitization, in order.

    A token reduced to nothing by ``_sanitize_token``, or reserved as a
    boolean word, contributes no matching text and is dropped.
    """
    return [
        cleaned
        for token in (query or "").split()
        if (cleaned := _sanitize_token(token))
        and cleaned.lower() not in _SOLR_BOOLEAN_WORDS
    ]


def _build_query(query: str, collection: str) -> str:
    """Compose every filter into ``q``.

    ``fq`` is silently ignored by this API, and the default operator is OR,
    so user tokens are explicitly ANDed inside their own group.
    """
    clauses = [BASE_FILTER]
    if collection == "brisa":
        clauses.append(BRISA_FILTER)
    tokens = _usable_tokens(query)
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


def _select_document_url(doc: dict[str, Any]) -> str:
    """Select the best candidate document URL from multi-valued Solr ``ur``.

    Prioritizes fi-admin document view URLs and allowed repository hosts over
    generic portal links or off-site resources.
    """
    urls = _as_list(doc.get("ur"))
    if not urls:
        return ""
    for u in urls:
        if _derive_fulltext_id(u):
            return u
    for u in urls:
        if _is_allowed_host(u):
            return u
    return urls[0]


def _build_record(doc: dict[str, Any]) -> BrazilGuideline:
    """Map one Solr document onto a BrazilGuideline."""
    document_url = _select_document_url(doc)
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


def is_allowed_bvs_host(url: str) -> bool:
    """True only for the BVS and Brazilian MoH hosts this module is permitted to fetch."""
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return (
        host in FULLTEXT_ALLOWED_HOSTS
        or host == "gov.br"
        or host.endswith(".gov.br")
    )


is_allowed_host = is_allowed_bvs_host
_is_allowed_host = is_allowed_bvs_host


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
        record_id = _first(doc.get("id"))
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
        self.pcdt_engine = GovBrPCDTEngine(http_client, cache, settings)

    async def search_guidelines(
        self,
        query: str,
        limit: int = 10,
        collection: str = "all",
    ) -> tuple[list[BrazilGuideline], CacheMetadata]:
        norm_collection = (collection or "all").strip().lower()
        if norm_collection not in VALID_COLLECTIONS:
            logger.warning("unknown brazil_moh collection %r", collection)
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        clamped = min(max(1, limit), MAX_RESULTS)

        if norm_collection == "pcdt":
            return await self.pcdt_engine.search(query, limit=clamped)

        # A blank query deliberately browses the collection. A query that
        # carries text but sanitizes away to nothing is different: composing
        # filters alone would return arbitrary top-of-index documents dressed
        # as matches for terms that were never searched.
        if (query or "").strip() and not _usable_tokens(query):
            logger.info("brazil_moh query %r has no searchable tokens", query)
            return [], CacheMetadata(cached=False, cache_age=0, error=False)

        # Keyed on the composed query, not the raw one: "dengue" and
        # "  dengue  " compose identically and must share one cache row.
        composed = _build_query(query, norm_collection)
        cache_key = f"brazil_moh_search:{norm_collection}:{clamped}:{composed}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [BrazilGuideline.from_dict(item) for item in cached_data], meta

        # Query PCDT engine first
        pcdt_records, pcdt_meta = await self.pcdt_engine.search(query, limit=clamped)

        count = min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)
        resp = await self.http_client.get(
            BVS_SEARCH_URL,
            headers=BVS_HEADERS,
            params={
                "q": composed,
                "output": "json",
                "count": count,
            },
        )
        if resp is None:
            if pcdt_records:
                return pcdt_records, pcdt_meta
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        try:
            data = resp.json()
        except ValueError:
            logger.warning("brazil_moh search returned non-JSON payload")
            if pcdt_records:
                return pcdt_records, pcdt_meta
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        bvs_records = [_build_record(doc) for doc in _dedupe_by_id(_extract_docs(data))]
        bvs_records = [record for record in bvs_records if _is_brazilian(record)]

        # Merge PCDT records (first) and BVS records, deduplicating by record_id
        seen_ids: set[str] = set()
        merged_records: list[BrazilGuideline] = []
        for r in pcdt_records + bvs_records:
            if r.record_id and r.record_id in seen_ids:
                continue
            if r.record_id:
                seen_ids.add(r.record_id)
            merged_records.append(r)

        records = merged_records[:clamped]

        await self.cache.set(
            cache_key,
            [record.to_dict() for record in records],
            source="brazil_moh",
        )
        return records, CacheMetadata(cached=False, cache_age=0, error=False)

    async def _lookup_record(self, record_id: str) -> tuple[BrazilGuideline | None, bool]:
        """Resolve one record by its Solr id. Returns (record, errored)."""
        # The id is caller-controlled; escape it so a quote or backslash
        # cannot terminate the id:"..." phrase and rewrite the query.
        escaped = record_id.replace("\\", "\\\\").replace('"', '\\"')
        resp = await self.http_client.get(
            BVS_SEARCH_URL,
            headers=BVS_HEADERS,
            params={"q": f'id:"{escaped}"', "output": "json", "count": 5},
        )
        if resp is None:
            return None, True
        try:
            data = resp.json()
        except ValueError:
            return None, True
        # ``id:"..."`` is a phrase query against a tokenized field, so a
        # near-miss record can come back ahead of the requested one. Only an
        # exact id is accepted: the wrong record would otherwise be served and
        # cached under the caller's id for the full TTL.
        docs = [
            doc
            for doc in _dedupe_by_id(_extract_docs(data))
            if _first(doc.get("id")) == record_id
        ]
        if not docs:
            return None, False
        return _build_record(docs[0]), False

    async def _extract_pdf_text(self, document_url: str) -> tuple[str, bool]:
        """Fetch and extract the document PDF. Returns (text, errored).

        Gates on content-type explicitly. ``get_bytes`` is not used here:
        its HTML guard only catches Cloudflare-style challenge pages, so a
        plain "Estamos em manutenção" WAF page would reach the PDF parser.

        Redirects are followed (the BVS hosts hand off between themselves),
        but the response's final URL is re-checked against the allowlist so
        a redirect cannot carry the fetch off-host.
        """
        if not _is_allowed_host(document_url):
            return "", False
        headers = GOVBR_HEADERS if "gov.br" in document_url else BVS_HEADERS
        resp = await self.http_client.get(document_url, headers=headers)
        if resp is None:
            return "", True
        if not _is_allowed_host(str(resp.url)):
            logger.info(
                "brazil_moh full text redirected off the allowed hosts (%s)",
                str(resp.url),
            )
            return "", True
        content_type = resp.headers.get("content-type", "").lower()
        if "application/pdf" not in content_type:
            logger.info(
                "brazil_moh full text is not a PDF (content-type=%r)", content_type
            )
            return "", True
        try:
            # Bounded before it is cached: an unbounded extraction would write
            # a multi-megabyte row into the shared cache for a long manual.
            return pdf_bytes_to_text(resp.content)[:MAX_FULL_TEXT_CHARS], False
        except Exception as exc:
            logger.warning("brazil_moh PDF extraction failed: %s", exc)
            return "", True

    async def get_full_text(
        self,
        record_id: str,
        max_chars: int | None = None,
    ) -> tuple[dict[str, Any], CacheMetadata]:
        normalized = (record_id or "").strip()
        base = {
            "source": "brazil-moh",
            "record_id": normalized,
            "document_url": "",
            "truncated": False,
        }
        if not normalized:
            return (
                {**base, "status": "error", "error": "record_id is required",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=True),
            )

        cache_key = f"brazil_moh_fulltext:{normalized}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return self._serve_full_text(cached_data, max_chars), meta

        pcdt_record = await self.pcdt_engine.get_guideline(normalized)
        if pcdt_record is not None:
            record = pcdt_record
            errored = False
        else:
            record, errored = await self._lookup_record(normalized)

        if errored:
            return (
                {**base, "status": "error", "error": "bvs request failed",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=True),
            )
        if record is None:
            return (
                {**base, "status": "not_found", "error": "no record for id",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=False),
            )

        base["document_url"] = record.document_url
        pdf_text, errored = await self._extract_pdf_text(record.document_url)

        if pdf_text:
            result = {"content_type": "pdf", "content": pdf_text}
        elif record.abstract:
            result = {"content_type": "abstract", "content": record.abstract}
        else:
            # A transient fetch failure with nothing to fall back on is an
            # error, not an absence: callers must retry, not move on.
            status = "error" if errored else "not_found"
            error_text = (
                "full text fetch failed and no abstract available"
                if errored
                else "no full text or abstract available"
            )
            return (
                {**base, "status": status,
                 "error": error_text,
                 "title": record.title, "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=errored),
            )

        payload = {**base, "status": "success", "title": record.title, **result}
        # An errored payload is never cached: a transient block must not
        # poison a 30-day TTL.
        if not errored:
            await self.cache.set(cache_key, payload, source="brazil_moh")
        return (
            self._serve_full_text(payload, max_chars),
            CacheMetadata(cached=False, cache_age=0, error=errored),
        )

    @staticmethod
    def _serve_full_text(payload: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
        # ``max_chars`` is caller-supplied and is bounded on both sides:
        # MAX_FULL_TEXT_CHARS is the ceiling the tool documents, so a large
        # value must not return an entire manual in one response.
        limit = (
            MAX_FULL_TEXT_CHARS
            if max_chars is None
            else min(max(1, max_chars), MAX_FULL_TEXT_CHARS)
        )
        content, truncated = truncate_content(payload.get("content", ""), limit)
        return {**payload, "content": content, "truncated": truncated}
