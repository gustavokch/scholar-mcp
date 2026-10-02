# PR #55 Review Remediation — BVS breaker reset by shield-200

- **PR:** gustavokch/scholar-mcp#55 (`fix/bvs-short-chain-and-guard`)
- **Date:** 2026-10-02
- **Goal:** A 200 response carrying the CDN shield block-HTML (or other non-JSON
  garbage) must neither trip nor reset `BvsGuard`'s breaker; only a validated
  JSON answer resets it. Plus two nits from the review.

## Architecture

`BrazilMoHEngine._bvs_request` wraps the BVS GET in `BvsGuard.single_flight`.
Today `_work` calls `bvs_guard.record_success()` on *any* HTTP 200, before the
payload is parsed. `_fetch_records` later classifies a 200 carrying block-HTML
as `cdn_challenge` (`state.bvs_shielded = True`) and non-JSON garbage as
`backend_error` — but the breaker was already reset. During a degradation with
intermittent shield-200s the failure count never reaches
`brazil_breaker_threshold`, so the breaker never opens, contradicting the
documented invariant (AGENTS.md #14, `_bvs_request` docstring).

## Change

Move `record_success()` from `_bvs_request._work` to `_fetch_records`, after
`resp.json()` succeeds. The call still runs in the issuer task (inside the
single-flight work), exactly once per shared request.

## Tech Stack

Python 3.10, pytest, respx, httpx. Tests: `tests/medical/test_brazil_moh.py`.

## Task 1: Failing test — shield-200 does not reset the breaker

- **Modify:** `tests/medical/test_brazil_moh.py`
- Drive `engine._fetch_records` directly (same pattern as
  `test_only_origin_failures_count_against_the_breaker`):
  1. stub `http_client.get` to fail 502 → assert not tripped (1 failure).
  2. stub it to return a 200 HTML page with a shield marker → assert
     `state.bvs_shielded` and still not tripped (failure count preserved).
  3. stub it to fail 502 again → assert `engine.bvs_guard.tripped() ==
     "origin_outage"` (threshold 2 reached only if step 2 did not reset).
- Run: `pytest tests/medical/test_brazil_moh.py -k shield_block_html -x` → RED.

## Task 2: Move `record_success` past payload validation

- **Modify:** `src/scholar_mcp/medical/brazil_moh.py`
- Remove `record_success()` from `_bvs_request._work`'s `resp is not None`
  branch; add it in `_fetch_records` right after `data = resp.json()` succeeds.
- Update `_bvs_request` docstring ("a response resets it" → a validated JSON
  answer resets it) and add a one-line note that the single-flight key omits
  `deadline` deliberately (the waiter's own `_stage` wait bounds its wait).
- Run test → GREEN.

## Task 3: Docs + nits

- **Modify:** `AGENTS.md` #14 — "A shield 403" → "A shield outcome (403 or a
  block-HTML 200)".
- **Modify:** `tests/test_http_deadline.py` — collapse stray triple blank line.
- Full suite: `pytest -q` must be 100% green.

## Verification

- New test fails before / passes after the move.
- `tests/medical/test_bvs_guard.py` unchanged and green (guard semantics
  untouched).
- Full suite green; push `fix/bvs-short-chain-and-guard`.
