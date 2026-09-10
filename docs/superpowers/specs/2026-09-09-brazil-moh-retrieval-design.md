# Brazilian Ministry of Health Retrieval Improvements — Design

Date: 2026-09-09
Status: Approved, ready for implementation planning

## Purpose

Improve recall and ranking precision for the Brazilian Ministry of Health (`brazil-moh`) guideline search tooling.

This design introduces:
1. Portuguese-aware tokenization and re-ranking in `scholar_mcp.medical.ranking`.
2. Automatic query relaxation (two-stage `AND` -> `OR` search) in `scholar_mcp.medical.brazil_moh` when strict conjunction yields zero records.

## Scope

In scope:
- Portuguese text normalization (Unicode NFKD accent folding, lowercasing, stopword stripping).
- Re-ranking function `rank_brazil_guidelines` combining Portuguese lexical coverage (title weighted 2x abstract), BVS source position prior (35%), and recency decay (7-year half-life).
- Query relaxation in `BrazilMoHEngine.search_guidelines` fallback from `AND` to `OR` on zero hits when query contains two or more usable tokens.
- Updating `BrazilGuideline.score` in search results.
- Unit and integration tests in `tests/medical/`.

Out of scope:
- Changes to `ScoringEngine` in `src/scholar_mcp/ranking.py` (kept isolated to `medical/`).
- External NLP dependencies or heavy ML models.
- Changes to MCP tool schema or parameters in `src/scholar_mcp/server.py`.

## Architecture & Data Flow

### 1. Portuguese Normalization & Ranking (`scholar_mcp/medical/ranking.py`)

#### Normalization
- Function `normalize_portuguese(text: str | None) -> str`:
  - Applies Unicode NFKD decomposition and strips non-ASCII diacritics: `unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("utf-8")`.
  - Converts text to lowercase.
- Constant `PORTUGUESE_STOPWORDS`:
  - Curated set of common Portuguese prepositions, conjunctions, and articles (`de`, `do`, `da`, `dos`, `das`, `em`, `no`, `na`, `nos`, `nas`, `para`, `por`, `pelo`, `pela`, `com`, `sem`, `sob`, `sobre`, `um`, `uma`, `uns`, `umas`, `o`, `a`, `os`, `as`, `e`, `ou`, `se`, `que`).
- Function `tokenize_portuguese(text: str | None) -> list[str]`:
  - Normalizes input with `normalize_portuguese`.
  - Splits tokens on non-alphanumeric boundaries `[^a-z0-9]+`.
  - Retains tokens with `len(token) >= 2` and `token not in PORTUGUESE_STOPWORDS`.

#### Lexical Scoring
- Function `calculate_portuguese_coverage(query_terms: list[str], title: str | None, abstract: str | None) -> float`:
  - Computes fraction of unique `query_terms` present in tokenized title (`title_cov`) and tokenized abstract (`abstract_cov`).
  - Weights title 2.0 and abstract 1.0 (matching `ScoringEngine.text_coverage` formula):
    `min(1.0, title_cov + 0.5 * abstract_cov)`.

#### Guideline Re-Ranking
- Function `rank_brazil_guidelines(guidelines: list[BrazilGuideline], query: str, current_year: int | None = None) -> list[BrazilGuideline]`:
  - If `guidelines` is empty, returns empty list.
  - Tokenizes `query` using `tokenize_portuguese`. If no terms remain, returns copy of `guidelines`.
  - Blends components:
    - `lexical_coverage = calculate_portuguese_coverage(terms, g.title, g.abstract)`
    - `position_prior = 1.0 / math.sqrt(idx + 1)` (where `idx` is 0-indexed BVS returned order)
    - `relevance = 0.65 * lexical_coverage + 0.35 * position_prior`
    - `recency = ScoringEngine.calculate_recency_feature(g.year, current_year=now_year, half_life_years=7.0, default_age=10.0)[0]`
    - `final_score = 0.7 * relevance + 0.3 * recency`
  - Sets `g.score = final_score` on each record.
  - Sorts stably by `(-final_score, original_index)`.

### 2. Query Relaxation Engine (`scholar_mcp/medical/brazil_moh.py`)

#### Query Construction
- Update `_build_query(query: str, collection: str, operator: str = "AND") -> str`:
  - Accepts operator parameter (default `"AND"`, alternative `"OR"`).
  - Joins usable tokens with specified operator inside parentheses: `f"({f' {operator} '.join(tokens)})"`.

#### Search Workflow in `BrazilMoHEngine.search_guidelines`
1. Validate collection and parameters as currently implemented.
2. Check SQLite cache using key `f"brazil_moh_search:{norm_collection}:{clamped}:{composed_strict}"`. If cached, return.
3. Fetch candidate batch from BVS using `composed_strict` (`count = min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)`).
4. Parse docs, deduplicate by ID, filter by `_is_brazilian`.
5. **Zero-hit Fallback:**
   - If candidate records list is empty AND `len(_usable_tokens(query)) >= 2`:
     - Construct `composed_relaxed = _build_query(query, norm_collection, operator="OR")`.
     - Request BVS using `composed_relaxed`.
     - Parse docs, deduplicate by ID, filter by `_is_brazilian`.
6. Apply `rank_brazil_guidelines(records, query)`.
7. Slice top `clamped` records.
8. Store resulting records in cache under the strict cache key.
9. Return `(records, CacheMetadata(cached=False, cache_age=0, error=False))`.

## Error Handling & Edge Cases

- **Single token query yielding zero hits:** Does not trigger relaxed query; strict and relaxed clauses are identical for a single token.
- **BVS failure on relaxed attempt:** If the relaxed request returns an error or non-JSON, log warning and return empty result with `error=True`. Do not cache errored payload.
- **Diacritics in BVS query:** `_sanitize_token` retains diacritics; Solr BVS handles Portuguese characters natively. Accent folding occurs in client-side re-ranking.
- **Missing or non-standard publication year:** Handled gracefully via `ScoringEngine.parse_year` falling back to `default_age=10.0`.

## Verification & Testing Plan

### Unit Tests
- `tests/medical/test_ranking_portuguese.py`:
  - Accent folding across common accented vowels and cedilla (`ç`, `ã`, `õ`, `á`, `é`, `í`, `ó`, `ú`, `â`, `ê`, `ô`).
  - Stopword filtering: ensures stopwords like `de`, `da`, `para` are omitted, while clinical words are retained.
  - Scoring correctness: verify title matches score higher than abstract-only matches, newer documents score higher than older ones ceteris paribus, and BVS tie-breaking is preserved.
  - In-place population of `BrazilGuideline.score`.

- `tests/medical/test_brazil_moh.py`:
  - Verify strict `AND` query is sent on initial search.
  - Verify relaxation occurs when strict query returns zero Brazilian documents.
  - Verify relaxation does not occur when query has only one token.
  - Verify relaxation does not occur when strict query returns at least one Brazilian document.
  - Verify cached result of a relaxed search is returned on subsequent searches.

### Manual / Integration Verification
- Execute `pytest tests/medical/` to ensure 100% test pass across all medical modules.
