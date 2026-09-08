# PR #19 Review Round 3 Remediation — Brazilian MoH via BVS/iAHx

Date: 2026-09-08
PR: https://github.com/gustavokch/scholar-mcp/pull/19
Head branch: `worktree-feat+brazil-moh-guidelines`

## Goal

Fix the 5 findings from PR #19 code review round 3 without regressing the 498-test suite.

## Architecture

Changes live in:
- `src/scholar_mcp/medical/brazil_moh.py`: plain country fallback, public `is_allowed_bvs_host`/`is_allowed_host`, whitespace trimming in `_as_list` & `_derive_fulltext_id`
- `src/scholar_mcp/medical/formatters.py`: import public `is_allowed_bvs_host`
- `src/scholar_mcp/server.py`: normalize collection parameter before validation in `search_brazil_moh_guidelines`
- `tests/medical/test_brazil_moh.py`, `tests/medical/test_models_formatters.py`, `tests/test_server_medical.py`: unit tests

## Tech Stack

pytest + respx, existing `AsyncHttpClient` / `SQLiteCacheManager`.

## Spec Reference

Review comment round 3 on PR #19 (2026-09-08).

---

### Task 1: Add plain string fallback and whitespace trimming in `_parse_country`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_parse_country(value: Any) -> str`
**Produces:** Correct country parsing when `pais_publicacao` contains plain strings (e.g. `"Brasil"` or `[" Brasil "]`) rather than `^`-subfields.

- Step 1: Write failing test in `test_brazil_moh.py` for `_parse_country(["Brasil"])`, `_parse_country(" Brasil ")`, and `_parse_country(["brasil"])`.
- Step 2: Run test — verify failure.
- Step 3: Implement fallback in `_parse_country` to check if cleaned string equals `BRAZIL_COUNTRY` or case-insensitive `brasil`.
- Step 4: Run test — verify pass.
- Step 5: `git commit -m "fix(brazil-moh): handle plain country string in _parse_country"`

### Task 2: Expose public `is_allowed_bvs_host` / `is_allowed_host`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, `src/scholar_mcp/medical/formatters.py`, Test `tests/medical/test_brazil_moh.py`, `tests/medical/test_models_formatters.py`

**Consumes:** `is_allowed_bvs_host(url: str) -> bool` and `_is_allowed_host` alias
**Produces:** Clean module boundary with public host checker.

- Step 1: Write failing test in `test_brazil_moh.py` verifying `is_allowed_bvs_host` and `is_allowed_host` are exported and work identically.
- Step 2: Run test — verify failure.
- Step 3: Define `is_allowed_bvs_host`, alias `is_allowed_host` and `_is_allowed_host`, update `formatters.py` import.
- Step 4: Run test — verify pass.
- Step 5: `git commit -m "refactor(brazil-moh): expose public is_allowed_bvs_host helper"`

### Task 3: Strip whitespace in `_as_list` and `_derive_fulltext_id`

**Files:** Modify `src/scholar_mcp/medical/brazil_moh.py`, Test `tests/medical/test_brazil_moh.py`

**Consumes:** `_as_list(value: Any) -> list[str]`, `_derive_fulltext_id(url: str) -> str`
**Produces:** Cleaner Solr metadata extraction ignoring whitespace-only list elements and tolerating surrounding whitespace on URLs.

- Step 1: Write failing test in `test_brazil_moh.py` for `_as_list(["   ", "pt", " "])` returning `["pt"]`, and `_derive_fulltext_id("  https://fi-admin.bvsalud.org/document/view/cfpaj  ") == "cfpaj"`.
- Step 2: Run test — verify failure.
- Step 3: Update `_as_list` and `_derive_fulltext_id` to strip whitespace.
- Step 4: Run test — verify pass.
- Step 5: `git commit -m "fix(brazil-moh): strip whitespace in list extraction and url parsing"`

### Task 4: Normalize collection parameter in `search_brazil_moh_guidelines`

**Files:** Modify `src/scholar_mcp/server.py`, Test `tests/test_server_medical.py`

**Consumes:** `search_brazil_moh_guidelines(query: str, limit: int = 10, collection: str = "all")`
**Produces:** Case-insensitive and trimmed collection handling accepting `"BRISA"`, `"all "`, etc.

- Step 1: Write failing test in `tests/test_server_medical.py` verifying `search_brazil_moh_guidelines("dengue", collection="BRISA")` succeeds.
- Step 2: Run test — verify failure.
- Step 3: Update `src/scholar_mcp/server.py` to normalize `collection = (collection or "all").strip().lower()`.
- Step 4: Run test — verify pass.
- Step 5: `git commit -m "fix(server): normalize collection parameter in brazil moh search"`
