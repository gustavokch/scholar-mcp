# scholar-mcp

Unified academic paper discovery and multi-tier waterfall full-text retrieval MCP server for AI assistants, research agents, and Claude Desktop.

![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/github/license/w8s/scholar-mcp)

---

## What is scholar-mcp?

`scholar-mcp` gives LLMs and AI agents structured access to scientific literature. It unifies PubMed discovery and CrossRef metadata with a 5-tier waterfall resolver that converts full-text academic papers into token-efficient, clean Markdown with citation noise and XML artifacts removed.

### 5-Tier Waterfall Resolver

When resolving paper full text from a DOI, PMID, PMCID, or title, `scholar-mcp` traverses:

1. **Europe PMC** — Direct JATS XML full-text extraction to clean Markdown.
2. **PubMed Central (PMC)** — NCBI E-utilities JATS XML retrieval to clean Markdown.
3. **Unpaywall** — Open-access PDF discovery and in-memory text extraction (requires email).
4. **Sci-Hub** — Multi-mirror failover and in-memory PDF text extraction.
5. **Abstract Fallback** — PubMed and CrossRef structured abstract and metadata if full text is unavailable.

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
Search academic papers across PubMed and CrossRef with structured filters and Europe PMC Open Access annotation.

```python
search_papers(
    query="crispr cas9 off target effects",
    source="auto",      # "auto" (PubMed + CrossRef top-up), "pubmed", or "crossref"
    num_results=10,     # Max 50
    year_start=2020,
    year_end=2024,
    author="Doudna J",
    journal="Nature",
)
```

### 2. `get_full_text`
Retrieve full text using the 5-tier waterfall resolver, converted to clean Markdown.

```python
get_full_text(
    identifier="10.1038/s41586-020-2003-7", # DOI, PMID, PMCID, or Title
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
Fast metadata and abstract retrieval without executing the full-text waterfall.

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

### 6. `deep_paper_analysis_prompt` & `@mcp.prompt("deep_paper_analysis")`
Constructs a structured prompt template containing the full text for comprehensive scientific analysis.

```python
deep_paper_analysis_prompt(identifier="10.1038/s41586-020-2003-7")
```

---

## Configuration Options

All options are configured via environment variables:

| Variable | Default | Description |
|---|---|---|
| `UNPAYWALL_EMAIL` | None | Email address required to enable the Unpaywall tier. |
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

---

## Acknowledgments

- PubMed search integration logic ported from [JackKuo666/PubMed-MCP-Server](https://github.com/JackKuo666/PubMed-MCP-Server) (MIT, Copyright (c) 2025 JackKuo666).
- Sci-Hub scraping and failover logic derived from [CyberKrypton/Sci-Hub-MCP-Server](https://github.com/CyberKrypton/Sci-Hub-MCP-Server) and [JackKuo666/Sci-Hub-MCP-Server](https://github.com/JackKuo666/Sci-Hub-MCP-Server).

---

## License

MIT License.
