# Design Specification: Porting Medical MCP to Python and Integrating into Scholar MCP

**Date**: 2026-08-29
**Status**: Approved (rev 2 — refined after review against both codebases)
**Target Repository**: `scholar-mcp` (`/Users/gus/Git/scholar-mcp`)
**Source Repository**: `medical-mcp` (`/Users/gus/Git/medical-mcp`)

> Revision 2 notes: this revision corrects the PubMed cache TTL (1 hour, not 7 days),
> adds the three missing pediatric TTL source keys, adds the dedicated medical PubMed
> client and the multi-database engine to the module layout, replaces the
> Google-Scholar-based journal search with PubMed journal-field queries, pins the
> guideline scoring weights to the original values, and documents every element of the
> original server that is deliberately not ported.

---

## 1. Executive Summary & Goals

`medical-mcp` is a TypeScript/Node.js Model Context Protocol (MCP) server providing access to authoritative medical data sources (FDA drug labels, RxNorm nomenclature, WHO Global Health Observatory, PubMed guidelines, AAP Bright Futures, and ClinicalTrials.gov) without requiring external API keys.

`scholar-mcp` is a high-performance Python MCP server built on `FastMCP` (v3), `httpx`, `beautifulsoup4`, `lxml`, and `pypdf` for academic discovery, full-text waterfall extraction, and citation analysis.

### Objectives
1. **Faithful Python Port**: Port the medical data clients, parsing logic, and heuristic scoring that `scholar-mcp` does not already provide, from TypeScript to idiomatic async Python.
2. **Unified Architecture**: Integrate all medical tools natively into `scholar-mcp` as a cohesive `scholar_mcp.medical` subsystem under the main FastMCP server instance.
3. **SQLite Multi-Tier Persistent Cache**: Replace transient in-memory storage with an async SQLite persistent cache supporting per-source configurable TTLs (FDA: 24h, PubMed: 1h, WHO: 7d, RxNorm: 30d, Guidelines: 7d — full table in §4).
4. **Resilient Dual-Tier Web Scraping**: Implement high-speed async HTTP scraping via `httpx` + `BeautifulSoup4` with an optional fallback to `playwright` for JavaScript-heavy or bot-protected sites (Cochrane, AAP publications).
5. **Cross-Source Deduplication**: Port title normalization, Levenshtein distance similarity matching, and metadata-rich deduplication into `scholar_mcp.utils.deduplication`.
6. **Clean Tool Output**: Expose clean snake_case tools returning structured data and readable markdown formatting without extraneous safety banners and with emoji-free cache provenance tags.

### 1.1 Deliberately Not Ported

The original server exposes 16 MCP tools; this port registers 13 (snake_case renames; `get-cache-stats` becomes `get_medical_cache_stats`). The following original elements are dropped, with rationale:

| Dropped element | Rationale |
|---|---|
| `search-google-scholar` tool + Google Scholar Puppeteer scraper | Bot-protected browser scraping is brittle and redundant: `scholar-mcp` `search_papers` covers academic search via PubMed, CrossRef, and Semantic Scholar APIs. Puppeteer is not ported at all. |
| `search-medical-literature` tool | Superseded by the existing `scholar-mcp` `search_papers` tool. |
| `get-article-details` tool (PMID lookup) | Superseded by `scholar-mcp` `get_metadata` and `get_full_text`. |
| `fetchFullTextFromPMC` (PMC full-text embedding inside article search) | Superseded by the `scholar-mcp` `get_full_text` waterfall. `MedicalArticle.full_text` stays `None` in this port. |
| Google-Scholar-based `searchJournal` helper inside `searchMedicalJournals` | Replaced with PubMed `[Journal]` field queries (see §5.7), since Google Scholar scraping is not ported. |
| `logSafetyWarnings()` and per-formatter "CRITICAL SAFETY WARNING" banners | Deliberate product decision: tool output carries no safety banners or disclaimers. |

Everything else ports 1:1, including query validation, layered FDA search, WHO indicator
variation fallback, guideline layering and scoring weights, scraping selectors, and
cache TTL behavior.

---

## 2. Directory Layout & Modular Structure

```
scholar-mcp/
├── pyproject.toml                         # Add aiosqlite to dependencies; playwright to optional [medical] extra
├── src/
│   └── scholar_mcp/
│       ├── __init__.py
│       ├── config.py                      # Extended Settings with medical and SQLite cache configs
│       ├── models.py                      # Base scholar data models
│       ├── resolver.py                    # Academic waterfall resolver
│       ├── server.py                      # FastMCP server with unified tool registry
│       ├── medical/                       # NEW: Medical domain subsystem
│       │   ├── __init__.py
│       │   ├── models.py                  # Medical data schemas and dataclasses
│       │   ├── pubmed.py                  # Medical PubMed client (esearch + efetch + XML parse)
│       │   ├── fda.py                     # openFDA Drug Label client + pediatric filters
│       │   ├── rxnorm.py                  # NLM RxNav REST API client
│       │   ├── who.py                     # WHO GHO OData API client
│       │   ├── clinical_trials.py         # ClinicalTrials.gov v2 REST client
│       │   ├── guidelines.py              # Clinical guideline search + heuristic scoring
│       │   ├── pediatrics.py              # AAP Bright Futures + Policy scraper + journal search
│       │   ├── databases.py               # Multi-database + top-journal aggregate search
│       │   └── formatters.py              # Markdown and structured dict formatters
│       └── utils/
│           ├── __init__.py
│           ├── cache.py                   # In-memory LRU cache (existing, unchanged)
│           ├── sqlite_cache.py            # NEW: SQLite persistent cache with source TTLs
│           ├── deduplication.py           # NEW: Levenshtein-based deduplication
│           ├── http.py                    # AsyncHttpClient with per-host rate limiting (existing, reused)
│           └── rate_limit.py              # Token-bucket rate limiters (existing, unchanged)
└── tests/
    ├── medical/
    │   ├── test_models_formatters.py
    │   ├── test_pubmed.py
    │   ├── test_fda.py
    │   ├── test_rxnorm.py
    │   ├── test_who.py
    │   ├── test_clinical_trials.py
    │   ├── test_guidelines.py
    │   ├── test_pediatrics.py
    │   └── test_databases.py
    ├── utils/
    │   ├── test_sqlite_cache.py
    │   └── test_deduplication.py
    ├── test_config_medical.py
    └── test_server_medical.py
```

`scholar_mcp.providers.pubmed.PubMedProvider` (existing) is **not** used by the medical
subsystem: its `search()` returns `PaperMetadata` with empty abstracts and no
publication-type-aware term building, while guideline scoring and multi-database output
require abstracts and journal names from a single `efetch` call. The medical subsystem
gets its own thin PubMed client (§5.0) mirroring the original
`searchPubMedArticles`/`parsePubMedXML` logic.

---

## 3. Data Models (`scholar_mcp.medical.models`)

All medical data models are defined using Python `@dataclass` with `.to_dict()` serialization methods.

```python
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class OpenFDAData:
    brand_name: list[str] = field(default_factory=list)
    generic_name: list[str] = field(default_factory=list)
    manufacturer_name: list[str] = field(default_factory=list)
    product_ndc: list[str] = field(default_factory=list)
    substance_name: list[str] = field(default_factory=list)
    route: list[str] = field(default_factory=list)
    dosage_form: list[str] = field(default_factory=list)


@dataclass
class DrugLabel:
    openfda: OpenFDAData = field(default_factory=OpenFDAData)
    effective_time: str = ""
    purpose: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    adverse_reactions: list[str] = field(default_factory=list)
    drug_interactions: list[str] = field(default_factory=list)
    dosage_and_administration: list[str] = field(default_factory=list)
    indications_and_usage: list[str] = field(default_factory=list)
    contraindications: list[str] = field(default_factory=list)
    use_in_specific_populations: list[str] = field(default_factory=list)
    clinical_pharmacology: list[str] = field(default_factory=list)
    pediatric_dosing: str | None = None
    pediatric_warnings: str | None = None
    raw_sections: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RxNormDrug:
    rxcui: str
    name: str
    tty: str = ""
    language: str = "ENG"
    suppress: str = ""
    synonyms: list[str] = field(default_factory=list)
    umlscui: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WHOIndicatorRecord:
    indicator_code: str
    indicator_name: str
    spatial_dim: str  # Country code or "Global"
    spatial_dim_type: str = "Country"
    time_dim: str = ""  # Year
    time_dim_type: str = "Year"
    value: str = ""
    numeric_value: float | None = None
    low: float = 0.0
    high: float = 0.0
    unit: str = ""
    age_group: str = ""
    sex: str = ""
    comments: str = ""
    date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GuidelineScore:
    publication_type: float = 0.0
    title_keywords: float = 0.0
    journal_reputation: float = 0.0
    author_affiliation: float = 0.0
    abstract_keywords: float = 0.0
    mesh_terms: float = 0.0  # Weight 0.5 is reserved but never awarded: the port does not parse MeSH (original never did either)
    total: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ClinicalGuideline:
    title: str
    organization: str
    year: str
    url: str
    description: str = ""
    category: str = "General"
    evidence_level: str = "Systematic Review/Consensus"
    pmid: str | None = None
    score: float = 0.0
    score_details: GuidelineScore | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PediatricGuideline:
    title: str
    organization: str
    url: str
    source: str  # "bright-futures" | "aap-policy"
    year: str = ""
    description: str = ""
    age_group: str = ""
    category: str = ""
    screening_recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MedicalArticle:
    title: str
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: str = ""  # Derived from PubMed publication_date (first 4 chars)
    abstract: str = ""
    pmid: str | None = None
    pmc_id: str | None = None
    doi: str | None = None
    url: str = ""
    citations: str = ""
    full_text_available: bool = False
    full_text: str | None = None  # Always None in this port; use scholar-mcp get_full_text
    source_database: str = "PubMed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

---

## 4. SQLite Persistent Cache Architecture (`scholar_mcp.utils.sqlite_cache`)

### Database Design
- **Path**: Configured in `Settings.cache_db_path` (default `~/.cache/scholar_mcp/cache.db`). The manager creates parent directories on first connect.
- **Engine**: `aiosqlite` with WAL mode (`PRAGMA journal_mode=WAL;`).
- **Lazy lifecycle**: the manager is constructed at module import (no event loop required), and opens/initializes the database on first `get`/`set`/`get_stats` call, guarded by an `asyncio.Lock`. Explicit `await cache.init_db()` is available and idempotent for tests. This is required because `scholar_mcp.server` instantiates clients at module level.
- **Capacity**: `Settings.cache_max_entries` (default 1000, env `CACHE_MAX_SIZE`). When `set` exceeds capacity, the oldest rows by `last_accessed` are evicted.
- **Schema**:
```sql
CREATE TABLE IF NOT EXISTS cache_entries (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    data TEXT NOT NULL,         -- JSON serialized payload
    created_at REAL NOT NULL,   -- Epoch timestamp
    ttl_seconds INTEGER NOT NULL,
    last_accessed REAL NOT NULL -- Epoch timestamp for LRU eviction
);
CREATE INDEX IF NOT EXISTS idx_cache_source ON cache_entries(source);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(created_at, ttl_seconds);
CREATE INDEX IF NOT EXISTS idx_cache_lru ON cache_entries(last_accessed);
```

### TTL Policy

Values match the original `medical-mcp` `cache/config.ts` exactly. The original has 11
source TTLs; the Google Scholar key is dropped with its source, and `clinical_trials`
is added for the new dedicated client.

| Source Key | Default TTL | Duration | Reason |
|---|---|---|---|
| `fda` | 86,400s | 24 Hours | FDA label updates are daily/weekly |
| `pubmed` | 3,600s | 1 Hour | Medical literature changes hourly; original uses 1h, not 7d |
| `who` | 604,800s | 7 Days | WHO global health statistics are updated annually/semi-annually |
| `rxnorm` | 2,592,000s | 30 Days | Standardized drug nomenclature is stable |
| `guidelines` | 604,800s | 7 Days | Guideline publications and scoring |
| `bright_futures` | 2,592,000s | 30 Days | Pediatric preventive guidelines change rarely |
| `aap_policy` | 604,800s | 7 Days | Policy statements publication cadence |
| `pediatric_journals` | 3,600s | 1 Hour | Same as PubMed in the original |
| `child_health` | 604,800s | 7 Days | Same as WHO in the original |
| `pediatric_drugs` | 86,400s | 24 Hours | Same as FDA in the original |
| `clinical_trials` | 86,400s | 24 Hours | Active trial status updates |

### Operations
- `async get(key: str) -> tuple[Any | None, CacheMetadata]`: Retrieves item, updates `last_accessed`, validates `created_at + ttl_seconds > now`. Returns `(data, CacheMetadata(cached=True, cache_age=...))` on hit or `(None, CacheMetadata(cached=False, cache_age=0))` on miss/expiration. Expired rows are deleted on read. Tracks hit/miss counters.
- `async set(key: str, data: Any, source: str, ttl: int | None = None) -> None`: Serializes JSON and inserts/replaces entry. TTL resolves from the explicit argument, then the per-source map, then `Settings.cache_ttl_seconds`. Evicts LRU rows when entry count exceeds `max_entries`.
- `async get_stats() -> dict[str, Any]`: Returns total entries, source breakdowns, hit count, miss count, hit rate %, database file size.
- `async init_db() -> None`: Idempotent schema/pragma initialization (also implicit on first use).
- `async close() -> None`: Closes the connection.

---

## 5. Medical Data Providers & Clients

All clients share the existing `scholar_mcp.utils.http.AsyncHttpClient` (per-host rate
limiting, retries with backoff, NCBI credential injection, 30s timeout) and the
`SQLiteCacheManager`. `AsyncHttpClient.get` returns `None` on HTTP >= 400 after retries
or on transport failure; every client treats `None` as an empty result or a structured
error, never an exception.

### 5.0 Medical PubMed Client (`scholar_mcp.medical.pubmed`)

Port of the original `searchPubMedArticles` / `parsePubMedXML` / `searchPubMed` logic.
This is a distinct client from `scholar_mcp.providers.pubmed.PubMedProvider` (see §2).

- **Endpoints**: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi` (params `db=pubmed`, `term`, `retmode=json`, `retmax`) and `.../efetch.fcgi` (params `db=pubmed`, `id=<comma-joined PMIDs>`, `retmode=xml`).
- **Flow**: esearch for IDs, then a single efetch for all IDs, then XML parse. Results are deduplicated with `scholar_mcp.utils.deduplication.deduplicate_papers` before return (mirrors the original).
- **`parse_pubmed_xml(xml: str) -> list[MedicalArticle]`** extracts, per `<PubmedArticle>`: PMID, title, abstract (HTML-stripped, whitespace-normalized), authors (`CollectiveName`, else `ForeName LastName`, else `LastName`), journal (`<Title>`), year (from `<Year>`, fallback `publication_date` prefix), DOI (`<ELocationID EIdType="doi">`), PMC ID (`<ArticleId IdType="pmc">`, `PMC` prefix stripped). Parsing uses `BeautifulSoup` with `lxml-xml`.
- **Interface**: `MedicalPubMedClient(http_client, cache, settings)` with `async search_articles(query: str, max_results: int = 10) -> tuple[list[MedicalArticle], CacheMetadata]`. The `query` is an arbitrary PubMed term — callers compose publication-type and journal filters into it. Cached under source `pubmed`.
- **Not ported**: `fetchFullTextFromPMC` embedding (§1.1).

### 5.1 FDA Client (`scholar_mcp.medical.fda`)
- **API Endpoint**: `https://api.fda.gov/drug/label.json`
- **Validation** (`is_valid_drug_query`): rejects queries whose lowercase form equals a common word (`medication`, `medicine`, `drug`, `pill`, `tablet`, `capsule`, `injection`, `dose`, `dosage`); rejects trimmed length < 3; queries matching `^[a-z]+-\d+$` or containing 3+ consecutive digits require length >= 5.
- **Search Strategy**: Sequential layered queries, deduplicating results by first `product_ndc`, stopping once `limit` results are collected:
  1. `openfda.brand_name:"{query}"`
  2. `openfda.generic_name:"{query}"`
  3. `openfda.substance_name:"{query}"`
  4. `openfda.brand_name:{query}` (partial fallback)
- **NDC Lookup**: Queries `openfda.product_ndc:{ndc}` with `limit: 1`; returns the first result or `None`.
- **Pediatric Drug Search**: Runs `search_drugs(query, limit * 2)`, then filters labels where the joined-lowercase `purpose`, `warnings`, or `dosage_and_administration` sections contain any of `pediatric`, `child`, `infant`, `neonatal`, or `pediatric dosing`; slices to `limit`. Cached under source `pediatric_drugs`.

### 5.2 RxNorm Client (`scholar_mcp.medical.rxnorm`)
- **API Endpoint**: `https://rxnav.nlm.nih.gov/REST/drugs.json` with `name={query}`.
- **Logic**: Parses `drugGroup.conceptGroup` array, extracts `conceptProperties`, maps RxCUI, name, `tty`, language, suppress flag, synonyms, and UMLS CUIs. `synonym` and `umlscui` appear in API responses as either a string or a list; both are normalized to lists. Cached under source `rxnorm` (30-day TTL).

### 5.3 WHO Global Health Observatory Client (`scholar_mcp.medical.who`)
- **API Endpoint**: `https://ghoapi.azureedge.net/api`
- **Two-Step Query Execution**:
  1. **Indicator Discovery**: Queries `/Indicator?$filter=contains(IndicatorName, '{query}')&$format=json`. If this returns zero indicators, retries with synonym variations (below) until one returns results.
  2. **Data Extraction**: For matched indicator codes (up to 3), queries `/{IndicatorCode}?$format=json&$top=50`, adding `&$filter=SpatialDim eq '{country}'` when a country is specified.
  3. **Multi-Dimension Categorization**: Extracts numeric values, bounds, year (`TimeDim`), units, age group, and sex. Groups by country keeping the most recent year per country, then sorts by recency and numeric significance.
- **Synonym Variation Map** (applied only when the primary filter returns zero; port verbatim from `medical-mcp/src/utils.ts getIndicatorVariations`): `maternal mortality`, `infant mortality`, `life expectancy`, `mortality rate`, `birth rate`, `death rate`, `population`, `health expenditure`, `immunization`, `malnutrition`, `diabetes`, `hypertension`, `cancer`, `hiv`, `tuberculosis`, `malaria`, `obesity` (each mapping to short variation lists, e.g. `"life expectancy": ["life expectancy", "expectancy", "life"]`).
- **Child Health Indicators**: Dedicated search querying predefined WHO pediatric codes (`MDG_0000000029`–`MDG_0000000034`, `WHS4_544`, `WHS9_86`) and filtering age ranges (0–18 years, under-five, infant). Cached under source `child_health`.

### 5.4 ClinicalTrials.gov Client (`scholar_mcp.medical.clinical_trials`)
- **API Endpoint**: `https://clinicaltrials.gov/api/v2/studies` with params `query`, `format=json`, `limit=10`.
- **Mapping** per study `protocolSection`: title = `identificationModule.briefTitle` or `officialTitle` (fallback `"Clinical Trial"`); authors = `[identificationModule.leadSponsor.name]` when present; abstract = `identificationModule.briefSummary`; journal = `"ClinicalTrials.gov"`; year = `statusModule.startDateStruct.date`; URL = `https://clinicaltrials.gov/study/{nctId}`.
- **No dedicated MCP tool**: this client feeds `search_medical_databases` only, matching the original.

### 5.5 Clinical Guidelines Engine (`scholar_mcp.medical.guidelines`)

Consumes `MedicalPubMedClient` (§5.0), not `scholar_mcp.providers.pubmed.PubMedProvider`.

- **Layer 1 Search (High Precision)**: PubMed query `({query}) AND ({GUIDELINE_PUBLICATION_TYPES joined with " OR "})`, `max_results=20`, where:
  - `GUIDELINE_PUBLICATION_TYPES = ['"practice guideline"[pt]', '"guideline"[pt]', '"consensus development conference"[pt]', '"consensus development conference, nih"[pt]', '"technical report"[pt]']`
- **Layer 2 Search (Semantic Fallback)**: Only if Layer 1 yields fewer than 5 results, query the **first 5** `GUIDELINE_KEYWORDS` as `[tiab]` terms (matching the original's `slice(0, 5)`):
  - `({query}) AND (guideline[tiab] OR recommendation[tiab] OR consensus[tiab] OR "position statement"[tiab] OR "standard of care"[tiab])`
  - Layer 2 articles already present in Layer 1 (by PMID) are skipped.
- **`GUIDELINE_KEYWORDS`** (all 8, port verbatim): `guideline`, `recommendation`, `consensus`, `position statement`, `standard of care`, `best practice`, `evidence-based`, `expert consensus`.
- **Heuristic Scoring** — weights port verbatim from `GUIDELINE_SCORE_WEIGHTS`:

  ```python
  def calculate_guideline_score(article: MedicalArticle, has_publication_type: bool) -> GuidelineScore:
      score = GuidelineScore()
      if has_publication_type:
          score.publication_type = 2.0
      if any(kw in article.title.lower() for kw in GUIDELINE_KEYWORDS):
          score.title_keywords = 1.0            # counted once
      for kw in GUIDELINE_KEYWORDS:              # abstract keywords, partial
          if kw in (article.abstract or "").lower():
              score.abstract_keywords += 0.5
      score.abstract_keywords = min(score.abstract_keywords, 1.0)  # cap at 2 * weight
      if any(j in article.journal.lower() for j in KNOWN_GUIDELINE_JOURNALS):
          score.journal_reputation = 1.0
      if extract_organization(article) != "Unknown Organization":
          score.author_affiliation = 1.0         # org pattern matched
      score.mesh_terms = 0.0                    # reserved weight 0.5; never awarded (original behavior)
      score.total = (score.publication_type + score.title_keywords + score.journal_reputation
                     + score.author_affiliation + score.abstract_keywords + score.mesh_terms)
      return score
  ```
  - `KNOWN_GUIDELINE_JOURNALS` substrings (port verbatim): `journal of the american`, `new england journal`, `lancet`, `bmj`, `annals of`, `guidelines`, `recommendations`.
  - Only articles with `score.total >= 2.5` (`MIN_SCORE_THRESHOLD`) qualify as clinical guidelines.
- **Organization Extraction** (`extract_organization(article) -> str`): defaults to `"Unknown Organization"`; tries the article's journal name first, then regex patterns against the abstract, then the title (first full match wins):
  - `(American|European|National|International|World|Global).*?(Association|College|Society|Academy|Institute|Foundation|Organization|Committee|Ministry)`
  - `(World Health Organization|WHO)`, `(Centers for Disease Control|CDC)`, `(National Institutes of Health|NIH)`
- **Organization Filter** (when the caller passes `organization`): keeps articles where the filter string (case-insensitive) appears in the extracted org, title, abstract, or journal, or matches an abbreviation alias. Alias map (port verbatim): `aap` → American Academy of Pediatrics; `who` → World Health Organization; `cdc` → Centers for Disease Control; `aha` → American Heart Association; `acc` → American College of Cardiology; `ada` → American Diabetes Association; `acp` → American College of Physicians.

### 5.6 Pediatric Scraping & Literature Engine (`scholar_mcp.medical.pediatrics`)

- **AAP Bright Futures**: Requests `https://brightfutures.aap.org/Search?q={query}`.
- **AAP Policy Statements**: Requests `https://publications.aap.org/pediatrics/search?q={query}`.
- **Scraping Strategy (Dual-Tier)**:
  1. *Tier 1 (`httpx` + `BeautifulSoup4`)*: async HTTP GET with a realistic desktop browser User-Agent. A short randomized delay (1–3 s, port of `randomDelay`) precedes the request to mimic the original's anti-bot pacing.
  2. *Tier 2 (Playwright Fallback)*: If Tier 1 returns zero results or a bot-block marker and `enable_playwright_fallback` is true, launch headless Chromium via `playwright` (import-guarded: if the package is not installed, the tier is skipped gracefully, never raising).
- **Selectors** (port verbatim; used identically in Tier 1 and Tier 2):
  - Bright Futures items: `.search-result, .result-item, .guideline-item, article, .content-item`; AAP Policy items: `.search-result, .result-item, .article-item, article, .publication-item`.
  - Title: `h2, h3, .title, a.title`; URL: first `a` href (relative URLs prefixed with the site base).
  - Description: `.description, .summary, .abstract, p`, truncated to 300 characters.
  - Items with empty or <= 10-character titles are dropped.
  - Bright Futures records get `organization="American Academy of Pediatrics"`, `category="Preventive Care"`, `source="bright-futures"`, and an age group extracted from the title via regex `(\d+\s*(?:-|\s*to\s*)\s*\d+\s*(?:months?|years?|days?)|infant|toddler|preschool|school-age|adolescent)` (case-insensitive).
  - AAP Policy records get `category="Policy Statement"`, `source="aap-policy"`, and a year extracted from the title via `\b(19|20)\d{2}\b`.
- **AAP Guidelines (combined)**: `search_aap_guidelines(query)` runs Bright Futures and AAP Policy searches concurrently (`asyncio.gather(..., return_exceptions=True)`), concatenates, and removes duplicates by normalized title (lowercase, non-word characters stripped — exact-match normalization, not Levenshtein).
- **Pediatric Journals Filter** (`search_pediatric_literature`): one `MedicalPubMedClient.search_articles` call with the query `({query}) AND ("Pediatrics"[Journal] OR "JAMA Pediatrics"[Journal] OR "The Journal of Pediatrics"[Journal] OR "Pediatric Research"[Journal] OR "Archives of Disease in Childhood"[Journal] OR "European Journal of Pediatrics"[Journal] OR "Pediatric Clinics of North America"[Journal])`. Cached under source `pediatric_journals`.

### 5.7 Multi-Database & Journal Search Engine (`scholar_mcp.medical.databases`)

- **`search_medical_databases(query)`**: runs concurrent searches over Medical PubMed (`max_results=5`), ClinicalTrials.gov, and Cochrane Library, using `asyncio.gather(*tasks, return_exceptions=True)` so one failing source never aborts the others. Results are converted to a common shape, deduplicated with `deduplicate_papers`, re-ranked with `rank_medical_articles(query)` (default `position_weight=0.0`, since a merged multi-source pool has no single meaningful ordering), and capped at 20.
  - **Cochrane scraping**: Tier 1 HTTP GET `https://www.cochranelibrary.com/search?q={query}` (with the 1–3 s randomized delay), parsing items `.search-result-item, .result-item, .search-result`, titles `h3 a, .title a, .result-title a`, descriptions `.abstract, .snippet, .summary`, journal defaulting to `"Cochrane Database"`, relative URLs prefixed with `https://www.cochranelibrary.com`; same Playwright fallback rules as §5.6. Google Scholar is not included (§1.1).
- **`search_medical_journals(query)`**: one PubMed query restricted to the top journals — `({query}) AND ("New England Journal of Medicine"[Journal] OR "JAMA"[Journal] OR "Lancet"[Journal] OR "BMJ"[Journal] OR "Nature Medicine"[Journal])` — then deduplicate, re-rank with `rank_medical_articles(deduped, query)` using the raw user `query` (not the `[Journal]`-expanded term, so journal names don't count as query terms), and cap at 15 (matching the original's result cap; the original's per-journal Google Scholar queries are replaced by this single PubMed query per §1.1).

---

## 6. Literature Deduplication Engine (`scholar_mcp.utils.deduplication`)

### Algorithm
1. **Title Normalization** (`normalize_title`):
   - Decode HTML entities (`&amp;`, `&quot;`, `&#39;`, etc.).
   - Strip preprint & version tags (`[preprint]`, `arXiv:\d+`, `version \d+`, `v\d+`).
   - Remove punctuation (`[-:.,;]`), lowercase, normalize whitespace. `&` is kept.
2. **Matching Strategy** (`are_duplicates(p1, p2, threshold=0.9)`):
   - *DOI Match*: If both items have valid DOIs and match, return True.
   - *Exact Title Match*: If normalized titles are identical and first author last names match (or year matches).
   - *Fuzzy Match*: Levenshtein distance similarity ratio `sim(t1, t2) >= 0.90` combined with identical first author and publication year.
3. **Metadata Merging** (`deduplicate_papers(papers) -> tuple[list[dict], dict]`):
   - When a duplicate is detected, retain the record with richer metadata (prefers DOI, longer abstract, explicit author list).
   - Returns the unique list plus a stats dict (`duplicates_removed`, `total_input`, `unique_count`).

---

## 7. FastMCP Tool Registry & Formatting Pipeline

### 7.1 Registered Tools in `scholar_mcp.server`

All tools are registered on the existing `mcp: FastMCP` instance with `@mcp.tool()`
decorators, docstrings, type annotations, and validation, following the existing
`server.py` conventions (module-level client instances, structured error dicts on
failure). Registration is gated by `settings.enable_medical_tools`.

1. `search_drugs(query: str, limit: int = 10) -> list[dict[str, Any]] | dict[str, Any]`
2. `get_drug_details(ndc: str) -> dict[str, Any]`
3. `search_pediatric_drugs(query: str, limit: int = 10) -> list[dict[str, Any]] | dict[str, Any]`
4. `search_drug_nomenclature(query: str) -> list[dict[str, Any]] | dict[str, Any]`
5. `get_health_statistics(indicator: str, country: str | None = None, limit: int = 10) -> dict[str, Any]`
6. `get_child_health_statistics(indicator: str, country: str | None = None, limit: int = 10) -> dict[str, Any]`
7. `search_clinical_guidelines(query: str, organization: str | None = None) -> list[dict[str, Any]] | dict[str, Any]`
8. `search_pediatric_guidelines(query: str, source: str = "all") -> list[dict[str, Any]] | dict[str, Any]` (`source` in `"bright-futures" | "aap-policy" | "all"`)
9. `search_aap_guidelines(query: str) -> list[dict[str, Any]] | dict[str, Any]`
10. `search_pediatric_literature(query: str, max_results: int = 10) -> list[dict[str, Any]] | dict[str, Any]`
11. `search_medical_databases(query: str) -> list[dict[str, Any]] | dict[str, Any]`
12. `search_medical_journals(query: str) -> list[dict[str, Any]] | dict[str, Any]`
13. `get_medical_cache_stats() -> dict[str, Any]`

Dropped tools and their replacements: see §1.1.

### 7.2 Formatting Rules
- **No Safety Banner**: No warnings or disclaimers in tool outputs.
- **Emoji-Free Cache Provenance**: Output metadata tags formatted as `[Cached: {age}s old]` or `[Fresh response]` without emojis.
- **Dual Representation**: Formatters return `{"data": <structured list/dict>, "markdown": <readable summary>}`. The structured fields serve programmatic agent use; the markdown serves human-readable display.

---

## 8. Configuration Additions (`scholar_mcp.config.Settings`)

Follows the existing `Settings` convention: plain dataclass field defaults, with all
environment parsing inside `Settings.load()`. (The dataclass does not read environment
variables in `default_factory` — that pattern is not used anywhere in `scholar-mcp`.)

```python
# New dataclass fields (defaults shown; parsed in Settings.load())
cache_db_path: Path = field(default_factory=lambda: Path("~/.cache/scholar_mcp/cache.db").expanduser())
cache_max_entries: int = 1000
cache_ttl_fda: int = 86400
cache_ttl_pubmed: int = 3600
cache_ttl_who: int = 604800
cache_ttl_rxnorm: int = 2592000
cache_ttl_guidelines: int = 604800
cache_ttl_bright_futures: int = 2592000
cache_ttl_aap_policy: int = 604800
cache_ttl_pediatric_journals: int = 3600
cache_ttl_child_health: int = 604800
cache_ttl_pediatric_drugs: int = 86400
cache_ttl_clinical_trials: int = 86400
enable_playwright_fallback: bool = True
enable_medical_tools: bool = True

# Corresponding Settings.load() entries (reuse the existing _bool helper):
cache_db_path=Path(os.getenv("SCHOLAR_CACHE_DB", "~/.cache/scholar_mcp/cache.db")).expanduser(),
cache_max_entries=int(os.getenv("CACHE_MAX_SIZE", "1000")),
cache_ttl_fda=int(os.getenv("CACHE_TTL_FDA", "86400")),
cache_ttl_pubmed=int(os.getenv("CACHE_TTL_PUBMED", "3600")),
cache_ttl_who=int(os.getenv("CACHE_TTL_WHO", "604800")),
cache_ttl_rxnorm=int(os.getenv("CACHE_TTL_RXNORM", "2592000")),
cache_ttl_guidelines=int(os.getenv("CACHE_TTL_CLINICAL_GUIDELINES", "604800")),
cache_ttl_bright_futures=int(os.getenv("CACHE_TTL_BRIGHT_FUTURES", "2592000")),
cache_ttl_aap_policy=int(os.getenv("CACHE_TTL_AAP_POLICY", "604800")),
cache_ttl_pediatric_journals=int(os.getenv("CACHE_TTL_PEDIATRIC_JOURNALS", "3600")),
cache_ttl_child_health=int(os.getenv("CACHE_TTL_CHILD_HEALTH", "604800")),
cache_ttl_pediatric_drugs=int(os.getenv("CACHE_TTL_PEDIATRIC_DRUGS", "86400")),
cache_ttl_clinical_trials=int(os.getenv("CACHE_TTL_CLINICAL_TRIALS", "86400")),
enable_playwright_fallback=_bool(os.getenv("ENABLE_PLAYWRIGHT_FALLBACK"), True),
enable_medical_tools=_bool(os.getenv("ENABLE_MEDICAL_TOOLS"), True),
```

Environment variable names match the original `medical-mcp` wherever the key existed
there (`CACHE_TTL_FDA`, `CACHE_TTL_PUBMED`, ..., `CACHE_MAX_SIZE`), so existing
deployments migrate without config changes.

---

## 9. Error Handling & Resilience

- **Network Timeouts**: Individual requests use `request_timeout` (default 30s) via the shared `AsyncHttpClient`.
- **HTTP Failure Semantics**: `AsyncHttpClient.get` returns `None` on status >= 400 after retries or on transport errors. Every medical client treats `None` as "no results" (or a structured error for detail lookups), never raises.
- **Graceful Degradation**: If an external service is unavailable, the client returns a structured error object `{"status": "error", "error": "...", "source": "fda"}` rather than crashing the MCP server.
- **Concurrent Search Protection**: Multi-database and AAP combined queries use `asyncio.gather(*tasks, return_exceptions=True)` to ensure slow or failing endpoints do not abort successful queries from other sources.
- **Playwright Availability**: The Playwright fallback is import-guarded and flag-gated; a missing `playwright` package degrades to Tier-1-only scraping without errors.

---

## 10. Testing & Verification Plan

Tests follow the repo's existing conventions: `pytest` with `asyncio_mode = "auto"` (no
`@pytest.mark.asyncio` decorators needed), `respx` for HTTP mocking, `AsyncMock` +
`monkeypatch` for engine dependencies, and direct calls to the decorated tool functions
(no FastMCP private APIs).

### 10.1 Unit Tests (`pytest`)
- `tests/test_config_medical.py`: Defaults and env overrides for all new `Settings` fields.
- `tests/utils/test_deduplication.py`: Normalization, exact match, fuzzy Levenshtein match, and metadata merge.
- `tests/utils/test_sqlite_cache.py`: Async get/set, source-specific TTL expiration, LRU eviction, hit/miss stats, lazy initialization.
- `tests/medical/test_models_formatters.py`: Dataclass defaults and emoji-free, banner-free formatter output.
- `tests/medical/test_pubmed.py`: esearch/efetch flow and `parse_pubmed_xml` extraction (title, abstract, authors, journal, year, DOI, PMC ID) against a recorded XML fixture.
- `tests/medical/test_fda.py`: Query validation, layered search, NDC retrieval, pediatric keyword filtering.
- `tests/medical/test_rxnorm.py`: Concept group traversal, RxCUI extraction, synonym normalization (string vs list).
- `tests/medical/test_who.py`: Indicator discovery with synonym fallback, multi-dimensional extraction, country filter, child health indicators.
- `tests/medical/test_clinical_trials.py`: ClinicalTrials.gov v2 REST parser and mapping.
- `tests/medical/test_guidelines.py`: Layer 1 vs Layer 2 search selection, heuristic scoring weights, organization extraction and abbreviation aliases.
- `tests/medical/test_pediatrics.py`: Bright Futures and Policy Statement scrapers with mock HTML payloads (selectors, age-group/year regexes, description cap), AAP combined dedup, pediatric journal query composition.
- `tests/medical/test_databases.py`: Multi-database aggregation with mocked sub-engines, deduplication, cap; journal search query composition.
- `tests/test_server_medical.py`: All 13 medical tools exposed on the server module and callable (repo convention: `callable(getattr(srv, name))` plus direct tool-function invocation with mocked engines).

### 10.2 Quality Gate Commands
```bash
# Run all tests
pytest tests/

# Run medical unit tests specifically
pytest tests/medical/ tests/utils/test_sqlite_cache.py tests/utils/test_deduplication.py

# Run test coverage
pytest --cov=scholar_mcp --cov-report=term-missing
```
