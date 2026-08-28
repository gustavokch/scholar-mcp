# Specification: Unified Academic Discovery & Full-Text MCP Server (`scholar-mcp`)

> **Revision 2 (2026-08-28)** — amended after spec review. This revision resolves four design
> questions (bounded output, async I/O, download sandboxing, package rename) and closes twelve
> gaps found in revision 1. Changes are summarised in section 11. The implementation plan at
> `docs/superpowers/plans/2026-08-28-scholar-mcp-unified-plan.md` matches this revision.

## 1. Overview

`scholar-mcp` unifies PubMed scientific discovery and Sci-Hub full-text retrieval into a single
FastMCP server. It implements a multi-tier waterfall retrieval pipeline that checks legal Open
Access sources first (PubMed Central JATS XML, Europe PMC, Unpaywall), optionally routes through
Sci-Hub, and degrades gracefully to structured abstracts.

Full-text articles are rendered as clean, token-efficient Markdown directly inside MCP tool
responses for LLM consumption, bounded by an explicit character cap and optionally narrowed to
named sections, with dedicated tools for local PDF download and deep paper analysis.

---

## 2. Goals & Non-Goals

### Goals
- **Single Source of Truth:** Unified MCP server providing paper search, metadata resolution,
  full-text extraction, and file downloading across scientific domains (biomedical and general).
- **Waterfall Full-Text Resolver:** Prioritized retrieval order
  (PMC OA -> Europe PMC OA -> Unpaywall -> Sci-Hub -> Abstract fallback).
- **LLM-Optimized Text Extraction:** Clean JATS XML-to-Markdown conversion (stripping citation
  bibliographies, MathML noise, and licensing boilerplate) and in-memory PDF-to-text extraction.
- **Bounded, Navigable Output:** Every full-text response is capped at a configurable character
  budget and can be narrowed to named sections, so a long article never silently consumes the
  caller's whole context window.
- **Identifier Agnostic:** Automatic normalization and conversion between PMID, PMCID, DOI, and
  Title, with an explicit confidence threshold on title matching.
- **Async Throughout:** All network I/O is non-blocking `httpx`, so waterfall tiers and batch
  requests complete within predictable time budgets.
- **Observable Fallbacks:** Every response reports what each tier did and why, so waterfall
  behavior is debuggable in production.
- **Configurable Routing:** Config flags `ENABLE_SCIHUB` and `PREFER_SCIHUB_OVER_UNPAYWALL` to
  tune compliance vs. speed.
- **Safe Local Writes:** Downloads are confined to a configured directory root.

### Non-Goals
- Full citation graph analysis or reference tree traversing.
- Optical Character Recognition (OCR) for scanned legacy image PDFs.
- Long-term persistent database storage (an in-memory TTL/LRU cache is sufficient).
- Caching of full-text bodies or PDF bytes (see section 6.2).
- Backward compatibility with the `scihub-mcp` package (see section 10).

---

## 3. Architecture & Waterfall Resolution Flow

### 3.1 Identifier Resolution

Given any input identifier (PMID, PMCID, DOI, or Title string), the system resolves all
corresponding IDs:

1. `PMID` / `PMCID` / `DOI` -> the full identifier set via the NCBI ID Converter API
   (`/pmc/utils/idconv`), supplemented by `esummary` where needed.
2. `Title` -> `DOI` via the CrossRef Works API (`query.bibliographic`).

**Detection order** is significant and is fixed as: PMCID (`PMC\d+`) before DOI (`10.\d{4,9}/...`)
before a bare-digit PMID; anything else is treated as a title. DOIs are normalized by stripping
`doi:` and `https://doi.org/` prefixes and trailing punctuation, and lowercased for cache keys.

**Title-match confidence.** CrossRef always returns a best-effort result for any query string,
including nonsense. The resolver therefore compares the returned `score` against
`SCHOLAR_TITLE_MATCH_THRESHOLD` (default `80.0`):

- **At or above threshold** — the DOI is accepted and `match_score` is reported.
- **Below threshold** — no DOI is accepted. The resolver returns `status="ambiguous_match"` with
  the candidate title and `match_score` so the caller can disambiguate. No full-text tier runs.

This prevents the failure mode where a weak title match silently returns the full text of an
unrelated paper.

**Failure behavior.** Identifier resolution never raises. If every upstream lookup fails, the
resolver returns an `IdentifierMap` populated with whatever the caller supplied, and the waterfall
proceeds with the tiers that identifier can support.

### 3.2 Full-Text Waterfall Pipeline

```
                              [Input Identifier]
                                      │
                                      ▼
                           [Normalize & Map IDs]
                           (PMID, PMCID, DOI)
                                      │
                        Ambiguous title match? ──YES──► [Return status="ambiguous_match"]
                                      │ NO
                                      ▼
                    ╔═════ total budget: SCHOLAR_TOTAL_BUDGET ═════╗
                    ║                                              ║
                    ║   ┌──────────────────────────────┐           ║
                    ║   │  Step 1: PMC Open Access     │           ║
                    ║   │  (efetch JATS XML -> MD)     │           ║
                    ║   └──────────────┬───────────────┘           ║
                    ║     Found? ──YES─┴──────────► [Full Text: pmc]
                    ║              │ NO                            ║
                    ║              ▼                               ║
                    ║   ┌──────────────────────────────┐           ║
                    ║   │  Step 2: Europe PMC OA       │           ║
                    ║   │  (fullTextXML / OA PDF)      │           ║
                    ║   └──────────────┬───────────────┘           ║
                    ║     Found? ──YES─┴──────────► [Full Text: europepmc]
                    ║              │ NO                            ║
                    ║              ▼                               ║
                    ║   ┌──────────────────────────────┐           ║
                    ║   │  Step 3: Unpaywall (DOI)     │           ║
                    ║   │  SKIPPED if:                 │           ║
                    ║   │   - UNPAYWALL_EMAIL unset    │           ║
                    ║   │   - PREFER_SCIHUB_OVER_      │           ║
                    ║   │     UNPAYWALL and Sci-Hub    │           ║
                    ║   │     tier is enabled          │           ║
                    ║   └──────────────┬───────────────┘           ║
                    ║     Found? ──YES─┴──────────► [Full Text: unpaywall]
                    ║              │ NO                            ║
                    ║              ▼                               ║
                    ║   ┌──────────────────────────────┐           ║
                    ║   │  Step 4: Sci-Hub Mirrors     │           ║
                    ║   │  SKIPPED if ENABLE_SCIHUB    │           ║
                    ║   │  is false, or no DOI known   │           ║
                    ║   └──────────────┬───────────────┘           ║
                    ║     Found? ──YES─┴──────────► [Full Text: scihub]
                    ║              │ NO                            ║
                    ╚══════════════│═══════════════════════════════╝
                                   ▼   (also reached on budget expiry)
                       ┌──────────────────────────────┐
                       │  Step 5: Abstract Fallback   │
                       │  (PubMed / CrossRef abstract)│
                       └──────────────┬───────────────┘
                         Found? ──YES─┴──────────► [Abstract + Links]
                                  │ NO
                                  ▼
                          [status="not_found"]
```

Every step, including skipped ones, appends a `FetchAttempt` record to the response.

### 3.3 Tier Gating Rules

`ENABLE_SCIHUB` is the master switch and `PREFER_SCIHUB_OVER_UNPAYWALL` is only a reordering
preference. The two interact as follows, and this table is normative:

| `ENABLE_SCIHUB` | `PREFER_SCIHUB_OVER_UNPAYWALL` | Step 3 (Unpaywall) | Step 4 (Sci-Hub) |
|---|---|---|---|
| `true` | `false` (default) | runs | runs if Step 3 missed |
| `true` | `true` | **skipped** | runs |
| `false` | `false` | runs | **skipped** |
| `false` | `true` | **runs** | **skipped** |

The last row is the important one: disabling Sci-Hub must never also disable Unpaywall, or the
compliance-conscious configuration would be the one with the fewest working tiers.

> **Note on naming.** Revision 1 called this flag `FORCE_SCIHUB`. The name was misleading — it
> never forced Sci-Hub, it only moved it ahead of Unpaywall, and it had no defined interaction
> with `ENABLE_SCIHUB`. It is renamed here. This is a breaking configuration change (section 10).

### 3.4 What Counts As A Hit

A tier is a **hit** only when it yields extracted text of at least `MIN_USEFUL_CHARS`
(200 characters). Anything shorter is treated as a **miss** and the waterfall continues. This
rule exists because two common upstream responses are superficially successful but useless:

- PMC returns HTTP 200 with a metadata-only stub for records that are indexed but not in the OA
  subset.
- An OA PDF may be a scanned page image, which `pypdf` extracts to whitespace (OCR is a non-goal).

Without this gate, either case would short-circuit the waterfall and return an empty body.

### 3.5 Time Budget

Each `get_full_text` call runs under a total wall-clock budget (`SCHOLAR_TOTAL_BUDGET`, default
45s) that is independent of the per-request timeout (`SCHOLAR_REQUEST_TIMEOUT`, default 30s).
Five tiers at a 30s per-request timeout could otherwise reach 150s, past most MCP client
timeouts. On budget expiry the in-flight tier is recorded with `outcome="timeout"` and the
resolver degrades to the abstract fallback rather than raising.

### 3.6 Concurrency Model

All providers are `async def` over a single shared `httpx.AsyncClient`. There is no
`requests` usage and no `asyncio.to_thread` bridge anywhere in the network path; the sole
permitted use of `to_thread` is the local disk write in `download_paper`, which is filesystem
I/O rather than network I/O.

`get_full_text_batch` fans out across identifiers with an `asyncio.Semaphore` bounded by
`SCHOLAR_MAX_CONCURRENCY` (default 5). A failure on one identifier yields an error entry for
that identifier only and never fails the batch.

**Rate limiting.** A per-host token bucket sits in front of the HTTP client. NCBI E-utilities
hosts are limited to the documented ceiling — 3 requests/second without an API key, 10 with one —
derived automatically from whether `PUBMED_API_KEY` is set. Other hosts get a permissive default.
Without this, batch fan-out would breach the NCBI limit and risk a block.

---

## 4. MCP Tools Specification

Six tools are exposed. `deep_paper_analysis` is additionally registered as a native MCP prompt;
the tool form below is retained for clients that do not support the prompts primitive.

### 4.1 `search_papers`
Search for scientific literature across PubMed or CrossRef.
- **Parameters**:
  - `query` (`str`, required): Search terms, keywords, or boolean query.
  - `source` (`str`, optional, default `"auto"`): `"auto"`, `"pubmed"`, or `"crossref"`.
  - `num_results` (`int`, optional, default `10`, clamped to `50`).
  - `year_start` (`int`, optional), `year_end` (`int`, optional).
  - `author` (`str`, optional), `journal` (`str`, optional).
- **Returns**: `list[PaperMetadata]` with `title`, `authors`, `year`, `venue`, `doi`, `pmid`,
  `pmcid`, `abstract`, `oa_status`.

**`source="auto"` semantics** (undefined in revision 1, now normative):
1. Query PubMed for `num_results`.
2. If PubMed returns fewer than `num_results`, top up from CrossRef.
3. Deduplicate on lowercased DOI; where a DOI is absent, fall back to a normalized-title key.
4. On conflict the PubMed record wins, because it carries richer identifiers.

**Filter mapping.** Filters are best-effort per backend and map as follows:

| Filter | PubMed (E-utilities term) | CrossRef |
|---|---|---|
| `author` | `"<name>"[Author]` | `query.author` |
| `journal` | `"<name>"[Journal]` | `query.container-title` |
| `year_start` / `year_end` | `("<start>"[PDAT] : "<end>"[PDAT])` | `filter=from-pub-date:`, `until-pub-date:` |

**`oa_status` sourcing.** Populated by a single batched Europe PMC query returning `isOpenAccess`
for the whole result page — never by a per-result Unpaywall call, which would cost up to 50 extra
HTTP requests for one search. Values are `"oa"`, `"closed"`, or `"unknown"`.

### 4.2 `get_full_text`
Retrieve full text of a paper using the prioritized waterfall pipeline.
- **Parameters**:
  - `identifier` (`str`, required): PMID (e.g. `"34567890"`), PMCID (e.g. `"PMC8765432"`),
    DOI (e.g. `"10.1038/s41586-020-2003-7"`), or Title.
  - `max_chars` (`int`, optional, default `SCHOLAR_MAX_CHARS` = `50000`): Character cap on the
    returned body.
  - `sections` (`list[str]`, optional): Return only sections whose heading matches one of these
    names (case-insensitive substring match), preserving document order. An empty selection is
    returned when nothing matches — this is not an error.
- **Returns**: `FullTextResponse` containing:
  - `status`: `"full_text"` | `"abstract_only"` | `"ambiguous_match"` | `"not_found"` | `"error"`
  - `source`: `"pmc"` | `"europepmc"` | `"unpaywall"` | `"scihub"` | `"abstract_fallback"` | `"none"`
  - `format`: `"markdown"` | `"text"`
  - `title`, `doi`, `pmid`, `pmcid`: Resolved metadata (nullable)
  - `content`: Extracted article body in Markdown, or abstract text if fallback
  - `url`: Direct link to OA source, publisher DOI, or PDF location
  - `truncated` (`bool`): Whether `content` was cut by `max_chars`
  - `total_chars` (`int`): **Pre-truncation** length, so the caller knows what it did not receive
  - `sections_available` (`list[str]`): **All** section headings in the document, regardless of
    what `sections` selected
  - `attempts` (`list[FetchAttempt]`): Per-tier trace (section 4.7)
  - `error` (`str`, nullable)

**Order of operations.** Section selection is applied first, then the `max_chars` cap, cutting on
a paragraph boundary and appending a truncation marker. Truncation is applied once, centrally in
the resolver, not inside individual providers.

### 4.3 `get_full_text_batch`
Batch check and preview full-text availability for multiple identifiers.
- **Parameters**:
  - `identifiers` (`list[str]`, required, max `25`): PMIDs, PMCIDs, DOIs, or titles.
- **Returns**: `list[FullTextSummary]` with `identifier`, `status`, `source`, `title`, `excerpt`,
  `url`. Exceeding 25 identifiers returns a structured error rather than truncating the input.

### 4.4 `get_metadata`
Resolve identifiers and return metadata and abstract **without** running the full-text waterfall.
- **Parameters**: `identifier` (`str`, required).
- **Returns**: `PaperMetadata`, or a structured error when the identifier cannot be resolved.

This tool exists so a caller that only needs an abstract or an ID mapping does not pay for up to
five full-text retrieval attempts.

### 4.5 `download_paper`
Download the original PDF to a local file inside the configured download root.
- **Parameters**:
  - `identifier` (`str`, required): PMID, PMCID, DOI, or title.
  - `output_path` (`str`, required): Path **relative to `SCHOLAR_DOWNLOAD_DIR`**
    (e.g. `"papers/paper.pdf"`).
  - `overwrite` (`bool`, optional, default `False`).
- **Returns**: `DownloadResult` (`success`, `saved_path`, `source_used`, `file_size_bytes`,
  `message`).

**Path sandboxing.** `output_path` is caller-supplied and, in an MCP context, model-generated.
It is resolved against `SCHOLAR_DOWNLOAD_DIR` and the result is checked to be inside the resolved
root. Absolute paths outside the root and `..` traversal are both rejected. An existing file is
never overwritten unless `overwrite=True`. Every rejection returns `success=False` with an
explanatory `message` — never a raised exception.

### 4.6 `deep_paper_analysis_prompt`
Generate a comprehensive multi-point analytical evaluation prompt from article metadata and
extracted content.
- **Parameters**: `identifier` (`str`, required).
- **Returns**: `dict[str, str]` with `analysis_prompt`.
- Also registered as the MCP prompt `deep_paper_analysis`.

### 4.7 Response Models

```
IdentifierMap    pmid, pmcid, doi, title, match_score, ambiguous
FetchAttempt     tier, outcome, reason, elapsed_ms
PaperMetadata    title, authors, year, venue, doi, pmid, pmcid, abstract, oa_status
FullTextResponse status, source, format, title, doi, pmid, pmcid, content, url,
                 truncated, total_chars, sections_available, attempts, error
FullTextSummary  identifier, status, source, title, excerpt, url
DownloadResult   success, saved_path, source_used, file_size_bytes, message
```

`FetchAttempt.outcome` is one of `"hit"`, `"miss"`, `"skipped"`, `"error"`, `"timeout"`. The
`reason` field carries the explanation for skips and errors — for example
`"UNPAYWALL_EMAIL not configured"` or `"PREFER_SCIHUB_OVER_UNPAYWALL"`. Without this trace,
`source` alone says which tier won but nothing about why the earlier four lost, which makes
production waterfall behavior effectively undebuggable.

### 4.8 Error Handling Contract

No tool raises into the MCP transport. Provider-level network faults are caught and reported as a
tier miss so the waterfall keeps its shape; anything escaping that is converted at the tool
boundary into `{"status": "error", "error": "<message>"}`. Argument limits (`num_results` <= 50,
`identifiers` <= 25) are enforced at the tool boundary so a malformed model call returns a clear
message rather than a stack trace.

---

## 5. Parsers & Extractors

### 5.1 JATS XML to Markdown Converter (`scholar_mcp.parsers.jats`)

Parses PMC full-text XML in document order using `BeautifulSoup(xml, "lxml-xml")`, which tolerates
the malformed and truncated XML that upstream services occasionally return. Removed elements are
decomposed **before** the tree walk, not filtered during rendering.

- **Transformations**:
  - `<article-title>` -> `# Title`
  - `<contrib>` -> `**Authors:** Author 1, Author 2...`
  - `<abstract>` -> `## Abstract\n\n...`
  - `<sec><title>...</title></sec>` -> `## Section Title` (nested depth mapped, clamped at `######`)
  - `<p>` -> normalized single-spaced paragraphs separated by blank lines
  - `<list list-type="...">` -> `- item` or `1. item`
  - `<fig>` / `<table-wrap>` -> `[Figure 1] Caption text` / `[Table 1] Caption text`
  - `<table><tr><td>...</td></tr></table>` -> Markdown table rows (`| col1 | col2 |`)
  - `<boxed-text>` -> `> blockquote` callout
  - `<inline-formula>` -> `alttext` or `[formula]` placeholder
- **Removed Elements**:
  - `<ref-list>`, `<ref>` (citations stripped to minimize token overhead)
  - `<fn-group>`, `<fn>` (footnotes)
  - `<mml:math>`, `<tex-math>` (raw MathML/TeX trees)
  - `<permissions>`, `<license>`, `<copyright-holder>` (boilerplate)
  - `<supplementary-material>`, `<related-article>`

> **Correctness note.** The output legitimately contains `>` characters, because `<boxed-text>`
> renders as a Markdown blockquote. A conformance check for leftover markup must therefore match
> an XML-tag pattern (`</?tag ...>`), not the bare `<` and `>` characters. Revision 1's testing
> strategy specified the latter, which contradicted this section.

### 5.2 Section Navigation (`scholar_mcp.parsers.jats`)

Two helpers back the `sections` parameter of `get_full_text`:

- `list_sections(markdown) -> list[str]` — heading names present in the rendered Markdown.
- `select_sections(markdown, wanted) -> str` — case-insensitive substring match on headings,
  preserving document order.

Both operate on rendered Markdown rather than on the XML tree, so they apply equally to
PDF-derived text once headings are detected.

### 5.3 In-Memory PDF Text Extractor (`scholar_mcp.parsers.pdf`)

- Extracts raw text using `pypdf.PdfReader` over `io.BytesIO` streams (no disk I/O).
- Returns `""` rather than raising on corrupt, empty, or encrypted input.
- Post-processing is split into independently testable helpers:
  - `_strip_repeated_lines(pages)` — drops lines appearing on a majority of pages (running
    headers and footers).
  - `_postprocess(text)` — rejoins hyphenated line breaks (`infor-\nmation` -> `information`) and
    collapses runs of whitespace and blank lines.
- Output shorter than `MIN_USEFUL_CHARS` is treated as a tier miss (section 3.4).

---

## 6. Configuration & Environment Variables

### 6.1 Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `PUBMED_API_KEY` | `str` | `None` | NCBI API key. Also raises the enforced E-utilities rate limit from 3 to 10 req/s |
| `PUBMED_EMAIL` | `str` | `None` | Contact email for NCBI E-utilities; used as the `UNPAYWALL_EMAIL` fallback |
| `PUBMED_TOOL` | `str` | `"ScholarMCP"` | `tool` parameter sent to NCBI E-utilities |
| `UNPAYWALL_EMAIL` | `str` | `None` (falls back to `PUBMED_EMAIL`) | Required by the Unpaywall API. When neither is set, the Unpaywall tier is skipped |
| `ENABLE_SCIHUB` | `bool` | `True` | Master switch for the Sci-Hub tier. When false, the tier never runs (section 3.3) |
| `PREFER_SCIHUB_OVER_UNPAYWALL` | `bool` | `False` | Reordering preference: skip Unpaywall and go straight to Sci-Hub. Has no effect when `ENABLE_SCIHUB` is false. **Renamed from `FORCE_SCIHUB`** |
| `SCIHUB_MIRRORS` | `str` | `"https://sci-hub.hkvisa.net,https://sci-hub.mksa.top,https://sci-hub.ren,https://sci-hub.se,https://sci-hub.st,https://sci-hub.ee"` | Comma-separated list of mirrors, tried in order |
| `SCHOLAR_REQUEST_TIMEOUT` | `int` | `30` | Per-HTTP-request timeout in seconds |
| `SCHOLAR_TOTAL_BUDGET` | `int` | `45` | Total wall-clock budget for one `get_full_text` call (section 3.5) |
| `SCHOLAR_MAX_CONCURRENCY` | `int` | `5` | Semaphore bound for batch fan-out |
| `SCHOLAR_MAX_CHARS` | `int` | `50000` | Default character cap on returned full text |
| `SCHOLAR_TITLE_MATCH_THRESHOLD` | `float` | `80.0` | Minimum CrossRef score to accept a title -> DOI match (section 3.1) |
| `SCHOLAR_DOWNLOAD_DIR` | `path` | `"./downloads"` | Root directory for `download_paper`; writes outside it are rejected |
| `SCHOLAR_CACHE_SIZE` | `int` | `500` | LRU capacity for identifier and metadata entries |
| `SCHOLAR_CACHE_TTL` | `int` | `3600` | Cache entry lifetime in seconds |

### 6.2 Caching Policy

The cache is a TTL-bounded LRU holding **identifier maps and paper metadata only**. Full-text
bodies and PDF bytes are explicitly **not** cached: 500 cached articles would be hundreds of
megabytes resident, and the cost being avoided is one HTTP request, not a computation. This makes
the memory ceiling a function of entry count rather than of article length.

---

## 7. Package & Project Layout

```
scholar-mcp/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── CHANGELOG.md
├── LICENSE
├── .github/workflows/{ci.yml,publish.yml}
├── docs/
│   └── superpowers/
│       ├── specs/2026-08-28-scholar-mcp-unified-design.md
│       └── plans/2026-08-28-scholar-mcp-unified-plan.md
├── src/
│   └── scholar_mcp/
│       ├── __init__.py
│       ├── server.py              # FastMCP application, tool & prompt registrations
│       ├── config.py              # Settings & env var validation
│       ├── models.py              # Data models (section 4.7)
│       ├── resolver.py            # Waterfall coordinator, truncation, download sandbox
│       ├── identifiers.py         # PMID <-> PMCID <-> DOI conversion, title thresholding
│       ├── providers/
│       │   ├── __init__.py
│       │   ├── base.py            # BaseProvider interface, MIN_USEFUL_CHARS
│       │   ├── pmc.py             # PMC JATS XML provider
│       │   ├── europe_pmc.py      # Europe PMC provider + batched annotate_oa_status
│       │   ├── unpaywall.py       # Unpaywall API provider
│       │   ├── scihub.py          # Sci-Hub scraper & mirror rotator
│       │   ├── pubmed.py          # PubMed E-utilities search & metadata
│       │   └── crossref.py        # CrossRef API search & metadata
│       ├── parsers/
│       │   ├── __init__.py
│       │   ├── jats.py            # JATS XML -> Markdown, section navigation
│       │   └── pdf.py             # PDF text extraction
│       └── utils/
│           ├── __init__.py
│           ├── http.py            # AsyncHttpClient: backoff, retry, NCBI credentials
│           ├── rate_limit.py      # AsyncRateLimiter token bucket (per host)
│           └── cache.py           # TTLCache (LRU + expiry)
└── tests/
    ├── test_config_models.py
    ├── test_http_cache.py
    ├── test_jats_parser.py
    ├── test_pdf_parser.py
    ├── test_identifiers.py
    ├── test_oa_providers.py
    ├── test_search_scihub_providers.py
    ├── test_waterfall_resolver.py
    └── test_server_tools.py
```

### 7.1 Dependencies

| Package | Purpose |
|---|---|
| `fastmcp>=3.0.0` | MCP server framework |
| `httpx>=0.27.0` | Async HTTP client (replaces `requests` + `urllib3`) |
| `beautifulsoup4>=4.12.0` | JATS XML and Sci-Hub HTML parsing |
| `lxml>=5.0.0` | `lxml-xml` backend for tolerant XML parsing |
| `pypdf>=5.0.0` | In-memory PDF text extraction |

Dev extra: `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`, `respx>=0.21.0`.

`requests` and `urllib3` are removed. Revision 1 named neither `pypdf`, `lxml`, nor `httpx`
despite depending on all three.

---

## 8. Testing Strategy

All tests use `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"`). HTTP is mocked with
`respx` against the shared `httpx.AsyncClient`; **no test performs live network I/O.**

1. **Config & models:** flag defaults; `ENABLE_SCIHUB=false` beating the preference flag; NCBI
   rate limit derived from API key presence; serialization round-trips.
2. **HTTP, rate limiting, cache:** LRU eviction; TTL expiry against an injected clock; token
   bucket throttling; NCBI credential injection applied to NCBI hosts only; retry-then-succeed;
   `None` after retries are exhausted.
3. **JATS converter:** headings, authors, abstract, nested section depth, lists, figure and table
   captions, blockquote callouts; references, footnotes, and licence boilerplate absent; no
   residual XML tags (matched by tag regex, not bare `<`/`>` — section 5.1); malformed XML
   tolerated; `list_sections` / `select_sections` behavior including a non-matching selection.
4. **PDF parser:** single-page and multi-page extraction; corrupt and empty input return `""`;
   de-hyphenation and whitespace collapse; repeated header/footer removal.
5. **Identifier conversion:** type detection for every input form; PMID -> DOI/PMCID via idconv;
   title match above threshold accepted; **title match below threshold returns `ambiguous`
   with no DOI**; results cached (second call issues no request); upstream failure preserves the
   caller's input.
6. **OA providers:** PMC hit; missing PMCID is a miss; metadata-only stub is a miss; upstream 500
   is a miss rather than a raise; Europe PMC hit; **Unpaywall skipped with zero HTTP calls when
   no email is configured**; closed-access is a miss; a PDF extracting to whitespace is a miss.
7. **Discovery & Sci-Hub providers:** PubMed query builder applies every filter; PubMed and
   CrossRef search shapes; `oa_status` annotated for a whole page in **one** request; Sci-Hub
   mirror rotation on failure; all mirrors down is a miss; no DOI is a miss.
8. **Waterfall resolver:** termination at each of steps 1-4; `PREFER_SCIHUB_OVER_UNPAYWALL`
   skipping Unpaywall; `ENABLE_SCIHUB=false` beating that preference; total failure to abstract
   fallback; nothing at all to `not_found` with five recorded attempts; ambiguous title fetching
   nothing; truncation setting `truncated` and pre-truncation `total_chars`; section selection;
   budget expiry degrading to abstract; batch concurrency and the 25-identifier limit; download
   sandbox — `..` escape, absolute path outside root, overwrite refusal, successful nested write.
9. **Server tools:** each tool's success path; `max_chars` and `sections` forwarded;
   `num_results` clamped; `get_metadata` not invoking the waterfall; batch over-limit rejected;
   exceptions converted to structured errors; all six tools registered.

CI (`.github/workflows/ci.yml`) runs `pytest` across Python 3.10-3.12 in addition to the existing
install-and-import check.

---

## 9. Security & Compliance Notes

- **Filesystem.** `download_paper` is the only tool that writes to disk, and it writes only inside
  `SCHOLAR_DOWNLOAD_DIR` (section 4.5). Path arguments in an MCP server are model-generated and
  are treated as untrusted input.
- **Credentials.** `PUBMED_API_KEY` is injected as a query parameter for NCBI hosts only, never
  appended to third-party requests.
- **Rate limits.** The NCBI ceiling is enforced client-side (section 3.6) rather than assumed.
- **Sci-Hub.** The tier is opt-out via `ENABLE_SCIHUB`, and `oa_status` plus the `attempts` trace
  let a caller see which source served any given result. Operators who need OA-only behavior set
  `ENABLE_SCIHUB=false`, which leaves all three legal tiers active (section 3.3).

---

## 10. Migration from `scihub-mcp`

The package is renamed outright. There is no compatibility shim and `src/scihub_mcp/` is deleted.

| Item | Before | After |
|---|---|---|
| PyPI project | `scihub-mcp` | `scholar-mcp` (**new project**; a new Trusted Publisher must be configured before the first publish) |
| Python module | `scihub_mcp` | `scholar_mcp` |
| Console script | `scihub-mcp` | `scholar-mcp` |
| Version | `0.4.0` | `1.0.0` |
| Env var | `FORCE_SCIHUB` | `PREFER_SCIHUB_OVER_UNPAYWALL` |
| Tools | `search_scihub_by_doi`, `search_scihub_by_title`, `search_scihub_by_keyword`, `download_scihub_pdf` | `search_papers`, `get_full_text`, `get_full_text_batch`, `get_metadata`, `download_paper`, `deep_paper_analysis_prompt` |

**Breaking for existing users:** every `mcpServers` entry pointing at the old script or module
path stops working. `README.md` must show the old and new configuration blocks side by side, and
`CHANGELOG.md` must record the rename and the env var change under `1.0.0`.

---

## 11. Revision History

### Revision 2 (2026-08-28) — post-review amendments

Four design decisions resolved:

| # | Decision |
|---|---|
| D1 | Full text is bounded. `get_full_text` gains `max_chars` and `sections`; responses gain `truncated`, `total_chars`, `sections_available` (sections 4.2, 5.2). |
| D2 | All network I/O is async on `httpx`, with a total time budget, bounded batch fan-out, and per-host rate limiting (sections 3.5, 3.6). |
| D3 | `download_paper` writes only inside `SCHOLAR_DOWNLOAD_DIR` (section 4.5). |
| D4 | Hard rename to `scholar-mcp` with no shim (section 10). |

Twelve gaps closed:

1. `FORCE_SCIHUB` renamed and its interaction with `ENABLE_SCIHUB` made normative (section 3.3).
2. Unpaywall behavior when no email is configured is now defined as a recorded skip (sections 3.2, 6.1).
3. NCBI rate limiting given an owning component (section 3.6).
4. Title -> DOI matching given a confidence threshold and an `ambiguous_match` status (section 3.1).
5. `source="auto"` semantics defined, with a per-backend filter mapping table (section 4.1).
6. `oa_status` sourced from one batched Europe PMC query instead of per-result Unpaywall calls (section 4.1).
7. `FetchAttempt` trace added so tier outcomes are observable (section 4.7).
8. `get_metadata` added so a metadata-only caller does not pay for the waterfall (section 4.4).
9. Caching policy stated: identifiers and metadata only, never bodies (section 6.2).
10. `httpx`, `lxml`, and `pypdf` added to the declared dependency set (section 7.1).
11. Testing strategy names `respx`, and covers `search_papers` and `download_paper` (section 8).
12. The "no residual XML tags" check corrected — it contradicted the `<boxed-text>` -> `>`
    blockquote mapping in the same document (section 5.1).

Additionally introduced: `MIN_USEFUL_CHARS`, so a metadata-only PMC stub or a scanned image PDF
counts as a tier miss rather than short-circuiting the waterfall with an empty body (section 3.4).

---

## 12. Open Items

1. **GitHub repository rename.** The Python package and PyPI project are renamed, but the repo is
   still `w8s/scihub-mcp` and `[project.urls]` points there. Rename the repo or accept the
   mismatch — GitHub redirects old URLs, so either works.
2. **Sci-Hub default.** `ENABLE_SCIHUB` defaults to `true`, inherited from the current project.
   Under a neutral `scholar-mcp` name that default is a more visible stance than it was under
   `scihub-mcp`. Flipping it is a one-line change.
3. **`PubMed-MCP-Server/` provenance.** Currently untracked vendored reference code with an
   unconfirmed licence. Confirm before porting logic from it, and record attribution in
   `README.md` alongside the existing CyberKrypton credit.
4. **Europe PMC vs PMC ordering.** Both tiers largely serve the same corpus and Europe PMC is less
   rate-limited than NCBI E-utilities. If PMC hit rates prove low in the smoke test, swapping
   steps 1 and 2 is a cheap win.
