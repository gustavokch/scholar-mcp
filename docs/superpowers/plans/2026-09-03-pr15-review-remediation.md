# PR #15 Review Remediation Plan

- **PR:** #15 (`fix(clinical-trials): cap query.term to CT.gov parser term limit`)
- **Branch:** `fix-clinical-trials-query-cap`
- **Goal:** Address review findings on PR #15: balance unclosed quotes in `_cap_query_terms`, normalize cache key using capped query to prevent duplicate requests/cache entries, and add unit test coverage for `_cap_query_terms`.
- **Tech Stack:** Python 3.10+, pytest, respx, httpx, SQLite cache.

---

### Task 1: Harden `_cap_query_terms` with Quote Balancing & Unit Tests

- **Target Files:**
  - `src/scholar_mcp/medical/clinical_trials.py`
  - `tests/medical/test_clinical_trials.py`
- **Consumes / Produces:**
  - `_cap_query_terms(query: str, max_terms: int = CT_MAX_QUERY_TERMS) -> str`
- **Step 1 (Red):** Write unit tests in `tests/medical/test_clinical_trials.py` testing:
  - Empty / whitespace queries
  - Queries <= max_terms
  - Queries > max_terms (term truncation)
  - Queries with unbalanced quotes created by truncation
  - Queries with original unbalanced quotes
  - Custom `max_terms` (including `<= 0`)
- **Step 2 (Verify Red):** Run `uv run pytest tests/medical/test_clinical_trials.py -k test_cap_query_terms -v` and observe failures.
- **Step 3 (Green):** Implement quote balancing and whitespace handling in `_cap_query_terms`.
- **Step 4 (Verify Green):** Re-run `uv run pytest tests/medical/test_clinical_trials.py -k test_cap_query_terms -v`.
- **Step 5 (Commit):** `git commit -m "fix(clinical-trials): balance quotes and handle edge cases in _cap_query_terms"`

---

### Task 2: Align `search_clinical_trials` Cache Key with Capped Query

- **Target Files:**
  - `src/scholar_mcp/medical/clinical_trials.py`
  - `tests/medical/test_clinical_trials.py`
- **Consumes / Produces:**
  - `search_clinical_trials(query: str, limit: int = 10)`
- **Step 1 (Red):** Add test in `tests/medical/test_clinical_trials.py` confirming that queries sharing identical capped terms hit the cache on second invocation without re-fetching.
- **Step 2 (Verify Red):** Run `uv run pytest tests/medical/test_clinical_trials.py -k test_search_clinical_trials_shares_cache_for_overlong_queries -v` and observe failure (due to raw query cache key).
- **Step 3 (Green):** Update `search_clinical_trials` to construct `cache_key` from `capped_query`.
- **Step 4 (Verify Green):** Run `uv run pytest tests/medical/test_clinical_trials.py -v`.
- **Step 5 (Commit):** `git commit -m "fix(clinical-trials): use capped query in cache key to share cache"`

---

### Task 3: Full Test Suite Verification & Remote Push

- **Step 1:** Run full test suite: `uv run pytest`
- **Step 2:** Push branch to remote: `git push origin fix-clinical-trials-query-cap`
