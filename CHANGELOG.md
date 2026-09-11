# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Brazilian MoH guidelines tools**: `search_brazil_moh_guidelines` and `get_brazil_moh_full_text` MCP tools for Brazilian Ministry of Health technical publications (PCDT, CONITEC, cadernos, manuais) via BVS/iAHx API with allowlisted PDF full-text extraction, Portuguese language support, and exact-id deduplication (PR #19).
- **`CACHE_TTL_BRAZIL_MOH` setting**: Configurable cache TTL (default 2,592,000s / 30d) for caching Brazilian MoH searches and document full text in SQLite (PR #19).
- **`fonttools>=4.40.0` dependency**: Added to `pyproject.toml` for `pypdf` optional font processing support (PR #18).
- **HTTP diagnostic logging with credential redaction**: `AsyncHttpClient` logs `>= 400` errors, retries, timeouts, and exceptions with automatic query-parameter credential redaction (`api_key`, `apikey`, `email`, `token`, `access_token`) (PR #18).
- **Camoufox browser fallback for Sci-Hub**: Last-resort headless anti-detection browser fallback in `SciHubProvider` for Cloudflare-protected/blocked mirrors, capped to 3 mirrors and 20s total timeout with `%PDF-` magic header validation (PR #17).
- **Direct PDF links in WHO IRIS**: `WHOGuideline.pdf_url` field exposed in WHO IRIS search results and markdown formatted responses (PR #16).
- **`get_who_iris_full_text` MCP tool**: Full-text retrieval for WHO IRIS guidelines from DSpace 7 PDF bitstreams with abstract fallback and serve-time character truncation (PR #14).
- **`search_who_iris_guidelines` MCP tool**: WHO IRIS guideline search in `prefix` (browse) and `fulltext` (search) modes, backed by `WHOIRISEngine` and `WHOGuideline` model (PR #13).
- **`CACHE_TTL_WHO_IRIS` setting**: Configurable cache TTL (default 2,592,000s / 30d) for caching WHO IRIS search results and full text in SQLite (PR #13).
- **Query-aware re-ranking** in `search_papers`: The relevance signal now blends real lexical coverage of the query against title (weighted 2x) and abstract with the source `1/sqrt(rank + 1)` position prior; previously the query was not used for scoring. New `ScoringEngine` primitives: `tokenize`, `text_coverage`, `best_matching_sentence`. `medical/ranking.py` refactored to reuse them (behavior preserved) (PR #10).
- **Evidence grade ranking signal**: `classify_evidence_grade` maps PubMed `PublicationType` to an Oxford CEBM-style ladder (`1a` / `1b` / `2b` / `3b` / `4` / `5`); the best (lowest-rank) grade wins when a paper carries multiple types. PubMed enrichment now captures `study_type` and `issn` to support it (PR #10).
- **Journal impact ranking signal**: `lookup_journal_impact(issn, venue)` resolves a Scimago SJR value with ISSN-first, normalized-name-second lookup. `src/scholar_mcp/data/scimago_sjr.json` ships empty; the signal contributes a neutral `0.0` for every paper until the dataset is populated (procedure: `src/scholar_mcp/data/SOURCES.md`). Regenerator: `scripts/update_scimago_data.py` (PR #10).
- **Author authority ranking signal**: Last-author h-index is fetched via OpenAlex batch (`fetch_work_details_batch`, `fetch_author_h_indices_batch`) and stored on `PaperMetadata.last_author_h_index` (PR #10).
- **`check_citations` MCP tool**: Verifies each `{"text", "identifier"}` claim against the cited paper's title/abstract (or full text with `deep=True`). Verdicts: `SUPPORTED` / `WEAK` / `UNSUPPORTED` / `NOT_FOUND`. Max 25 claims per batch; per-claim failures are isolated (PR #10).
- New `Settings` fields and environment variables: `ranking_weight_evidence_grade` (0.20), `ranking_weight_journal_impact` (0.10), `ranking_weight_author_authority` (0.05), `ranking_position_weight` (0.25), `ranking_recency_half_life_years` (7.0), `ranking_candidate_multiplier` (3), `ranking_min_candidates` (20), `ranking_max_candidates` (50), `ranking_enrichment_timeout` (1.5), `citation_check_supported_threshold` (0.5), `citation_check_weak_threshold` (0.15).
- New `PaperMetadata` fields: `issn`, `study_type`, `evidence_grade`, `last_author_h_index`.

### Changed

- **Sci-Hub mirror ordering**: Re-ordered `DEFAULT_SCIHUB_MIRRORS` to put active mirror `https://sci-hub.mksa.top` first (PR #17).
- **Quieted routine HTTP retries**: Demoted 429/503 retry logs in `AsyncHttpClient` to `INFO`; `WARNING` is reserved for exhausted attempts (PR #18).
- **Bounded error body decode**: Sliced error response content to first 500 bytes and decoded with `errors="replace"` to prevent large/binary payload memory waste (PR #18).
- **Re-balanced default ranking weights**: `relevance` 0.30, `citations` 0.20, `recency` 0.15 (previously 0.4 / 0.3 / 0.3). The remaining 0.35 weight is split across the three new signals (0.20 + 0.10 + 0.05). Sum of the six signal weights is 1.0 (PR #10).

### Fixed

- **Sci-Hub Referer header**: PDF fetches now send the landing mirror page as `Referer`, which upstream PDF hosts (for example `sci.bban.top`) require. When the host refuses that header — a 401/403 or a bot-challenge page — the fetch is retried once without it, for mirrors whose hotlink protection accepts only bare requests. A request that fails outright (timeout, 5xx, transport error) is not retried and falls through to the next mirror instead (PR #25).
- **NCBI credential preservation**: `AsyncHttpClient._inject_credentials` now merges caller-provided `params` into the URL before credential injection so `httpx` does not overwrite authentication query parameters when `params` is supplied (PR #18).
- **ClinicalTrials.gov query term limit**: Capped `query.term` at 10 terms (`CT_MAX_QUERY_TERMS`) to prevent CT.gov Essie parser HTTP 400 "Too complicated query" failures on complex natural-language queries (PR #15).
- **PubMed metadata parsing DOI collision**: Fixed `PubMedProvider.fetch_abstract` to skip `ArticleId` tags nested within `ReferenceList`/`Reference` blocks, preventing cited articles' DOIs from overwriting the primary paper's DOI (PR #12).
- **openFDA natural-language drug query resolution**: Structured query ladder with quoted name clauses, per-candidate context refinement, and candidate name extraction to prevent Elasticsearch unfielded multi-term noise; bumped cache key to `fda:search:v2` (PR #11).
- **PubMed clinical guidelines over-constraining**: Added `_relaxed_queries` progressive relaxation ladder and partial publication-type score credit for keyword-layer hits to prevent empty results on specific clinical queries; bumped cache key to `guidelines:v2` (PR #11).
- **Medical PubMed relevance ranking**: `rank_medical_articles` now normalizes relevance as field coverage (title/abstract) instead of a raw weighted-hit count, so genuine lexical matches aren't outweighed by recency (PR #6, #7).
- **NCBI source-position blend**: single-source PubMed results carry a `position_weight=0.35` prior (`1/sqrt(rank+1)`) into the relevance score, keeping NCBI's own Best-Match ordering influential; multi-source merges stay at `position_weight=0.0` (PR #6, #7).
- **Rank before truncate**: `search_medical_databases` and `search_medical_journals` re-rank the deduplicated candidate pool before slicing to 20/15, instead of truncating first (PR #6, #7).
- **Journal search ranks on raw query**: `search_medical_journals` ranks against the user's original query, not the `[Journal]`-filter-expanded PubMed search term (PR #6, #7).
- Tokenizer (`_tokenize`) now guards against `None` input.

## [1.0.0] — 2026-08-28

### Changed (Breaking Changes)

- **Package & Project Rename**: Renamed project and package from `scihub-mcp` to `scholar-mcp`. Console script is now `scholar-mcp`.
- **Async Architecture**: Rewrote the entire server on `httpx` async client. Removed synchronous `requests` and `urllib3` dependencies.
- **5-Tier Waterfall Resolver**: Unified discovery and full-text retrieval pipeline:
  1. Europe PMC (JATS XML -> clean Markdown)
  2. PMC (JATS XML -> clean Markdown)
  3. Unpaywall (Legal Open Access PDF -> in-memory text extraction)
  4. Sci-Hub (Multi-mirror rotation PDF -> in-memory text extraction)
  5. Abstract Fallback (PubMed & CrossRef metadata)
- **Six MCP Tools**:
  - `search_papers` (PubMed + CrossRef search with Europe PMC Open Access annotations)
  - `get_full_text` (waterfall full-text retrieval with `max_chars` and `sections` filtering)
  - `get_full_text_batch` (concurrent batch retrieval for up to 25 papers)
  - `get_metadata` (lightweight metadata & abstract retrieval)
  - `download_paper` (sandboxed PDF download)
  - `deep_paper_analysis_prompt` + `@mcp.prompt("deep_paper_analysis")`
- **Environment Variables**:
  - Renamed `FORCE_SCIHUB` to `PREFER_SCIHUB_OVER_UNPAYWALL`.
  - Added `UNPAYWALL_EMAIL`, `PUBMED_API_KEY`, `PUBMED_EMAIL`, `PUBMED_TOOL`, `SCHOLAR_DOWNLOAD_DIR`, `SCHOLAR_MAX_CHARS`, `SCHOLAR_TOTAL_BUDGET`, `SCHOLAR_MAX_CONCURRENCY`, `SCHOLAR_CACHE_TTL`, `SCHOLAR_TITLE_MATCH_THRESHOLD`.

## [0.4.0] — 2026-06-19

### Added

- `urllib3` declared as an explicit dependency (was an implicit transitive dep of `requests`)
- PyPI classifiers: license, Python versions, topic
- `[project.urls]` block: homepage, repository, changelog, issues link

## [0.3.0] — 2026-01-01

### Added

- MIT LICENSE file
- `license` field declared in `pyproject.toml`

### Changed

- Renamed project to `scihub-mcp`
- Adopted `src/` package layout (PEP 517)
- Replaced broken `scihub` PyPI package with direct BeautifulSoup scraper
- Added CrossRef metadata enrichment: title, author, year, abstract, venue
- Fixed missing `main()` entry point that prevented `uvx` installation

## [0.2.0] — 2025-12-01

### Fixed

- Exposed `main()` at module level for uvx entrypoint
- Explicitly included `.py` files in hatchling wheel build

## [0.1.0] — 2025-11-01

### Added

- Initial MCP server with `search_scihub_by_doi`, `search_scihub_by_title`, `search_scihub_by_keyword`, `download_scihub_pdf` tools
- FastMCP integration with async tool handlers
- CrossRef-based paper discovery
- Multi-mirror Sci-Hub fallback strategy
