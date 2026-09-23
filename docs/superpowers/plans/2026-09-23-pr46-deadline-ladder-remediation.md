# PR #46 review remediation — deadline-aware retry ladder

**Goal.** Close the findings from the review of
[PR #46](https://github.com/gustavokch/scholar-mcp/pull/46) without changing the
feature the PR adds: a retry ladder in `AsyncHttpClient.get` that ends itself
before the caller's ceiling does, so the terminal failure still carries the
status the host answered with.

**Architecture.** Two behavioural defects live in the new deadline path in
`src/scholar_mcp/utils/http.py`; the rest are hygiene. Nothing here touches the
public signature (`deadline: float | None = None`) or the `None` default that
keeps every non-opting caller on the pre-existing loop.

**Tech stack.** Python 3.11, httpx, pytest + pytest-asyncio, respx (engine
tests) and `httpx.MockTransport` (transport-level tests — respx rewrites
`__cause__`, which these tests need intact).

**Spec reference.** §2 failure taxonomy (`cdn_challenge` / `origin_outage` /
`timeout` / `backend_error`), S2.4 explicit degradation flags.

---

## Task 1 — a budget-exhausted call must not report someone else's failure

**Target files**

- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_deadline.py`

**Consumes / produces.** Consumes `AsyncHttpClient.last_failure`
(`ContextScoped[FetchFailure | None]`). Produces the same attribute, but only
ever describing the call that set it.

**Problem.** `last_failure` is cleared only on success, and `ContextScoped`
keeps a mutable dict that child tasks share with their parent. The pre-attempt
bailout at `http.py:550` guards with `if self.last_failure is None`, so when the
budget is already spent it keeps whatever the *previous* call left behind. A
stage that issues no request then reports the prior stage's HTTP status as its
own. The guard is correct *within* one call — it preserves the 503 from attempt
N when the limiter parks past the deadline before attempt N+1 — and wrong
*across* calls.

**Step 1 — failing test**

```python
async def test_spent_deadline_does_not_inherit_an_earlier_calls_failure():
    """A call that issues no request must not report the previous call's status.

    ``last_failure`` is cleared only on success and its ``ContextScoped`` dict is
    shared with child tasks, so a stale ``FetchFailure`` outlives the call that
    produced it. Reporting it here hands the caller an HTTP status this call
    never received, and the ``§2`` classification is made from that status.
    """
    client, calls = _client(_always_503, max_retries=1)
    try:
        await client.get("https://example.org/degraded")
        assert client.last_failure is not None
        assert client.last_failure.status == 503

        assert (
            await client.get(
                "https://example.org/degraded", deadline=time.monotonic()
            )
            is None
        )
        assert calls["count"] == 1  # the second call issued nothing
        failure = client.last_failure
        assert failure is not None
        assert failure.kind == "transport"
        assert failure.status is None
        assert failure.detail == "DeadlineExceeded"
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()
```

**Step 2 — confirm failure**

```
uv run --extra dev pytest tests/test_http_deadline.py::test_spent_deadline_does_not_inherit_an_earlier_calls_failure -v
```

Expected: `assert failure.kind == "transport"` fails with `"http"`.

**Step 3 — implementation.** Add a per-call flag next to `last_attempt_cost`:

```python
recorded_failure = False
```

Set it `True` at each site in the loop that writes `self.last_failure`, and
change the bailout guard from `if self.last_failure is None:` to
`if not recorded_failure:`.

**Step 4 — confirm pass**

```
uv run --extra dev pytest tests/test_http_deadline.py -v
```

Both the new test and `test_ladder_stops_when_the_next_retry_cannot_fit_the_deadline`
(which depends on the intra-call preservation) must pass.

**Step 5 — commit**

```
git add src/scholar_mcp/utils/http.py tests/test_http_deadline.py
git commit -m "fix(http): scope the deadline bailout's last_failure to the call"
```

---

## Task 2 — the retry gate must cost the limiter park it is about to pay

**Target files**

- Modify: `src/scholar_mcp/utils/http.py`
- Test: `tests/test_http_deadline.py`

**Consumes / produces.** Consumes `AsyncRateLimiter.rate_per_sec` and
`.throttled_until` (plain reads; `_reserve` cannot be peeked without consuming a
token). Produces a retry decision that accounts for the spacing.

**Problem.** The gate weighs `wait_time + last_attempt_cost`, then `continue`s
into `await limiter.acquire()`. Production BVS is a 1 req/s bucket, and on the
429/shielded-403 branch `limiter.throttle(wait_time)` was just installed two
lines above. So the estimate is short by at least one token interval, often by
the whole throttle. The gate approves a retry, the call parks in `acquire()`
past the deadline, and the caller's `wait_for` cancels it there — reintroducing
the statusless cancellation this PR exists to remove.

**Step 1 — failing test**

```python
async def test_gate_costs_the_limiter_spacing_before_retrying():
    """The next attempt's price includes the bucket it has to wait for.

    Backoff alone fits the budget here; backoff plus the 1 req/s spacing does
    not. A gate blind to the limiter approves the retry, parks in ``acquire``
    past the deadline, and the caller's ceiling — not the ladder — ends the
    call, taking the 503 with it.
    """
    client, calls = _client(_always_503, backoff_base=0.01)
    limiter = AsyncRateLimiter(rate_per_sec=1.0)
    await limiter.acquire()  # drain the burst token: the next one costs ~1 s
    client._limiter_for_url = lambda url: limiter
    try:
        resp = await client.get(
            "https://example.org/degraded", deadline=time.monotonic() + 0.3
        )
        assert resp is None
        assert calls["count"] == 1
        failure = client.last_failure
        assert failure is not None
        assert failure.status == 503
    finally:
        await client.aclose()
        AsyncHttpClient.reset_dead_hosts()
```

**Step 2 — confirm failure**

```
uv run --extra dev pytest tests/test_http_deadline.py::test_gate_costs_the_limiter_spacing_before_retrying -v
```

Expected: the gate lets the retry through, the second `acquire()` parks ~1 s,
and the assertion on `calls["count"]` fails (or the test runs a second past its
deadline).

**Step 3 — implementation.** One helper beside the loop:

```python
def _limiter_spacing(limiter: AsyncRateLimiter) -> float:
    """Lower bound on what the next ``acquire`` will cost.

    ``_reserve`` consumes a token, so the real figure cannot be peeked. A
    full token interval, or the remainder of an installed throttle, is the
    part that is knowable without taking the lock -- and it is the part the
    gate is currently blind to.
    """
    interval = 1.0 / limiter.rate_per_sec if limiter.rate_per_sec > 0 else 0.0
    return max(interval, limiter.throttled_until - time.monotonic())
```

Add its result to both gates: the status gate (`http.py:618`) and the transport
gate (`http.py:685`).

**Step 4 — confirm pass**

```
uv run --extra dev pytest tests/test_http_deadline.py tests/medical/test_brazil_moh.py -v
```

`test_5xx_ladder_stops_inside_the_stage_ceiling_and_keeps_the_status` asserts
`route.call_count >= 1`, so a gate that now refuses earlier still satisfies it.

**Step 5 — commit**

```
git add src/scholar_mcp/utils/http.py tests/test_http_deadline.py
git commit -m "fix(http): price the limiter park into the deadline retry gate"
```

---

## Task 3 — hygiene, no behaviour change

**Target files**

- Modify: `src/scholar_mcp/utils/http.py`,
  `src/scholar_mcp/medical/brazil_moh.py`, `tests/test_http_deadline.py`

No new test: every item is either a comment, a statement move, or test
teardown. The existing suite is the check.

1. `http.py:567` — hoist `attempt_started = time.monotonic()` above the `try`.
   It is read in the `except`, and is safe today only because it is the first
   statement inside; any line inserted above it turns a transport error into
   `UnboundLocalError`.
2. `http.py:606` — record in the comment that the throttle is installed before
   the deadline gate on purpose: the server asked for backoff, and the sibling
   coroutines on that bucket should honour it even though this caller is giving
   up.
3. `brazil_moh.py:1812` — move `deadline = budget_start + ceiling if ceiling > 0
   else None` above `async def _resolve()` (`:1768`), which closes over it.
4. ~~`tests/test_http_deadline.py:46` — close the `AsyncClient` that
   `AsyncHttpClient.__init__` built before replacing it.~~ **Skipped.** The
   replaced client never issues a request, so its pool is empty and nothing
   leaks: `pytest -W error::ResourceWarning` on the file is clean. Making
   `_client` async to `await aclose()` it would rewrite nine call sites to fix
   a warning that is not emitted.
5. `tests/test_http_deadline.py:99` — drop the `slept` counter, written and
   never asserted.

**Verify**

```
uv run --extra dev pytest
```

**Commit**

```
git add -A
git commit -m "chore(http): tidy the deadline ladder's edges"
```
