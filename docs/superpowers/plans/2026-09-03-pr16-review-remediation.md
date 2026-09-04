# PR 16 Review Remediation Plan

- **Goal:** Remediate review findings for PR #16 (bounded search enrichment concurrency, bitstream link resolution alignment, safe sizeBytes parsing, and test performance).
- **Architecture:** `WHOIRISEngine` in `src/scholar_mcp/medical/who_iris.py`, `tests/medical/test_who_iris.py`.
- **Tech Stack:** Python 3.10+, httpx, respx, pytest, pytest-asyncio.

---

### Task 1: Bounded Concurrency and Robust Bitstream Link Resolution

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/who_iris.py`
  - Modify: `tests/medical/test_who_iris.py`
- **Consumes / Produces:**
  - `WHOIRISEngine._resolve_pdf_bitstream` -> checks `_links.content.href` first before fallback to `IRIS_BITSTREAM_CONTENT_URL/{best_uuid}/content`, safely coerces `sizeBytes`.
  - `WHOIRISEngine.search_guidelines` -> uses `asyncio.Semaphore(10)` (or constant `MAX_CONCURRENT_PDF_RESOLUTIONS = 10`) when enriching items.
- **Step 1:** Write failing tests in `tests/medical/test_who_iris.py` testing:
  - `_resolve_pdf_bitstream` uses `_links.content.href` if present.
  - `_extract_pdf_link_from_item` and `_resolve_pdf_bitstream` handle non-integer `sizeBytes`.
- **Step 2:** Run `uv run pytest tests/medical/test_who_iris.py -k "test_resolve_pdf_bitstream"` to confirm.
- **Step 3:** Implement changes in `src/scholar_mcp/medical/who_iris.py`.
- **Step 4:** Run `uv run pytest tests/medical/test_who_iris.py` to confirm pass.
- **Step 5:** Git commit.

---

### Task 2: Fast Mocking for Limit Clamping Test

- **Target Files:**
  - Modify: `tests/medical/test_who_iris.py`
- **Consumes / Produces:**
  - `test_search_guidelines_clamps_limit_inside_engine` -> responds to `IRIS_ITEM_BUNDLES_URL` (or provides embedded bundles/empty) so 200 items do not trigger 20s of unmocked HTTP retries.
- **Step 1:** Update `test_search_guidelines_clamps_limit_inside_engine` in `tests/medical/test_who_iris.py`.
- **Step 2:** Run `uv run pytest tests/medical/test_who_iris.py -k "test_search_guidelines_clamps_limit_inside_engine" --durations=1` to confirm < 0.2s duration.
- **Step 3:** Git commit.

---

### Task 3: Full Verification and PR Push

- **Target Files:**
  - Entire repo test suite
- **Step 1:** Run `uv run pytest` to ensure 100% green suite.
- **Step 2:** Push to remote PR branch.
