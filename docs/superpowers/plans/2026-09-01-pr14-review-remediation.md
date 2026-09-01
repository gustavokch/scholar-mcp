# PR #14 Review Remediation Plan

**Goal:** Fix the 4 findings from the PR #14 review (comment 5502153212) on `get_who_iris_full_text`.

**Architecture:** All changes inside `WHOIRISEngine` (`src/scholar_mcp/medical/who_iris.py`) plus its test module. No server/README changes.

**Tech Stack:** pytest-asyncio, respx, existing `_engine` fixture builders in `tests/medical/test_who_iris.py`.

**Spec:** Review comment https://github.com/gustavokch/scholar-mcp/pull/14#issuecomment-5502153212

## Global Constraints

- Run tests with `uv run --frozen pytest` from the PR worktree (`.claude/worktrees/who-iris-full-text`) — `--frozen` avoids stray `uv.lock` re-resolution.
- Every async test closes `cache` and `http_client` in `try/finally` (pytest shutdown hang).
- Use distinct handles per test to avoid cross-test cache pollution.
- Existing tests use `http_client.aclose()`.

---

### Task 1: Pass DSpace page size on bundles/bitstreams list requests

**Files:**
- Modify: `src/scholar_mcp/medical/who_iris.py` (`_extract_pdf_text`)
- Test: `tests/medical/test_who_iris.py`

**Step 1: Failing test** — assert `size=100` param on both list calls (one ORIGINAL bundle, one non-PDF bitstream so both endpoints are hit without a download):

```python
@respx.mock
async def test_get_full_text_requests_full_page_size(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        bundles_route = respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(
            json=_bundles_page([_bundle()]))
        bits_route = respx.get(f"{IRIS_BUNDLE_BITSTREAMS_URL}/bundle-uuid-1/bitstreams").respond(
            json=_bitstreams_page([_bitstream(mime="text/html", name="index.html")]))
        payload, meta = await engine.get_full_text("10665/311554")
        assert payload["content_type"] == "abstract"
        assert meta.error is False
        assert bundles_route.calls[0].request.url.params["size"] == "100"
        assert bits_route.calls[0].request.url.params["size"] == "100"
    finally:
        await cache.close()
        await http_client.aclose()
```

**Step 2:** `uv run --frozen pytest tests/medical/test_who_iris.py -k full_page_size -q` → FAIL (no `size` param).

**Step 3:** Add `params={"size": str(MAX_PAGE_SIZE)}` to the bundles and bitstreams `http_client.get` calls.

**Step 4:** Same command → PASS.

**Step 5:** `git commit -m "fix(who-iris): request full DSpace page size for bundles and bitstreams"`

---

### Task 2: Strip query strings and fragments in `_normalize_handle`

**Files:**
- Modify: `src/scholar_mcp/medical/who_iris.py` (`_normalize_handle`)
- Test: `tests/medical/test_who_iris.py`

**Step 1: Failing test:**

```python
@respx.mock
async def test_get_full_text_strips_query_and_fragment(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(IRIS_PID_FIND_URL).respond(json=_pid_find_item())
        respx.get(f"{IRIS_ITEM_BUNDLES_URL}/item-uuid-1/bundles").respond(json=_bundles_page([]))
        for raw, normalized in (
            ("https://iris.who.int/handle/10665/311555?show=full", "10665/311555"),
            ("hdl:10665/311556#abstract", "10665/311556"),
        ):
            route.calls.clear()
            payload, _ = await engine.get_full_text(raw)
            assert payload["handle"] == normalized
            assert route.calls[0].request.url.params["id"] == f"hdl:{normalized}"
    finally:
        await cache.close()
        await http_client.aclose()
```

**Step 2:** `-k strips_query_and_fragment -q` → FAIL.

**Step 3:** In `_normalize_handle`, after the `/handle/` split add:

```python
h = h.split("?", 1)[0].split("#", 1)[0]
```

**Step 4:** PASS.

**Step 5:** `git commit -m "fix(who-iris): strip query strings and fragments from IRIS handles"`

---

### Task 3: Honor `max_chars=0` instead of silently defaulting

**Files:**
- Modify: `src/scholar_mcp/medical/who_iris.py` (`_serve_full_text`)
- Test: `tests/medical/test_who_iris.py`

**Step 1: Failing test** — extend `test_get_full_text_truncates_served_content_not_cached` with a `max_chars=0` call:

```python
        zero, _ = await engine.get_full_text("10665/311551", max_chars=0)
        assert zero["truncated"] is True
        assert zero["content"].endswith("[... Truncated due to max_chars limit ...]")
```

**Step 2:** `-k truncates_served -q` → FAIL (0 becomes 50k default, truncated False).

**Step 3:** Replace `max_chars or MAX_FULL_TEXT_CHARS` with `MAX_FULL_TEXT_CHARS if max_chars is None else max_chars`.

**Step 4:** PASS.

**Step 5:** `git commit -m "fix(who-iris): only default max_chars when None"`

---

### Task 4: Cover the empty-handle error path

**Files:**
- Test only: `tests/medical/test_who_iris.py`

**Step 1: Failing test** (no respx — no network call expected):

```python
async def test_get_full_text_requires_handle(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, meta = await engine.get_full_text("   ")
        assert payload["status"] == "error"
        assert payload["error"] == "handle is required"
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()
```

**Step 2:** Should already pass (documents existing behavior; guard against regression). If it fails, stop and investigate.

**Step 3:** No implementation change expected.

**Step 4:** PASS.

**Step 5:** `git commit -m "test(who-iris): cover empty-handle error path"`

---

## Verification

`uv run --frozen pytest -q` full suite green, then push `worktree-who-iris-full-text`.
