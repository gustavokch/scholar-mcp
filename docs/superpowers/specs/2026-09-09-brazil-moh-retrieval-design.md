# Brazilian Ministry of Health Retrieval Improvements — Design

Date: 2026-09-09
Status: In review (revision 3)

## Purpose

Improve recall and ranking precision for the Brazilian Ministry of Health (`brazil-moh`) guideline search tooling.

This design introduces:

1. Portuguese-aware tokenization and re-ranking for `BrazilGuideline` records.
2. Automatic query relaxation (two-stage `AND` -> `OR` search) in `scholar_mcp.medical.brazil_moh` when strict conjunction yields zero Brazilian records.

It does so by **generalizing the existing ranking function rather than duplicating it**. `scholar_mcp.medical.ranking.rank_medical_articles` already implements the target scoring contract (`0.7 * relevance + 0.3 * recency`, a `0.35` source-position share, `1/sqrt(idx + 1)` position prior, title weighted 2x abstract, stable sort on `(-score, source_index)`). The only genuine difference for the Brazilian corpus is the tokenizer and the choice of text fields. Both become injected parameters.

## Scope

In scope:

- Portuguese text normalization (Unicode NFKD accent folding, lowercasing, stopword stripping) in `scholar_mcp/medical/ranking.py`.
- An optional `tokenizer` parameter on `ScoringEngine.text_coverage` in `src/scholar_mcp/ranking.py`, defaulting to current behaviour.
- A shared, tokenizer-agnostic ranking core in `scholar_mcp/medical/ranking.py`, with `rank_medical_articles` and a new `rank_brazil_guidelines` as thin wrappers over it.
- Query relaxation in `BrazilMoHEngine.search_guidelines`: fallback from `AND` to `OR` when the strict query yields zero Brazilian records and the query carries two or more usable tokens.
- Stripping Portuguese stopwords in `_usable_tokens`, so they are excluded from **both** the strict and the relaxed outbound query.
- Populating `BrazilGuideline.score` in search results.
- Including `title_en` in title coverage and `mesh_subjects` in secondary coverage for Brazilian records.
- Correcting the existing slice-before-rank ordering in `search_guidelines`.
- Updating the `brazil_moh.py` module docstring and the `BrazilGuideline.score` docstring, both of which currently document the *absence* of ranking as a deliberate decision.
- Unit tests in `tests/medical/`.

Out of scope:

- Any change to the scoring contract itself (weights, half-life, position prior) for the existing `MedicalArticle` path. `rank_medical_articles` must remain behaviourally identical.
- New scoring signals (evidence grade, citation counts, MeSH matching).
- External NLP dependencies, stemmers, or ML models.
- Changes to the MCP tool schema or parameters in `src/scholar_mcp/server.py`.
- Server-side Solr relevance tuning (`mm`, boosts, `fq`). The BVS endpoint silently ignores `fq`, and its handling of `mm` is unverified.

## Architecture & Data Flow

### 1. Tokenizer injection in `ScoringEngine` (`src/scholar_mcp/ranking.py`)

`ScoringEngine.text_coverage` currently hardcodes `ScoringEngine.tokenize` for both the query terms and the document fields. That is the sole reason a Portuguese coverage function would otherwise have to be duplicated.

Change:

```python
@staticmethod
def text_coverage(
    query_terms: list[str],
    title: str | None,
    abstract: str | None,
    tokenizer: Callable[[str | None], list[str]] | None = None,
) -> float:
```

- `tokenizer` resolves to `ScoringEngine.tokenize` when `None`.
- All existing call sites are unchanged and keep identical behaviour.
- `_TITLE_WEIGHT` / `_ABSTRACT_WEIGHT` and the `min(1.0, title_cov + ratio * abstract_cov)` formula are untouched.
- `Callable` is imported from `collections.abc`.

This is the only edit to `src/scholar_mcp/ranking.py`.

### 2. Portuguese normalization (`scholar_mcp/medical/ranking.py`)

- `normalize_portuguese(text: str | None) -> str`:
  - Returns `""` for falsy input.
  - Applies NFKD decomposition, drops the combining marks, lowercases:
    `unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()`.
  - Note the ASCII `encode(..., "ignore")` also discards any non-Latin script. Acceptable: the corpus is Portuguese, and a record whose title is entirely non-Latin cannot match a Portuguese query anyway.

- `PORTUGUESE_STOPWORDS: frozenset[str]`, stored **already accent-folded** (the tokenizer folds before it consults the set):
  `a`, `ao`, `aos`, `as`, `com`, `como`, `da`, `das`, `de`, `do`, `dos`, `e`, `em`, `entre`, `na`, `nao`, `nas`, `no`, `nos`, `o`, `os`, `ou`, `para`, `pela`, `pelo`, `por`, `que`, `se`, `sem`, `sob`, `sobre`, `um`, `uma`, `umas`, `uns`.

  This set has two consumers: client-side re-ranking (§3) and outbound query composition (§4). It is the single source of truth for both, so a term can never be scored as substantive while being dropped from the query, or vice versa.

- `tokenize_portuguese(text: str | None) -> list[str]`:
  - Normalizes with `normalize_portuguese`, splits on a module-local `_WORD_SPLIT_RE = re.compile(r"[^a-z0-9]+")`, and keeps tokens where `len(token) >= 2 and token not in PORTUGUESE_STOPWORDS`. The pattern is declared locally rather than imported from `scholar_mcp.ranking`, whose copy is private to that module; the two are intentionally identical, since normalization has already reduced the text to ASCII.
  - Signature and filtering rules deliberately mirror `ScoringEngine.tokenize` so the two are interchangeable as an injected `tokenizer`.

### 3. Shared ranking core (`scholar_mcp/medical/ranking.py`)

A private generic replaces the body of `rank_medical_articles`:

```python
class _Rankable(Protocol):
    year: str
    score: float | None

R = TypeVar("R", bound=_Rankable)

def _rank_records(
    records: list[R],
    query: str,
    *,
    tokenizer: Callable[[str | None], list[str]],
    text_fields: Callable[[R], tuple[str, str]],
    position_weight: float,
    current_year: int | None = None,
) -> list[R]:
```

- Empty input returns `[]`.
- `terms = tokenizer(query)`; if empty, returns `list(records)` with `score` left untouched.
- Per record, `text_fields(record)` yields `(title_text, abstract_text)`.
- `lexical = ScoringEngine.text_coverage(terms, title_text, abstract_text, tokenizer=tokenizer)`.
- When `position_weight` is non-zero: `relevance = (1 - position_weight) * lexical + position_weight * ScoringEngine.calculate_relevance(idx)`; otherwise `relevance = lexical`.
- `recency, _ = ScoringEngine.calculate_recency_feature(record.year, current_year=now_year, half_life_years=RECENCY_HALF_LIFE_YEARS, default_age=DEFAULT_AGE_YEARS)`.
- `record.score = RELEVANCE_WEIGHT * relevance + RECENCY_WEIGHT * recency`, assigned in place.
- Sorts stably on `(-score, source_index)`.

Existing module constants (`RELEVANCE_WEIGHT`, `RECENCY_WEIGHT`, `RECENCY_HALF_LIFE_YEARS`, `DEFAULT_AGE_YEARS`, `SOURCE_POSITION_WEIGHT`) are reused unchanged.

Two public wrappers:

- `rank_medical_articles(articles, query, current_year=None, position_weight=0.0)` — signature, docstring intent, and behaviour unchanged. Delegates with `tokenizer=ScoringEngine.tokenize` and `text_fields=lambda a: (a.title, a.abstract)`.

- `rank_brazil_guidelines(guidelines, query, current_year=None)` — delegates with:
  - `tokenizer=tokenize_portuguese`
  - `text_fields=lambda g: (f"{g.title} {g.title_en}", " ".join([g.abstract, *g.mesh_subjects]))`
  - `position_weight=SOURCE_POSITION_WEIGHT` (`0.35`)

  The title field is the **union** of the Portuguese and English titles. `BrazilGuideline.title_en` is populated from Solr `ti_en` (`brazil_moh.py:209`); without it an English query ("dengue treatment") scores zero lexical coverage against a record titled "Tratamento da dengue" whose `title_en` reads "Dengue treatment". Joining with a space is safe because the tokenizer splits on non-alphanumerics, so the result is exactly the union of both token sets. Coverage is a fraction of *query* terms found, so widening the document token set cannot deflate the score of a Portuguese-only match.

  The secondary field is the union of the abstract and the DeCS descriptors. `BrazilGuideline.mesh_subjects` is populated from Solr `mh` (`brazil_moh.py:220`), and non-conventional literature — PCDT, Cadernos de Atenção Básica — routinely carries an empty `ab` while being richly indexed ("Atenção Primária à Saúde"). Without the descriptors those records score on title alone and sink below abstract-bearing records of lower topical fit. The descriptors take the abstract's own weight (`1.0` against title's `2.0`); no new parameter is introduced, and `ScoringEngine.text_coverage` is unchanged beyond the injected tokenizer.

  Accepted cost of that choice: a record carrying 30 descriptors has a far larger secondary token set than a sparse one, so its `abstract_coverage` term inflates relative to a peer of equal topical fit. This is a real thumb on the scale for heavily-indexed records, not a free win. It is bounded by the existing `min(1.0, title_cov + 0.5 * abstract_cov)` cap and outweighed by title coverage, and the recall it buys on abstract-less MoH records is the larger effect. Revisit only if ranking quality regresses on abstract-bearing records.

  `position_weight` is non-zero here because BVS returns a single relevance-sorted list, matching the condition documented on `rank_medical_articles`.

### 4. Query relaxation (`scholar_mcp/medical/brazil_moh.py`)

#### Stopword stripping in `_usable_tokens`

`_usable_tokens` (`brazil_moh.py:141`) currently drops only tokens that sanitize away to nothing and tokens reserved as Solr boolean words. Portuguese stopwords pass straight through into the composed query, and they damage **both** stages:

- Strict. `manejo da dengue` composes to `(manejo AND da AND dengue)`. A record titled "Manejo clínico **da** dengue" matches; a record titled "Manejo **de** dengue" does not. The `AND da` clause silently discards correct records. This is a pre-existing recall bug, independent of relaxation.
- Relaxed. The same `da` as an `OR` clause matches a large share of the Portuguese corpus, flooding the over-fetch pool with records that carry no substantive term and pushing topical matches past `count`.

Change: `_usable_tokens` additionally drops a token whose accent-folded form is in `PORTUGUESE_STOPWORDS`.

Two details are load-bearing:

- **Fold to test, emit unfolded.** `_usable_tokens` does not accent-fold, and `PORTUGUESE_STOPWORDS` is stored folded, so the raw token `à` is absent from the set while folding to `a`, which is present. Membership is therefore tested against `normalize_portuguese(cleaned)`, while the **original** `cleaned` token is what enters the query — the design keeps diacritics outbound because BVS handles Portuguese natively. Only stopword membership is folded; the `len(token) >= 2` floor from `tokenize_portuguese` is *not* applied here, since dropping a short token from the outbound query is a separate decision this design does not take.
- **Applies to both operators, never one.** Stripping only in the relaxed path would make the two stages search different term sets, so the relaxed stage could return records the strict stage could never have matched for a reason unrelated to the operator. That breaks the invariant the two-stage design rests on: relaxation loosens the *operator* and nothing else. Because the strip lives in `_usable_tokens`, both `_build_query` calls and the relaxation gate see the same tokens by construction.

Consequences, both accepted:

- The relaxation gate `len(_usable_tokens(query)) >= 2` now counts *substantive* tokens. `manejo da` yields one, so it must not relax. This follows automatically and is asserted in tests.
- `_usable_tokens` also backs the existing "no searchable tokens" guard (`brazil_moh.py:297`), so a query composed entirely of stopwords ("sobre a") now returns empty with `error=False` instead of searching. Correct: those terms were never going to select on topic.
- Every existing `brazil_moh_search` cache row goes cold, because `composed` changes for any query containing a stopword. Harmless, no key version bump.
- This is a live behaviour change to the strict path, beyond "add relaxation": queries containing stopwords return strictly more records than before. Intended.

Import direction is safe. `scholar_mcp/medical/ranking.py` imports only `medical.models` and `scholar_mcp.ranking`, so `brazil_moh -> medical.ranking` for `normalize_portuguese` and `PORTUGUESE_STOPWORDS` introduces no cycle.

#### Query construction

`_build_query(query: str, collection: str, operator: str = "AND") -> str`:

- `operator` is `"AND"` (default) or `"OR"`; it joins the usable tokens inside their own parenthesized group. Repo style: `"(" + f" {operator} ".join(tokens) + ")"`.
- `BASE_FILTER` and `BRISA_FILTER` remain joined with `AND` regardless of `operator`. Only the user-token group relaxes.
- The token list comes from `_usable_tokens`, so it is stopword-free in both modes.

#### Search workflow in `BrazilMoHEngine.search_guidelines`

1. Validate collection and the no-usable-tokens case exactly as today.
2. `composed_strict = _build_query(query, norm_collection)`. Cache key stays `f"brazil_moh_search:{norm_collection}:{clamped}:{composed_strict}"`. A cache hit returns as today. The relaxed query never appears in the key: it is derived from the same user query, so one user query keeps one cache row.
3. `count = min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)`. Request BVS with `composed_strict`.
4. Parse docs, dedupe by id, filter by `_is_brazilian`. **No slice yet** — see step 7.
5. Zero-hit fallback. Trigger condition, stated precisely: **the record list is empty after the `_is_brazilian` filter**, and `len(_usable_tokens(query)) >= 2` — that is, two or more *substantive* tokens, stopwords already removed. A pool of non-Brazilian Portuguese hits therefore also triggers relaxation, which is the intent — the strict conjunction yielded nothing usable. On trigger:
   - `composed_relaxed = _build_query(query, norm_collection, operator="OR")`.
   - Request BVS with `composed_relaxed` and **the same `count`**.
   - Parse, dedupe, filter by `_is_brazilian`. The result replaces the (empty) strict list.
6. `records = rank_brazil_guidelines(records, query)`.
7. `records = records[:clamped]`. **This slice moves after ranking.** Today it sits at `brazil_moh.py:329`, before any ranking exists; leaving it in place would hand the ranker only `clamped` of the `clamped * OVERFETCH_FACTOR` candidates and discard the rest in BVS order, defeating the over-fetch. The reordering is intentional and is part of this change.
8. Cache the sliced records under the strict cache key.
9. Return `(records, CacheMetadata(cached=False, cache_age=0, error=False))`.

#### Why a bare `OR` and not minimum-should-match

An N-token `OR` matches a record carrying only one token, which without ranking would be a clear precision loss. It is acceptable **only because step 6 now sorts by lexical coverage first**: a record matching one of four tokens sinks below one matching all four. Do not add a `mm` parameter (the endpoint's support is unverified, and it silently ignores `fq`) and do not implement progressive token-dropping — coverage-first ranking already recovers the precision, at a fraction of the complexity.

Note the BVS default operator is OR, so the explicit `OR` group is equivalent to omitting the operator. It is written explicitly for symmetry with the strict path and to keep the composed query self-documenting.

### 5. Docstring corrections

Both of these currently document the absence of ranking as a considered decision, and both become false with this change. Updating them is a deliverable, not a nicety.

- `src/scholar_mcp/medical/brazil_moh.py:14-17` — "Results are served in the order BVS returns them. No re-ranking is applied: `ScoringEngine` does not fold accents or strip Portuguese stopwords, so blending it against a Solr ordering tuned for this corpus would degrade it. `BrazilGuideline.score` is the seam for adding that later." Replace with a description of the two-stage search and the Portuguese-aware re-ranking, recording that accent folding and Portuguese stopwords are what made blending viable, and that the BVS ordering is retained as a weighted prior rather than discarded. The module's existing note that "the default boolean operator is OR, so user tokens are joined with AND" stays true but now needs the relaxation stage alongside it, plus the fact that Portuguese stopwords are stripped from the outbound query in both stages.
- `src/scholar_mcp/medical/models.py:246` — "`score` is reserved for a future ranking pass and is unset in v1." Replace with a statement that `score` is populated by `rank_brazil_guidelines` on the search path.

## Error Handling & Edge Cases

- **Single-token query, zero hits:** no relaxation. The strict and relaxed groups are byte-identical for one substantive token, so a second request would be pure waste. Note this now covers `manejo da`, which reduces to one substantive token.
- **Query with no usable tokens:** unchanged code path, wider trigger — a query of only stopwords ("sobre a") now reaches it and returns early with `error=False`, before any request.
- **Strict request fails** (`resp is None`, or non-JSON): unchanged — early return, `error=True`, nothing cached.
- **Relaxed request fails:** log a warning and return `([], CacheMetadata(cached=False, cache_age=0, error=True))`. Do not cache. Do not fall back to the strict result, which is empty by construction.
- **Zero-hit latency:** a zero-hit multi-token query now costs two sequential BVS round trips. Accepted: the alternative is a speculative parallel `OR` request on every search, which would double load on a host that already 403s the default User-Agent.
- **Empty result caching:** unchanged. A search where both stages return nothing caches `[]` for the TTL, as today.
- **Missing or malformed year:** `ScoringEngine.parse_year` returns `None` and `calculate_recency_feature` applies `default_age=10.0`. `BrazilGuideline.year` is already normalized to a 4-digit string or `""` by `_parse_issued`.
- **Diacritics in the outbound query:** `_sanitize_token` keeps them and BVS handles Portuguese natively, so the request is unchanged. Accent folding is client-side only, in re-ranking.
- **Cached rows written before this change:** `BrazilGuideline.from_dict` fills `score=None` for rows lacking the key, and ordering for a cache hit is whatever was stored. Pre-existing rows are served unranked until their TTL expires. Acceptable; no cache-key version bump. In practice most such rows go cold anyway, since stopword stripping changes `composed` for any query containing one.
- **`score` across the cache boundary:** `to_dict` is `asdict` and `from_dict` filters on `__dataclass_fields__` (`models.py:265-273`), so `score` already round-trips without change. Untested today; a test is added below.

## Verification & Testing Plan

### `tests/medical/test_medical_ranking.py` (extend; do not create a new ranking test file)

This file already covers `rank_medical_articles` and is where the Portuguese tests belong.

Regression — the generalization must not move the existing path:

- Existing `rank_medical_articles` tests pass unchanged, including the `position_weight=0.0` and non-zero cases.
- `ScoringEngine.text_coverage` called without `tokenizer` returns exactly what it returns today.

New — normalization:

- Accent folding over `á é í ó ú â ê ô ã õ à ç`, and `ção`/`cao` equivalence.
- Stopword filtering: `de`, `da`, `para`, `nao` dropped; `dengue`, `tratamento`, `diretriz` retained.
- Tokens shorter than 2 characters dropped.
- `None` and `""` inputs return `[]` / `""`.

New — `rank_brazil_guidelines`:

- Accent-insensitive matching: query `"cancer"` matches a title reading `"Câncer"`.
- Title match outranks abstract-only match, all else equal.
- Newer year outranks older year, all else equal.
- Equal scores preserve BVS input order (stable sort).
- `guideline.score` is populated in place on every record, and is a float in `[0.0, 1.0]`.
- A query that tokenizes to nothing returns the input order with `score` untouched (`None`).
- Empty input returns `[]`.
- **`title_en` coverage:** an English query matches a record whose `title` is Portuguese and whose `title_en` carries the English terms; and a Portuguese-only match is not penalised when `title_en` is `""`.
- **`mesh_subjects` coverage:** a record with an empty `abstract` but matching DeCS descriptors outranks a non-matching record, and a record whose descriptors match scores above an otherwise identical record whose `mesh_subjects` is `[]`.

### `tests/medical/test_brazil_moh.py` (extend)

Stopword stripping:

- `_usable_tokens("manejo da dengue") == ["manejo", "dengue"]`.
- Accent-folded membership: a query containing `à` drops it, while a substantive accented token (`atenção`) survives **with its diacritics intact** — assert the outbound `q` still carries `atenção`, not `atencao`.
- Strict `q` for `"manejo da dengue"` is `(manejo AND dengue)`; relaxed `q` is `(manejo OR dengue)`. Neither carries `da`.
- A query of only stopwords ("sobre a") returns `([], error=False)` with zero HTTP calls.
- Boolean-word and Solr-special stripping still behave as they do today (regression).

Required update to an existing test: `test_build_query_strips_solr_special_characters` (`tests/medical/test_brazil_moh.py:129`) asserts `(a AND quote AND b AND c AND d AND e)`. Both `a` and `e` are Portuguese stopwords, so the expected group becomes `(quote AND b AND c AND d)`. The single-character `b`, `c`, `d` survive, confirming the `len >= 2` floor is deliberately not applied in `_usable_tokens`. This is the only existing test the stopword change breaks; verify by running the file before editing it, so the failure is observed rather than assumed.

Relaxation:

- Strict `AND` group is sent on the first request.
- Relaxation fires when the strict request returns zero *Brazilian* records — including the case where it returned non-Brazilian Portuguese records that the filter dropped. Assert the second request's `q` carries the `OR` group and the same `count`.
- No relaxation when the query has one substantive token, **including `"manejo da"`**, which has two raw tokens and one after stripping. Assert exactly one HTTP call.
- No relaxation when the strict request returns at least one Brazilian record (assert exactly one HTTP call).
- A failing relaxed request returns `([], error=True)` and writes nothing to the cache.

Caching and ranking:

- Relaxed results are cached under the strict cache key, and a repeat search is served from cache with one HTTP call total.
- `score` survives the cache round trip: the cache-hit path returns records whose `score` is the stored float, not `None`.
- Ranking is applied before the slice: with `limit=2` and an over-fetched pool whose best-matching record sits outside the first two in BVS order, that record is present in the returned two.
- Returned records carry a non-`None` `score`.

### Gate

`pytest tests/medical/ tests/test_ranking.py` green, plus the repo's full suite and lint gate before merge. Per the worktree convention, run tests with the main repo venv python; do not `uv sync` in a worktree.
