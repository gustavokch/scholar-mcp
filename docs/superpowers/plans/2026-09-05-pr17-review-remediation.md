# PR #17 Review Remediation Plan

**Goal:** Fix review findings from the Camoufox fallback PR.
**Branch:** `feat/scihub-camoufox-fallback`

---

## Task 1 — Fix `__import__` mock in ImportError test

**Files:** `tests/test_search_scihub_providers.py`

**Step 1 — Red:** Add assertion that calling `__import__("os")` inside the monkeypatched scope succeeds (currently it does by luck but is fragile).

**Step 2 — Green:** Replace inline lambda with a proper function that captures `builtins.__import__` before patching and delegates non-camoufox imports to the original.

**Step 3 — Commit:** `fix(tests): use captured __import__ in camoufox ImportError test`

---

## Task 2 — Add explicit `settings=` to unguarded SciHubProvider constructors

**Files:** `tests/test_search_scihub_providers.py`

**Step 1 — Red:** Verify `test_scihub_mirror_fallback` and `test_scihub_without_doi_is_miss` currently construct SciHubProvider without `settings=`.

**Step 2 — Green:** Pass `settings=Settings(enable_browser_fallback=False)` to those two constructors.

**Step 3 — Commit:** `fix(tests): disable browser fallback in unguarded SciHubProvider tests`

---

## Task 3 — Cap camoufox mirror attempts and total timeout

**Files:** `src/scholar_mcp/providers/scihub.py`

**Step 1 — Red:** Write test: camoufox with 5 mirrors only attempts first 3.

**Step 2 — Green:** Add `_CAMOUFOX_MAX_MIRRORS = 3` constant and slice `self.mirrors[:_CAMOUFOX_MAX_MIRRORS]` in `_fetch_via_camoufox`. Wrap browser block in `asyncio.wait_for(timeout=20)`.

**Step 3 — Commit:** `fix(scihub): cap camoufox to 3 mirrors and 20s total timeout`

---

## Task 4 — Clean up 175-char lambda line

**Files:** `tests/test_search_scihub_providers.py`

**Step 1 — Green:** Extract named function, same semantics, readable.

**Step 2 — Commit:** `style(tests): extract __import__ override for readability`

Note: Task 4 merges with Task 1 — single commit.

---

## Verify & Push

```bash
uv run pytest
git push origin feat/scihub-camoufox-fallback
```
