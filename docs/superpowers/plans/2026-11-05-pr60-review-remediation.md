# PR #60 Review Remediation Plan

**Goal:** Close every finding of the PR #60 review
(https://github.com/gustavokch/scholar-mcp/pull/60#issuecomment-6666268707).

**Architecture:** `WHOClient.get_health_statistics` and
`get_child_health_statistics` (`src/scholar_mcp/medical/who.py`) return a result
that had a failed request with `error=True` and do not cache it. #59 merged to
`main` first, with cache keys `who:v2` / `child_health:v2` and the old rule, so
partial rows can already exist under `v2`. The fix bumps both keys to `v3`.

**Tech Stack:** Python 3.10, pytest + pytest-asyncio, respx, SQLite cache.

**Spec reference:** PR #60 body and the review comment above.

**Test command** (borrowed venv, import path pinned to this worktree):

```bash
PY=/Users/gus/Git/scholar-mcp/.worktrees/pr59-remediation/.venv/bin/python
PYTHONPATH="$PWD/src" $PY -m pytest tests/medical/test_who.py -q -p no:randomly
```

---

## Task 0: Merge `origin/main` into the PR branch

The key bump edits lines that #59 changed, so the branch must carry #59 first.

- Step 1: `git merge origin/main` (expected: no conflict, per `git merge-tree`).
- Step 2: run `tests/medical/test_who.py`; all pass.

## Task 1 (🟡): Bump the WHO cache keys to `v3`

**Files:** Modify `src/scholar_mcp/medical/who.py` (two `cache_key` lines).
Test `tests/medical/test_who.py`.

**Consumes:** `SQLiteCacheManager.set`. **Produces:** keys `who:v3:…`,
`child_health:v3:…`.

- Step 1: failing test.

```python
@respx.mock
async def test_statistics_ignore_rows_cached_under_the_v2_keys(tmp_path: Path):
    """`main` with #59 but without this fix cached partial answers under the
    v2 keys; the v3 keys leave those rows unread."""
    client, cache, http_client = _client(tmp_path)
    try:
        await cache.set("who:v2:life expectancy:USA:10", [], source="who")
        await cache.set("child_health:v2:zzz:USA:10", [], source="child_health")
        respx.get(f"{GHO_BASE}/Indicator").respond(
            json={"value": [{"IndicatorCode": "WHOSIS_000001", "IndicatorName": "Life expectancy at birth (years)"}]}
        )
        respx.get(url__startswith=GHO_BASE).respond(
            json={"value": [{"SpatialDim": "USA", "TimeDim": "2020", "NumericValue": 78.5}]}
        )

        records, meta = await client.get_health_statistics("life expectancy", country="USA")
        assert meta.cached is False
        assert records

        records, meta = await client.get_child_health_statistics("zzz", country="USA")
        assert meta.cached is False
        assert records
    finally:
        await cache.close()
        await http_client.aclose()
```

- Step 2: run it; expect FAIL (`meta.cached` is True: the seeded `v2` row is served).
- Step 3: change `who:v2:` → `who:v3:` and `child_health:v2:` → `child_health:v3:`.
- Step 4: run `tests/medical/test_who.py`; all pass.
- Step 5: `git commit -m "fix(who): retire statistics rows cached under the v2 keys"`

## Task 2 (🔵): Cover the variation-recovery path and the second call

**Files:** Test `tests/medical/test_who.py` only.

- Step 1: add the test below, and on the second call of both
  `*_does_not_cache_a_partial_result` tests assert the answering indicator's
  rows and `meta.error is True`.

```python
@respx.mock
async def test_get_health_statistics_does_not_cache_a_variation_recovered_lookup(tmp_path: Path):
    """The primary indicator lookup fails and a name variation finds an
    indicator. The primary term could have matched a closer indicator, so the
    answer is flagged and not cached."""
    client, cache, http_client = _client(tmp_path)

    def found():
        return respx.MockResponse(
            json={"value": [{"IndicatorCode": "WHS9_86", "IndicatorName": "Exclusive breastfeeding"}]}
        )

    try:
        indicator_route = respx.get(f"{GHO_BASE}/Indicator")
        # A 404 fails the primary lookup without the retry ladder's backoff.
        indicator_route.side_effect = [
            respx.MockResponse(status_code=404), found(),
            respx.MockResponse(status_code=404), found(),
        ]
        respx.get(f"{GHO_BASE}/WHS9_86").respond(
            json={"value": [{"SpatialDim": "USA", "TimeDim": "2021", "NumericValue": 0.42}]}
        )

        records, meta = await client.get_health_statistics("breastfeeding", country="USA")
        assert [r.indicator_code for r in records] == ["WHS9_86"]
        assert meta.error is True

        records, meta = await client.get_health_statistics("breastfeeding", country="USA")
        assert indicator_route.call_count == 4
        assert [r.indicator_code for r in records] == ["WHS9_86"]
        assert meta.cached is False
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()
```

- Step 2: run; expect PASS (the PR's fix already covers it).
- Step 3: mutation check: revert `if errored:` to `if errored and not final_records:`
  in `get_health_statistics`; the new test and the strengthened partial test
  must FAIL. Restore.
- Step 4: run `tests/medical/test_who.py`; all pass.
- Step 5: `git commit -m "test(who): cover the variation-recovered lookup and the uncached re-ask"`

## Task 3 (🔵): Drop the dead clean-path flag; move the CHANGELOG entry

**Files:** Modify `src/scholar_mcp/medical/who.py`, `CHANGELOG.md`.

- Step 1: no new test: behaviour does not change. The existing clean-path
  tests (`test_get_health_statistics`, `test_get_child_health_statistics`) cover it.
- Step 2: in both clean-path returns, replace
  `CacheMetadata(cached=False, cache_age=0, error=errored)` with
  `CacheMetadata(cached=False, cache_age=0)`.
- Step 3: move the "WHO statistics never cache a partial answer" entry to sit
  after "WHO statistics keep the right slice" at the top of `### Fixed`, and
  add "Cache keys `who:v3`, `child_health:v3`." to it.
- Step 4: run `tests/medical/test_who.py`; all pass.
- Step 5: `git commit -m "refactor(who): drop the always-false clean-path error flag"`
  and `git commit -m "docs(changelog): place the WHO partial-cache entry with the other WHO fix"`

## Verification and push

- Full offline suite; must be green.
- `python -c "from scholar_mcp.server import main"` with the pinned path.
- `trail check`.
- `git push origin pr60-remediation:fix/who-partial-cache` (no force).
- Update the PR body: Evidence counts, and Merge Danger (#59 is merged; `v3` keys).
