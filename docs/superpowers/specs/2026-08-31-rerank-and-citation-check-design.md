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
3. Ranking has no notion of study rigor (systematic review vs. case
   report), journal standing, or author track record — only relevance
   proxy, raw citation count, and recency.

## Scope

- Fix relevance scoring in the main ranking path to use real
  query-text relevance (lexical), blended with source position as a
  secondary signal.
- Deduplicate the lexical scoring logic between `ranking.py` and
  `medical/ranking.py`.
- Add a new `check_citations` MCP tool for claim-to-source grounding
  checks, reusing the same lexical scoring primitive.
- Add three new ranking signals to the main `search_papers` path:
  evidence grade (study design), journal impact (SJR proxy), author
  authority (last-author h-index).
- No new ML dependencies (no embeddings/ML libs) — lexical only,
  matching the project's existing zero-ML-dependency footprint. One
  static data file (Scimago SJR) added for journal impact.
- No free-text citation parsing (`[1]`, `(Author, 2020)`, etc.) —
  `check_citations` takes structured claim/identifier pairs supplied
  by the calling agent.
- The three new ranking signals apply only to the main `search_papers`
  path. `medical/ranking.py` (guidelines/pediatrics/databases tools)
  stays lexical-only with no network enrichment — future extension,
  not built now.

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

### 4. Evidence grade (real PubMed data, no new dependency)

PubMed's ESummary JSON already returns `pubtype` (list of strings) per
article — fetched in `providers/pubmed.py::search` today and
discarded. EFetch XML also carries `<PublicationTypeList>`. Use it
instead of any heuristic.

- Add `PaperMetadata.study_type: str | None` (raw top pubtype string)
  and `PaperMetadata.evidence_grade: str | None` (normalized tier).
- Oxford CEBM-style ladder, mapped from `pubtype` values:
  - `1a`: "Meta-Analysis", "Systematic Review"
  - `1b`: "Randomized Controlled Trial"
  - `2b`: "Observational Study", "Comparative Study", "Multicenter Study"
  - `3b`: "Case-Control Studies"
  - `4`: "Case Reports"
  - `5`: "Review", "Editorial", "Comment", "Practice Guideline"
  - `None`: no pubtype match, or non-PubMed source (CrossRef/S2/arXiv
    lack this field) — neutral, no bonus or penalty
- Grade -> numeric feature: `evidence_score = 1.0 / grade_rank` where
  `grade_rank` is the ladder position (`1a`=1, `1b`=2, `2b`=3, `3b`=4,
  `4`=5, `5`=6); `None` -> `0.0`. Z-scored like the other features.
- New `RankingWeights.evidence_grade` term.

### 5. Journal impact (static Scimago SJR dataset)

No free official Journal Impact Factor (Clarivate) API exists. Use
Scimago Journal Rank (open data) as a proxy, shipped as a static
in-repo file rather than a live external call:

- `src/scholar_mcp/data/scimago_sjr.json`, built once from Scimago's
  public downloadable CSV (journal-level SJR indicator + ISSN). Keyed
  by ISSN (primary) with normalized-journal-name as fallback key.
  Accompanying `SOURCES.md` records the dataset version/year and its
  usage terms; `scripts/update_scimago_data.py` regenerates the JSON
  from a freshly downloaded CSV — no runtime network dependency.
- Add `PaperMetadata.issn: str | None`. Populate from fields already
  present but unused in provider responses: PubMed ESummary
  `issn`/`essn`, CrossRef metadata `ISSN` list, OpenAlex
  `host_venue.issn_l`.
- Lookup: `issn` -> SJR value if present; else normalize `venue`
  (lowercase, strip punctuation) and try a name match; else `None`
  (neutral).
- New feature: `raw_impact = log(1 + sjr_value)` if found else `0.0`,
  z-scored like citations. New `RankingWeights.journal_impact` term.
- Loaded once at process start as a module-level singleton dict — zero
  added runtime latency or dependency.

### 6. Author authority (last-author h-index via OpenAlex)

Reuses the existing citation-enrichment batch call rather than a
separate per-paper author-name search, which avoids name-disambiguation
risk entirely:

- `OpenAlexProvider.fetch_citation_counts_batch` already fetches each
  work by DOI/PMID; each work's `authorships[].author.id` is an
  unambiguous OpenAlex author ID, currently discarded. Extend that
  method to also return, per paper, the **last** authorship's
  `author.id`.
- New `OpenAlexProvider.fetch_author_h_indices_batch(author_ids: list[str]) -> dict[str, int]`:
  one batched call to `/authors?filter=openalex_id:ID1|ID2|...`,
  reading `summary_stats.h_index`. Cached in the existing `TTLCache`
  (`hidx:<openalex_author_id>`), same pattern as citation-count caching
  in `RankingPipeline`.
- Only populated when the paper has a DOI/PMID resolvable in OpenAlex
  (same precondition citation enrichment already has); else `None`,
  neutral.
- New feature: `raw_authority = log(1 + h_index)` if found else `0.0`,
  z-scored. New `RankingWeights.author_authority` term.
- Runs inside `RankingPipeline.enrich_citations`'s existing OpenAlex
  batch step — one extra field read, one extra batched HTTP call, no
  new network round-trip pattern.

### 7. Rebalanced weights

`RankingWeights` gains 3 fields; defaults rebalanced so the total
stays 1.0:

| term | old default | new default |
|---|---|---|
| relevance | 0.40 | 0.30 |
| citations | 0.30 | 0.20 |
| recency | 0.30 | 0.15 |
| evidence_grade | — | 0.20 |
| journal_impact | — | 0.10 |
| author_authority | — | 0.05 |

All overridable via `Settings`/env vars, same convention as existing
weights: `RANKING_WEIGHT_EVIDENCE_GRADE`, `RANKING_WEIGHT_JOURNAL_IMPACT`,
`RANKING_WEIGHT_AUTHOR_AUTHORITY`.

## Error handling

- Empty/whitespace `query` in `search()` -> lexical coverage is 0 for
  every candidate -> ranking degrades gracefully to position +
  citation + recency (z-scoring naturally washes out an all-zero
  term; no special-case branch needed).
- `check_citations`: per-claim `try/except`; unresolvable identifier
  or fetch error -> `NOT_FOUND` verdict for that claim only.
- `check_citations`: batch size > 25 -> single error response, same
  shape as `get_full_text_batch`'s existing over-limit response.
- Evidence grade: unrecognized/missing `pubtype`, or non-PubMed
  source -> `evidence_grade=None`, feature contributes `0.0` (neutral,
  never penalized).
- Journal impact: ISSN and name both miss the SJR table -> feature
  `0.0` (neutral). Corrupt/missing `scimago_sjr.json` at startup ->
  log a warning, treat the table as empty (ranking degrades to the
  other 5 signals, never crashes).
- Author authority: no DOI/PMID, no OpenAlex work match, or OpenAlex
  batch call failure -> feature `0.0` (neutral), mirrors existing
  citation-enrichment fallback (`enrich_citations`'s own `except`
  blocks already swallow OpenAlex failures the same way).

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
- Evidence grade: unit tests mapping each `pubtype` string set to its
  expected ladder tier, including the `None`/non-PubMed neutral case.
- Journal impact: unit tests for ISSN-hit, name-fallback-hit, and
  miss (neutral) lookups; a test asserting a missing/corrupt
  `scimago_sjr.json` doesn't crash the ranking pipeline.
- Author authority: unit test for the OpenAlex last-authorship-ID
  extraction and the h-index batch fetch, plus a neutral-fallback
  test when no DOI/PMID is present.
- Full-pipeline regression test: a paper with SR/MA design, high SJR
  journal, and high-h-index last author outranks an otherwise
  equal-relevance/citation/recency paper lacking all three signals.
