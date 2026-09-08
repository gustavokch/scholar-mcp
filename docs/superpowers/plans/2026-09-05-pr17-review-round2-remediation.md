# PR 17 Review Round 2 Remediation Plan

## Goal
Address findings from PR #17 round 2 review:
1. Validate `b"%PDF-"` header in `SciHubProvider.fetch_pdf_bytes`.
2. Support `base_url` resolution for relative PDF URLs in `_extract_pdf_url`.
3. Guard against whitespace-only DOIs in `SciHubProvider`.

## Architecture & Interfaces
- Module: `src/scholar_mcp/providers/scihub.py`
- Tests: `tests/test_search_scihub_providers.py`

---

## Tasks

### Task 1: Validate `%PDF-` header in `SciHubProvider.fetch_pdf_bytes`
- **Files**:
  - Modify: `src/scholar_mcp/providers/scihub.py`
  - Modify: `tests/test_search_scihub_providers.py`
- **Step 1 (Red)**: Write unit test `test_scihub_fetch_pdf_bytes_ignores_non_pdf_content` expecting `None, None` when response bytes lack `b"%PDF-"`.
- **Step 2 (Verify Red)**: Run `uv run pytest tests/test_search_scihub_providers.py -k test_scihub_fetch_pdf_bytes_ignores_non_pdf_content`.
- **Step 3 (Green)**: Update `fetch_pdf_bytes` to check `if pdf_bytes and pdf_bytes.startswith(b"%PDF-"):`.
- **Step 4 (Verify Green)**: Run `uv run pytest tests/test_search_scihub_providers.py -k test_scihub_fetch_pdf_bytes_ignores_non_pdf_content`.
- **Step 5 (Commit)**: `git commit -m "fix(scihub): validate PDF magic header in fetch_pdf_bytes"`

### Task 2: Resolve relative PDF URLs in `_extract_pdf_url`
- **Files**:
  - Modify: `src/scholar_mcp/providers/scihub.py`
  - Modify: `tests/test_search_scihub_providers.py`
- **Step 1 (Red)**: Write unit test `test_scihub_extract_pdf_url_resolves_relative_path` verifying `_extract_pdf_url('<iframe src="/storage/10.1038/test.pdf"></iframe>', base_url='https://sci-hub.se/10.1038/test')` returns `https://sci-hub.se/storage/10.1038/test.pdf`.
- **Step 2 (Verify Red)**: Run `uv run pytest tests/test_search_scihub_providers.py -k test_scihub_extract_pdf_url_resolves_relative_path`.
- **Step 3 (Green)**: Update `_extract_pdf_url(html: str, base_url: str | None = None)` with `urllib.parse.urljoin`. Pass `base_url=mirror_url` from `fetch_pdf_bytes` and `_fetch_via_camoufox`.
- **Step 4 (Verify Green)**: Run `uv run pytest tests/test_search_scihub_providers.py -k test_scihub_extract_pdf_url_resolves_relative_path`.
- **Step 5 (Commit)**: `git commit -m "fix(scihub): resolve relative PDF URLs against mirror base URL"`

### Task 3: Guard whitespace-only DOIs in `SciHubProvider`
- **Files**:
  - Modify: `src/scholar_mcp/providers/scihub.py`
  - Modify: `tests/test_search_scihub_providers.py`
- **Step 1 (Red)**: Write unit test `test_scihub_whitespace_doi_is_miss`.
- **Step 2 (Verify Red)**: Run `uv run pytest tests/test_search_scihub_providers.py -k test_scihub_whitespace_doi_is_miss`.
- **Step 3 (Green)**: Update `fetch_pdf_bytes` and `fetch_full_text` with `if not ids.doi or not ids.doi.strip(): return None`.
- **Step 4 (Verify Green)**: Run `uv run pytest tests/test_search_scihub_providers.py -k test_scihub_whitespace_doi_is_miss`.
- **Step 5 (Commit)**: `git commit -m "fix(scihub): handle whitespace-only DOI gracefully"`
