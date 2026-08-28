# Unified Academic Discovery & Full-Text MCP Server (`scholar-mcp`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scholar-mcp`, a unified Python FastMCP server combining PubMed discovery and multi-tier waterfall full-text retrieval (PMC JATS XML -> Europe PMC -> Unpaywall -> Sci-Hub -> Abstract fallback) with clean Markdown extraction for AI agents.

**Architecture:** Layered resolver pipeline: Domain Models & Config -> HTTP/Cache utils -> JATS XML & PDF Parsers -> Identifier Converters -> Modular Providers (PMC, Europe PMC, Unpaywall, Sci-Hub, PubMed, CrossRef) -> Waterfall Resolver -> FastMCP Server Tools.

**Tech Stack:** Python >= 3.10, `fastmcp>=3.0.0`, `requests>=2.31.0`, `beautifulsoup4>=4.12.0`, `pypdf>=5.0.0`, `pydantic>=2.0.0`, `pytest>=8.0.0`, `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-08-28-scholar-mcp-unified-design.md`

## Global Constraints

- Python >= 3.10 compatibility.
- Use `pytest` for all unit and integration tests.
- Full text returned directly in MCP response formatted in clean, token-efficient Markdown without citation bibliographies (`<ref-list>`).
- Zero unhandled network crashes; graceful degradation to abstract fallback.
- `FORCE_SCIHUB` and `ENABLE_SCIHUB` environment variables respected.

---

### Task 1: Package Scaffolding, Configuration, and Data Models

**Files:**
- Modify: `pyproject.toml`
- Create: `src/scholar_mcp/__init__.py`
- Create: `src/scholar_mcp/config.py`
- Create: `src/scholar_mcp/models.py`
- Test: `tests/test_config_models.py`

**Interfaces:**
- Produces: `Settings` (from `scholar_mcp.config`), `PaperMetadata`, `FullTextResponse`, `FullTextSummary`, `DownloadResult`, `IdentifierMap` (from `scholar_mcp.models`).

- [ ] **Step 1: Write the failing test for models and config**

```python
# tests/test_config_models.py
import pytest
from scholar_mcp.config import Settings
from scholar_mcp.models import (
    PaperMetadata,
    FullTextResponse,
    FullTextSummary,
    DownloadResult,
    IdentifierMap,
)

def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("PUBMED_API_KEY", raising=False)
    monkeypatch.delenv("ENABLE_SCIHUB", raising=False)
    monkeypatch.delenv("FORCE_SCIHUB", raising=False)
    settings = Settings.load()
    assert settings.enable_scihub is True
    assert settings.force_scihub is False
    assert settings.pubmed_api_key is None
    assert len(settings.scihub_mirrors) > 0

def test_settings_custom_env(monkeypatch):
    monkeypatch.setenv("FORCE_SCIHUB", "true")
    monkeypatch.setenv("ENABLE_SCIHUB", "false")
    monkeypatch.setenv("PUBMED_API_KEY", "test-key")
    settings = Settings.load()
    assert settings.force_scihub is True
    assert settings.enable_scihub is False
    assert settings.pubmed_api_key == "test-key"

def test_paper_metadata_serialization():
    meta = PaperMetadata(
        title="Sample Paper",
        authors=["Alice Doe", "Bob Smith"],
        year="2023",
        venue="Nature",
        doi="10.1038/s41586-020-2003-7",
        pmid="32000000",
        pmcid="PMC7000000",
        abstract="This is a test abstract.",
        oa_status="gold",
    )
    d = meta.to_dict()
    assert d["title"] == "Sample Paper"
    assert d["pmid"] == "32000000"

def test_full_text_response_serialization():
    resp = FullTextResponse(
        status="full_text",
        source="pmc",
        format="markdown",
        title="Sample Paper",
        doi="10.1038/s41586-020-2003-7",
        pmid="32000000",
        pmcid="PMC7000000",
        content="# Sample Paper\n\nFull text body...",
        url="https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7000000/",
    )
    assert resp.status == "full_text"
    assert resp.source == "pmc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_models.py`
Expected: FAIL (ModuleNotFoundError: No module named 'scholar_mcp')

- [ ] **Step 3: Update `pyproject.toml` and implement config and models**

Update `pyproject.toml` to rename package to `scholar-mcp`, include `pypdf>=5.0.0` and point entrypoint to `scholar_mcp.server:main`.

Create `src/scholar_mcp/config.py`:
```python
import os
from dataclasses import dataclass, field

DEFAULT_SCIHUB_MIRRORS = [
    "https://sci-hub.hkvisa.net",
    "https://sci-hub.mksa.top",
    "https://sci-hub.ren",
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ee",
]

@dataclass
class Settings:
    pubmed_api_key: str | None = None
    pubmed_email: str | None = None
    pubmed_tool: str = "ScholarMCP"
    unpaywall_email: str | None = None
    enable_scihub: bool = True
    force_scihub: bool = False
    scihub_mirrors: list[str] = field(default_factory=lambda: list(DEFAULT_SCIHUB_MIRRORS))
    request_timeout: int = 30
    cache_size: int = 500

    @classmethod
    def load(cls) -> "Settings":
        def _bool(val: str | None, default: bool) -> bool:
            if val is None:
                return default
            return val.strip().lower() in ("1", "true", "yes", "on")

        mirrors_env = os.getenv("SCIHUB_MIRRORS")
        mirrors = [m.strip() for m in mirrors_env.split(",") if m.strip()] if mirrors_env else list(DEFAULT_SCIHUB_MIRRORS)

        return cls(
            pubmed_api_key=os.getenv("PUBMED_API_KEY"),
            pubmed_email=os.getenv("PUBMED_EMAIL"),
            pubmed_tool=os.getenv("PUBMED_TOOL", "ScholarMCP"),
            unpaywall_email=os.getenv("UNPAYWALL_EMAIL") or os.getenv("PUBMED_EMAIL"),
            enable_scihub=_bool(os.getenv("ENABLE_SCIHUB"), True),
            force_scihub=_bool(os.getenv("FORCE_SCIHUB"), False),
            scihub_mirrors=mirrors,
            request_timeout=int(os.getenv("SCHOLAR_REQUEST_TIMEOUT", "30")),
            cache_size=int(os.getenv("SCHOLAR_CACHE_SIZE", "500")),
        )
```

Create `src/scholar_mcp/models.py`:
```python
from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass
class IdentifierMap:
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    title: str | None = None

@dataclass
class PaperMetadata:
    title: str
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    abstract: str = ""
    oa_status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class FullTextResponse:
    status: str  # "full_text" | "abstract_only" | "not_found" | "error"
    source: str  # "pmc" | "europepmc" | "unpaywall" | "scihub" | "abstract_fallback" | "none"
    format: str = "markdown"  # "markdown" | "text"
    title: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    content: str = ""
    url: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class FullTextSummary:
    identifier: str
    status: str
    source: str
    title: str = ""
    url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass
class DownloadResult:
    success: bool
    saved_path: str
    source_used: str
    file_size_bytes: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_models.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/scholar_mcp/config.py src/scholar_mcp/models.py tests/test_config_models.py src/scholar_mcp/__init__.py
git commit -m "feat: scaffold scholar-mcp configuration and core models"
```

---

### Task 2: HTTP Client and In-Memory Cache Utilities

**Files:**
- Create: `src/scholar_mcp/utils/__init__.py`
- Create: `src/scholar_mcp/utils/http.py`
- Create: `src/scholar_mcp/utils/cache.py`
- Test: `tests/test_http_cache.py`

**Interfaces:**
- Produces: `HttpClient` (with exponential backoff, retryable status codes, NCBI credential injector, user agent rotator), `LRUCache` (thread-safe generic in-memory cache).

- [ ] **Step 1: Write the failing test for HTTP and Cache**

```python
# tests/test_http_cache.py
import pytest
import requests
from unittest.mock import patch, MagicMock
from scholar_mcp.utils.http import HttpClient
from scholar_mcp.utils.cache import LRUCache
from scholar_mcp.config import Settings

def test_lru_cache_operations():
    cache = LRUCache(maxsize=2)
    cache.set("a", 1)
    cache.set("b", 2)
    assert cache.get("a") == 1
    cache.set("c", 3)
    assert cache.get("b") is None  # b evicted
    assert cache.get("a") == 1
    assert cache.get("c") == 3

def test_http_client_ncbi_credential_injection():
    settings = Settings(pubmed_api_key="secret-key", pubmed_email="test@example.com", pubmed_tool="TestApp")
    client = HttpClient(settings=settings)
    
    url = client._inject_credentials("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?id=123")
    assert "api_key=secret-key" in url
    assert "email=test%40example.com" in url or "email=test@example.com" in url
    assert "tool=TestApp" in url

    # Non-NCBI URL should not be modified
    other_url = client._inject_credentials("https://api.unpaywall.org/v2/10.1038/abc")
    assert "api_key" not in other_url
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_http_cache.py`
Expected: FAIL

- [ ] **Step 3: Implement `cache.py` and `http.py`**

Implement `src/scholar_mcp/utils/cache.py` with `collections.OrderedDict` and `threading.Lock`.
Implement `src/scholar_mcp/utils/http.py` with retry loop, exponential backoff, jitter, `_is_unexpected_html` check, and session pooling.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_http_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/utils/ tests/test_http_cache.py
git commit -m "feat: add resilient HTTP client and LRU cache utilities"
```

---

### Task 3: JATS XML to Markdown Parser

**Files:**
- Create: `src/scholar_mcp/parsers/__init__.py`
- Create: `src/scholar_mcp/parsers/jats.py`
- Test: `tests/test_jats_parser.py`

**Interfaces:**
- Produces: `jats_to_markdown(xml_content: str | bytes) -> str`

- [ ] **Step 1: Write the failing test for JATS parser**

```python
# tests/test_jats_parser.py
import pytest
from scholar_mcp.parsers.jats import jats_to_markdown

SAMPLE_JATS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <article-meta>
      <title-group>
        <article-title>Mechanisms of Cellular Respiration</article-title>
      </title-group>
      <contrib-group>
        <contrib contrib-type="author">
          <name><surname>Curie</surname><given-names>Marie</given-names></name>
        </contrib>
        <contrib contrib-type="author">
          <name><surname>Franklin</surname><given-names>Rosalind</given-names></name>
        </contrib>
      </contrib-group>
      <abstract>
        <p>This study analyzes oxidative phosphorylation in mitochondria.</p>
      </abstract>
    </article-meta>
  </front>
  <body>
    <sec sec-type="intro">
      <title>Introduction</title>
      <p>Cellular respiration is vital <xref rid="bib1">[1]</xref>.</p>
      <fig id="f1">
        <label>Figure 1</label>
        <caption><p>Diagram of electron transport chain.</p></caption>
      </fig>
      <table-wrap id="t1">
        <label>Table 1</label>
        <caption><p>Reaction rates.</p></caption>
        <table>
          <tr><th>Complex</th><th>Rate</th></tr>
          <tr><td>Complex I</td><td>12.5</td></tr>
        </table>
      </table-wrap>
    </sec>
  </body>
  <back>
    <ref-list>
      <ref id="bib1"><element-citation><article-title>Old Reference</article-title></element-citation></ref>
    </ref-list>
    <fn-group><fn><p>Footnote noise</p></fn></fn-group>
  </back>
</article>
"""

def test_jats_to_markdown_structure():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    assert "# Mechanisms of Cellular Respiration" in md
    assert "Marie Curie" in md and "Rosalind Franklin" in md
    assert "## Abstract" in md
    assert "oxidative phosphorylation" in md
    assert "## Introduction" in md
    assert "Cellular respiration is vital [1]." in md
    assert "[Figure 1] Diagram of electron transport chain." in md
    assert "| Complex | Rate |" in md
    assert "| Complex I | 12.5 |" in md
    # Stripped items
    assert "Old Reference" not in md
    assert "Footnote noise" not in md
    assert "<" not in md and ">" not in md  # No leftover XML tags
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_jats_parser.py`
Expected: FAIL

- [ ] **Step 3: Implement `scholar_mcp/parsers/jats.py`**

Implement ordered DOM tree rendering using `xml.etree.ElementTree` or `BeautifulSoup(..., 'lxml-xml')` following the token-optimized JATS-to-Markdown spec.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_jats_parser.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/parsers/jats.py tests/test_jats_parser.py src/scholar_mcp/parsers/__init__.py
git commit -m "feat: implement JATS XML to clean Markdown parser"
```

---

### Task 4: In-Memory PDF Text Extractor

**Files:**
- Create: `src/scholar_mcp/parsers/pdf.py`
- Test: `tests/test_pdf_parser.py`

**Interfaces:**
- Produces: `pdf_bytes_to_text(pdf_bytes: bytes) -> str`

- [ ] **Step 1: Write the failing test for PDF text extraction**

```python
# tests/test_pdf_parser.py
import io
import pytest
from pypdf import PdfWriter
from scholar_mcp.parsers.pdf import pdf_bytes_to_text

def create_sample_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()

def test_pdf_bytes_to_text_empty():
    pdf_data = create_sample_pdf()
    text = pdf_bytes_to_text(pdf_data)
    assert isinstance(text, str)

def test_pdf_bytes_to_text_corrupt():
    text = pdf_bytes_to_text(b"not-a-valid-pdf")
    assert text == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_parser.py`
Expected: FAIL

- [ ] **Step 3: Implement `scholar_mcp/parsers/pdf.py`**

Use `pypdf.PdfReader` with `io.BytesIO`. Post-process pages, handle word hyphenation across lines and normalize whitespace.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_parser.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/parsers/pdf.py tests/test_pdf_parser.py
git commit -m "feat: implement in-memory PDF text extractor"
```

---

### Task 5: Identifier Resolution Utilities

**Files:**
- Create: `src/scholar_mcp/identifiers.py`
- Test: `tests/test_identifiers.py`

**Interfaces:**
- Produces:
  - `clean_identifier(raw: str) -> tuple[str, str]` (detects type `"pmid" | "pmcid" | "doi" | "title"` and cleaned value)
  - `resolve_identifiers(identifier: str, http_client: HttpClient, cache: LRUCache) -> IdentifierMap`

- [ ] **Step 1: Write the failing test for identifier resolution**

```python
# tests/test_identifiers.py
import pytest
from unittest.mock import MagicMock
from scholar_mcp.identifiers import clean_identifier, resolve_identifiers
from scholar_mcp.models import IdentifierMap
from scholar_mcp.utils.cache import LRUCache

def test_clean_identifier():
    assert clean_identifier("34567890") == ("pmid", "34567890")
    assert clean_identifier("PMID: 34567890") == ("pmid", "34567890")
    assert clean_identifier("PMC8765432") == ("pmcid", "PMC8765432")
    assert clean_identifier("10.1038/s41586-020-2003-7") == ("doi", "10.1038/s41586-020-2003-7")
    assert clean_identifier("https://doi.org/10.1038/s41586-020-2003-7") == ("doi", "10.1038/s41586-020-2003-7")
    assert clean_identifier("A deep learning model for genomics") == ("title", "A deep learning model for genomics")

def test_resolve_identifiers_mocked():
    http_mock = MagicMock()
    cache = LRUCache()
    # Mock NCBI idconv response
    http_mock.get.return_value.status_code = 200
    http_mock.get.return_value.json.return_value = {
        "records": [{"pmid": "32000000", "pmcid": "PMC7000000", "doi": "10.1038/nature123"}]
    }
    res = resolve_identifiers("32000000", http_client=http_mock, cache=cache)
    assert res.pmid == "32000000"
    assert res.pmcid == "PMC7000000"
    assert res.doi == "10.1038/nature123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identifiers.py`
Expected: FAIL

- [ ] **Step 3: Implement `scholar_mcp/identifiers.py`**

Implement regex identification, NCBI idconv API lookup, and CrossRef title-to-DOI lookup.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_identifiers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/identifiers.py tests/test_identifiers.py
git commit -m "feat: implement multi-source identifier resolution"
```

---

### Task 6: Legal Open Access Providers (PMC, Europe PMC, Unpaywall)

**Files:**
- Create: `src/scholar_mcp/providers/__init__.py`
- Create: `src/scholar_mcp/providers/base.py`
- Create: `src/scholar_mcp/providers/pmc.py`
- Create: `src/scholar_mcp/providers/europe_pmc.py`
- Create: `src/scholar_mcp/providers/unpaywall.py`
- Test: `tests/test_oa_providers.py`

**Interfaces:**
- Produces: `PMCProvider`, `EuropePMCProvider`, `UnpaywallProvider` implementing `fetch_full_text(ids: IdentifierMap) -> FullTextResponse | None`.

- [ ] **Step 1: Write failing tests for OA providers**

```python
# tests/test_oa_providers.py
import pytest
from unittest.mock import MagicMock
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.pmc import PMCProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.providers.unpaywall import UnpaywallProvider

def test_pmc_provider_fetch_success():
    http = MagicMock()
    http.get.return_value.status_code = 200
    http.get.return_value.content = b"<article><front><article-meta><title-group><article-title>Test</article-title></title-group></article-meta></front><body><p>Content</p></body></article>"
    provider = PMCProvider(http_client=http)
    ids = IdentifierMap(pmcid="PMC123456")
    res = provider.fetch_full_text(ids)
    assert res is not None
    assert res.status == "full_text"
    assert res.source == "pmc"
    assert "Test" in res.content

def test_unpaywall_provider_fetch_success():
    http = MagicMock()
    # 1. Unpaywall API response
    http.get.side_effect = [
        MagicMock(status_code=200, json=lambda: {
            "is_oa": True,
            "title": "Unpaywall Title",
            "best_oa_location": {"url_for_pdf": "https://oa.org/paper.pdf", "url": "https://oa.org/paper"}
        }),
        # 2. PDF fetch
        MagicMock(status_code=200, content=b"%PDF-sample")
    ]
    provider = UnpaywallProvider(http_client=http, email="test@example.com")
    ids = IdentifierMap(doi="10.1038/sample")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("scholar_mcp.providers.unpaywall.pdf_bytes_to_text", lambda b: "Extracted PDF Body")
        res = provider.fetch_full_text(ids)
    assert res is not None
    assert res.source == "unpaywall"
    assert "Extracted PDF Body" in res.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_oa_providers.py`
Expected: FAIL

- [ ] **Step 3: Implement `base.py`, `pmc.py`, `europe_pmc.py`, and `unpaywall.py`**

Implement provider classes adhering to `BaseProvider` interface and error handling.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_oa_providers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/providers/ tests/test_oa_providers.py
git commit -m "feat: implement PMC, Europe PMC, and Unpaywall OA providers"
```

---

### Task 7: Discovery & Sci-Hub Providers (PubMed, CrossRef, SciHub)

**Files:**
- Create: `src/scholar_mcp/providers/pubmed.py`
- Create: `src/scholar_mcp/providers/crossref.py`
- Create: `src/scholar_mcp/providers/scihub.py`
- Test: `tests/test_search_scihub_providers.py`

**Interfaces:**
- Produces: `PubMedProvider`, `CrossRefProvider` (for searching & metadata retrieval), `SciHubProvider` (mirror rotation & PDF extraction).

- [ ] **Step 1: Write failing test for PubMed, CrossRef, and SciHub**

```python
# tests/test_search_scihub_providers.py
import pytest
from unittest.mock import MagicMock
from scholar_mcp.providers.pubmed import PubMedProvider
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.scihub import SciHubProvider
from scholar_mcp.models import IdentifierMap

def test_scihub_provider_mirror_fallback():
    http = MagicMock()
    # Mirror 1 fails, Mirror 2 returns HTML with PDF iframe, then PDF downloads
    http.get.side_effect = [
        MagicMock(status_code=500),
        MagicMock(status_code=200, text='<html><iframe src="//cyber.sci-hub.se/tree/10.1038/test.pdf#view=fitH"></iframe></html>'),
        MagicMock(status_code=200, content=b"%PDF-scihub-data")
    ]
    provider = SciHubProvider(http_client=http, mirrors=["https://mirror1.org", "https://mirror2.org"])
    ids = IdentifierMap(doi="10.1038/test")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("scholar_mcp.providers.scihub.pdf_bytes_to_text", lambda b: "SciHub Extracted Content")
        res = provider.fetch_full_text(ids)
    assert res is not None
    assert res.source == "scihub"
    assert "SciHub Extracted Content" in res.content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_search_scihub_providers.py`
Expected: FAIL

- [ ] **Step 3: Implement `pubmed.py`, `crossref.py`, and `scihub.py`**

Port and modernize PubMed E-utilities search, CrossRef REST queries, and Sci-Hub mirror scraper.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_search_scihub_providers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/providers/pubmed.py src/scholar_mcp/providers/crossref.py src/scholar_mcp/providers/scihub.py tests/test_search_scihub_providers.py
git commit -m "feat: implement PubMed search, CrossRef metadata, and SciHub scraper providers"
```

---

### Task 8: Waterfall Resolver Pipeline Coordinator

**Files:**
- Create: `src/scholar_mcp/resolver.py`
- Test: `tests/test_waterfall_resolver.py`

**Interfaces:**
- Produces: `WaterfallResolver(settings, http_client, cache)` with methods:
  - `resolve_full_text(identifier: str) -> FullTextResponse`
  - `resolve_full_text_batch(identifiers: list[str]) -> list[FullTextSummary]`
  - `download_article(identifier: str, output_path: str) -> DownloadResult`
  - `search(query: str, source: str, num_results: int, ...) -> list[PaperMetadata]`

- [ ] **Step 1: Write failing test for Waterfall Resolver**

```python
# tests/test_waterfall_resolver.py
import pytest
from unittest.mock import MagicMock
from scholar_mcp.resolver import WaterfallResolver
from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap, FullTextResponse

def test_waterfall_force_scihub_path():
    settings = Settings(force_scihub=True, enable_scihub=True)
    resolver = WaterfallResolver(settings=settings)
    
    # Mock providers
    resolver.identifiers.resolve = MagicMock(return_value=IdentifierMap(doi="10.1038/xyz"))
    resolver.pmc.fetch_full_text = MagicMock(return_value=None)
    resolver.europe_pmc.fetch_full_text = MagicMock(return_value=None)
    resolver.unpaywall.fetch_full_text = MagicMock(return_value=FullTextResponse(status="full_text", source="unpaywall", content="Unpaywall text"))
    resolver.scihub.fetch_full_text = MagicMock(return_value=FullTextResponse(status="full_text", source="scihub", content="SciHub text"))

    res = resolver.resolve_full_text("10.1038/xyz")
    # With FORCE_SCIHUB=True, Unpaywall was skipped in favor of SciHub
    assert res.source == "scihub"
    resolver.unpaywall.fetch_full_text.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_waterfall_resolver.py`
Expected: FAIL

- [ ] **Step 3: Implement `src/scholar_mcp/resolver.py`**

Implement complete prioritized fallback logic according to spec.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_waterfall_resolver.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/resolver.py tests/test_waterfall_resolver.py
git commit -m "feat: implement waterfall full-text resolver coordinator"
```

---

### Task 9: FastMCP Server & MCP Tool Registrations

**Files:**
- Create: `src/scholar_mcp/server.py`
- Test: `tests/test_server_tools.py`

**Interfaces:**
- Produces: FastMCP server `mcp` with tools:
  - `search_papers`
  - `get_full_text`
  - `get_full_text_batch`
  - `download_paper`
  - `deep_paper_analysis_prompt`
  - Entrypoint `main()`

- [ ] **Step 1: Write failing test for server tools**

```python
# tests/test_server_tools.py
import pytest
from unittest.mock import MagicMock, AsyncMock
from scholar_mcp.server import mcp, search_papers, get_full_text, download_paper
from scholar_mcp.models import PaperMetadata, FullTextResponse, DownloadResult

@pytest.mark.asyncio
async def test_server_tool_get_full_text(monkeypatch):
    mock_resolver = MagicMock()
    mock_resolver.resolve_full_text.return_value = FullTextResponse(
        status="full_text",
        source="pmc",
        format="markdown",
        title="Test Title",
        content="# Test Title\n\nFull text content",
    )
    monkeypatch.setattr("scholar_mcp.server.resolver", mock_resolver)
    
    result = await get_full_text("32000000")
    assert result["status"] == "full_text"
    assert result["source"] == "pmc"
    assert "Full text content" in result["content"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_tools.py`
Expected: FAIL

- [ ] **Step 3: Implement `src/scholar_mcp/server.py`**

Wire all 5 tools to the `WaterfallResolver` via `FastMCP("scholar")`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_server_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/server.py tests/test_server_tools.py
git commit -m "feat: implement FastMCP server and tool registrations"
```

---

### Task 10: Documentation, Migration, and End-to-End Verification

**Files:**
- Modify: `README.md`
- Remove / Archive: `src/scihub_mcp/` (replaced by `src/scholar_mcp/`)
- Test: `tests/` (full test suite run)

- [ ] **Step 1: Update README.md with comprehensive tool usage & configuration docs**
- [ ] **Step 2: Remove obsolete legacy files while maintaining backward-compatible module aliases if needed**
- [ ] **Step 3: Run entire pytest test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 4: Commit and tag**

```bash
git add README.md src/ tests/ pyproject.toml
git commit -m "chore: complete migration to scholar-mcp unified architecture"
```
