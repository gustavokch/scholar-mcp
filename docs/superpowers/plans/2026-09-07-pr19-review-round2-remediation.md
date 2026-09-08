# PR #19 Review Round 2 Remediation — Brazilian MoH via BVS/iAHx

Date: 2026-09-07
PR: https://github.com/gustavokch/scholar-mcp/pull/19
Head branch: `worktree-feat+brazil-moh-guidelines`

## Goal

Fix the 6 findings from PR #19 code review round 2 without regressing the 495-test suite.

## Architecture

Changes live in:
- `src/scholar_mcp/medical/brazil_moh.py`: host allowlist check, regex matching, dedup consistency, Solr query sanitization, document URL selection
- `src/scholar_mcp/medical/formatters.py`: off-site document messaging
- `tests/medical/test_brazil_moh.py` and `tests/medical/test_models_formatters.py`: unit tests

## Tech Stack

pytest + respx, existing `AsyncHttpClient` / `SQLiteCacheManager`.

## Spec Reference

Review comment round 2 on PR #19 (2026-09-07).

---

### Task 1: Fix `_is_allowed_host` using `.hostname`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_is_allowed_host(url: str) -> bool`
**Produces:** Host extracted via `urlparse(url).hostname` so ports and userinfo do not cause allowlist false-negatives.

- Step 1: Failing test — assert `_is_allowed_host("https://fi-admin.bvsalud.org:443/document/view/123")` and `_is_allowed_host("https://user:pass@docs.bvsalud.org/file.pdf")` return True.
- Step 2: `PYTHONPATH=src .venv/bin/pytest tests/medical/test_brazil_moh.py -k is_allowed_host` — expect fail.
- Step 3: Use `hostname = (urllib.parse.urlparse(url or "").hostname or "").lower()`, return `hostname in FULLTEXT_ALLOWED_HOSTS`.
- Step 4: Re-run — pass.
- Step 5: `git commit -m "fix(brazil-moh): use url hostname for allowlist verification"`

### Task 2: Support query parameters and fragments in `FI_ADMIN_DOC_RE`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_derive_fulltext_id(url: str) -> str`
**Produces:** Correct document ID extracted when URL contains query parameters (e.g. `?lang=pt`) or fragments (`#p=1`).

- Step 1: Failing test — `_derive_fulltext_id("https://fi-admin.bvsalud.org/document/view/cfpaj?lang=pt") == "cfpaj"`.
- Step 2: Run test — expect fail.
- Step 3: Update `FI_ADMIN_DOC_RE = re.compile(r"^https?://fi-admin\.bvsalud\.org/document/view/([A-Za-z0-9._-]+)(?:[/?#]|$)")`.
- Step 4: Run test — pass.
- Step 5: `git commit -m "fix(brazil-moh): extract fulltext id from urls with query params"`

### Task 3: Use `_first` for document ID in `_dedupe_by_id`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_dedupe_by_id(docs: list[dict[str, Any]]) -> list[dict[str, Any]]`
**Produces:** Dedup key derived consistently using `_first(doc.get("id"))`.

- Step 1: Failing test — dedup handles docs where `id` is a list, e.g. `{"id": ["biblio-1"]}` and `{"id": "biblio-1"}` deduplicate properly.
- Step 2: Run test — expect fail.
- Step 3: Replace `record_id = str(doc.get("id") or "")` with `record_id = _first(doc.get("id"))`.
- Step 4: Run test — pass.
- Step 5: `git commit -m "fix(brazil-moh): use _first for id deduplication consistency"`

### Task 4: Sanitize `&` and `|` in `_SOLR_SPECIALS_RE`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_sanitize_token(token: str) -> str`, `_build_query(query: str, collection: str) -> str`
**Produces:** Solr binary operators `&&` and `||` stripped, preventing query syntax errors.

- Step 1: Failing test — `_build_query("dengue && zika || chikungunya", "all")` produces clean ANDed query without bare `&` or `|`.
- Step 2: Run test — expect fail.
- Step 3: Update `_SOLR_SPECIALS_RE = re.compile(r'[\[\]{}()^"~*?:\\/+!&|]')`.
- Step 4: Run test — pass.
- Step 5: `git commit -m "fix(brazil-moh): strip ampersand and pipe in solr query composition"`

### Task 5: Select best candidate URL in `_build_record`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_build_record(doc: dict[str, Any]) -> BrazilGuideline`
**Produces:** `document_url` prioritized for fi-admin / allowed hosts when `ur` is multi-valued.

- Step 1: Failing test — doc with `ur = ["https://generic-portal.com/view", "https://fi-admin.bvsalud.org/document/view/cfpaj"]` sets `document_url` to the fi-admin URL and extracts `fulltext_id="cfpaj"`.
- Step 2: Run test — expect fail.
- Step 3: Implement `_select_document_url(doc)` helper to prioritize fi-admin and allowed hosts before returning first non-empty URL.
- Step 4: Run test — pass.
- Step 5: `git commit -m "fix(brazil-moh): prioritize allowed document urls in record mapping"`

### Task 6: Clarify off-site document messaging in formatter

**Files:** Modify `src/scholar_mcp/medical/formatters.py`, Test `tests/medical/test_models_formatters.py`

**Consumes:** `format_brazil_moh_guidelines(guidelines, query, meta)`
**Produces:** Off-site message only shown when `document_url` is present but on an off-site (non-allowed) host.

- Step 1: Failing test — guideline with `document_url="https://docs.bvsalud.org/doc.pdf"` (allowed host) does not show "hosted off-site" message.
- Step 2: Run test — expect fail.
- Step 3: Check `_is_allowed_host(g.document_url)` or only show off-site note when url is present and not an allowed host.
- Step 4: Run test — pass.
- Step 5: `git commit -m "fix(brazil-moh): refine full text availability note in formatter"`

---

## Verification

1. `PYTHONPATH=src .venv/bin/pytest tests` — 100% green gate before push.
2. `git push origin worktree-feat+brazil-moh-guidelines`.
