# PR #30 Review Remediation — 2026-09-16

**Goal:** Resolve the 9 findings from the PR #30 review without changing the
public MCP tool contract more than the sentinel fix requires.

**Spec reference:** https://github.com/gustavokch/scholar-mcp/pull/30#issuecomment-5706070142

**Tech stack:** Python 3.10+, asyncio, httpx, pytest / pytest-asyncio, uv.

**Architecture notes:**
- `WaterfallResolver` and every provider are module-level singletons in
  `server.py`, so any per-request state they hold is shared across concurrent
  MCP tool calls. Per-request state must live in a `ContextVar`, which asyncio
  propagates down an `await` chain but isolates between concurrently scheduled
  tasks.
- `AsyncHttpClient._limiters` is deliberately process-global so two clients
  built from the same settings share one token bucket per host. It is keyed by
  event loop because `AsyncRateLimiter` owns an `asyncio.Lock`; the key must
  not keep dead loops alive.

---

## Task 1 — Context-scoped per-request state helper

**Files:** Create `src/scholar_mcp/utils/ctxstate.py`; Test
`tests/test_ctxstate.py`.

**Produces:** `ContextScoped` — a data descriptor backed by a `ContextVar`,
usable as a normal instance attribute on a shared singleton.

1. Write failing test: two concurrently scheduled tasks each set the attribute
   on the *same* object and read back only their own value.
2. `uv run --extra dev pytest tests/test_ctxstate.py -v` — fails (no module).
3. Implement `ContextScoped` with `__set_name__`, `__get__`, `__set__`, each
   instance keyed by `id(obj)` inside the ContextVar's dict.
4. Re-run — passes.
5. `git commit -m "feat(utils): add ContextScoped descriptor for per-request state"`

## Task 2 — Resolver degradation map is per-request

**Files:** Modify `src/scholar_mcp/resolver.py`; Test
`tests/test_waterfall_resolver.py`.

1. Failing test: two concurrent `search()` calls on one resolver, each with a
   different mocked backend outcome; assert each sees only its own map.
2. Run — fails (maps cross-contaminate).
3. Make `last_search_sources` a `ContextScoped` attribute.
4. Re-run — passes.
5. `git commit -m "fix(resolver): scope last_search_sources per request"`

## Task 3 — Provider `last_error` is per-request

**Files:** Modify `providers/pubmed.py`, `providers/crossref.py`,
`providers/semantic_scholar.py`; Test `tests/test_search_providers.py`.

1. Failing test: two concurrent `search()` calls on one provider, one failing
   and one succeeding; assert the successful call does not observe the other's
   `last_error`.
2. Run — fails.
3. Swap the three `last_error` attributes to `ContextScoped`.
4. Re-run — passes.
5. `git commit -m "fix(providers): scope last_error per request"`

## Task 4 — `search_papers` sentinel must not break the list contract

**Files:** Modify `src/scholar_mcp/server.py`; Test `tests/test_server_tools.py`.

**Decision:** keep the list return (changing it to an envelope would break every
existing consumer), but make the sentinel share the `PaperMetadata.to_dict()`
key set so naive `r["title"]` access cannot raise, and tag it with
`status="degraded"` so consumers can filter it out.

1. Failing test: every element of the degraded payload exposes the full paper
   key set; exactly one element has `status == "degraded"`; the paper slice is
   unchanged.
2. Run — fails (`KeyError`).
3. Build the sentinel from `PaperMetadata(title="")` and overlay
   `status`/`_sources`/`degraded`.
4. Re-run — passes.
5. `git commit -m "fix(server): make degradation sentinel shape-compatible"`

## Task 5 — Brazil MoH camoufox must not clear the error on an empty filter

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`; Test
`tests/medical/test_brazil_moh.py`.

1. Failing test: every HTTP stage errors, camoufox returns docs that all fail
   `_is_brazilian`; assert `meta.error is True` and nothing was cached.
2. Run — fails (`error=False`, empty list cached).
3. Move `errored_any = False` inside an `if records:` guard.
4. Re-run — passes.
5. `git commit -m "fix(brazil-moh): keep error flag when browser docs filter empty"`

## Task 6 — Limiter registry must not pin dead event loops

**Files:** Modify `src/scholar_mcp/utils/http.py`; Test `tests/test_http_cache.py`.

1. Failing test: create a limiter under a throwaway loop, close and drop the
   loop, force a GC pass, assert the registry no longer holds that loop.
2. Run — fails.
3. Replace the flat dict with `weakref.WeakKeyDictionary[loop, dict[(host,
   rate), limiter]]`, plus one plain dict for the no-loop (sync caller) case.
4. Re-run — passes.
5. `git commit -m "fix(http): stop pinning dead event loops in limiter registry"`

## Task 7 — Title backfill inside the waterfall budget

**Files:** Modify `src/scholar_mcp/resolver.py`; Test `tests/test_waterfall_resolver.py`.

1. Failing test: with `total_budget_seconds=0`, a hit with an empty title skips
   the metadata backfill entirely.
2. Run — fails (backfill still runs).
3. Record the waterfall start, cap the backfill at the remaining budget
   (max 5 s), and skip it when nothing remains.
4. Re-run — passes.
5. `git commit -m "fix(resolver): bound title backfill by remaining budget"`

## Task 8 — Small fixes: scihub except, mirror penalty cap, camoufox timeout setting, AAP journal match

**Files:** Modify `providers/scihub.py`, `medical/brazil_moh.py`, `config.py`,
`medical/guidelines.py`; Tests `tests/test_search_scihub_providers.py`,
`tests/medical/test_guidelines.py`.

1. Failing tests: mirror penalty saturates at a cap; `Pediatrics in Review`
   counts as the AAP journal signal.
2. Run — fails.
3. Apply: `except Exception`; cap the penalty at `MAX_MIRROR_PENALTY`; add
   `brazil_browser_timeout_s` to `Settings`; match the AAP journal by prefix.
4. Re-run — passes.
5. `git commit -m "fix: scihub penalty cap, camoufox timeout setting, AAP journal prefix"`

## Task 9 — Full suite and push

1. `uv run --extra dev pytest` — must be fully green.
2. `git push origin fix/handoff-8-9-scholar-mcp`
