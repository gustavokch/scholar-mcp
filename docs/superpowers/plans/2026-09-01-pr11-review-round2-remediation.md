# PR #11 — Round-2 Review Remediation

**PR:** https://github.com/gustavokch/scholar-mcp/pull/11
**Branch:** `fix/medical-query-construction`
**Review:** https://github.com/gustavokch/scholar-mcp/pull/11#issuecomment-5498580509
**Baseline:** 327 passed, CI green on 3.10/3.11/3.12.

## Goal

Close the two substantive defects found in round 2 (name-guard hole, unescaped
Lucene phrase) and clear the three nits, without changing the ladder semantics
the PR established.

## Tech stack

Python 3.10+, `uv`, `pytest` + `pytest-asyncio` + `respx`.

---

## Task 1 — Name guard must ignore product-descriptor words

**Modify:** `src/scholar_mcp/medical/fda.py`
**Test:** `tests/medical/test_fda.py`

`_label_names_drug` builds its token set from `_query_tokens`, which removes
`_QUERY_STOPWORDS` only. `COMMON_DRUG_WORDS` (`oral`, `tablet`, `capsule`,
`injection`, `label`, `fda`, `dose`, ...) survive and whole-word-match real
openFDA name fields, so an unrelated label passes the guard.

Consumes: `DrugLabel`, query string.
Produces: `bool` — true only when a *name-bearing* query token appears in a
name field.

**Step 1 — failing test**

```python
def test_label_names_drug_ignores_product_descriptor_words():
    """'oral', 'tablet', 'capsule' describe the product, not its name, and
    appear verbatim in real openFDA brand names. They must not satisfy the
    name guard, or the unfielded fallback re-admits unrelated labels."""
    junk = DrugLabel(
        openfda=OpenFDAData(
            brand_name=["Childrens Allergy Oral Solution"],
            generic_name=["DIPHENHYDRAMINE"],
            substance_name=["DIPHENHYDRAMINE HYDROCHLORIDE"],
        )
    )
    assert _label_names_drug(junk, "ibuprofen oral dosing pregnancy") is False

    real = DrugLabel(
        openfda=OpenFDAData(
            brand_name=["MOTRIN IB"],
            generic_name=["IBUPROFEN"],
            substance_name=["IBUPROFEN"],
        )
    )
    assert _label_names_drug(real, "ibuprofen oral dosing pregnancy") is True


def test_label_names_drug_permissive_when_only_descriptor_words():
    """If nothing name-bearing remains the guard stays permissive rather
    than rejecting every label."""
    drug = DrugLabel(openfda=OpenFDAData(brand_name=["ANYTHING"]))
    assert _label_names_drug(drug, "oral tablet dosage") is True
```

**Step 2** `uv run pytest tests/medical/test_fda.py -k label_names_drug -v` (RED)

**Step 3 — implementation**

Add `_name_tokens(query)` returning `_query_tokens(query)` minus
`COMMON_DRUG_WORDS`; `_label_names_drug` uses it, falling back to
`_query_tokens(query)` when the filtered set is empty is *not* wanted —
fall back to permissive `True` as the docstring already promises.
`_name_candidates` reuses `_name_tokens` so the two stay in lockstep.

**Step 4** re-run (GREEN), then the full fda module.

**Step 5** `git commit -m "fix(fda): exclude product-descriptor words from the drug-name guard"`

---

## Task 2 — Escape Lucene phrase delimiters

**Modify:** `src/scholar_mcp/medical/fda.py`
**Test:** `tests/medical/test_fda.py`

`_name_clause` interpolates into `field:"<value>"` unescaped. A query holding
`"` produces a malformed clause; api.fda.gov 400s; `errored` is set for the
whole call and the cache write is skipped even when later variants matched.

**Step 1 — failing test**

```python
def test_name_clause_escapes_quotes():
    clause = _name_clause('ibuprofen "extra strength"')
    assert clause.count('"') == 6  # exactly the three field delimiters
    assert '\\"' not in clause     # stripped, not backslash-escaped


@respx.mock
async def test_search_drugs_quoted_query_does_not_error(tmp_path: Path):
    """A query containing a double quote must not poison the whole call."""
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload("MOTRIN IB", generic="IBUPROFEN", ndc="0573-0164")
    )
    try:
        drugs, meta = await client.search_drugs('ibuprofen "extra strength"', limit=5)
        assert meta.error is False
        assert len(drugs) == 1
        for call in respx.calls:
            search = call.request.url.params.get("search", "")
            assert search.count('"') % 2 == 0, f"unbalanced quotes: {search}"
    finally:
        await cache.close()
        await http_client.aclose()
```

**Step 2** run (RED)

**Step 3 — implementation**

`_sanitize_phrase(value)` removes `"` and `\` and collapses whitespace;
`_name_clause` applies it. The raw-query unfielded variant (variant 4) uses
the sanitized query too.

**Step 4** re-run (GREEN)

**Step 5** `git commit -m "fix(fda): sanitize quotes before building fielded name clauses"`

---

## Task 3 — Pin the openFDA request budget

**Test:** `tests/medical/test_fda.py`

Symmetric with `test_search_clinical_guidelines_l1_ladder_bounded`.

**Step 1 — test**

```python
@respx.mock
async def test_search_drugs_request_budget_bounded(tmp_path: Path):
    """Worst case (every variant returns nothing) must stay within
    1 + 2*MAX_NAME_CANDIDATES + 1 openFDA requests."""
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(404, json={"error": {"code": "NOT_FOUND"}})
    try:
        drugs, _ = await client.search_drugs(
            "ibuprofen naproxen aspirin pregnancy trimester", limit=10
        )
        assert drugs == []
        assert len(respx.calls) <= 1 + 2 * MAX_NAME_CANDIDATES + 1
    finally:
        await cache.close()
        await http_client.aclose()
```

**Steps 2-4** run; expect GREEN immediately (pin, not a fix).

**Step 5** `git commit -m "test(fda): pin the openFDA variant-ladder request budget"`

---

## Task 4 — Remove the leftover debug print

**Modify:** `tests/medical/test_fda.py:494`

Delete `print(f"CAPTURED: {search}")`.

**Step 5** `git commit -m "test(fda): drop leftover debug print from context-filter router"`

---

## Verification gate

`uv run pytest` — must be green, count >= 327 + new tests.
Then `git push origin fix/medical-query-construction`.
