# PR #19 Review Remediation — Round 4

**Goal:** Resolve the eight findings from the round-4 review of PR #19
(`feat(medical): Brazilian MoH guidelines via BVS/iAHx`).

**Branch:** `worktree-feat+brazil-moh-guidelines`

**Baseline:** 500 tests passing before any change.

**Files in scope:**
- `src/scholar_mcp/medical/brazil_moh.py`
- `src/scholar_mcp/medical/formatters.py`
- `src/scholar_mcp/medical/models.py`
- `src/scholar_mcp/server.py`
- `tests/medical/test_brazil_moh.py`

**Test command:** `.venv/bin/python -m pytest tests/medical/test_brazil_moh.py -q`
(the worktree has no venv of its own; the main repository's interpreter is used).

---

## Task 1 — Verify the looked-up record id (🔴 bug)

**Modify:** `src/scholar_mcp/medical/brazil_moh.py` (`_lookup_record`)
**Test:** `tests/medical/test_brazil_moh.py`

`_lookup_record` issues `q=id:"<escaped>"` with `count=5` and returns the first
document. The BVS `id` field is tokenized, so the phrase query can match a
different record. That record's PDF is then served under the caller's
`record_id` and cached for the 30-day TTL.

- Step 1: add `test_get_full_text_ignores_non_matching_lookup_hit` — the search
  mock returns a document whose `id` is `biblio-999` while `biblio-1` was
  requested; assert `status == "not_found"`.
- Step 2: run the test, confirm it fails (the mismatched record is served).
- Step 3: in `_lookup_record`, select the first doc whose `_first(doc["id"])`
  equals `record_id`; return `(None, False)` when none matches.
- Step 4: re-run, confirm pass.
- Step 5: `git commit -m "fix(brazil-moh): verify record id in lookup response"`

## Task 2 — Clamp `max_chars` from above (🟡 risk)

**Modify:** `src/scholar_mcp/medical/brazil_moh.py` (`_serve_full_text`)

The tool docstring documents a 50,000-character default, but a caller-supplied
`max_chars` has no upper bound, so the whole extracted PDF can be returned.

- Step 1: add `test_serve_full_text_clamps_max_chars_to_module_ceiling`
  asserting a `max_chars` above `MAX_FULL_TEXT_CHARS` truncates at the ceiling.
- Step 2: run, confirm failure.
- Step 3: `limit = min(max(1, max_chars), MAX_FULL_TEXT_CHARS)`.
- Step 4: re-run, confirm pass.
- Step 5: `git commit -m "fix(brazil-moh): cap served full text at module ceiling"`

## Task 3 — Cap cached full text (🟡 risk)

**Modify:** `src/scholar_mcp/medical/brazil_moh.py` (`_extract_pdf_text`)

The cached payload holds untruncated PDF text, so one long manual writes a
multi-megabyte row into the shared SQLite cache.

- Step 1: add `test_get_full_text_caps_cached_content_at_ceiling` — extraction
  yields `MAX_FULL_TEXT_CHARS + 1000` characters; assert the cached row holds at
  most `MAX_FULL_TEXT_CHARS`.
- Step 2: run, confirm failure.
- Step 3: truncate the extracted text to `MAX_FULL_TEXT_CHARS` in
  `_extract_pdf_text`.
- Step 4: re-run, confirm pass (the existing "truncates served not cached" test
  stays green — 500 characters is far below the ceiling).
- Step 5: `git commit -m "fix(brazil-moh): bound cached full text length"`

## Task 4 — Reject a query that sanitizes away (🟡 risk)

**Modify:** `src/scholar_mcp/medical/brazil_moh.py` (`_build_query`,
`search_guidelines`)

A non-blank query whose tokens are all stripped (`"***"`, `"AND OR"`) currently
falls through to the base filters and returns arbitrary top-of-index documents
presented as matches. A blank query browsing the collection stays supported.

- Step 1: add `test_search_returns_empty_for_query_with_no_usable_tokens`
  asserting no HTTP call is made and `meta.error is False`.
- Step 2: run, confirm failure.
- Step 3: add a `_usable_tokens` helper shared by `_build_query`; in
  `search_guidelines`, return `([], CacheMetadata(error=False))` when the query
  is non-blank and yields no tokens.
- Step 4: re-run, confirm pass.
- Step 5: `git commit -m "fix(brazil-moh): return empty for queries with no usable tokens"`

## Task 5 — Plain-string country parsing (🔵 nit)

**Modify:** `src/scholar_mcp/medical/brazil_moh.py` (`_parse_country`)

The `^` subfield scan runs before the plain-string fallback, so `"Espanha"`
parses as `"spanha"`.

- Step 1: add `test_parse_country_plain_string_starting_with_e` asserting
  `_parse_country(["Espanha"]) == "Espanha"`.
- Step 2: run, confirm failure.
- Step 3: take the plain path when `"^" not in raw`.
- Step 4: re-run, confirm pass.
- Step 5: `git commit -m "fix(brazil-moh): parse plain country strings verbatim"`

## Task 6 — Normalize the search cache key (🔵 nit)

**Modify:** `src/scholar_mcp/medical/brazil_moh.py` (`search_guidelines`)

`"dengue"` and `" dengue "` occupy separate cache rows for an identical
composed query.

- Step 1: add `test_search_cache_key_ignores_query_whitespace` — two searches
  differing only in surrounding whitespace issue one HTTP call.
- Step 2: run, confirm failure.
- Step 3: build the composed query once and key the cache on it.
- Step 4: re-run, confirm pass.
- Step 5: `git commit -m "fix(brazil-moh): key search cache on the composed query"`

## Task 7 — Merge the duplicate formatters import (🔵 nit)

**Modify:** `src/scholar_mcp/server.py`

`scholar_mcp.medical.formatters` is imported twice.

- Step 1: no new test; the existing `tests/test_server_medical.py` covers the
  tools.
- Step 2: merge `format_brazil_moh_guidelines` into the existing formatters
  import block and group the `brazil_moh` imports with the other medical
  imports.
- Step 3: run `tests/test_server_medical.py`, confirm pass.
- Step 4: `git commit -m "refactor(server): merge duplicate formatters import"`

## Task 8 — Trim trailing blank lines (🔵 nit)

**Modify:** `src/scholar_mcp/medical/brazil_moh.py`,
`src/scholar_mcp/medical/formatters.py`, `src/scholar_mcp/medical/models.py`

- Step 1: trim each file to a single trailing newline.
- Step 2: run the medical test package, confirm pass.
- Step 3: `git commit -m "style(medical): trim trailing blank lines"`

---

## Verification

1. `.venv/bin/python -m pytest -q` — must reach 100% green (baseline 500 tests
   plus the new cases).
2. `git push origin worktree-feat+brazil-moh-guidelines`.
