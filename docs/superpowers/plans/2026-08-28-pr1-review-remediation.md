# PR #1 Review Remediation Plan

- **Goal:** Remediate all 7 code review findings from PR #1 via bite-sized TDD tasks.
- **Architecture:** Python 3.10+, FastMCP, httpx, BeautifulSoup4 / lxml, pypdf.
- **Target Branch:** `feature/scholar-mcp-unified`
- **Review Findings:**
  1. `select_sections` in `jats.py`: Retain child subsections under selected parent headers.
  2. `list_sections` in `jats.py`: Remove unused regex statement.
  3. `jats_to_markdown` in `jats.py`: Wrap DOM traversal in `list(soup.find_all(True))` during element decomposition.
  4. `fetch_abstract` in `pubmed.py`: Preserve structured abstract XML `Label` attributes.
  5. `AsyncHttpClient` in `http.py`: Thread/coroutine-safe limiter initialization with mutex.
  6. `AsyncHttpClient` in `http.py`: Bump User-Agent version to `ScholarMCP/1.0.0`.
  7. `WaterfallResolver` in `resolver.py`: Clean up inline `UNPAYWALL_BASE` import to module level.

---

## Task 1: Fix JATS Section Selection Hierarchy & Cleanup DOM Traversal

### Files
- **Modify:** `src/scholar_mcp/parsers/jats.py`
- **Test:** `tests/test_jats_parser.py`

### Consumes / Produces
- **Consumes:** `jats_to_markdown(xml)`, `select_sections(md, wanted)`
- **Produces:** Full section hierarchy preservation when selecting parent headings, safe decomposition.

### Steps
1. **Red Test:** Add `test_select_sections_preserves_nested_subsections` in `tests/test_jats_parser.py` selecting `"Introduction"`, expecting `### Sub Background` and its body to be included while `## Methods` is excluded.
2. **Verify Failure:** Run `uv run pytest tests/test_jats_parser.py -k test_select_sections_preserves_nested_subsections`.
3. **Green Implementation:**
   - In `src/scholar_mcp/parsers/jats.py`, implement depth-aware section parsing in `select_sections`:
     Parse headings into structured blocks with level `len(hashes)`. When a block matches `wanted`, include the heading, its body, and all subsequent blocks with level greater than the matched block's level until a block with level <= matched level is reached.
   - Remove unused `matches = re.findall(...)` in `list_sections`.
   - Wrap `soup.find_all(True)` in `list(...)` in `jats_to_markdown`.
4. **Verify Pass:** Run `uv run pytest tests/test_jats_parser.py`.
5. **Commit:** `git commit -m "fix(jats): retain nested subsections in select_sections and clean DOM decomposition"`

---

## Task 2: Preserve Structured Abstract Labels in PubMed XML Parser

### Files
- **Modify:** `src/scholar_mcp/providers/pubmed.py`
- **Test:** `tests/test_search_scihub_providers.py`

### Consumes / Produces
- **Consumes:** PubMed efetch XML with `<AbstractText Label="METHODS">...</AbstractText>`
- **Produces:** Structured abstract string with labeled prefixes (e.g. `METHODS: ...`).

### Steps
1. **Red Test:** Add `test_pubmed_fetch_abstract_structured_labels` in `tests/test_search_scihub_providers.py` with multi-label XML (`BACKGROUND`, `METHODS`, `RESULTS`).
2. **Verify Failure:** Run `uv run pytest tests/test_search_scihub_providers.py -k test_pubmed_fetch_abstract_structured_labels`.
3. **Green Implementation:**
   - In `src/scholar_mcp/providers/pubmed.py`: In `fetch_abstract`, iterate over `abstract_elem.find_all("AbstractText")`, check `p.get("Label")`. If label exists, prefix with `f"{label.strip()}: {text}"`, else use `text`.
4. **Verify Pass:** Run `uv run pytest tests/test_search_scihub_providers.py`.
5. **Commit:** `git commit -m "feat(pubmed): support structured abstract labels in efetch parser"`

---

## Task 3: Harden HTTP Limiter Concurrency, Bump User-Agent, and Clean Resolver Imports

### Files
- **Modify:** `src/scholar_mcp/utils/http.py`, `src/scholar_mcp/resolver.py`
- **Test:** `tests/test_http_cache.py`

### Consumes / Produces
- **Consumes:** `AsyncHttpClient`, `Settings`
- **Produces:** Thread-safe `_limiters` initialization, User-Agent `ScholarMCP/1.0.0`, module-level `UNPAYWALL_BASE`.

### Steps
1. **Red Test:** Add `test_http_client_user_agent_version` and `test_limiters_concurrent_access` in `tests/test_http_cache.py`.
2. **Verify Failure:** Run `uv run pytest tests/test_http_cache.py -k "test_http_client_user_agent_version or test_limiters_concurrent_access"`.
3. **Green Implementation:**
   - In `src/scholar_mcp/utils/http.py`: Use `threading.Lock()` to synchronize `_limiters` dictionary in `_limiter_for`. Update User-Agent to `"ScholarMCP/1.0.0 (mailto:...)"`.
   - In `src/scholar_mcp/resolver.py`: Move `from scholar_mcp.providers.unpaywall import UNPAYWALL_BASE` to top-level imports.
4. **Verify Pass:** Run `uv run pytest tests/test_http_cache.py`.
5. **Commit:** `git commit -m "fix(http): harden rate limiter concurrency, bump UA to v1.0.0, clean imports"`

---

## Task 4: Full Suite Verification & Remote Branch Push

### Steps
1. Run full test suite: `uv run pytest -v` (confirm 100% green).
2. Push branch: `git push origin feature/scholar-mcp-unified`.
3. Summarize remediation in PR and report to user.
