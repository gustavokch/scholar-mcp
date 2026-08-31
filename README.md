# scholar-mcp

Unified academic paper discovery and multi-tier waterfall full-text retrieval MCP server for AI assistants, research agents, and Claude Desktop.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/github/license/w8s/scholar-mcp)

---

## What is scholar-mcp?

`scholar-mcp` gives LLMs and AI agents structured access to scientific literature. It unifies PubMed discovery, CrossRef metadata, OpenAlex citation data, and Semantic Scholar recommendations with a 6-tier waterfall resolver that converts full-text academic papers into token-efficient, clean Markdown with citation noise and XML artifacts removed.

### 6-Tier Waterfall Resolver

When resolving paper full text from a DOI, PMID, PMCID, arXiv ID, or title, `scholar-mcp` traverses:

1. **Europe PMC** — Direct JATS XML full-text extraction to clean Markdown.
2. **PubMed Central (PMC)** — NCBI E-utilities JATS XML retrieval to clean Markdown.
3. **Unpaywall** — Open-access PDF discovery and in-memory text extraction (requires email).
4. **arXiv** — Preprint PDF retrieval and in-memory text extraction (only when an arXiv ID is known; arXiv DOIs `10.48550/arXiv.*` are detected automatically).
5. **Sci-Hub** — Multi-mirror failover and in-memory PDF text extraction.
6. **Abstract Fallback** — PubMed, CrossRef, and arXiv structured abstract and metadata if full text is unavailable.

Accepted identifier formats include `10.xxxx/...` DOIs, `PMID:...`, `PMC...`, arXiv IDs (`arXiv:2305.18290`, `2305.18290v2`, `hep-th/9901001`, arxiv.org abs/pdf URLs), and free-text titles.

---

## Quick Start

### Claude Desktop Configuration

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "scholar": {
      "command": "uvx",
      "args": ["scholar-mcp"],
      "env": {
        "UNPAYWALL_EMAIL": "your-email@example.com",
        "PUBMED_EMAIL": "your-email@example.com"
      }
    }
  }
}
```

---

## Migration from `scihub-mcp` (v0.4.0)

`scholar-mcp` is a complete rewrite and superset of `scihub-mcp`.

| `scihub-mcp` (Old) | `scholar-mcp` (New) |
|---|---|
| Command: `uvx scihub-mcp` | Command: `uvx scholar-mcp` |
| `search_scihub_by_keyword` | `search_papers(query, source="auto")` |
| `search_scihub_by_title` | `get_full_text(title)` or `get_metadata(title)` |
| `search_scihub_by_doi` | `get_full_text(doi)` or `get_metadata(doi)` |
| `download_scihub_pdf` | `download_paper(identifier, output_path)` |
| Env var: `FORCE_SCIHUB` | Env var: `PREFER_SCIHUB_OVER_UNPAYWALL` |

---

## Tools

### 1. `search_papers`
Search academic papers across PubMed, CrossRef, and Semantic Scholar with structured filters and Europe PMC Open Access annotation.

```python
search_papers(
    query="crispr cas9 off target effects",
    source="auto",      # "auto" (PubMed + CrossRef top-up), "pubmed", "crossref", or "s2" (Semantic Scholar)
    num_results=10,     # Max 50
    year_start=2020,
    year_end=2024,
    author="Doudna J",
    journal="Nature",
)
```

### 2. `get_full_text`
Retrieve full text using the 6-tier waterfall resolver, converted to clean Markdown.

```python
get_full_text(
    identifier="10.1038/s41586-020-2003-7", # DOI, PMID, PMCID, arXiv ID, or Title
    max_chars=50000,                         # Optional token budget cap
    sections=["Methods", "Results"],         # Optional section filter
)
```

### 3. `get_full_text_batch`
Concurrent full-text retrieval for up to 25 papers with bounded concurrency.

```python
get_full_text_batch(
    identifiers=["32000000", "PMC7000000", "10.1038/nature123"]
)
```

### 4. `get_metadata`
Fast metadata and abstract retrieval without executing the full-text waterfall. Responses include OpenAlex-enriched `citation_count`, `oa_url`, and `institutions` fields when available.

```python
get_metadata(identifier="32000000")
```

### 5. `download_paper`
Download the PDF of a paper into the sandboxed download directory.

```python
download_paper(
    identifier="10.1038/s41586-020-2003-7",
    output_path="nature_paper.pdf",
    overwrite=False,
)
```

### 6. `get_references`
Extract bibliography and cited references for an academic paper (via Europe PMC and CrossRef).

```python
get_references(
    identifier="10.1038/s41586-020-2003-7",
    limit=50,  # Max 100
)
```

### 7. `get_citations`
Retrieve forward citations (papers citing this target paper) via Europe PMC, with OpenAlex as fallback (covers non-biomedical literature).

```python
get_citations(
    identifier="10.1038/s41586-020-2003-7",
    limit=50,  # Max 100
)
```

### 8. `get_related_papers`
Retrieve computationally related and similar literature via PubMed E-Link, with Semantic Scholar recommendations as fallback (works from DOI or arXiv ID).

```python
get_related_papers(
    identifier="32000000",
    limit=10,  # Max 25
)
```

### 9. `deep_paper_analysis_prompt` & `@mcp.prompt("deep_paper_analysis")`
Constructs a structured prompt template containing the full text for comprehensive scientific analysis.

```python
deep_paper_analysis_prompt(identifier="10.1038/s41586-020-2003-7")
```

---

## Medical Tools

`scholar-mcp` includes native medical intelligence tools backed by openFDA, RxNav, WHO Global Health Observatory, ClinicalTrials.gov, PubMed, and AAP clinical guidelines with persistent SQLite caching and cross-source deduplication.

Install with the optional `medical` extra to enable browser-based scraping fallbacks for Cochrane and AAP portals:
```bash
pip install 'scholar-mcp[medical]'
```

### Medical Tool Suite

| Tool | Source | Description |
|---|---|---|
| `search_drugs` | openFDA | Search drug labels, generic/brand names, active ingredients, and indications. |
| `get_drug_details` | openFDA | Fetch full structured FDA drug label sections by NDC code. |
| `search_pediatric_drugs` | openFDA | Search FDA labels filtered for pediatric indications and dosing. |
| `search_drug_nomenclature` | RxNav | Search standardized RxNorm drug concepts, synonyms, RxCUIs, and UMLS CUIs. |
| `get_health_statistics` | WHO GHO | Query global and country-specific health indicators with synonym expansion. |
| `get_child_health_statistics` | WHO GHO | Retrieve pediatric and infant health metrics from WHO GHO. |
| `search_clinical_guidelines` | PubMed | Search PubMed for clinical practice guidelines with heuristic relevance scoring. |
| `search_pediatric_guidelines` | Bright Futures / AAP Policy | Search pediatric clinical practice guidelines across AAP sources. |
| `search_aap_guidelines` | Bright Futures & AAP | Concurrent search across AAP Bright Futures and Policy Statements with deduplication. |
| `search_pediatric_literature` | PubMed | Targeted search across 7 premier pediatric medical journals. |
| `search_medical_databases` | PubMed, ClinicalTrials, Cochrane | Cross-database literature search with fuzzy deduplication and metadata preservation. |
| `search_medical_journals` | PubMed | Search top-tier medical journals (NEJM, JAMA, Lancet, BMJ, Nature Medicine). |
| `get_medical_cache_stats` | SQLite Cache | Retrieve hit/miss metrics and active entry counts from the SQLite cache. |

---

## Configuration Options

All options are configured via environment variables:

| Variable | Default | Description |
|---|---|---|
| `UNPAYWALL_EMAIL` | None | Email address required to enable the Unpaywall tier. |
| `OPENALEX_MAILTO` | Falls back to `UNPAYWALL_EMAIL`/`PUBMED_EMAIL` | Email for the OpenAlex polite pool. |
| `S2_API_KEY` | None | Semantic Scholar API key (raises S2 rate limit from 1 rps to 5 rps). |
| `ENABLE_OPENALEX` | `true` | Master switch for OpenAlex metadata enrichment and citations fallback. |
| `ENABLE_S2` | `true` | Master switch for Semantic Scholar search and recommendations. |
| `PUBMED_EMAIL` | None | Email sent in NCBI and CrossRef polite pool headers. |
| `PUBMED_API_KEY` | None | NCBI API key (raises rate limit from 3 rps to 10 rps). |
| `PUBMED_TOOL` | `ScholarMCP` | Tool identifier sent to NCBI E-utilities. |
| `ENABLE_SCIHUB` | `true` | Master switch for Sci-Hub tier. `false` disables Sci-Hub. |
| `PREFER_SCIHUB_OVER_UNPAYWALL` | `false` | When `true`, tries Sci-Hub before Unpaywall. |
| `SCIHUB_MIRRORS` | Built-in list | Comma-separated list of Sci-Hub mirror base URLs. |
| `SCHOLAR_DOWNLOAD_DIR` | `./downloads` | Root directory sandbox for `download_paper`. |
| `SCHOLAR_MAX_CHARS` | `50000` | Default character limit for full-text responses. |
| `SCHOLAR_TOTAL_BUDGET` | `45` | Total wall-clock budget (seconds) per full-text call. |
| `SCHOLAR_MAX_CONCURRENCY` | `5` | Semaphore limit for concurrent batch requests. |
| `SCHOLAR_CACHE_TTL` | `3600` | Cache time-to-live in seconds for IDs and metadata. |
| `SCHOLAR_TITLE_MATCH_THRESHOLD` | `80.0` | CrossRef minimum score for resolving title queries. |
| `SCHOLAR_CACHE_DB` | `~/.cache/scholar_mcp/cache.db` | Persistent SQLite cache database path. |
| `CACHE_MAX_SIZE` | `1000` | Maximum entries in persistent SQLite cache before LRU eviction. |
| `CACHE_TTL_FDA` | `86400` | openFDA drug label cache TTL in seconds (24h). |
| `CACHE_TTL_PUBMED` | `3600` | Medical PubMed search cache TTL in seconds (1h). |
| `CACHE_TTL_WHO` | `604800` | WHO Global Health Observatory cache TTL in seconds (7d). |
| `CACHE_TTL_RXNORM` | `2592000` | RxNorm drug nomenclature cache TTL in seconds (30d). |
| `CACHE_TTL_GUIDELINES` | `604800` | Clinical practice guidelines cache TTL in seconds (7d). |
| `CACHE_TTL_BRIGHT_FUTURES` | `2592000` | AAP Bright Futures scraping cache TTL in seconds (30d). |
| `CACHE_TTL_AAP_POLICY` | `604800` | AAP Policy statements scraping cache TTL in seconds (7d). |
| `CACHE_TTL_PEDIATRIC_JOURNALS` | `3600` | Pediatric journals literature cache TTL in seconds (1h). |
| `CACHE_TTL_CHILD_HEALTH` | `604800` | WHO child health indicators cache TTL in seconds (7d). |
| `CACHE_TTL_PEDIATRIC_DRUGS` | `86400` | Pediatric drug search cache TTL in seconds (24h). |
| `CACHE_TTL_CLINICAL_TRIALS` | `86400` | ClinicalTrials.gov cache TTL in seconds (24h). |
| `ENABLE_BROWSER_FALLBACK` | `true` | Enable the last-resort camoufox (headless anti-detection Firefox) browser fallback for scraping. `ENABLE_PLAYWRIGHT_FALLBACK` still works as a legacy alias. |
| `ENABLE_MEDICAL_TOOLS` | `true` | Master switch for medical MCP tools and persistent cache. |

---

## Acknowledgments

- PubMed search integration logic ported from [JackKuo666/PubMed-MCP-Server](https://github.com/JackKuo666/PubMed-MCP-Server) (MIT, Copyright (c) 2025 JackKuo666).
- Sci-Hub scraping and failover logic derived from [CyberKrypton/Sci-Hub-MCP-Server](https://github.com/CyberKrypton/Sci-Hub-MCP-Server) and [JackKuo666/Sci-Hub-MCP-Server](https://github.com/JackKuo666/Sci-Hub-MCP-Server).

---

## License

MIT License.
