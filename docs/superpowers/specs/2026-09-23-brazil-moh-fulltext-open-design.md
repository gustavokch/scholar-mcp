# Brazilian MoH full-text open — budget, attribution, and parse isolation: design

Date: 2026-09-23
Status: design approved; awaiting spec review
Scope: `scholar_mcp.medical.brazil_moh` full-text path. No zimqa changes.

## Origin

One line from an ENAMED misses run:

```text
brazil_moh full text fetch for 'biblio-935743' exceeded its 30.0s budget
```

Two distinct causes emit that exact string, so the line states neither which
phase ran out nor what the caller received. Investigation found three defects in
the open path, plus one decision that is deliberately held.

## Evidence

All line references are to `src/scholar_mcp/medical/brazil_moh.py` unless stated.

1. **Identical text, two causes.** The budget-already-spent branch (`1941-1945`,
   after the lookup succeeded) and the PDF `asyncio.wait_for` expiry
   (`1965-1970`) log the same message and both set `error_kind="timeout"`. The
   two are distinguishable in the returned payload — the lookup-timeout branch
   passes an empty title (`1921`), the fetch branch carries the record's — but
   nothing surfaces that where a reader of the log stream sees it.

2. **The common path re-pays a BVS lookup.** Search rows are cached by query key
   (`1180-1182`) with the record dicts already materialised (`1478-1482`).
   `get_full_text` never consults them: it resolves through `pcdt_engine` →
   `az_engine` → `_lookup_record` (`1874-1881`), the last being a live Solr
   `id:"..."` query (`1635-1641`). Opening a document the caller just found
   therefore repeats work that was already done and cached.

3. **One ceiling, no reservation.** `ceiling = settings.brazil_fulltext_timeout_s`
   (30.0 by default; `config.py:131,267`) and
   `deadline = budget_start + ceiling` (`1869`) is handed to both phases. The
   docstring defends the absence of a reservation (`1799-1810`): cutting the
   lookup short loses the record *and* the abstract fallback with it. That
   reasoning is sound for the lookup and is preserved here. The consequence,
   however, is that the PDF phase inherits `ceiling - lookup_elapsed`, and a
   healthy BVS lookup is measured at up to 27.9 s (`1860-1865`). The `biblio-*`
   case above is overwhelmingly likely to have taken the path in finding 2: the
   agent's only source of `record_id` values is a search it ran. That is a
   structural inference about the id's provenance, not a verified fact for that
   run.

4. **The ceiling is unenforceable where it matters.** `pdf_bytes_to_text(resp.content)`
   (`1726`) is synchronous `pypdf` (`parsers/pdf.py:57`) invoked directly on the
   event loop. `asyncio.wait_for` cancels only at an await, so an in-flight parse
   cannot be interrupted — the ceiling holds between awaits, not during the
   expensive step it exists to bound. The same call blocks every other in-flight
   tool call in the process for the duration of the parse. No byte cap precedes
   it.

5. **Repeat opens re-pay in full.** A timed-out lookup is never cached (`1921`);
   a failed fetch is cached at the degraded TTL but re-attempts the PDF on the
   next request (`2022-2030`).

**Not implicated.** `has_full_text` (`models.py:272`, set at `637`), ranking
damping (`ranking.py:208-217`), and `serve_body` passages have all landed. They
are why a body-less card is damped rather than dropped; they are not why the 30 s
was spent. Nothing here changes them.

**Attribution confidence.** Findings 2 and 3 together describe a mechanism that
fits the observed line: a non-catalogue id provokes a live lookup measured at up
to 27.9 s against a shared 30 s ceiling. Finding 4 is read directly off the call
graph. Which branch actually fired for `biblio-935743` is *not* known — that is
what finding 1 costs us, and what D7 fixes. Both fixes below are correct under
either attribution, which is why no diagnostic round precedes them.

## Scope

In scope:

- Per-id caching of resolved records, written at search time (D6).
- Phase-attributed timeout, in the log and in the payload (D7).
- PDF parse isolation behind a byte cap (D8).
- `AGENTS.md` §1 amendment (D8), plus the §3 and §10 notes below.

Out of scope:

- The two-budget split (D5) — held, with a stated revisit condition.
- Streaming the PDF download. The byte cap bounds the parse, not the transfer;
  the deadline already bounds the transfer.
- PDF text extraction quality, dehyphenation, running-header removal.
- BVS error taxonomy and breaker semantics. `error_kind` values do not change.
- Ranking, `has_full_text`, passages.

## Design

### D6 — Cache the resolved record per id

**Decision.** Write a `brazil_moh_record:{CACHE_SCHEMA}:{record_id}` row for each
merged record at search time, from the `to_dict()` values already in hand, using
the existing `SQLiteCacheManager` with source `brazil_moh` and the standard
30-day TTL. `get_full_text` reads that row before attempting `_lookup_record`.

**Why this and not a bigger ceiling.** The cost being removed is work that has
already been done. Enlarging the ceiling scales the waste instead of eliminating
it, and still leaves a remainder rather than a budget.

**Write site.** A `_cache_records(records)` helper called once after the merge,
next to the two existing search-cache writes (`1478-1482`, `1489-1492`). Both
branches write with the *standard* TTL: the degraded short TTL applies to the
search row, because that row is a merge that may be missing sources. Each record
row is individually complete regardless of which sources answered, so it carries
the normal TTL.

**Read site.** Inside `_resolve()` (`1871-1906`), after the offline `pcdt_engine`
and `az_engine` lookups and before `_lookup_record`. Revival is
`BrazilGuideline.from_dict(cached_data)` — the same call the search cache read
already uses (`1187`), so no new revival path is introduced.

**Cache-schema note.** No `CACHE_SCHEMA` bump. The row kind is a new key prefix,
`from_dict` already fills absent fields, and existing rows are unaffected. The
bump discipline at `124-128` exists because a *shape change to an existing row*
would otherwise be read as current; this adds a row kind, not a shape.

**Effect.** A search-then-open becomes: cache hit (milliseconds) → the full
ceiling available for the body → a served body, or a fast abstract fallback. For
any id that came from a search, the lookup branch of the `biblio-*` line is
removed as a possibility.

### D7 — Attribute the timeout

**Decision.** A local `timeout_phase` (`"lookup"` / `"fetch"` / `None`) is set at
each budget branch and carried into **every** exit path of `get_full_text` — the
early empty-id and cache-hit returns (`1819-1844`), the lookup-timeout result
(`1848-1857`), the local-corpus return (`1927-1929` via `_serve_local_text`), the
no-abstract error return (`1990-2002`), and the success payload (`2016-2019`) —
and `None` wherever no timeout occurred. It is also written into the
degraded cache row alongside `_CACHED_ERROR_KIND_KEY`, and it is a
caller-visible field — not stripped by `_serve_full_text` (`2064-2066`), which
removes cache bookkeeping only.

**`error_kind` does not change.** It feeds the `BvsErrorKind` taxonomy
(`utils/sqlite_cache.py`) and the breaker exemption for `origin_outage`
(`AGENTS.md` §10). The phase is additive information, not a new taxonomy value.

**Always present.** `timeout_phase: null` on a clean result, matching the
existing convention for `abstract_fallback` — an explicitly absent degradation is
more useful to a consumer than a missing key.

**Log.** Both existing messages also gain the phase and the elapsed split, so a
future occurrence is attributable from the log alone.

### D8 — Parse isolation behind a byte cap

**Decision.** `await asyncio.to_thread(pdf_bytes_to_text, resp.content)`, and a
`brazil_pdf_max_bytes` setting checked before parsing: over the cap returns
`backend_error` without invoking the parser.

**Why.** Two defects share one line of code. The loop block is a cross-request
defect — one slow parse stalls unrelated tool calls in the same process. The
unenforceable ceiling is worse: the branch at `1965-1970` claims to bound
something it cannot interrupt. Moving the parse to a thread makes cancellation
land at the await, which is the only way the ceiling means what it says.

**The cap is the mitigation for the cost that introduces.** An abandoned thread
keeps burning CPU until `pypdf` returns; the ceiling bounds the *caller*, not the
thread. The cap keeps a pathological document from reaching the parser at all.
It is deliberately not a streaming rewrite: bounding the parse is the defect,
and the download is already bounded by the deadline.

**Default value is unvalidated, and the requirement on it is precise.** The spec
requires that a cap exists, that it be configurable, and that over-cap classify as
`backend_error` without parsing. The default (`brazil_pdf_max_bytes`) must be set
from measurement of the largest real document in the corpus during
implementation — not guessed here — and it must admit every document the corpus
contains, so that D8 cannot silently regress a fetch that works today. The one
known data point is a 129k-character PCDT body.

**`AGENTS.md` §1 is amended, not excepted.** §1 currently permits
`asyncio.to_thread` in exactly one place (saving downloaded PDFs in
`WaterfallResolver.download_article`). The spec adds the second and states why:
CPU-bound extraction cannot be made cancellable, or kept off the loop's critical
path, any other way.

**`AGENTS.md` §10 and §3** gain the corresponding notes: §10 records that a
search-sourced record lookup is cache-served (the ceiling now bounds the body
fetch and a possible cold-open lookup, not a routine one), and §3 lists the
per-id record row among cached medical artifacts.

## Held — D5, the two-budget split

Splitting `brazil_fulltext_timeout_s` into a lookup ceiling and a fetch ceiling
would additionally protect **cold opens** — an id the caller never searched,
where a slow lookup can still starve the fetch. It is held, not rejected.

Revisit condition: a replay shows a cold open whose `timeout_phase` is `lookup`,
or a fetch that starts with under 5 s of a cold-open ceiling remaining. With D6
in place the common path is lookup-free, so the split's remaining benefit is
narrow and the cost is real: the two ceilings must sum below the caller's per-call
adapter timeout (60 s in zimqa's `[scholar] timeout_s`), and a sum that exceeds it
loses even the abstract fallback.

Same reasoning applies to raising `brazil_fulltext_timeout_s` above 30 s once the
lookup is off the clock. That is a separate configuration decision, bounded by the
same caller constraint, and not part of this spec.

## Published contract changes

| Change | Surface | Compatibility |
| --- | --- | --- |
| `timeout_phase` added to the payload | `get_brazil_moh_full_text` (`server.py:679`) | Additive. `degraded: true` already carries the boolean half (`server.py:149-150`) |
| Docstring documents `timeout_phase` and `abstract_fallback` | `server.py:685-701` | Documentation only |
| `brazil_pdf_max_bytes` added to `Settings` | `config.py` + env loader | Additive; existing behaviour preserved by the default |

## Testing

Failing-test-first, repo conventions (`respx` against the real `AsyncHttpClient`,
`httpx.MockTransport` where `__cause__` must survive).

1. **Search-then-open issues no BVS lookup.** After a mocked search, opening a
   returned id must produce zero requests to `BVS_SEARCH_URL` carrying an
   `id:"..."` query, and must serve a body byte-identical to the same open
   performed without the record row present.
2. **`timeout_phase` attribution, three cases.** Slow lookup → `"lookup"`,
   `status="error"`, nothing cached. Fast lookup with a slow PDF → `"fetch"`,
   `abstract_fallback=True`, `degraded=True`. Clean PDF → `None`.
3. **Byte cap boundary.** A body at the cap is parsed; one byte over is not, and
   the result is `backend_error` with no content. The boundary pair is the
   behaviour under test.
4. **The loop stays responsive.** Patch the parser with one that sleeps, launch
   it concurrently with a coroutine that must finish well inside that sleep, and
   assert the second completes first. This fails today and is the observable
   form of D8 — it does not assert the wiring.
5. **Repeat open.** Two consecutive opens of one id: the second issues no lookup
   and, on a successful body, is served from the full-text cache.
6. **Regression.** The existing "a timed-out lookup is never cached" behaviour
   stays green.

## Risks

- **D6 writes ~30 rows per search** — bounded, once per search, each no larger
  than the search row it accompanies. Staleness equals the search row's
  staleness: identical catalogue data, identical TTL.
- **Abandoned parse threads** keep burning CPU after the caller has moved on.
  The byte cap bounds the input, not the thread.
- **`timeout_phase` is a new field on a tool response** that zimqa parses. All
  changes are additive; zimqa's readers key on `status` and `error`.
- **Cold-open starvation remains** by decision (D5 held).
- **The byte-cap default is unmeasured** until the corpus is surveyed.

## Open items

1. D5 — held; revisit condition stated above.
2. `brazil_pdf_max_bytes` default — to be set from measurement.
3. `brazil_fulltext_timeout_s` above 30 s — separate decision, caller-bounded.