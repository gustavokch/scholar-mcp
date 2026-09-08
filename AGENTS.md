# AGENTS.md — scholar-mcp Developer Guide

Developer-facing architecture and development reference. For user configuration, see [README.md](README.md).

## Architecture

```
src/scholar_mcp/
├── __init__.py           # Package version (1.0.0)
├── config.py             # Settings dataclass, env loader, defaults
├── models.py             # Domain models (PaperMetadata, FullTextResponse, IdentifierMap, FetchAttempt, etc.)
├── identifiers.py        # Identifier cleaner, cross-service resolution (PMID <-> PMCID <-> DOI), title thresholding
├── resolver.py           # Multi-tier waterfall coordinator, batch concurrency, download sandbox
├── server.py             # FastMCP server tool and prompt definitions
├── citation_check.py     # Claim-to-source grounding checker (check_citations MCP tool)
├── ranking.py            # ScoringEngine + RankingPipeline: query-aware re-ranking, Z-scoring, evidence/impact/authority signals
├── data/
│   ├── scimago_sjr.json  # Journal-impact lookup table for the ranking signal (ships empty)
│   └── SOURCES.md        # Procedure for populating scimago_sjr.json
├── medical/
│   ├── __init__.py
│   ├── clinical_trials.py # ClinicalTrials.gov search and metadata lookup
│   ├── databases.py      # Cross-database medical search and deduplication
│   ├── fda.py            # openFDA drug search and pediatric drug lookups
│   ├── formatters.py     # Medical response formatting helpers
│   ├── guidelines.py     # Clinical practice guidelines engine
│   ├── models.py         # Medical data models
│   ├── pediatrics.py     # Pediatric literature and guidelines engine
│   ├── pubmed.py         # Dedicated medical PubMed client
│   ├── ranking.py        # Medical article ranking
│   ├── rxnorm.py         # RxNorm drug nomenclature
│   ├── who.py            # WHO GHO health statistics client
│   ├── who_iris.py       # WHO IRIS publication repository search and full-text retrieval
│   └── brazil_moh.py     # Brazilian MoH publications via BVS/iAHx search + PDF full text
├── parsers/
│   ├── __init__.py
│   ├── jats.py           # JATS XML to clean Markdown parser and section extractor
│   └── pdf.py            # In-memory PDF text extraction, dehyphenation, running header/footer removal
├── providers/
│   ├── __init__.py
│   ├── arxiv.py          # arXiv preprint search and PDF full-text extraction
│   ├── base.py           # BaseProvider ABC with MIN_USEFUL_CHARS threshold
│   ├── crossref.py       # CrossRef bibliographic search and metadata lookup
│   ├── europe_pmc.py     # Europe PMC JATS XML full text and batched OA annotation
│   ├── openalex.py       # OpenAlex metadata enrichment, citations fallback, author h-index
│   ├── pmc.py            # PubMed Central NCBI E-utilities XML provider
│   ├── pubmed.py         # PubMed E-utilities search and abstract retrieval
│   ├── scihub.py         # Sci-Hub multi-mirror scraper, PDF text extractor, Camoufox fallback
│   ├── semantic_scholar.py # Semantic Scholar paper search and recommendations
│   └── unpaywall.py      # Unpaywall open-access PDF extractor
└── utils/
    ├── __init__.py
    ├── cache.py          # LRU TTLCache with async locks
    ├── deduplication.py  # Fuzzy and normalized deduplication for medical search
    ├── http.py           # AsyncHttpClient with per-host rate limiting, retries, exponential backoff, logging
    ├── rate_limit.py     # AsyncRateLimiter token bucket
    ├── sqlite_cache.py   # Persistent SQLite cache for medical intelligence subsystem
    └── text.py           # Text processing utilities, dehyphenation, tokenization, truncation
```

## Key Architectural Decisions

1. **Async-first on `httpx`** — All network I/O is asynchronous using a single shared `httpx.AsyncClient` inside `AsyncHttpClient`. No `requests` or `urllib3` are used. `asyncio.to_thread` is permitted in exactly one place: saving downloaded PDFs to local disk in `WaterfallResolver.download_article`.
2. **6-Tier Waterfall Resolver** — The order is:
   - Tier 1: Europe PMC (JATS XML -> Markdown)
   - Tier 2: PMC (JATS XML -> Markdown)
   - Tier 3: Unpaywall (Legal OA PDF -> Text) — skipped when `PREFER_SCIHUB_OVER_UNPAYWALL` and `ENABLE_SCIHUB` are both set
   - Tier 4: arXiv (Preprint PDF -> Text, automatic for arXiv DOIs `10.48550/arXiv.*` and arXiv IDs) — the provider self-skips when no arXiv ID is known
   - Tier 5: Sci-Hub (Mirror-rotated PDF -> Text with Camoufox anti-detection fallback) — skipped when `ENABLE_SCIHUB` is false
   - Tier 6: Abstract Fallback (PubMed / CrossRef metadata)

   Skipped tiers are still recorded in the response as `FetchAttempt(outcome="skipped")` with the reason, so the order above describes the plan, not a guarantee that every tier runs.
3. **Caching Policy** — Identifier maps and paper metadata are cached in `TTLCache`. Full-text bodies and raw PDF bytes in the core waterfall are **never cached** to keep memory consumption bounded. The medical subsystem uses persistent `SQLiteCacheManager` with source-specific TTLs (FDA 24h, PubMed 1h, WHO GHO 7d, RxNorm 30d, Guidelines 7d, AAP Bright Futures 30d, AAP Policy 7d, Pediatric Journals 1h, Child Health 7d, Pediatric Drugs 24h, Clinical Trials 24h, WHO IRIS 30d, Brazil MoH 30d).
4. **Resilience and Error Boundaries** — Providers never raise on network failure or unexpected payloads; they report a miss/skip and allow the waterfall to degrade smoothly. The same boundary applies to the ranking enrichment stage (time-bounded by `RANKING_ENRICHMENT_TIMEOUT`) and to `check_citations` (per-claim failure isolation).
5. **Download Sandbox** — `download_paper` enforces that paths resolve within `SCHOLAR_DOWNLOAD_DIR` and rejects path traversal.
6. **Query-aware re-ranking** — `search_papers` re-ranks the candidate pool with six Z-score-standardized signals (relevance, citations, recency, evidence grade, journal impact, author authority). The relevance signal blends lexical coverage of the query against title/abstract with a `1/sqrt(rank+1)` source-position prior. `ScoringEngine` exposes `tokenize`, `text_coverage`, and `best_matching_sentence` as shared primitives reused by `medical/ranking.py` and `citation_check.py`. Journal-impact data is loaded from `src/scholar_mcp/data/scimago_sjr.json` (ships empty; see `src/scholar_mcp/data/SOURCES.md`).
7. **Browser Scraping Fallback via Camoufox** — When HTTP requests to bot-protected sources (Sci-Hub mirrors, AAP Bright Futures, AAP Policy) hit Cloudflare or 403 blocks, a headless anti-detection Firefox browser (`camoufox`) is invoked as a last-resort fallback. The Cochrane Library is deliberately **not** in that list: its HTML site blocks headless browsers as well, so `MedicalDatabasesEngine._search_cochrane` queries the Europe PMC REST API and tags the mirrored systematic reviews as Cochrane records. Sci-Hub browser fallback is capped to 3 mirrors and 20s total timeout. The pediatrics browser fallback runs only when `ENABLE_BROWSER_FALLBACK` is true (default true; the legacy alias `ENABLE_PLAYWRIGHT_FALLBACK` is still honored) and only after the plain HTTP path returns nothing. If `camoufox` is not installed, the subsystem degrades gracefully with an `ImportError` boundary.
8. **Medical Intelligence Subsystem** — Standalone tools backed by openFDA, RxNav, WHO GHO, ClinicalTrials.gov (with a 10-term Essie parser cap to avoid HTTP 400 errors), PubMed (with query relaxation ladder and partial keyword credit for guideline searches), WHO IRIS (DSpace 7 REST API with direct PDF resolution and full-text extraction), and Brazilian Ministry of Health technical publications (BVS/iAHx API with country filtering, exact-id deduplication, and allowlisted PDF retrieval).
9. **HTTP Client Resilience and Redaction** — `AsyncHttpClient` folds caller-supplied `params` into the URL before credential injection (`api_key`, `email`, `tool`) to ensure query parameters are not overwritten by `httpx`. Diagnostic logs capture `>= 400` errors, retries, and timeouts while automatically redacting sensitive credentials. Response body decoding on errors is bounded to 500 characters with `errors="replace"`.

## Local Development & Testing

```bash
uv venv --python 3.10
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Run test suite:
```bash
pytest -v
```

Verify server entrypoint:
```bash
python -c "from scholar_mcp.server import main; print('Import OK')"
```
