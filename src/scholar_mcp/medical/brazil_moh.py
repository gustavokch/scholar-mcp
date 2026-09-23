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

Downstream contract (ENAMED 2026 misses, track B §2): every
``search_guidelines`` call reports a machine-readable ``error_kind`` on its
``CacheMetadata`` -- ``ok`` | ``successful_empty`` | ``cdn_challenge`` |
``origin_outage`` | ``timeout`` | ``backend_error`` -- plus ``http_status``,
``challenge_hit``, ``timeout`` and ``cache_hit`` (``CacheMetadata.cached``)
fields, so the caller distinguishes a CDN shield from a sick origin
instead of reading one ``backend_error``. ``origin_outage`` must never
count against any caller-side breaker, and a search that ends in one is
never cached. One document fetch is the exception: an abstract served
after a failed PDF fetch is held for ``DEGRADED_RESULT_TTL_SECONDS`` and
carries its ``error_kind`` inside the cached row, so every hit in that
window reports the same degradation the first caller saw. ``record_id``
is the stable Solr document id and the fold key for ``med:brmoh:``.
Published budgets: the search chain (``brazil_chain_timeout_s``,
per-stage ``brazil_stage_timeout_s``, browser tier
``brazil_browser_timeout_s``) is a separate budget from one document
fetch (``brazil_fulltext_timeout_s``) -- a slow search never eats into
the full-text ceiling, and vice versa.
"""

import asyncio
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.govbr_az import GovBrAZEngine
from scholar_mcp.medical.govbr_pcdt import GOVBR_HEADERS, GovBrPCDTEngine
from scholar_mcp.medical.models import BrazilGuideline, has_retrievable_body
from scholar_mcp.medical.passages import DEFAULT_SERVING_CHARS, serve_body
from scholar_mcp.medical.ranking import (
    PORTUGUESE_STOPWORDS,
    normalize_portuguese,
    rank_brazil_guidelines,
    tokenize_portuguese,
)
from scholar_mcp.parsers.pdf import pdf_bytes_to_text
from scholar_mcp.utils.http import RETRYABLE_STATUS_CODES, AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import BvsErrorKind, CacheMetadata, SQLiteCacheManager

_BVS_HOST = "pesquisa.bvsalud.org"
BVS_SEARCH_URL = f"https://{_BVS_HOST}/portal/"

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
# Over-fetch, then re-rank client-side, then slice. The factor is tied to
# BASE_FILTER's width: admitting the monography class takes the pt pool from
# roughly 23.6k to 117.8k documents and about triples the hit count of a
# topical query, so a factor of 3 would truncate targets out of the window
# before rank_brazil_guidelines ever sees them. At limit=10 this fetches 100.
# The factor of 10 is fully realized up to limit=20 (200 records); above that,
# MAX_PAGE_SIZE = 200 governs (e.g. at limit=50 the effective factor is 4).
OVERFETCH_FACTOR = 10
MAX_PAGE_SIZE = 200
MAX_FULL_TEXT_CHARS = 600_000

# Bumped whenever a cached row's shape changes. from_dict() fills missing
# fields with defaults rather than failing, so an un-bumped key serves a
# pre-change row as if it were current: has_full_text silently False, a
# body already cut to the old 50k ceiling with no total_chars. TTL here is
# 30 days (config.cache_ttl_brazil_moh), so an un-bumped key is a month of
# wrong answers. v2: has_full_text (B4) + 600k bodies with total_chars (B3).
CACHE_SCHEMA = "v2"

# Camoufox (anti-detection Firefox) fetches the JSON search payload with the
# browser fingerprint the CDN shield accepts. Mirrors the pediatrics scraper:
# one navigation, hard total ceiling so a hung browser cannot outlive the
# caller's own timeout. The total ceiling comes from
# ``settings.brazil_browser_timeout_s``. 30 s lets the navigation absorb a
# slow Solr response or a long Bunny CDN challenge without the browser tier
# ceiling (45 s) firing first.
_CAMOUFOX_NAV_TIMEOUT_MS = 30000

# A Camoufox launch needs tens of seconds; below this floor the browser
# tier cannot do useful work, so skip the launch outright. Not independently
# measured for this deployment -- 20.0 is a conservative floor consistent
# with "tens of seconds" that still leaves headroom for navigation on a
# ceiling that clears it. Raise once a real launch-time measurement exists.
_CAMOUFOX_MIN_USEFUL_CEILING_S = 20.0

_BVS_CHALLENGE_MARKERS = (
    "shield-templates",
    "b-cdn.net",
    "block.html",
    "challenge-platform",
    "just a moment",
)

# The Bunny CDN challenge is upstream of the origin; once it passes, a sick origin
# answers with its own Portuguese error page ("Erro 504 - Gateway Timeout"). That is
# an outage, not a block, and mislabelling it sends the next debugging session at the
# wrong layer.
_BVS_OUTAGE_MARKERS = (
    "erro 502",
    "erro 503",
    "erro 504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
)

# Machine-readable failure taxonomy for the §2 contract, defined in
# sqlite_cache.py next to ``CacheMetadata.error_kind``. "ok" means records
# were returned; "successful_empty" means the endpoint answered cleanly and
# there is genuinely nothing matching (not a failure). "cdn_challenge" is a
# Bunny/CDN shield 403 or block-HTML page (retryable once via the browser
# path); "origin_outage" is a 5xx or origin error page (no retry, no breaker
# count, never cached); "timeout" is a stage/chain/transport timeout;
# "backend_error" is anything else.

# Raised abstract cap (S2.1): shaped hits must carry a decidable body, and
# the old downstream previews truncated well below what the source provides.
# BVS ``ab`` fields run to a few KB; 2000 chars keeps the full abstract of a
# technical manual while bounding the merged payload.
ABSTRACT_MAX_CHARS = 2000

# BVS returns a transient 5xx per request, not per outage: the same URL
# alternates 502 and 200 across consecutive requests, and the 502 arrives in
# well under a second, so the retry ladder recovers it for a fraction of the
# stage budget. Treating it as a settled outage discarded the stage's whole
# yield. BVS therefore narrows nothing today and this is an alias, not a copy:
# ``retryable_statuses`` is a narrowing hook (see ``AsyncHttpClient.get``), and
# a hand-copied literal would silently fall behind a future widening of the
# shared default. The name stays so a per-host narrowing is one line. The
# non-challenge shield-403 burst retry still applies inside the HTTP layer even
# under this override.
_BVS_RETRYABLE_STATUSES = RETRYABLE_STATUS_CODES

_DOI_RE = re.compile(r"(?<![\w.])10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)


@dataclass
class _SearchState:
    """Mutable state for one ``search_guidelines`` call.

    The engine is a shared singleton (src/scholar_mcp/server.py), so state
    that must not bleed across concurrent searches lives here, never on
    ``self``. ``bvs_shielded`` carries the CDN-shield 403 verdict out of
    ``_fetch_records`` — the client's ``last_failure`` is a per-task
    ContextScoped var, so a stage running under ``asyncio.wait_for`` cannot
    read it afterwards (the write happened in the child task).
    ``bvs_origin_down`` records a 5xx origin error (500, 502, 503, 504), which
    short-circuits subsequent BVS stages in the same call. ``bvs_shielded``
    is also set when a 200 carries block-HTML instead of JSON; truncated
    JSON or other garbage sets no flag, since a single extra stage is not
    a cascade. ``http_status``/``challenge_hit``/``error_kind`` feed the §2
    diagnostics contract; ``stages_attempted`` and ``overfetch_window`` are
    observed per call for the S0.1 error-taxonomy table.
    """

    bvs_shielded: bool = False
    bvs_origin_down: bool = False
    bvs_timed_out: bool = False
    http_status: int | None = None
    challenge_hit: bool = False
    error_kind: BvsErrorKind | Literal[""] = ""
    stages_attempted: int = 0
    overfetch_window: int = 0


@dataclass
class _SearchOutcome:
    """The fields that vary across ``search_guidelines``' meta+log call sites.

    ``query``, ``norm_collection``, ``clamped``, and ``call_start`` are the
    same for every exit of one ``search_guidelines`` call, so they stay as
    plain arguments to ``_finalize_search`` rather than living here.
    """

    cached: bool
    cache_age: int
    error: bool
    state: _SearchState
    records: list[BrazilGuideline]
    rerank_in: int
    rerank_out: int


BASE_FILTER = 'la:"pt" AND (type:"non-conventional" OR type:"monography")'
BRISA_FILTER = 'db:"BRISA"'
VALID_COLLECTIONS = frozenset({"all", "brisa", "pcdt", "az"})

# How long a merged result is held when BVS answered cleanly but a local
# gov.br scraper failed. Short enough that the missing rows reappear soon
# after the scraper recovers, long enough that a burst of queries does not
# re-run the whole BVS chain each time.
DEGRADED_RESULT_TTL_SECONDS = 300

# The classified failure that produced a cached full-text payload travels
# inside the row under this key. Without it, only the first caller in the
# 300 s degraded window sees ``error_kind`` (and the ``degraded`` flag
# server.py derives from it); everyone behind the cache reads a byte-identical
# payload as clean. Private to the stored row: ``_serve_full_text`` strips it,
# so it never reaches a caller. Rows written before this key existed read as
# "" -- the same value they reported before.
_CACHED_ERROR_KIND_KEY = "_error_kind"

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


def _candidate_terms(record: BrazilGuideline) -> set[str]:
    """The record text the topic gate matches on.

    Deliberately the same fields ``rank_brazil_guidelines`` scores -- both
    titles, the abstract, and the DeCS descriptors. Keying on the title alone
    would drop a record that names the topic only in its body.
    """
    return set(
        tokenize_portuguese(
            " ".join(
                [
                    record.title or "",
                    record.title_en or "",
                    record.abstract or "",
                    *(record.mesh_subjects or []),
                ]
            )
        )
    )


def _topic_filtered(
    records: list[BrazilGuideline], query: str
) -> list[BrazilGuideline]:
    """Drop fallback candidates that match no discriminating query term.

    The ranker weights every query term equally (``ScoringEngine.text_coverage``
    divides by the term count), so a clinically generic token carries as much
    weight as the topic itself. Measured 2026-09-23: "curvas de crescimento
    sindrome de Down" returned growth-hormone, SRAG, myelodysplastic and
    nephrotic documents, all scoring on "sindrome" and "crescimento" alone,
    and "dengue manejo clinico" ranked Bronquiolite and Chikungunya above the
    actual dengue guides. The two pools' scores overlap, so a score floor
    cannot separate them -- topic-term presence can.

    Document frequency is computed over the candidate pool itself rather than
    a static word list or corpus statistics: a term carried by most of the
    pool discriminates nothing, whichever term it happens to be. The rarest
    terms are the topical ones, and a candidate survives by carrying at least
    one of them.

    Rarest, not "below the median": with frequencies {dengue: 2, manejo: 4,
    clinico: 4} the median is 4 and every term qualifies, which filters
    nothing. The minimum isolates "dengue".

    A query term absent from the whole pool has frequency zero, so it becomes
    the sole discriminating term and cannot be satisfied, which empties the
    pool. That is the intended reading: the fallback corpus does not cover
    this topic, and offering its nearest lexical neighbours is worse than
    offering nothing. It also makes the gate strict on long queries, where a
    single incidental term that happens to be absent empties the pool -- the
    conservative direction for a fallback.

    Follow-up: document frequency is taken over the candidate pool only; real
    IDF over the PCDT catalogue if a ~15-document pool proves too coarse.
    """
    # Solr operators are stripped for the same reason ``_usable_tokens``
    # strips them before a token reaches the index: they are query syntax, not
    # topic. Left in, they carry frequency zero against any Portuguese record,
    # become the sole discriminating term, and empty every pool -- "dengue AND
    # manejo" would drop the dengue guides it names.
    terms = [t for t in tokenize_portuguese(query) if t not in _SOLR_BOOLEAN_WORDS]
    if not records or not terms:
        return records

    per_record = [_candidate_terms(r) for r in records]
    frequencies = {t: sum(1 for c in per_record if t in c) for t in terms}
    lowest = min(frequencies.values())
    highest = max(frequencies.values())
    if highest == 0:
        # Zero overlap anywhere: the pool is noise, not evidence.
        return []
    if lowest == highest:
        # Uniform pool: nothing here discriminates, so drop nothing.
        return records

    discriminating = {t for t, f in frequencies.items() if f == lowest}
    return [
        record
        for record, carried in zip(records, per_record, strict=True)
        if carried & discriminating
    ]


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


def _extract_doi(doc: dict[str, Any]) -> str:
    """First DOI found in the record's link list, or "".

    BVS non-conventional records usually carry no DOI; the field stays
    empty rather than guessed. Scans ``ur`` for a bare ``10.xxxx/...``
    string or a ``doi.org`` URL, cuts URL query/fragment, and strips
    trailing punctuation the Solr field occasionally appends. The left
    boundary keeps mid-string numeric paths (``v10.1234/...``) from
    matching.
    """
    for url in _as_list(doc.get("ur")):
        match = _DOI_RE.search(url or "")
        if match:
            doi = re.split(r"[?#]", match.group(0), maxsplit=1)[0]
            return doi.rstrip(".,;:)]}")
    return ""


def _coerce_abstract(doc: dict[str, Any]) -> tuple[str, bool]:
    """Record abstract under the raised cap, never silently empty when text exists.

    ``ab`` is truncated to ``ABSTRACT_MAX_CHARS`` (S2.1). When the source
    provides no abstract but indexes DeCS descriptors, those become the
    decidable body (``Temas (DeCS): ...``); when even those are absent but
    an English title distinct from the Portuguese one exists, it stands in.
    Genuinely textless records keep "" so the caller can still tell them
    apart from a populated snippet.

    Returns ``(body, synthetic)``: ``synthetic`` is True when the body was
    fabricated from descriptors or ``ti_en`` rather than the source abstract.
    """
    raw = _first(doc.get("ab")).strip()
    if raw:
        return raw[:ABSTRACT_MAX_CHARS], False
    mesh = _as_list(doc.get("mh"))
    if mesh:
        fallback = "Temas (DeCS): " + "; ".join(mesh)
        return fallback[:ABSTRACT_MAX_CHARS], True
    title = _first(doc.get("ti")).strip()
    title_en = _first(doc.get("ti_en")).strip()
    if title_en and title_en != title:
        return title_en[:ABSTRACT_MAX_CHARS], True
    return "", False


def _build_record(doc: dict[str, Any]) -> BrazilGuideline:
    """Map one Solr document onto a BrazilGuideline."""
    document_url = _select_document_url(doc)
    year, issued = _parse_issued(doc.get("da"))
    abstract, synthetic = _coerce_abstract(doc)
    fulltext_id = _derive_fulltext_id(document_url)
    return BrazilGuideline(
        title=_first(doc.get("ti")),
        title_en=_first(doc.get("ti_en")),
        record_id=_first(doc.get("id")),
        document_url=document_url,
        fulltext_id=fulltext_id,
        # Body-less catalog cards (no `ur`, no `ab`) are shaped here too;
        # the flag lets ranking damp them instead of citing them as
        # evidence (ENAMED misses plan B4). One shared rule with the gov.br
        # converters: see medical.models.has_retrievable_body.
        # A synthetic body (DeCS descriptors or ti_en) is a search snippet,
        # not a retrievable body, so it is not offered as fallback_text.
        has_full_text=has_retrievable_body(
            document_url,
            fulltext_id=fulltext_id,
            fallback_text="" if synthetic else abstract,
            url_trusted=is_allowed_bvs_host(document_url),
        ),
        abstract=abstract,
        abstract_synthetic=synthetic,
        year=year,
        issued=issued,
        country=_parse_country(doc.get("pais_publicacao")),
        authors=_as_list(doc.get("au")),
        languages=_as_list(doc.get("la")),
        collections=_as_list(doc.get("db")),
        mesh_subjects=_as_list(doc.get("mh")),
        doi=_extract_doi(doc),
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


def _classify_failure(failure: Any) -> BvsErrorKind:
    """Map an ``AsyncHttpClient`` failure onto a ``BvsErrorKind``.

    403 -> cdn_challenge, 5xx -> origin_outage, transport-kind failures ->
    timeout, anything else -> backend_error.
    """
    status = getattr(failure, "status", None)
    if status == 403:
        return "cdn_challenge"
    if status is not None and 500 <= status < 600:
        return "origin_outage"
    if getattr(failure, "kind", "") == "transport":
        return "timeout"
    return "backend_error"


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
        self.az_engine = GovBrAZEngine(http_client, cache, settings)

    async def _fetch_records(
        self,
        composed: str,
        count: int,
        state: _SearchState,
    ) -> tuple[list[BrazilGuideline], bool]:
        """One BVS search request, parsed, deduplicated and Brazil-filtered.

        Returns ``(records, errored)``. Extracted so the strict and relaxed
        stages cannot drift apart in how they parse or filter. A shield 403
        verdict is recorded on ``state`` for ``_is_bvs_shielded``. A 200
        carrying block-HTML (shield/challenge page) instead of JSON also
        sets ``state.bvs_shielded`` so later BVS stages short-circuit;
        truncated JSON or other garbage sets no flag.

        Transient 5xx retries inside this call
        (``_BVS_RETRYABLE_STATUSES``): the host alternates 5xx and 200 across
        consecutive requests, so the next attempt usually carries the records
        this one missed, and it arrives fast enough to fit the stage budget.
        A failure surviving the bounded ladder is classified onto ``state``
        (``cdn_challenge`` vs ``origin_outage`` vs ``timeout`` vs
        ``backend_error``) for the §2 contract.
        """
        state.stages_attempted += 1
        state.overfetch_window = max(state.overfetch_window, count)
        resp = await self.http_client.get(
            BVS_SEARCH_URL,
            headers=BVS_HEADERS,
            params={
                "q": composed,
                "output": "json",
                "count": count,
            },
            retryable_statuses=_BVS_RETRYABLE_STATUSES,
        )
        if resp is None:
            failure = getattr(self.http_client, "last_failure", None)
            state.http_status = getattr(failure, "status", None)
            kind = _classify_failure(failure)
            state.error_kind = kind
            if kind == "cdn_challenge":
                state.bvs_shielded = True
                state.challenge_hit = True
            elif kind == "origin_outage":
                state.bvs_origin_down = True
            elif kind == "timeout":
                state.bvs_timed_out = True
            return [], True

        state.http_status = resp.status_code
        try:
            data = resp.json()
        except ValueError:
            if self.http_client.is_unexpected_html(
                resp
            ) or self.http_client.is_challenge_html(resp):
                logger.warning("brazil_moh search returned block-HTML payload")
                state.bvs_shielded = True
                state.challenge_hit = True
                state.error_kind = "cdn_challenge"
            else:
                logger.warning("brazil_moh search returned non-JSON payload")
                state.error_kind = "backend_error"
            return [], True

        records = [_build_record(doc) for doc in _dedupe_by_id(_extract_docs(data))]
        return [record for record in records if _is_brazilian(record)], False

    def _is_bvs_shielded(self, state: _SearchState) -> bool:
        if state.bvs_shielded:
            return True
        return self.http_client.is_throttled(_BVS_HOST)

    def _bvs_unavailable(self, state: _SearchState) -> bool:
        """True when BVS is shielded by CDN 403, throttled, origin 5xx, or timed out."""
        return self._is_bvs_shielded(state) or state.bvs_origin_down or state.bvs_timed_out

    def _stage_budget(self, chain_start: float | None = None) -> float:
        """Effective time budget for one retrieval stage.

        Returns min(brazil_stage_timeout_s, chain_budget_remaining).
        A setting <= 0 disables that bound.
        """
        stage_budget = float(self.settings.brazil_stage_timeout_s)
        if chain_start is None:
            return stage_budget
        chain_budget = float(self.settings.brazil_chain_timeout_s)
        if chain_budget <= 0:
            return stage_budget
        remaining = max(chain_budget - (time.monotonic() - chain_start), 0.0)
        if stage_budget <= 0:
            return remaining
        return min(stage_budget, remaining)

    def _fire_on_timeout(
        self, stage: str, on_timeout: Callable[[], None] | None
    ) -> None:
        """Run a stage's timeout callback, never letting it break the stage.

        The callback exists to arm a circuit breaker. A callback that raises
        must not turn a handled stage timeout into a chain-level failure.
        """
        if on_timeout is None:
            return
        try:
            on_timeout()
        except Exception:
            logger.exception("brazil_moh %s on_timeout callback failed", stage)

    async def _stage(
        self,
        stage: str,
        coro: Any,
        default: Any,
        chain_start: float | None = None,
        on_timeout: Callable[[], None] | None = None,
    ) -> Any:
        """Run one retrieval stage under its own time budget.

        Callers wrap the whole ``search_guidelines`` chain in a hard ceiling
        (zimqa's per-call ``wait_for``). A stage stalled behind a throttled
        rate limiter must die here so the remaining stages — or the PCDT
        fallback — still get their share of that ceiling, and so the chain
        degrades to partial results instead of a cancelled coroutine.

        Passing ``on_timeout`` allows callers to register a timeout callback,
        e.g. to arm a circuit breaker to halt remaining stages.

        Budget is min(brazil_stage_timeout_s, chain_time_left) when chain_start
        is provided. If the chain budget is already exhausted (or stage budget <= 0
        and expired), the stage is skipped, ``on_timeout`` is fired, and ``default``
        is returned. If both bounds are disabled (<= 0), ``coro`` runs unbounded.
        """
        stage_setting = float(self.settings.brazil_stage_timeout_s)
        chain_setting = (
            float(self.settings.brazil_chain_timeout_s)
            if chain_start is not None
            else 0.0
        )
        # If neither setting is active, timeout is disabled.
        if stage_setting <= 0 and chain_setting <= 0:
            return await coro

        budget = self._stage_budget(chain_start)
        if budget <= 0:
            logger.warning(
                "brazil_moh %s stage skipped: chain budget expired", stage
            )
            if asyncio.iscoroutine(coro):
                coro.close()
            self._fire_on_timeout(stage, on_timeout)
            return default
        try:
            return await asyncio.wait_for(coro, budget)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "brazil_moh %s stage exceeded its %.1fs budget", stage, budget
            )
            self._fire_on_timeout(stage, on_timeout)
            return default

    def _mark_bvs_timed_out(self, state: _SearchState) -> None:
        state.bvs_timed_out = True
        if not state.error_kind:
            state.error_kind = "timeout"

    async def _bvs_stage(
        self,
        stage: str,
        composed_query: str,
        count: int,
        state: _SearchState,
        chain_start: float | None = None,
    ) -> tuple[list[BrazilGuideline], bool]:
        def _on_timeout() -> None:
            self._mark_bvs_timed_out(state)

        return await self._stage(
            stage,
            self._fetch_records(composed_query, count, state),
            ([], True),
            chain_start=chain_start,
            on_timeout=_on_timeout,
        )

    def _overfetch_count(self, clamped: int, chain_start: float | None = None) -> int:
        """Over-fetch window for one BVS stage, adaptive to the chain budget.

        Full window is ``min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)``.
        When the chain has burned over half its budget (slow origin), the
        remaining stages shrink to ``min(clamped * 3, 60)``: a huge window
        against a sick Solr endpoint only buys latency, and the client-side
        ranker cannot lift what the server never returns in time. The window
        is adaptive from the chain start, so the title-scoped first stage
        already shrinks when the PCDT/A-Z phase has burned over half the
        chain budget.
        """
        full = min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)
        if chain_start is None:
            return full
        chain_budget = float(self.settings.brazil_chain_timeout_s)
        if chain_budget <= 0:
            return full
        elapsed = time.monotonic() - chain_start
        if elapsed < chain_budget / 2:
            return full
        return min(clamped * 3, 60)

    @staticmethod
    def _apply_since_year(
        records: list[BrazilGuideline], since_year: int | None
    ) -> list[BrazilGuideline]:
        """Drop records published before ``since_year`` (S2.2).

        ``since_year <= 0`` or ``None`` disables the filter. Records with a
        missing or unparseable year are kept: absence of metadata must not
        read as old. This runs before ranking so the relaxed-OR slice cannot
        drown 2024-2025 documents under a larger pool of pre-2015 ones.
        """
        if not since_year or since_year <= 0:
            return records
        kept: list[BrazilGuideline] = []
        for record in records:
            try:
                year = int((record.year or "").strip()[:4])
            except ValueError:
                kept.append(record)
                continue
            if year >= since_year:
                kept.append(record)
        return kept

    @staticmethod
    def _finalize_records(
        records: list[BrazilGuideline],
        query: str,
        since_year: int | None,
        clamped: int,
    ) -> tuple[list[BrazilGuideline], int]:
        """Filter by ``since_year``, rank, then slice to ``clamped``.

        Shared by the cache-hit and cache-miss paths of ``search_guidelines``
        so the cache can hold one unfiltered row per (collection, limit,
        query) and still reproduce the exact per-``since_year`` result --
        filtering before ranking, as ``_apply_since_year`` requires.
        """
        filtered = BrazilMoHEngine._apply_since_year(records, since_year)
        ranked = rank_brazil_guidelines(filtered, query)[:clamped]
        return ranked, len(filtered)

    def _search_meta(
        self,
        *,
        error: bool,
        state: _SearchState,
        records: list[BrazilGuideline],
    ) -> CacheMetadata:
        """Build the §2 diagnostics-bearing CacheMetadata for a search call."""
        if not error and records:
            kind = "ok"
        elif not error:
            kind = state.error_kind or "successful_empty"
            if kind not in ("successful_empty", "ok") and not (
                state.challenge_hit or state.bvs_origin_down
            ):
                kind = "successful_empty"
        else:
            kind = state.error_kind or "backend_error"
            if state.bvs_timed_out:
                kind = "timeout"
            elif kind == "ok":
                kind = "backend_error"
        return CacheMetadata(
            cached=False,
            cache_age=0,
            error=error,
            error_kind=kind,
            http_status=state.http_status,
            challenge_hit=state.challenge_hit,
            timeout=state.bvs_timed_out,
        )

    def _log_diagnostics(
        self,
        *,
        query: str,
        norm_collection: str,
        clamped: int,
        state: _SearchState,
        meta: CacheMetadata,
        elapsed_s: float,
        cache_hit: bool,
        rerank_in: int,
        rerank_out: int,
    ) -> None:
        """Emit the S0.1 per-call diagnostics line (no behavior change)."""
        logger.info(
            "brazil_moh search query=%r collection=%s limit=%d "
            "elapsed=%.2fs http_status=%s challenge_hit=%s cache_hit=%s "
            "timeout=%s overfetch_window=%d stages=%d rerank_in=%d "
            "rerank_out=%d error=%s error_kind=%s",
            query,
            norm_collection,
            clamped,
            elapsed_s,
            state.http_status,
            state.challenge_hit,
            cache_hit,
            state.bvs_timed_out,
            state.overfetch_window,
            state.stages_attempted,
            rerank_in,
            rerank_out,
            meta.error,
            meta.error_kind,
        )

    def _finalize_search(
        self,
        outcome: _SearchOutcome,
        *,
        query: str,
        norm_collection: str,
        clamped: int,
        call_start: float,
    ) -> CacheMetadata:
        """Build the CacheMetadata for one search_guidelines exit and log it.

        Collapses the cache-hit and cache-miss meta-building paths behind one
        call: a cache hit carries its own ``cached``/``cache_age`` and a
        simpler success/empty classification, while every other exit goes
        through ``_search_meta``'s state-based classification.
        """
        elapsed_s = time.monotonic() - call_start
        if outcome.cached:
            meta = CacheMetadata(
                cached=True,
                cache_age=outcome.cache_age,
                error=False,
                error_kind="ok" if outcome.records else "successful_empty",
            )
        else:
            meta = self._search_meta(
                error=outcome.error, state=outcome.state, records=outcome.records
            )
        self._log_diagnostics(
            query=query,
            norm_collection=norm_collection,
            clamped=clamped,
            state=outcome.state,
            meta=meta,
            elapsed_s=elapsed_s,
            cache_hit=outcome.cached,
            rerank_in=outcome.rerank_in,
            rerank_out=outcome.rerank_out,
        )
        return meta

    async def search_guidelines(
        self,
        query: str,
        limit: int = 10,
        collection: str = "all",
        since_year: int | None = None,
    ) -> tuple[list[BrazilGuideline], CacheMetadata]:
        norm_collection = (collection or "all").strip().lower()
        if norm_collection not in VALID_COLLECTIONS:
            logger.warning("unknown brazil_moh collection %r", collection)
            return [], CacheMetadata(
                cached=False, cache_age=0, error=True, error_kind="backend_error"
            )

        clamped = min(max(1, limit), MAX_RESULTS)

        if norm_collection in ("pcdt", "az"):
            sub_engine = (
                self.pcdt_engine if norm_collection == "pcdt" else self.az_engine
            )
            # The sub-engine ranks its catalog and slices to the limit it is
            # handed, so asking for `clamped` and dropping pre-``since_year``
            # rows afterwards returns fewer rows than the caller asked for
            # while matching ones sat further down that ranking. Widen the
            # window instead and slice after the filter -- the catalogs are
            # local, so a wider window costs no extra request.
            fetch_limit = (
                min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)
                if since_year and since_year > 0
                else clamped
            )
            records, sub_meta = await sub_engine.search(query, limit=fetch_limit)
            records = self._apply_since_year(records, since_year)[:clamped]
            return records, CacheMetadata(
                cached=sub_meta.cached,
                cache_age=sub_meta.cache_age,
                error=sub_meta.error,
                error_kind=sub_meta.error_kind
                or ("ok" if records else "successful_empty"),
                http_status=sub_meta.http_status,
                challenge_hit=sub_meta.challenge_hit,
                timeout=sub_meta.timeout,
            )

        # A blank query deliberately browses the collection. A query that
        # carries text but sanitizes away to nothing is different: composing
        # filters alone would return arbitrary top-of-index documents dressed
        # as matches for terms that were never searched. Stopword stripping
        # widens this: "sobre a" now lands here rather than being searched.
        tokens = _usable_tokens(query)
        if (query or "").strip() and not tokens:
            logger.info("brazil_moh query %r has no searchable tokens", query)
            return [], CacheMetadata(
                cached=False, cache_age=0, error=False, error_kind="successful_empty"
            )

        # Keyed on the composed strict title-scoped query, not the raw one:
        # "dengue" and "  dengue  " compose identically and must share one
        # cache row. The fallback and relaxed queries never enter the key --
        # they derive from the same user query, so one user query keeps one row.
        # ``since_year`` is deliberately absent from the key: the cached row
        # holds the unfiltered merge, and ``_finalize_records`` applies the
        # year filter uniformly on every read (hit or miss), so one row
        # serves every ``since_year`` instead of fragmenting the 30-day
        # cache per distinct year requested.
        title_composed = _build_query(query, norm_collection, operator="AND", title_scoped=True)
        cache_key = (
            f"brazil_moh_search:{CACHE_SCHEMA}:{norm_collection}:{clamped}:{title_composed}"
        )
        call_start = time.monotonic()
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            cached_records, _ = self._finalize_records(
                [BrazilGuideline.from_dict(item) for item in cached_data],
                query,
                since_year,
                clamped,
            )
            hit_meta = self._finalize_search(
                _SearchOutcome(
                    cached=True,
                    cache_age=meta.cache_age,
                    error=False,
                    state=_SearchState(),
                    records=cached_records,
                    rerank_in=0,
                    rerank_out=len(cached_records),
                ),
                query=query,
                norm_collection=norm_collection,
                clamped=clamped,
                call_start=call_start,
            )
            return cached_records, hit_meta

        # Query PCDT engine first. Every stage runs under its own budget, so
        # one stalled stage costs its budget and the chain moves on. The chain
        # start anchors the browser tier's share of the chain budget (Task 3:
        # the whole chain, not each stage, is what the caller's ceiling
        # bounds).
        chain_start = time.monotonic()
        state = _SearchState()
        stage_error_meta = CacheMetadata(cached=False, cache_age=0, error=True)
        (pcdt_records, pcdt_meta), (az_records, az_meta) = await asyncio.gather(
            self._stage("govbr_pcdt", self.pcdt_engine.search(query, limit=clamped), ([], stage_error_meta), chain_start=chain_start),
            self._stage("govbr_az", self.az_engine.search(query, limit=clamped), ([], stage_error_meta), chain_start=chain_start),
        )
        errored_any = pcdt_meta.error or az_meta.error
        # The browser tier answers BVS failures (the CDN shield 403s plain
        # HTTP clients); a PCDT outage with a healthy BVS must not launch a
        # real browser. Track BVS errors on their own flag.
        bvs_errored = False

        count = self._overfetch_count(clamped, chain_start)
        # The all-field query is built once and shared by the all-field
        # fallback stage and the browser tier, which need the identical
        # composition. Both consumers only run when `tokens` is non-empty.
        all_composed: str | None = (
            _build_query(query, norm_collection, operator="AND", title_scoped=False)
            if tokens
            else None
        )
        records, errored = await self._bvs_stage(
            "title-scoped",
            title_composed,
            count,
            state,
            chain_start=chain_start,
        )
        errored_any = errored_any or errored
        bvs_errored = bvs_errored or errored

        # Progressive title-token relaxation: when the full-token title AND
        # returns zero records without error, drop trailing tokens and retry.
        # Scenario queries frequently contain clinical descriptors ('grupo',
        # 'criterios', 'hidratacao') that do not appear in formal manual titles.
        # An errored title stage halts the remaining BVS stages regardless of
        # which title stage failed: the endpoint is already misbehaving, so
        # further variants likely fail the same way. When BVS is unavailable
        # (shielded by CDN 403 or origin 5xx down), skip further stages.
        title_chain_errored = False
        if not records and not errored and tokens and not self._bvs_unavailable(state):
            for relaxed_tokens in _title_token_relaxations(tokens):
                relaxed_title_composed = _build_query(
                    query, norm_collection, title_scoped=True, tokens=relaxed_tokens
                )

                relaxed_title_records, relaxed_title_errored = await self._bvs_stage(
                    "title-scoped-relaxed",
                    relaxed_title_composed,
                    count,
                    state,
                    chain_start=chain_start,
                )
                errored_any = errored_any or relaxed_title_errored
                bvs_errored = bvs_errored or relaxed_title_errored
                if relaxed_title_errored:
                    title_chain_errored = True
                    break
                if relaxed_title_records:
                    records = relaxed_title_records
                    break

        # OR-title stage. Every AND conjunction above requires all tokens to
        # share one title, which a clinical-scenario query rarely satisfies:
        # measured on the dengue item, the strict stage and all three
        # relaxation steps return zero while ORing the same tokens in ti:
        # surfaces the manual inside the over-fetch window for the client
        # ranker to lift. It runs before the all-field fallback because a
        # title match is a stronger signal than an abstract match, and it is
        # skipped for a single token, where it would compose identically to
        # the strict stage and waste a request. It is also skipped if the
        # strict title stage errored (stalled/failed) to avoid paying an extra
        # stage budget against a misbehaving endpoint.
        if (
            not records
            and not errored
            and len(tokens) >= 2
            and not title_chain_errored
            and not self._bvs_unavailable(state)
        ):
            or_title_composed = _build_query(
                query, norm_collection, operator="OR", title_scoped=True
            )
            or_title_records, or_title_errored = await self._bvs_stage(
                "title-scoped-or",
                or_title_composed,
                self._overfetch_count(clamped, chain_start),
                state,
                chain_start=chain_start,
            )
            errored_any = errored_any or or_title_errored
            bvs_errored = bvs_errored or or_title_errored
            if or_title_errored:
                title_chain_errored = True
            else:
                records = or_title_records

        # Fall back to an all-field query when the title-scoped stage yields no
        # Brazilian records. A stalled, shielded (CDN anti-bot 403) or 5xx BVS
        # is treated as unhealthy for the rest of the call: further HTTP stages
        # would each burn a full stage budget against the same bad host, and the
        # browser tier -- which is what actually beats a shield -- needs what is
        # left of the chain budget more than they do.
        if (
            not records
            and tokens
            and not title_chain_errored
            and not self._bvs_unavailable(state)
        ):
            fallback_records, fallback_errored = await self._bvs_stage(
                "all-field",
                all_composed,
                self._overfetch_count(clamped, chain_start),
                state,
                chain_start=chain_start,
            )
            errored_any = errored_any or fallback_errored
            bvs_errored = bvs_errored or fallback_errored
            records = fallback_records

        # The strict conjunction found nothing usable -- either no hits at all,
        # or only records the Brazil assertion dropped. Retry the same tokens
        # ORed. A single substantive token is skipped: the two groups would be
        # byte-identical, so the request would be pure waste.
        if (
            not records
            and len(tokens) >= 2
            and not title_chain_errored
            and not self._bvs_unavailable(state)
        ):
            composed_relaxed = _build_query(query, norm_collection, operator="OR", title_scoped=False)
            relaxed_records, relaxed_errored = await self._bvs_stage(
                "relaxed",
                composed_relaxed,
                self._overfetch_count(clamped, chain_start),
                state,
                chain_start=chain_start,
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
                all_composed,
                self._overfetch_count(clamped, chain_start),
                ceiling=self._browser_ceiling(chain_start),
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

        local_records: list[BrazilGuideline] = []
        seen_local: set[str] = set()
        for r in pcdt_records + az_records:
            if r.record_id and r.record_id in seen_local:
                continue
            if r.record_id:
                seen_local.add(r.record_id)
            local_records.append(r)

        if not records and errored_any:
            # Standing in for a failed BVS, these rows reach the caller without
            # the relevance-sorted Solr ordering the ranker's position prior
            # assumes, so a lexical near-miss can top the list. Gate on topic
            # first: an empty result is a truthful "not covered", while four
            # unrelated syndromes read as Brazilian evidence downstream.
            on_topic = _topic_filtered(local_records, query)
            if on_topic:
                # Same filter-rank-slice order as every other exit: slicing
                # first would hand the caller fewer rows than it asked for
                # whenever `since_year` drops a high-ranked old document.
                ranked_local, rerank_in_local = self._finalize_records(
                    on_topic, query, since_year, clamped
                )
                local_meta = self._finalize_search(
                    _SearchOutcome(
                        cached=False,
                        cache_age=0,
                        error=False,
                        state=state,
                        records=ranked_local,
                        rerank_in=rerank_in_local,
                        rerank_out=len(ranked_local),
                    ),
                    query=query,
                    norm_collection=norm_collection,
                    clamped=clamped,
                    call_start=call_start,
                )
                return ranked_local, local_meta
            fail_meta = self._finalize_search(
                _SearchOutcome(
                    cached=False,
                    cache_age=0,
                    error=True,
                    state=state,
                    records=[],
                    rerank_in=0,
                    rerank_out=0,
                ),
                query=query,
                norm_collection=norm_collection,
                clamped=clamped,
                call_start=call_start,
            )
            return [], fail_meta

        # Merge local gov.br records (first) and BVS records, deduplicating
        # by record_id.
        seen_ids: set[str] = set(seen_local)
        merged_records: list[BrazilGuideline] = list(local_records)
        for r in records:
            if r.record_id and r.record_id in seen_ids:
                continue
            if r.record_id:
                seen_ids.add(r.record_id)
            merged_records.append(r)

        # Filter, rank, then slice -- via the same helper the cache-hit path
        # uses, so caching the unfiltered merge below reproduces this exact
        # per-``since_year`` result on every future read. Filtering before
        # ranking matters: a large relaxed-OR pool must not drown 2024-2025
        # documents under pre-2015 ones (S2.2). Ranking before slicing
        # matters too: slicing first would hand the ranker only `clamped` of
        # the `count` over-fetched candidates and discard the rest in BVS
        # order, defeating the over-fetch.
        records, rerank_in = self._finalize_records(
            merged_records, query, since_year, clamped
        )

        # A chain with a stalled stage returns partial results; caching them
        # under the 30-day TTL would make a transient stall permanent. An
        # origin outage is never cached at all: it is not evidence about
        # the corpus, and pinning it would both poison the TTL and count a
        # sick origin against the caller's breaker on replay.
        #
        # The row cached is the unfiltered merge, not the sliced `records`
        # returned to this caller: `since_year` is not part of `cache_key`,
        # so the same row must reproduce the correct result for any
        # `since_year` a later call asks for (via `_finalize_records` on
        # read).
        if not errored_any:
            await self.cache.set(
                cache_key,
                [record.to_dict() for record in merged_records],
                source="brazil_moh",
            )
        elif not bvs_errored:
            # BVS answered cleanly and only a local gov.br scraper failed, so
            # the merge is complete except for that scraper's rows. Pinning it
            # for the full TTL would freeze the gap, but re-running the entire
            # BVS chain on every call for as long as the scraper is down is
            # its own cost. Hold the degraded merge briefly instead.
            await self.cache.set(
                cache_key,
                [record.to_dict() for record in merged_records],
                source="brazil_moh",
                ttl=DEGRADED_RESULT_TTL_SECONDS,
            )
        done_meta = self._finalize_search(
            _SearchOutcome(
                cached=False,
                cache_age=0,
                error=False,
                state=state,
                records=records,
                rerank_in=rerank_in,
                rerank_out=len(records),
            ),
            query=query,
            norm_collection=norm_collection,
            clamped=clamped,
            call_start=call_start,
        )
        return records, done_meta

    def _browser_ceiling(self, chain_start: float) -> float | None:
        """Ceiling for the browser tier, in seconds.

        Returns ``None`` when ``brazil_chain_timeout_s`` is unset (<= 0): the
        chain bound is disabled, so the browser tier is unbounded by the
        chain and the caller falls back to the flat per-tier cap
        (``brazil_browser_timeout_s``) alone.

        Otherwise returns the flat per-tier cap shrunk to the chain budget
        still left when the browser tier starts, floored at ``0.0``. The
        whole chain (PCDT + every BVS stage + browser) is what the caller's
        hard timeout bounds, so a flat 45 s browser cap after five stalled
        20 s stages would outlast a 90 s chain ceiling. A returned ``0.0``
        means the chain budget is exhausted: the caller must skip the tier,
        never launch with a zero timeout.
        """
        chain_budget = float(self.settings.brazil_chain_timeout_s)
        if chain_budget <= 0:
            return None
        remaining = chain_budget - (time.monotonic() - chain_start)
        return min(float(self.settings.brazil_browser_timeout_s), max(remaining, 0.0))

    async def _camoufox_search(
        self, composed: str, count: int, ceiling: float | None
    ) -> list[dict[str, Any]]:
        """Last-resort rendered fetch of the BVS JSON search payload.

        ``pesquisa.bvsalud.org`` 403s plain HTTP clients behind a Bunny CDN
        shield, and the HTTP layer retries that 403 like a 429 until the retry
        budget exhausts, so every stage ends as ``([], True)``. A real browser
        carries the fingerprint the shield accepts. Returns raw Solr-style doc
        dicts so ``_dedupe_by_id``/``_build_record``/``_is_brazilian`` apply
        unchanged. Any failure or timeout returns ``[]``.

        ``ceiling`` comes from ``_browser_ceiling``: ``None`` means the chain
        bound is disabled, so the flat per-tier cap
        (``brazil_browser_timeout_s``) alone applies; ``0.0`` means the chain
        budget is exhausted and the tier is skipped outright, never launched
        with a zero timeout; any positive value is the seconds actually left.
        Whichever value results (``effective_ceiling``) bounds both the
        overall ``wait_for`` and, after subtracting the time the launch
        itself took, the in-browser navigation -- so navigation can never
        silently outlive what launch already spent. An ``effective_ceiling``
        below ``_CAMOUFOX_MIN_USEFUL_CEILING_S`` skips the launch outright: a
        Camoufox launch needs tens of seconds, so a doomed ceiling would only
        burn startup time.
        """
        if ceiling is not None and ceiling <= 0:
            logger.info("brazil_moh camoufox tier skipped: chain budget exhausted")
            return []

        effective_ceiling = (
            float(self.settings.brazil_browser_timeout_s) if ceiling is None else ceiling
        )
        if effective_ceiling < _CAMOUFOX_MIN_USEFUL_CEILING_S:
            logger.info(
                "brazil_moh camoufox tier skipped: %.1fs ceiling below useful floor",
                effective_ceiling,
            )
            return []
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
            launch_start = time.monotonic()
            async with AsyncCamoufox(headless=True) as browser:
                launch_elapsed = time.monotonic() - launch_start
                nav_budget_s = max(effective_ceiling - launch_elapsed, 0.0)
                nav_timeout_ms = min(
                    _CAMOUFOX_NAV_TIMEOUT_MS, max(int(nav_budget_s * 1000), 1)
                )
                page = await browser.new_page()
                await page.goto(
                    target, wait_until="domcontentloaded", timeout=nav_timeout_ms
                )
                content = await page.content()
            soup = BeautifulSoup(content, "html.parser")
            pre = soup.find("pre")
            text = pre.get_text() if pre else soup.get_text()
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                # Only an unparseable payload can be a shield or an error page; a
                # marker phrase inside a parsed record is just record text.
                lowered = content.lower()
                if any(marker in lowered for marker in _BVS_CHALLENGE_MARKERS):
                    logger.info("brazil_moh: BVS browser fallback received challenge or block page")
                elif any(marker in lowered for marker in _BVS_OUTAGE_MARKERS):
                    logger.info("brazil_moh: BVS origin returned an error page")
                else:
                    logger.warning("brazil_moh: BVS browser fallback received non-JSON payload")
                return []
            return _extract_docs(data)

        try:
            return await asyncio.wait_for(_run(), timeout=effective_ceiling)
        except Exception:
            logger.warning("brazil_moh camoufox fallback failed", exc_info=True)
            return []

    async def _lookup_record(
        self, record_id: str
    ) -> tuple[BrazilGuideline | None, BvsErrorKind | None]:
        """Resolve one record by its Solr id. Returns (record, error_kind).

        Second element is a ``BvsErrorKind`` on failure and ``None`` on
        success/not-found.
        """
        # The id is caller-controlled; escape it so a quote or backslash
        # cannot terminate the id:"..." phrase and rewrite the query.
        escaped = record_id.replace("\\", "\\\\").replace('"', '\\"')
        resp = await self.http_client.get(
            BVS_SEARCH_URL,
            headers=BVS_HEADERS,
            params={"q": f'id:"{escaped}"', "output": "json", "count": 5},
            retryable_statuses=_BVS_RETRYABLE_STATUSES,
        )
        if resp is None:
            failure = getattr(self.http_client, "last_failure", None)
            return None, _classify_failure(failure)
        try:
            data = resp.json()
        except ValueError:
            if self.http_client.is_unexpected_html(
                resp
            ) or self.http_client.is_challenge_html(resp):
                return None, "cdn_challenge"
            return None, "backend_error"
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
            return None, None
        return _build_record(docs[0]), None

    async def _extract_pdf_text(
        self, document_url: str
    ) -> tuple[str, BvsErrorKind | None]:
        """Fetch and extract the document PDF. Returns (text, error_kind).

        ``error_kind`` is a ``BvsErrorKind`` on failure and ``None`` on
        success. Gates on content-type explicitly. ``get_bytes`` is not used
        here: its HTML guard only catches Cloudflare-style challenge pages, so a
        plain "Estamos em manutenção" WAF page would reach the PDF parser.

        Redirects are followed (the BVS hosts hand off between themselves),
        but the response's final URL is re-checked against the allowlist so
        a redirect cannot carry the fetch off-host.

        Transient 5xx retries inside this call
        (``_BVS_RETRYABLE_STATUSES``): the document host answers per request
        rather than per outage, so the attempt after a 5xx often returns the
        document. The ladder is bounded in attempt count only
        (``AsyncHttpClient.max_retries``), never in elapsed time: it does not
        consult the budget left. Against a host that is genuinely down, the
        attempts cost roughly ``max_retries x TTFB`` plus backoff, which
        exceeds ``brazil_fulltext_timeout_s``, so the caller's
        ``asyncio.wait_for`` is what ends the call and the result is a
        ``timeout`` rather than a classified ``origin_outage``. That is the
        accepted cost of recovering the far more common transient 5xx.
        """
        if not _is_allowed_host(document_url):
            return "", None
        # Pick headers by hostname, not by substring: "gov.br" appearing in
        # a query string or path on a non-gov host must not match.
        try:
            host = (urllib.parse.urlparse(document_url or "").hostname or "").lower()
        except ValueError:
            host = ""
        headers = GOVBR_HEADERS if host.endswith("gov.br") else BVS_HEADERS
        resp = await self.http_client.get(
            document_url, headers=headers, retryable_statuses=_BVS_RETRYABLE_STATUSES
        )
        if resp is None:
            failure = getattr(self.http_client, "last_failure", None)
            return "", _classify_failure(failure)
        if not _is_allowed_host(str(resp.url)):
            logger.info(
                "brazil_moh full text redirected off the allowed hosts (%s)",
                str(resp.url),
            )
            return "", "backend_error"
        content_type = resp.headers.get("content-type", "").lower()
        if "application/pdf" not in content_type:
            logger.info(
                "brazil_moh full text is not a PDF (content-type=%r)", content_type
            )
            return "", "backend_error"
        try:
            # Unbounded here: the ceiling is applied at cache/serve time so
            # the pre-truncation length survives as ``total_chars``.
            return pdf_bytes_to_text(resp.content), None
        except Exception as exc:
            logger.warning("brazil_moh PDF extraction failed: %s", exc)
            return "", "backend_error"

    async def _serve_local_text(
        self,
        cache_key: str,
        base: dict[str, Any],
        record: BrazilGuideline,
        max_chars: int | None,
        query: str | None = None,
        offset: int = 0,
    ) -> tuple[dict[str, Any], CacheMetadata]:
        """Serve a bundled ``local:`` corpus file from disk, offline.

        The full file is read into memory (hundreds of KB, accepted) and the
        Task 1 ``total_chars``/ceiling/cache contract applies unchanged. No
        network, PDF parsing, or BVS lookup happens on this path.
        """

        def _local_error(status: str, error_text: str) -> tuple[dict[str, Any], CacheMetadata]:
            return (
                {**base, "status": status, "error": error_text,
                  "title": record.title, "content_type": "none", "content": "",
                  "abstract_fallback": False},
                CacheMetadata(cached=False, cache_age=0, error=False),
            )

        rel = (record.document_url or "")[len("local:"):]
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            return _local_error("not_found", "invalid local corpus path")
        try:
            data_dir = (Path(__file__).resolve().parent.parent / "data").resolve()
            path = (data_dir / rel).resolve()
            path.relative_to(data_dir)
        except ValueError:
            return _local_error("not_found", "invalid local corpus path")
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _local_error("not_found", "local corpus file missing")
        except (UnicodeDecodeError, OSError) as exc:
            logger.warning("brazil_moh local text read failed (%s): %s", path, exc)
            return (
                {**base, "status": "error", "error": "local text read failed",
                 "title": record.title, "content_type": "none", "content": "",
                 "abstract_fallback": False},
                CacheMetadata(cached=False, cache_age=0, error=True),
            )
        total_chars = len(text)
        payload = {
            **base, "status": "success", "title": record.title,
            "content_type": "text",
            "content": text[:MAX_FULL_TEXT_CHARS],
            "total_chars": total_chars,
            "abstract_fallback": False,
        }
        await self.cache.set(cache_key, payload, source="brazil_moh")
        return (
            self._serve_full_text(payload, max_chars, query=query, offset=offset),
            CacheMetadata(cached=False, cache_age=0, error=False),
        )

    async def get_full_text(
        self,
        record_id: str,
        max_chars: int | None = None,
        query: str | None = None,
        offset: int = 0,
    ) -> tuple[dict[str, Any], CacheMetadata]:
        """Fetch one document's full text under the published full-text ceiling.

        Budget: ``brazil_fulltext_timeout_s`` bounds the record lookup plus
        the PDF fetch, kept separate from the search-chain budget above.
        A timeout surfaces as ``status=error`` with ``error_kind=timeout``
        in the metadata and is never cached. When the PDF cannot be
        retrieved but the record carries an abstract, the abstract is served with
        ``abstract_fallback=True`` and ``content_type="abstract"`` -- an
        explicit flag, never a silent substitution (S2.4).
        """
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
                 "title": "", "content_type": "none", "content": "",
                 "abstract_fallback": False},
                CacheMetadata(cached=False, cache_age=0, error=True),
            )

        cache_key = f"brazil_moh_fulltext:{CACHE_SCHEMA}:{normalized}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            payload = self._serve_full_text(
                cached_data, max_chars, query=query, offset=offset
            )
            payload.setdefault(
                "abstract_fallback", payload.get("content_type") == "abstract"
            )
            # Replay the kind stored with the row. A degraded payload lives
            # for DEGRADED_RESULT_TTL_SECONDS, so without this every request
            # behind the first one in that window reports a clean result.
            return payload, CacheMetadata(
                cached=True,
                cache_age=meta.cache_age,
                error=False,
                error_kind=cached_data.get(_CACHED_ERROR_KIND_KEY, ""),
            )

        ceiling = float(self.settings.brazil_fulltext_timeout_s)

        def _timeout_result(title_str: str) -> tuple[dict[str, Any], CacheMetadata]:
            return (
                {**base, "status": "error", "error": "full text fetch timed out",
                 "title": title_str, "content_type": "none", "content": "",
                 "abstract_fallback": False},
                CacheMetadata(
                    cached=False, cache_age=0, error=True,
                    error_kind="timeout", timeout=True,
                ),
            )

        async def _resolve() -> tuple[
            BrazilGuideline | None, bool, dict[str, Any], CacheMetadata | None
        ]:
            record = await self.pcdt_engine.get_guideline(normalized)
            if record is None:
                record = await self.az_engine.get_guideline(normalized)
            if record is not None:
                return record, False, {}, None
            record, error_kind = await self._lookup_record(normalized)
            if error_kind:
                return (
                    None,
                    True,
                    {**base, "status": "error", "error": "bvs request failed",
                     "title": "", "content_type": "none", "content": "",
                     "abstract_fallback": False},
                    CacheMetadata(
                        cached=False, cache_age=0, error=True,
                        error_kind=error_kind or "backend_error",
                    ),
                )
            if record is None:
                return (
                    None,
                    False,
                    {**base, "status": "not_found", "error": "no record for id",
                     "title": "", "content_type": "none", "content": "",
                     "abstract_fallback": False},
                    CacheMetadata(
                        cached=False, cache_age=0, error=False,
                        error_kind="successful_empty",
                    ),
                )
            return record, False, {}, None

        budget_start = time.monotonic()
        try:
            if ceiling > 0:
                record, errored, early_payload, early_meta = await asyncio.wait_for(
                    _resolve(), timeout=ceiling
                )
            else:
                record, errored, early_payload, early_meta = await _resolve()
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "brazil_moh full text lookup for %r exceeded its %.1fs budget",
                normalized,
                ceiling,
            )
            return _timeout_result("")
        if early_meta is not None:
            return early_payload, early_meta

        base["document_url"] = record.document_url
        if record.document_url.startswith("local:"):
            return await self._serve_local_text(
                cache_key, base, record, max_chars, query=query, offset=offset
            )
        if ceiling > 0:
            remaining = ceiling - (time.monotonic() - budget_start)
            if remaining <= 0:
                logger.warning(
                    "brazil_moh full text fetch for %r exceeded its %.1fs budget",
                    normalized,
                    ceiling,
                )
                return _timeout_result(record.title)
        else:
            remaining = ceiling
        try:
            if ceiling > 0:
                pdf_text, error_kind = await asyncio.wait_for(
                    self._extract_pdf_text(record.document_url), timeout=remaining
                )
            else:
                pdf_text, error_kind = await self._extract_pdf_text(record.document_url)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "brazil_moh full text fetch for %r exceeded its %.1fs budget",
                normalized,
                ceiling,
            )
            return _timeout_result(record.title)
        errored = error_kind is not None

        if pdf_text:
            source_text = pdf_text
            content_type = "pdf"
            abstract_fallback = False
        elif record.abstract and not record.abstract_synthetic:
            source_text = record.abstract
            content_type = "abstract"
            abstract_fallback = True
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
                 "title": record.title, "content_type": "none", "content": "",
                 "abstract_fallback": False},
                CacheMetadata(
                    cached=False, cache_age=0, error=errored,
                    # The classified kind, not a blanket backend_error: an
                    # origin_outage reported here would count against a
                    # caller-side breaker the taxonomy exempts it from.
                    error_kind=error_kind or "successful_empty",
                ),
            )

        total_chars = len(source_text)
        # Bounded before it is cached: an unbounded extraction would write
        # a multi-megabyte row into the shared cache for a long manual.
        result = {
            "content_type": content_type,
            "content": source_text[:MAX_FULL_TEXT_CHARS],
            "total_chars": total_chars,
            "abstract_fallback": abstract_fallback,
        }
        # The kind is written into the row itself so a later cache hit reports
        # the same degradation this caller sees; _serve_full_text strips it
        # from what either caller receives.
        payload = {
            **base, "status": "success", "title": record.title, **result,
            _CACHED_ERROR_KIND_KEY: error_kind or "ok",
        }
        if not errored:
            await self.cache.set(cache_key, payload, source="brazil_moh")
        else:
            # A PDF fetch that failed and fell back to the abstract is
            # still cached -- at the shorter degraded TTL, so the next
            # request retries the PDF instead of serving a stale fallback
            # indefinitely.
            await self.cache.set(
                cache_key, payload, source="brazil_moh",
                ttl=DEGRADED_RESULT_TTL_SECONDS,
            )
        return (
            self._serve_full_text(payload, max_chars, query=query, offset=offset),
            CacheMetadata(
                cached=False, cache_age=0, error=False,
                error_kind=error_kind or "ok",
            ),
        )

    @staticmethod
    def _serve_full_text(
        payload: dict[str, Any],
        max_chars: int | None,
        query: str | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        # ``max_chars`` is caller-supplied and stays clamped to the storage
        # ceiling on the upper side; when it is None the serving default
        # applies, not the ceiling, so a plain get_full_text call cannot
        # return a whole multi-megabyte manual in one response. Targeted
        # reads use ``query`` (passages) or ``offset`` (paging); without
        # either the response is the serving-budget head cut.
        # See medical.passages.serve_body.
        limit = (
            DEFAULT_SERVING_CHARS
            if max_chars is None
            else min(max(1, max_chars), MAX_FULL_TEXT_CHARS)
        )
        stored = payload.get("content", "")
        # Old cache rows predate ``total_chars`` and degrade to the stored
        # length (truncation at the ceiling reads as False) — accepted.
        total = payload.get("total_chars", len(stored))
        served = {**payload, **serve_body(stored, total, limit, query=query, offset=offset),
                  "total_chars": total}
        # The stored error kind is cache bookkeeping, not part of the tool's
        # response shape.
        served.pop(_CACHED_ERROR_KIND_KEY, None)
        return served
