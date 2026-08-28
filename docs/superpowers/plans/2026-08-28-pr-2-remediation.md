# PR #2 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remediate all 6 findings from the PR #2 code review (JATS MathML parsing bug, CrossRef author parsing safety, PubMed E-Link null ID filtering, Europe PMC search helper DRY refactoring, and comprehensive test coverage).

**Architecture:**
- `src/scholar_mcp/parsers/jats.py`: Remove `"mml:" in tag_name` discard check in `_render_node` so MathML nodes and fallback handlers work properly.
- `src/scholar_mcp/providers/crossref.py`: Add type guard for `author` in reference parsing.
- `src/scholar_mcp/providers/pubmed.py`: Filter out candidate links with null IDs before processing.
- `src/scholar_mcp/providers/europe_pmc.py`: Extract `_resolve_source_and_ext_id` helper method.
- `tests/test_jats_parser.py`: Add MathML formula parsing test.
- `tests/test_citations_references.py`: Add tests for CrossRef author edge-cases and Europe PMC DOI search fallback.

---

### Task 1: Fix JATS MathML Parsing Bug and Add MathML Test

- **Modify:** `src/scholar_mcp/parsers/jats.py`
- **Test:** `tests/test_jats_parser.py`

- [x] **Step 1:** Write failing test in `tests/test_jats_parser.py` asserting MathML formula with `alttext` and child MathML text are rendered correctly.
- [x] **Step 2:** Run `uv run pytest tests/test_jats_parser.py -k test_jats_mathml_rendering` to confirm failure.
- [x] **Step 3:** Edit `src/scholar_mcp/parsers/jats.py` to remove `"mml:" in tag_name` from line 106.
- [x] **Step 4:** Run `uv run pytest tests/test_jats_parser.py` to confirm all tests pass.
- [x] **Step 5:** Git commit fix: `fix(parsers): remove invalid mml discard check in JATS renderer`

---

### Task 2: Robust CrossRef Reference Author Parsing

- **Modify:** `src/scholar_mcp/providers/crossref.py`
- **Test:** `tests/test_citations_references.py`

- [x] **Step 1:** Add test in `tests/test_citations_references.py` with malformed/dict/non-string author in CrossRef references.
- [x] **Step 2:** Run `uv run pytest tests/test_citations_references.py -k test_crossref_fetch_references_author_edge_cases` to confirm failure.
- [x] **Step 3:** Edit `src/scholar_mcp/providers/crossref.py` to guard `isinstance(author, str)` and handle dict author gracefully.
- [x] **Step 4:** Run `uv run pytest tests/test_citations_references.py` to confirm pass.
- [x] **Step 5:** Git commit fix: `fix(providers): handle non-string author entries in CrossRef references`

---

### Task 3: PubMed E-Link Candidate Link ID Guard

- **Modify:** `src/scholar_mcp/providers/pubmed.py`
- **Test:** `tests/test_citations_references.py`

- [x] **Step 1:** Add test in `tests/test_citations_references.py` verifying PubMed neighbor links with null/missing ID are discarded safely.
- [x] **Step 2:** Run `uv run pytest tests/test_citations_references.py -k test_pubmed_fetch_related_papers_none_id` to confirm behavior.
- [x] **Step 3:** Edit `src/scholar_mcp/providers/pubmed.py` to filter `l.get("id") is not None` before string casting.
- [x] **Step 4:** Run `uv run pytest tests/test_citations_references.py` to confirm pass.
- [x] **Step 5:** Git commit fix: `fix(providers): guard against null link IDs in PubMed related papers`

---

### Task 4: Deduplicate Europe PMC Source/ExtID Resolution & Add DOI Fallback Tests

- **Modify:** `src/scholar_mcp/providers/europe_pmc.py`
- **Test:** `tests/test_citations_references.py`

- [x] **Step 1:** Add tests in `tests/test_citations_references.py` for Europe PMC reference and citation retrieval via DOI lookup.
- [x] **Step 2:** Run `uv run pytest tests/test_citations_references.py -k test_europe_pmc_doi_fallback` to confirm test status.
- [x] **Step 3:** Extract `_resolve_source_and_ext_id` in `src/scholar_mcp/providers/europe_pmc.py` and use in `fetch_references` and `fetch_citations`.
- [x] **Step 4:** Run `uv run pytest tests/test_citations_references.py` to confirm pass.
- [x] **Step 5:** Git commit fix: `refactor(providers): deduplicate Europe PMC DOI identifier resolution`

---

### Task 5: Full Verification & Remote PR Push

- [x] **Step 1:** Run full test suite: `uv run pytest -v` (100% green).
- [x] **Step 2:** Push branch to remote: `git push origin feature/citation-graph-and-extraction`.
- [x] **Step 3:** Output completion summary and PR URL.
