# PR #3 Review Remediation (Phase 2)

**Goal:** Resolve code review findings for PR #3 (arXiv URL-encoding, robust author parsing, arXiv error feed detection, OA status enrichment, and arXiv HTML URL detection).

**Architecture:** `scholar_mcp` MCP server provider layer (`src/scholar_mcp/providers/`, `identifiers.py`, `resolver.py`).

**Tech Stack:** Python 3.10, httpx, respx, pytest (asyncio), uv.

**Spec Reference:** PR #3 review findings on `feature/citation-graph-and-extraction`.

---

## Task 1: URL-encode `paper_id` in Semantic Scholar recommendations

**Target files:**
- Modify: `src/scholar_mcp/providers/semantic_scholar.py`
- Test: `tests/test_openalex_s2.py`

**Consumes:** `paper_id` (e.g. `DOI:10.1038/nature123`, `ARXIV:hep-th/9901001`).
**Produces:** Properly URL-encoded endpoint path `/papers/forpaper/{quoted_id}`.

### Step 1: Write failing test
In `tests/test_openalex_s2.py`, update `test_s2_recommendations` to assert the request URL encodes the `/` and `:` characters in `paper_id`.

### Step 2: Run test to confirm failure
```bash
uv run pytest tests/test_openalex_s2.py -k test_s2_recommendations -v
```

### Step 3: Minimal implementation
In `src/scholar_mcp/providers/semantic_scholar.py`:
Use `urllib.parse.quote(paper_id, safe="")` when building the recommendations URL in `fetch_recommendations`.

### Step 4: Run test to confirm pass
```bash
uv run pytest tests/test_openalex_s2.py -k test_s2_recommendations -v
```

### Step 5: Git commit command
```bash
git commit -m "fix(s2): url-encode paper_id in recommendations endpoint"
```

---

## Task 2: Guard author parsing and DOI stripping in S2 and OpenAlex providers

**Target files:**
- Modify: `src/scholar_mcp/providers/semantic_scholar.py`, `src/scholar_mcp/providers/openalex.py`
- Test: `tests/test_openalex_s2.py`

**Consumes:** Malformed or non-dict author/institution objects and varied DOI URL prefixes.
**Produces:** Resilient `PaperMetadata` and `CitationItem` objects without `AttributeError`.

### Step 1: Write failing test
In `tests/test_openalex_s2.py`, add tests for:
- S2 search & recommendations with raw string authors or `None` values in authors list.
- OpenAlex metadata & citations with non-dict authorships/institutions and `http://dx.doi.org/` / `doi:` prefixes.

### Step 2: Run test to confirm failure
```bash
uv run pytest tests/test_openalex_s2.py -k "author_edge_cases or strip_doi" -v
```

### Step 3: Minimal implementation
- In `semantic_scholar.py`: support string authors and dict authors with `.get("name")` guard.
- In `openalex.py`: guard `authorships`, `author`, `institutions` loops with `isinstance`, and update `_strip_doi_url` with regex for `https?://(?:dx\.)?doi\.org/` and `doi:`.

### Step 4: Run test to confirm pass
```bash
uv run pytest tests/test_openalex_s2.py -v
```

### Step 5: Git commit command
```bash
git commit -m "fix(providers): guard author parsing and doi stripping in s2 and openalex"
```

---

## Task 3: Reject arXiv Atom API error entries

**Target files:**
- Modify: `src/scholar_mcp/providers/arxiv.py`
- Test: `tests/test_arxiv_provider.py`

**Consumes:** arXiv Atom API responses with error entry.
**Produces:** `None` from `fetch_metadata` rather than `PaperMetadata(title="Error")`.

### Step 1: Write failing test
In `tests/test_arxiv_provider.py`, test `fetch_metadata` with arXiv error Atom feed XML.

### Step 2: Run test to confirm failure
```bash
uv run pytest tests/test_arxiv_provider.py -k test_arxiv_fetch_metadata_error_feed -v
```

### Step 3: Minimal implementation
In `src/scholar_mcp/providers/arxiv.py`:
Inspect entry ID and title; return `None` if `api/errors` is present or title is `"Error"`.

### Step 4: Run test to confirm pass
```bash
uv run pytest tests/test_arxiv_provider.py -v
```

### Step 5: Git commit command
```bash
git commit -m "fix(arxiv): reject error entries in atom metadata response"
```

---

## Task 4: Propagate OA status from OpenAlex and recognize arXiv `/html/` URLs

**Target files:**
- Modify: `src/scholar_mcp/resolver.py`, `src/scholar_mcp/identifiers.py`
- Test: `tests/test_identifiers.py`, `tests/test_openalex_s2.py`

**Consumes:** OpenAlex OA status for metadata with unknown status; `arxiv.org/html/...` URLs.
**Produces:** Enriched `oa_status` on `PaperMetadata`; `"arxiv"` identifier type for arXiv HTML URLs.

### Step 1: Write failing test
- Test `clean_identifier` with `https://arxiv.org/html/2305.18290v1`.
- Test `fetch_abstract` updating `oa_status` when OpenAlex reports `oa_status="oa"`.

### Step 2: Run test to confirm failure
```bash
uv run pytest tests/test_identifiers.py tests/test_openalex_s2.py -v
```

### Step 3: Minimal implementation
- In `identifiers.py`: update `ARXIV_URL_RE` to match `(?:abs|pdf|html)`.
- In `resolver.py`: update `meta.oa_status = enriched.oa_status` if `meta.oa_status in ("", "unknown") and enriched.oa_status != "unknown"`.

### Step 4: Run test to confirm pass
```bash
uv run pytest -v
```

### Step 5: Git commit command
```bash
git commit -m "fix(resolver): propagate oa_status from openalex and recognize arxiv html urls"
```

---

## Verification

Run full test suite:
```bash
uv run pytest
```
All tests must pass.
