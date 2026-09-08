# PR #19 Review Remediation — Brazilian MoH via BVS/iAHx

Date: 2026-09-07
PR: https://github.com/gustavokch/scholar-mcp/pull/19
Head branch: `worktree-feat+brazil-moh-guidelines`

## Goal

Fix the 7 findings from the PR #19 code review without regressing the 487-test suite.

## Architecture

All changes live in `src/scholar_mcp/medical/brazil_moh.py`, its engine tests
(`tests/medical/test_brazil_moh.py`), and the server tool layer
(`src/scholar_mcp/server.py`). The BVS query builder and the full-text fetcher
are the two seams touched. No model or formatter changes.

## Tech Stack

pytest + respx (mocked httpx), existing `AsyncHttpClient` / `SQLiteCacheManager`.

## Spec Reference

Review comment posted on PR #19 (2026-09-07).

---

### Task 1: Sanitize user tokens in `_build_query`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_build_query(query, collection) -> str`
**Produces:** tokens stripped of Solr-special characters and bare boolean words.

- Step 1: Failing test — token `"a AND b"` yields `(... a AND AND AND b)` today;
  assert query contains `a` and `b` joined by AND with no bare `AND AND` and no
  stray specials: `_build_query('a "quote" AND (b)', "all")` == base filter +
  `(a AND quote AND b)`.
- Step 2: `uv run pytest tests/medical/test_brazil_moh.py -k build_query` — expect fail.
- Step 3: Add `_sanitize_token(token)`: drop chars `[]{}()^"~*?:\/!` and reject
  bare `AND OR NOT TO` (case-insensitive) tokens; apply in `_build_query`.
- Step 4: Re-run — pass.
- Step 5: `git commit -m "fix(brazil-moh): sanitize user tokens before query composition"`

### Task 2: Escape `record_id` in `_lookup_record`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_lookup_record(record_id) -> (record|None, errored)`
**Produces:** record_id quoted-escaped before Solr interpolation.

- Step 1: Failing test — respx asserts the requested `q` for
  `record_id='bi"blio'` equals `id:"bi\"blio"` (escaped), no 500.
- Step 2: Run — expect fail.
- Step 3: `escaped = record_id.replace("\\", "\\\\").replace('"', '\\"')`.
- Step 4: Run — pass.
- Step 5: `git commit -m "fix(brazil-moh): escape record_id in id lookup"`

### Task 3: Disable redirect-following for the PDF fetch

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `AsyncHttpClient.get` (redirects on by default, `utils/http.py:68`)
**Produces:** full-text fetch never leaves allowlisted hosts.

- Step 1: Failing test — mock allowlisted URL returning `302` with
  `Location: https://evil.example.com/x.pdf`; assert no request to
  `evil.example.com` was made and the result degrades to abstract with
  `meta.error is True`.
- Step 2: Run — expect fail (client follows to evil host).
- Step 3: Add `follow_redirects=False` support to the fetch call path (per-request
  kwarg on `AsyncHttpClient.get` if not present) and use it in `_extract_pdf_text`.
- Step 4: Run — pass.
- Step 5: `git commit -m "fix(brazil-moh): forbid redirects on full-text fetch"`

### Task 4: `errored` + no abstract must be `error`, not `not_found`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `get_full_text` result tuple
**Produces:** status `error` when the PDF fetch errored and no abstract exists.

- Step 1: Failing test — PDF route `ConnectError`, record has no `ab`;
  assert `payload["status"] == "error"`.
- Step 2: Run — expect fail (currently `not_found`).
- Step 3: In the no-pdf/no-abstract branch, choose `status = "error" if errored
  else "not_found"` with matching error strings.
- Step 4: Run — pass.
- Step 5: `git commit -m "fix(brazil-moh): report error status when full text fetch fails"`

### Task 5: Clamp limit in one place

**Files:** Modify `src/scholar_mcp/server.py`, Test `tests/test_server_medical.py`

- Step 1: Failing test — server tool forwards the raw limit to the engine
  (`limit=9999` reaches the mock unclamped).
- Step 2: Run — expect fail.
- Step 3: Remove the server-side clamp; engine already clamps.
- Step 4: Run — pass.
- Step 5: `git commit -m "refactor(server): let brazil-moh engine own limit clamping"`

### Task 6: Robust count assertions

**Files:** Test `tests/medical/test_brazil_moh.py`

- Step 1: Refactor the three `"count=…" in str(url)` assertions to
  `parse_qs(urlparse(...).query)["count"] == ["30"]` etc.
- Step 2: Run full engine test module — pass (no behavior change).
- Step 3: `git commit -m "test(brazil-moh): assert count param exactly via parse_qs"`

### Task 7: Cover the `max_chars <= 0` clamp

**Files:** Test `tests/medical/test_brazil_moh.py`

- Step 1: New test — cached payload + `max_chars=0` serves at least 1 char,
  `truncated is True`.
- Step 2: Run — pass (clamp already exists: `max(1, max_chars)`); this pins it.
- Step 3: `git commit -m "test(brazil-moh): pin max_chars zero clamp"`

---

## Verification

1. `uv run pytest` — 100% green gate before push.
2. `git push origin worktree-feat+brazil-moh-guidelines`.
