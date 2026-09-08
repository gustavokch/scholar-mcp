# PR #21 Remediation — HTTP Rate Limiting Hardening

**Goal:** Resolve the review findings on PR #21 (`fix/http-rate-limiting-and-throttling`) without
regressing the 516-test baseline.

**Architecture:** `AsyncHttpClient` (`src/scholar_mcp/utils/http.py`) owns a per-host-key dict of
`AsyncRateLimiter` buckets (`src/scholar_mcp/utils/rate_limit.py`). A 429 response both sleeps the
current request and calls `limiter.throttle(duration)` so sibling coroutines on the same host pause.

**Tech stack:** Python 3.10, httpx, respx, pytest (`asyncio_mode = "auto"`).

**Spec reference:** PR #21 review comment
<https://github.com/gustavokch/scholar-mcp/pull/21#issuecomment-5590191468>

## Task 1 — Bound `Retry-After` (🔴)

- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_cache.py`
- Consumes: `httpx.Response.headers["Retry-After"]`
- Produces: `_parse_retry_after -> float | None` guaranteed finite and `<= MAX_RETRY_AFTER`

1. Write failing tests: `Retry-After: inf`, `1e400`, and `NaN` return `None`; `86400` clamps to
   `MAX_RETRY_AFTER`; an HTTP-date far in the future clamps too.
2. `pytest tests/test_http_cache.py -k retry_after` (expect failure).
3. Add `MAX_RETRY_AFTER = 60.0`; guard the numeric branch with `math.isfinite`; clamp both the
   numeric and HTTP-date branches with `min(..., MAX_RETRY_AFTER)`.
4. Re-run to confirm pass.
5. `git commit -m "fix(http): reject non-finite and clamp oversized Retry-After"`

## Task 2 — Key limiters by hostname, not netloc (🟡)

- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_cache.py`

1. Write failing tests: `_host_key` on `user:pw@a.example.com` and on an IPv6 netloc; assert
   `AsyncHttpClient` keys a userinfo URL and a bare URL to the same limiter.
2. Run to confirm failure.
3. Pass `parsed.hostname or parsed.netloc` into `_limiter_for`; strip the `split(":")` port hack
   from `_host_key` while keeping lowercase normalization.
4. Re-run to confirm pass.
5. `git commit -m "fix(http): key rate limiters by hostname instead of netloc"`

## Task 3 — Make the 429 floor explicit (🟡)

- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_cache.py`

1. Write failing test: `AsyncHttpClient(min_429_wait=0.0)` retries a 429 in well under 1s, and the
   default client exposes `min_429_wait == 1.0`.
2. Run to confirm failure.
3. Add the `min_429_wait: float = 1.0` constructor argument; replace the
   `backoff_base >= 0.1` branch with it.
4. Re-run to confirm pass.
5. `git commit -m "fix(http): replace backoff_base heuristic with explicit min_429_wait"`

## Task 4 — Document the throttle concurrency contract (🟡)

- Modify: `src/scholar_mcp/utils/rate_limit.py`

1. No behavior change, so no new test; the existing `tests/test_rate_limit.py` is the guard.
2. Document on `throttle()` that it is sync and single-event-loop only, and why setting
   `last_update` forward is intentional.
3. Run `pytest tests/test_rate_limit.py`.
4. `git commit -m "docs(rate_limit): state throttle single-loop contract"`

## Task 5 — Separate the arXiv PDF bucket (🟡) — DROPPED

Investigation invalidated the finding. `src/scholar_mcp/providers/arxiv.py:8-9` puts both the API
(`export.arxiv.org/api/query`) and the PDF downloads (`export.arxiv.org/pdf`) on the *same*
hostname, so no host-key split can separate them. arXiv's published policy asks for no more than
one request every three seconds with a single connection at a time, and it applies to the whole
host — so 0.33 rps is the correct shared rate, and raising it for PDFs would breach the policy.
The slow bulk-PDF path is inherent, not a defect. No change made.

## Task 6 — Narrow the exception and tighten the throttle assertion (🔵)

- Modify: `src/scholar_mcp/utils/http.py`, `tests/test_http_cache.py`

1. Replace `except Exception` around `parsedate_to_datetime` with `(TypeError, ValueError)`.
2. Replace `assert limiter.throttled_until > 0.0` with an assertion against a monotonic baseline
   captured before the request.
3. Run the full suite; expect 516 plus the new tests, all green.
4. `git commit -m "refactor(http): narrow Retry-After parse except and strengthen throttle assert"`

## Deferred

The `DEFAULT_FALLBACK_RATE` 10.0 -> 5.0 drop and the unbounded `_limiters` dict are recorded in the
review but left in place: both are deliberate or pre-existing, and changing them is a policy call
for the PR author rather than a defect fix.
