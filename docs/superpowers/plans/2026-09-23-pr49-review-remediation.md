# PR #49 Review Remediation

**Goal:** Resolve the review findings on `fix/govbr-stage-budget` (PR #49) without changing the PR's stated invariant (a partial retrieval is never cached).

**Architecture:** `BrazilMoHEngine.search_guidelines` gathers two gov.br stages under `_stage`, then a BVS ladder; `_search_meta` builds the §2 `CacheMetadata` from `_SearchState`. `get_full_text` writes only clean rows. `GovBrAZEngine._fetch_html` is the crawl's only page fetch.

**Tech stack:** Python 3.10+, pytest (asyncio auto, `-m "not network"`), respx, ruff.

**Spec:** PR #49 body; review comment https://github.com/gustavokch/scholar-mcp/pull/49#issuecomment-5805965873.

Not remediated (author's call, listed in the review): login-gated folder as crawl failure; uncacheable abstract fallback for permanently non-PDF documents.

---

## Task 1: Surface the gov.br stage kind in the search meta

**Modify:** `src/scholar_mcp/medical/brazil_moh.py` (`_SearchState`, `_search_meta`, `search_guidelines`)
**Test:** `tests/medical/test_brazil_moh.py`

**Consumes:** `pcdt_meta` / `az_meta` from the two `_stage` calls.
**Produces:** `CacheMetadata` from `search_guidelines` with `error=False`, `error_kind` = the errored gov.br stage's kind (`timeout` / `backend_error`), `timeout=True` on a stage timeout.

- Step 1: Rewrite `test_govbr_stage_timeout_reports_timeout_kind` to assert on the returned meta (no `_stage` spy); add `error_kind == "backend_error"` to `test_clean_bvs_result_is_not_cached_when_a_govbr_stage_fails`.
- Step 2: `uv run pytest tests/medical/test_brazil_moh.py -k "govbr_stage_timeout_reports or not_cached_when_a_govbr_stage_fails" -q` → fails (`"ok" != "timeout"`).
- Step 3: `_SearchState.local_error_kind` / `local_timed_out`; set after the gather; `_search_meta` returns the local kind when `not error` and it is set; `timeout` ORs `local_timed_out`.
- Step 4: Re-run → pass.
- Step 5: `git commit -m "fix(brazil_moh): surface a failed govbr stage in the search meta"`

## Task 2: Warn on every page the A-Z crawl loses

**Modify:** `src/scholar_mcp/medical/govbr_az.py` (`_fetch_html`)
**Test:** `tests/medical/test_govbr_az.py`

- Step 1: Test: a 503 folder and a login-gated folder each produce a WARNING record naming the URL.
- Step 2: Run → fails (no record).
- Step 3: `logger.warning` for non-200 (with status) and for the login gate.
- Step 4: Run → pass.
- Step 5: `git commit -m "fix(govbr_az): warn on every page the crawl loses"`

## Task 3: Drop the dead `_CACHED_ERROR_KIND_KEY` plumbing

**Modify:** `src/scholar_mcp/medical/brazil_moh.py`

- Step 1-2: No new test; `assert "_error_kind" not in payload` already covers the strip.
- Step 3: Remove the write and the read; cache hit reports `error_kind="ok"`; keep the strip in `_serve_full_text` for rows written by earlier releases; fix comments.
- Step 4: `uv run pytest tests/medical/test_brazil_moh.py tests/medical/test_enamed_2026_misses_engines.py -q` → pass.
- Step 5: `git commit -m "refactor(brazil_moh): drop the dead cached error-kind key"`

## Task 4: Nits

**Modify:** `govbr_az.py`, `govbr_pcdt.py` comments; `tests/medical/test_govbr_az.py` (rename, `dict`); `tests/medical/test_govbr_pcdt.py` (`dict`); `tests/test_update_govbr_catalogs.py` (module-scoped fixture, newline); `scripts/update_govbr_catalogs.py` (newline).

- Step 4: `uv run pytest tests/medical/test_govbr_az.py tests/medical/test_govbr_pcdt.py tests/test_update_govbr_catalogs.py -q`; `uv run ruff check` on the touched files shows no PIE807.
- Step 5: `git commit -m "chore(govbr): review nits"`

## Verification

`uv run pytest -q` green; `git push origin fix/govbr-stage-budget`.
