# PR #6 Remediation Plan: Robust Ranking & Post-Ranking Slicing

**Goal:** Address code review findings on PR #6 by safeguarding `_tokenize` against `None` values, removing unused `math` import, ranking all multi-database candidates before slicing the top 20, and adding full edge-case test coverage.
**Architecture:** Pure function re-ranking pipeline in `scholar_mcp.medical.ranking`, invoked within `scholar_mcp.medical.databases` and `scholar_mcp.medical.pubmed`.
**Tech Stack:** Python 3.10+, pytest, respx, asyncio.

---

### Task 1: Safeguard `_tokenize`, Remove Unused Import, and Add Ranking Edge Tests

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/ranking.py`
  - Modify: `tests/medical/test_medical_ranking.py`
- **Consumes:** `MedicalArticle` models with potentially `None` or empty text attributes, stopword-only queries.
- **Produces:** Resilient tokenization and ranking without runtime exceptions.
- **Step 1:** Write failing tests in `tests/medical/test_medical_ranking.py` for:
  - `None` title / abstract on `MedicalArticle`
  - Stopword-only query returning original articles
  - Punctuation-heavy query
- **Step 2:** Run test to confirm failure:
  `uv run pytest tests/medical/test_medical_ranking.py -k "test_none_or_empty_text or test_stopword_only_query"`
- **Step 3:** Minimal implementation:
  - Remove `import math` in `src/scholar_mcp/medical/ranking.py`
  - In `_tokenize(text: str)`: add `if not text: return []`
- **Step 4:** Run test to confirm pass:
  `uv run pytest tests/medical/test_medical_ranking.py`
- **Step 5:** Git commit:
  `git commit -m "fix(medical): guard tokenization against None and remove unused import"`

---

### Task 2: Rank All Merged Database Candidates Before Slicing Top 20

- **Target Files:**
  - Modify: `src/scholar_mcp/medical/databases.py`
  - Modify: `tests/medical/test_databases.py`
- **Consumes:** List of merged and deduplicated `MedicalArticle` candidates across PubMed, ClinicalTrials, Cochrane.
- **Produces:** Globally ranked list of articles truncated to at most 20 after scoring.
- **Step 1:** Write failing test in `tests/medical/test_databases.py` verifying that when >20 items exist across databases, high-relevance items from secondary sources are not dropped before ranking.
- **Step 2:** Run test to confirm failure:
  `uv run pytest tests/medical/test_databases.py -k "test_search_medical_databases_ranks_before_truncation"`
- **Step 3:** Minimal implementation in `src/scholar_mcp/medical/databases.py`:
  - `ranked = rank_medical_articles([MedicalArticle.from_dict(p) for p in unique], query)`
  - `final_articles = ranked[:20]`
- **Step 4:** Run test to confirm pass:
  `uv run pytest tests/medical/test_databases.py`
- **Step 5:** Git commit:
  `git commit -m "fix(medical): rank all multi-database candidates before truncation"`
