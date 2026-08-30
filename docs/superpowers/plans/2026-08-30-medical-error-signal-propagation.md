# Remediation Plan — PR #8 Review: Medical Fetch-Error Signal Propagation

Date: 2026-08-30
Branch: `fix/clinical-trials-nct-and-error-signal`
PR: https://github.com/gustavokch/scholar-mcp/pull/8
Review: https://github.com/gustavokch/scholar-mcp/pull/8#issuecomment-5471201382

## Goal

PR #8 added `CacheMetadata.error` and `MedicalArticle.nct_id`, but neither
reaches a consumer: no caller reads `meta.error`, no formatter renders
`nct_id`, and the aggregators discard sub-search metadata. This plan wires both
signals end to end and applies the same error contract to every medical client,
so an upstream fetch failure is never reported to the model as evidence of
absence.

## Contract

`CacheMetadata.error is True` means: **at least one upstream fetch for this
result failed, so the result set may be incomplete.** Three rules follow.

1. Any client that swallows an exception on a fetch path must set `error=True`
   on the metadata it returns.
2. A failed fetch is never written to the cache. Caching an empty result caused
   by a network failure would serve that failure for the whole TTL.
3. An aggregator that fans out to several sources must OR the `error` flags of
   its sub-results (including sub-results that raised under
   `asyncio.gather(..., return_exceptions=True)`) into the metadata it returns.

Every swallowed exception is also logged with `exc_info=True` through a
module-level `logging.getLogger(__name__)`. The standard library's last-resort
handler writes to stderr, which is safe for an MCP stdio server (stdout carries
the protocol).

## Architecture

```
clients (pubmed, fda, who, rxnorm, clinical_trials, pediatrics scrapers)
    │  set error=True on swallow, skip cache write
    ▼
aggregators (databases.search_medical_databases / _journals,
             pediatrics.search_aap_guidelines / _literature,
             guidelines.search_clinical_guidelines)
    │  OR sub-result error flags
    ▼
formatters (append_cache_info + empty-state lines)
    │  render "[Fetch error: results may be incomplete]"
    │  suppress the false "No X found for: {query}" claim
    ▼
MCP tool markdown
```

## Tech stack

Python 3.11+, `dataclasses`, `httpx`, `respx` for HTTP mocking, `pytest` +
`pytest-asyncio` (auto mode), `uv run pytest`.

---

## Task 1 — Formatters surface the error signal

**Modify:** `src/scholar_mcp/medical/formatters.py`
**Test:** `tests/medical/test_models_formatters.py`

**Consumes:** `CacheMetadata.error`
**Produces:** markdown that never claims absence after a failed fetch.

### Step 1 — failing tests

```python
def test_append_cache_info_marks_fetch_error():
    text = append_cache_info("body", CacheMetadata(cached=False, cache_age=0, error=True))
    assert "Fetch error" in text
    assert "[Fresh response]" not in text


def test_format_medical_articles_does_not_claim_absence_on_error():
    out = format_medical_articles([], "asthma", CacheMetadata(cached=False, cache_age=0, error=True))
    assert "No medical literature found" not in out["markdown"]
    assert "Fetch error" in out["markdown"]


def test_format_medical_articles_still_reports_absence_without_error():
    out = format_medical_articles([], "asthma", CacheMetadata(cached=False, cache_age=0))
    assert "No medical literature found for: asthma" in out["markdown"]
```

Equivalent absence-claim tests for `format_drug_search_results`,
`format_drug_details`, `format_rxnorm_drugs`, `format_health_indicators`,
`format_guidelines`, `format_pediatric_guidelines`.

### Step 2 — confirm failure

`uv run pytest tests/medical/test_models_formatters.py -v`

### Step 3 — implementation

Add to `formatters.py`:

```python
FETCH_ERROR_NOTE = "[Fetch error: an upstream source failed; results may be incomplete]"
_FETCH_FAILED_LINE = (
    "The search could not be completed because an upstream source failed. "
    "This is not evidence that no results exist."
)


def append_cache_info(text: str, meta: CacheMetadata) -> str:
    if meta.error:
        return f"{text}\n\n{FETCH_ERROR_NOTE}"
    if meta.cached:
        return f"{text}\n\n[Cached: {meta.cache_age}s old]"
    return f"{text}\n\n[Fresh response]"


def _empty_state(message: str, meta: CacheMetadata) -> str:
    return _FETCH_FAILED_LINE if meta.error else message
```

Wrap each `lines.append("No … found …")` in `_empty_state(...)`, and the
`format_drug_details` `drug is None` branch likewise.

### Step 4 — confirm pass

`uv run pytest tests/medical/test_models_formatters.py -v`

### Step 5 — commit

```bash
git add src/scholar_mcp/medical/formatters.py tests/medical/test_models_formatters.py
git commit -m "fix(medical): stop reporting fetch failures as zero results"
```

---

## Task 2 — Render `nct_id`

**Modify:** `src/scholar_mcp/medical/formatters.py`
**Test:** `tests/medical/test_models_formatters.py`

### Step 1 — failing test

```python
def test_format_medical_articles_renders_nct_id():
    art = MedicalArticle(title="T", nct_id="NCT01234567", source_database="ClinicalTrials.gov")
    out = format_medical_articles([art], "asthma", CacheMetadata(cached=False, cache_age=0))
    assert "- **NCT ID:** NCT01234567" in out["markdown"]
```

### Step 2 — confirm failure. ### Step 3 — add the line beside PMID/DOI.
### Step 4 — confirm pass.

### Step 5 — commit

```bash
git commit -m "feat(medical): render NCT ID in medical article output"
```

---

## Task 3 — PubMed client

**Modify:** `src/scholar_mcp/medical/pubmed.py` (lines 147-169)
**Test:** `tests/medical/test_pubmed.py`

Both `except Exception` blocks return `error=True` and log. Neither writes to
the cache today, so rule 2 already holds.

### Step 1 — failing test

```python
@respx.mock
async def test_search_articles_marks_error_on_esearch_failure(tmp_path):
    ...
    respx.get(ESEARCH_URL).mock(side_effect=httpx.ConnectError("boom"))
    articles, meta = await client.search_articles("asthma")
    assert articles == []
    assert meta.error is True
```

Plus the same for an `EFETCH_URL` failure after a successful esearch, and a
happy-path assertion that `meta.error is False`.

### Steps 2-4 — red, implement, green.
### Step 5 — commit `fix(medical): signal fetch errors from pubmed client`

---

## Task 4 — RxNorm client

**Modify:** `src/scholar_mcp/medical/rxnorm.py` (line 74)
**Test:** `tests/medical/test_rxnorm.py`

Same shape as Task 3, single swallow path.

Commit: `fix(medical): signal fetch errors from rxnorm client`

---

## Task 5 — FDA client

**Modify:** `src/scholar_mcp/medical/fda.py` (lines 123-151, 164-194)
**Test:** `tests/medical/test_fda.py`

Two defects beyond the missing flag:

- `search_drugs` runs four query variants and `continue`s past each failure. If
  every variant fails it caches an empty list, poisoning the cache for the
  TTL. Track `errored`; when `errored and not all_results`, skip `cache.set`
  and return `error=True`.
- `get_drug_by_ndc` caches `None` when both attempts raise, with the same
  effect. Skip the cache write and return `error=True` in that case.

Partial success (some variants failed, results found) still returns
`error=True` under the contract, but the results are cached and returned.

### Step 1 — failing tests

```python
async def test_search_drugs_marks_error_and_skips_cache_when_all_queries_fail(tmp_path): ...
async def test_get_drug_by_ndc_marks_error_and_skips_cache_on_failure(tmp_path): ...
```

Assert `meta.error is True` and that a second call re-issues the HTTP request
(`respx` route `call_count` increases), proving nothing was cached.

Commit: `fix(medical): signal fda fetch errors and stop caching failures`

---

## Task 6 — WHO client

**Modify:** `src/scholar_mcp/medical/who.py` (lines 141-221, 236-264)
**Test:** `tests/medical/test_who.py`

`get_health_statistics`: track failures across the primary indicator query, the
variation fallback, and the per-code record fetches. When the indicator lookup
fails outright, do not cache the empty list at line 175; return `error=True`.

`get_child_health_statistics`: same treatment across the per-code loop.

Commit: `fix(medical): signal who fetch errors and stop caching failures`

---

## Task 7 — Pediatrics engine

**Modify:** `src/scholar_mcp/medical/pediatrics.py`
**Test:** `tests/medical/test_pediatrics.py`

- `_scrape_html` returns `tuple[list[PediatricGuideline], bool]`; the second
  element is `True` when the HTTP fetch or the Playwright fallback raised.
- `search_bright_futures` / `search_aap_policy` propagate that into
  `CacheMetadata(..., error=errored)` and skip the cache write when errored.
- `search_aap_guidelines` ORs the two sub-metadata flags, counting a
  `BaseException` from `gather` as an error, and skips the cache write when
  errored.
- `search_pediatric_literature` stops discarding the PubMed metadata
  (`articles, _ =`) and propagates its `error`.

Commit: `fix(medical): propagate fetch errors through pediatrics engine`

---

## Task 8 — Guidelines engine

**Modify:** `src/scholar_mcp/medical/guidelines.py`
**Test:** `tests/medical/test_guidelines.py`

`search_clinical_guidelines` discards the metadata of both PubMed layers.
Capture them, OR the flags, propagate, and skip the cache write when errored.

Commit: `fix(medical): propagate pubmed fetch errors through guidelines engine`

---

## Task 9 — Databases engine

**Modify:** `src/scholar_mcp/medical/databases.py`
**Test:** `tests/medical/test_databases.py`

- `_search_cochrane` returns `error=True` on its swallow path (line 95).
- `search_medical_databases` ORs the metadata of the PubMed, ClinicalTrials and
  Cochrane sub-searches, treating a `BaseException` from `gather` as an error,
  and skips the cache write when errored. This is the finding that made
  `error=True` unreachable.
- `search_medical_journals` propagates the PubMed metadata it currently drops.

### Step 1 — failing test

```python
@respx.mock
async def test_search_medical_databases_marks_error_when_a_source_fails(tmp_path):
    # pubmed succeeds, clinicaltrials connect-errors
    articles, meta = await engine.search_medical_databases("asthma")
    assert articles  # partial results still returned
    assert meta.error is True
```

Commit: `fix(medical): aggregate sub-search fetch errors in databases engine`

---

## Task 10 — Clinical trials logging and cache-poisoning guard

**Modify:** `src/scholar_mcp/medical/clinical_trials.py`
**Test:** `tests/medical/test_clinical_trials.py`

Log the swallowed exception, and add the regression test the review asked for:
a failure must not be cached, so a second call re-issues the request.

```python
@respx.mock
async def test_search_clinical_trials_does_not_cache_failures(tmp_path):
    route = respx.get(CT_URL).mock(side_effect=httpx.ConnectError("boom"))
    await client.search_clinical_trials("asthma")
    await client.search_clinical_trials("asthma")
    assert route.call_count == 2
```

Commit: `fix(medical): log trials fetch failures and cover cache-poisoning guard`

---

## Verification gate

```bash
uv run pytest
```

Must be fully green before pushing to `fix/clinical-trials-nct-and-error-signal`.
