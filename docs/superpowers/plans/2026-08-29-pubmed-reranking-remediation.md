# PR #4 Remediation Plan: PubMed Ranking & OpenAlex Citation Enrichment

**Goal:** Resolve code review findings on PR #4: fix OpenAlex filter AND-logic bug by splitting batched DOI/PMID queries, normalize DOIs during citation enrichment/caching, and make citation enrichment timeout configurable in Settings.

**Architecture:** Update `OpenAlexProvider.fetch_citation_counts_batch` to execute independent concurrent requests for DOIs and PMIDs and combine results. Update `RankingPipeline` to normalize DOIs with `_strip_doi_url` before cache/dict lookups. Add `ranking_enrichment_timeout` setting to `Settings` and `RankingPipeline.rank_papers`.

**Tech Stack:** Python 3.10+, httpx, respx, pytest, pytest-asyncio.

---

### Task 1: Fix OpenAlex Batch Citation Query Logic

- **Target files:**
  - Modify: `src/scholar_mcp/providers/openalex.py`
  - Test: `tests/test_openalex_s2.py`
- **Consumes / Produces:**
  - Consumes: `dois: list[str] | None`, `pmids: list[str] | None`
  - Produces: merged `dict[str, int]` mapping normalized lower DOIs and PMIDs to cited_by_count.
- **Step 1: Write failing test:**
  Test that `fetch_citation_counts_batch` sends separate requests when both `dois` and `pmids` are provided, properly returning counts for both without requiring intersection.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/test_openalex_s2.py -k test_openalex_fetch_citation_counts_batch_independent_filters`
- **Step 3: Minimal implementation:**
  In `fetch_citation_counts_batch`: if both `clean_dois` and `clean_pmids` are present, query them as distinct requests (gathered via `asyncio.gather`) or single request if only one is present, and aggregate results into `counts`.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/test_openalex_s2.py`
- **Step 5: Git commit command:**
  `git commit -m "fix(openalex): split doi and pmid batched queries to prevent AND filtering"`

---

### Task 2: Normalize DOIs in RankingPipeline Citation Enrichment

- **Target files:**
  - Modify: `src/scholar_mcp/ranking.py`
  - Test: `tests/test_ranking.py`
- **Consumes / Produces:**
  - Consumes: `PaperMetadata` with URL-prefixed or uppercase DOIs (e.g. `https://doi.org/10.1038/...`)
  - Produces: Correct cache hits and `oa_counts` matches.
- **Step 1: Write failing test:**
  Add a test in `tests/test_ranking.py` where candidates have `doi="https://doi.org/10.1038/foo"` and verify it correctly matches OpenAlex batch count and caches cleanly.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/test_ranking.py -k test_ranking_pipeline_normalizes_doi_url`
- **Step 3: Minimal implementation:**
  In `RankingPipeline.enrich_citations`, use `_strip_doi_url` to clean DOIs for cache checking, batch query input, and dictionary lookups.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/test_ranking.py`
- **Step 5: Git commit command:**
  `git commit -m "fix(ranking): normalize DOIs in citation enrichment pipeline"`

---

### Task 3: Configurable Ranking Enrichment Timeout

- **Target files:**
  - Modify: `src/scholar_mcp/config.py`
  - Modify: `src/scholar_mcp/ranking.py`
  - Test: `tests/test_config_models.py`
  - Test: `tests/test_ranking.py`
- **Consumes / Produces:**
  - Consumes: `RANKING_ENRICHMENT_TIMEOUT` environment variable
  - Produces: `settings.ranking_enrichment_timeout` (default 1.5s) applied in `RankingPipeline.rank_papers`.
- **Step 1: Write failing test:**
  Add test for `ranking_enrichment_timeout` in `tests/test_config_models.py` and verify `RankingPipeline` respects custom timeout.
- **Step 2: Run test to confirm failure:**
  `uv run pytest tests/test_config_models.py -k test_settings_ranking_enrichment_timeout`
- **Step 3: Minimal implementation:**
  Add `ranking_enrichment_timeout: float = 1.5` to `Settings` and load from env `RANKING_ENRICHMENT_TIMEOUT`. Use `self.settings.ranking_enrichment_timeout` in `RankingPipeline.rank_papers`.
- **Step 4: Run test to confirm pass:**
  `uv run pytest tests/test_config_models.py tests/test_ranking.py`
- **Step 5: Git commit command:**
  `git commit -m "feat(config): add configurable ranking enrichment timeout"`
