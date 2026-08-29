# Remediation Plan for PR #5 (Medical MCP Subsystem)

- **Goal:** Remediate review findings for PR #5 covering deduplication author collision, WHO indicator float conversion resilience, pediatric drug label section coverage, bidirectional guideline organization aliasing, RxNorm empty record filtering, and server tool return typing.
- **Architecture:** Python `scholar_mcp` medical subsystem (`scholar_mcp.medical`, `scholar_mcp.utils`).
- **Tech Stack:** Python 3.10+, pytest, respx, aiosqlite, FastMCP.
- **Spec Reference:** `docs/superpowers/specs/2026-08-29-medical-mcp-python-port-design.md`.

---

## Tasks

### Task 1: Fix Author Collision in `are_duplicates()`

- **Target Files:**
  - Modify: `src/scholar_mcp/utils/deduplication.py`
  - Test: `tests/utils/test_deduplication.py`
- **Consumes / Produces:**
  - Consumes: `are_duplicates(paper1, paper2)` with identical titles and differing known first authors.
  - Produces: Returns `False` when both authors are known and non-matching.
- **Step 1: Write failing test:**
  Add `test_are_duplicates_different_authors_same_title()` to `tests/utils/test_deduplication.py`.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/utils/test_deduplication.py -k test_are_duplicates_different_authors_same_title`
- **Step 3: Minimal implementation:**
  In `src/scholar_mcp/utils/deduplication.py`, update `if t1 == t2:` block so `if a1 and a2 and a1 != a2: return False`.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/utils/test_deduplication.py`
- **Step 5: Git commit command:**
  `git commit -m "fix(deduplication): prevent false duplicate match when authors differ"`

---

### Task 2: Safe Float Conversion for WHO GHO Indicators

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/who.py`
  - Test: `tests/medical/test_who.py`
- **Consumes / Produces:**
  - Consumes: WHO GHO records with non-numeric, empty, or string `NumericValue`, `Low`, and `High` values.
  - Produces: Gracefully parsed `WHOIndicatorRecord` with `None` or float defaults without raising `ValueError`.
- **Step 1: Write failing test:**
  Add `test_get_health_statistics_non_numeric_fields()` to `tests/medical/test_who.py`.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/medical/test_who.py -k test_get_health_statistics_non_numeric_fields`
- **Step 3: Minimal implementation:**
  Add `_safe_float()` in `src/scholar_mcp/medical/who.py` and use it for `numeric_value`, `low`, and `high`.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/medical/test_who.py`
- **Step 5: Git commit command:**
  `git commit -m "fix(who): handle non-numeric and empty indicator values safely"`

---

### Task 3: Comprehensive Pediatric Section Checks in openFDA Filtering

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/fda.py`
  - Test: `tests/medical/test_fda.py`
- **Consumes / Produces:**
  - Consumes: FDA `DrugLabel` objects with pediatric details in `pediatric_dosing`, `pediatric_warnings`, `indications_and_usage`, or `use_in_specific_populations`.
  - Produces: Successfully identified pediatric drug matches in `search_pediatric_drugs()`.
- **Step 1: Write failing test:**
  Add `test_search_pediatric_drugs_matches_use_in_specific_populations()` to `tests/medical/test_fda.py`.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/medical/test_fda.py -k test_search_pediatric_drugs_matches_use_in_specific_populations`
- **Step 3: Minimal implementation:**
  In `src/scholar_mcp/medical/fda.py`, inspect `drug.pediatric_dosing`, `drug.pediatric_warnings`, `drug.indications_and_usage`, and `drug.use_in_specific_populations` in addition to purpose, warnings, and dosage.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/medical/test_fda.py`
- **Step 5: Git commit command:**
  `git commit -m "fix(fda): check all pediatric sections in search_pediatric_drugs"`

---

### Task 4: Bidirectional Organization Aliasing in Guidelines Engine

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/guidelines.py`
  - Test: `tests/medical/test_guidelines.py`
- **Consumes / Produces:**
  - Consumes: User search with full organization name e.g. `"American Heart Association"` or abbreviation `"AHA"`.
  - Produces: Matches articles using either the abbreviation or the expanded name.
- **Step 1: Write failing test:**
  Add `test_search_clinical_guidelines_organization_expansion()` to `tests/medical/test_guidelines.py`.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/medical/test_guidelines.py -k test_search_clinical_guidelines_organization_expansion`
- **Step 3: Minimal implementation:**
  In `src/scholar_mcp/medical/guidelines.py`, build bidirectional `resolve_organization_aliases()` function and apply it during filtering.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/medical/test_guidelines.py`
- **Step 5: Git commit command:**
  `git commit -m "feat(guidelines): support bidirectional organization alias matching"`

---

### Task 5: Filter Empty Concepts in RxNorm Search

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/rxnorm.py`
  - Test: `tests/medical/test_rxnorm.py`
- **Consumes / Produces:**
  - Consumes: RxNorm response with empty or blank `rxcui` or `name` items.
  - Produces: `RxNormDrug` list omitting records missing `rxcui` or `name`.
- **Step 1: Write failing test:**
  Add `test_search_drug_nomenclature_filters_empty_concepts()` to `tests/medical/test_rxnorm.py`.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/medical/test_rxnorm.py -k test_search_drug_nomenclature_filters_empty_concepts`
- **Step 3: Minimal implementation:**
  In `src/scholar_mcp/medical/rxnorm.py`, skip entries where `not rxcui or not name`.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/medical/test_rxnorm.py`
- **Step 5: Git commit command:**
  `git commit -m "fix(rxnorm): filter drug concepts with missing rxcui or name"`

---

### Task 6: Clean Server Tool Return Type Annotations

- **Target Files:**
  - Modify: `src/scholar_mcp/server.py`
  - Test: `tests/test_server_medical.py`
- **Consumes / Produces:**
  - Consumes: Tool function definitions on FastMCP server.
  - Produces: Precise `dict[str, Any]` return type annotations matching the formatters.
- **Step 1: Write failing test:**
  Add return type assertion in `tests/test_server_medical.py`.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/test_server_medical.py`
- **Step 3: Minimal implementation:**
  In `src/scholar_mcp/server.py`, change return type annotations on all 11 formatted medical tools to `dict[str, Any]`.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/test_server_medical.py`
- **Step 5: Git commit command:**
  `git commit -m "refactor(server): clean medical tool return type annotations"`
