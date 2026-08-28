# Specification: Unified Academic Discovery & Full-Text MCP Server (`scholar-mcp`)

## 1. Overview

`scholar-mcp` unifies PubMed scientific discovery and Sci-Hub full-text retrieval into a single FastMCP server. It implements a multi-tier waterfall retrieval pipeline that checks legal Open Access sources first (PubMed Central JATS XML, Europe PMC, Unpaywall), optionally routes through Sci-Hub, and degrades gracefully to structured abstracts.

Full-text articles are rendered as clean, token-efficient Markdown directly inside MCP tool responses for LLM consumption, with dedicated tools for local PDF/file download and deep paper analysis.

---

## 2. Goals & Non-Goals

### Goals
- **Single Source of Truth:** Unified MCP server providing paper search, metadata resolution, full-text extraction, and file downloading across scientific domains (biomedical and general).
- **Waterfall Full-Text Resolver:** Prioritized retrieval order (PMC OA -> Europe PMC OA -> Unpaywall -> Sci-Hub -> Abstract fallback).
- **LLM-Optimized Text Extraction:** Clean JATS XML-to-Markdown conversion (stripping citation bibliographies, MathML noise, and licensing boilerplate) and in-memory PDF-to-text extraction.
- **Identifier Agnostic:** Automatic normalization and conversion between PMID, PMCID, DOI, and Title.
- **Configurable Routing:** Config flags `ENABLE_SCIHUB` and `FORCE_SCIHUB` to tune compliance vs. speed.

### Non-Goals
- Full citation graph analysis or reference tree traversing.
- Optical Character Recognition (OCR) for scanned legacy image PDFs.
- Long-term persistent database storage (in-memory LRU cache is sufficient).

---

## 3. Architecture & Waterfall Resolution Flow

### 3.1 Identifier Resolution
Given any input identifier (PMID, PMCID, DOI, or Title string), the system resolves all corresponding IDs:
1. `PMID` -> `DOI` and `PMCID` via NCBI E-utilities `esummary` / `idconv` APIs.
2. `DOI` -> `PMID` and `PMCID` via Europe PMC / PubMed `esearch`.
3. `Title` -> `DOI` via CrossRef Works API (`query.title`).

### 3.2 Full-Text Waterfall Pipeline

```
                              [Input Identifier]
                                      │
                                      ▼
                           [Normalize & Map IDs]
                           (PMID, PMCID, DOI)
                                      │
                                      ▼
                       ┌──────────────────────────────┐
                       │  Step 1: PMC Open Access     │
                       │  (efetch JATS XML -> MD)     │
                       └──────────────┬───────────────┘
                         Found? ──YES─┴─────────────► [Return Full Text (PMC XML)]
                                  │ NO
                                  ▼
                       ┌──────────────────────────────┐
                       │  Step 2: Europe PMC OA       │
                       │  (XML / Direct OA PDF)       │
                       └──────────────┬───────────────┘
                         Found? ──YES─┴─────────────► [Return Full Text (Europe PMC)]
                                  │ NO
                                  ▼
                     ┌───────────────────────────┐
                     │ FORCE_SCIHUB is Enabled?  ├─YES─► (Jump directly to Step 4)
                     └────────────┬──────────────┘
                                  │ NO
                                  ▼
                       ┌──────────────────────────────┐
                       │  Step 3: Unpaywall (DOI)     │
                       │  (Publisher OA / Green Rep)  │
                       └──────────────┬───────────────┘
                         Found? ──YES─┴─────────────► [Return Full Text (Unpaywall PDF)]
                                  │ NO
                                  ▼
                       ┌──────────────────────────────┐
                       │  Step 4: Sci-Hub Mirrors     │
                       │  (if ENABLE_SCIHUB is True)  │
                       └──────────────┬───────────────┘
                         Found? ──YES─┴─────────────► [Return Full Text (Sci-Hub PDF)]
                                  │ NO
                                  ▼
                       ┌──────────────────────────────┐
                       │  Step 5: Abstract Fallback   │
                       │  (PubMed / CrossRef abstract)│
                       └──────────────┬───────────────┘
                                      ▼
                         [Return Abstract + Links]
```

---

## 4. MCP Tools Specification

### 4.1 `search_papers`
Search for scientific literature across PubMed or CrossRef.
- **Parameters**:
  - `query` (`str`, required): Search terms, keywords, or boolean query.
  - `source` (`str`, optional, default `"auto"`): Search engine to query (`"auto"`, `"pubmed"`, `"crossref"`).
  - `num_results` (`int`, optional, default `10`, max `50`): Number of results to return.
  - `year_start` (`int`, optional): Starting publication year filter.
  - `year_end` (`int`, optional): Ending publication year filter.
  - `author` (`str`, optional): Author name filter.
  - `journal` (`str`, optional): Journal title filter.
- **Returns**: `list[PaperMetadata]` with `title`, `authors`, `year`, `venue`, `doi`, `pmid`, `pmcid`, `abstract`, `oa_status`.

### 4.2 `get_full_text`
Retrieve full text of a paper using the prioritized waterfall pipeline.
- **Parameters**:
  - `identifier` (`str`, required): PMID (e.g. `"34567890"`), PMCID (e.g. `"PMC8765432"`), DOI (e.g. `"10.1038/s41586-020-2003-7"`), or Title.
- **Returns**: `FullTextResponse` containing:
  - `status`: `"full_text"` | `"abstract_only"` | `"not_found"`
  - `source`: `"pmc"` | `"europepmc"` | `"unpaywall"` | `"scihub"` | `"abstract_fallback"`
  - `format`: `"markdown"` | `"text"`
  - `title`: Paper title
  - `doi`: Normalized DOI (if found)
  - `pmid`: PubMed ID (if found)
  - `pmcid`: PMC ID (if found)
  - `content`: Complete extracted article body formatted in Markdown, or abstract text if fallback.
  - `url`: Direct link to OA source, publisher DOI, or PDF location.

### 4.3 `get_full_text_batch`
Batch check and preview full text availability for multiple identifiers.
- **Parameters**:
  - `identifiers` (`list[str]`, required, max `25`): List of PMIDs, PMCIDs, or DOIs.
- **Returns**: `list[FullTextSummary]` with availability status, source found, and short excerpt.

### 4.4 `download_paper`
Download original PDF or text document to local file.
- **Parameters**:
  - `identifier` (`str`, required): PMID, PMCID, or DOI.
  - `output_path` (`str`, required): Target file path on disk (e.g. `"./papers/paper.pdf"`).
- **Returns**: `DownloadResult` (`success: bool`, `saved_path: str`, `source_used: str`, `file_size_bytes: int`, `message: str`).

### 4.5 `deep_paper_analysis_prompt`
Generate comprehensive multi-point analytical evaluation prompt from article metadata and extracted content.
- **Parameters**:
  - `identifier` (`str`, required): PMID, PMCID, or DOI.
- **Returns**: `dict[str, str]` with `analysis_prompt`.

---

## 5. Parsers & Extractors

### 5.1 JATS XML to Markdown Converter (`scholar_mcp.parsers.jats`)
- Parses PMC full-text XML documents in document order.
- **Transformations**:
  - `<article-title>` -> `# Title`
  - `<contrib>` -> `**Authors:** Author 1, Author 2...`
  - `<abstract>` -> `## Abstract\n\n...`
  - `<sec><title>...</title></sec>` -> `## Section Title` (nested depth mapped up to `######`).
  - `<p>` -> normalized single-spaced paragraphs separated by blank lines.
  - `<list list-type="...">` -> `- item` or `1. item`.
  - `<fig>` / `<table-wrap>` -> `[Figure 1] Caption text` / `[Table 1] Caption text`.
  - `<table><tr><td>...</td></tr></table>` -> Markdown table rows (`| col1 | col2 |`).
  - `<boxed-text>` -> `> blockquote` callout.
  - `<inline-formula>` -> `alttext` or `[formula]` placeholder.
- **Removed Elements**:
  - `<ref-list>`, `<ref>` (citations stripped to minimize token overhead).
  - `<fn-group>`, `<fn>` (footnotes).
  - `<mml:math>`, `<tex-math>` (raw MathML/TeX trees).
  - `<permissions>`, `<license>`, `<copyright-holder>` (boilerplate).
  - `<supplementary-material>`, `<related-article>`.

### 5.2 In-Memory PDF Text Extractor (`scholar_mcp.parsers.pdf`)
- Extracts raw text using `pypdf.PdfReader` over `io.BytesIO` streams (no disk I/O).
- Post-processes text: strips recurring header/footer noise, stitches hyphenated word breaks across lines (`infor-\nmation` -> `information`), collapses whitespace.

---

## 6. Configuration & Environment Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `PUBMED_API_KEY` | `str` | `None` | NCBI API key (raises E-utilities rate limit from 3 to 10 req/s) |
| `PUBMED_EMAIL` | `str` | `None` | Contact email for NCBI E-utilities |
| `UNPAYWALL_EMAIL` | `str` | `None` | Required email for Unpaywall API requests |
| `ENABLE_SCIHUB` | `bool` | `True` | Whether to attempt Sci-Hub fallback |
| `FORCE_SCIHUB` | `bool` | `False` | When True, bypass Unpaywall and jump straight to Sci-Hub if PMC/Europe PMC fails |
| `SCIHUB_MIRRORS` | `str` | `"https://sci-hub.hkvisa.net,https://sci-hub.mksa.top,https://sci-hub.ren,https://sci-hub.se,https://sci-hub.st,https://sci-hub.ee"` | Comma-separated list of active Sci-Hub mirrors |
| `SCHOLAR_REQUEST_TIMEOUT` | `int` | `30` | HTTP request timeout in seconds |
| `SCHOLAR_CACHE_SIZE` | `int` | `500` | In-memory LRU cache capacity for identifier & metadata mappings |

---

## 7. Package & Project Layout

```
scihub-mcp/ (renamed package: scholar-mcp)
├── pyproject.toml
├── README.md
├── LICENSE
├── docs/
│   └── superpowers/specs/2026-08-28-scholar-mcp-unified-design.md
├── src/
│   └── scholar_mcp/
│       ├── __init__.py
│       ├── server.py              # FastMCP application & tool registrations
│       ├── config.py              # Settings & env var validation
│       ├── models.py              # Data models (PaperMetadata, FullTextResponse, etc.)
│       ├── resolver.py            # Waterfall pipeline coordinator
│       ├── identifiers.py         # PMID <-> PMCID <-> DOI conversion utilities
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── base.py            # BaseProvider interface
│       │   ├── pmc.py             # PMC JATS XML provider
│       │   ├── europe_pmc.py      # Europe PMC REST API provider
│       │   ├── unpaywall.py       # Unpaywall API provider
│       │   ├── scihub.py          # Sci-Hub scraper & mirror rotator
│       │   ├── pubmed.py          # PubMed E-utilities search & metadata
│       │   └── crossref.py        # CrossRef API search & metadata
│       ├── parsers/
│       │   ├── __init__.py
│       │   ├── jats.py            # JATS XML to Markdown parser
│       │   └── pdf.py             # PDF text extraction parser
│       └── utils/
│           ├── __init__.py
│           ├── http.py            # Session management, backoff & retry helpers
│           └── cache.py           # In-memory TTL / LRU cache
└── tests/
    ├── test_jats_parser.py        # JATS XML conversion tests
    ├── test_pdf_parser.py         # PDF text extraction tests
    ├── test_identifiers.py        # ID conversion tests
    ├── test_waterfall.py          # Resolver fallback logic tests
    └── test_server.py             # FastMCP tool integration tests
```

---

## 8. Testing Strategy

1. **JATS XML Converter Tests:**
   - Test against representative PMC JATS XML containing titles, sections, tables, formula placeholders, figures, and abstract types.
   - Assert zero leftover `<...>` XML tags.
   - Assert references (`<ref-list>`) and licensing tags are completely absent.
2. **PDF Parser Tests:**
   - Test extraction from valid single-page and multi-page PDF byte streams.
   - Test handling of encrypted / corrupt PDF streams without crashes.
3. **Identifier Conversion Tests:**
   - Test PMID -> DOI, DOI -> PMID, and PMCID lookup resolution with mocked responses.
4. **Waterfall Resolver Tests:**
   - Test PMC success (terminates at Step 1).
   - Test PMC failure -> Europe PMC success (terminates at Step 2).
   - Test Europe PMC failure -> Unpaywall success (terminates at Step 3).
   - Test `FORCE_SCIHUB=True` jumping from Step 2 failure directly to Sci-Hub.
   - Test total OA failure -> Sci-Hub success.
   - Test total failure -> Abstract fallback.
5. **Tool Invocations:**
   - Integration tests on FastMCP tool endpoints.
