# PR #45 review remediation — BVS stage budget and 5xx retry ladder

**PR:** https://github.com/gustavokch/scholar-mcp/pull/45
**Branch:** `fix/bvs-502-retry-and-stage-budget`
**Review:** https://github.com/gustavokch/scholar-mcp/pull/45#issuecomment-5803683711
**Baseline:** 980 passed, 8 deselected (offline suite, 547 s).

## Goal

The load-bearing change in PR #45 — raising `brazil_stage_timeout_s` above the BVS
host's real TTFB — is correct and supported by paired live trials. Remediation does
not touch it. What this plan fixes is the set of claims the code makes that the
measured numbers do not support, the silent coupling the README does not name, a
duplicated status literal that can drift, and 19.3 s of real `asyncio.sleep` the new
tests added to the suite.

## Architecture

Three layers are involved and the budget flows one way through them:

```
Settings.brazil_stage_timeout_s (30 s)   <-- asyncio.wait_for around one stage
Settings.brazil_fulltext_timeout_s (30 s) <-- asyncio.wait_for around lookup + PDF
        |
        v
AsyncHttpClient.get()  max_retries=4, backoff_base=0.5
        |
        v
httpx.AsyncClient(timeout=request_timeout)  (30 s)   <-- hard per-attempt ceiling
```

The retry ladder sits *inside* the stage ceiling and is bounded only in attempt
count. Nothing in the ladder consults the remaining budget. That is not being
changed here — it is being documented honestly and pinned by a test.

## Tech stack

Python 3.10, pytest + pytest-asyncio, respx for HTTP mocking, `uv` for the venv.

Test command (the main checkout has no venv; a sibling worktree venv is borrowed
and `PYTHONPATH` is pinned to this worktree's `src`):

```
PYTHONPATH=$PWD/src \
  /Users/gus/Git/scholar-mcp/.worktrees/enamed-misses-engine-knobs/.venv/bin/pytest -q -p no:randomly
```

---

## Task 1 — `_BVS_RETRYABLE_STATUSES` must not drift from `RETRYABLE_STATUS_CODES`

**Modify:** `src/scholar_mcp/medical/brazil_moh.py`
**Test:** `tests/medical/test_brazil_moh.py`

**Consumes:** `scholar_mcp.utils.http.RETRYABLE_STATUS_CODES`
**Produces:** a `_BVS_RETRYABLE_STATUSES` that cannot silently fall behind it.

The two sets are now literally equal, and `AsyncHttpClient.get()` documents
`retryable_statuses` as a *narrowing* override, so the call-site pass is a no-op.
Keeping a hand-copied literal means a future widening of the shared constant leaves
BVS behind with nothing to catch it.

### Step 1: failing test

```python
def test_bvs_retryable_statuses_track_the_shared_default():
    """The BVS override must not silently fall behind the shared default.

    It is a *narrowing* hook (see ``AsyncHttpClient.get``), so while it holds no
    narrowing it must BE the shared set, not a copy of today's members: widening
    ``RETRYABLE_STATUS_CODES`` later would otherwise leave BVS behind in silence.
    """
    assert _BVS_RETRYABLE_STATUSES is RETRYABLE_STATUS_CODES
```

### Step 2: confirm failure

```
pytest tests/medical/test_brazil_moh.py::test_bvs_retryable_statuses_track_the_shared_default
```
Expect `AssertionError` — it is an equal but distinct `frozenset`.

### Step 3: implementation

Replace the literal with an alias of the shared constant and rewrite the comment to
say what the name now buys (a one-line per-host narrowing point), not what the
members are.

### Step 4: confirm pass

### Step 5: commit

```
git commit -m "refactor(brazil_moh): alias the BVS retry set to the shared default"
```

---

## Task 2 — retract the false time-bound claims

**Modify:** `src/scholar_mcp/medical/brazil_moh.py`, `src/scholar_mcp/config.py`
**Test:** none (documentation only; Task 4 pins the behaviour the text describes)

Two claims fail against this PR's own measurements:

- `brazil_moh.py:1568` — "bounded by `AsyncHttpClient.max_retries`, which keeps a
  host that is genuinely down inside the caller's budget". Four attempts against
  the measured degraded window (504 in 10–13.5 s) plus 3.5 s of backoff is
  43.5–57.5 s, against a 30 s `brazil_fulltext_timeout_s`.
- `config.py:84` — 30 s covers "the slowest observed response plus the retry ladder".
  Slowest observed success was 27.9 s; one fast 502 plus 0.5 s backoff plus that
  response is 28.9 s, and a second 5xx exceeds the ceiling.

Rewrite both to state the real bound: the caller's `asyncio.wait_for`. The ladder is
bounded in attempt count only, and a genuinely-down host converts into a timeout
rather than a classified outage. That is the accepted cost, so it belongs in the
text rather than being contradicted by it.

### Commit

```
git commit -m "docs(brazil_moh): state the real bound on the 5xx retry ladder"
```

---

## Task 3 — name the `SCHOLAR_REQUEST_TIMEOUT` cap in the README

**Modify:** `README.md`, `src/scholar_mcp/config.py`
**Test:** none (documentation only)

`AsyncHttpClient` builds `httpx.AsyncClient(timeout=float(settings.request_timeout))`
(`utils/http.py:331`), so every single attempt is cut at `SCHOLAR_REQUEST_TIMEOUT`
(default 30) regardless of the stage ceiling. The README's new row invites operators
to raise `BRAZIL_STAGE_TIMEOUT_S`; above 30 that buys nothing on its own.

Add the coupling to the `BRAZIL_STAGE_TIMEOUT_S` row and to the `config.py` comment.

### Commit

```
git commit -m "docs(config): note the request-timeout cap on the stage ceiling"
```

---

## Task 4 — pin the known cost: a 5xx ladder that overruns the stage ceiling

**Test:** `tests/medical/test_brazil_moh.py`
**Modify:** none

The PR's own "Known cost" section is the main argument against the retry widening:
in a degraded window the ladder converts a fast, honest `504` into a stage timeout
with `http_status=None`. Nothing pins that. A test makes the trade visible to
whoever next tunes these numbers, and fails loudly if the ladder later learns to
consult the remaining budget (which would be an improvement worth noticing).

**Consumes:** `engine.search_guidelines`, `engine.settings.brazil_stage_timeout_s`
**Produces:** an executable record of the degraded-window behaviour.

### Step 1: failing test

```python
@respx.mock
async def test_5xx_ladder_overrunning_the_stage_ceiling_reports_timeout(tmp_path: Path):
    """Known cost of retrying 5xx: a fast honest 504 becomes a stage timeout.

    In a degraded window the host answers 504 quickly and never recovers. The
    bounded ladder still costs attempts x TTFB, so the stage ceiling fires before
    the ladder ends and the caller loses the ``origin_outage`` classification --
    it sees ``timeout`` with no HTTP status instead. This is the trade the retry
    widening accepts, recorded so a later budget-aware ladder shows up as a
    failure here rather than passing unnoticed.
    """
```

Drive it with a `side_effect` that sleeps a little and returns 504 every time, a
stage ceiling below `attempts x sleep`, and the browser tier off. Assert
`meta.error_kind == "timeout"` and `meta.http_status is None`, and that more than
one request was made (so the ladder, not a single attempt, is what overran).

### Step 2: confirm the test fails for the intended reason

Run it first with `backoff_base` high enough / ceiling wide enough that the ladder
completes, and watch `error_kind == "origin_outage"` — that is the pre-widening
behaviour the assertion must reject.

### Step 3: no implementation — behaviour already exists

### Step 4: confirm pass

### Step 5: commit

```
git commit -m "test(brazil_moh): pin the stage timeout a 5xx ladder produces"
```

---

## Task 5 — stop the ladder tests sleeping 19.3 s of real time

**Modify:** `tests/medical/test_brazil_moh.py`, `tests/medical/test_enamed_2026_misses_engines.py`
**Test:** the suite's own wall clock

Measured on this branch:

| test | duration |
|---|---|
| `test_500_origin_outage_classified_and_not_cached` | 9.12 s |
| `test_extract_pdf_text_gives_up_after_the_retry_ladder` | 4.08 s |
| `test_lookup_record_retries_5xx_then_reports_outage` | 4.07 s |
| `test_search_retries_502_then_succeeds` | 1.01 s |
| `test_extract_pdf_text_retries_transient_5xx` | 1.01 s |

Before the widening these paths failed fast at ~0 s. Every other retry test in the
repo passes `backoff_base=0.01` explicitly; these go through `_engine()` helpers
that hardcode `AsyncHttpClient(settings)`.

**Consumes:** `AsyncHttpClient(settings, backoff_base=...)`
**Produces:** `_engine(..., backoff_base=...)` in both helpers, production default
preserved so no existing test changes behaviour.

### Step 1: thread the knob

Add a `backoff_base: float = 0.5` keyword to both `_engine` helpers. In
`test_enamed_2026_misses_engines.py` it must not land in `**settings_overrides` —
`Settings` has no such field, and `dataclasses.replace` would raise.

### Step 2: pass `backoff_base=0.01` from the five ladder tests

The two `_extract_pdf_text` tests build their client inline rather than via a helper;
pass the knob directly there.

### Step 3: confirm the same five tests still pass and the durations collapse

### Step 4: commit

```
git commit -m "test(brazil_moh): cut real sleep from the 5xx ladder tests"
```

---

## Task 6 — make the second-ladder assertion state its intent

**Modify:** `tests/medical/test_enamed_2026_misses_engines.py`

`assert len(route.calls) == first_calls * 2` is self-referential: change the ladder
length and both sides move together, so the assertion cannot fail for the reason it
exists (proving the outage was not cached and the second search paid a *full* second
ladder). Write it as `first_calls + http_client.max_retries`.

### Commit

```
git commit -m "test(brazil_moh): assert the second ladder by length, not by ratio"
```

---

## Verification

1. Targeted: the five ladder tests plus the two new ones.
2. Full offline suite. Gate: no regression against the 980-passed baseline, and the
   suite must not be slower than baseline.
3. `git push origin fix/bvs-502-retry-and-stage-budget`.
