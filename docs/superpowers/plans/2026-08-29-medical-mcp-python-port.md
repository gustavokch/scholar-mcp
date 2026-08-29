# Medical MCP Python Port & Scholar MCP Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for inline task execution. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port all medical data providers, heuristic guideline scoring, and scraping engines from TypeScript `medical-mcp` to Python, integrating them natively as a persistent SQLite-backed `scholar_mcp.medical` subsystem under `scholar-mcp`.

**Architecture:** A modular async architecture in `scholar_mcp.medical` containing dedicated clients for openFDA, RxNav, WHO GHO, ClinicalTrials.gov, PubMed guideline heuristics, and AAP pediatric scrapers (`httpx` + `bs4` with optional Playwright fallback). Data is persistently cached with source-specific TTLs in `scholar_mcp.utils.sqlite_cache`, deduplicated via `scholar_mcp.utils.deduplication`, and registered directly as FastMCP tools in `scholar_mcp.server`.

**Tech Stack:** Python 3.10+, `FastMCP`, `httpx`, `aiosqlite`, `beautifulsoup4`, `lxml`, `pytest`, `pytest-asyncio`, `respx`.

**Spec:** `docs/superpowers/specs/2026-08-29-medical-mcp-python-port-design.md`

## Global Constraints
- Target Python version: `>=3.10`
- Dependencies to add: `aiosqlite>=0.20.0`
- Optional dependencies: `playwright>=1.40.0`
- Output formatting: No medical safety banners or disclaimers; cache provenance tags must be plain text without emojis (e.g. `[Cached: 142s old]`, `[Fresh response]`).
- All tool names on FastMCP must use Pythonic `snake_case` (e.g. `search_drugs`, `get_health_statistics`).
- All network interactions must be asynchronous and use `AsyncHttpClient` or `httpx.AsyncClient` with timeouts.
- All code must follow test-driven development (TDD) with tests in `tests/medical/` and `tests/utils/`.

---

### Task 1: Project Dependencies & Settings Configuration

**Files:**
- Modify: `/Users/gus/Git/scholar-mcp/pyproject.toml`
- Modify: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/config.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/test_config_medical.py`

**Interfaces:**
- Consumes: Standard `os` and `dataclasses`.
- Produces: `Settings` with fields `cache_db_path`, `cache_ttl_fda`, `cache_ttl_who`, `cache_ttl_rxnorm`, `cache_ttl_guidelines`, `cache_ttl_pubmed`, `cache_ttl_bright_futures`, `cache_ttl_aap_policy`, `cache_ttl_clinical_trials`, `enable_playwright_fallback`, `enable_medical_tools`.

- [ ] **Step 1: Write the failing test for configuration settings**

Create `/Users/gus/Git/scholar-mcp/tests/test_config_medical.py`:
```python
from pathlib import Path
import os
from scholar_mcp.config import Settings


def test_medical_settings_defaults():
    settings = Settings.load()
    assert settings.cache_ttl_fda == 86400
    assert settings.cache_ttl_who == 604800
    assert settings.cache_ttl_rxnorm == 2592000
    assert settings.cache_ttl_guidelines == 604800
    assert settings.cache_ttl_pubmed == 604800
    assert settings.cache_ttl_bright_futures == 2592000
    assert settings.cache_ttl_aap_policy == 604800
    assert settings.cache_ttl_clinical_trials == 86400
    assert settings.enable_playwright_fallback is True
    assert settings.enable_medical_tools is True
    assert isinstance(settings.cache_db_path, Path)


def test_medical_settings_env_override(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_FDA", "12345")
    monkeypatch.setenv("ENABLE_MEDICAL_TOOLS", "false")
    monkeypatch.setenv("SCHOLAR_CACHE_DB", "/tmp/custom_cache.db")
    settings = Settings.load()
    assert settings.cache_ttl_fda == 12345
    assert settings.enable_medical_tools is False
    assert settings.cache_db_path == Path("/tmp/custom_cache.db")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/test_config_medical.py -v`
Expected: FAIL with AttributeError (missing attributes on Settings).

- [ ] **Step 3: Update `pyproject.toml` and `scholar_mcp/config.py`**

In `/Users/gus/Git/scholar-mcp/pyproject.toml`, add `"aiosqlite>=0.20.0"` to `dependencies`.

In `/Users/gus/Git/scholar-mcp/src/scholar_mcp/config.py`, add the new fields to `Settings` and load logic:
```python
    # Medical and Persistent SQLite Cache Settings
    cache_db_path: Path = field(
        default_factory=lambda: Path(
            os.getenv("SCHOLAR_CACHE_DB", "~/.cache/scholar_mcp/cache.db")
        ).expanduser()
    )
    cache_ttl_fda: int = 86400
    cache_ttl_who: int = 604800
    cache_ttl_rxnorm: int = 2592000
    cache_ttl_guidelines: int = 604800
    cache_ttl_pubmed: int = 604800
    cache_ttl_bright_futures: int = 2592000
    cache_ttl_aap_policy: int = 604800
    cache_ttl_clinical_trials: int = 86400
    enable_playwright_fallback: bool = True
    enable_medical_tools: bool = True
```
And parse them in `Settings.load()`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/test_config_medical.py -v`
Expected: PASS

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
- Consumes: Standard `re`, `html`, `typing`.
- Produces: `normalize_title(str) -> str`, `calculate_similarity(str, str) -> float`, `extract_first_author(authors) -> str | None`, `extract_year(date_str) -> str | None`, `are_duplicates(p1, p2, threshold=0.9) -> bool`, `deduplicate_papers(papers: list[dict]) -> tuple[list[dict], dict]`.

- [ ] **Step 1: Write the failing test for deduplication**

Create `/Users/gus/Git/scholar-mcp/tests/utils/test_deduplication.py`:
```python
from scholar_mcp.utils.deduplication import (
    normalize_title,
    calculate_similarity,
    extract_first_author,
    extract_year,
    are_duplicates,
    deduplicate_papers,
)


def test_normalize_title():
    raw = "Efficacy of Metformin &amp; Diet in Type 2 Diabetes: [Preprint] Version 1"
    normalized = normalize_title(raw)
    assert normalized == "efficacy of metformin & diet in type 2 diabetes"


def test_calculate_similarity():
    s1 = "treatment of hypertension in elderly patients"
    s2 = "treatment of hypertension in elderly patient"
    sim = calculate_similarity(s1, s2)
    assert sim > 0.95
    assert calculate_similarity("", "abc") == 0.0
    assert calculate_similarity("exact", "exact") == 1.0


def test_extract_first_author():
    assert extract_first_author(["Smith J", "Doe A"]) == "smith"
    assert extract_first_author("Johnson, M. et al.") == "johnson"
    assert extract_first_author("J. Watson") == "watson"


def test_extract_year():
    assert extract_year("2023-05-12") == "2023"
    assert extract_year("Published in 2021") == "2021"
    assert extract_year("Unknown") is None


def test_are_duplicates():
    p1 = {"title": "Aspirin in Cardiovascular Disease", "doi": "10.1001/jama.2020.1", "authors": ["Smith J"], "year": "2020"}
    p2 = {"title": "Aspirin in cardiovascular disease", "doi": "10.1001/jama.2020.1", "authors": ["Smith, John"], "year": "2020"}
    assert are_duplicates(p1, p2) is True


def test_deduplicate_papers():
    papers = [
        {"title": "Study A", "doi": "10.1000/1", "abstract": "Short"},
        {"title": "Study A", "doi": "10.1000/1", "abstract": "Detailed abstract with more text"},
        {"title": "Study B", "doi": "10.1000/2", "abstract": "Another study"},
    ]
    unique, stats = deduplicate_papers(papers)
    assert len(unique) == 2
    assert stats["duplicates_removed"] == 1
    # Check that richer metadata was preserved
    match_a = next(p for p in unique if p["title"] == "Study A")
    assert match_a["abstract"] == "Detailed abstract with more text"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/utils/test_deduplication.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/utils/deduplication.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/utils/deduplication.py` implementing title normalization, Levenshtein distance, first author extraction, year extraction, duplicate matching, and metadata preservation.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/utils/test_deduplication.py -v`
Expected: PASS

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
- Consumes: `aiosqlite`, `json`, `time`, `pathlib.Path`, `scholar_mcp.config.Settings`.
- Produces: `CacheMetadata(cached: bool, cache_age: int)`, `SQLiteCacheManager(db_path: Path, settings: Settings)`. Methods: `init_db()`, `get(key: str) -> tuple[Any | None, CacheMetadata]`, `set(key: str, data: Any, source: str, ttl: int | None = None)`, `get_stats() -> dict[str, Any]`, `close()`.

- [ ] **Step 1: Write the failing test for SQLite persistent cache**

Create `/Users/gus/Git/scholar-mcp/tests/utils/test_sqlite_cache.py`:
```python
import pytest
import asyncio
from pathlib import Path
from scholar_mcp.config import Settings
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager, CacheMetadata


@pytest.mark.asyncio
async def test_sqlite_cache_set_and_get(tmp_path: Path):
    db_file = tmp_path / "test_cache.db"
    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=db_file, settings=settings)
    await cache.init_db()

    # Initial miss
    val, meta = await cache.get("fda:search:aspirin")
    assert val is None
    assert meta.cached is False

    # Set value with source "fda"
    await cache.set("fda:search:aspirin", {"brand": "Aspirin", "ndc": "123"}, source="fda")

    # Hit
    val, meta = await cache.get("fda:search:aspirin")
    assert val is not None
    assert val["brand"] == "Aspirin"
    assert meta.cached is True
    assert meta.cache_age >= 0

    await cache.close()


@pytest.mark.asyncio
async def test_sqlite_cache_expiration(tmp_path: Path):
    db_file = tmp_path / "test_cache_exp.db"
    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=db_file, settings=settings)
    await cache.init_db()

    # Set with 1s TTL
    await cache.set("short_lived", {"data": 1}, source="fda", ttl=1)
    val, meta = await cache.get("short_lived")
    assert val == {"data": 1}

    # Wait for expiry
    await asyncio.sleep(1.1)
    val, meta = await cache.get("short_lived")
    assert val is None
    assert meta.cached is False

    await cache.close()


@pytest.mark.asyncio
async def test_sqlite_cache_stats(tmp_path: Path):
    db_file = tmp_path / "test_cache_stats.db"
    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=db_file, settings=settings)
    await cache.init_db()

    await cache.set("k1", "v1", source="fda")
    await cache.set("k2", "v2", source="who")
    await cache.get("k1")  # Hit
    await cache.get("k_miss")  # Miss

    stats = await cache.get_stats()
    assert stats["total_entries"] == 2
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert "fda" in stats["sources"]
    assert "who" in stats["sources"]

    await cache.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/utils/test_sqlite_cache.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/utils/sqlite_cache.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/utils/sqlite_cache.py` using `aiosqlite` with schema initialization, WAL mode, per-source TTL resolution from settings, LRU eviction, and stats.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/utils/test_sqlite_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/utils/sqlite_cache.py tests/utils/test_sqlite_cache.py
git commit -m "feat(cache): add persistent multi-tier SQLite cache manager"
```

---

### Task 4: Medical Data Models and Output Formatters

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/__init__.py`
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/models.py`
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/formatters.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_models_formatters.py`

**Interfaces:**
- Consumes: `dataclasses`, `typing`, `scholar_mcp.utils.sqlite_cache.CacheMetadata`.
- Produces: Data classes (`DrugLabel`, `RxNormDrug`, `WHOIndicatorRecord`, `ClinicalGuideline`, `PediatricGuideline`, `MedicalArticle`), formatters (`format_drug_search_results`, `format_drug_details`, `format_rxnorm_drugs`, `format_health_indicators`, `format_guidelines`, `format_pediatric_guidelines`, `format_medical_articles`, `append_cache_info`).
- Constraint: No medical safety banners; plain text cache tags (`[Cached: 142s old]`, `[Fresh response]`).

- [ ] **Step 1: Write the failing test for models & formatters**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_models_formatters.py`:
```python
from scholar_mcp.medical.models import DrugLabel, OpenFDAData, WHOIndicatorRecord, ClinicalGuideline
from scholar_mcp.medical.formatters import (
    format_drug_search_results,
    format_drug_details,
    format_health_indicators,
    append_cache_info,
)
from scholar_mcp.utils.sqlite_cache import CacheMetadata


def test_append_cache_info_no_emojis():
    text = "Result content"
    fresh_meta = CacheMetadata(cached=False, cache_age=0)
    cached_meta = CacheMetadata(cached=True, cache_age=120)

    fresh_out = append_cache_info(text, fresh_meta)
    cached_out = append_cache_info(text, cached_meta)

    assert "[Fresh response]" in fresh_out
    assert "[Cached: 120s old]" in cached_out
    assert "🔄" not in fresh_out
    assert "📦" not in cached_out


def test_format_drug_search_results_no_safety_banner():
    drug = DrugLabel(
        openfda=OpenFDAData(brand_name=["Advil"], generic_name=["Ibuprofen"], manufacturer_name=["Pfizer"]),
        effective_time="20230101",
        purpose=["Pain reliever"],
    )
    meta = CacheMetadata(cached=False, cache_age=0)
    out = format_drug_search_results([drug], "advil", meta)
    assert "Advil" in out["markdown"]
    assert "Ibuprofen" in out["markdown"]
    assert "[Fresh response]" in out["markdown"]
    assert "🚨" not in out["markdown"]
    assert "SAFETY NOTICE" not in out["markdown"]
    assert len(out["data"]) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_models_formatters.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `models.py` and `formatters.py` in `scholar_mcp/medical/`**

Implement all dataclasses in `models.py` and all markdown/dict formatters in `formatters.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_models_formatters.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/__init__.py src/scholar_mcp/medical/models.py src/scholar_mcp/medical/formatters.py tests/medical/test_models_formatters.py
git commit -m "feat(medical): add medical data models and clean formatters"
```

---

### Task 5: openFDA Client & Pediatric Drug Filtering

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/fda.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_fda.py`

**Interfaces:**
- Consumes: `httpx`, `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.DrugLabel`.
- Produces: `FDAClient(http_client, cache, settings)`. Methods: `search_drugs(query: str, limit: int = 10) -> tuple[list[DrugLabel], CacheMetadata]`, `get_drug_by_ndc(ndc: str) -> tuple[DrugLabel | None, CacheMetadata]`, `search_pediatric_drugs(query: str, limit: int = 10) -> tuple[list[DrugLabel], CacheMetadata]`.

- [ ] **Step 1: Write the failing test for FDA client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_fda.py`:
```python
import pytest
import respx
import httpx
from pathlib import Path
from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager
from scholar_mcp.medical.fda import FDAClient


@pytest.fixture
async def fda_client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    await cache.init_db()
    client = FDAClient(http_client=http_client, cache=cache, settings=settings)
    yield client
    await cache.close()


@pytest.mark.asyncio
@respx.mock
async def test_search_drugs_success(fda_client):
    mock_response = {
        "results": [
            {
                "openfda": {
                    "brand_name": ["Tylenol"],
                    "generic_name": ["Acetaminophen"],
                    "manufacturer_name": ["Johnson & Johnson"],
                    "product_ndc": ["50580-488"],
                },
                "effective_time": "20230101",
                "purpose": ["Pain reliever/fever reducer"],
            }
        ]
    }
    respx.get("https://api.fda.gov/drug/label.json").respond(json=mock_response)

    drugs, meta = await fda_client.search_drugs("tylenol", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name[0] == "Tylenol"
    assert drugs[0].openfda.generic_name[0] == "Acetaminophen"


@pytest.mark.asyncio
async def test_search_drugs_invalid_query(fda_client):
    drugs, meta = await fda_client.search_drugs("medication")
    assert len(drugs) == 0


@pytest.mark.asyncio
@respx.mock
async def test_get_drug_by_ndc(fda_client):
    mock_response = {
        "results": [
            {
                "openfda": {
                    "brand_name": ["Advil"],
                    "product_ndc": ["0573-0164"],
                },
                "effective_time": "20230101",
                "dosage_and_administration": ["Take 1 tablet every 4 to 6 hours"],
            }
        ]
    }
    respx.get("https://api.fda.gov/drug/label.json").respond(json=mock_response)

    drug, meta = await fda_client.get_drug_by_ndc("0573-0164")
    assert drug is not None
    assert drug.openfda.brand_name[0] == "Advil"


@pytest.mark.asyncio
@respx.mock
async def test_search_pediatric_drugs(fda_client):
    mock_response = {
        "results": [
            {
                "openfda": {
                    "brand_name": ["Children's Motrin"],
                    "generic_name": ["Ibuprofen"],
                    "product_ndc": ["50580-601"],
                },
                "effective_time": "20230101",
                "purpose": ["Pain reliever"],
                "dosage_and_administration": ["Pediatric dosing: 10mg/kg every 6-8 hours for children"],
            }
        ]
    }
    respx.get("https://api.fda.gov/drug/label.json").respond(json=mock_response)

    drugs, meta = await fda_client.search_pediatric_drugs("motrin", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name[0] == "Children's Motrin"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_fda.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/fda.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/fda.py` with query validation, multi-strategy layered FDA search, NDC retrieval, pediatric keyword extraction, and SQLite caching.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_fda.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/fda.py tests/medical/test_fda.py
git commit -m "feat(medical): implement openFDA client and pediatric drug filtering"
```

---

### Task 6: RxNorm Drug Nomenclature Client

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/rxnorm.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_rxnorm.py`

**Interfaces:**
- Consumes: `httpx`, `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.RxNormDrug`.
- Produces: `RxNormClient(http_client, cache, settings)`. Methods: `search_drug_nomenclature(query: str) -> tuple[list[RxNormDrug], CacheMetadata]`.

- [ ] **Step 1: Write the failing test for RxNorm client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_rxnorm.py`:
```python
import pytest
import respx
from pathlib import Path
from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager
from scholar_mcp.medical.rxnorm import RxNormClient


@pytest.fixture
async def rxnorm_client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    await cache.init_db()
    client = RxNormClient(http_client=http_client, cache=cache, settings=settings)
    yield client
    await cache.close()


@pytest.mark.asyncio
@respx.mock
async def test_search_drug_nomenclature(rxnorm_client):
    mock_response = {
        "drugGroup": {
            "conceptGroup": [
                {
                    "conceptProperties": [
                        {
                            "rxcui": "161",
                            "name": "Acetaminophen",
                            "synonym": "APAP",
                            "tty": "IN",
                            "language": "ENG",
                            "suppress": "N",
                            "umlscui": "C0000970",
                        }
                    ]
                }
            ]
        }
    }
    respx.get("https://rxnav.nlm.nih.gov/REST/drugs.json").respond(json=mock_response)

    drugs, meta = await rxnorm_client.search_drug_nomenclature("acetaminophen")
    assert len(drugs) == 1
    assert drugs[0].rxcui == "161"
    assert drugs[0].name == "Acetaminophen"
    assert "APAP" in drugs[0].synonyms
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_rxnorm.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/rxnorm.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/rxnorm.py` parsing concept groups and properties with SQLite caching (30-day TTL).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_rxnorm.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/rxnorm.py tests/medical/test_rxnorm.py
git commit -m "feat(medical): implement RxNorm nomenclature client"
```

---

### Task 7: WHO Global Health Observatory Client

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/who.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_who.py`

**Interfaces:**
- Consumes: `httpx`, `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.WHOIndicatorRecord`.
- Produces: `WHOClient(http_client, cache, settings)`. Methods: `get_health_statistics(indicator: str, country: str | None = None, limit: int = 10) -> tuple[list[WHOIndicatorRecord], CacheMetadata]`, `get_child_health_statistics(indicator: str, country: str | None = None, limit: int = 10) -> tuple[list[WHOIndicatorRecord], CacheMetadata]`.

- [ ] **Step 1: Write the failing test for WHO client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_who.py`:
```python
import pytest
import respx
from pathlib import Path
from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager
from scholar_mcp.medical.who import WHOClient


@pytest.fixture
async def who_client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    await cache.init_db()
    client = WHOClient(http_client=http_client, cache=cache, settings=settings)
    yield client
    await cache.close()


@pytest.mark.asyncio
@respx.mock
async def test_get_health_statistics(who_client):
    # 1. Indicator search response
    respx.get("https://ghoapi.azureedge.net/api/Indicator").respond(
        json={"value": [{"IndicatorCode": "WHOSIS_000001", "IndicatorName": "Life expectancy at birth (years)"}]}
    )
    # 2. Indicator data response
    respx.get("https://ghoapi.azureedge.net/api/WHOSIS_000001").respond(
        json={
            "value": [
                {
                    "SpatialDim": "USA",
                    "TimeDim": "2020",
                    "NumericValue": 78.5,
                    "Unit": "years",
                    "Sex": "BTSX",
                }
            ]
        }
    )

    records, meta = await who_client.get_health_statistics("life expectancy", country="USA", limit=5)
    assert len(records) == 1
    assert records[0].indicator_code == "WHOSIS_000001"
    assert records[0].numeric_value == 78.5
    assert records[0].spatial_dim == "USA"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_who.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/who.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/who.py` supporting synonym variation expansion, multi-dimensional indicator extraction, child health indicators, and SQLite caching.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_who.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/who.py tests/medical/test_who.py
git commit -m "feat(medical): implement WHO Global Health Observatory client"
```

---

### Task 8: ClinicalTrials.gov Client

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/clinical_trials.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_clinical_trials.py`

**Interfaces:**
- Consumes: `httpx`, `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.MedicalArticle`.
- Produces: `ClinicalTrialsClient(http_client, cache, settings)`. Methods: `search_clinical_trials(query: str, limit: int = 10) -> tuple[list[MedicalArticle], CacheMetadata]`.

- [ ] **Step 1: Write the failing test for ClinicalTrials client**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_clinical_trials.py`:
```python
import pytest
import respx
from pathlib import Path
from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager
from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient


@pytest.fixture
async def ct_client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    await cache.init_db()
    client = ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings)
    yield client
    await cache.close()


@pytest.mark.asyncio
@respx.mock
async def test_search_clinical_trials(ct_client):
    mock_response = {
        "studies": [
            {
                "protocolSection": {
                    "identificationModule": {
                        "nctId": "NCT01234567",
                        "briefTitle": "Evaluation of Drug X in Asthma",
                    },
                    "descriptionModule": {"briefSummary": "This study evaluates safety and efficacy."},
                    "statusModule": {"startDateStruct": {"date": "2021-01"}},
                }
            }
        ]
    }
    respx.get("https://clinicaltrials.gov/api/v2/studies").respond(json=mock_response)

    articles, meta = await ct_client.search_clinical_trials("asthma", limit=5)
    assert len(articles) == 1
    assert articles[0].title == "Evaluation of Drug X in Asthma"
    assert "https://clinicaltrials.gov/study/NCT01234567" in articles[0].url
    assert articles[0].source_database == "ClinicalTrials.gov"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_clinical_trials.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/clinical_trials.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/clinical_trials.py` integrating ClinicalTrials.gov v2 REST API with SQLite caching.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_clinical_trials.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/clinical_trials.py tests/medical/test_clinical_trials.py
git commit -m "feat(medical): implement ClinicalTrials.gov v2 client"
```

---

### Task 9: Clinical Guidelines Search & Heuristic Scoring Engine

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/guidelines.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_guidelines.py`

**Interfaces:**
- Consumes: `scholar_mcp.providers.pubmed.PubMedProvider`, `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.ClinicalGuideline`.
- Produces: `GuidelinesEngine(pubmed_provider, http_client, cache, settings)`. Methods: `search_clinical_guidelines(query: str, organization: str | None = None) -> tuple[list[ClinicalGuideline], CacheMetadata]`, `calculate_guideline_score(...) -> GuidelineScore`, `extract_organization(...) -> str`.

- [ ] **Step 1: Write the failing test for guidelines engine**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_guidelines.py`:
```python
import pytest
from scholar_mcp.medical.guidelines import calculate_guideline_score, extract_organization, GuidelinesEngine
from scholar_mcp.medical.models import MedicalArticle


def test_calculate_guideline_score():
    article = MedicalArticle(
        title="American Heart Association Clinical Practice Guideline for Hypertension",
        authors=["Whelton PK"],
        journal="Journal of the American College of Cardiology",
        abstract="Evidence-based recommendation and consensus for blood pressure management.",
        pmid="12345",
    )
    score = calculate_guideline_score(article, has_publication_type=True, organization="American Heart Association")
    assert score.publication_type == 2.0
    assert score.title_keywords >= 1.0
    assert score.journal_reputation >= 1.0
    assert score.author_affiliation >= 1.0
    assert score.total >= 5.0


def test_extract_organization():
    article = MedicalArticle(
        title="Management of Asthma",
        journal="Pediatrics",
        abstract="Official statement from the American Academy of Pediatrics on pediatric asthma care.",
    )
    org = extract_organization(article)
    assert "American Academy of Pediatrics" in org
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_guidelines.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/guidelines.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/guidelines.py` with 2-layer search (publication type filter + semantic keyword fallback), scoring weights, regex organization extraction, deduplication, and SQLite caching.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_guidelines.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/guidelines.py tests/medical/test_guidelines.py
git commit -m "feat(medical): implement clinical guidelines search and heuristic scoring engine"
```

---

### Task 10: Pediatric Scraping & Literature Engine

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/pediatrics.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_pediatrics.py`

**Interfaces:**
- Consumes: `httpx`, `BeautifulSoup`, `scholar_mcp.utils.http.AsyncHttpClient`, `scholar_mcp.utils.sqlite_cache.SQLiteCacheManager`, `scholar_mcp.medical.models.PediatricGuideline`, `scholar_mcp.medical.models.MedicalArticle`.
- Produces: `PediatricsEngine(http_client, cache, settings)`. Methods: `search_bright_futures(query: str) -> tuple[list[PediatricGuideline], CacheMetadata]`, `search_aap_policy(query: str) -> tuple[list[PediatricGuideline], CacheMetadata]`, `search_aap_guidelines(query: str) -> tuple[list[PediatricGuideline], CacheMetadata]`, `search_pediatric_literature(query: str, max_results: int = 10) -> tuple[list[MedicalArticle], CacheMetadata]`.

- [ ] **Step 1: Write the failing test for pediatrics engine**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_pediatrics.py`:
```python
import pytest
import respx
from pathlib import Path
from scholar_mcp.config import Settings
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager
from scholar_mcp.medical.pediatrics import PediatricsEngine


@pytest.fixture
async def pediatrics_engine(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    await cache.init_db()
    engine = PediatricsEngine(http_client=http_client, cache=cache, settings=settings)
    yield engine
    await cache.close()


@pytest.mark.asyncio
@respx.mock
async def test_search_bright_futures_html(pediatrics_engine):
    mock_html = """
    <html>
      <body>
        <div class="search-result">
          <h3 class="title"><a href="/guidelines/infant-nutrition">Infant Nutrition Guidelines (0-12 months)</a></h3>
          <p class="description">Recommendations on breastfeeding and complementary feeding.</p>
        </div>
      </body>
    </html>
    """
    respx.get("https://brightfutures.aap.org/Search").respond(html=mock_html)

    guidelines, meta = await pediatrics_engine.search_bright_futures("nutrition")
    assert len(guidelines) == 1
    assert "Infant Nutrition" in guidelines[0].title
    assert guidelines[0].source == "bright-futures"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_pediatrics.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/pediatrics.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/pediatrics.py` implementing HTTP scraping with BeautifulSoup4, optional Playwright fallback for dynamic sites, and PubMed pediatric journal search with SQLite caching.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_pediatrics.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/pediatrics.py tests/medical/test_pediatrics.py
git commit -m "feat(medical): implement pediatric guidelines scraping and literature engine"
```

---

### Task 11: Multi-Database Medical Literature & Top Journals Search

**Files:**
- Create: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/databases.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/medical/test_databases.py`

**Interfaces:**
- Consumes: `FDAClient`, `WHOClient`, `ClinicalTrialsClient`, `GuidelinesEngine`, `PediatricsEngine`, `scholar_mcp.providers.pubmed.PubMedProvider`, `scholar_mcp.utils.deduplication.deduplicate_papers`.
- Produces: `MedicalDatabasesEngine`. Methods: `search_medical_databases(query: str) -> tuple[list[MedicalArticle], CacheMetadata]`, `search_medical_journals(query: str) -> tuple[list[MedicalArticle], CacheMetadata]`.

- [ ] **Step 1: Write the failing test for multi-database search**

Create `/Users/gus/Git/scholar-mcp/tests/medical/test_databases.py`:
```python
import pytest
from unittest.mock import AsyncMock
from pathlib import Path
from scholar_mcp.config import Settings
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager, CacheMetadata
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.medical.databases import MedicalDatabasesEngine


@pytest.fixture
async def databases_engine(tmp_path: Path):
    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    await cache.init_db()

    mock_pubmed = AsyncMock()
    mock_pubmed.search_articles = AsyncMock(return_value=([
        MedicalArticle(title="Diabetes Management", pmid="111", doi="10.1000/1", journal="NEJM")
    ], CacheMetadata(cached=False, cache_age=0)))

    mock_ct = AsyncMock()
    mock_ct.search_clinical_trials = AsyncMock(return_value=([
        MedicalArticle(title="Diabetes Clinical Trial", journal="ClinicalTrials.gov", url="https://clinicaltrials.gov/study/NCT123")
    ], CacheMetadata(cached=False, cache_age=0)))

    engine = MedicalDatabasesEngine(pubmed=mock_pubmed, clinical_trials=mock_ct, cache=cache, settings=settings)
    yield engine
    await cache.close()


@pytest.mark.asyncio
async def test_search_medical_databases_combines_and_deduplicates(databases_engine):
    articles, meta = await databases_engine.search_medical_databases("diabetes")
    assert len(articles) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_databases.py -v`
Expected: FAIL with ModuleNotFoundError.

- [ ] **Step 3: Implement `scholar_mcp/medical/databases.py`**

Write `/Users/gus/Git/scholar-mcp/src/scholar_mcp/medical/databases.py` running concurrent searches over PubMed, ClinicalTrials.gov, Cochrane, and top medical journals (NEJM, JAMA, Lancet, BMJ, Nature Medicine) with cross-source deduplication.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/medical/test_databases.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/medical/databases.py tests/medical/test_databases.py
git commit -m "feat(medical): implement multi-database and top medical journal search engine"
```

---

### Task 12: Wire Medical Tools into FastMCP Server

**Files:**
- Modify: `/Users/gus/Git/scholar-mcp/src/scholar_mcp/server.py`
- Test: `/Users/gus/Git/scholar-mcp/tests/test_server_medical.py`

**Interfaces:**
- Consumes: All `scholar_mcp.medical` clients and formatters.
- Produces: 13 registered FastMCP tools on `mcp` instance:
  - `search_drugs`
  - `get_drug_details`
  - `search_pediatric_drugs`
  - `search_drug_nomenclature`
  - `get_health_statistics`
  - `get_child_health_statistics`
  - `search_clinical_guidelines`
  - `search_pediatric_guidelines`
  - `search_aap_guidelines`
  - `search_pediatric_literature`
  - `search_medical_databases`
  - `search_medical_journals`
  - `get_medical_cache_stats`

- [ ] **Step 1: Write the failing test for server tool registrations**

Create `/Users/gus/Git/scholar-mcp/tests/test_server_medical.py`:
```python
import pytest
from scholar_mcp.server import mcp


def test_server_has_all_medical_tools():
    tool_names = [t.name for t in mcp._tool_manager.list_tools()]
    expected_tools = [
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
    ]
    for expected in expected_tools:
        assert expected in tool_names, f"Tool {expected} not registered on FastMCP server"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/test_server_medical.py -v`
Expected: FAIL (medical tools not yet registered).

- [ ] **Step 3: Update `scholar_mcp/server.py`**

Instantiate medical subsystem clients (`FDAClient`, `RxNormClient`, `WHOClient`, `GuidelinesEngine`, `PediatricsEngine`, `MedicalDatabasesEngine`, `SQLiteCacheManager`) and register all 13 tools with docstrings and clean output formatters.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest /Users/gus/Git/scholar-mcp/tests/test_server_medical.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /Users/gus/Git/scholar-mcp
git add src/scholar_mcp/server.py tests/test_server_medical.py
git commit -m "feat(server): register all 13 medical tools on FastMCP server"
```

---

### Task 13: End-to-End Verification & Gate Run

**Files:**
- Test: All tests in `tests/`
- Docs: `/Users/gus/Git/scholar-mcp/README.md` (Update tools table & configuration docs)

- [ ] **Step 1: Update `README.md` in `scholar-mcp`**

Add documentation for all medical tools, data sources, and configuration options.

- [ ] **Step 2: Run the full test suite**

Run: `pytest /Users/gus/Git/scholar-mcp/tests -v`
Expected: All tests PASS.

- [ ] **Step 3: Commit documentation & final integration**

```bash
cd /Users/gus/Git/scholar-mcp
git add README.md
git commit -m "docs: document medical tools and sqlite cache configuration in README"
```
