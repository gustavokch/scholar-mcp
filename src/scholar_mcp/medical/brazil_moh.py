"""Brazilian Ministry of Health technical publications via BVS/iAHx.

Discovery uses the BVS portal search API. Several of its behaviours are
counter-intuitive and are load-bearing for this module:

* ``fq`` is silently ignored, so every filter is composed into ``q``.
* The default boolean operator is OR, so user tokens carry an explicit
  operator inside their own group.
* ``pais_publicacao`` is subfield-encoded and is neither exact-matchable
  nor wildcard-searchable, so Brazil scoping is ``la:"pt"`` server-side
  plus a client-side assertion on the parsed country.
* Records are duplicated across indexing collections at roughly 2.1-2.3x,
  so the engine over-fetches and trims after deduplication.
* The index does not strip Portuguese stopwords, so ``_usable_tokens``
  does. Measured, ``(da)`` alone matches 25,635 records.
* ``count=0`` returns HTTP 500 rather than a count-only response, and the
  host is unreliable enough that the error paths here are live.

Search runs as a stage chain. A title-scoped stage ANDs the user tokens in
the ``ti:`` field; when it returns no Brazilian records, progressive
title-token relaxation drops trailing tokens right-to-left and retries,
because scenario queries carry clinical descriptors that formal document
titles rarely contain. A relaxed step that errors halts the chain: the
endpoint is already misbehaving, so further variants likely fail the same
way. When the ladder is exhausted without a hit, an all-field stage ANDs
the same tokens; if it also yields nothing and two or more substantive
tokens remain, the same tokens are retried ORed. Relaxation loosens the
field scope or the operator and nothing else -- every stage composes from
one stopword-stripped token list, so a relaxed hit is never one the strict
stage structurally could not have matched.

Results are then re-ranked by ``rank_brazil_guidelines``. Accent folding
and Portuguese stopword stripping are what made that viable: without
them, blending a generic scorer against a Solr ordering tuned for this
corpus degraded it. The BVS ordering is not discarded but retained as a
weighted position prior, because a relaxed ``OR`` pool is far larger than
the over-fetch window and the server still chooses which slice we see.
"""

import asyncio
import json
import logging
import re
import time
import urllib.parse
from typing import Any, Literal

from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.govbr_pcdt import GOVBR_HEADERS, GovBrPCDTEngine
from scholar_mcp.medical.models import BrazilGuideline
from scholar_mcp.medical.ranking import (
    PORTUGUESE_STOPWORDS,
    normalize_portuguese,
    rank_brazil_guidelines,
)
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

# Camoufox (anti-detection Firefox) fetches the JSON search payload with the
# browser fingerprint the CDN shield accepts. Mirrors the pediatrics scraper:
# one navigation, hard total ceiling so a hung browser cannot outlive the
# caller's own timeout. The total ceiling comes from
# ``settings.brazil_browser_timeout_s``.
_CAMOUFOX_NAV_TIMEOUT_MS = 15000

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


_FIELD_PREFIX_RE = re.compile(r"^(?:[a-zA-Z_]+:)+")
_SOLR_SPECIALS_RE = re.compile(r'[\[\]{}()^"~*?:\\/+!&|]')

# Boolean words are composed by this module itself; a user token of "AND"
# would otherwise surface as ``AND AND AND`` in the composed query.
_SOLR_BOOLEAN_WORDS = frozenset({"and", "or", "not", "to"})


def _sanitize_token(token: str) -> str:
    """Strip Solr query syntax from one user token.

    The BVS endpoint receives every filter composed into ``q``, so a token
    carrying Solr syntax (``"`` ``()[]`` ``^`` ``~`` ``*`` ``:`` ``\\`` ``/``
    ``+`` ``!``) or a leading ``+``/``-`` operator would corrupt the query
    rather than match text. Characters are removed, not escaped, because the
    endpoint's escaping rules differ from Solr's own.

    Caller-supplied field prefixes (e.g. ``ti:dengue``) are stripped before
    special character removal so colon removal does not concatenate them into
    ``tidengue``. Field scoping is the engine's decision, not the caller's.
    """
    stripped = _FIELD_PREFIX_RE.sub("", token.lstrip("+-"))
    return _SOLR_SPECIALS_RE.sub("", stripped).lstrip("+-")


def _usable_tokens(query: str) -> list[str]:
    """User tokens that survive sanitization, in order.

    A token reduced to nothing by ``_sanitize_token``, reserved as a boolean
    word, or serving only as a Portuguese function word contributes no useful
    matching text and is dropped.

    Portuguese stopwords are stripped because this index does not strip them
    itself: measured against the live endpoint, ``(da)`` alone matches 25,635
    records and ``(a)`` 24,779. Left in, a stopword inflates a relaxed ``OR``
    pool by roughly 10x (2,538 -> 25,759 for one added token) against an
    over-fetch of at most ``MAX_PAGE_SIZE`` documents, so the topical matches
    never get retrieved. In the strict ``AND`` path the effect is narrower --
    the token is near-universal in Portuguese prose, so it usually changes
    nothing -- but it still discards short-title records carrying no abstract,
    which is the class this module most wants to surface.

    Stripping happens here rather than in ``_build_query`` so that both
    operators and the relaxation gate see one identical token list. Stripping
    in only one stage would let the relaxed query match records the strict
    query never could, for a reason unrelated to the operator.

    Membership is tested on the accent-folded form while the original token is
    emitted: ``PORTUGUESE_STOPWORDS`` is stored folded, so the raw token "à"
    would otherwise escape the set, and BVS handles Portuguese diacritics
    natively so there is no reason to strip them from the query.

    The ``len >= 2`` floor used by ``tokenize_portuguese`` is deliberately not
    applied. A single character that is not a Portuguese word is selective
    here -- ``hepatite AND b`` retains 71% of bare ``hepatite`` -- while the
    single-character function words are already covered by the stopword set.
    """
    tokens: list[str] = []
    for token in (query or "").split():
        cleaned = _sanitize_token(token)
        if not cleaned:
            continue
        folded = normalize_portuguese(cleaned)
        if folded in _SOLR_BOOLEAN_WORDS or folded in PORTUGUESE_STOPWORDS:
            continue
        tokens.append(cleaned)
    return tokens


MAX_TITLE_RELAXATION_STEPS = 3


def _title_token_relaxations(
    tokens: list[str],
    max_steps: int = MAX_TITLE_RELAXATION_STEPS,
    min_tokens: int = 1,
) -> list[list[str]]:
    """Ladder of progressively relaxed token subsets for title-scoped search.

    When a full conjunction of title tokens returns no documents, trailing
    tokens are dropped right-to-left. Trailing tokens in scenario queries
    represent specific clinical criteria or modalities (e.g. 'parenteral',
    'observacao') that rarely appear in formal document titles.

    Relaxation stops when ``max_steps`` is reached or the token list length
    would drop below ``min_tokens``.
    """
    ladder: list[list[str]] = []
    current = list(tokens)
    for _ in range(max_steps):
        if len(current) <= min_tokens:
            break
        current = current[:-1]
        ladder.append(current)
    return ladder


def _build_query(
    query: str,
    collection: str,
    operator: Literal["AND", "OR"] = "AND",
    title_scoped: bool = False,
    tokens: list[str] | None = None,
) -> str:
    """Compose every filter into ``q``.

    ``fq`` is silently ignored by this API, and the default operator is OR, so
    the user tokens get an explicit operator inside their own group.
    When ``title_scoped`` is True, each user token is scoped to the ``ti:``
    field so title matching takes precedence over full-text matches.

    ``operator`` relaxes only that group. ``BASE_FILTER`` and ``BRISA_FILTER``
    stay conjunctive regardless: an ``OR`` across them would match
    conventional and non-Portuguese literature.

    ``tokens`` overrides the sanitized user tokens, so the progressive
    title-relaxation ladder composes through this one function and a filter
    added here cannot drift out of the relaxed stages.
    """
    clauses = [BASE_FILTER]
    if collection == "brisa":
        clauses.append(BRISA_FILTER)
    if tokens is None:
        tokens = _usable_tokens(query)
    if tokens:
        if title_scoped:
            token_clause = f" {operator} ".join(f"ti:{t}" for t in tokens)
        else:
            token_clause = f" {operator} ".join(tokens)
        clauses.append(f"({token_clause})")
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
    # The *.gov.br catch-all is deliberate: PCDT PDFs are served from
    # www.gov.br, bvsms.saude.gov.br, and static asset hosts that change
    # without notice. gov.br is a state-run registry, so the catch-all
    # stays inside Brazilian government infrastructure. Lookalike hosts
    # (gov.br.evil.com) fail the endswith check; the boundary is pinned
    # by test_is_allowed_host_accepts_bvs_hosts_only.
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

    async def _fetch_records(
        self,
        composed: str,
        count: int,
    ) -> tuple[list[BrazilGuideline], bool]:
        """One BVS search request, parsed, deduplicated and Brazil-filtered.

        Returns ``(records, errored)``. Extracted so the strict and relaxed
        stages cannot drift apart in how they parse or filter.
        """
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
            return [], True

        try:
            data = resp.json()
        except ValueError:
            logger.warning("brazil_moh search returned non-JSON payload")
            return [], True

        records = [_build_record(doc) for doc in _dedupe_by_id(_extract_docs(data))]
        return [record for record in records if _is_brazilian(record)], False

    async def _stage(self, stage: str, coro: Any, default: Any) -> Any:
        """Run one retrieval stage under its own time budget.

        Callers wrap the whole ``search_guidelines`` chain in a hard ceiling
        (zimqa's per-call ``wait_for``). A stage stalled behind a throttled
        rate limiter must die here so the remaining stages — or the PCDT
        fallback — still get their share of that ceiling, and so the chain
        degrades to partial results instead of a cancelled coroutine.

        ``brazil_stage_timeout_s <= 0`` disables the budget. On expiry the
        stage counts as errored and ``default`` is returned.
        """
        budget = float(getattr(self.settings, "brazil_stage_timeout_s", 0.0) or 0.0)
        if budget <= 0:
            return await coro
        try:
            return await asyncio.wait_for(coro, budget)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "brazil_moh %s stage exceeded its %.1fs budget", stage, budget
            )
            return default

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
        # as matches for terms that were never searched. Stopword stripping
        # widens this: "sobre a" now lands here rather than being searched.
        tokens = _usable_tokens(query)
        if (query or "").strip() and not tokens:
            logger.info("brazil_moh query %r has no searchable tokens", query)
            return [], CacheMetadata(cached=False, cache_age=0, error=False)

        # Keyed on the composed strict title-scoped query, not the raw one:
        # "dengue" and "  dengue  " compose identically and must share one
        # cache row. The fallback and relaxed queries never enter the key --
        # they derive from the same user query, so one user query keeps one row.
        title_composed = _build_query(query, norm_collection, operator="AND", title_scoped=True)
        cache_key = f"brazil_moh_search:{norm_collection}:{clamped}:{title_composed}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [BrazilGuideline.from_dict(item) for item in cached_data], meta

        # Query PCDT engine first. Every stage runs under its own budget, so
        # one stalled stage costs its budget and the chain moves on. The chain
        # start anchors the browser tier's share of the chain budget (Task 3:
        # the whole chain, not each stage, is what the caller's ceiling
        # bounds).
        chain_start = time.monotonic()
        stage_error_meta = CacheMetadata(cached=False, cache_age=0, error=True)
        pcdt_records, pcdt_meta = await self._stage(
            "pcdt",
            self.pcdt_engine.search(query, limit=clamped),
            ([], stage_error_meta),
        )
        errored_any = pcdt_meta.error
        # The browser tier answers BVS failures (the CDN shield 403s plain
        # HTTP clients); a PCDT outage with a healthy BVS must not launch a
        # real browser. Track BVS errors on their own flag.
        bvs_errored = False

        count = min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)
        # The all-field query is built once and shared by the all-field
        # fallback stage and the browser tier, which need the identical
        # composition. Both consumers only run when `tokens` is non-empty.
        all_composed: str | None = (
            _build_query(query, norm_collection, operator="AND", title_scoped=False)
            if tokens
            else None
        )
        records, errored = await self._stage(
            "title-scoped", self._fetch_records(title_composed, count), ([], True)
        )
        errored_any = errored_any or errored
        bvs_errored = bvs_errored or errored

        # Progressive title-token relaxation: when the full-token title AND
        # returns zero records without error, drop trailing tokens and retry.
        # Scenario queries frequently contain clinical descriptors ('grupo',
        # 'criterios', 'hidratacao') that do not appear in formal manual titles.
        # An errored relaxation step halts the whole BVS chain: the endpoint is
        # already misbehaving, so further variants likely fail the same way.
        title_relaxed_errored = False
        if not records and not errored and tokens:
            for relaxed_tokens in _title_token_relaxations(tokens):
                relaxed_title_composed = _build_query(
                    query, norm_collection, title_scoped=True, tokens=relaxed_tokens
                )

                relaxed_title_records, relaxed_title_errored = await self._stage(
                    "title-scoped-relaxed",
                    self._fetch_records(relaxed_title_composed, count),
                    ([], True),
                )
                errored_any = errored_any or relaxed_title_errored
                bvs_errored = bvs_errored or relaxed_title_errored
                if relaxed_title_errored:
                    title_relaxed_errored = True
                    break
                if relaxed_title_records:
                    records = relaxed_title_records
                    break

        # Fall back to all-field query when the title-scoped stage yields no
        # Brazilian records — including when it stalled, since a slow strict
        # query says nothing about the relaxed one.
        if not records and tokens and not title_relaxed_errored:
            fallback_records, fallback_errored = await self._stage(
                "all-field", self._fetch_records(all_composed, count), ([], True)
            )
            errored_any = errored_any or fallback_errored
            bvs_errored = bvs_errored or fallback_errored
            records = fallback_records

        # The strict conjunction found nothing usable -- either no hits at all,
        # or only records the Brazil assertion dropped. Retry the same tokens
        # ORed. A single substantive token is skipped: the two groups would be
        # byte-identical, so the request would be pure waste.
        if not records and len(tokens) >= 2 and not title_relaxed_errored:
            composed_relaxed = _build_query(query, norm_collection, operator="OR", title_scoped=False)
            relaxed_records, relaxed_errored = await self._stage(
                "relaxed", self._fetch_records(composed_relaxed, count), ([], True)
            )
            errored_any = errored_any or relaxed_errored
            bvs_errored = bvs_errored or relaxed_errored
            records = relaxed_records

        # Every BVS HTTP stage errored (the CDN shield 403s every request) and
        # the PCDT engine alone cannot cover the non-conventional index. One
        # rendered browser fetch carries the fingerprint the shield accepts;
        # success clears errored_any so the merged result is cached.
        if (
            not records
            and bvs_errored
            and tokens
            and self.settings.enable_browser_fallback
            and self.settings.brazil_browser_fallback
        ):
            docs = await self._camoufox_search(
                all_composed, count, ceiling=self._browser_ceiling(chain_start)
            )
            if docs:
                browser_records = [
                    r
                    for r in (_build_record(d) for d in _dedupe_by_id(docs))
                    if _is_brazilian(r)
                ]
                # Only a non-empty Brazilian slice counts as the shield being
                # beaten. Docs that all fail the Brazil assertion leave us
                # exactly where the 403s did, and clearing the flag here would
                # cache an empty list under the 30-day TTL.
                if browser_records:
                    records = browser_records
                    errored_any = False

        if not records and errored_any:
            if pcdt_records:
                return pcdt_records, pcdt_meta
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        # Merge PCDT records (first) and BVS records, deduplicating by record_id
        seen_ids: set[str] = set()
        merged_records: list[BrazilGuideline] = []
        for r in pcdt_records + records:
            if r.record_id and r.record_id in seen_ids:
                continue
            if r.record_id:
                seen_ids.add(r.record_id)
            merged_records.append(r)

        # Rank, then slice. Slicing first would hand the ranker only `clamped`
        # of the `count` over-fetched candidates and discard the rest in BVS
        # order, defeating the over-fetch.
        records = rank_brazil_guidelines(merged_records, query)[:clamped]

        # A chain with a stalled stage returns partial results; caching them
        # under the 30-day TTL would make a transient stall permanent.
        if not errored_any:
            await self.cache.set(
                cache_key,
                [record.to_dict() for record in records],
                source="brazil_moh",
            )
        return records, CacheMetadata(cached=False, cache_age=0, error=False)

    def _browser_ceiling(self, chain_start: float) -> float:
        """Ceiling for the browser tier: the flat per-tier cap, shrunk to the
        chain budget still left when the browser tier starts. The whole chain
        (PCDT + every BVS stage + browser) is what the caller's hard timeout
        bounds, so a flat 30 s browser cap after five stalled 10 s stages
        would outlast a 60 s caller ceiling. ``brazil_chain_timeout_s <= 0``
        disables the chain bound and leaves the flat cap.
        """
        ceiling = float(self.settings.brazil_browser_timeout_s)
        chain_budget = float(
            getattr(self.settings, "brazil_chain_timeout_s", 0.0) or 0.0
        )
        if chain_budget > 0:
            remaining = chain_budget - (time.monotonic() - chain_start)
            ceiling = min(ceiling, max(remaining, 0.0))
        return ceiling

    async def _camoufox_search(
        self, composed: str, count: int, ceiling: float
    ) -> list[dict[str, Any]]:
        """Last-resort rendered fetch of the BVS JSON search payload.

        ``pesquisa.bvsalud.org`` 403s plain HTTP clients behind a Bunny CDN
        shield, and the HTTP layer retries that 403 like a 429 until the retry
        budget exhausts, so every stage ends as ``([], True)``. A real browser
        carries the fingerprint the shield accepts. Returns raw Solr-style doc
        dicts so ``_dedupe_by_id``/``_build_record``/``_is_brazilian`` apply
        unchanged. Any failure or timeout returns ``[]``.

        ``ceiling`` is mandatory: the caller passes the chain budget left
        (see ``_browser_ceiling``), never the flat cap alone.
        """
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            logger.warning("camoufox unavailable; BVS browser fallback disabled")
            return []

        target = (
            f"{BVS_SEARCH_URL}?q={urllib.parse.quote(composed)}"
            f"&output=json&count={count}"
        )

        async def _run() -> list[dict[str, Any]]:
            async with AsyncCamoufox(headless=True) as browser:
                page = await browser.new_page()
                await page.goto(
                    target, wait_until="domcontentloaded", timeout=_CAMOUFOX_NAV_TIMEOUT_MS
                )
                content = await page.content()
            # A JSON payload rendered in a browser arrives wrapped in
            # <html><body><pre>...</pre></body></html>; tag-stripping must
            # leave a bare JSON body untouched.
            text = BeautifulSoup(content, "html.parser").get_text()
            data = json.loads(text)
            return _extract_docs(data)

        try:
            return await asyncio.wait_for(_run(), timeout=ceiling)
        except Exception:
            logger.warning("brazil_moh camoufox fallback failed", exc_info=True)
            return []

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
        # Pick headers by hostname, not by substring: "gov.br" appearing in
        # a query string or path on a non-gov host must not match.
        try:
            host = (urllib.parse.urlparse(document_url or "").hostname or "").lower()
        except ValueError:
            host = ""
        headers = GOVBR_HEADERS if host.endswith("gov.br") else BVS_HEADERS
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
