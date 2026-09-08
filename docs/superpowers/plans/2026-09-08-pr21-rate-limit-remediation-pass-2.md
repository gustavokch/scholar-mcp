# PR #21 Remediation — Pass 2 Rate Limiter & HTTP Edge-Case Hardening

**Goal:** Resolve second-pass review findings on PR #21 (`fix/http-rate-limiting-and-throttling`) without regressing existing tests.

**Architecture:** `AsyncRateLimiter` (`src/scholar_mcp/utils/rate_limit.py`) bounds token acquisition and dynamic throttle durations; `AsyncHttpClient` (`src/scholar_mcp/utils/http.py`) safely normalizes host keys and parses retry headers across malformed edge cases.

**Tech stack:** Python 3.10, httpx, respx, pytest (`asyncio_mode = "auto"`).

**Spec reference:** PR #21 review comment pass 2
<https://github.com/gustavokch/scholar-mcp/pull/21#issuecomment-5590568642>

## Task 1 — Validate `rate_per_sec > 0` in `AsyncRateLimiter` (🟡)

- Modify: `src/scholar_mcp/utils/rate_limit.py`
- Test: `tests/test_rate_limit.py`
- Consumes: `rate_per_sec: float` in `__init__`
- Produces: `ValueError` if `rate_per_sec <= 0` or non-finite

1. Write failing tests: `AsyncRateLimiter(rate_per_sec=0)` and `AsyncRateLimiter(rate_per_sec=-1.0)` raise `ValueError`.
2. `pytest tests/test_rate_limit.py -k test_rate_limiter_rejects_invalid_rate` (expect failure).
3. In `AsyncRateLimiter.__init__`, validate `float(rate_per_sec)` is finite and `> 0`, else raise `ValueError("rate_per_sec must be positive and finite")`.
4. Re-run to confirm pass.
5. `git commit -m "fix(rate_limit): reject non-positive and non-finite rate_per_sec"`

## Task 2 — Guard `throttle(duration)` against `nan` / `inf` (🟡)

- Modify: `src/scholar_mcp/utils/rate_limit.py`
- Test: `tests/test_rate_limit.py`
- Consumes: `duration: float`
- Produces: `throttled_until` remains valid monotonic timestamp even with `nan`, `inf`, or negative durations

1. Write failing tests: `limiter.throttle(float("nan"))` and `limiter.throttle(float("inf"))` do not set `throttled_until` to `nan` or `inf`.
2. `pytest tests/test_rate_limit.py -k test_rate_limiter_throttle_non_finite` (expect failure).
3. In `AsyncRateLimiter.throttle`, check `math.isfinite(duration)` and clamp duration to `max(0.0, duration)` if finite, else ignore or set duration to 0.0.
4. Re-run to confirm pass.
5. `git commit -m "fix(rate_limit): sanitize non-finite durations in throttle"`

## Task 3 — Guard `_host_key` against `None` (🔵)

- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_cache.py`
- Consumes: `host: str | None`
- Produces: Normalized string, empty string for `None`/empty inputs without `AttributeError`

1. Write failing test: `_host_key(None)` returns `""`.
2. `pytest tests/test_http_cache.py -k test_host_key_none` (expect failure).
3. In `_host_key(host: str)`, use `hostname = (host or "").lower().strip()`.
4. Re-run to confirm pass.
5. `git commit -m "fix(http): handle None safely in _host_key"`

## Task 4 — Expand exception tuple in `_parse_retry_after` (🔵)

- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_cache.py`
- Consumes: Malformed RFC 7231 / RFC 2822 date string in `Retry-After`
- Produces: `None` instead of uncaught exception

1. Write test: `_parse_retry_after` with malformed date strings returning `None`.
2. Catch `(TypeError, ValueError, IndexError, OverflowError)` around `parsedate_to_datetime(raw)`.
3. Re-run to confirm pass.
4. `git commit -m "fix(http): expand exception handling for malformed Retry-After dates"`
