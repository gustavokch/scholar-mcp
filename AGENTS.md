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
├── query_relax.py        # Shared PubMed query-relaxation ladder (full → 5 → 4 → 3) + content-overlap scoring; no medical content, used by resolver.py and providers/pubmed.py as well as medical/
├── data/
│   ├── scimago_sjr.json  # SCImago SJR 2025 journal-impact table (non-commercial terms; see SOURCES.md)
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
│   ├── passages.py       # Shared passage/offset full-text serving for stored bodies
│   ├── ranking.py        # Medical article ranking
│   ├── rxnorm.py         # RxNorm drug nomenclature
│   ├── who.py            # WHO GHO health statistics client
│   ├── who_iris.py       # WHO IRIS publication repository search and full-text retrieval
│   ├── brazil_moh.py     # Brazilian MoH publications via BVS/iAHx search + PDF full text
│   ├── govbr_common.py   # Shared gov.br (Plone) parsers, scoring, headers
│   ├── govbr_pcdt.py     # Scraper for Brazilian MoH PCDT guidelines
│   └── govbr_az.py       # Saúde de A a Z publication trees (SVSA, guias-e-manuais)
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

1. **Async-first on `httpx`** — All network I/O is asynchronous using a single shared `httpx.AsyncClient` inside `AsyncHttpClient`. No `requests` or `urllib3` are used. `asyncio.to_thread` is permitted in exactly two places: saving downloaded PDFs to local disk in `WaterfallResolver.download_article`, and parsing a fetched PDF in `BrazilMoHEngine._extract_pdf_text`. The second exists because CPU-bound `pypdf` extraction cannot be made cancellable, or kept off the loop's critical path, any other way — inline it stalls every in-flight call, and the caller's ceiling cannot interrupt it. Its cost is bounded by `brazil_pdf_max_bytes` (checked before the parser is invoked), since an abandoned parse thread keeps burning CPU until `pypdf` returns.
2. **6-Tier Waterfall Resolver** — The order is:
   - Tier 1: Europe PMC (JATS XML -> Markdown)
   - Tier 2: PMC (JATS XML -> Markdown)
   - Tier 3: Unpaywall (Legal OA PDF -> Text) — skipped when `PREFER_SCIHUB_OVER_UNPAYWALL` and `ENABLE_SCIHUB` are both set
   - Tier 4: arXiv (Preprint PDF -> Text, automatic for arXiv DOIs `10.48550/arXiv.*` and arXiv IDs) — the provider self-skips when no arXiv ID is known
   - Tier 5: Sci-Hub (Mirror-rotated PDF -> Text with Camoufox anti-detection fallback) — skipped when `ENABLE_SCIHUB` is false
   - Tier 6: Abstract Fallback (PubMed / CrossRef metadata)

   Skipped tiers are still recorded in the response as `FetchAttempt(outcome="skipped")` with the reason, so the order above describes the plan, not a guarantee that every tier runs.
3. **Caching Policy** — Identifier maps and paper metadata are cached in `TTLCache`. Full-text bodies and raw PDF bytes in the core waterfall are **never cached** to keep memory consumption bounded. The medical subsystem uses persistent `SQLiteCacheManager` with source-specific TTLs (FDA 24h, PubMed 1h, WHO GHO 7d, RxNorm 30d, Guidelines 7d, AAP Bright Futures 30d, AAP Policy 7d, Pediatric Journals 1h, Child Health 7d, Pediatric Drugs 24h, Clinical Trials 24h, WHO IRIS 30d, Brazil MoH 30d). The Brazil MoH engine additionally writes one `brazil_moh_record:{schema}:{record_id}` row per merged record at search time (standard 30-day TTL; each row is individually complete, so it never takes the degraded short TTL), which `get_full_text` consults before re-paying a live Solr `id:"..."` lookup.
4. **Resilience and Error Boundaries** — Providers never raise on network failure or unexpected payloads; they report a miss/skip and allow the waterfall to degrade smoothly. The same boundary applies to the ranking enrichment stage (time-bounded by `RANKING_ENRICHMENT_TIMEOUT`) and to `check_citations` (per-claim failure isolation).
5. **Download Sandbox** — `download_paper` enforces that paths resolve within `SCHOLAR_DOWNLOAD_DIR` and rejects path traversal.
6. **Query-aware re-ranking** — `search_papers` re-ranks the candidate pool with six Z-score-standardized signals (relevance, citations, recency, evidence grade, journal impact, author authority). The relevance signal blends lexical coverage of the query against title/abstract with a `1/sqrt(rank+1)` source-position prior. `ScoringEngine` exposes `tokenize`, `text_coverage`, and `best_matching_sentence` as shared primitives reused by `medical/ranking.py` and `citation_check.py`. Journal-impact data is loaded from `src/scholar_mcp/data/scimago_sjr.json` (SCImago SJR 2025; terms and provenance in `src/scholar_mcp/data/SOURCES.md`).
7. **Browser Scraping Fallback via Camoufox** — When HTTP requests to bot-protected sources (Sci-Hub mirrors, AAP Bright Futures, AAP Policy) hit Cloudflare or 403 blocks, a headless anti-detection Firefox browser (`camoufox`) is invoked as a last-resort fallback. The Cochrane Library is deliberately **not** in that list: its HTML site blocks headless browsers as well, so `MedicalDatabasesEngine._search_cochrane` queries the Europe PMC REST API and tags the mirrored systematic reviews as Cochrane records. Sci-Hub browser fallback is capped to 3 mirrors and 20s total timeout. The pediatrics browser fallback runs only when `ENABLE_BROWSER_FALLBACK` is true (default true; the legacy alias `ENABLE_PLAYWRIGHT_FALLBACK` is still honored) and only after the plain HTTP path returns nothing. If `camoufox` is not installed, the subsystem degrades gracefully with an `ImportError` boundary.
8. **Medical Intelligence Subsystem** — Standalone tools backed by openFDA, RxNav, WHO GHO, ClinicalTrials.gov (with a 10-term Essie parser cap to avoid HTTP 400 errors), PubMed (with query relaxation ladder and partial keyword credit for guideline searches), WHO IRIS (DSpace 7 REST API with direct PDF resolution and full-text extraction), and Brazilian Ministry of Health technical publications. MoH guidelines support collections: `all` (default merged search), `brisa` (BVS/iAHx API with country filtering), `pcdt` (Clinical Protocols and Therapeutic Guidelines scraped from gov.br), and `az` (Saúde de A a Z surveillance manuals and guides from SVSA and guias-e-manuais trees). The bundled PCDT and A-Z catalogs (`src/scholar_mcp/data/govbr_pcdt_catalog.json`, `govbr_az_catalog.json`) are the catalogs the server searches; no search crawls gov.br. Regenerate them offline with `scripts/update_govbr_catalogs.py --catalog pcdt|az`, which refuses to write a partial crawl. `src/scholar_mcp/data/brazil_moh_extended_catalog.json` is a second bundled catalog, merged over the PCDT seed catalog by `govbr_pcdt.load_extended_catalog`/`_merge_extended`; its records point at an offline full-text corpus under `src/scholar_mcp/data/guidelines/*.txt`, served straight from disk (no network, no PDF parsing) by `BrazilMoHEngine._serve_local_text`.
9. **HTTP Client Resilience and Redaction** — `AsyncHttpClient` folds caller-supplied `params` into the URL before credential injection (`api_key`, `email`, `tool`) to ensure query parameters are not overwritten by `httpx`. Diagnostic logs capture `>= 400` errors, retries, and timeouts while automatically redacting sensitive credentials. Response body decoding on errors is bounded to 500 characters with `errors="replace"`. Callers may narrow the retry set per request via `get(..., retryable_statuses=...)`; endpoints that return a status that is retryable for other endpoints but permanent here use this to fail fast: Europe PMC `fullTextXML` answers 500 when no OA XML exists, and the Brazilian MoH BVS search (`_BVS_RETRYABLE_STATUSES`, `medical/brazil_moh.py`) retries only 429, since a 5xx there is an origin outage and retrying it with backoff would burn the stage budget the remaining search stages need. A `Retry-After` of up to `MAX_RETRY_AFTER` (60 s) is waited out and retried. A longer one (OpenAlex answers 660 s once its keyless daily budget is spent) cannot be outlasted within a call, so `get` does not retry: it reports the real status and short-circuits the host process-wide (`FetchFailure(status, "RateLimitedCached")`) until the stated time, capped at `MAX_RATE_LIMITED_HOST_S`. This applies to any retryable status that carries such a header (a 503 maintenance window short-circuits the host as well, and BVS classifies it `origin_outage`). The key is the grouped `_host_key` bucket, so one E-utilities 429 also short-circuits PMC and PubMed. `is_throttled(host)` reports a short-circuited host as throttled.
10. **Brazilian MoH Engine: Error Taxonomy and Timeout Budgets** — `BrazilMoHEngine` classifies every BVS search outcome into a `BvsErrorKind` (`utils/sqlite_cache.py`: `ok`, `successful_empty`, `cdn_challenge`, `origin_outage`, `timeout`, `backend_error`) that downstream callers use to decide whether a miss counts against a breaker; `origin_outage` never does. `origin_outage` covers an unreachable host as well as 5xx: connect-phase failures (`ConnectTimeout`/`ConnectError`, named in `FetchFailure.detail`) mean the origin is not serving, so a dead third-party document host is not charged against the breaker as a `timeout`. The engine also splits its timeout budget in two rather than using one blanket value: `brazil_stage_timeout_s`/`brazil_chain_timeout_s` bound `search_brazil_moh_guidelines` (per-stage and whole-chain, including the camoufox tier), while `brazil_fulltext_timeout_s` separately bounds `get_brazil_moh_full_text`'s network work (record lookup plus PDF fetch) — the two calls have different cost profiles and must not share a ceiling. Within any of these budgets the connect phase is bounded client-wide by `connect_timeout_s` (default 5.0 s; healthy hosts measure 0.01–0.18 s), because a scalar request timeout lets one hopeless connect consume the whole ceiling — the retry ladder, not a longer connect, is the right response to a slow host. Every `get_full_text` payload carries `timeout_phase` (`"lookup"`/`"fetch"`/`None`) naming the phase a failure is attributed to; it is additive attribution, not a taxonomy value.
11. **Brazilian MoH Camoufox Tier Is Stateless Per Call** — `BrazilMoHEngine._camoufox_search` (`medical/brazil_moh.py:1421`) opens `async with AsyncCamoufox(headless=True)` fresh on every invocation of the last-resort browser fallback; no browser instance, context, or cookie state is reused or carried across stages or calls. This is the current deliberate state, not an oversight.
12. **BVS Search Has No Ordering Parameter** — `BrazilMoHEngine._fetch_records` sends only `q`, `output`, and `count` to the BVS search endpoint; there is no date-sort or relevance-sort parameter on the request. Recency is applied entirely rank-side, after retrieval, by `medical/ranking.py` (`RECENCY_WEIGHT = 0.3`, 7-year half-life, applied inside `_rank_records`). A future reader should not assume BVS results arrive date-sorted.
13. **PMID Enrichment Is Out of Scope for Brazilian MoH Records** — `BrazilMoHEngine` resolves DOI from a record's link list when present (`_extract_doi`) but never attempts PMID resolution. This is a deliberate scope decision, not an oversight: BVS non-conventional literature (PCDT, Cadernos de Atenção Básica, ministry technical reports) is grey literature that is largely not PubMed-indexed.

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

<!-- graft:start -->
## Graft — repo context graph

This repo is indexed in `graft/`: small linked markdown nodes that explain each
system and carry exact file:line spans, kept in sync with the code through git.

For ANY task here — understanding how something works, finding where code lives,
or scoping a change — get context from the graph before grepping or opening
source files. Re-ask freely (it's cheap) and reuse literal identifiers you
already have (symbol, error string, file name) as the query. New to this repo?
Run `graft map` first — a token-budgeted orientation (dir clusters, hubs,
hotspots), no LLM, no key.

- Run `graft ask "<your question>" --source` → ranked nodes with the relevant
  code spans inlined (each hit's ≤8-line crux by default; `--full` for whole
  definitions when the crux isn't enough). Match the tool to the task shape:
  for understanding or editing, the top node IS the answer — cite its
  `covers:` file:line spans and edit straight from `--source`. For
  exhaustive tasks ("every occurrence / every caller of this pattern"), ranked
  results are top-N, not complete — run `graft grep "<literal>"` instead
  (exhaustive over indexed files, grouped by enclosing symbol), falling back
  to raw `grep -rn` only for unindexed files.
- `graft skeleton <file>` → every definition's signature + span, ~10× cheaper
  than reading the file; use it to skim an API surface.
- `graft callers <symbol>` gives precomputed, exact edges — who calls this.
  Add `--direction out` for what it calls, or `--depth N` to walk
  transitively for the full blast radius. For structural questions, skip
  ranking and use this directly.
- Or browse: `graft/INDEX.md` lists every node; follow the links.
- Monorepos and folders of multiple repos rank fairly across sub-projects —
  hits carry `[scope/]` labels naming which one they're from. Narrow with
  `graft ask "<task>" --in <scope>/` once you know where you're working.

If a returned span is truncated ("+N more lines"), open the file at that exact
range before finalizing. Only open source files when a node genuinely lacks a
needed detail, and then at the exact file:line the node points to — never
re-read whole files.

After big code changes, refresh the graph with `graft build` (deterministic,
no API key, $0).
<!-- graft:end -->
