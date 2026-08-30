# Medical MCP Python Port & Scholar MCP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the medical data providers, heuristic guideline scoring, and scraping engines from TypeScript `medical-mcp` to Python, integrating them natively as a persistent SQLite-backed `scholar_mcp.medical` subsystem under `scholar-mcp`.

**Architecture:** A modular async architecture in `scholar_mcp.medical` containing a dedicated medical PubMed client (esearch + efetch XML with abstracts) and clients for openFDA, RxNav, WHO GHO, ClinicalTrials.gov, PubMed guideline heuristics, AAP pediatric scrapers (`httpx` + `bs4` with optional Playwright fallback), and a multi-database aggregate engine. Data is persistently cached with source-specific TTLs in `scholar_mcp.utils.sqlite_cache` (lazily initialized — the server constructs clients at module import), deduplicated via `scholar_mcp.utils.deduplication`, and registered as FastMCP tools in `scholar_mcp.server` following that module's existing conventions.

**Tech Stack:** Python 3.10+, `fastmcp` 3.x (installed 3.4.2), `httpx`, `aiosqlite`, `beautifulsoup4`, `lxml`, `pytest`, `pytest-asyncio`, `respx`.

**Spec:** `docs/superpowers/specs/2026-08-29-medical-mcp-python-port-design.md`

## Global Constraints

- Target Python: `>=3.10` (existing `requires-python`).
- FastMCP: `>=3.0.0` is already installed (3.4.2). Register tools with `@mcp.tool()` on the existing `mcp` instance in `server.py`. Never use private FastMCP APIs (no `mcp._tool_manager`); tests call tool functions directly, following `tests/test_server_tools.py` conventions.
- New dependency: add `aiosqlite>=0.20.0` to `[project] dependencies`. Add optional extra `[project.optional-dependencies] medical = ["playwright>=1.40.0"]`. `pytest`, `pytest-asyncio`, and `respx` are already in the `dev` extra — do not re-add.
- Test conventions: `asyncio_mode = "auto"` is set in `pyproject.toml` — do not use `@pytest.mark.asyncio` decorators; plain async fixtures work. Use `respx` for HTTP mocking, `AsyncMock`/`monkeypatch` for engine dependencies.
- All HTTP goes through the existing `scholar_mcp.utils.http.AsyncHttpClient` (per-host rate limiting, retries, NCBI credential injection, 30s timeout). `AsyncHttpClient.get` returns `None` on status >= 400 after retries or transport failure — clients treat `None` as "no results", never raise.
- Settings follow repo convention: plain dataclass field defaults; all env parsing inside `Settings.load()`. Env var names match original `medical-mcp` (`CACHE_TTL_FDA`, `CACHE_TTL_PUBMED`, `CACHE_MAX_SIZE`, ...).
- Cache TTL defaults (seconds): fda 86400, pubmed 3600, who 604800, rxnorm 2592000, guidelines 604800, bright_futures 2592000, aap_policy 604800, pediatric_journals 3600, child_health 604800, pediatric_drugs 86400, clinical_trials 86400. `cache_max_entries` 1000.
- Output formatting: no medical safety banners or disclaimers; cache provenance tags are plain text without emojis (`[Cached: 142s old]`, `[Fresh response]`).
- All medical tool names use snake_case (`search_drugs`, `get_health_statistics`, ...).
- SQLite lifecycle: `SQLiteCacheManager` is constructed at module import and opens/initializes the DB lazily on first use (guarded by an `asyncio.Lock`); parent directories are created on first connect.
- Every task is strict TDD: write the failing test, watch it fail, implement, watch it pass, commit.

---

### Task 1: Project Dependencies & Settings Configuration

**Files:**
- Modify: `/Users/gus/Git/scholar-mcp/pyproject.toml`
- Modify: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/config.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/test_config_medical.py`

**Interfaces:**
- Consumes: existing `Settings` dataclass and `Settings.load()` in `src/scholar_mcp/config.py`.
- Produces: `Settings` fields `cache_db_path: Path`, `cache_max_entries: int`, `cache_ttl_fda`, `cache_ttl_pubmed`, `cache_ttl_who`, `cache_ttl_rxnorm`, `cache_ttl_guidelines`, `cache_ttl_bright_futures`, `cache_ttl_aap_policy`, `cache_ttl_pediatric_journals`, `cache_ttl_child_health`, `cache_ttl_pediatric_drugs`, `cache_ttl_clinical_trials` (all `int`), `enable_playwright_fallback: bool`, `enable_medical_tools: bool`.

- [ ] **Step 1: Write the failing test for configuration settings**

Create `/Users/gus/Git/scholar-mcp/tests/test_config_medical.py`:
```python
from pathlib import Path

from scholar_mcp.config import Settings


def test_medical_settings_defaults():
    settings = Settings.load()
    assert settings.cache_ttl_fda == 86400
    assert settings.cache_ttl_pubmed == 3600
    assert settings.cache_ttl_who == 604800
    assert settings.cache_ttl_rxnorm == 2592000
    assert settings.cache_ttl_guidelines == 604800
    assert settings.cache_ttl_bright_futures == 2592000
    assert settings.cache_ttl_aap_policy == 604800
    assert settings.cache_ttl_pediatric_journals == 3600
    assert settings.cache_ttl_child_health == 604800
    assert settings.cache_ttl_pediatric_drugs == 86400
    assert settings.cache_ttl_clinical_trials == 86400
    assert settings.cache_max_entries == 1000
    assert settings.enable_playwright_fallback is True
    assert settings.enable_medical_tools is True
    assert isinstance(settings.cache_db_path, Path)
    assert settings.cache_db_path == Path("~/.cache/scholar_mcp/cache.db").expanduser()


def test_medical_settings_env_override(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_FDA", "12345")
    monkeypatch.setenv("CACHE_TTL_PUBMED", "90")
    monkeypatch.setenv("CACHE_TTL_PEDIATRIC_JOURNALS", "120")
    monkeypatch.setenv("CACHE_MAX_SIZE", "50")
    monkeypatch.setenv("ENABLE_MEDICAL_TOOLS", "false")
    monkeypatch.setenv("ENABLE_PLAYWRIGHT_FALLBACK", "no")
    monkeypatch.setenv("SCHOLAR_CACHE_DB", "/tmp/custom_cache.db")
    settings = Settings.load()
    assert settings.cache_ttl_fda == 12345
    assert settings.cache_ttl_pubmed == 90
    assert settings.cache_ttl_pediatric_journals == 120
    assert settings.cache_max_entries == 50
    assert settings.enable_medical_tools is False
    assert settings.enable_playwright_fallback is False
    assert settings.cache_db_path == Path("/tmp/custom_cache.db")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/test_config_medical.py -v`
Expected: FAIL with AttributeError (missing attributes on Settings).

- [ ] **Step 3: Update `pyproject.toml` and `config.py`**

In `/Users/gus/Git/scholar-mcp/pyproject.toml`: add `"aiosqlite>=0.20.0"` to `[project] dependencies`, and add to `[project.optional-dependencies]`:
```toml
medical = [
    "playwright>=1.40.0",
]
```

In `/Users/gus/Git/scholar-mcp/src/scholar_mcp/config.py`, add dataclass fields (after `ranking_enrichment_timeout`):
```python
    # Medical subsystem and persistent SQLite cache settings
    cache_db_path: Path = field(
        default_factory=lambda: Path("~/.cache/scholar_mcp/cache.db").expanduser()
    )
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
```
And in `Settings.load()`'s `return cls(...)`, add (reuse the existing `_bool` helper):
```python
            cache_db_path=Path(
                os.getenv("SCHOLAR_CACHE_DB", "~/.cache/scholar_mcp/cache.db")
            ).expanduser(),
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

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/test_config_medical.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add pyproject.toml src/scholar_mcp/config.py tests/test_config_medical.py
git commit -m "feat(config): add medical and sqlite cache configuration settings"
```

---

### Task 2: Cross-Source Literature Deduplication Engine

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/utils/deduplication.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/utils/test_deduplication.py`

**Interfaces:**
- Consumes: standard `re`, `html`, `typing`.
- Produces: `normalize_title(title: str) -> str`, `calculate_similarity(s1: str, s2: str) -> float`, `extract_first_author(authors: list[str] | str | None) -> str | None`, `extract_year(date_str: str) -> str | None`, `are_duplicates(p1: dict, p2: dict, threshold: float = 0.9) -> bool`, `deduplicate_papers(papers: list[dict]) -> tuple[list[dict], dict]` (stats dict has keys `total_input`, `unique_count`, `duplicates_removed`).

- [ ] **Step 1: Write the failing test for deduplication**

Create `/Users/gus/Git/scholar-mcp/tests/utils/test_deduplication.py`:
```python
from scholar_mcp.utils.deduplication import (
    are_duplicates,
    calculate_similarity,
    deduplicate_papers,
    extract_first_author,
    extract_year,
    normalize_title,
)


def test_normalize_title():
    raw = "Efficacy of Metformin &amp; Diet in Type 2 Diabetes: [Preprint] Version 1"
    assert normalize_title(raw) == "efficacy of metformin & diet in type 2 diabetes"


def test_calculate_similarity():
    s1 = "treatment of hypertension in elderly patients"
    s2 = "treatment of hypertension in elderly patient"
    assert calculate_similarity(s1, s2) > 0.95
    assert calculate_similarity("", "abc") == 0.0
    assert calculate_similarity("exact", "exact") == 1.0


def test_extract_first_author():
    assert extract_first_author(["Smith J", "Doe A"]) == "smith"
    assert extract_first_author("Johnson, M. et al.") == "johnson"
    assert extract_first_author("J. Watson") == "watson"
    assert extract_first_author([]) is None


def test_extract_year():
    assert extract_year("2023-05-12") == "2023"
    assert extract_year("Published in 2021") == "2021"
    assert extract_year("Unknown") is None


def test_are_duplicates_doi_match():
    p1 = {"title": "Aspirin in Cardiovascular Disease", "doi": "10.1001/jama.2020.1",
          "authors": ["Smith J"], "year": "2020"}
    p2 = {"title": "Aspirin in cardiovascular disease", "doi": "10.1001/jama.2020.1",
          "authors": ["Smith, John"], "year": "2020"}
    assert are_duplicates(p1, p2) is True


def test_are_duplicates_fuzzy_match():
    p1 = {"title": "Treatment of Hypertension in Elderly Patients", "doi": None,
          "authors": ["Smith J"], "year": "2020"}
    p2 = {"title": "Treatment of Hypertension in Elderly Patient", "doi": None,
          "authors": ["Smith J"], "year": "2020"}
    assert are_duplicates(p1, p2) is True


def test_deduplicate_papers_keeps_richer_metadata():
    papers = [
        {"title": "Study A", "doi": "10.1000/1", "abstract": "Short"},
        {"title": "Study A", "doi": "10.1000/1", "abstract": "Detailed abstract with more text"},
        {"title": "Study B", "doi": "10.1000/2", "abstract": "Another study"},
    ]
    unique, stats = deduplicate_papers(papers)
    assert len(unique) == 2
    assert stats["duplicates_removed"] == 1
    assert stats["total_input"] == 3
    match_a = next(p for p in unique if p["title"] == "Study A")
    assert match_a["abstract"] == "Detailed abstract with more text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/utils/test_deduplication.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/utils/deduplication.py`**

```python
import html
import re
from difflib import SequenceMatcher  # replaced by Levenshtein below; see note


def normalize_title(title: str) -> str:
    text = html.unescape(title or "")
    text = re.sub(r"\[preprint\]", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"arXiv:\d+\.\d+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bversion\s+\d+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\bv\d+\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[-:.,;]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()
```
Note: the original uses Levenshtein distance similarity. Implement `_levenshtein(a: str, b: str) -> int` (classic DP matrix) and:
```python
def calculate_similarity(s1: str, s2: str) -> float:
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    return 1.0 - _levenshtein(s1, s2) / max(len(s1), len(s2))
```
Then `extract_first_author` (handles list input, `"Last, Initial"` comma form, `"Initial. Last"` form, all lowercase), `extract_year` (`re.search(r"\b(19|20)\d{2}\b", ...)`), `are_duplicates` (DOI equality first; then exact normalized-title equality with matching first author or year; then `calculate_similarity(normalized titles) >= threshold` with identical first author and year), and `deduplicate_papers` (O(n²) keep-list: for each paper, compare against kept papers with `are_duplicates`; on match, keep whichever record has richer metadata — has DOI wins, then longer abstract, then more authors; count removals into the stats dict).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/utils/test_deduplication.py -v`
Expected: PASS (all seven tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/utils/deduplication.py tests/utils/test_deduplication.py
git commit -m "feat(utils): add cross-source literature deduplication engine"
```

---

### Task 3: SQLite Multi-Tier Persistent Cache

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/utils/sqlite_cache.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/utils/test_sqlite_cache.py`

**Interfaces:**
- Consumes: `aiosqlite`, `json`, `time`, `pathlib.Path`, `asyncio`, `scholar_mcp.config.Settings`.
- Produces: `CacheMetadata(cached: bool, cache_age: int)` dataclass; `SQLiteCacheManager(db_path: Path, settings: Settings)`. Methods: `async init_db()`, `async get(key: str) -> tuple[Any | None, CacheMetadata]`, `async set(key: str, data: Any, source: str, ttl: int | None = None) -> None`, `async get_stats() -> dict[str, Any]`, `async close() -> None`. Lazy: first `get`/`set`/`get_stats` call opens the DB (WAL mode, parent dirs created) under an `asyncio.Lock`; `init_db()` is idempotent.

- [ ] **Step 1: Write the failing test for SQLite persistent cache**

Create `/Users/gus/Git/scholar-mcp/tests/utils/test_sqlite_cache.py`:
```python
import asyncio
from pathlib import Path

from scholar_mcp.config import Settings
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager


async def test_sqlite_cache_set_get_miss(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "test_cache.db", settings=Settings.load())
    await cache.init_db()

    val, meta = await cache.get("fda:search:aspirin")
    assert val is None
    assert meta.cached is False
    assert meta.cache_age == 0

    await cache.set("fda:search:aspirin", {"brand": "Aspirin", "ndc": "123"}, source="fda")

    val, meta = await cache.get("fda:search:aspirin")
    assert val is not None
    assert val["brand"] == "Aspirin"
    assert meta.cached is True
    assert meta.cache_age >= 0

    await cache.close()


async def test_sqlite_cache_lazy_init_without_explicit_init_db(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "lazy.db", settings=Settings.load())
    await cache.set("k", "v", source="fda")  # must open DB implicitly
    val, meta = await cache.get("k")
    assert val == "v"
    assert meta.cached is True
    await cache.close()


async def test_sqlite_cache_expiration(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "exp.db", settings=Settings.load())
    await cache.init_db()

    await cache.set("short_lived", {"data": 1}, source="fda", ttl=1)
    val, _ = await cache.get("short_lived")
    assert val == {"data": 1}

    await asyncio.sleep(1.1)
    val, meta = await cache.get("short_lived")
    assert val is None
    assert meta.cached is False

    await cache.close()


async def test_sqlite_cache_source_ttl_resolution(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "ttl.db", settings=Settings.load())
    await cache.init_db()
    # source "fda" resolves to settings.cache_ttl_fda; "unknown-source" falls back to cache_ttl_seconds
    await cache.set("a", 1, source="fda")
    await cache.set("b", 2, source="unknown-source")
    async with cache._db.execute(
        "SELECT key, ttl_seconds FROM cache_entries WHERE key IN ('a', 'b')"
    ) as cur:
        rows = {k: ttl for k, ttl in await cur.fetchall()}
    assert rows["a"] == Settings.load().cache_ttl_fda
    assert rows["b"] == Settings.load().cache_ttl_seconds
    await cache.close()


async def test_sqlite_cache_stats(tmp_path: Path):
    cache = SQLiteCacheManager(db_path=tmp_path / "stats.db", settings=Settings.load())
    await cache.init_db()

    await cache.set("k1", "v1", source="fda")
    await cache.set("k2", "v2", source="who")
    await cache.get("k1")  # hit
    await cache.get("missing")  # miss

    stats = await cache.get_stats()
    assert stats["total_entries"] == 2
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["sources"]["fda"] == 1
    assert stats["sources"]["who"] == 1

    await cache.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/utils/test_sqlite_cache.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/utils/sqlite_cache.py`**

```python
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from scholar_mcp.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS cache_entries (
    key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    last_accessed REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_source ON cache_entries(source);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(created_at, ttl_seconds);
CREATE INDEX IF NOT EXISTS idx_cache_lru ON cache_entries(last_accessed);
"""


@dataclass
class CacheMetadata:
    cached: bool
    cache_age: int


class SQLiteCacheManager:
    """Persistent async SQLite cache with per-source TTLs and LRU eviction.

    Constructed at module import; the database is opened lazily on first use
    (server.py instantiates clients before an event loop is running).
    """

    def __init__(self, db_path: Path, settings: Settings) -> None:
        self.db_path = db_path
        self.settings = settings
        self._db: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    def _ttl_for(self, source: str, ttl: int | None) -> int:
        if ttl is not None:
            return ttl
        by_source = {
            "fda": self.settings.cache_ttl_fda,
            "pubmed": self.settings.cache_ttl_pubmed,
            "who": self.settings.cache_ttl_who,
            "rxnorm": self.settings.cache_ttl_rxnorm,
            "guidelines": self.settings.cache_ttl_guidelines,
            "bright_futures": self.settings.cache_ttl_bright_futures,
            "aap_policy": self.settings.cache_ttl_aap_policy,
            "pediatric_journals": self.settings.cache_ttl_pediatric_journals,
            "child_health": self.settings.cache_ttl_child_health,
            "pediatric_drugs": self.settings.cache_ttl_pediatric_drugs,
            "clinical_trials": self.settings.cache_ttl_clinical_trials,
        }
        return by_source.get(source, self.settings.cache_ttl_seconds)
```
Implement `async _ensure_db()` (double-checked under `self._init_lock`: `mkdir(parents=True, exist_ok=True)` on `db_path.parent`, `aiosqlite.connect`, `PRAGMA journal_mode=WAL`, `executescript(SCHEMA)`, commit), `init_db()` delegating to it, `get()` (SELECT row, delete + miss on expiry, UPDATE `last_accessed` + hit on success, JSON-decode data, `cache_age = int(now - created_at)`), `set()` (INSERT OR REPLACE with resolved TTL; then if `SELECT COUNT(*)` exceeds `settings.cache_max_entries`, DELETE the oldest rows by `last_accessed` down to the limit), `get_stats()` (totals, per-source `GROUP BY source` dict, hits, misses, hit rate, `db_path.stat().st_size` as `db_size_bytes`), and `close()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/utils/test_sqlite_cache.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/utils/sqlite_cache.py tests/utils/test_sqlite_cache.py
git commit -m "feat(cache): add persistent multi-tier SQLite cache manager"
```

---

### Task 4: Medical Data Models and Output Formatters

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/__init__.py` (empty)
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/models.py`
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/formatters.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_models_formatters.py`

**Interfaces:**
- Consumes: `dataclasses`, `typing`, `scholar_mcp.utils.sqlite_cache.CacheMetadata`.
- Produces: dataclasses exactly as specified in spec §3 — `OpenFDAData`, `DrugLabel`, `RxNormDrug`, `WHOIndicatorRecord`, `GuidelineScore`, `ClinicalGuideline`, `PediatricGuideline`, `MedicalArticle` — each with `to_dict()` and a `from_dict(cls, data: dict)` classmethod (rehydrates cached JSON; nested `DrugLabel.openfda` reconstructs `OpenFDAData`). Formatters: `append_cache_info(text: str, meta: CacheMetadata) -> str`, `format_drug_search_results(drugs: list[DrugLabel], query: str, meta: CacheMetadata) -> dict` (`{"data": [...], "markdown": str}`), `format_drug_details(drug: DrugLabel | None, ndc: str, meta: CacheMetadata) -> dict`, `format_rxnorm_drugs(...)`, `format_health_indicators(...)`, `format_guidelines(...)`, `format_pediatric_guidelines(...)`, `format_medical_articles(...) -> dict` (same shape).
- Constraint: no safety banners or disclaimers; plain-text cache tags (`[Cached: 142s old]`, `[Fresh response]`); no emojis in any output.

- [ ] **Step 1: Write the failing test for models & formatters**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_models_formatters.py`:
```python
from scholar_mcp.medical.formatters import (
    append_cache_info,
    format_drug_search_results,
)
from scholar_mcp.medical.models import DrugLabel, OpenFDAData
from scholar_mcp.utils.sqlite_cache import CacheMetadata


def test_append_cache_info_no_emojis():
    fresh = append_cache_info("Result content", CacheMetadata(cached=False, cache_age=0))
    cached = append_cache_info("Result content", CacheMetadata(cached=True, cache_age=120))
    assert "[Fresh response]" in fresh
    assert "[Cached: 120s old]" in cached
    for out in (fresh, cached):
        assert not any(ch in out for ch in "🔄📦🚨⚠️")


def test_format_drug_search_results_no_safety_banner():
    drug = DrugLabel(
        openfda=OpenFDAData(
            brand_name=["Advil"], generic_name=["Ibuprofen"], manufacturer_name=["Pfizer"]
        ),
        effective_time="20230101",
        purpose=["Pain reliever"],
    )
    out = format_drug_search_results([drug], "advil", CacheMetadata(cached=False, cache_age=0))
    assert "Advil" in out["markdown"]
    assert "Ibuprofen" in out["markdown"]
    assert "[Fresh response]" in out["markdown"]
    assert "SAFETY" not in out["markdown"].upper()
    assert "🚨" not in out["markdown"]
    assert len(out["data"]) == 1
    assert out["data"][0]["openfda"]["brand_name"] == ["Advil"]


def test_drug_label_from_dict_roundtrip():
    drug = DrugLabel(openfda=OpenFDAData(brand_name=["Advil"]), purpose=["Pain reliever"])
    restored = DrugLabel.from_dict(drug.to_dict())
    assert restored.openfda.brand_name == ["Advil"]
    assert restored.purpose == ["Pain reliever"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_models_formatters.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `models.py` and `formatters.py`**

`models.py`: copy the dataclass definitions verbatim from spec §3, then add to each class:
```python
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DrugLabel":
        payload = dict(data)
        if "openfda" in payload:
            payload["openfda"] = OpenFDAData.from_dict(payload["openfda"])
        if "score_details" in payload and isinstance(payload["score_details"], dict):
            payload["score_details"] = GuidelineScore.from_dict(payload["score_details"])
        return cls(**{k: v for k, v in payload.items() if k in cls.__dataclass_fields__})
```
(same pattern for the other classes; `OpenFDAData.from_dict`/`GuidelineScore.from_dict` filter to known fields).

`formatters.py` core:
```python
def append_cache_info(text: str, meta: CacheMetadata) -> str:
    if meta.cached:
        return f"{text}\n\n[Cached: {meta.cache_age}s old]"
    return f"{text}\n\n[Fresh response]"


def format_drug_search_results(drugs, query, meta):
    lines = [f"## Drug Search Results: {query}", ""]
    for drug in drugs:
        of = drug.openfda
        brand = ", ".join(of.brand_name) or "Unknown"
        generic = ", ".join(of.generic_name) or "N/A"
        maker = ", ".join(of.manufacturer_name) or "N/A"
        purpose = "; ".join(drug.purpose) or "N/A"
        lines.append(f"- **{brand}** ({generic}) — {maker}. Purpose: {purpose}")
    markdown = append_cache_info("\n".join(lines), meta)
    return {"data": [d.to_dict() for d in drugs], "markdown": markdown}
```
Implement `format_drug_details` (single label, all populated sections as markdown bullet groups), `format_rxnorm_drugs`, `format_health_indicators`, `format_guidelines`, `format_pediatric_guidelines`, `format_medical_articles` with the same `{"data", "markdown"}` shape and cache-tag suffix. None of them emit warnings, disclaimers, or emojis.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_models_formatters.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/__init__.py src/scholar_mcp/medical/models.py src/scholar_mcp/medical/formatters.py tests/medical/test_models_formatters.py
git commit -m "feat(medical): add medical data models and clean formatters"
```

---

### Task 5: Medical PubMed Client (esearch + efetch + XML Parse)

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/pubmed.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_pubmed.py`

**Interfaces:**
- Consumes: `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.utils.deduplication.deduplicate_papers`, `scholar_mcp.medical.models.MedicalArticle`, `bs4.BeautifulSoup` (`lxml-xml`).
- Produces: `MedicalPubMedClient(http_client: AsyncHttpClient, cache: SQLiteCacheManager, settings: Settings)` with `async search_articles(query: str, max_results: int = 10) -> tuple[list[MedicalArticle], CacheMetadata]`; module-level `parse_pubmed_xml(xml_text: str) -> list[MedicalArticle]`. Cache source: `"pubmed"`; key `f"pubmed:search:{query}:{max_results}"`. NOTE: this is a separate client from `scholar_mcp.providers.pubmed.PubMedProvider` — that provider's `search()` returns no abstracts and cannot back guideline scoring.

- [ ] **Step 1: Write the failing test for the medical PubMed client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_pubmed.py`:
```python
from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.pubmed import MedicalPubMedClient, parse_pubmed_xml
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

PUBMED_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345</PMID>
      <Article>
        <Journal><Title>New England Journal of Medicine</Title></Journal>
        <ArticleTitle>Metformin efficacy in type 2 diabetes</ArticleTitle>
        <Abstract><AbstractText>Biguanide therapy lowers HbA1c.</AbstractText></Abstract>
        <AuthorList>
          <Author><ForeName>John</ForeName><LastName>Smith</LastName></Author>
          <Author><CollectiveName>Diabetes Research Group</CollectiveName></Author>
        </AuthorList>
        <ArticleDate><Year>2021</Year></ArticleDate>
        <ELocationID EIdType="doi">10.1056/NEJMoa2021</ELocationID>
        <ArticleIdList><ArticleId IdType="pmc">PMC7123456</ArticleId></ArticleIdList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


async def test_parse_pubmed_xml():
    articles = parse_pubmed_xml(PUBMED_XML)
    assert len(articles) == 1
    a = articles[0]
    assert a.pmid == "12345"
    assert a.title == "Metformin efficacy in type 2 diabetes"
    assert a.abstract == "Biguanide therapy lowers HbA1c."
    assert a.authors == ["John Smith", "Diabetes Research Group"]
    assert a.journal == "New England Journal of Medicine"
    assert a.year == "2021"
    assert a.doi == "10.1056/NEJMoa2021"
    assert a.pmc_id == "7123456"


@respx.mock
async def test_search_articles_flow(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(ESEARCH_URL).respond(
        json={"esearchresult": {"idlist": ["12345"]}}
    )
    respx.get(EFETCH_URL).respond(content=PUBMED_XML.encode())

    articles, meta = await client.search_articles("metformin", max_results=5)
    assert len(articles) == 1
    assert articles[0].pmid == "12345"
    assert meta.cached is False

    # Second call hits the cache (no new HTTP traffic needed)
    articles2, meta2 = await client.search_articles("metformin", max_results=5)
    assert meta2.cached is True
    assert articles2[0].to_dict() == articles[0].to_dict()

    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_articles_empty_id_list(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(ESEARCH_URL).respond(json={"esearchresult": {"idlist": []}})

    articles, meta = await client.search_articles("nothing")
    assert articles == []
    await cache.close()
    await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_pubmed.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/pubmed.py`**

```python
ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def parse_pubmed_xml(xml_text: str) -> list[MedicalArticle]:
    soup = BeautifulSoup(xml_text, "lxml-xml")
    articles: list[MedicalArticle] = []
    for citation in soup.find_all("PubmedArticle"):
        pmid_elem = citation.find("PMID")
        if pmid_elem is None or not pmid_elem.get_text(strip=True):
            continue
        title_elem = citation.find("ArticleTitle")
        abstract_elem = citation.find("AbstractText")
        authors: list[str] = []
        for author in citation.find_all("Author"):
            collective = author.find("CollectiveName")
            if collective is not None and collective.get_text(strip=True):
                authors.append(collective.get_text(strip=True))
                continue
            last = author.find("LastName")
            fore = author.find("ForeName")
            if last is not None:
                if fore is not None:
                    authors.append(f"{fore.get_text(strip=True)} {last.get_text(strip=True)}")
                else:
                    authors.append(last.get_text(strip=True))
        journal_elem = citation.find("Journal")
        title = title_elem.get_text(" ", strip=True) if title_elem is not None else ""
        doi = None
        eloc = citation.find("ELocationID", EIdType="doi")
        if eloc is not None:
            doi = eloc.get_text(strip=True)
        pmc_id = None
        aid = citation.find("ArticleId", IdType="pmc")
        if aid is not None:
            pmc_id = re.sub(r"^PMC", "", aid.get_text(strip=True), flags=re.IGNORECASE)
        year = ""
        year_elem = citation.find("Year")
        if year_elem is not None:
            year = year_elem.get_text(strip=True)
        articles.append(MedicalArticle(
            title=title, abstract=abstract_elem.get_text(" ", strip=True) if abstract_elem is not None else "",
            authors=authors,
            journal=journal_elem.find("Title").get_text(strip=True)
            if journal_elem is not None and journal_elem.find("Title") is not None else "",
            year=year, pmid=pmid_elem.get_text(strip=True), pmc_id=pmc_id, doi=doi,
        ))
    return articles
```
`MedicalPubMedClient.search_articles`: check cache first (rehydrate via `MedicalArticle.from_dict`); on miss, `esearch` (params `db=pubmed`, `term=query`, `retmode=json`, `retmax=max_results`) — a `None` response or empty `idlist` returns `([], CacheMetadata(cached=False, cache_age=0))` after caching the empty result; otherwise `efetch` (params `db=pubmed`, `id=",".join(ids)`, `retmode=xml`), parse, run `deduplicate_papers([a.to_dict() for a in parsed])`, rehydrate, cache under source `"pubmed"`, and return.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_pubmed.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/pubmed.py tests/medical/test_pubmed.py
git commit -m "feat(medical): add medical PubMed client with efetch XML parsing"
```

---

### Task 6: openFDA Client & Pediatric Drug Filtering

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/fda.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_fda.py`

**Interfaces:**
- Consumes: `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.DrugLabel`, `OpenFDAData`.
- Produces: `FDAClient(http_client, cache, settings)`. Methods: `async search_drugs(query: str, limit: int = 10) -> tuple[list[DrugLabel], CacheMetadata]` (cache source `"fda"`, key `f"fda:search:{query}:{limit}"`), `async get_drug_by_ndc(ndc: str) -> tuple[DrugLabel | None, CacheMetadata]` (key `f"fda:ndc:{ndc}"`), `async search_pediatric_drugs(query: str, limit: int = 10) -> tuple[list[DrugLabel], CacheMetadata]` (cache source `"pediatric_drugs"`, key `f"pediatric_drugs:{query}:{limit}"`). Module-level `is_valid_drug_query(query: str) -> bool`.

- [ ] **Step 1: Write the failing test for the FDA client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_fda.py`:
```python
from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.fda import FDAClient, is_valid_drug_query
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

FDA_URL = "https://api.fda.gov/drug/label.json"


def _label_payload(brand, generic=None, ndc="50580-488", dosage=None):
    return {
        "results": [
            {
                "openfda": {
                    "brand_name": [brand],
                    "generic_name": [generic] if generic else [],
                    "manufacturer_name": ["Johnson & Johnson"],
                    "product_ndc": [ndc],
                },
                "effective_time": "20230101",
                "purpose": ["Pain reliever/fever reducer"],
                "dosage_and_administration": [dosage] if dosage else [],
            }
        ]
    }


async def _make_client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = FDAClient(http_client=http_client, cache=cache, settings=settings)
    return client, cache, http_client


def test_is_valid_drug_query():
    assert is_valid_drug_query("medication") is False
    assert is_valid_drug_query("pill") is False
    assert is_valid_drug_query("ab") is False
    assert is_valid_drug_query("aspirin") is True


@respx.mock
async def test_search_drugs_success(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(json=_label_payload("Tylenol", "Acetaminophen"))

    drugs, meta = await client.search_drugs("tylenol", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name == ["Tylenol"]
    assert drugs[0].openfda.generic_name == ["Acetaminophen"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drugs_invalid_query_returns_empty(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    drugs, meta = await client.search_drugs("medication")
    assert drugs == []
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_get_drug_by_ndc(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(json=_label_payload("Advil", ndc="0573-0164"))

    drug, meta = await client.get_drug_by_ndc("0573-0164")
    assert drug is not None
    assert drug.openfda.brand_name == ["Advil"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_drugs(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload(
            "Children's Motrin",
            generic="Ibuprofen",
            ndc="50580-601",
            dosage="Pediatric dosing: 10mg/kg every 6-8 hours for children",
        )
    )

    drugs, meta = await client.search_pediatric_drugs("motrin", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name == ["Children's Motrin"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_drugs_filters_adult_labels(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload("Adult Formula", dosage="Take one tablet with water")
    )

    drugs, meta = await client.search_pediatric_drugs("adult formula", limit=5)
    assert drugs == []
    await cache.close()
    await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_fda.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/fda.py`**

```python
FDA_LABEL_URL = "https://api.fda.gov/drug/label.json"
COMMON_DRUG_WORDS = {
    "medication", "medicine", "drug", "pill", "tablet", "capsule",
    "injection", "dose", "dosage",
}
PEDIATRIC_TERMS = ("pediatric", "child", "infant", "neonatal", "pediatric dosing")


def is_valid_drug_query(query: str) -> bool:
    trimmed = query.strip()
    lower = trimmed.lower()
    if lower in COMMON_DRUG_WORDS:
        return False
    if len(trimmed) < 3:
        return False
    if re.fullmatch(r"[a-z]+-\d+", lower) or re.search(r"\d{3,}", trimmed):
        return len(trimmed) >= 5
    return True
```
`_parse_drug_label(raw: dict) -> DrugLabel`: map `raw["openfda"]` sublists into `OpenFDAData` (missing keys to empty lists), the known string-list sections (`purpose`, `warnings`, `adverse_reactions`, `drug_interactions`, `dosage_and_administration`, `indications_and_usage`, `contraindications`, `use_in_specific_populations`, `clinical_pharmacology`) into their fields, `effective_time`, and store the full raw payload in `raw_sections`.

`search_drugs`: validate (return `([], CacheMetadata(False, 0))` when invalid); check cache; on miss, iterate the four layered search terms sequentially —
```python
search_queries = [
    f'openfda.brand_name:"{query}"',
    f'openfda.generic_name:"{query}"',
    f'openfda.substance_name:"{query}"',
    f"openfda.brand_name:{query}",
]
```
— with `GET FDA_LABEL_URL, params={"search": term, "limit": limit}`; treat a `None` response as no results for that layer and continue; dedupe by first `product_ndc`; stop when `len(results) >= limit`; cache and return parsed labels.

`get_drug_by_ndc`: `params={"search": f"openfda.product_ndc:{ndc}", "limit": 1}`; first result or `None`.

`search_pediatric_drugs`: `base, _ = await self.search_drugs(query, limit * 2)`; filter labels where any joined-lowercase section (`purpose`, `warnings`, `dosage_and_administration`) contains a `PEDIATRIC_TERMS` entry; slice to `limit`; cache under source `"pediatric_drugs"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_fda.py -v`
Expected: PASS (all five tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/fda.py tests/medical/test_fda.py
git commit -m "feat(medical): implement openFDA client and pediatric drug filtering"
```

---

### Task 7: RxNorm Drug Nomenclature Client

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/rxnorm.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_rxnorm.py`

**Interfaces:**
- Consumes: `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.RxNormDrug`.
- Produces: `RxNormClient(http_client, cache, settings)` with `async search_drug_nomenclature(query: str) -> tuple[list[RxNormDrug], CacheMetadata]` (cache source `"rxnorm"`, key `f"rxnorm:{query}"`).

- [ ] **Step 1: Write the failing test for the RxNorm client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_rxnorm.py`:
```python
from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.rxnorm import RxNormClient
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

RXNORM_URL = "https://rxnav.nlm.nih.gov/REST/drugs.json"


@respx.mock
async def test_search_drug_nomenclature(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = RxNormClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(RXNORM_URL).respond(
        json={
            "drugGroup": {
                "conceptGroup": [
                    {
                        "conceptProperties": [
                            {
                                "rxcui": "161",
                                "name": "Acetaminophen",
                                "synonym": "APAP",  # string, not list
                                "tty": "IN",
                                "language": "ENG",
                                "suppress": "N",
                                "umlscui": "C0000970",  # string, not list
                            }
                        ]
                    },
                    {"conceptProperties": []},  # empty group is skipped
                    {"noProperties": True},  # group without properties is skipped
                ]
            }
        }
    )

    drugs, meta = await client.search_drug_nomenclature("acetaminophen")
    assert len(drugs) == 1
    assert drugs[0].rxcui == "161"
    assert drugs[0].name == "Acetaminophen"
    assert drugs[0].tty == "IN"
    assert drugs[0].synonyms == ["APAP"]
    assert drugs[0].umlscui == ["C0000970"]

    await cache.close()
    await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_rxnorm.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/rxnorm.py`**

```python
RXNORM_DRUGS_URL = "https://rxnav.nlm.nih.gov/REST/drugs.json"


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]
```
`search_drug_nomenclature`: cache-first; on miss `GET RXNORM_DRUGS_URL, params={"name": query}`; a `None` response returns `([], CacheMetadata(False, 0))`; traverse `body["drugGroup"]["conceptGroup"]` (guard every level with `.get(..., [])`), and for each `conceptProperties` entry build `RxNormDrug(rxcui=str(c.get("rxcui", "")), name=c.get("name", ""), tty=c.get("tty", ""), language=c.get("language", ""), suppress=c.get("suppress", ""), synonyms=_as_list(c.get("synonym")), umlscui=_as_list(c.get("umlscui")))`; cache and return.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_rxnorm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/rxnorm.py tests/medical/test_rxnorm.py
git commit -m "feat(medical): implement RxNorm nomenclature client"
```

---

### Task 8: WHO Global Health Observatory Client

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/who.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_who.py`

**Interfaces:**
- Consumes: `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.WHOIndicatorRecord`.
- Produces: `WHOClient(http_client, cache, settings)`. Methods: `async get_health_statistics(indicator: str, country: str | None = None, limit: int = 10) -> tuple[list[WHOIndicatorRecord], CacheMetadata]` (cache source `"who"`, key `f"who:{indicator}:{country}:{limit}"`), `async get_child_health_statistics(indicator: str, country: str | None = None, limit: int = 10) -> tuple[list[WHOIndicatorRecord], CacheMetadata]` (cache source `"child_health"`). Module-level `indicator_variations(indicator_name: str) -> list[str]` and `WHO_CHILD_HEALTH_INDICATORS = ["MDG_0000000029", ..., "WHS9_86"]`.

- [ ] **Step 1: Write the failing test for the WHO client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_who.py`:
```python
from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.who import WHOClient, indicator_variations
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

GHO_BASE = "https://ghoapi.azureedge.net/api"


def _client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    return (
        WHOClient(http_client=http_client, cache=cache, settings=settings),
        cache,
        http_client,
    )


def test_indicator_variations():
    assert "expectancy" in indicator_variations("life expectancy")
    assert "life expectancy" in indicator_variations("Life Expectancy")  # original term kept
    assert indicator_variations("made-up-metric") == ["made-up-metric"]


@respx.mock
async def test_get_health_statistics(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)
    respx.get(f"{GHO_BASE}/Indicator").respond(
        json={
            "value": [
                {"IndicatorCode": "WHOSIS_000001", "IndicatorName": "Life expectancy at birth (years)"}
            ]
        }
    )
    respx.get(f"{GHO_BASE}/WHOSIS_000001").respond(
        json={
            "value": [
                {
                    "SpatialDim": "USA",
                    "TimeDim": "2020",
                    "NumericValue": 78.5,
                    "Unit": "years",
                    "Sex": "BTSX",
                },
                {
                    "SpatialDim": "USA",
                    "TimeDim": "2019",
                    "NumericValue": 78.3,
                    "Unit": "years",
                    "Sex": "BTSX",
                },
            ]
        }
    )

    records, meta = await client.get_health_statistics("life expectancy", country="USA", limit=5)
    assert records[0].indicator_code == "WHOSIS_000001"
    assert records[0].numeric_value == 78.5  # most recent year kept per country
    assert records[0].spatial_dim == "USA"
    assert records[0].unit == "years"
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_indicator_discovery_falls_back_to_variations(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)

    indicator_route = respx.get(f"{GHO_BASE}/Indicator")
    # First filter (primary term): empty. Variation filters then hit.
    indicator_route.side_effect = [
        respx.MockResponse(json={"value": []}),
        respx.MockResponse(
            json={"value": [{"IndicatorCode": "WHS9_86", "IndicatorName": "Exclusive breastfeeding"}]}
        ),
    ]
    respx.get(f"{GHO_BASE}/WHS9_86").respond(
        json={"value": [{"SpatialDim": "USA", "TimeDim": "2021", "NumericValue": 0.42}]}
    )

    records, meta = await client.get_health_statistics("breastfeeding")
    assert records[0].indicator_code == "WHS9_86"
    assert indicator_route.call_count == 2
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_get_child_health_statistics(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)
    respx.get(f"{GHO_BASE}/MDG_0000000029").respond(
        json={"value": [{"SpatialDim": "USA", "TimeDim": "2021", "NumericValue": 6.2}]}
    )
    # All other child-health codes return empty values.
    for code in ("MDG_0000000030", "MDG_0000000031", "MDG_0000000032", "MDG_0000000033",
                 "MDG_0000000034", "WHS4_544", "WHS9_86"):
        respx.get(f"{GHO_BASE}/{code}").respond(json={"value": []})

    records, meta = await client.get_child_health_statistics("mortality", limit=5)
    assert records[0].indicator_code == "MDG_0000000029"
    assert records[0].numeric_value == 6.2
    await cache.close()
    await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_who.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/who.py`**

```python
WHO_API_BASE = "https://ghoapi.azureedge.net/api"

INDICATOR_SYNONYMS: dict[str, list[str]] = {
    "maternal mortality": ["maternal", "mortality", "maternal death"],
    "infant mortality": ["infant", "mortality", "infant death", "child mortality"],
    "life expectancy": ["life expectancy", "expectancy", "life"],
    "mortality rate": ["mortality", "death rate", "mortality rate"],
    "birth rate": ["birth", "fertility", "birth rate"],
    "death rate": ["death", "mortality", "death rate"],
    "population": ["population", "demographics"],
    "health expenditure": ["health", "expenditure", "spending"],
    "immunization": ["immunization", "vaccination", "vaccine"],
    "malnutrition": ["malnutrition", "nutrition", "undernutrition"],
    "diabetes": ["diabetes", "diabetic"],
    "hypertension": ["hypertension", "blood pressure", "high blood pressure"],
    "cancer": ["cancer", "neoplasm", "tumor"],
    "hiv": ["hiv", "aids", "hiv/aids"],
    "tuberculosis": ["tuberculosis", "tb"],
    "malaria": ["malaria"],
    "obesity": ["obesity", "overweight"],
}

WHO_CHILD_HEALTH_INDICATORS = [
    "MDG_0000000029",  # Under-five mortality rate
    "MDG_0000000030",  # Infant mortality rate
    "MDG_0000000031",  # Neonatal mortality rate
    "MDG_0000000032",  # Child mortality rate (1-4 years)
    "MDG_0000000033",  # Measles immunization coverage
    "MDG_0000000034",  # DPT3 immunization coverage
    "WHS4_544",        # Child malnutrition
    "WHS9_86",         # Exclusive breastfeeding
]


def indicator_variations(indicator_name: str) -> list[str]:
    lower = indicator_name.lower()
    variations: list[str] = []
    for key, values in INDICATOR_SYNONYMS.items():
        if key in lower:
            variations.extend(values)
    variations.extend([indicator_name, lower])
    return list(dict.fromkeys(variations))  # dedupe, keep order
```
`get_health_statistics`: cache-first; on miss, (1) `GET {WHO_API_BASE}/Indicator` with `params={"$filter": f"contains(IndicatorName, '{indicator}')", "$format": "json"}`; if the value list is empty, retry each variation from `indicator_variations(indicator)` until one returns results; (2) for up to 3 matched indicator codes, `GET {WHO_API_BASE}/{code}` with `params={"$format": "json", "$top": 50}` plus `"$filter": f"SpatialDim eq '{country}'"` when a country is given; (3) group each indicator's rows by `SpatialDim` keeping the row with the max `TimeDim`, build `WHOIndicatorRecord(indicator_code=code, indicator_name=..., spatial_dim=row.get("SpatialDim") or "Global", time_dim=str(row.get("TimeDim", "")), numeric_value=row.get("NumericValue"), value=str(row.get("NumericValue", "")), unit=row.get("Unit", ""), sex=row.get("Sex", ""), ...)`, sort by recency, cap at `limit`, cache, return. A `None` response at any step yields the empty result (cached).

`get_child_health_statistics`: cache-first (source `"child_health"`); query each code in `WHO_CHILD_HEALTH_INDICATORS` (params as above with optional country filter), convert rows the same way, apply the child age-group filter (port `extractAgeGroup`: match `0-18 years`, `under-five`, `infant`, `neonatal`, `child` in the indicator name, defaulting to keep), cap at `limit`, cache, return.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_who.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/who.py tests/medical/test_who.py
git commit -m "feat(medical): implement WHO Global Health Observatory client"
```

---

### Task 9: ClinicalTrials.gov Client

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/clinical_trials.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_clinical_trials.py`

**Interfaces:**
- Consumes: `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.MedicalArticle`.
- Produces: `ClinicalTrialsClient(http_client, cache, settings)` with `async search_clinical_trials(query: str, limit: int = 10) -> tuple[list[MedicalArticle], CacheMetadata]` (cache source `"clinical_trials"`, key `f"clinical_trials:{query}:{limit}"`). No dedicated MCP tool — feeds `search_medical_databases` only.

- [ ] **Step 1: Write the failing test for the ClinicalTrials client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_clinical_trials.py`:
```python
from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

CT_URL = "https://clinicaltrials.gov/api/v2/studies"


@respx.mock
async def test_search_clinical_trials(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(CT_URL).respond(
        json={
            "studies": [
                {
                    "protocolSection": {
                        "identificationModule": {
                            "nctId": "NCT01234567",
                            "briefTitle": "Evaluation of Drug X in Asthma",
                            "briefSummary": "This study evaluates safety and efficacy.",
                            "leadSponsor": {"name": "National Institute of Health"},
                        },
                        "descriptionModule": {"briefSummary": "This study evaluates safety and efficacy."},
                        "statusModule": {"startDateStruct": {"date": "2021-01"}},
                    }
                }
            ]
        }
    )

    articles, meta = await client.search_clinical_trials("asthma", limit=5)
    assert len(articles) == 1
    assert articles[0].title == "Evaluation of Drug X in Asthma"
    assert articles[0].authors == ["National Institute of Health"]
    assert articles[0].journal == "ClinicalTrials.gov"
    assert articles[0].year == "2021-01"
    assert articles[0].url == "https://clinicaltrials.gov/study/NCT01234567"
    assert articles[0].source_database == "ClinicalTrials.gov"
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_clinical_trials_handles_missing_fields(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(CT_URL).respond(
        json={"studies": [{"protocolSection": {"identificationModule": {"nctId": "NCT0000001"}}}]}
    )

    articles, meta = await client.search_clinical_trials("asthma")
    assert len(articles) == 1
    assert articles[0].title == "Clinical Trial"  # fallback title
    assert articles[0].authors == []
    await cache.close()
    await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_clinical_trials.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/clinical_trials.py`**

Cache-first; on miss `GET https://clinicaltrials.gov/api/v2/studies` with `params={"query": query, "format": "json", "limit": limit}`; a `None` response returns `([], CacheMetadata(False, 0))`. Per study, guard every nested level:
```python
ps = study.get("protocolSection") or {}
im = ps.get("identificationModule") or {}
status = ps.get("statusModule") or {}
lead = (im.get("leadSponsor") or {}).get("name")
articles.append(MedicalArticle(
    title=im.get("briefTitle") or im.get("officialTitle") or "Clinical Trial",
    authors=[lead] if lead else [],
    abstract=im.get("briefSummary", ""),
    journal="ClinicalTrials.gov",
    year=(status.get("startDateStruct") or {}).get("date", ""),
    url=f"https://clinicaltrials.gov/study/{im.get('nctId', '')}",
    source_database="ClinicalTrials.gov",
))
```
Cache under source `"clinical_trials"` and return.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_clinical_trials.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/clinical_trials.py tests/medical/test_clinical_trials.py
git commit -m "feat(medical): implement ClinicalTrials.gov v2 client"
```

---

### Task 10: Clinical Guidelines Search & Heuristic Scoring Engine

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/guidelines.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_guidelines.py`

**Interfaces:**
- Consumes: `scholar_mcp.medical.pubmed.MedicalPubMedClient` (NOT `scholar_mcp.providers.pubmed.PubMedProvider`), `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.MedicalArticle`, `ClinicalGuideline`, `GuidelineScore`.
- Produces: module-level `calculate_guideline_score(article: MedicalArticle, has_publication_type: bool) -> GuidelineScore`, `extract_organization(article: MedicalArticle) -> str`; `GuidelinesEngine(pubmed: MedicalPubMedClient, cache: SQLiteCacheManager, settings: Settings)` with `async search_clinical_guidelines(query: str, organization: str | None = None) -> tuple[list[ClinicalGuideline], CacheMetadata]` (cache source `"guidelines"`, key `f"guidelines:{query}:{organization}"`).

- [ ] **Step 1: Write the failing test for the guidelines engine**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_guidelines.py`:
```python
from scholar_mcp.medical.guidelines import calculate_guideline_score, extract_organization
from scholar_mcp.medical.models import MedicalArticle


def test_calculate_guideline_score():
    article = MedicalArticle(
        title="American Heart Association Clinical Practice Guideline for Hypertension",
        authors=["Whelton PK"],
        journal="Journal of the American College of Cardiology",
        abstract="Evidence-based recommendation and consensus for blood pressure management.",
        pmid="12345",
    )
    score = calculate_guideline_score(article, has_publication_type=True)
    assert score.publication_type == 2.0
    assert score.title_keywords == 1.0      # "guideline" in title
    assert score.journal_reputation == 1.0  # "journal of the american"
    assert score.author_affiliation == 1.0   # org extracted (journal fallback)
    assert score.abstract_keywords == 1.0    # 3 keywords hit, capped at 2 * 0.5
    assert score.mesh_terms == 0.0           # reserved weight, never awarded
    assert score.total == 6.0


def test_calculate_guideline_score_rejects_low_scores():
    article = MedicalArticle(
        title="A random case report",
        journal="Some Journal",
        abstract="An interesting case.",
    )
    score = calculate_guideline_score(article, has_publication_type=False)
    assert score.total < 2.5


def test_extract_organization():
    article = MedicalArticle(
        title="Management of Asthma",
        journal="Pediatrics",
        abstract="Official statement from the American Academy of Pediatrics on pediatric asthma care.",
    )
    assert "American Academy of Pediatrics" in extract_organization(article)
```

And an engine-level test in the same file:
```python
from pathlib import Path
from unittest.mock import AsyncMock

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.guidelines import GuidelinesEngine
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


@respx.mock
async def test_search_clinical_guidelines_layers(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    pubmed = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    engine = GuidelinesEngine(pubmed=pubmed, cache=cache, settings=settings)

    respx.get(ESEARCH_URL).respond(json={"esearchresult": {"idlist": ["999"]}})
    respx.get(EFETCH_URL).respond(content=(
        "<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>999</PMID>"
        "<Article><Journal><Title>Lancet</Title></Journal>"
        "<ArticleTitle>Clinical practice guideline for asthma</ArticleTitle>"
        "<Abstract><AbstractText>Guideline recommendations.</AbstractText></Abstract>"
        "<ELocationID EIdType='doi'>10.1/g</ELocationID>"
        "</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>"
    ).encode())

    guidelines, meta = await engine.search_clinical_guidelines("asthma")
    assert len(guidelines) >= 1
    assert guidelines[0].score >= 2.5
    assert guidelines[0].pmid == "999"
    await cache.close()
    await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_guidelines.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/guidelines.py`**

Constants (verbatim from original `constants.ts`):
```python
GUIDELINE_PUBLICATION_TYPES = [
    '"practice guideline"[pt]',
    '"guideline"[pt]',
    '"consensus development conference"[pt]',
    '"consensus development conference, nih"[pt]',
    '"technical report"[pt]',
]
GUIDELINE_KEYWORDS = [
    "guideline", "recommendation", "consensus", "position statement",
    "standard of care", "best practice", "evidence-based", "expert consensus",
]
KNOWN_GUIDELINE_JOURNALS = [
    "journal of the american", "new england journal", "lancet",
    "bmj", "annals of", "guidelines", "recommendations",
]
ORG_EXTRACTION_PATTERNS = [
    r"(American|European|National|International|World|Global).*?"
    r"(Association|College|Society|Academy|Institute|Foundation|Organization|Committee|Ministry)",
    r"(World Health Organization|WHO)",
    r"(Centers for Disease Control|CDC)",
    r"(National Institutes of Health|NIH)",
]
ORG_ABBREVIATIONS = {
    "aap": ["american academy of pediatrics", "american academy pediatric"],
    "who": ["world health organization"],
    "cdc": ["centers for disease control"],
    "aha": ["american heart association"],
    "acc": ["american college of cardiology"],
    "ada": ["american diabetes association"],
    "acp": ["american college of physicians"],
}
MIN_SCORE_THRESHOLD = 2.5
LAYER_THRESHOLD = 5
```
`extract_organization(article)`: start `"Unknown Organization"`; if `article.journal` is non-empty use it; if `article.abstract` matches any pattern (case-insensitive `re.search`), return that match; if still unknown and `article.title` matches, return that match.

`calculate_guideline_score`: exactly the spec §5.5 pseudocode (publication_type 2.0 when `has_publication_type`; title_keywords 1.0 once; abstract_keywords +0.5 per keyword capped at 1.0; journal_reputation 1.0 on substring match; author_affiliation 1.0 when `extract_organization(article) != "Unknown Organization"`; mesh_terms 0.0; total = sum).

`GuidelinesEngine.search_clinical_guidelines`: cache-first; on miss run Layer 1 — `await self.pubmed.search_articles(f"({query}) AND ({' OR '.join(GUIDELINE_PUBLICATION_TYPES)})", max_results=20)` — tracking `(article, has_pub_type=True)`; if fewer than `LAYER_THRESHOLD` articles, run Layer 2 with `f"({query}) AND ({' OR '.join(f'{kw}[tiab]' for kw in GUIDELINE_KEYWORDS[:5])})"` and append unseen PMIDs with `has_pub_type=False`. Apply the organization filter (case-insensitive containment in org/title/abstract/journal OR `ORG_ABBREVIATIONS` alias expansion) when provided. Score each article, keep `score.total >= MIN_SCORE_THRESHOLD`, build `ClinicalGuideline(title=article.title, organization=extract_organization(article), year=article.year, url=f"https://pubmed.ncbi.nlm.nih.gov/{article.pmid}/", description=(article.abstract or "")[:300], pmid=article.pmid, score=score.total, score_details=score)`, sort descending by score, cache, return.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_guidelines.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/guidelines.py tests/medical/test_guidelines.py
git commit -m "feat(medical): implement clinical guidelines search and heuristic scoring engine"
```

---

### Task 11: Pediatric Scraping & Literature Engine

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/pediatrics.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_pediatrics.py`

**Interfaces:**
- Consumes: `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.PediatricGuideline`, `MedicalArticle`, `scholar_mcp.medical.pubmed.MedicalPubMedClient`.
- Produces: `PediatricsEngine(http_client, cache, settings, pubmed: MedicalPubMedClient | None = None, jitter_range: tuple[float, float] | None = (1.0, 3.0))`. Methods: `async search_bright_futures(query: str) -> tuple[list[PediatricGuideline], CacheMetadata]` (source `"bright_futures"`), `async search_aap_policy(query: str) -> tuple[list[PediatricGuideline], CacheMetadata]` (source `"aap_policy"`), `async search_aap_guidelines(query: str) -> tuple[list[PediatricGuideline], CacheMetadata]`, `async search_pediatric_literature(query: str, max_results: int = 10) -> tuple[list[MedicalArticle], CacheMetadata]` (source `"pediatric_journals"`). Tests construct the engine with `jitter_range=None` to skip the anti-bot delay.

- [ ] **Step 1: Write the failing test for the pediatrics engine**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_pediatrics.py`:
```python
from pathlib import Path

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.pediatrics import PediatricsEngine
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

BF_URL = "https://brightfutures.aap.org/Search"
AAP_URL = "https://publications.aap.org/pediatrics/search"


async def _engine(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = PediatricsEngine(
        http_client=http_client, cache=cache, settings=settings, jitter_range=None
    )
    return engine, cache, http_client


@respx.mock
async def test_search_bright_futures_html(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    respx.get(BF_URL).respond(html="""
    <html><body>
      <div class="search-result">
        <h3 class="title"><a href="/guidelines/infant-nutrition">
          Infant Nutrition Guidelines (0-12 months)</a></h3>
        <p class="description">Recommendations on breastfeeding and complementary feeding.</p>
      </div>
      <div class="search-result">
        <h3 class="title"><a href="/x">No</a></h3>
      </div>
    </body></html>
    """)

    guidelines, meta = await engine.search_bright_futures("nutrition")
    assert len(guidelines) == 1  # short-title item dropped
    assert "Infant Nutrition" in guidelines[0].title
    assert guidelines[0].source == "bright-futures"
    assert guidelines[0].organization == "American Academy of Pediatrics"
    assert guidelines[0].category == "Preventive Care"
    assert "0-12 months" in guidelines[0].age_group
    assert guidelines[0].url.startswith("https://brightfutures.aap.org/")
    assert len(guidelines[0].description) <= 300
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_aap_policy_html(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    respx.get(AAP_URL).respond(html="""
    <html><body>
      <article class="publication-item">
        <h2><a href="/pediatrics/article/1">AAP Policy Statement on Asthma 2023</a></h2>
        <p>Policy summary.</p>
      </article>
    </body></html>
    """)

    guidelines, meta = await engine.search_aap_policy("asthma")
    assert len(guidelines) == 1
    assert guidelines[0].source == "aap-policy"
    assert guidelines[0].year == "2023"
    assert guidelines[0].category == "Policy Statement"
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_aap_guidelines_combines_and_dedups(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    shared = "<h3><a href='/a'>Guideline on Nutrition 2023</a></h3><p>Different text.</p>"
    respx.get(BF_URL).respond(
        html=f"<div class='search-result'>{shared}</div>"
    )
    respx.get(AAP_URL).respond(
        html=f"<div class='search-result'>{shared}</div>"  # identical normalized title
    )

    guidelines, meta = await engine.search_aap_guidelines("nutrition")
    assert len(guidelines) == 1  # exact normalized-title dedup
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_literature_composes_journal_query(tmp_path: Path):
    from unittest.mock import AsyncMock

    engine, cache, http_client = await _engine(tmp_path)
    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = ([], None)
    engine.pubmed = mock_pubmed

    await engine.search_pediatric_literature("asthma", max_results=5)
    term = mock_pubmed.search_articles.await_args.args[0]
    assert "asthma" in term
    assert '"Pediatrics"[Journal]' in term
    assert '"JAMA Pediatrics"[Journal]' in term
    assert "European Journal of Pediatrics" in term
    await cache.close()
    await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_pediatrics.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/pediatrics.py`**

Constants (verbatim from the original):
```python
BF_ITEM_SELECTORS = ".search-result, .result-item, .guideline-item, article, .content-item"
AAP_ITEM_SELECTORS = ".search-result, .result-item, .article-item, article, .publication-item"
TITLE_SELECTORS = "h2, h3, .title, a.title"
DESC_SELECTORS = ".description, .summary, .abstract, p"
AGE_GROUP_RE = re.compile(
    r"(\d+\s*(?:-|\s*to\s*)\s*\d+\s*(?:months?|years?|days?)|infant|toddler|"
    r"preschool|school-age|adolescent)", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
PEDIATRIC_JOURNALS = [
    "Pediatrics", "JAMA Pediatrics", "The Journal of Pediatrics", "Pediatric Research",
    "Archives of Disease in Childhood", "European Journal of Pediatrics",
    "Pediatric Clinics of North America",
]
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")
```
Scrape flow per site: optional `await asyncio.sleep(random.uniform(*self.jitter_range))` when `jitter_range` is set; Tier-1 `GET {search_url}?q={query}` with `headers={"User-Agent": BROWSER_UA}`; a `None` response or zero parsed items triggers Tier 2 when `settings.enable_playwright_fallback` — import `playwright` inside a `try/except ImportError` (skip silently when missing) and run the same selector logic via `page.eval_on_selector_all` or an in-page evaluate equivalent. Parsing (shared by both tiers, given a `BeautifulSoup` object or playwright item dicts):
```python
for item in soup.select(item_selectors):
    title_el = item.select_one(TITLE_SELECTORS)
    title = title_el.get_text(strip=True) if title_el else ""
    if not title or len(title) <= 10:
        continue
    link = item.find("a")
    href = link.get("href", "") if link else ""
    url = href if href.startswith("http") else base_url + href
    desc_el = item.select_one(DESC_SELECTORS)
    description = (desc_el.get_text(strip=True) if desc_el else "")[:300]
```
Bright Futures records: `organization="American Academy of Pediatrics"`, `category="Preventive Care"`, `source="bright-futures"`, `age_group=AGE_GROUP_RE.search(title).group(0)` when matched. AAP Policy records: `category="Policy Statement"`, `source="aap-policy"`, `year=YEAR_RE.search(title).group(0)` when matched.

`search_aap_guidelines`: `asyncio.gather(self.search_bright_futures(query), self.search_aap_policy(query), return_exceptions=True)`, concatenate, dedup by `re.sub(r"[^\w\s]", "", title.lower())` equality (first occurrence wins), cache under source `"guidelines"` with key `f"aap_guidelines:{query}"`.

`search_pediatric_literature`: one call —
```python
journal_filters = " OR ".join(f'"{j}"[Journal]' for j in PEDIATRIC_JOURNALS)
term = f"({query}) AND ({journal_filters})"
articles, _ = await self.pubmed.search_articles(term, max_results=max_results)
```
— cached under source `"pediatric_journals"` with key `f"pediatric_journals:{query}:{max_results}"`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_pediatrics.py -v`
Expected: PASS (all four tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/pediatrics.py tests/medical/test_pediatrics.py
git commit -m "feat(medical): implement pediatric guidelines scraping and literature engine"
```

---

### Task 12: Multi-Database & Journal Search Engine

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/databases.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_databases.py`

**Interfaces:**
- Consumes: `MedicalPubMedClient`, `ClinicalTrialsClient`, `scholar_mcp.utils.deduplication.deduplicate_papers`, `scholar_mcp.medical.models.MedicalArticle`.
- Produces: `MedicalDatabasesEngine(pubmed: MedicalPubMedClient, clinical_trials: ClinicalTrialsClient, http_client: AsyncHttpClient, cache: SQLiteCacheManager, settings: Settings)`. Methods: `async search_medical_databases(query: str) -> tuple[list[MedicalArticle], CacheMetadata]` (cache source `"pubmed"`, key `f"medical_databases:{query}"`), `async search_medical_journals(query: str) -> tuple[list[MedicalArticle], CacheMetadata]` (key `f"medical_journals:{query}"`).

- [ ] **Step 1: Write the failing test for the multi-database engine**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_databases.py`:
```python
from pathlib import Path
from unittest.mock import AsyncMock

import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.databases import MedicalDatabasesEngine
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

COCHRANE_URL = "https://www.cochranelibrary.com/search"


async def _engine(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)

    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles.return_value = (
        [MedicalArticle(title="Diabetes Management", authors=["Smith J"], year="2021",
                        doi="10.1000/1", journal="NEJM", abstract="Long detailed abstract.")],
        CacheMetadata(cached=False, cache_age=0),
    )
    mock_ct = AsyncMock()
    mock_ct.search_clinical_trials.return_value = (
        [MedicalArticle(title="Diabetes Clinical Trial", journal="ClinicalTrials.gov",
                        url="https://clinicaltrials.gov/study/NCT123")],
        CacheMetadata(cached=False, cache_age=0),
    )

    engine = MedicalDatabasesEngine(
        pubmed=mock_pubmed, clinical_trials=mock_ct,
        http_client=http_client, cache=cache, settings=settings,
    )
    return engine, cache, http_client, mock_pubmed


@respx.mock
async def test_search_medical_databases_combines_and_deduplicates(tmp_path: Path):
    engine, cache, http_client, _ = await _engine(tmp_path)
    # Cochrane returns the same paper under a case-variant title + same year: dedup keeps the PubMed record (has DOI).
    respx.get(COCHRANE_URL).respond(html="""
    <html><body>
      <div class="search-result-item">
        <h3><a href="/cd/1">DIABETES management</a></h3>
        <div class="abstract">Short.</div>
        <div class="journal">Cochrane Database</div>
      </div>
    </body></html>
    """)

    articles, meta = await engine.search_medical_databases("diabetes")
    titles = [a.title for a in articles]
    assert "Diabetes Management" in titles
    assert "Diabetes Clinical Trial" in titles
    assert len(articles) == 2  # Cochrane duplicate removed
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_medical_databases_survives_source_failure(tmp_path: Path):
    engine, cache, http_client, _ = await _engine(tmp_path)
    respx.get(COCHRANE_URL).mock(side_effect=Exception("cochrane down"))

    articles, meta = await engine.search_medical_databases("diabetes")
    assert len(articles) == 2  # PubMed + ClinicalTrials still returned
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_medical_journals_composes_query(tmp_path: Path):
    engine, cache, http_client, mock_pubmed = await _engine(tmp_path)
    mock_pubmed.search_articles.return_value = (
        [MedicalArticle(title="NEJM diabetes study", journal="NEJM")],
        CacheMetadata(cached=False, cache_age=0),
    )

    articles, meta = await engine.search_medical_journals("diabetes")
    term = mock_pubmed.search_articles.await_args.args[0]
    assert "New England Journal of Medicine" in term
    assert "Nature Medicine" in term
    assert "diabetes" in term
    assert len(articles) == 1
    await cache.close()
    await http_client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_databases.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/databases.py`**

`search_medical_databases` (cache-first, source `"pubmed"`): run the three sub-searches concurrently —
```python
tasks = [
    self.pubmed.search_articles(query, 5),
    self.clinical_trials.search_clinical_trials(query),
    self._search_cochrane(query),
]
results = await asyncio.gather(*tasks, return_exceptions=True)
papers: list[dict] = []
for result in results:
    if isinstance(result, BaseException) or not result or not result[0]:
        continue
    papers.extend(a.to_dict() for a in result[0])
unique, _stats = deduplicate_papers(papers)
ranked = rank_medical_articles([MedicalArticle.from_dict(p) for p in unique], query)
return ranked[:20], meta
```
`_search_cochrane(query)`: optional jitter delay (constructor arg, default `(1.0, 3.0)`, `None` disables), Tier-1 `GET https://www.cochranelibrary.com/search` with `params={"q": query}` and the browser User-Agent; parse items `.search-result-item, .result-item, .search-result`, titles `h3 a, .title a, .result-title a`, descriptions `.abstract, .snippet, .summary`, journal `.journal, .source, .publication` defaulting to `"Cochrane Database"`, relative URLs prefixed with `https://www.cochranelibrary.com`, titles <= 10 chars dropped; same Playwright fallback rules as `pediatrics.py` (import-guarded, flag-gated). Return `([], CacheMetadata(False, 0))` on any failure.

`search_medical_journals` (cache-first, source `"pubmed"`):
```python
TOP_JOURNALS = ["New England Journal of Medicine", "JAMA", "Lancet", "BMJ", "Nature Medicine"]
journal_filters = " OR ".join(f'"{j}"[Journal]' for j in TOP_JOURNALS)
articles, _ = await self.pubmed.search_articles(f"({query}) AND ({journal_filters})", 15)
deduped, _ = deduplicate_papers([a.to_dict() for a in articles])
# Rank on the raw user `query`, not the `[Journal]`-expanded term, so journal
# names don't count as query terms. Rank before slicing so the cap keeps the
# best 15, not the first 15.
ranked = rank_medical_articles([MedicalArticle.from_dict(p) for p in deduped], query)
```
Return `ranked[:15]`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/medical/test_databases.py -v`
Expected: PASS (all three tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/databases.py tests/medical/test_databases.py
git commit -m "feat(medical): implement multi-database and top medical journal search engine"
```

---

### Task 13: Wire Medical Tools into the FastMCP Server

**Files:**
- Modify: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/server.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/test_server_medical.py`

**Interfaces:**
- Consumes: every `scholar_mcp.medical` client and `scholar_mcp.medical.formatters`.
- Produces: module-level instances `medical_cache`, `pubmed_client`, `fda_client`, `rxnorm_client`, `who_client`, `clinical_trials_client`, `guidelines_engine`, `pediatrics_engine`, `databases_engine` on `scholar_mcp.server`, and 13 registered tools: `search_drugs`, `get_drug_details`, `search_pediatric_drugs`, `search_drug_nomenclature`, `get_health_statistics`, `get_child_health_statistics`, `search_clinical_guidelines`, `search_pediatric_guidelines`, `search_aap_guidelines`, `search_pediatric_literature`, `search_medical_databases`, `search_medical_journals`, `get_medical_cache_stats`.
- Test convention: follow `tests/test_server_tools.py` — call the decorated tool functions directly and assert `callable(getattr(srv, name))`. Never use `mcp._tool_manager` (that is FastMCP 1.x private API; this repo runs fastmcp 3.4.2).

- [ ] **Step 1: Write the failing test for server tool registration**

Create `/Users/gus/Git/scholar-mcp/tests/test_server_medical.py`:
```python
from unittest.mock import AsyncMock

from scholar_mcp import server as srv
from scholar_mcp.medical.models import DrugLabel, OpenFDAData
from scholar_mcp.utils.sqlite_cache import CacheMetadata

MEDICAL_TOOLS = {
    "search_drugs",
    "get_drug_details",
    "search_pediatric_drugs",
    "search_drug_nomenclature",
    "get_health_statistics",
    "get_child_health_statistics",
    "search_clinical_guidelines",
    "search_pediatric_guidelines",
    "search_aap_guidelines",
    "search_pediatric_literature",
    "search_medical_databases",
    "search_medical_journals",
    "get_medical_cache_stats",
}


def test_all_medical_tools_registered():
    for name in MEDICAL_TOOLS:
        assert callable(getattr(srv, name)), f"{name} is not exposed by scholar_mcp.server"


async def test_search_drugs_tool(monkeypatch):
    mock = AsyncMock()
    mock.search_drugs.return_value = (
        [DrugLabel(openfda=OpenFDAData(brand_name=["Advil"], generic_name=["Ibuprofen"]))],
        CacheMetadata(cached=False, cache_age=0),
    )
    monkeypatch.setattr(srv, "fda_client", mock)
    result = await srv.search_drugs("advil")
    assert result["data"][0]["openfda"]["brand_name"] == ["Advil"]
    assert "[Fresh response]" in result["markdown"]


async def test_get_drug_details_tool_handles_none(monkeypatch):
    mock = AsyncMock()
    mock.get_drug_by_ndc.return_value = (None, CacheMetadata(cached=False, cache_age=0))
    monkeypatch.setattr(srv, "fda_client", mock)
    result = await srv.get_drug_details("00-00-00")
    assert result["status"] == "not_found"


async def test_get_medical_cache_stats_tool(monkeypatch):
    mock = AsyncMock()
    mock.get_stats.return_value = {"total_entries": 0, "hits": 0, "misses": 0}
    monkeypatch.setattr(srv, "medical_cache", mock)
    result = await srv.get_medical_cache_stats()
    assert result["total_entries"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/test_server_medical.py -v`
Expected: FAIL (medical tools not yet defined on `scholar_mcp.server`).

- [ ] **Step 3: Update `scholar_mcp/server.py`**

After the existing `resolver = WaterfallResolver(...)` block, add the medical wiring (mirrors the existing module-level style; `SQLiteCacheManager` opens lazily so no await is needed here):
```python
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient
from scholar_mcp.medical.databases import MedicalDatabasesEngine
from scholar_mcp.medical.fda import FDAClient
from scholar_mcp.medical.formatters import (
    format_drug_details,
    format_drug_search_results,
)
from scholar_mcp.medical.guidelines import GuidelinesEngine
from scholar_mcp.medical.pediatrics import PediatricsEngine
from scholar_mcp.medical.pubmed import MedicalPubMedClient
from scholar_mcp.medical.rxnorm import RxNormClient
from scholar_mcp.medical.who import WHOClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

medical_cache = SQLiteCacheManager(db_path=settings.cache_db_path, settings=settings)
pubmed_client = MedicalPubMedClient(http_client=http_client, cache=medical_cache, settings=settings)
fda_client = FDAClient(http_client=http_client, cache=medical_cache, settings=settings)
rxnorm_client = RxNormClient(http_client=http_client, cache=medical_cache, settings=settings)
who_client = WHOClient(http_client=http_client, cache=medical_cache, settings=settings)
clinical_trials_client = ClinicalTrialsClient(http_client=http_client, cache=medical_cache, settings=settings)
guidelines_engine = GuidelinesEngine(pubmed=pubmed_client, cache=medical_cache, settings=settings)
pediatrics_engine = PediatricsEngine(
    http_client=http_client, cache=medical_cache, settings=settings, pubmed=pubmed_client
)
databases_engine = MedicalDatabasesEngine(
    pubmed=pubmed_client, clinical_trials=clinical_trials_client,
    http_client=http_client, cache=medical_cache, settings=settings,
)
```
Then register the 13 tools with `@mcp.tool()`, gated on the flag — every tool follows the existing error-dict convention. Example pattern (repeat for all 13, each with a Google-style docstring describing args like the existing tools):
```python
if settings.enable_medical_tools:

    @mcp.tool()
    async def search_drugs(query: str, limit: int = 10) -> list[dict[str, Any]] | dict[str, Any]:
        """Search for drug information using the FDA database.

        Args:
            query: Drug name to search for (brand name or generic name).
            limit: Number of results to return (max 50).
        """
        clamped = min(max(1, limit), 50)
        try:
            drugs, meta = await fda_client.search_drugs(query, clamped)
            return format_drug_search_results(drugs, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "fda"}

    @mcp.tool()
    async def get_drug_details(ndc: str) -> dict[str, Any]:
        """Get detailed information about a specific drug by NDC (National Drug Code).

        Args:
            ndc: National Drug Code (NDC) of the drug.
        """
        try:
            drug, meta = await fda_client.get_drug_by_ndc(ndc)
            if drug is None:
                return {"status": "not_found", "ndc": ndc}
            return format_drug_details(drug, ndc, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "fda"}
```
The remaining 11 tools map 1:1 onto the engine methods with their spec §7.1 signatures: `search_pediatric_drugs` (FDA), `search_drug_nomenclature` (RxNorm), `get_health_statistics` / `get_child_health_statistics` (WHO, limit clamped to 20), `search_clinical_guidelines` (guidelines, optional `organization`), `search_pediatric_guidelines` (dispatch on `source`: `"bright-futures"` / `"aap-policy"` / `"all"`), `search_aap_guidelines`, `search_pediatric_literature` (max_results clamped to 20), `search_medical_databases`, `search_medical_journals` (both via `databases_engine`), and `get_medical_cache_stats` (`await medical_cache.get_stats()`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests/test_server_medical.py tests/test_server_tools.py -v`
Expected: PASS (new medical tests plus the existing server tests — no regressions).

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/server.py tests/test_server_medical.py
git commit -m "feat(server): register all 13 medical tools on FastMCP server"
```

---

### Task 14: End-to-End Verification, README, and Gate Run

**Files:**
- Modify: `/Users/gus/Git/scholar-mcp/README.md`
- Test: all tests in `/Users/gus/Git/scholar-mcp/tests/`

- [ ] **Step 1: Update `README.md` in `scholar-mcp`**

Add a "Medical Tools" section documenting: the 13 tools with one-line descriptions; the data sources (openFDA, RxNav, WHO GHO, ClinicalTrials.gov, PubMed, AAP Bright Futures, AAP Pediatrics, Cochrane Library); the SQLite cache (default path `~/.cache/scholar_mcp/cache.db`, per-source TTL table from spec §4, `CACHE_*` env vars); the optional `medical` extra (`pip install 'scholar-mcp[medical]'` enables the Playwright fallback for Cochrane and AAP scraping); and the note that Google Scholar / general PubMed literature search are covered by the existing `search_papers` / `get_full_text` tools.

- [ ] **Step 2: Run the full test suite**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests -v`
Expected: all tests PASS (existing scholar tests plus all new medical/utils/config tests).

- [ ] **Step 3: Run the coverage gate**

Run: `cd /Users/gus/Git/scholar-mcp && pytest tests --cov=scholar_mcp --cov-report=term-missing`
Expected: completes without errors; note the coverage number in the PR description.

- [ ] **Step 4: Commit documentation & final integration**

```bash
cd /Users/gus/Git/scholar-mcp
git add README.md
git commit -m "docs: document medical tools and sqlite cache configuration in README"
```
