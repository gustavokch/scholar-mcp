# PR #12 Review Remediation — PubMed identifier parsing

**PR:** https://github.com/gustavokch/scholar-mcp/pull/12
**Branch:** `fix/pubmed-reference-doi`
**Base commit:** `b16f375`
**Review comment:** https://github.com/gustavokch/scholar-mcp/pull/12#issuecomment-5498772396
**Baseline suite:** 316 passed

## Goal

PR #12 stops `PubMedProvider.fetch_abstract` from returning a cited reference's DOI. The fix is
correct but leaves three gaps in the same identifier-parsing block, all confirmed by direct
reproduction against `bs4` with the `lxml-xml` parser:

1. `ELocationID` is never read, so a record whose own `ArticleIdList` has no `IdType="doi"` entry
   loses its DOI even though the value is present in the document.
2. `doi = aid.get_text(...) or doi` is followed by an unconditional `break`, so an empty
   `<ArticleId IdType="doi"/>` in the record's own list aborts the scan before a later valid entry.
3. The record's own `<ArticleId IdType="pmc">` is discarded; `pmcid=ids.pmcid` echoes the caller.

A fourth, structural point: the `aid.parents` walk is a denylist naming the one container known to
nest identifiers today. Positive scoping to `PubmedData`'s direct `ArticleIdList` child is immune to
any future nested id container and makes 1–3 straightforward to express.

## Architecture

Replace the document-wide `find_all("ArticleId")` scan with a helper that reads only the record's own
identifier list, then layer explicit precedence on top:

```
own ArticleIdList (direct child of PubmedData)
  -> first non-empty <ArticleId IdType="doi">      # record's own DOI
  -> else Article/ELocationID[@EIdType="doi"]      # record's own DOI, alternate location
  -> else ids.doi                                  # caller-supplied fallback
```

`pmcid` uses the same helper with `IdType="pmc"`, falling back to `ids.pmcid`. EFetch emits the
canonical `PMC#######` form, and `providers/pmc.py:22` already normalizes either form, so no
consumer changes are needed.

`find("ArticleIdList", recursive=False)` on `PubmedData` is the allowlist: `ReferenceList` and every
`Reference/ArticleIdList` under it are descendants, not direct children, so they cannot be selected
regardless of document order.

## Tech stack

Python 3.10+, `beautifulsoup4` with `lxml-xml`, `pytest` + `respx`, `uv`.

## Spec reference

PubMed EFetch DTD element order under `PubmedData`:
`(History?, PublicationStatus, ArticleIdList, ObjectList?, ReferenceList*)`.
`ELocationID` lives under `MedlineCitation/Article` and carries `EIdType="doi"`.

---

## Task 1 — Own-identifier scoping, ELocationID fallback, empty-value handling

**Modify:** `src/scholar_mcp/providers/pubmed.py`
**Test:** `tests/test_search_scihub_providers.py`

**Consumes:** EFetch XML for one `PubmedArticle`.
**Produces:** `PaperMetadata.doi` sourced only from the record's own identifiers, with
`ids.doi` as last resort.

### Step 1 — Write failing tests

Two new tests plus hardened assertions on the existing one:

- `test_pubmed_fetch_abstract_uses_elocationid_when_no_own_doi_id`: own `ArticleIdList` holds
  `pubmed` + `pmc` only, `Article/ELocationID[@EIdType="doi"]` holds `10.3390/ph17121592`, a
  `ReferenceList` decoy holds `10.1007/s00404-015-3648-7`. Caller passes `pmid` only.
  Expect `meta.doi == "10.3390/ph17121592"`.
- `test_pubmed_fetch_abstract_skips_empty_own_doi_id`: own `ArticleIdList` holds an empty
  `<ArticleId IdType="doi"></ArticleId>` followed by a valid one.
  Expect `meta.doi == "10.3390/ph17121592"`.
- Extend `test_pubmed_fetch_abstract_ignores_reference_list_dois` with negative assertions naming
  both decoy DOIs explicitly.

### Step 2 — Confirm failure

```
uv run pytest tests/test_search_scihub_providers.py -k "elocationid or empty_own_doi" -v
```

### Step 3 — Minimal implementation

Add a local `_own_article_id(id_type)` closure scoped to `PubmedData`'s direct `ArticleIdList`
child; resolve `doi` through the three-step precedence above.

### Step 4 — Confirm pass

```
uv run pytest tests/test_search_scihub_providers.py -v
```

### Step 5 — Commit

```
git add src/scholar_mcp/providers/pubmed.py tests/test_search_scihub_providers.py
git commit -m "fix(pubmed): scope identifier parsing to the record's own ArticleIdList"
```

---

## Task 2 — Parse the record's own PMC identifier

**Modify:** `src/scholar_mcp/providers/pubmed.py`
**Test:** `tests/test_search_scihub_providers.py`

**Consumes:** the `_own_article_id` helper from Task 1.
**Produces:** `PaperMetadata.pmcid` from the record when present, `ids.pmcid` otherwise.

### Step 1 — Write failing test

`test_pubmed_fetch_abstract_parses_own_pmcid`: caller passes `pmid` only; the record's own
`ArticleIdList` holds `PMC11676342`. Expect `meta.pmcid == "PMC11676342"`. Assert the reference
decoys cannot supply it by including a `pmc` id inside a `Reference`.

### Step 2 — Confirm failure

```
uv run pytest tests/test_search_scihub_providers.py -k own_pmcid -v
```

### Step 3 — Minimal implementation

`pmcid = _own_article_id("pmc") or ids.pmcid` in the `PaperMetadata` construction.

### Step 4 — Confirm pass

```
uv run pytest tests/test_search_scihub_providers.py -v
```

### Step 5 — Commit

```
git add src/scholar_mcp/providers/pubmed.py tests/test_search_scihub_providers.py
git commit -m "feat(pubmed): return the record's own PMC identifier from fetch_abstract"
```

---

## Verification

```
uv run pytest
```

Result: **319 passed**, exit 0 (316 baseline + 3 new tests, plus two negative
assertions added to `test_pubmed_fetch_abstract_ignores_reference_list_dois`).

Commits: `ae33495` (Task 1), `7357f68` (Task 2).

Then `git push origin fix/pubmed-reference-doi`.

## Out of scope

- The `search()` / ESummary path — already scoped correctly via `articleids`.
- Any change to `resolver.py` precedence; `meta.pmcid or ids.pmcid` at `resolver.py:264` already
  prefers record-derived values.
