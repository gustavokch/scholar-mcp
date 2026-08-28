# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
