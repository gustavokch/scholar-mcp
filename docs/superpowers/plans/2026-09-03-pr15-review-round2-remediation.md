# PR #15 Review Round 2 Remediation Plan

- **PR:** #15 (`fix(clinical-trials): cap query.term to CT.gov parser term limit`)
- **Branch:** `fix-clinical-trials-query-cap`
- **Goal:** Fix round-2 findings: dangling boolean connector after term truncation (Essie 400), import order nit.
- **Tech Stack:** Python 3.10+, pytest.

---

### Task 1: Drop dangling AND/OR/NOT after truncation in `_cap_query_terms`

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/clinical_trials.py`
  - Test: `tests/medical/test_clinical_trials.py`
- **Consumes / Produces:** `_cap_query_terms(query, max_terms) -> str` — unchanged signature.
- **Step 1 (Red):** Add param cases:
  - `("alpha beta gamma AND", 3, "alpha beta gamma")` — wait, 4 terms, max 3 → truncate → `alpha beta gamma AND` → strip → `alpha beta gamma`. Hmm: truncation keeps 3 of 4 = `alpha beta gamma`; connector at index 3 dropped by truncation already. Use connector inside kept window: `("one two three four five six seven eight nine ten AND more", 10, "one two three four five six seven eight nine ten")`.
  - `("one two OR", 2, "one two")` — 3 terms, max 2 → `one two OR`? No: `terms[:2]` = `one two`. Use `("alpha OR beta", 2, "alpha OR beta")`? truncation keeps `alpha OR` → strip → `alpha`. Case: `("alpha OR beta", 2, "alpha")`.
  - NOT chain: `("alpha AND NOT beta", 3, "alpha")`.
  - lowercase: `("alpha and beta", 2, "alpha")`.
- **Step 2 (Verify Red):** `uv run pytest tests/medical/test_clinical_trials.py -k test_cap_query_terms_matrix -v`
- **Step 3 (Green):** In truncation branch: build `kept = terms[:max_terms]`, `while kept and kept[-1].upper() in {"AND", "OR", "NOT"}: kept.pop()`, join. Strip connectors only on truncation path — verbatim path untouched. Quote balance stays after.
- **Step 4 (Verify Green):** same command.
- **Step 5 (Commit):** `git commit -m "fix(clinical-trials): drop dangling boolean connector after query truncation"`

### Task 2: Import order nit

- **Target Files:** Modify `tests/medical/test_clinical_trials.py`.
- Move `import pytest` above `import respx` (alphabetical third-party group).
- **Commit:** folded into Task 1 commit or `git commit -m "style(tests): fix import order"`.

### Task 3: Full suite + push

- `uv run pytest` — must be green.
- `git push origin fix-clinical-trials-query-cap`
