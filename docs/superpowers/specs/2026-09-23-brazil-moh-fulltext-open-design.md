# Brazilian MoH full-text open — unreachable documents, budget, and attribution: design

Date: 2026-09-23, revised 2026-09-24 after live measurement
Status: design approved; revision 2 measured; approved for planning
Scope: `scholar_mcp.medical.brazil_moh` and the shared HTTP client it uses. No zimqa changes.

## Origin

One line from an ENAMED misses run:

```text
brazil_moh full text fetch for 'biblio-935743' exceeded its 30.0s budget
```

Revision 1 of this design inferred the cause from code: the belief was that the
BVS record lookup was consuming nearly the whole ceiling, leaving the PDF phase a
remainder. That inference was wrong, and it is recorded as refuted below, because
the refutation is what changes the fix.

## Measured evidence (live probe, revision 2)

Probed against the live services with the engine's own components, and a
throwaway cache. No repository code was modified.

| Probe | Result |
| --- | --- |
| `_lookup_record("biblio-935743")` | **0.20 s**, HTTP 200, record found |
| The record | title = "Diretrizes brasileiras para o rastreamento do câncer do colo do útero", `document_url = http://www1.inca.gov.br/inca/Arquivos/Diretrizes.PDF`, `has_full_text=True`, abstract 128 chars, `abstract_synthetic=True` |
| TCP `www1.inca.gov.br:80` / `:443` | `TimeoutError` at a 6 s cap, both ports, repeated |
| DNS `www1.inca.gov.br` | resolves — `200.33.96.27` |
| TCP `inca.gov.br:443`, `www.gov.br:443` | **0.04 s** |
| Raw `httpx` GET, browser UA, 60 s timeout | `ConnectTimeout` — never a response |
| `_extract_pdf_text(url, deadline=now+45)` | **30.01 s**, `err='timeout'`, 0 chars. Ladder logged `stopped after attempt 1/4 on ConnectTimeout: budget cannot fit another attempt` |

The institution has relocated: `inca.gov.br` → 301 → `www.inca.gov.br` → 404 on
the legacy path; the live site is `www.gov.br/inca/pt-br` (200). **No live mirror
of this PDF was found.** The recorded URL is dead.

Probe caveat: repeated requests to `pesquisa.bvsalud.org` began returning 403
challenge pages after the first probe; only the first lookup — issued through
`AsyncHttpClient` with its headers — succeeded. That is probe noise, not a design
finding, but it bounds how much of this can be measured live.

## Refuted: the slow-lookup mechanism

Revision 1 argued that `get_full_text` re-pays a BVS lookup and that the
`biblio-935743` line was "overwhelmingly likely to have taken that path". The
lookup takes **0.20 s**. It did not.

The record cache proposed in revision 1 (D6) therefore does not address this
failure, and moving work off the lookup phase changes nothing here. D6 survives
below on its own honest merits and is explicitly secondary; it is not the fix.

What actually happened: a dead host, a connect that could never complete, and a
timeout policy that let that single connect consume the entire ceiling.

## Evidence (code)

1. **A scalar timeout bounds `connect` at the full request budget.**
   `request_timeout: int = 30` (`config.py:33`) is passed as a scalar —
   `httpx.AsyncClient(timeout=float(self.settings.request_timeout))`
   (`utils/http.py:346`) — so httpx applies 30 s to connect, read, write and pool
   independently. The per-attempt clamp is
   `request_kwargs["timeout"] = min(float(settings.request_timeout), remaining)`
   (`utils/http.py:586-588`), which here is `min(30, ~29.8)` — a no-op. One
   hopeless connect therefore costs the whole ceiling.

2. **One ceiling, no reservation.** `ceiling = settings.brazil_fulltext_timeout_s`
   (30.0; `config.py:131,267`) and `deadline = budget_start + ceiling`
   (`brazil_moh.py:1869`) is handed to both phases. The docstring defends the
   absence of a reservation (`1799-1810`) so that a shortfall cannot cut the
   lookup short, and with a 0.2 s lookup that reasoning costs nothing here.

3. **Identical text, two causes.** The budget-already-spent branch (`1941-1945`)
   and the PDF `wait_for` expiry (`1965-1970`) log the same message and both set
   `error_kind="timeout"`.

4. **Every transport failure classifies as `timeout`.** `_classify_failure`
   (`709-722`) maps `kind == "transport"` → `"timeout"`, so an unreachable host is
   indistinguishable from a slow read, and counts as a timeout rather than an
   outage. `FetchFailure.detail` already carries the exception class name
   (`utils/http.py:131-132`), so the distinction is available and unused.

5. **A synthetic abstract cannot rescue this record.** The fallback gate is
   `record.abstract and not record.abstract_synthetic` (`1977`); this record's
   128-char abstract is a DeCS/search snippet, so the gate is False and the call
   returns `status="error"` (`1984-2002`) with nothing served. For this class the
   wall-clock bound is the only mitigation — there is no degradation to fall back
   on.

6. **The ceiling is unenforceable during the parse.** `pdf_bytes_to_text(resp.content)`
   (`1726`) is synchronous `pypdf` (`parsers/pdf.py:57`) on the event loop:
   `wait_for` cannot interrupt it, and it stalls every other in-flight call in the
   process. No byte cap precedes it.

7. **Repeat opens re-pay in full.** A timed-out lookup is never cached (`1921`);
   a failed fetch is cached at the degraded TTL but re-attempts the PDF next time
   (`2022-2030`).

8. **Secondary only — the common path re-pays a BVS lookup.** Search rows are
   cached by query key (`1180-1182`) with the records already materialised
   (`1478-1482`), and `get_full_text` resolves through `pcdt_engine` → `az_engine`
   → a live Solr `id:"..."` query (`1874-1881`, `1635-1641`). Measured cost here:
   0.20 s. The code's own note puts a *degraded-window* lookup at up to 27.9 s
   (`1860-1865`), which is the case D6 addresses.

**Not implicated.** `has_full_text` (`models.py:272`, set at `637`) and ranking
damping (`ranking.py:208-217`) are unchanged — though note the flag reads True for
a document whose URL is dead, because a trusted host validates it. That is a
ranking-input question, not this path's.

## Scope

In scope:

- A connect-phase bound, with the taxonomy consequence (D9).
- Phase-attributed timeout, in the log and in the payload (D7).
- PDF parse isolation behind a byte cap (D8).
- Per-id record caching as a secondary improvement (D6).
- `AGENTS.md` §1, §3, §10 amendments.

Out of scope:

- **Resurrecting dead document URLs.** INCA has relocated; finding a live mirror
  per legacy document is a data-curation project, not a client fix. This design's
  job is to fail fast and attribute correctly, not to repair the catalogue.
- The two-budget split (D5) — held, and now doubly irrelevant.
- Streaming the transfer; PDF text quality; BVS search-stage behaviour; ranking.

## Design

### D9 (primary) — bound the connect phase

**Decision.** Give the client per-phase timeouts rather than a scalar:
`httpx.Timeout(connect=connect_timeout_s, read=request_timeout, write=request_timeout, pool=request_timeout)`,
with a new `connect_timeout_s: float = 5.0`. The per-attempt clamp must preserve
the connect bound rather than re-raise it to `min(request_timeout, remaining)`.

**Why 5.0 s.** Measured: healthy hosts completed a TCP connect in 0.04–0.47 s; the
dead host completed none in 60 s. A connect is a handshake carrying no payload, so
a host that will not accept a socket within seconds is, against a 30 s budget,
equivalent to unreachable — and the ladder can still retry it, which is the point.
The plan must validate the default against a slow-but-alive target. Note the BVS
27.9 s measurement is *read* time, not connect, and is unaffected.

**Effect.** The INCA case becomes an attributed failure at roughly the connect
bound plus backoff — about 5–6 s instead of 30 — and leaves budget for a real
retry where one could help.

**Decision requested — blast radius.** The connect bound can be set client-wide
(the `httpx.Timeout` in `utils/http.py:346`, affecting every provider) or applied
per-request (the clamp already builds `request_kwargs` per call, so it can carry a
bounded connect for the Brazil path only). Recommendation: **client-wide**, since
a 30 s connect is never the desired behaviour anywhere, and the read phase — the
one that legitimately needs 30 s — is untouched either way.

### D9b — unreachable is an outage, not a timeout

**Decision.** Classify connect failures as `origin_outage` rather than `timeout`.
The information already exists: `FetchFailure.kind == "transport"` with
`detail` naming `ConnectTimeout`/`ConnectError` (`utils/http.py:131-132`,
`brazil_moh.py:709-722`).

**Why.** The origin is not serving — that is an outage by the taxonomy's own
meaning, and the search stages already treat 5xx that way. It matters concretely:
`origin_outage` is the one kind exempted from breaker counting (`AGENTS.md` §10),
so a dead third-party host stops being charged against the BVS breaker.

**This widens an existing value's meaning** rather than adding a value. It is a
taxonomy change and needs explicit sign-off; `AGENTS.md` §10 records the current
definition.

### D7 — Attribute the timeout

**Decision.** A local `timeout_phase` (`"lookup"` / `"fetch"` / `None`) is set at
each budget branch and carried into **every** exit path of `get_full_text` — the
early empty-id and cache-hit returns (`1819-1844`), the lookup-timeout result
(`1848-1857`), the local-corpus return (`1927-1929` via `_serve_local_text`), the
no-abstract error return (`1990-2002`), and the success payload (`2016-2019`) —
`None` wherever no timeout occurred. It is also written into the degraded cache
row alongside `_CACHED_ERROR_KIND_KEY`, and it is caller-visible — not stripped by
`_serve_full_text` (`2064-2066`), which removes cache bookkeeping only.

**Log.** Both existing messages gain the phase, the elapsed split, and
`FetchFailure.detail`, so a connect failure is named rather than reported as a
bare timeout.

**`error_kind` values do not change beyond D9b.** The phase is additive
information, not a new taxonomy value.

**Always present.** `timeout_phase: null` on a clean result, matching
`abstract_fallback`'s convention.

### D8 — Parse isolation behind a byte cap

**Decision.** `await asyncio.to_thread(pdf_bytes_to_text, resp.content)`, and a
`brazil_pdf_max_bytes` setting checked before parsing: over the cap returns
`backend_error` without invoking the parser.

**Why.** Two defects share one line of code (finding 6): a cross-request loop
block, and a ceiling that claims to bound a step it cannot interrupt. Moving the
parse to a thread makes cancellation land at the await, which is the only way the
clause means what it says.

**The cap is the mitigation for the cost that introduces.** An abandoned thread
keeps burning CPU until `pypdf` returns; the ceiling bounds the caller, not the
thread. Deliberately not a streaming rewrite — bounding the parse is the defect,
and the transfer is already bounded by the deadline.

**Default value is unvalidated, and the requirement on it is precise.** The
default (`brazil_pdf_max_bytes`) must be set from measurement of the largest real
document in the corpus during implementation — not guessed here — and it must
admit every document the corpus contains, so that D8 cannot silently regress a
fetch that works today. The one known data point is a 129k-character PCDT body.

**`AGENTS.md` §1 is amended, not excepted.** §1 permits `asyncio.to_thread` in
exactly one place (saving downloaded PDFs in `WaterfallResolver.download_article`).
This adds the second and states why: CPU-bound extraction cannot be made
cancellable, or kept off the loop's critical path, any other way.

### D6 — Per-id record cache *(secondary; not the fix for this failure)*

**Decision.** Write a `brazil_moh_record:{CACHE_SCHEMA}:{record_id}` row for each
merged record at search time, from the `to_dict()` values already in hand, with
the existing `SQLiteCacheManager`, source `brazil_moh`, standard 30-day TTL.
`get_full_text` consults it before `_lookup_record`.

**Justification, stated honestly.** This is not what caused the observed line —
the lookup measured 0.20 s. It removes a 0–28 s cost on a cold open inside a
degraded BVS window (`1860-1865`), which is a real class of budget starvation but
not a measured one. If the reviewer would rather hold this too, the rest of the
design stands without it.

**Write site.** `_cache_records(records)` once after the merge, beside the two
existing search-cache writes (`1478-1482`, `1489-1492`), at the standard TTL in
both branches: the degraded short TTL belongs to the *search* row, which may be
missing sources; each record row is individually complete.

**Read site.** Inside `_resolve()` (`1871-1906`), after the offline `pcdt_engine`
and `az_engine` lookups and before `_lookup_record`. Revival is
`BrazilGuideline.from_dict(cached_data)` — the same call the search cache read
already uses (`1187`).

**Cache-schema note.** No `CACHE_SCHEMA` bump: a new key prefix, `from_dict`
already fills absent fields, existing rows unaffected. The bump discipline at
`124-128` guards a *shape change to an existing row*, which this is not.

## Held — D5, the two-budget split

Splitting `brazil_fulltext_timeout_s` into lookup and fetch ceilings would protect
cold opens where a lookup is slow. Measurement has removed its motivation for this
failure: the lookup was 0.20 s, so there was no starvation to prevent — the fetch
had essentially the entire ceiling and a dead host consumed it.

Revisit condition unchanged: a replay showing a cold open whose `timeout_phase` is
`lookup`, or a fetch starting with under 5 s of ceiling left. The two ceilings must
sum below the caller's per-call adapter timeout (60 s in zimqa's `[scholar]
timeout_s`), and exceeding it forfeits even an abstract fallback.

Raising `brazil_fulltext_timeout_s` above 30 s is separately out of scope and
caller-bounded. With D9 in place it is also less urgent: the dead-host case no
longer scales with the ceiling.

## Published contract changes

| Change | Surface | Compatibility |
| --- | --- | --- |
| `timeout_phase` added | `get_brazil_moh_full_text` (`server.py:679`) | Additive; `degraded: true` already carries the boolean half (`server.py:149-150`) |
| `origin_outage` widened to cover unreachable hosts (D9b) | `BvsErrorKind` consumers | **Behavioural.** An existing value's meaning changes; consumers reading it must not treat prior `timeout` cases as unchanged |
| `connect_timeout_s` added to `Settings` | `config.py` + env loader | Additive |
| `brazil_pdf_max_bytes` added to `Settings` | `config.py` + env loader | Additive; must not regress any corpus document |
| Docstring documents `timeout_phase`, `abstract_fallback` | `server.py:685-701` | Documentation only |

## Testing

Failing-test-first, repo conventions (`respx` against the real `AsyncHttpClient`,
`httpx.MockTransport` where `__cause__` must survive).

1. **Dead host fails fast and attributed.** A transport that raises `ConnectTimeout`
   → the call returns within the connect bound plus one backoff, with
   `error_kind == "origin_outage"` and `timeout_phase == "fetch"` — and does not
   consume the ceiling.
2. **The clamp preserves the connect bound.** With a remaining budget larger than
   the connect bound, the per-attempt timeout must still bound connect — the
   regression that would silently undo D9.
3. **Read is unaffected.** A slow-but-alive response still gets the full
   `request_timeout`; D9 bounds one phase, not the request.
4. **The ladder can retry after a connect failure** when budget allows, which is
   the point of bounding connect rather than lowering the ceiling.
5. **`timeout_phase` pair.** Slow lookup → `"lookup"`; dead host → `"fetch"`;
   clean → `None`.
6. **Byte-cap boundary.** At the cap is parsed; one byte over is not, and returns
   `backend_error` with no content.
7. **The loop stays responsive.** Patch the parser with one that sleeps, run it
   concurrently with a coroutine that must finish well inside that sleep, and
   assert the second completes first. Fails today; asserts behaviour, not wiring.
8. **Regression.** Healthy fetch, and "a timed-out lookup is never cached",
   stay green.

## Risks

- **`connect_timeout_s = 5.0` may be too low for a congested-but-alive host.**
  Mitigated by configurability and by the retry ladder; the plan must validate it
  against a slow-but-alive target rather than only against a dead one.
- **Client-wide vs per-request blast radius** is an open decision (D9).
- **D9b changes an existing taxonomy value's meaning.** Flagged for sign-off.
- **D6 writes ~30 rows per search** — bounded, once per search. Retained as
  secondary; droppable without affecting the rest.
- **Abandoned parse threads** burn CPU until `pypdf` returns; the byte cap bounds
  the input, not the thread.
- **The byte-cap default is unmeasured.**

## Open items

1. D9 blast radius — client-wide or Brazil-only (recommendation: client-wide).
2. D9b taxonomy widening — sign-off requested.
3. `connect_timeout_s` default — validate against a slow-but-alive target.
4. `brazil_pdf_max_bytes` default — set from measurement.
5. D6 — retain as secondary, or hold it with D5.
6. `brazil_fulltext_timeout_s` above 30 s — separate decision.