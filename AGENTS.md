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
├── parsers/
│   ├── __init__.py
│   ├── jats.py           # JATS XML to clean Markdown parser and section extractor
│   └── pdf.py            # In-memory PDF text extraction, dehyphenation, running header/footer removal
├── providers/
│   ├── __init__.py
│   ├── base.py           # BaseProvider ABC with MIN_USEFUL_CHARS threshold
│   ├── europe_pmc.py     # Europe PMC JATS XML full text and batched OA annotation
│   ├── pmc.py            # PubMed Central NCBI E-utilities XML provider
│   ├── unpaywall.py      # Unpaywall open-access PDF extractor
│   ├── scihub.py         # Sci-Hub multi-mirror scraper and PDF text extractor
│   ├── pubmed.py         # PubMed E-utilities search and abstract retrieval
│   └── crossref.py       # CrossRef bibliographic search and metadata lookup
└── utils/
    ├── __init__.py
    ├── cache.py          # LRU TTLCache with async locks
    ├── http.py           # AsyncHttpClient with per-host rate limiting, retries, exponential backoff
    └── rate_limit.py     # AsyncRateLimiter token bucket
```

## Key Architectural Decisions

1. **Async-first on `httpx`** — All network I/O is asynchronous using a single shared `httpx.AsyncClient` inside `AsyncHttpClient`. No `requests` or `urllib3` are used. `asyncio.to_thread` is permitted in exactly one place: saving downloaded PDFs to local disk in `WaterfallResolver.download_article`.
2. **5-Tier Waterfall Resolver** — The order is:
   - Tier 1: Europe PMC (JATS XML -> Markdown)
   - Tier 2: PMC (JATS XML -> Markdown)
   - Tier 3: Unpaywall (Legal OA PDF -> Text)
   - Tier 4: Sci-Hub (Mirror-rotated PDF -> Text)
   - Tier 5: Abstract Fallback (PubMed / CrossRef metadata)
3. **Caching Policy** — Identifier maps and paper metadata are cached in `TTLCache`. Full-text bodies and raw PDF bytes are **never cached** to keep memory consumption bounded.
4. **Resilience and Error Boundaries** — Providers never raise on network failure or unexpected payloads; they report a miss/skip and allow the waterfall to degrade smoothly.
5. **Download Sandbox** — `download_paper` enforces that paths resolve within `SCHOLAR_DOWNLOAD_DIR` and rejects path traversal.

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
