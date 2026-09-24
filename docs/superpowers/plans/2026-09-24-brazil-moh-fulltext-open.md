# Brazilian MoH full-text open Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an unreachable or oversized Brazilian full-text document fail fast, attributed, and without stalling the event loop — instead of burning the entire 30 s ceiling on a single hopeless TCP connect.

**Architecture:** Bound the connect phase of the shared HTTP client so a dead host is abandoned in seconds rather than the whole budget; classify unreachable hosts as `origin_outage` rather than `timeout`; carry a `timeout_phase` marker through every exit of `get_full_text`; move the synchronous PDF parse off the event loop behind a byte cap; and (secondary) cache the resolved record per id so a searched document no longer re-queries BVS.

**Tech Stack:** Python 3.11, httpx, pypdf, pytest + pytest-asyncio, `respx` (engine tests) and `httpx.MockTransport` (transport-level tests — `respx` rewrites `__cause__`, which some of these need intact).

**Spec:** `docs/superpowers/specs/2026-09-23-brazil-moh-fulltext-open-design.md` (read both this plan and the spec; the plan argues from the spec).

## Global Constraints

- `asyncio.to_thread` is permitted in exactly two places after this plan: `WaterfallResolver.download_article` (existing) and `BrazilMoHEngine._extract_pdf_text` (new). No others.
- `BvsErrorKind` is a closed set (`src/scholar_mcp/utils/sqlite_cache.py:31-33`). No new values may be added; D9b *widens* the meaning of the existing `origin_outage` value.
- Run the suite with the main repo venv python. Do not `uv sync` inside a worktree.
- Every changed task ends green on its own tests plus the existing `tests/test_http_deadline.py` and `tests/medical/test_brazil_moh.py` files, which must stay green (updated where the timeout type changes, per Task 1).

---

## Task 1 — Bound the connect phase (D9)

**Files:**
- Modify: `src/scholar_mcp/config.py` — add `connect_timeout_s` to `Settings` and its env loader.
- Modify: `src/scholar_mcp/utils/http.py:330-346` (client default timeout) and `:580-590` (per-attempt clamp).
- Test: `tests/test_http_connect_bound.py` (create).

**Interfaces:**
- Consumes: `Settings.request_timeout` (`config.py:33`, default 30).
- Produces: `Settings.connect_timeout_s: float` (default `5.0`); the client's default `httpx.Timeout` and the per-attempt `request_kwargs["timeout"]` are both now phase objects with `connect` bounded to `connect_timeout_s` and `read`/`write`/`pool` at `request_timeout`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_http_connect_bound.py
"""The connect phase is bounded separately from the read phase (spec D9)."""
import httpx
import pytest

from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient


def _client(handler, request_timeout=30, connect_timeout_s=5.0):
    client = AsyncHttpClient(
        settings=Settings(request_timeout=request_timeout,
                          connect_timeout_s=connect_timeout_s),
        max_retries=1, backoff_base=0.01, min_429_wait=0.0,
    )
    seen: dict = {}

    def counting_handler(request: httpx.Request):
        seen["timeout"] = request.extensions.get("timeout")
        return handler(request)

    client.client = httpx.AsyncClient(
        follow_redirects=True,
        transport=httpx.MockTransport(counting_handler),
    )
    return client, seen


async def test_connect_is_bounded_separately_from_read():
    client, seen = _client(lambda r: httpx.Response(200, text="ok"))
    try:
        await client.get("https://example.org/x")
        t = seen["timeout"]
        assert t is not None
        assert t.connect == pytest.approx(5.0)
        assert t.read == pytest.approx(30.0)
    finally:
        await client.aclose()


async def test_the_deadline_clamp_preserves_the_connect_bound():
    """A larger remaining budget must not re-raise connect to the request timeout."""
    import time
    client, seen = _client(lambda r: httpx.Response(200, text="ok"))
    try:
        await client.get("https://example.org/x",
                         deadline=time.monotonic() + 100.0)
        assert seen["timeout"].connect == pytest.approx(5.0)
        assert seen["timeout"].read == pytest.approx(30.0)
    finally:
        await client.aclose()
```

- [ ] **Step 2: Run to confirm they fail**

Run: `pytest tests/test_http_connect_bound.py -v`
Expected: FAIL — `connect_timeout_s` is not a `Settings` field / `request.extensions["timeout"]` is a `float`, not a phase object.

- [ ] **Step 3: Implement**

In `src/scholar_mcp/config.py`, add to the `Settings` dataclass next to `request_timeout: int = 30`:

```python
    connect_timeout_s: float = 5.0
```

and to the env loader next to the `request_timeout` line:

```python
            connect_timeout_s=_float_env("CONNECT_TIMEOUT_S", 5.0),
```

In `src/scholar_mcp/utils/http.py`, replace the client default in `__init__` (the line building `self.client` with `timeout=float(self.settings.request_timeout)`):

```python
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=float(self.settings.connect_timeout_s),
                read=float(self.settings.request_timeout),
                write=float(self.settings.request_timeout),
                pool=float(self.settings.request_timeout),
            ),
            follow_redirects=True,
        )
```

Replace the scalar clamp `request_kwargs["timeout"] = min(float(self.settings.request_timeout), remaining)` with:

```python
                request_kwargs["timeout"] = httpx.Timeout(
                    connect=min(float(self.settings.connect_timeout_s), remaining),
                    read=min(float(self.settings.request_timeout), remaining),
                    write=min(float(self.settings.request_timeout), remaining),
                    pool=min(float(self.settings.request_timeout), remaining),
                )
```

- [ ] **Step 4: Run to confirm they pass**

Run: `pytest tests/test_http_connect_bound.py -v`
Expected: PASS.

- [ ] **Step 5: Update the existing deadline suite to the new timeout type, then run it**

`tests/test_http_deadline.py` records `request.extensions.get("timeout")` into `calls["timeouts"]`. Any assertion that compared that value to a scalar (`== 30`, `<= N`) now compares a phase object. Change such assertions to read the read phase, e.g. `t is None or t.read <= N` (and `t.read` where a value is required). Do not weaken the assertion; only change how the value is read.

Run: `pytest tests/test_http_deadline.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/config.py src/scholar_mcp/utils/http.py \
        tests/test_http_connect_bound.py tests/test_http_deadline.py
git commit -m "feat(http): bound the connect phase separately from the read"
```

---

## Task 2 — Unreachable hosts classify as `origin_outage` (D9b)

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py:709-722` (`_classify_failure`).
- Modify: `AGENTS.md` §10 (error taxonomy).
- Test: `tests/medical/test_brazil_moh_fulltext_open.py` (create).

**Interfaces:**
- Consumes: `FetchFailure` (`utils/http.py:119`), whose `detail` carries the exception class name (e.g. `"ConnectTimeout"`).
- Produces: `_classify_failure` maps a transport failure whose `detail` names a connect failure to `"origin_outage"`; all other transport failures still map to `"timeout"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/medical/test_brazil_moh_fulltext_open.py
"""Attribution and fast failure on the Brazilian full-text open path."""
import pytest
from httpx import ConnectTimeout, ReadTimeout

from scholar_mcp.medical.brazil_moh import _classify_failure
from scholar_mcp.utils.http import FetchFailure


def test_a_connect_failure_is_an_origin_outage():
    assert _classify_failure(
        FetchFailure("transport", None, "ConnectTimeout")) == "origin_outage"


def test_a_connect_error_is_an_origin_outage():
    assert _classify_failure(
        FetchFailure("transport", None, "ConnectError")) == "origin_outage"


def test_a_read_timeout_still_classifies_as_timeout():
    assert _classify_failure(
        FetchFailure("transport", None, "ReadTimeout")) == "timeout"
```

- [ ] **Step 2: Run to confirm they fail**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py -v`
Expected: FAIL — connect failures currently return `"timeout"`.

- [ ] **Step 3: Implement**

In `_classify_failure`, inside the `if getattr(failure, "kind", "") == "transport":` branch, check the detail before returning `"timeout"`:

```python
    if getattr(failure, "kind", "") == "transport":
        detail = str(getattr(failure, "detail", "") or "")
        # An unreachable host is not serving — an outage, not a slow read.
        # origin_outage is also the breaker-exempt kind, which is correct:
        # a dead third-party host must not trip the BVS breaker.
        if detail in ("ConnectTimeout", "ConnectError", "NetworkError"):
            return "origin_outage"
        return "timeout"
```

- [ ] **Step 4: Update `AGENTS.md` §10**

In the taxonomy description, extend the `origin_outage` entry: it now also covers transport connect failures (a host that accepts no TCP connection), not only 5xx responses, and it remains the breaker-exempt kind.

- [ ] **Step 5: Run to confirm they pass**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py AGENTS.md \
        tests/medical/test_brazil_moh_fulltext_open.py
git commit -m "fix(brazil_moh): classify an unreachable host as origin_outage"
```

---

## Task 3 — `timeout_phase` attribution (D7)

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py` — `get_full_text` (every exit), `_timeout_result` (`1848-1857`), `_serve_local_text` (`1783`), the fetch branches (`1936-1970`).
- Modify: `src/scholar_mcp/server.py:685-701` (docstring).
- Test: `tests/medical/test_brazil_moh_fulltext_open.py` (extend).

**Interfaces:**
- Produces: a `timeout_phase` key in the returned payload (`"lookup"` / `"fetch"` / `None`) on **every** exit of `get_full_text`; written into the degraded cache row beside `_CACHED_ERROR_KIND_KEY`; logged with the phase and `FetchFailure.detail`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/medical/test_brazil_moh_fulltext_open.py
import pytest
import pytest_asyncio


async def test_timeout_phase_is_fetch_when_the_pdf_host_is_unreachable():
    from scholar_mcp.medical.brazil_moh import BrazilMoHEngine
    # A fast lookup that succeeds, then a document URL on a dead host.
    payload, meta = await _open_with_dead_document(tmp_path)
    assert payload["timeout_phase"] == "fetch"
    assert payload.get("status") in ("error", "success")
    assert payload.get("abstract_fallback") in (True, False, None)
```

Provide `_open_with_dead_document(tmp_path)` as a fixture helper in the test module: build the engine via the existing `tests/medical/test_brazil_moh.py` construction pattern (`http_client, cache, engine = await _engine(tmp_path)`), `respx`-mock `BVS_SEARCH_URL` to return a record whose `document_url` is a host that the mock answers with a connect failure, and await `engine.get_full_text("biblio-x", ceiling bound by a short `brazil_fulltext_timeout_s` in its `Settings`).

- [ ] **Step 2: Run to confirm it fails**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py::test_timeout_phase_is_fetch_when_the_pdf_host_is_unreachable -v`
Expected: FAIL — no `timeout_phase` key in the payload.

- [ ] **Step 3: Implement**

Introduce `timeout_phase: str | None = None` near the top of `get_full_text`. Set it:
- to `"lookup"` in the lookup-timeout return (`_timeout_result`, called at `1921`);
- to `"fetch"` in both fetch branches (`1941-1945` and `1965-1970`);
- leave it `None` elsewhere.

Add `"timeout_phase": timeout_phase` to every payload dict: the `_timeout_result` return, the `local:` path in `_serve_local_text` (`1777-1783`), the no-abstract error return (`1984-1994`), and the success payload (`2016-2019`). Write it into the degraded cache row alongside `_CACHED_ERROR_KIND_KEY` (`2018`). Add the phase to both `logger.warning` calls (`1941`, `1965`) alongside the elapsed split and `FetchFailure.detail`.

- [ ] **Step 4: Update the `get_brazil_moh_full_text` docstring**

In `server.py`, document that the response carries `timeout_phase` (`"lookup"`, `"fetch"`, or absent/`null`) and the explicit `abstract_fallback` flag.

- [ ] **Step 5: Run to confirm they pass**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py src/scholar_mcp/server.py \
        tests/medical/test_brazil_moh_fulltext_open.py
git commit -m "feat(brazil_moh): attribute the full-text timeout to its phase"
```

---

## Task 4 — Parse off the event loop (D8a)

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py:1723-1729` (`_extract_pdf_text` parse site).
- Modify: `AGENTS.md` §1 (the `to_thread` permit).
- Test: `tests/medical/test_brazil_moh_fulltext_open.py` (extend).

**Interfaces:**
- Produces: the PDF text is produced on a worker thread, so the 30 s ceiling becomes enforceable during parsing and a concurrent call is not stalled.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/medical/test_brazil_moh_fulltext_open.py
import asyncio


async def test_parse_does_not_stall_the_event_loop(monkeypatch):
    """A slow parse must not block a concurrent coroutine (spec D8)."""
    import scholar_mcp.medical.brazil_moh as bm

    async def sleeper(_bytes):
        await asyncio.sleep(0.3)
        return "text"

    # The real parse is sync; wrap it so the test can sleep without a real PDF.
    monkeypatch.setattr(bm, "pdf_bytes_to_text", lambda b: "text")
    engine = (await _engine(tmp_path))[2]

    import time
    start = time.monotonic()
    async def other():
        return "done"
    results = await asyncio.gather(
        _force_extract_sleeping(engine),  # helper that patches the parser to sleep 0.3s
        asyncio.create_task(other()),
    )
    # The concurrent task finished well before the 0.3 s parse.
    assert results[1] == "done"
```

Implement `_force_extract_sleeping(engine)` by monkeypatching `engine`'s `pdf_bytes_to_text` to `time.sleep(0.3)` and calling `_extract_pdf_text` against a mock 200 PDF response. Assert a concurrently scheduled coroutine (no sleep) completed before the parse returned. This **fails today** because the sync parse blocks the loop.

- [ ] **Step 2: Run to confirm it fails**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py::test_parse_does_not_stall_the_event_loop -v`
Expected: FAIL — the concurrent task is delayed by the blocking parse.

- [ ] **Step 3: Implement**

At the parse site in `_extract_pdf_text`, replace:

```python
            return pdf_bytes_to_text(resp.content), None
```

with:

```python
            return await asyncio.to_thread(pdf_bytes_to_text, resp.content), None
```

Ensure `asyncio` is imported at module scope (`import asyncio` — it already is, given the `wait_for` usage).

- [ ] **Step 4: Update `AGENTS.md` §1**

§1 currently permits `asyncio.to_thread` in exactly one place (`WaterfallResolver.download_article`). Add the second, `BrazilMoHEngine._extract_pdf_text`, with the reason: CPU-bound PDF extraction cannot be made cancellable or kept off the loop's critical path any other way, and a stall there blocks every concurrent tool call in the process.

- [ ] **Step 5: Run to confirm they pass**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py AGENTS.md \
        tests/medical/test_brazil_moh_fulltext_open.py
git commit -m "fix(brazil_moh): parse the PDF off the event loop"
```

---

## Task 5 — Byte cap before the parse (D8b)

**Files:**
- Modify: `src/scholar_mcp/config.py` — add `brazil_pdf_max_bytes`.
- Modify: `src/scholar_mcp/medical/brazil_moh.py:1716-1726` (gate before parse).
- Test: `tests/medical/test_brazil_moh_fulltext_open.py` (extend).

**Interfaces:**
- Produces: `Settings.brazil_pdf_max_bytes: int` (default `64 * 1024 * 1024`, env `BRAZIL_PDF_MAX_BYTES`); a body over the cap returns `("", "backend_error")` without invoking the parser.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/medical/test_brazil_moh_fulltext_open.py
@pytest.mark.parametrize("body_len,parsed", [(1024, True), (1025, False)])
async def test_byte_cap_boundary(tmp_path, respx_mock, monkeypatch, body_len, parsed):
    """At the cap the body is parsed; one byte over it is not (spec D8)."""
    import scholar_mcp.medical.brazil_moh as bm
    calls = {"n": 0}

    def spy(content):
        calls["n"] += 1
        return "texto"

    monkeypatch.setattr(bm, "pdf_bytes_to_text", spy)
    engine = await _engine_with(tmp_path, brazil_pdf_max_bytes=1024)
    respx_mock.get(DOC_URL).mock(return_value=httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=b"x" * body_len))
    text, kind = await engine._extract_pdf_text(DOC_URL)
    assert (calls["n"] == 1) is parsed
    if parsed:
        assert (text, kind) == ("texto", None)
    else:
        assert (text, kind) == ("", "backend_error")
```

- [ ] **Step 2: Run to confirm they fail**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py::test_byte_cap_boundary -v`
Expected: FAIL — `brazil_pdf_max_bytes` does not exist.

- [ ] **Step 3: Implement**

In `config.py`, add to `Settings` and its env loader:

```python
    brazil_pdf_max_bytes: int = 64 * 1024 * 1024
```
```python
            brazil_pdf_max_bytes=_int_env("BRAZIL_PDF_MAX_BYTES", 64 * 1024 * 1024),
```

In `_extract_pdf_text`, before the parse:

```python
        if len(resp.content) > self.settings.brazil_pdf_max_bytes:
            logger.info(
                "brazil_moh full text exceeds the byte cap (%d > %d)",
                len(resp.content), self.settings.brazil_pdf_max_bytes,
            )
            return "", "backend_error"
        return await asyncio.to_thread(pdf_bytes_to_text, resp.content), None
```

- [ ] **Step 4: Confirm the default against the corpus before shipping**

Before merging, download the largest known real document referenced by the bundled catalog (a PCDT or A-Z PDF) and confirm its byte size is under `brazil_pdf_max_bytes`. If any corpus document exceeds the default, raise the default to admit it — the spec requires the cap not regress any working fetch. Record the measured maximum in a comment next to the default.

- [ ] **Step 5: Run to confirm they pass**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/config.py src/scholar_mcp/medical/brazil_moh.py \
        tests/medical/test_brazil_moh_fulltext_open.py
git commit -m "feat(brazil_moh): cap the PDF body before parsing"
```

---

## Task 6 — Per-id record cache (D6, secondary)

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py` — add `_record_cache_key`, `_cache_records`; write at `search_guidelines` (`1478-1482`, `1489-1492`); read in `_resolve` (`1871-1906`).
- Modify: `AGENTS.md` §3 (caching policy).
- Test: `tests/medical/test_brazil_moh_fulltext_open.py` (extend).

**Interfaces:**
- Consumes: `BrazilGuideline.from_dict` (`1187`), `cache.set/get`.
- Produces: a `brazil_moh_record:{CACHE_SCHEMA}:{record_id}` row written at search time and consulted before `_lookup_record`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/medical/test_brazil_moh_fulltext_open.py
async def test_search_then_open_issues_no_bvs_lookup(tmp_path, respx_mock):
    """Opening an id the search just returned must not re-query BVS (spec D6)."""
    engine = (await _engine(tmp_path))[2]
    respx_mock.get(BVS_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_one_record_doc("biblio-1", "http://ok.example/x.pdf"))
    )
    records = await engine.search_guidelines("query", collection="all")
    # Now re-open; the record row should already be cached.
    payload, meta = await engine.get_full_text("biblio-1")
    # No second BVS id:"..." request was issued.
    assert len([c for c in respx_mock.calls if "id:" in str(c.request.url)]) == 1
```

(`_one_record_doc` builds the minimal BVS Solr JSON for one record; the mock is set so the *open* path's BVS call would be a second hit on the same route, which the test asserts never happened.)

- [ ] **Step 2: Run to confirm it fails**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py::test_search_then_open_issues_no_bvs_lookup -v`
Expected: FAIL — a second BVS request is issued.

- [ ] **Step 3: Implement**

Add:

```python
def _record_cache_key(record_id: str) -> str:
    return f"brazil_moh_record:{CACHE_SCHEMA}:{record_id}"
```

Add `_cache_records(self, records)` that writes each `record.to_dict()` under `_record_cache_key(record.id)` with the standard TTL (`self.cache.set(key, record.to_dict(), source="brazil_moh")`), called once after the merge at both the clean write (`1478`) and the degraded write (`1489`) paths.

In `_resolve`, after the offline `pcdt_engine`/`az_engine` lookups and before `_lookup_record`:

```python
            record_cache_key = _record_cache_key(normalized)
            cached_record, rec_meta = await self.cache.get(record_cache_key)
            if rec_meta.cached and cached_record is not None:
                return BrazilGuideline.from_dict(cached_record), False, {}, None
```

- [ ] **Step 4: Update `AGENTS.md` §3**

List the per-id `brazil_moh_record` row among the cached medical artifacts, noting it uses the standard 30-day Brazil TTL and is written at search time so an open of a searched document costs no BVS lookup.

- [ ] **Step 5: Run to confirm they pass**

Run: `pytest tests/medical/test_brazil_moh_fulltext_open.py tests/medical/test_brazil_moh.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py AGENTS.md \
        tests/medical/test_brazil_moh_fulltext_open.py
git commit -m "feat(brazil_moh): cache resolved records per id"
```

---

## Verification (whole plan)

```bash
pytest tests/test_http_deadline.py tests/test_http_connect_bound.py \
       tests/medical/test_brazil_moh_fulltext_open.py tests/medical/test_brazil_moh.py -v
python -c "from scholar_mcp.server import main; print('Import OK')"
```

Gate (from the spec): a dead host no longer consumes the full ceiling — it fails within the connect bound plus one backoff and reports `error_kind=origin_outage`, `timeout_phase=fetch`; the healthy path is unchanged.

## Open items carried from the spec

- D9 blast radius: client-wide vs Brazil-only — this plan implements it **client-wide** per the spec's recommendation. If the reviewer wants Brazil-only, Task 1's clamp change must instead apply only on the Brazil path.
- `connect_timeout_s` default — validate against a slow-but-alive target (spec open item 3).
- D6 is secondary; if the reviewer wants it held with D5, Task 6 is droppable and the rest stands.

## Shared test fixtures

Every task's tests in `tests/medical/test_brazil_moh_fulltext_open.py` use these helpers. Put them at the top of that file in Task 2 (the task that creates it); later tasks only append tests.

```python
import asyncio
import time

import httpx
import pytest

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import BVS_SEARCH_URL, BrazilMoHEngine
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

# Must pass _is_allowed_host, or _extract_pdf_text returns ("", None) untouched.
DOC_URL = "https://bvsms.saude.gov.br/bvs/publicacoes/teste.pdf"


async def _engine_with(tmp_path, **settings_over) -> BrazilMoHEngine:
    settings = Settings(**settings_over)
    http = AsyncHttpClient(settings, max_retries=1, backoff_base=0.0, min_429_wait=0.0)
    cache = SQLiteCacheManager(tmp_path / "cache.sqlite", settings)
    return BrazilMoHEngine(http, cache, settings)


def _one_record_doc(record_id: str, doc_url: str, title: str = "Diretriz teste") -> dict:
    """Minimal BVS Solr JSON carrying one record with a document link."""
    return {"diaServerResponse": [{"response": {"docs": [{
        "id": record_id, "ti": [title], "ur": [doc_url],
        "ab": ["Resumo real do documento."], "da": "2020",
    }]}}]}
```

Check `_extract_docs` / `_build_record` in `brazil_moh.py` before relying on `_one_record_doc`'s field names; if the envelope differs, copy the shape from the fixture JSON the existing `tests/medical/test_brazil_moh.py` search tests already feed `respx`.

Task 3's helper — a lookup that succeeds then a dead document host:

```python
async def _open_with_dead_document(tmp_path, respx_mock):
    engine = await _engine_with(tmp_path, brazil_fulltext_timeout_s=2.0,
                                connect_timeout_s=0.2)
    respx_mock.get(BVS_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_one_record_doc("biblio-x", DOC_URL)))
    respx_mock.get(DOC_URL).mock(side_effect=httpx.ConnectTimeout("dead host"))
    return await engine.get_full_text("biblio-x")
```

In Task 3's test, call it as `payload, meta = await _open_with_dead_document(tmp_path, respx_mock)` and additionally assert `meta.error_kind == "origin_outage"` (from Task 2).

Task 4's loop-responsiveness test, concrete form (replaces the sketch in Task 4 Step 1):

```python
async def test_parse_does_not_stall_the_event_loop(tmp_path, respx_mock, monkeypatch):
    import scholar_mcp.medical.brazil_moh as bm

    def slow_parse(content):
        time.sleep(0.3)          # CPU-bound stand-in: blocks whatever thread runs it
        return "texto"

    monkeypatch.setattr(bm, "pdf_bytes_to_text", slow_parse)
    engine = await _engine_with(tmp_path)
    respx_mock.get(DOC_URL).mock(return_value=httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=b"%PDF"))

    finished = {}

    async def ticker():
        await asyncio.sleep(0.01)
        finished["tick"] = time.monotonic()

    start = time.monotonic()
    await asyncio.gather(engine._extract_pdf_text(DOC_URL), ticker())
    # Off-loop parse: the ticker completes long before the 0.3 s parse does.
    assert finished["tick"] - start < 0.15
```