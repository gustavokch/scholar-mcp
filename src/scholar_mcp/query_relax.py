"""Query relaxation ladder for PubMed-backed searches (ENAMED misses plan B1).

PubMed ANDs every term of a natural-language query, so an 8-15-token agent
query over-constrains esearch to zero hits while a 3-5-token prefix of the
same query returns dozens (measured live 2026-09-22). The ladder below
walks a long query down to its leading content tokens and stops at the
first step that returns hits.

Both PubMed clients share this schedule: ``MedicalPubMedClient`` walks it
internally when ``relax=True``; ``GuidelinesEngine`` walks it itself (with
``relax=False`` on the client) so its ``[pt]``/``[tiab]`` filters survive
on every step; ``PubMedProvider`` rebuilds its author/journal/date filters
around each relaxed variant.

Lives in core, not ``medical/``: the ladder and ``content_overlap_count``
carry no medical content, and ``resolver.py`` and ``providers/pubmed.py``
need them on the scholar path -- a core module importing from ``medical/``
inverts the package dependency.
"""

import re
import unicodedata

# Stored already accent-folded, because tokenization folds before it consults
# this set -- an accented member would never be matched. Shared by
# ``medical.ranking`` (client-side re-ranking) and outbound query composition
# in ``medical/brazil_moh.py``. One source of truth keeps a term from being
# scored as substantive while being dropped from the query, or the reverse.
PORTUGUESE_STOPWORDS = frozenset({
    "a", "ao", "aos", "as", "com", "como", "da", "das", "de", "do", "dos",
    "e", "em", "entre", "na", "nao", "nas", "no", "nos", "o", "os", "ou",
    "para", "pela", "pelo", "por", "que", "se", "sem", "sob", "sobre",
    "um", "uma", "umas", "uns",
})


def normalize_portuguese(text: str | None) -> str:
    """Fold Portuguese diacritics to ASCII and lowercase.

    NFKD splits an accented character into its base plus a combining mark;
    encoding to ASCII with ``ignore`` then drops the marks. This also discards
    any non-Latin script, which is acceptable: a record whose text is entirely
    non-Latin cannot match a Portuguese query.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return folded.lower()


# Declared locally rather than imported: the identically-named copy in
# scholar_mcp.ranking is private to its module (see medical/ranking.py for
# the documented pattern). The two sets are intentionally identical --
# normalization has already reduced the text to ASCII here.
ENGLISH_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "is",
    "of", "on", "or", "the", "to", "with",
})

# Leading-token windows tried after the full query, longest first. Shorter
# than 3 tokens a PubMed query is noise, so the schedule floors there; the
# full query itself may be shorter and is always kept (floor 2 overall).
# Three rungs, not four: MAX_RELAX_EXTRA_CALLS below allows three extra
# esearch calls, so a fourth rung (e.g. a 6-token window) would be
# structurally unreachable -- the budget, not the schedule, binds.
RELAX_WINDOW_SIZES = (5, 4, 3)

# At most this many extra esearch calls beyond the initial one. NCBI allows
# 3 req/s unauthenticated, so the extra requests stay bounded.
MAX_RELAX_EXTRA_CALLS = 3

_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")
# Query-language punctuation neutralized the same way _search_cochrane does
# (medical/databases.py): a stray quote or paren in an agent-supplied query
# is E-utility syntax, not part of the topic.
_QUERY_PUNCT_RE = re.compile(r'["()]')
_BOOLEAN_RE = re.compile(r"\b(?:OR|AND)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def normalize_query(query: str) -> str:
    """Strip query-language operators, keeping the topic terms.

    Quotes and parentheses are E-utility syntax; bare ``OR``/``AND`` tokens
    are boolean operators PubMed would otherwise AND as literal terms.
    Removing them leaves the terms, which esearch ANDs by default.
    """
    text = _QUERY_PUNCT_RE.sub(" ", query or "")
    text = _BOOLEAN_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def content_tokens(query: str) -> list[str]:
    """Accent-folded, stopword-stripped content tokens in order, deduplicated.

    The Portuguese stopword set and folding are shared with ``medical.ranking``
    so a term scored as substantive here is substantive there too; the
    English stopword set is a local copy of ``ScoringEngine``'s, so both
    query languages relax the same way.
    """
    norm = normalize_portuguese(normalize_query(query))
    tokens = [
        t
        for t in _WORD_SPLIT_RE.split(norm)
        if len(t) >= 2 and t not in PORTUGUESE_STOPWORDS and t not in ENGLISH_STOPWORDS
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def relax_ladder(query: str) -> list[str]:
    """Successively relaxed variants: full, then 5/4/3 leading tokens.

    Entries are deduplicated (an 8-token query yields ``[8, 5, 4, 3]``, a
    5-token query ``[5, 4, 3]``) and floored at 2 tokens, except that the
    full query itself is always kept -- a 1-token query cannot relax
    further.
    A query with no content tokens falls back to the normalized raw query
    so the caller still issues one attempt.
    """
    tokens = content_tokens(query)
    if not tokens:
        fallback = normalize_query(query)
        return [fallback] if fallback else []
    full = " ".join(tokens)
    ladder = [full]
    for n in RELAX_WINDOW_SIZES:
        if len(tokens) > n:
            ladder.append(" ".join(tokens[:n]))
    ladder = [q for q in ladder if len(q.split()) >= 2] or [full]
    seen: set[str] = set()
    unique: list[str] = []
    for q in ladder:
        if q not in seen:
            seen.add(q)
            unique.append(q)
    return unique


def content_overlap_count(query: str, *texts: str | None) -> int:
    """Number of query content tokens present in the given texts.

    Both sides run through ``content_tokens``, so accents and stopwords
    compare equally. Used by the CrossRef top-up hygiene gate (plan B2):
    a record is kept when the overlap is >= 1.
    """
    query_terms = set(content_tokens(query))
    if not query_terms:
        return 0
    doc_terms: set[str] = set()
    for text in texts:
        doc_terms.update(content_tokens(text or ""))
    return len(query_terms & doc_terms)
