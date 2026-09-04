# PR 16 Review Remediation Plan (round 2)

- **Goal:** Fix re-review findings for PR #16: crash on non-finite `sizeBytes`, full-TTL caching of degraded enrichment, dropped `pdf_url` in `not_found`, EOF whitespace.
- **Architecture:** `WHOIRISEngine` in `src/scholar_mcp/medical/who_iris.py`, `tests/medical/test_who_iris.py`.
- **Tech Stack:** Python 3.10+, httpx, respx, pytest, pytest-asyncio.
- **Review:** https://github.com/gustavokch/scholar-mcp/pull/16#issuecomment-5534842590

---

### Task 1: `_safe_size` non-finite guard

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/who_iris.py` (`_safe_size`, L67)
  - Modify: `tests/medical/test_who_iris.py`
- **Consumes / Produces:** `_safe_size(bitstream) -> int` returns 0 for NaN/inf/bool-weirdness instead of raising `ValueError`/`OverflowError`.
- **Step 1:** Failing tests:
  - `test_safe_size_handles_non_finite_and_bad_values`: `{"sizeBytes": float("nan")}` → 0, `float("inf")` → 0, `"abc"` → 0, missing → 0, `123` → 123, `"123"` → 123.
  - `test_extract_pdf_link_survives_nan_size`: item with one NaN-size bitstream and one normal bitstream → returns normal bitstream URL (no raise).
- **Step 2:** `uv run pytest tests/medical/test_who_iris.py -k "safe_size or nan_size"` → red.
- **Step 3:** In `_safe_size`: guard `isinstance(val, float) and not math.isfinite(val)` → 0 before `int(val)`.
- **Step 4:** Same command → green.
- **Step 5:** `git add -A src/scholar_mcp/medical/who_iris.py tests/medical/test_who_iris.py && git commit -m "fix(who-iris): guard _safe_size against non-finite sizeBytes"`

### Task 2: Skip cache write when any PDF enrichment errored

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/who_iris.py` (`search_guidelines`, L239-262)
  - Modify: `tests/medical/test_who_iris.py`
- **Consumes / Produces:** `_enrich_item` propagates an `errored` flag; `search_guidelines` skips `cache.set` when any item's bitstream resolution errored (matches existing page-fetch convention).
- **Step 1:** Failing test `test_search_guidelines_bitstream_error_not_cached`: bundles route responds 500; call `search_guidelines` twice; assert bundles route call_count == 2 (second call not served from cache). Current behavior: cached → call_count == 1 → red.
- **Step 2:** Run `-k bitstream_error_not_cached` → red.
- **Step 3:** `_enrich_item` returns `(rec, errored)`; gather collects; `cache_error = errored or any(...)`; `if not cache_error: await self.cache.set(...)`.
- **Step 4:** Run full `tests/medical/test_who_iris.py` → green (update existing `test_search_guidelines_item_bitstream_error_does_not_fail_search` if it asserts caching — it does not, only meta.error).
- **Step 5:** `git add ... && git commit -m "fix(who-iris): skip search cache write when PDF enrichment errored"`

### Task 3: `not_found` branch keeps resolved pdf_url + EOF trim

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/who_iris.py` (`get_full_text` not_found branch, L365)
  - Modify: `tests/medical/test_who_iris.py`
- **Step 1:** Failing test `test_get_full_text_not_found_includes_pdf_url`: bitstream exists (pdf_url resolves) but content download 404s and no abstract → payload `status == "not_found"` and `pdf_url == f"{IRIS_BITSTREAM_CONTENT_URL}/bit-x/content"`.
- **Step 2:** Run → red.
- **Step 3:** Replace `"pdf_url": ""` with `"pdf_url": pdf_url` in not_found branch; trim EOF blank lines in test file.
- **Step 4:** Run → green.
- **Step 5:** `git commit -m "fix(who-iris): keep resolved pdf_url in not_found full-text payload"`

### Task 4: Full verification and PR push

- **Step 1:** `uv run pytest` → 100% green.
- **Step 2:** `git push origin fix-clinical-trials-query-cap`.
