# Query-time reranking fix + citation-aware answer checks

Date: 2026-08-31

## Problem

1. `ScoringEngine.score_candidates` (`src/scholar_mcp/ranking.py`) never
   receives the search query. Its "relevance" term is
   `1/sqrt(source_rank+1)` — purely the candidate's position in the
   upstream provider's result list. Two papers with identical source
   rank but wildly different textual relevance to the query score
   identically. `medical/ranking.py` already solves this with a
   lexical token-coverage score, but the logic is duplicated there and
   absent from the main scholar path.
2. There is no way for an agent consuming `search_papers` /
   `get_full_text` to verify that a drafted answer's citations are
   actually supported by the papers they cite. No tool exists for
   this today.

## Scope

- Fix relevance scoring in the main ranking path to use real
  query-text relevance (lexical), blended with source position as a
  secondary signal.
- Deduplicate the lexical scoring logic between `ranking.py` and
  `medical/ranking.py`.
- Add a new `check_citations` MCP tool for claim-to-source grounding
  checks, reusing the same lexical scoring primitive.
- No new dependencies (no embeddings/ML libs) — lexical only, matching
  the project's existing zero-ML-dependency footprint.
- No free-text citation parsing (`[1]`, `(Author, 2020)`, etc.) —
  `check_citations` takes structured claim/identifier pairs supplied
  by the calling agent.

## Design

### 1. Shared lexical scoring primitive

Move the tokenizer and coverage-scoring logic currently private to
`medical/ranking.py` (`_tokenize`, `_WORD_SPLIT_RE`, `_STOPWORDS`, the
title/abstract coverage formula) into `ranking.py` as reusable
functions/staticmethods on `ScoringEngine`:

- `ScoringEngine.tokenize(text: str | None) -> list[str]`
- `ScoringEngine.text_coverage(query_terms: list[str], title: str | None, abstract: str | None) -> float`
  (title terms weighted 2x abstract terms, same formula as today's
  medical path)
- `ScoringEngine.best_matching_sentence(query_terms: list[str], text: str) -> tuple[str, float]`
  (new — sentence-level lookup for citation-check evidence snippets;
  simple regex split on `.!?` boundaries, no NLP dependency)

`medical/ranking.py` imports `tokenize`/`text_coverage` from
`ranking.py` instead of defining its own. Its weight profile (0.7
relevance / 0.3 recency, `SOURCE_POSITION_WEIGHT`) and output
(`article.score`, sort order) are unchanged — this is a
behavior-preserving refactor for the medical path.

### 2. Main ranking path: real relevance

`ScoringEngine.calculate_relevance` changes from:

```python
calculate_relevance(rank_idx: int) -> float
```

to a lexical/position blend:

```python
calculate_relevance(rank_idx: int, query_terms: list[str], title, abstract, position_weight: float) -> float:
    lexical = text_coverage(query_terms, title, abstract)
    position = 1.0 / math.sqrt(rank_idx + 1)
    return (1.0 - position_weight) * lexical + position_weight * position
```

`position_weight` defaults to 0.25 — multi-source merges (PubMed +
CrossRef top-up in `resolver.search`'s `auto` mode) still carry
information in source order (e.g. MeSH-term matches invisible to
title/abstract text), so position stays a secondary signal rather than
being dropped.

`ScoringEngine.score_candidates` gains a required `query: str`
parameter, threaded through:

- `score_candidates(papers, weights, query, current_year=None)`
- `RankingPipeline.rank_papers(papers, query, weights=None, top_n=10)`
- `WaterfallResolver.search(...)` passes its `query` arg into
  `rank_papers`

New `Settings` field (`config.py`, following existing
`ranking_weight_*` convention):

- `ranking_position_weight: float = 0.25` — env `RANKING_POSITION_WEIGHT`

No change to citation or recency scoring, candidate-pool sizing, or
the `enrich_citations` pipeline — out of scope, not broken.

### 3. `check_citations` MCP tool

New file `src/scholar_mcp/citation_check.py`:

```python
async def check_citations(
    resolver: WaterfallResolver,
    claims: list[dict],       # [{"text": str, "identifier": str}, ...]
    deep: bool = False,
    settings: Settings = ...,
) -> list[dict]
```

Per claim, isolated (one failure never aborts the batch, mirrors
`resolve_full_text_batch`):

1. Resolve `identifier` via `resolver.get_metadata` (abstract, default)
   or `resolver.resolve_full_text` when `deep=True` (slower, full
   text). Resolution failure -> verdict `NOT_FOUND`, skip scoring.
2. Tokenize claim text -> `query_terms`.
3. `coverage = ScoringEngine.text_coverage(query_terms, title, content)`.
4. `evidence_sentence, _ = ScoringEngine.best_matching_sentence(query_terms, content)`.
5. Bucket by `coverage`:
   - `>= citation_check_supported_threshold` -> `SUPPORTED`
   - `>= citation_check_weak_threshold` -> `WEAK`
   - else -> `UNSUPPORTED`

Return per claim: `{"identifier", "verdict", "coverage_score",
"best_evidence_sentence", "resolved_title"}`.

Batch capped at 25 claims (`ValueError` above that, mirrors
`get_full_text_batch`'s existing limit pattern in `server.py`).

New `Settings` fields:

- `citation_check_supported_threshold: float = 0.5` — env `CITATION_CHECK_SUPPORTED_THRESHOLD`
- `citation_check_weak_threshold: float = 0.15` — env `CITATION_CHECK_WEAK_THRESHOLD`

`server.py` registers:

```python
@mcp.tool()
async def check_citations(claims: list[dict[str, str]], deep: bool = False) -> list[dict[str, Any]]:
    """Verify each claim is supported by its cited paper (claim-to-source grounding).

    Args:
        claims: List of {"text": <claim sentence>, "identifier": <DOI/PMID/PMCID/arXiv ID>}, max 25.
        deep: Fetch full text instead of abstract only (slower, more thorough).
    """
```

## Error handling

- Empty/whitespace `query` in `search()` -> lexical coverage is 0 for
  every candidate -> ranking degrades gracefully to position +
  citation + recency (z-scoring naturally washes out an all-zero
  term; no special-case branch needed).
- `check_citations`: per-claim `try/except`; unresolvable identifier
  or fetch error -> `NOT_FOUND` verdict for that claim only.
- `check_citations`: batch size > 25 -> single error response, same
  shape as `get_full_text_batch`'s existing over-limit response.

## Testing

- `ranking.py`: unit tests for `tokenize`/`text_coverage`/
  `best_matching_sentence`. Regression test: a query-matching
  low-source-rank paper must outrank a non-matching high-source-rank
  paper after the fix (the exact gap being closed).
- `medical/ranking.py`: existing test suite must pass unchanged
  (behavior-preserving refactor — same weights, same output ordering).
- `citation_check.py`: one test per verdict bucket, sentence-extraction
  correctness, 25-claim cap enforcement, isolated-failure handling
  (one bad identifier doesn't affect other claims' results).
- `resolver.py` / `server.py`: integration test confirming
  `search_papers` still returns correctly ranked results end-to-end
  with the query threaded through to `rank_papers`.
