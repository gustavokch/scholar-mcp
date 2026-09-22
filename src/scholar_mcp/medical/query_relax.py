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
"""

import re

from scholar_mcp.medical.ranking import PORTUGUESE_STOPWORDS, normalize_portuguese
from scholar_mcp.ranking import _STOPWORDS as ENGLISH_STOPWORDS

# Leading-token windows tried after the full query, longest first. Shorter
# than 3 tokens a PubMed query is noise, so the schedule floors there; the
# full query itself may be shorter and is always kept (floor 2 overall).
RELAX_WINDOW_SIZES = (6, 5, 4, 3)

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

    Folding and the Portuguese stopword set are reused from
    ``medical.ranking`` so a term scored as substantive here is substantive
    there too; the English stopword set is ``ScoringEngine``'s, so both
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
    """Successively relaxed variants: full, then 6/5/4/3 leading tokens.

    Entries are deduplicated (a 5-token query yields ``[5, 4, 3]``, not
    ``[5, 6, 5, 4, 3]``) and floored at 2 tokens, except that the full
    query itself is always kept -- a 1-token query cannot relax further.
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
