# PR #9 Review Remediation — Round 2

- **Date:** 2026-08-31
- **PR:** https://github.com/gustavokch/scholar-mcp/pull/9
- **Review comment:** https://github.com/gustavokch/scholar-mcp/pull/9#issuecomment-5473562994
- **Goal:** Fix the 6 findings from the round-2 review (2 🟡 code risks, 3 🔵 nits, 1 🟡 pre-existing risk made wider by this PR).
- **Architecture:** No interface changes. `FDAClient.search_drugs` filter hardening; `MedicalDatabasesEngine._search_cochrane` input hygiene; dependency cleanup; cache-write policy.
- **Tech stack:** Python 3.12, pytest + pytest-asyncio, respx.
- **Test runner:** `.venv/bin/python -m pytest` (bare `uv run` stalls on this box).

## Task 1 — Harden `_label_names_drug` (word boundaries, stopwords, all tokens)

Files: Modify `src/scholar_mcp/medical/fda.py`, Test `tests/medical/test_fda.py`

Consumes: `DrugLabel.openfda.brand_name/generic_name/substance_name`
Produces: `_label_names_drug(drug, query) -> bool` (same signature)

1. Red test: `test_search_drugs_unfielded_filter_ignores_stopword_lead_tokens` — query
   `"what is the dose of aspirin"`; unfielded route returns a THEOPHYLLINE label
   (name fields contain none of dose/aspirin). Today `any()` over first-3 tokens
   (`what, is, the`) substring-matches "the" in "THEOPHYLLINE" → junk survives.
   Expect `drugs == []`.
2. Run: `.venv/bin/python -m pytest tests/medical/test_fda.py -k unfielded -x` → new test fails.
3. Implement: tokens = all `[A-Za-z][A-Za-z0-9-]+` matches, lowercase; drop len<3
   and stopwords; word-boundary match (`\b{re.escape(token)}\b`) over joined name
   fields; `any()`. Empty filtered set → True (permissive, current behavior).
4. Run → pass; existing SILICEA + Advil tests stay green.
5. `git commit -m "fix(fda): word-boundary, stopword-aware drug-token filter"`

## Task 2 — Drop dead playwright dependency

Files: Modify `pyproject.toml`

No test (config change). Verify `rg -n playwright src/` is empty, then remove
`"playwright>=1.40.0"` from the `medical` extra. Full suite must stay green
(tests fake the playwright module; they assert the legacy path stays dead).

## Task 3 — Sanitize query before Europe PMC splice

Files: Modify `src/scholar_mcp/medical/databases.py`, Test `tests/medical/test_databases.py`

Consumes: user query string. Produces: `(... AND (PUB_TYPE:...))` Europe PMC query.

1. Red test: `test_search_cochrane_neutralizes_query_syntax_characters` — query
   `what is "ibuprofen" (for children)`; capture request; assert `"` and parens
   are gone from the `query` param while `systematic review` filter remains.
2. Run → fails (raw quotes present today).
3. Implement: `clean = re.sub(r'["()]', " ", query).strip()` before building
   `europe_pmc_query`; collapse whitespace.
4. Run → pass. Commit `fix(databases): neutralize query punctuation before europe pmc search`.

## Task 4 — pmid fallback only for MED-source records

Files: Modify `src/scholar_mcp/medical/databases.py`, Test `tests/medical/test_databases.py`

1. Red test (case inside `test_search_cochrane_calls_europe_pmc_when_http_to_cochrane_fails`
   or new test): record with `id="777"`, `source="CBA"`, no pmid/pmcid → expect
   `article.pmid == ""` and empty url (no bogus MED/777 link).
2. Run → fails (today pmid="777").
3. Implement: `pmid = rec.get("pmid") or (rec.get("id") if rec.get("source") == "MED" else "") or ""`.
4. Run → pass. Commit with Task 3 or separate `fix(databases)` commit.

## Task 5 — Test hygiene in test_databases.py

Files: Modify `tests/medical/test_databases.py`

Rename `test_search_cochrane_calls_europe_pmc_when_http_to_cochrane_fails` →
`test_search_cochrane_routes_through_europe_pmc`; drop unused `import sys, types`;
drop the never-requested cochrane 403 respx route. No red phase (test-only).

## Task 6 — Don't cache partial results when a variant errored

Files: Modify `src/scholar_mcp/medical/fda.py`, Test `tests/medical/test_fda.py`

1. Red test: `test_search_drugs_does_not_cache_partial_result_on_variant_error` —
   quoted-variant routes 500, unfielded variant returns 1 label. First call returns
   results with `error=True`; second call must re-issue the request
   (`route.call_count` increases). Today the partial set is cached → fails.
2. Implement: `if errored:` → return without cache write (empty case unchanged).
3. Run → pass. Commit `fix(fda): skip cache write when any query variant errored`.

## Verification

- `uv run pytest` equivalents via `.venv/bin/python -m pytest` — 100% green.
- Push `fix/fda-404-and-aap-guideline-fallback`, update PR.
