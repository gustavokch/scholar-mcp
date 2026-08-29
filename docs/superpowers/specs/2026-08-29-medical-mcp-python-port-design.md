# Design Specification: Porting Medical MCP to Python and Integrating into Scholar MCP

**Date**: 2026-08-29  
**Status**: Approved  
**Target Repository**: `scholar-mcp` (`/Users/gus/Git/scholar-mcp`)  
**Source Repository**: `medical-mcp` (`/Users/gus/Git/medical-mcp`)  

---

## 1. Executive Summary & Goals

`medical-mcp` is a TypeScript/Node.js Model Context Protocol (MCP) server providing access to authoritative medical data sources (FDA drug labels, RxNorm nomenclature, WHO Global Health Observatory, PubMed guidelines, AAP Bright Futures, and ClinicalTrials.gov) without requiring external API keys.

`scholar-mcp` is a high-performance Python MCP server built on `FastMCP`, `httpx`, `beautifulsoup4`, `lxml`, and `pypdf` for academic discovery, full-text waterfall extraction, and citation analysis.

### Objectives
1. **Full Python Port**: Port all external medical data clients, parsing logic, and heuristic scoring from TypeScript to idiomatic async Python.
2. **Unified Architecture**: Integrate all medical tools natively into `scholar-mcp` as a cohesive `scholar_mcp.medical` subsystem under the main FastMCP server instance.
3. **SQLite Multi-Tier Persistent Cache**: Replace transient in-memory storage with an async SQLite persistent cache supporting per-source configurable TTLs (e.g., FDA: 24h, WHO: 7d, RxNorm: 30d, Guidelines: 7d, PubMed: 7d).
4. **Resilient Dual-Tier Web Scraping**: Implement high-speed async HTTP scraping via `httpx` + `BeautifulSoup4` with an optional fallback to `playwright` for JavaScript-heavy or bot-protected sites (Cochrane, AAP publications).
5. **Cross-Source Deduplication**: Port title normalization, Levenshtein distance similarity matching, and metadata-rich deduplication into `scholar_mcp.utils.deduplication`.
6. **Clean Tool Output**: Expose clean snake_case tools returning structured data and readable markdown formatting without extraneous safety banners and with emoji-free cache provenance tags.

---

## 2. Directory Layout & Modular Structure

```
scholar-mcp/
├── pyproject.toml                         # Updated with optional [playwright, aiosqlite] dependencies
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
│       │   ├── fda.py                     # openFDA Drug Label client + pediatric filters
│       │   ├── rxnorm.py                  # NLM RxNav REST API client
│       │   ├── who.py                     # WHO GHO OData API client
│       │   ├── clinical_trials.py         # ClinicalTrials.gov v2 REST client
│       │   ├── guidelines.py              # Clinical guideline search + heuristic scoring
│       │   ├── pediatrics.py              # AAP Bright Futures + Policy scraper + journal search
│       │   └── formatters.py              # Markdown and structured dict formatters
│       └── utils/
│           ├── __init__.py
│           ├── cache.py                   # In-memory LRU cache
│           ├── sqlite_cache.py            # NEW: SQLite persistent cache with source TTLs
│           ├── deduplication.py           # NEW: Levenshtein-based deduplication
│           ├── http.py                    # AsyncHttpClient with per-host rate limiting
│           └── rate_limit.py              # Token-bucket rate limiters
└── tests/
    ├── medical/
    │   ├── test_fda.py
    │   ├── test_rxnorm.py
    │   ├── test_who.py
    │   ├── test_guidelines.py
    │   ├── test_pediatrics.py
    │   └── test_clinical_trials.py
    ├── utils/
    │   ├── test_sqlite_cache.py
    │   └── test_deduplication.py
    └── test_server_medical.py
```

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
    mesh_terms: float = 0.0
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
    year: str = ""
    abstract: str = ""
    pmid: str | None = None
    pmc_id: str | None = None
    doi: str | None = None
    url: str = ""
    citations: str = ""
    full_text_available: bool = False
    full_text: str | None = None
    source_database: str = "PubMed"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

---

## 4. SQLite Persistent Cache Architecture (`scholar_mcp.utils.sqlite_cache`)

### Database Design
- **Path**: Configured in `Settings.cache_db_path` (default `~/.cache/scholar_mcp/cache.db`).
- **Engine**: SQLite via standard `sqlite3` in worker threads or `aiosqlite` with WAL mode (`PRAGMA journal_mode=WAL;`).
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
| Source Key | Default TTL | Duration | Reason |
|---|---|---|---|
| `fda` | 86,400s | 24 Hours | FDA label updates are daily/weekly |
| `who` | 604,800s | 7 Days | WHO global health statistics are updated annually/semi-annually |
| `rxnorm` | 2,592,000s | 30 Days | Standardized drug nomenclature is stable |
| `guidelines` | 604,800s | 7 Days | Guideline publications and scoring |
| `pubmed` | 604,800s | 7 Days | Medical literature queries |
| `bright_futures` | 2,592,000s | 30 Days | Pediatric preventive guidelines change rarely |
| `aap_policy` | 604,800s | 7 Days | Policy statements publication cadence |
| `clinical_trials` | 86,400s | 24 Hours | Active trial status updates |

### Operations
- `async get(key: str) -> tuple[Any | None, CacheMetadata]`: Retrieves item, updates `last_accessed`, validates `created_at + ttl_seconds > now`. Returns `(data, CacheMetadata(cached=True, cache_age=...))` on hit or `(None, CacheMetadata(cached=False, cache_age=0))` on miss/expiration.
- `async set(key: str, data: Any, source: str, ttl: int | None = None) -> None`: Serializes JSON and inserts/replaces entry. Runs periodic cleanup when entry count exceeds `max_entries` (deletes oldest `last_accessed` items).
- `async get_stats() -> dict[str, Any]`: Returns total entries, source breakdowns, hit count, miss count, hit rate %, database file size.

---

## 5. Medical Data Providers & Clients

### 5.1 FDA Client (`scholar_mcp.medical.fda`)
- **API Endpoint**: `https://api.fda.gov/drug/label.json`
- **Validation**: Rejects common generic terms (e.g. "medication", "pill", "tablet") and queries < 3 characters.
- **Search Strategy**: Executes layered queries with exact matching precedence:
  1. `openfda.brand_name:"{query}"`
  2. `openfda.generic_name:"{query}"`
  3. `openfda.substance_name:"{query}"`
  4. `openfda.brand_name:{query}` (partial fallback)
- **NDC Lookup**: Queries `openfda.product_ndc:{ndc}`.
- **Pediatric Indicator Search**: Queries FDA label endpoint with expanded limit and applies regex matching over `purpose`, `warnings`, `dosage_and_administration` for pediatric terms (`pediatric`, `child`, `infant`, `neonatal`, `children`).

### 5.2 RxNorm Client (`scholar_mcp.medical.rxnorm`)
- **API Endpoint**: `https://rxnav.nlm.nih.gov/REST/drugs.json`
- **Logic**: Parses `drugGroup.conceptGroup` array, extracts `conceptProperties`, maps RxCUI, name, term types (`tty`), synonyms, and UMLS CUIs.

### 5.3 WHO Global Health Observatory Client (`scholar_mcp.medical.who`)
- **API Endpoint**: `https://ghoapi.azureedge.net/api`
- **Two-Step Query Execution**:
  1. **Indicator Discovery**: Queries `/Indicator?$filter=contains(IndicatorName, '{query}')&$format=json`. Expands search using synonym map (e.g., "life expectancy", "infant mortality", "maternal mortality", "malnutrition", "immunization", "diabetes", "hypertension", "hiv", "malaria").
  2. **Data Extraction**: For matched indicator codes (up to 3), queries `/{IndicatorCode}?$format=json&$top=50` (plus `$filter=SpatialDim eq '{country}'` if country specified).
  3. **Multi-Dimension Categorization**: Extracts numeric values, bounds, year (`TimeDim`), units, age group, and sex. Groups and sorts by recency and numeric significance.
- **Child Health Indicators**: Dedicated search querying predefined WHO pediatric codes (`MDG_0000000029`–`MDG_0000000034`, `WHS4_544`, `WHS9_86`) and filtering age ranges (0–18 years, under-five, infant).

### 5.4 Clinical Trials Client (`scholar_mcp.medical.clinical_trials`)
- **API Endpoint**: `https://clinicaltrials.gov/api/v2/studies`
- **Logic**: Queries official REST API for study protocols, lead sponsors, brief summaries, conditions, phase, study status, and NCT IDs (`https://clinicaltrials.gov/study/{nctId}`).

### 5.5 Clinical Guidelines Engine (`scholar_mcp.medical.guidelines`)
- **Layer 1 Search (High Precision)**: Query PubMed with controlled publication types:
  - `({query}) AND ("practice guideline"[pt] OR "guideline"[pt] OR "consensus development conference"[pt] OR "consensus development conference, nih"[pt] OR "technical report"[pt])`
- **Layer 2 Search (Semantic Fallback)**: If Layer 1 returns < 5 results, query semantic keywords:
  - `({query}) AND (guideline[tiab] OR recommendation[tiab] OR consensus[tiab] OR "position statement"[tiab] OR "standard of care"[tiab] OR "evidence-based"[tiab] OR "expert consensus"[tiab])`
- **Heuristic Scoring System**:
  ```python
  score = 0.0
  if has_publication_type:
      score += 2.0
  if any(kw in title.lower() for kw in GUIDELINE_KEYWORDS):
      score += 1.0
  if any(j in journal.lower() for j in KNOWN_GUIDELINE_JOURNALS):
      score += 1.0
  if matched_organization:
      score += 1.0
  abstract_kw_count = sum(1 for kw in GUIDELINE_KEYWORDS if kw in abstract.lower())
  score += min(abstract_kw_count * 0.5, 1.0)
  ```
  Only articles with `total_score >= 2.5` qualify as clinical guidelines.
- **Dynamic Organization Extraction**: Regex matching against:
  - `(American|European|National|International|World|Global).*?(Association|College|Society|Academy|Institute|Foundation|Organization|Committee|Ministry)`
  - Known abbreviations: WHO, CDC, NIH, AHA, ACC, ADA, ACP, AAP.

### 5.6 Pediatric Scraping & Literature Engine (`scholar_mcp.medical.pediatrics`)
- **AAP Bright Futures**: Searches `https://brightfutures.aap.org/Search?q={query}`.
- **AAP Policy Statements**: Searches `https://publications.aap.org/pediatrics/search?q={query}`.
- **Scraping Strategy (Dual-Tier)**:
  1. *Tier 1 (`httpx` + `BeautifulSoup4`)*: Standard async HTTP GET with realistic headers.
  2. *Tier 2 (Playwright Fallback)*: If HTTP encounters anti-scraping blocks or zero results, and Playwright is available, invoke headless browser evaluation.
- **Pediatric Journals Filter**: Queries PubMed restricted to top pediatric journals:
  - `"Pediatrics"[Journal] OR "JAMA Pediatrics"[Journal] OR "The Journal of Pediatrics"[Journal] OR "Pediatric Research"[Journal] OR "Archives of Disease in Childhood"[Journal] OR "European Journal of Pediatrics"[Journal] OR "Pediatric Clinics of North America"[Journal]`.

---

## 6. Literature Deduplication Engine (`scholar_mcp.utils.deduplication`)

### Algorithm
1. **Title Normalization**:
   - Decode HTML entities (`&amp;`, `&quot;`, `&#39;`, etc.).
   - Strip preprint & version tags (`[preprint]`, `arXiv:\d+`, `version \d+`, `v\d+`).
   - Remove punctuation (`[-:.,;]`), lowercase, normalize whitespace.
2. **Matching Strategy**:
   - *DOI Match*: If both items have valid DOIs and match, return True.
   - *Exact Title Match*: If normalized titles are identical and first author last names match (or year matches).
   - *Fuzzy Match*: Levenshtein distance similarity ratio `sim(t1, t2) >= 0.90` combined with identical first author and publication year.
3. **Metadata Merging**:
   - When a duplicate is detected, retain the record with richer metadata (prefers DOI, longer abstract, explicit author list).

---

## 7. FastMCP Tool Registry & Formatting Pipeline

### 7.1 Registered Tools in `scholar_mcp.server`

All tools are registered on `mcp: FastMCP` with docstrings, type annotations, and validation:

1. `search_drugs(query: str, limit: int = 10) -> list[dict[str, Any]] | dict[str, Any]`
2. `get_drug_details(ndc: str) -> dict[str, Any]`
3. `search_pediatric_drugs(query: str, limit: int = 10) -> list[dict[str, Any]] | dict[str, Any]`
4. `search_drug_nomenclature(query: str) -> list[dict[str, Any]] | dict[str, Any]`
5. `get_health_statistics(indicator: str, country: str | None = None, limit: int = 10) -> dict[str, Any]`
6. `get_child_health_statistics(indicator: str, country: str | None = None, limit: int = 10) -> dict[str, Any]`
7. `search_clinical_guidelines(query: str, organization: str | None = None) -> list[dict[str, Any]] | dict[str, Any]`
8. `search_pediatric_guidelines(query: str, source: str = "all") -> list[dict[str, Any]] | dict[str, Any]`
9. `search_aap_guidelines(query: str) -> list[dict[str, Any]] | dict[str, Any]`
10. `search_pediatric_literature(query: str, max_results: int = 10) -> list[dict[str, Any]] | dict[str, Any]`
11. `search_medical_databases(query: str) -> list[dict[str, Any]] | dict[str, Any]`
12. `search_medical_journals(query: str) -> list[dict[str, Any]] | dict[str, Any]`
13. `get_medical_cache_stats() -> dict[str, Any]`

### 7.2 Formatting Rules
- **No Safety Banner**: No warnings or disclaimers in tool outputs.
- **Emoji-Free Cache Provenance**: Output metadata tags formatted as `[Cached: {age}s old]` or `[Fresh response]` without emojis.
- **Dual Representation**: Responses return structured JSON fields for programmatic agent use, accompanied by markdown representations where appropriate.

---

## 8. Configuration Additions (`scholar_mcp.config.Settings`)

New fields in `Settings`:
```python
# Medical and Cache Settings
cache_db_path: Path = field(
    default_factory=lambda: Path(
        os.getenv("SCHOLAR_CACHE_DB", "~/.cache/scholar_mcp/cache.db")
    ).expanduser()
)
cache_ttl_fda: int = int(os.getenv("CACHE_TTL_FDA", "86400"))
cache_ttl_who: int = int(os.getenv("CACHE_TTL_WHO", "604800"))
cache_ttl_rxnorm: int = int(os.getenv("CACHE_TTL_RXNORM", "2592000"))
cache_ttl_guidelines: int = int(os.getenv("CACHE_TTL_CLINICAL_GUIDELINES", "604800"))
cache_ttl_pubmed: int = int(os.getenv("CACHE_TTL_PUBMED", "604800"))
cache_ttl_bright_futures: int = int(os.getenv("CACHE_TTL_BRIGHT_FUTURES", "2592000"))
cache_ttl_aap_policy: int = int(os.getenv("CACHE_TTL_AAP_POLICY", "604800"))
cache_ttl_clinical_trials: int = int(os.getenv("CACHE_TTL_CLINICAL_TRIALS", "86400"))
enable_playwright_fallback: bool = field(
    default_factory=lambda: os.getenv("ENABLE_PLAYWRIGHT_FALLBACK", "true").lower()
    in ("1", "true", "yes")
)
enable_medical_tools: bool = field(
    default_factory=lambda: os.getenv("ENABLE_MEDICAL_TOOLS", "true").lower()
    in ("1", "true", "yes")
)
```

---

## 9. Error Handling & Resilience

- **Network Timeouts**: Individual requests use `request_timeout` (default 30s).
- **Graceful Degradation**: If an external service is unavailable (e.g., openFDA 500 or WHO GHO timeout), the client returns a structured error object `{"status": "error", "error": "...", "source": "fda"}` rather than crashing the MCP server.
- **Concurrent Search Protection**: Multi-database queries use `asyncio.gather(*tasks, return_exceptions=True)` to ensure slow or failing endpoints do not abort successful queries from other sources.

---

## 10. Testing & Verification Plan

### 10.1 Unit Tests (`pytest`)
- `tests/medical/test_fda.py`: Test query validation, parameter encoding, response parsing, and pediatric filtering against recorded fixtures.
- `tests/medical/test_rxnorm.py`: Test concept group traversal, RxCUI extraction, and synonym collection.
- `tests/medical/test_who.py`: Test indicator synonym mapping, multi-dimensional response extraction, and child health indicator filters.
- `tests/medical/test_guidelines.py`: Test Layer 1 vs Layer 2 search selection, heuristic scoring weights, and organization extraction.
- `tests/medical/test_pediatrics.py`: Test Bright Futures and Policy Statement scrapers with mock HTML payloads.
- `tests/medical/test_clinical_trials.py`: Test ClinicalTrials.gov v2 REST parser.
- `tests/utils/test_sqlite_cache.py`: Test async get/set, source-specific TTL expiration, LRU eviction, and stats reporting.
- `tests/utils/test_deduplication.py`: Test normalization, exact match, fuzzy Levenshtein match, and metadata merge.
- `tests/test_server_medical.py`: End-to-end FastMCP tool invocation tests verifying output schema.

### 10.2 Quality Gate Commands
```bash
# Run all tests
pytest tests/

# Run medical unit tests specifically
pytest tests/medical/ tests/utils/test_sqlite_cache.py tests/utils/test_deduplication.py

# Run test coverage
pytest --cov=scholar_mcp --cov-report=term-missing
```
