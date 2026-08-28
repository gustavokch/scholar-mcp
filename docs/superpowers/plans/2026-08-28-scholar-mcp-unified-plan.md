# Unified Academic Discovery & Full-Text MCP Server (`scholar-mcp`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scholar-mcp`, a unified Python FastMCP server combining PubMed discovery and multi-tier waterfall full-text retrieval (PMC JATS XML -> Europe PMC -> Unpaywall -> Sci-Hub -> Abstract fallback) with clean Markdown extraction for AI agents.

**Architecture:** Layered resolver pipeline: Domain Models & Config -> Async HTTP/Cache/RateLimiter utils -> JATS XML & PDF Parsers -> Identifier Converters -> Modular Async Providers (PMC, Europe PMC, Unpaywall, Sci-Hub, PubMed, CrossRef) -> Waterfall Resolver -> FastMCP Server Tools.

**Tech Stack:** Python >= 3.10, `fastmcp>=3.0.0`, `httpx>=0.27.0`, `beautifulsoup4>=4.12.0`, `lxml>=5.0.0`, `pypdf>=5.0.0`, `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`, `respx>=0.21.0`.

**Spec:** `docs/superpowers/specs/2026-08-28-scholar-mcp-unified-design.md`

---

## Design Decisions (resolved during spec review)

These decisions supersede the original spec text where they conflict. The spec must be
amended to match before or alongside Task 1.

| # | Question | Decision |
|---|---|---|
| D1 | Full-text responses can reach 40-80k tokens | **Cap + sections.** `get_full_text` takes `max_chars` (default 50000) and an optional `sections` filter. Response carries `truncated`, `total_chars`, and `sections_available`. |
| D2 | Waterfall is up to 5 serial HTTP hops; batch allows 25 papers | **Rewrite async on `httpx`.** All providers are `async def` over a shared `httpx.AsyncClient`. No `asyncio.to_thread`. Batch fans out with a bounded semaphore. |
| D3 | `download_paper` accepts an arbitrary LLM-supplied path | **Configured root.** `SCHOLAR_DOWNLOAD_DIR` (default `./downloads`). Paths resolve against it; anything escaping the root is rejected. Overwrite requires an explicit flag. |
| D4 | `scihub-mcp` 0.4.0 already on PyPI with Trusted Publishing | **Hard rename** to `scholar-mcp` / `scholar_mcp`. No compatibility shim. `src/scihub_mcp/` is deleted. Requires a new PyPI project and a new Trusted Publisher before the first publish. |

### Review findings folded into this plan

| Finding | Where addressed |
|---|---|
| `FORCE_SCIHUB` does not force; it only reorders past Unpaywall | Renamed `PREFER_SCIHUB_OVER_UNPAYWALL` (Task 1). `ENABLE_SCIHUB=false` always wins. |
| `UNPAYWALL_EMAIL` unset behavior undefined | Tier skipped, reason recorded in the attempt trace (Task 6). |
| No component owns the NCBI rate limit | `AsyncRateLimiter` per host, 3 rps without key / 10 rps with key (Task 2). |
| Title -> DOI can silently return the wrong paper | CrossRef score threshold + `match_score` + `ambiguous_match` status (Task 5). |
| `source="auto"` semantics undefined | Defined: PubMed first, CrossRef top-up, dedupe on normalized DOI (Task 7). |
| `oa_status` per search result would cost 1 Unpaywall call each | Sourced from Europe PMC `isOpenAccess` in one batched query (Task 7). |
| No visibility into why earlier tiers lost | `FullTextResponse.attempts: list[FetchAttempt]` (Task 1, populated in Task 8). |
| No cheap metadata-only path | New `get_metadata` tool (Task 9). |
| Cache TTL and cached-value scope unspecified | TTL LRU; identifier maps and metadata cached, full-text bodies **not** cached (Task 2). |
| `pypdf` / `lxml` / `httpx` missing from dependency list | Tech stack above; `pyproject.toml` in Task 1. |
| No test for `search_papers` or `download_paper`; no mocking library named | `respx` throughout; explicit tests in Tasks 7, 8, 9. |
| CI only runs an import check | `ci.yml` gains a `pytest` step (Task 10). |
| `deep_paper_analysis_prompt` is a tool returning a prompt | Registered as a native FastMCP `@mcp.prompt` **and** kept as a tool for clients without prompt support (Task 9). |

---

## Global Constraints

- Python >= 3.10 compatibility.
- **All I/O is async.** Providers are `async def` and share one `httpx.AsyncClient`. Do not
  introduce `requests` or `asyncio.to_thread` anywhere in the new package.
- Use `pytest` with `pytest-asyncio` for all unit and integration tests. Mock HTTP with `respx`,
  never with live network calls.
- Full text is returned as clean, token-efficient Markdown without citation bibliographies
  (`<ref-list>`), and is always bounded by `max_chars`.
- Zero unhandled network crashes; every tier failure is caught, recorded as a `FetchAttempt`,
  and degrades to the next tier and ultimately to the abstract fallback.
- Every `get_full_text` call respects a total wall-clock budget (`SCHOLAR_TOTAL_BUDGET`,
  default 45s) independent of the per-request timeout.
- `ENABLE_SCIHUB` and `PREFER_SCIHUB_OVER_UNPAYWALL` are respected; `ENABLE_SCIHUB=false`
  disables the Sci-Hub tier regardless of the preference flag.

---

### Task 1: Package Scaffolding, Configuration, and Data Models

**Files:**
- Modify: `pyproject.toml`
- Create: `src/scholar_mcp/__init__.py`
- Create: `src/scholar_mcp/config.py`
- Create: `src/scholar_mcp/models.py`
- Test: `tests/test_config_models.py`

**Interfaces:**
- Produces: `Settings` (from `scholar_mcp.config`), `PaperMetadata`, `FullTextResponse`,
  `FullTextSummary`, `DownloadResult`, `IdentifierMap`, `FetchAttempt` (from `scholar_mcp.models`).

- [x] **Step 1: Write the failing test for models and config**

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
    FetchAttempt,
)


def test_settings_defaults(monkeypatch):
    for var in (
        "PUBMED_API_KEY",
        "ENABLE_SCIHUB",
        "PREFER_SCIHUB_OVER_UNPAYWALL",
        "SCHOLAR_DOWNLOAD_DIR",
        "SCHOLAR_MAX_CHARS",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings.load()
    assert settings.enable_scihub is True
    assert settings.prefer_scihub_over_unpaywall is False
    assert settings.pubmed_api_key is None
    assert settings.max_chars == 50_000
    assert settings.total_budget_seconds == 45
    assert settings.max_concurrency == 5
    assert settings.cache_ttl_seconds == 3600
    assert settings.title_match_threshold == 80.0
    assert settings.download_dir.name == "downloads"
    assert len(settings.scihub_mirrors) > 0


def test_settings_custom_env(monkeypatch):
    monkeypatch.setenv("PREFER_SCIHUB_OVER_UNPAYWALL", "true")
    monkeypatch.setenv("ENABLE_SCIHUB", "false")
    monkeypatch.setenv("PUBMED_API_KEY", "test-key")
    monkeypatch.setenv("SCHOLAR_MAX_CHARS", "1000")
    settings = Settings.load()
    assert settings.prefer_scihub_over_unpaywall is True
    assert settings.enable_scihub is False
    assert settings.pubmed_api_key == "test-key"
    assert settings.max_chars == 1000


def test_scihub_disabled_beats_preference(monkeypatch):
    """ENABLE_SCIHUB=false wins even when the preference flag is set."""
    monkeypatch.setenv("ENABLE_SCIHUB", "false")
    monkeypatch.setenv("PREFER_SCIHUB_OVER_UNPAYWALL", "true")
    settings = Settings.load()
    assert settings.scihub_tier_enabled() is False


def test_ncbi_rate_limit_depends_on_api_key(monkeypatch):
    monkeypatch.delenv("PUBMED_API_KEY", raising=False)
    assert Settings.load().ncbi_rate_limit == 3.0
    monkeypatch.setenv("PUBMED_API_KEY", "k")
    assert Settings.load().ncbi_rate_limit == 10.0


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


def test_full_text_response_carries_truncation_and_trace():
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
        truncated=False,
        total_chars=32,
        sections_available=["Abstract", "Introduction"],
        attempts=[FetchAttempt(tier="pmc", outcome="hit")],
    )
    d = resp.to_dict()
    assert d["truncated"] is False
    assert d["attempts"][0]["tier"] == "pmc"
    assert d["sections_available"] == ["Abstract", "Introduction"]


def test_fetch_attempt_records_skip_reason():
    a = FetchAttempt(tier="unpaywall", outcome="skipped", reason="UNPAYWALL_EMAIL not configured")
    assert a.to_dict()["reason"] == "UNPAYWALL_EMAIL not configured"
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_models.py`
Expected: FAIL (ModuleNotFoundError: No module named 'scholar_mcp')

- [x] **Step 3: Update `pyproject.toml` and implement config and models**

`pyproject.toml` changes for the hard rename (D4):
- `name = "scholar-mcp"`
- `description` updated to describe unified discovery + waterfall full text
- `dependencies = ["fastmcp>=3.0.0", "httpx>=0.27.0", "beautifulsoup4>=4.12.0", "lxml>=5.0.0", "pypdf>=5.0.0"]`
  (`requests` and `urllib3` are removed)
- `[project.optional-dependencies] dev = ["pytest>=8.0.0", "pytest-asyncio>=0.23.0", "respx>=0.21.0"]`
- `[project.scripts] scholar-mcp = "scholar_mcp.server:main"`
- `[tool.hatch.build.targets.wheel] packages = ["src/scholar_mcp"]`
- `[project.urls]` repository links kept as-is unless the GitHub repo is also renamed
- `[tool.pytest.ini_options] asyncio_mode = "auto"`

Create `src/scholar_mcp/config.py`:
```python
import os
from dataclasses import dataclass, field
from pathlib import Path

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
    prefer_scihub_over_unpaywall: bool = False
    scihub_mirrors: list[str] = field(default_factory=lambda: list(DEFAULT_SCIHUB_MIRRORS))
    request_timeout: int = 30
    total_budget_seconds: int = 45
    max_concurrency: int = 5
    cache_size: int = 500
    cache_ttl_seconds: int = 3600
    max_chars: int = 50_000
    title_match_threshold: float = 80.0
    download_dir: Path = field(default_factory=lambda: Path("./downloads"))

    @property
    def ncbi_rate_limit(self) -> float:
        """NCBI E-utilities requests per second: 10 with an API key, 3 without."""
        return 10.0 if self.pubmed_api_key else 3.0

    def scihub_tier_enabled(self) -> bool:
        """ENABLE_SCIHUB is the master switch; the preference flag cannot override it."""
        return self.enable_scihub

    def unpaywall_configured(self) -> bool:
        return bool(self.unpaywall_email)

    @classmethod
    def load(cls) -> "Settings":
        def _bool(val: str | None, default: bool) -> bool:
            if val is None:
                return default
            return val.strip().lower() in ("1", "true", "yes", "on")

        mirrors_env = os.getenv("SCIHUB_MIRRORS")
        mirrors = (
            [m.strip() for m in mirrors_env.split(",") if m.strip()]
            if mirrors_env
            else list(DEFAULT_SCIHUB_MIRRORS)
        )

        return cls(
            pubmed_api_key=os.getenv("PUBMED_API_KEY"),
            pubmed_email=os.getenv("PUBMED_EMAIL"),
            pubmed_tool=os.getenv("PUBMED_TOOL", "ScholarMCP"),
            unpaywall_email=os.getenv("UNPAYWALL_EMAIL") or os.getenv("PUBMED_EMAIL"),
            enable_scihub=_bool(os.getenv("ENABLE_SCIHUB"), True),
            prefer_scihub_over_unpaywall=_bool(
                os.getenv("PREFER_SCIHUB_OVER_UNPAYWALL"), False
            ),
            scihub_mirrors=mirrors,
            request_timeout=int(os.getenv("SCHOLAR_REQUEST_TIMEOUT", "30")),
            total_budget_seconds=int(os.getenv("SCHOLAR_TOTAL_BUDGET", "45")),
            max_concurrency=int(os.getenv("SCHOLAR_MAX_CONCURRENCY", "5")),
            cache_size=int(os.getenv("SCHOLAR_CACHE_SIZE", "500")),
            cache_ttl_seconds=int(os.getenv("SCHOLAR_CACHE_TTL", "3600")),
            max_chars=int(os.getenv("SCHOLAR_MAX_CHARS", "50000")),
            title_match_threshold=float(os.getenv("SCHOLAR_TITLE_MATCH_THRESHOLD", "80")),
            download_dir=Path(os.getenv("SCHOLAR_DOWNLOAD_DIR", "./downloads")),
        )
```

Create `src/scholar_mcp/models.py`:
```python
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class IdentifierMap:
    pmid: str | None = None
    pmcid: str | None = None
    doi: str | None = None
    title: str | None = None
    match_score: float | None = None  # set when resolved from a title query
    ambiguous: bool = False           # True when the best title match is below threshold

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FetchAttempt:
    """One waterfall tier outcome, for debuggability."""

    tier: str      # "pmc" | "europepmc" | "unpaywall" | "scihub" | "abstract_fallback"
    outcome: str   # "hit" | "miss" | "skipped" | "error" | "timeout"
    reason: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    oa_status: str = "unknown"  # "oa" | "closed" | "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullTextResponse:
    status: str  # "full_text" | "abstract_only" | "ambiguous_match" | "not_found" | "error"
    source: str  # "pmc" | "europepmc" | "unpaywall" | "scihub" | "abstract_fallback" | "none"
    format: str = "markdown"  # "markdown" | "text"
    title: str = ""
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    content: str = ""
    url: str | None = None
    truncated: bool = False
    total_chars: int = 0
    sections_available: list[str] = field(default_factory=list)
    attempts: list[FetchAttempt] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FullTextSummary:
    identifier: str
    status: str
    source: str
    title: str = ""
    excerpt: str = ""
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

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_models.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add pyproject.toml src/scholar_mcp/ tests/test_config_models.py
git commit -m "feat: scaffold scholar-mcp configuration and core models"
```

---

### Task 2: Async HTTP Client, Rate Limiter, and TTL Cache

**Files:**
- Create: `src/scholar_mcp/utils/__init__.py`
- Create: `src/scholar_mcp/utils/http.py`
- Create: `src/scholar_mcp/utils/rate_limit.py`
- Create: `src/scholar_mcp/utils/cache.py`
- Test: `tests/test_http_cache.py`

**Interfaces:**
- Produces:
  - `AsyncHttpClient` — wraps one shared `httpx.AsyncClient`; exponential backoff with jitter on
    retryable status codes (429, 500, 502, 503, 504) and transport errors; NCBI credential
    injection; per-host rate limiting; `aclose()` for shutdown.
  - `AsyncRateLimiter(rate_per_sec)` — asyncio token bucket, one instance per host.
  - `TTLCache(maxsize, ttl_seconds)` — LRU with expiry, `asyncio.Lock` protected.

**Caching policy (resolves the "what is cached" gap):** identifier maps and paper metadata are
cached. **Full-text bodies and PDF bytes are never cached** — 500 cached articles would be
hundreds of megabytes resident.

- [x] **Step 1: Write the failing test for HTTP, rate limiting, and cache**

```python
# tests/test_http_cache.py
import asyncio
import time

import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.rate_limit import AsyncRateLimiter


async def test_ttl_cache_lru_eviction():
    cache = TTLCache(maxsize=2, ttl_seconds=60)
    await cache.set("a", 1)
    await cache.set("b", 2)
    assert await cache.get("a") == 1  # refreshes recency of "a"
    await cache.set("c", 3)
    assert await cache.get("b") is None  # "b" was least recently used
    assert await cache.get("a") == 1
    assert await cache.get("c") == 3


async def test_ttl_cache_expiry(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("scholar_mcp.utils.cache.time.monotonic", lambda: clock[0])
    cache = TTLCache(maxsize=10, ttl_seconds=30)
    await cache.set("k", "v")
    assert await cache.get("k") == "v"
    clock[0] += 31
    assert await cache.get("k") is None


async def test_rate_limiter_throttles():
    limiter = AsyncRateLimiter(rate_per_sec=10.0)
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    # 5 tokens at 10/s cannot complete faster than ~0.4s after the initial token
    assert time.monotonic() - start >= 0.3


def test_ncbi_credential_injection():
    settings = Settings(
        pubmed_api_key="secret-key", pubmed_email="test@example.com", pubmed_tool="TestApp"
    )
    client = AsyncHttpClient(settings=settings)
    url = client._inject_credentials(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?id=123"
    )
    assert "api_key=secret-key" in url
    assert "tool=TestApp" in url
    assert "test%40example.com" in url or "test@example.com" in url

    other = client._inject_credentials("https://api.unpaywall.org/v2/10.1038/abc")
    assert "api_key" not in other


@respx.mock
async def test_retries_then_succeeds():
    route = respx.get("https://example.org/data").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, text="ok"),
        ]
    )
    client = AsyncHttpClient(settings=Settings(request_timeout=5), backoff_base=0.01)
    resp = await client.get("https://example.org/data")
    assert resp is not None and resp.text == "ok"
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_returns_none_after_exhausting_retries():
    respx.get("https://example.org/down").mock(return_value=httpx.Response(500))
    client = AsyncHttpClient(settings=Settings(request_timeout=5), max_retries=2, backoff_base=0.01)
    assert await client.get("https://example.org/down") is None
    await client.aclose()


@respx.mock
async def test_ncbi_requests_are_rate_limited(monkeypatch):
    """Without an API key the NCBI host bucket must be 3 rps, not unlimited."""
    respx.get(url__regex=r"https://eutils\.ncbi\.nlm\.nih\.gov/.*").mock(
        return_value=httpx.Response(200, text="ok")
    )
    client = AsyncHttpClient(settings=Settings(pubmed_api_key=None))
    assert client._limiter_for("eutils.ncbi.nlm.nih.gov").rate_per_sec == 3.0
    await client.aclose()
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_http_cache.py`
Expected: FAIL (ModuleNotFoundError)

- [x] **Step 3: Implement `cache.py`, `rate_limit.py`, and `http.py`**

`cache.py` — `TTLCache` over `collections.OrderedDict`, storing `(value, expires_at)` with
`time.monotonic()`. `get` evicts on expiry and moves live entries to the end; `set` evicts the
oldest entry when over `maxsize`. Guard mutations with `asyncio.Lock`.

`rate_limit.py` — `AsyncRateLimiter` token bucket: capacity equals `rate_per_sec`, refills
continuously, `acquire()` sleeps for the shortfall. Expose `rate_per_sec` as a public attribute
so tests can assert configuration.

`http.py` — `AsyncHttpClient` holding one `httpx.AsyncClient(timeout=settings.request_timeout,
follow_redirects=True)`:
- `_limiter_for(host)` returns a memoized `AsyncRateLimiter`; NCBI hosts get
  `settings.ncbi_rate_limit`, all other hosts get a permissive default (e.g. 10 rps).
- `_inject_credentials(url)` appends `api_key`, `email`, and `tool` query params **only** for
  `eutils.ncbi.nlm.nih.gov`.
- `get()` / `get_bytes()` retry retryable statuses and `httpx.TransportError` with exponential
  backoff plus jitter, and return `None` rather than raising once retries are exhausted.
- `_is_unexpected_html(resp)` detects Cloudflare/captcha interstitials served where a PDF was
  expected, so Sci-Hub mirror rotation treats them as misses.
- `aclose()` closes the underlying client; `server.py` calls it on shutdown.

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_http_cache.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/scholar_mcp/utils/ tests/test_http_cache.py
git commit -m "feat: add async HTTP client, per-host rate limiter, and TTL cache"
```

---

### Task 3: JATS XML to Markdown Parser (with section extraction)

**Files:**
- Create: `src/scholar_mcp/parsers/__init__.py`
- Create: `src/scholar_mcp/parsers/jats.py`
- Test: `tests/test_jats_parser.py`

**Interfaces:**
- Produces:
  - `jats_to_markdown(xml_content: str | bytes) -> str`
  - `list_sections(markdown: str) -> list[str]` — heading names present in rendered Markdown
  - `select_sections(markdown: str, wanted: list[str]) -> str` — case-insensitive substring match
    on headings, preserving document order; supports D1's `sections` parameter

> **Corrected from the previous draft:** the old test asserted `"<" not in md and ">" not in md`.
> That contradicts spec section 5.1, which maps `<boxed-text>` to a Markdown `>` blockquote, and
> also breaks on legitimate prose containing `<` or `>`. Assert on an XML-tag regex instead.

- [x] **Step 1: Write the failing test for the JATS parser**

```python
# tests/test_jats_parser.py
import re

import pytest

from scholar_mcp.parsers.jats import jats_to_markdown, list_sections, select_sections

XML_TAG_RE = re.compile(r"</?[a-zA-Z][\w:-]*(\s[^<>]*)?/?>")

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
      <permissions><license><p>Boilerplate licence text</p></license></permissions>
    </article-meta>
  </front>
  <body>
    <sec sec-type="intro">
      <title>Introduction</title>
      <p>Cellular respiration is vital <xref rid="bib1">[1]</xref>.</p>
      <boxed-text><p>Key insight callout.</p></boxed-text>
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
      <sec>
        <title>Sub Background</title>
        <p>Nested section body.</p>
      </sec>
    </sec>
    <sec>
      <title>Methods</title>
      <p>We measured flux with <inline-formula><mml:math><mml:mi>x</mml:mi></mml:math></inline-formula> assays.</p>
      <list list-type="bullet"><list-item><p>First item</p></list-item></list>
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
    assert "> Key insight callout." in md
    assert "- First item" in md


def test_jats_nested_section_depth():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    assert "### Sub Background" in md  # nested one level below "## Introduction"


def test_jats_strips_noise():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    assert "Old Reference" not in md      # <ref-list>
    assert "Footnote noise" not in md     # <fn-group>
    assert "Boilerplate licence text" not in md  # <permissions>


def test_jats_leaves_no_xml_tags():
    """Blockquote '>' is legal Markdown; assert on real XML tags, not bare angle brackets."""
    md = jats_to_markdown(SAMPLE_JATS_XML)
    assert XML_TAG_RE.search(md) is None
    assert "mml:" not in md


def test_jats_handles_malformed_xml():
    assert jats_to_markdown("<article><body><p>unclosed") is not None
    assert jats_to_markdown(b"") == ""


def test_list_and_select_sections():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    sections = list_sections(md)
    assert "Abstract" in sections and "Introduction" in sections and "Methods" in sections

    only_methods = select_sections(md, ["methods"])
    assert "We measured flux" in only_methods
    assert "Cellular respiration is vital" not in only_methods

    # Unknown section names yield an empty selection rather than raising
    assert select_sections(md, ["Nonexistent"]).strip() == ""
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_jats_parser.py`
Expected: FAIL

- [x] **Step 3: Implement `scholar_mcp/parsers/jats.py`**

Parse with `BeautifulSoup(xml, "lxml-xml")` (tolerant of malformed input, unlike
`xml.etree.ElementTree`). Decompose the removed elements listed in spec section 5.1
(`ref-list`, `ref`, `fn-group`, `fn`, `mml:math`, `tex-math`, `permissions`, `license`,
`copyright-holder`, `supplementary-material`, `related-article`) **before** walking the tree, then
render remaining nodes in document order. Map `<sec>` nesting depth to heading level, clamped at
`######`. `list_sections` / `select_sections` operate on the rendered Markdown by scanning ATX
headings, so they work for PDF-derived text too once headings are detected.

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_jats_parser.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/scholar_mcp/parsers/ tests/test_jats_parser.py
git commit -m "feat: implement JATS XML to clean Markdown parser with section selection"
```

---

### Task 4: In-Memory PDF Text Extractor

**Files:**
- Create: `src/scholar_mcp/parsers/pdf.py`
- Test: `tests/test_pdf_parser.py`

**Interfaces:**
- Produces: `pdf_bytes_to_text(pdf_bytes: bytes) -> str`

- [x] **Step 1: Write the failing test for PDF text extraction**

```python
# tests/test_pdf_parser.py
import io

import pytest
from pypdf import PdfWriter

from scholar_mcp.parsers.pdf import pdf_bytes_to_text


def make_blank_pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_pdf_bytes_to_text_returns_string():
    assert isinstance(pdf_bytes_to_text(make_blank_pdf()), str)


def test_pdf_bytes_to_text_multipage_does_not_crash():
    assert isinstance(pdf_bytes_to_text(make_blank_pdf(3)), str)


def test_pdf_bytes_to_text_corrupt_returns_empty():
    assert pdf_bytes_to_text(b"not-a-valid-pdf") == ""


def test_pdf_bytes_to_text_empty_input_returns_empty():
    assert pdf_bytes_to_text(b"") == ""


def test_dehyphenation_and_whitespace(monkeypatch):
    """Post-processing stitches hyphenated line breaks and collapses whitespace."""
    from scholar_mcp.parsers import pdf as pdf_mod

    raw = "This paper presents infor-\nmation about   spacing\n\n\n\nand   breaks."
    cleaned = pdf_mod._postprocess(raw)
    assert "information" in cleaned
    assert "infor-" not in cleaned
    assert "   " not in cleaned


def test_repeated_header_footer_removed():
    from scholar_mcp.parsers import pdf as pdf_mod

    pages = [
        "Journal of Testing\nReal content one\nPage 1",
        "Journal of Testing\nReal content two\nPage 2",
        "Journal of Testing\nReal content three\nPage 3",
    ]
    out = pdf_mod._strip_repeated_lines(pages)
    assert "Journal of Testing" not in out
    assert "Real content two" in out
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_parser.py`
Expected: FAIL

- [x] **Step 3: Implement `scholar_mcp/parsers/pdf.py`**

`pypdf.PdfReader` over `io.BytesIO` — never write to disk. Wrap the whole extraction in
`try/except Exception` and return `""` on corrupt, empty, or encrypted input. Split the helpers so
they are unit-testable: `_strip_repeated_lines(pages)` drops lines appearing on a majority of
pages (running headers/footers), `_postprocess(text)` rejoins `word-\nbreak` hyphenation and
collapses runs of whitespace and blank lines.

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_parser.py -v`
Expected: PASS

- [x] **Step 5: Commit**

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
  - `clean_identifier(raw: str) -> tuple[str, str]` — detects `"pmid" | "pmcid" | "doi" | "title"`
  - `async resolve_identifiers(identifier, http_client, cache, settings) -> IdentifierMap`

**Title-match safety (review finding):** CrossRef `query.bibliographic` always returns a
best-effort hit. Compare the returned `score` against `settings.title_match_threshold` and record
`match_score`. Below threshold, set `ambiguous=True` and leave `doi` unset so the resolver can
return `status="ambiguous_match"` instead of silently fetching the wrong paper.

- [x] **Step 1: Write the failing test for identifier resolution**

```python
# tests/test_identifiers.py
import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.identifiers import clean_identifier, resolve_identifiers
from scholar_mcp.utils.cache import TTLCache
from scholar_mcp.utils.http import AsyncHttpClient

IDCONV = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"
CROSSREF = "https://api.crossref.org/works"


def test_clean_identifier_detects_types():
    assert clean_identifier("34567890") == ("pmid", "34567890")
    assert clean_identifier("PMID: 34567890") == ("pmid", "34567890")
    assert clean_identifier("PMC8765432") == ("pmcid", "PMC8765432")
    assert clean_identifier("pmc8765432") == ("pmcid", "PMC8765432")
    assert clean_identifier("10.1038/s41586-020-2003-7") == ("doi", "10.1038/s41586-020-2003-7")
    assert clean_identifier("https://doi.org/10.1038/s41586-020-2003-7") == (
        "doi",
        "10.1038/s41586-020-2003-7",
    )
    assert clean_identifier("doi:10.1038/abc") == ("doi", "10.1038/abc")
    assert clean_identifier("A deep learning model for genomics") == (
        "title",
        "A deep learning model for genomics",
    )


@respx.mock
async def test_resolve_from_pmid_via_idconv():
    respx.get(url__startswith=IDCONV).mock(
        return_value=httpx.Response(
            200,
            json={"records": [{"pmid": "32000000", "pmcid": "PMC7000000", "doi": "10.1038/nature123"}]},
        )
    )
    client = AsyncHttpClient(settings=Settings())
    res = await resolve_identifiers("32000000", client, TTLCache(), Settings())
    assert (res.pmid, res.pmcid, res.doi) == ("32000000", "PMC7000000", "10.1038/nature123")
    await client.aclose()


@respx.mock
async def test_resolve_title_above_threshold():
    respx.get(url__startswith=CROSSREF).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1038/nature123",
                            "score": 95.0,
                            "title": ["A deep learning model for genomics"],
                        }
                    ]
                }
            },
        )
    )
    respx.get(url__startswith=IDCONV).mock(return_value=httpx.Response(200, json={"records": []}))
    client = AsyncHttpClient(settings=Settings())
    res = await resolve_identifiers(
        "A deep learning model for genomics", client, TTLCache(), Settings()
    )
    assert res.doi == "10.1038/nature123"
    assert res.match_score == 95.0
    assert res.ambiguous is False
    await client.aclose()


@respx.mock
async def test_resolve_title_below_threshold_is_ambiguous():
    """A weak CrossRef match must NOT be treated as a resolved DOI."""
    respx.get(url__startswith=CROSSREF).mock(
        return_value=httpx.Response(
            200,
            json={"message": {"items": [{"DOI": "10.1038/wrong", "score": 12.0, "title": ["Unrelated"]}]}},
        )
    )
    client = AsyncHttpClient(settings=Settings())
    res = await resolve_identifiers("some very obscure phrase", client, TTLCache(), Settings())
    assert res.ambiguous is True
    assert res.doi is None
    assert res.match_score == 12.0
    await client.aclose()


@respx.mock
async def test_resolution_is_cached():
    route = respx.get(url__startswith=IDCONV).mock(
        return_value=httpx.Response(200, json={"records": [{"pmid": "1", "doi": "10.1/a"}]})
    )
    client, cache, settings = AsyncHttpClient(settings=Settings()), TTLCache(), Settings()
    await resolve_identifiers("1", client, cache, settings)
    await resolve_identifiers("1", client, cache, settings)
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_resolution_survives_upstream_failure():
    respx.get(url__startswith=IDCONV).mock(return_value=httpx.Response(500))
    client = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    res = await resolve_identifiers("32000000", client, TTLCache(), Settings())
    assert res.pmid == "32000000"  # input is preserved even when enrichment fails
    await client.aclose()
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_identifiers.py`
Expected: FAIL

- [x] **Step 3: Implement `scholar_mcp/identifiers.py`**

Regex detection order matters: PMCID (`PMC\d+`) before DOI before bare-digit PMID; anything else
is a title. Normalize DOIs by stripping `doi:`, `https://doi.org/`, and trailing punctuation, and
lowercase them for cache keys. Resolution path: titles go to CrossRef first (threshold check),
then any known PMID/PMCID/DOI is expanded through NCBI `idconv`. Cache the resulting
`IdentifierMap` under every identifier it contains. Never raise: on upstream failure return an
`IdentifierMap` populated with whatever the caller supplied.

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_identifiers.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/scholar_mcp/identifiers.py tests/test_identifiers.py
git commit -m "feat: implement identifier resolution with title match thresholding"
```

---

### Task 6: Legal Open Access Providers (Europe PMC, PMC, Unpaywall)

**Files:**
- Create: `src/scholar_mcp/providers/__init__.py`
- Create: `src/scholar_mcp/providers/base.py`
- Create: `src/scholar_mcp/providers/pmc.py`
- Create: `src/scholar_mcp/providers/europe_pmc.py`
- Create: `src/scholar_mcp/providers/unpaywall.py`
- Test: `tests/test_oa_providers.py`

**Interfaces:**
- `BaseProvider` with `tier: str` and `async fetch_full_text(ids: IdentifierMap) -> FullTextResponse | None`.
  Returning `None` means "miss, try the next tier". Providers **never raise** — network faults are
  caught and reported as a miss so the waterfall keeps its shape.
- `PMCProvider`, `EuropePMCProvider`, `UnpaywallProvider`.

**Unpaywall email gap (review finding):** when `settings.unpaywall_configured()` is false the
provider must return `None` immediately without any HTTP call, and expose
`last_skip_reason = "UNPAYWALL_EMAIL not configured"` so the resolver can record a
`FetchAttempt(outcome="skipped")`.

- [ ] **Step 1: Write the failing tests for the OA providers**

```python
# tests/test_oa_providers.py
import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.providers.pmc import PMCProvider
from scholar_mcp.providers.unpaywall import UnpaywallProvider
from scholar_mcp.utils.http import AsyncHttpClient

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL = "https://api.unpaywall.org/v2"

PMC_XML = (
    b"<article><front><article-meta><title-group>"
    b"<article-title>Test</article-title></title-group></article-meta></front>"
    b"<body><sec><title>Results</title><p>Content body.</p></sec></body></article>"
)


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_pmc_provider_hit(client):
    respx.get(url__startswith=EFETCH).mock(return_value=httpx.Response(200, content=PMC_XML))
    res = await PMCProvider(client).fetch_full_text(IdentifierMap(pmcid="PMC123456"))
    assert res is not None
    assert res.status == "full_text" and res.source == "pmc"
    assert "Content body." in res.content
    assert "Results" in res.sections_available


@respx.mock
async def test_pmc_provider_without_pmcid_is_miss(client):
    assert await PMCProvider(client).fetch_full_text(IdentifierMap(doi="10.1/x")) is None


@respx.mock
async def test_pmc_provider_empty_body_is_miss(client):
    """PMC returns 200 with a metadata-only stub for non-OA records; that is a miss."""
    respx.get(url__startswith=EFETCH).mock(
        return_value=httpx.Response(200, content=b"<article><front/></article>")
    )
    assert await PMCProvider(client).fetch_full_text(IdentifierMap(pmcid="PMC1")) is None


@respx.mock
async def test_pmc_provider_upstream_error_is_miss_not_raise(client):
    respx.get(url__startswith=EFETCH).mock(return_value=httpx.Response(500))
    assert await PMCProvider(client).fetch_full_text(IdentifierMap(pmcid="PMC1")) is None


@respx.mock
async def test_europe_pmc_provider_hit(client):
    respx.get(url__regex=rf"{EPMC}/.*fullTextXML").mock(
        return_value=httpx.Response(200, content=PMC_XML)
    )
    res = await EuropePMCProvider(client).fetch_full_text(
        IdentifierMap(pmcid="PMC123456", doi="10.1/x")
    )
    assert res is not None and res.source == "europepmc"
    assert "Content body." in res.content


@respx.mock
async def test_unpaywall_skipped_without_email(client):
    provider = UnpaywallProvider(client, email=None)
    assert await provider.fetch_full_text(IdentifierMap(doi="10.1038/sample")) is None
    assert "UNPAYWALL_EMAIL" in provider.last_skip_reason
    assert respx.calls.call_count == 0  # no HTTP attempted at all


@respx.mock
async def test_unpaywall_provider_hit(client, monkeypatch):
    respx.get(url__startswith=UNPAYWALL).mock(
        return_value=httpx.Response(
            200,
            json={
                "is_oa": True,
                "title": "Unpaywall Title",
                "best_oa_location": {
                    "url_for_pdf": "https://oa.org/paper.pdf",
                    "url": "https://oa.org/paper",
                },
            },
        )
    )
    respx.get("https://oa.org/paper.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-sample")
    )
    monkeypatch.setattr(
        "scholar_mcp.providers.unpaywall.pdf_bytes_to_text", lambda b: "Extracted PDF Body"
    )
    res = await UnpaywallProvider(client, email="test@example.com").fetch_full_text(
        IdentifierMap(doi="10.1038/sample")
    )
    assert res is not None and res.source == "unpaywall"
    assert res.format == "text"
    assert "Extracted PDF Body" in res.content


@respx.mock
async def test_unpaywall_closed_access_is_miss(client):
    respx.get(url__startswith=UNPAYWALL).mock(
        return_value=httpx.Response(200, json={"is_oa": False, "best_oa_location": None})
    )
    res = await UnpaywallProvider(client, email="t@e.com").fetch_full_text(
        IdentifierMap(doi="10.1038/closed")
    )
    assert res is None


@respx.mock
async def test_unpaywall_pdf_yielding_no_text_is_miss(client, monkeypatch):
    """A PDF that extracts to nothing (scanned image) must fall through, not return empty text."""
    respx.get(url__startswith=UNPAYWALL).mock(
        return_value=httpx.Response(
            200,
            json={"is_oa": True, "best_oa_location": {"url_for_pdf": "https://oa.org/scan.pdf"}},
        )
    )
    respx.get("https://oa.org/scan.pdf").mock(return_value=httpx.Response(200, content=b"%PDF"))
    monkeypatch.setattr("scholar_mcp.providers.unpaywall.pdf_bytes_to_text", lambda b: "  ")
    res = await UnpaywallProvider(client, email="t@e.com").fetch_full_text(
        IdentifierMap(doi="10.1038/scan")
    )
    assert res is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_oa_providers.py`
Expected: FAIL

- [ ] **Step 3: Implement `base.py`, `pmc.py`, `europe_pmc.py`, `unpaywall.py`**

`base.py` defines the `BaseProvider` ABC plus a shared `MIN_USEFUL_CHARS` constant (e.g. 200).
Any tier producing less than that after extraction counts as a miss — this is what stops
metadata-only PMC stubs and scanned image PDFs from short-circuiting the waterfall with an
effectively empty body.

`pmc.py` requires a `pmcid`; calls `efetch.fcgi?db=pmc&rettype=xml`, runs `jats_to_markdown`,
populates `sections_available` via `list_sections`.

`europe_pmc.py` tries `/{source}/{id}/fullTextXML` for a PMCID first, then falls back to the
Europe PMC search API to discover an OA record from a DOI. Reuses the same JATS parser.

`unpaywall.py` short-circuits when no email is configured; otherwise queries
`/v2/{doi}?email=...`, prefers `best_oa_location.url_for_pdf` over `.url`, downloads the PDF as
bytes, and extracts with `pdf_bytes_to_text` (`format="text"`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_oa_providers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/providers/ tests/test_oa_providers.py
git commit -m "feat: implement PMC, Europe PMC, and Unpaywall OA providers"
```

---

### Task 7: Discovery & Sci-Hub Providers (PubMed, CrossRef, Sci-Hub)

**Files:**
- Create: `src/scholar_mcp/providers/pubmed.py`
- Create: `src/scholar_mcp/providers/crossref.py`
- Create: `src/scholar_mcp/providers/scihub.py`
- Test: `tests/test_search_scihub_providers.py`

**Interfaces:**
- `PubMedProvider`: `async search(...) -> list[PaperMetadata]`, `async fetch_abstract(ids) -> PaperMetadata | None`
- `CrossRefProvider`: `async search(...) -> list[PaperMetadata]`, `async fetch_metadata(doi) -> PaperMetadata | None`
- `SciHubProvider`: `async fetch_full_text(ids) -> FullTextResponse | None` with mirror rotation
- `async annotate_oa_status(papers, http_client) -> None` (in `europe_pmc.py`) — one batched
  Europe PMC query setting `oa_status` for a whole result page

**`source="auto"` semantics (review finding, previously undefined):**
1. Query PubMed for `num_results`.
2. If PubMed returns fewer than `num_results`, top up from CrossRef.
3. Deduplicate on lowercased DOI; when a DOI is absent, fall back to a normalized-title key.
4. PubMed records win on conflict (richer identifiers).

**Search filter mapping (best effort per backend, documented rather than silently divergent):**

| Filter | PubMed (E-utilities term) | CrossRef |
|---|---|---|
| `author` | `"<name>"[Author]` | `query.author` |
| `journal` | `"<name>"[Journal]` | `query.container-title` |
| `year_start` / `year_end` | `("<start>"[PDAT] : "<end>"[PDAT])` | `filter=from-pub-date:`, `until-pub-date:` |

**`oa_status` sourcing (review finding):** do **not** call Unpaywall per search result — that is
one HTTP request per row, up to 50 per search. Use a single Europe PMC query returning
`isOpenAccess` for the batch.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_search_scihub_providers.py
import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap, PaperMetadata
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import annotate_oa_status
from scholar_mcp.providers.pubmed import PubMedProvider
from scholar_mcp.providers.scihub import SciHubProvider
from scholar_mcp.utils.http import AsyncHttpClient

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
CROSSREF = "https://api.crossref.org/works"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


def test_pubmed_query_builder_applies_filters():
    q = PubMedProvider.build_query(
        "crispr", author="Doudna J", journal="Nature", year_start=2015, year_end=2020
    )
    assert "crispr" in q
    assert '"Doudna J"[Author]' in q
    assert '"Nature"[Journal]' in q
    assert '2015' in q and '2020' in q and "[PDAT]" in q


@respx.mock
async def test_pubmed_search_returns_metadata(client):
    respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["32000000"]}})
    )
    respx.get(url__startswith=ESUMMARY).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "uids": ["32000000"],
                    "32000000": {
                        "title": "A PubMed Paper",
                        "authors": [{"name": "Doudna J"}],
                        "pubdate": "2020 Mar",
                        "fulljournalname": "Nature",
                        "elocationid": "doi: 10.1038/nature123",
                    },
                }
            },
        )
    )
    results = await PubMedProvider(client, Settings()).search("crispr", num_results=5)
    assert len(results) == 1
    assert results[0].title == "A PubMed Paper"
    assert results[0].pmid == "32000000"
    assert results[0].doi == "10.1038/nature123"


@respx.mock
async def test_crossref_search_returns_metadata(client):
    respx.get(url__startswith=CROSSREF).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {
                            "DOI": "10.1038/xref1",
                            "title": ["A CrossRef Paper"],
                            "author": [{"given": "Ada", "family": "Lovelace"}],
                            "container-title": ["Science"],
                            "issued": {"date-parts": [[2019]]},
                        }
                    ]
                }
            },
        )
    )
    results = await CrossRefProvider(client).search("crispr", num_results=5)
    assert results[0].doi == "10.1038/xref1"
    assert "Ada Lovelace" in results[0].authors


@respx.mock
async def test_oa_status_annotated_in_one_batched_call(client):
    """oa_status must cost one request for the whole page, not one per paper."""
    route = respx.get(url__startswith=EPMC).mock(
        return_value=httpx.Response(
            200,
            json={
                "resultList": {
                    "result": [
                        {"doi": "10.1/a", "isOpenAccess": "Y"},
                        {"doi": "10.1/b", "isOpenAccess": "N"},
                    ]
                }
            },
        )
    )
    papers = [
        PaperMetadata(title="A", doi="10.1/a"),
        PaperMetadata(title="B", doi="10.1/b"),
        PaperMetadata(title="C", doi=None),
    ]
    await annotate_oa_status(papers, client)
    assert route.call_count == 1
    assert papers[0].oa_status == "oa"
    assert papers[1].oa_status == "closed"
    assert papers[2].oa_status == "unknown"


@respx.mock
async def test_scihub_mirror_fallback(client, monkeypatch):
    respx.get(url__startswith="https://mirror1.org").mock(return_value=httpx.Response(500))
    respx.get(url__startswith="https://mirror2.org").mock(
        return_value=httpx.Response(
            200,
            text='<html><iframe src="//cyber.sci-hub.se/tree/10.1038/test.pdf#view=fitH"></iframe></html>',
        )
    )
    respx.get(url__regex=r"https://cyber\.sci-hub\.se/.*\.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-scihub-data")
    )
    monkeypatch.setattr(
        "scholar_mcp.providers.scihub.pdf_bytes_to_text", lambda b: "SciHub Extracted Content"
    )
    provider = SciHubProvider(client, mirrors=["https://mirror1.org", "https://mirror2.org"])
    res = await provider.fetch_full_text(IdentifierMap(doi="10.1038/test"))
    assert res is not None and res.source == "scihub"
    assert "SciHub Extracted Content" in res.content


@respx.mock
async def test_scihub_all_mirrors_down_is_miss(client):
    respx.get(url__regex=r"https://mirror\d\.org.*").mock(return_value=httpx.Response(503))
    provider = SciHubProvider(client, mirrors=["https://mirror1.org", "https://mirror2.org"])
    assert await provider.fetch_full_text(IdentifierMap(doi="10.1038/test")) is None


async def test_scihub_without_doi_is_miss(client):
    provider = SciHubProvider(client, mirrors=["https://mirror1.org"])
    assert await provider.fetch_full_text(IdentifierMap(pmid="123")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_search_scihub_providers.py`
Expected: FAIL

- [ ] **Step 3: Implement `pubmed.py`, `crossref.py`, `scihub.py`, and `annotate_oa_status`**

`PubMedProvider.build_query` is a pure static method so filter mapping is testable without HTTP.
Search is `esearch` for the ID list followed by one `esummary` for the page. DOIs come from
`elocationid` or `articleids`.

`CrossRefProvider` uses `query.bibliographic` plus the filter mapping above, and sends a
`User-Agent` with a mailto for the polite pool when an email is configured.

`SciHubProvider` requires a DOI, iterates mirrors in order, extracts the PDF URL from `iframe`,
`embed`, or a raw `.pdf` regex, protocol-relative-corrects `//host/...` to `https://host/...`,
and treats a captcha/HTML interstitial as a mirror miss so rotation continues. Port the working
extraction logic from the existing `src/scihub_mcp/search.py` rather than rewriting it blind.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_search_scihub_providers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/providers/ tests/test_search_scihub_providers.py
git commit -m "feat: implement PubMed, CrossRef, and Sci-Hub providers with batched OA status"
```

---

### Task 8: Waterfall Resolver Pipeline Coordinator

**Files:**
- Create: `src/scholar_mcp/resolver.py`
- Test: `tests/test_waterfall_resolver.py`

**Interfaces:**
- `WaterfallResolver(settings, http_client, cache)`:
  - `async resolve_full_text(identifier, max_chars=None, sections=None) -> FullTextResponse`
  - `async resolve_full_text_batch(identifiers) -> list[FullTextSummary]`
  - `async get_metadata(identifier) -> PaperMetadata | None`
  - `async download_article(identifier, output_path, overwrite=False) -> DownloadResult`
  - `async search(query, source, num_results, ...) -> list[PaperMetadata]`

**Tier order** (spec section 3.2, with the flag renamed):
1. PMC -> 2. Europe PMC -> 3. Unpaywall -> 4. Sci-Hub -> 5. Abstract fallback.
When `prefer_scihub_over_unpaywall` is true, step 3 is skipped and recorded as
`FetchAttempt(tier="unpaywall", outcome="skipped", reason="PREFER_SCIHUB_OVER_UNPAYWALL")`.
When `enable_scihub` is false, step 4 is skipped the same way — **regardless of the preference
flag**, so `ENABLE_SCIHUB=false` plus `PREFER_SCIHUB_OVER_UNPAYWALL=true` still runs Unpaywall.

**Budget:** the whole call runs under `asyncio.timeout(settings.total_budget_seconds)`. On expiry,
whatever tier was in flight is recorded as `outcome="timeout"` and the resolver falls through to
the abstract fallback rather than raising.

**Truncation (D1):** applied once, centrally, after a tier wins. Apply `sections` selection first,
then cut to `max_chars` on a paragraph boundary, appending a truncation marker. Always set
`total_chars` to the **pre-truncation** length and `sections_available` to the full list, so the
caller knows what it did not receive.

**Download sandbox (D3):** `download_article` resolves `output_path` against
`settings.download_dir`, calls `Path.resolve()`, and rejects the write when the result is not
inside the resolved root. Absolute paths outside the root and `..` traversal are both rejected
with `success=False` and an explanatory message — never a raised exception. Existing files are
not overwritten unless `overwrite=True`.

- [ ] **Step 1: Write the failing tests for the resolver**

```python
# tests/test_waterfall_resolver.py
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scholar_mcp.config import Settings
from scholar_mcp.models import FullTextResponse, IdentifierMap, PaperMetadata
from scholar_mcp.resolver import WaterfallResolver


def make_resolver(settings: Settings) -> WaterfallResolver:
    r = WaterfallResolver(settings=settings, http_client=AsyncMock(), cache=None)
    r.resolve_ids = AsyncMock(return_value=IdentifierMap(doi="10.1038/xyz", pmcid="PMC1"))
    for name in ("europe_pmc", "pmc", "unpaywall", "scihub"):
        getattr(r, name).fetch_full_text = AsyncMock(return_value=None)
    r.fetch_abstract = AsyncMock(return_value=None)
    return r


def hit(source: str, content: str = "body text") -> FullTextResponse:
    return FullTextResponse(status="full_text", source=source, content=content)


async def test_europe_pmc_hit_short_circuits():
    r = make_resolver(Settings())
    r.europe_pmc.fetch_full_text.return_value = hit("europepmc")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "europepmc"
    r.pmc.fetch_full_text.assert_not_awaited()
    r.unpaywall.fetch_full_text.assert_not_awaited()


async def test_falls_through_to_pmc():
    r = make_resolver(Settings())
    r.pmc.fetch_full_text.return_value = hit("pmc")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "pmc"
    assert [a.tier for a in res.attempts] == ["europepmc", "pmc"]
    assert res.attempts[0].outcome == "miss"


async def test_falls_through_to_unpaywall():
    r = make_resolver(Settings())
    r.unpaywall.fetch_full_text.return_value = hit("unpaywall")
    assert (await r.resolve_full_text("10.1038/xyz")).source == "unpaywall"


async def test_prefer_scihub_skips_unpaywall():
    r = make_resolver(Settings(prefer_scihub_over_unpaywall=True, enable_scihub=True))
    r.unpaywall.fetch_full_text.return_value = hit("unpaywall")
    r.scihub.fetch_full_text.return_value = hit("scihub")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "scihub"
    r.unpaywall.fetch_full_text.assert_not_awaited()
    skipped = [a for a in res.attempts if a.tier == "unpaywall"][0]
    assert skipped.outcome == "skipped"


async def test_enable_scihub_false_beats_preference():
    """The master switch wins: Unpaywall still runs and Sci-Hub never does."""
    r = make_resolver(Settings(enable_scihub=False, prefer_scihub_over_unpaywall=True))
    r.unpaywall.fetch_full_text.return_value = hit("unpaywall")
    r.scihub.fetch_full_text.return_value = hit("scihub")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.source == "unpaywall"
    r.scihub.fetch_full_text.assert_not_awaited()


async def test_total_failure_falls_back_to_abstract():
    r = make_resolver(Settings())
    r.fetch_abstract.return_value = PaperMetadata(title="T", abstract="An abstract.")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.status == "abstract_only"
    assert res.source == "abstract_fallback"
    assert "An abstract." in res.content


async def test_nothing_at_all_returns_not_found():
    res = await make_resolver(Settings()).resolve_full_text("10.1038/xyz")
    assert res.status == "not_found"
    assert len(res.attempts) == 5


async def test_ambiguous_title_does_not_fetch():
    r = make_resolver(Settings())
    r.resolve_ids = AsyncMock(return_value=IdentifierMap(ambiguous=True, match_score=10.0))
    res = await r.resolve_full_text("a vague phrase")
    assert res.status == "ambiguous_match"
    r.pmc.fetch_full_text.assert_not_awaited()


async def test_truncation_marks_and_reports_total():
    r = make_resolver(Settings(max_chars=20))
    r.pmc.fetch_full_text.return_value = hit("pmc", "x" * 500)
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.truncated is True
    assert res.total_chars == 500
    assert len(res.content) < 500


async def test_section_selection_applied():
    r = make_resolver(Settings())
    r.pmc.fetch_full_text.return_value = hit(
        "pmc", "## Introduction\n\nintro text\n\n## Methods\n\nmethod text\n"
    )
    res = await r.resolve_full_text("10.1038/xyz", sections=["Methods"])
    assert "method text" in res.content
    assert "intro text" not in res.content


async def test_budget_exhaustion_degrades_to_abstract():
    r = make_resolver(Settings(total_budget_seconds=1))

    async def slow(_ids):
        await asyncio.sleep(5)

    r.pmc.fetch_full_text = AsyncMock(side_effect=slow)
    r.fetch_abstract.return_value = PaperMetadata(title="T", abstract="Fallback abstract.")
    res = await r.resolve_full_text("10.1038/xyz")
    assert res.status == "abstract_only"
    assert any(a.outcome == "timeout" for a in res.attempts)


async def test_batch_is_concurrent_and_bounded():
    r = make_resolver(Settings(max_concurrency=2))
    r.pmc.fetch_full_text.return_value = hit("pmc")
    out = await r.resolve_full_text_batch([f"10.1/{i}" for i in range(6)])
    assert len(out) == 6
    assert all(s.status == "full_text" for s in out)


async def test_batch_rejects_oversized_input():
    with pytest.raises(ValueError):
        await make_resolver(Settings()).resolve_full_text_batch([f"10.1/{i}" for i in range(26)])


async def test_download_rejects_path_escape(tmp_path):
    r = make_resolver(Settings(download_dir=tmp_path))
    res = await r.download_article("10.1038/xyz", "../../etc/passwd")
    assert res.success is False
    assert "outside" in res.message.lower()


async def test_download_rejects_absolute_path_outside_root(tmp_path):
    r = make_resolver(Settings(download_dir=tmp_path))
    res = await r.download_article("10.1038/xyz", "/etc/passwd")
    assert res.success is False


async def test_download_refuses_overwrite_without_flag(tmp_path):
    (tmp_path / "p.pdf").write_bytes(b"existing")
    r = make_resolver(Settings(download_dir=tmp_path))
    r.fetch_pdf_bytes = AsyncMock(return_value=(b"%PDF-new", "pmc"))
    res = await r.download_article("10.1038/xyz", "p.pdf")
    assert res.success is False
    assert "exists" in res.message.lower()
    assert (tmp_path / "p.pdf").read_bytes() == b"existing"


async def test_download_writes_inside_root(tmp_path):
    r = make_resolver(Settings(download_dir=tmp_path))
    r.fetch_pdf_bytes = AsyncMock(return_value=(b"%PDF-data", "unpaywall"))
    res = await r.download_article("10.1038/xyz", "sub/paper.pdf")
    assert res.success is True
    assert res.file_size_bytes == len(b"%PDF-data")
    assert Path(res.saved_path).read_bytes() == b"%PDF-data"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_waterfall_resolver.py`
Expected: FAIL

- [ ] **Step 3: Implement `src/scholar_mcp/resolver.py`**

Build the tier list as `(name, coroutine_factory, skip_reason_or_None)` tuples so skipping and
tracing share one code path and the flag interaction is expressed once. Time each tier with
`time.monotonic()` into `FetchAttempt.elapsed_ms`. Wrap the tier loop in
`asyncio.timeout(total_budget_seconds)`.

`resolve_full_text_batch` raises `ValueError` above 25 identifiers, then runs
`asyncio.gather` over a `asyncio.Semaphore(settings.max_concurrency)`, mapping each result to a
`FullTextSummary` with a short excerpt. Individual failures become `status="error"` entries — one
bad identifier must not fail the batch.

`download_article` reuses the waterfall to obtain PDF bytes (`fetch_pdf_bytes`), applies the
sandbox check described above, creates parent directories inside the root, and writes with
`asyncio.to_thread` **only for the local disk write** — that is filesystem I/O, not network, and is
the one permitted use.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_waterfall_resolver.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/resolver.py tests/test_waterfall_resolver.py
git commit -m "feat: implement waterfall resolver with trace, budget, and download sandbox"
```

---

### Task 9: FastMCP Server & MCP Tool Registrations

**Files:**
- Create: `src/scholar_mcp/server.py`
- Test: `tests/test_server_tools.py`

**Interfaces:**
- FastMCP server `mcp` exposing six tools:
  - `search_papers(query, source="auto", num_results=10, year_start=None, year_end=None, author=None, journal=None)`
  - `get_full_text(identifier, max_chars=None, sections=None)`
  - `get_full_text_batch(identifiers)`
  - `get_metadata(identifier)` — **new**; abstract plus resolved IDs without running the waterfall
  - `download_paper(identifier, output_path, overwrite=False)`
  - `deep_paper_analysis_prompt(identifier)`
- Plus `@mcp.prompt` registration of `deep_paper_analysis` for clients supporting the MCP prompts
  primitive. The tool form is retained for clients that do not.
- Entrypoint `main()`; providers share one `AsyncHttpClient`, closed on shutdown.

`num_results` is clamped to 50 and `identifiers` to 25 **at the tool boundary**, so a malformed LLM
call returns a clear error rather than a stack trace.

- [ ] **Step 1: Write the failing tests for the server tools**

```python
# tests/test_server_tools.py
from unittest.mock import AsyncMock

import pytest

from scholar_mcp import server as srv
from scholar_mcp.models import DownloadResult, FullTextResponse, FullTextSummary, PaperMetadata


@pytest.fixture
def resolver(monkeypatch):
    r = AsyncMock()
    monkeypatch.setattr(srv, "resolver", r)
    return r


async def test_get_full_text_tool(resolver):
    resolver.resolve_full_text.return_value = FullTextResponse(
        status="full_text", source="pmc", title="Test Title", content="# Test Title\n\nFull text content"
    )
    result = await srv.get_full_text("32000000")
    assert result["status"] == "full_text"
    assert result["source"] == "pmc"
    assert "Full text content" in result["content"]


async def test_get_full_text_forwards_max_chars_and_sections(resolver):
    resolver.resolve_full_text.return_value = FullTextResponse(status="full_text", source="pmc")
    await srv.get_full_text("32000000", max_chars=1000, sections=["Methods"])
    kwargs = resolver.resolve_full_text.await_args.kwargs
    assert kwargs["max_chars"] == 1000
    assert kwargs["sections"] == ["Methods"]


async def test_search_papers_tool(resolver):
    resolver.search.return_value = [PaperMetadata(title="A Paper", doi="10.1/a", oa_status="oa")]
    results = await srv.search_papers("crispr", num_results=5)
    assert results[0]["title"] == "A Paper"
    assert results[0]["oa_status"] == "oa"


async def test_search_papers_clamps_num_results(resolver):
    resolver.search.return_value = []
    await srv.search_papers("crispr", num_results=500)
    assert resolver.search.await_args.kwargs["num_results"] == 50


async def test_get_metadata_tool_does_not_run_waterfall(resolver):
    resolver.get_metadata.return_value = PaperMetadata(title="Meta", pmid="1", abstract="abs")
    result = await srv.get_metadata("1")
    assert result["title"] == "Meta"
    resolver.resolve_full_text.assert_not_awaited()


async def test_batch_tool_rejects_over_limit(resolver):
    result = await srv.get_full_text_batch([f"10.1/{i}" for i in range(26)])
    assert result[0]["status"] == "error"
    resolver.resolve_full_text_batch.assert_not_awaited()


async def test_batch_tool_success(resolver):
    resolver.resolve_full_text_batch.return_value = [
        FullTextSummary(identifier="10.1/a", status="full_text", source="pmc", excerpt="body")
    ]
    result = await srv.get_full_text_batch(["10.1/a"])
    assert result[0]["source"] == "pmc"


async def test_download_paper_tool_reports_failure_cleanly(resolver):
    resolver.download_article.return_value = DownloadResult(
        success=False, saved_path="", source_used="none", message="Path outside download root"
    )
    result = await srv.download_paper("10.1/a", "../escape.pdf")
    assert result["success"] is False
    assert "outside" in result["message"].lower()


async def test_tool_exceptions_become_structured_errors(resolver):
    resolver.resolve_full_text.side_effect = RuntimeError("boom")
    result = await srv.get_full_text("32000000")
    assert result["status"] == "error"
    assert "boom" in result["error"]


async def test_deep_analysis_prompt_includes_content(resolver):
    resolver.resolve_full_text.return_value = FullTextResponse(
        status="full_text", source="pmc", title="Deep Paper", content="Method details here."
    )
    result = await srv.deep_paper_analysis_prompt("10.1/a")
    assert "Deep Paper" in result["analysis_prompt"]
    assert "Method details here." in result["analysis_prompt"]


def test_all_tools_registered():
    expected = {
        "search_papers",
        "get_full_text",
        "get_full_text_batch",
        "get_metadata",
        "download_paper",
        "deep_paper_analysis_prompt",
    }
    for name in expected:
        assert callable(getattr(srv, name)), f"{name} is not exposed by scholar_mcp.server"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_tools.py`
Expected: FAIL

- [ ] **Step 3: Implement `src/scholar_mcp/server.py`**

Construct `Settings.load()`, one `AsyncHttpClient`, one `TTLCache`, and one `WaterfallResolver` at
module scope so tests can monkeypatch `srv.resolver`. Every tool body is a thin `try/except` that
converts exceptions into a structured `{"status": "error", "error": str(e)}` payload — the tool
layer never raises into the MCP transport. Tools are plain `async def` calling the resolver
directly; there is no `asyncio.to_thread` bridge any more (D2).

Docstrings are the tool descriptions the model reads, so state the identifier formats accepted,
the `max_chars` default, and that `download_paper` writes only inside `SCHOLAR_DOWNLOAD_DIR`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_server_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/server.py tests/test_server_tools.py
git commit -m "feat: implement FastMCP server with six tools and prompt registration"
```

---

### Task 10: Documentation, Hard Rename, CI, and End-to-End Verification

**Files:**
- Modify: `README.md`, `AGENTS.md`, `CHANGELOG.md`, `.github/workflows/ci.yml`
- Delete: `src/scihub_mcp/`
- Delete: `PubMed-MCP-Server/` (vendored reference source, once porting is complete)

**Hard rename consequences (D4) — all must be handled, not just the package directory:**
- `src/scihub_mcp/` is deleted outright. No compatibility shim, no alias module.
- `scholar-mcp` is a **new PyPI project**. A new Trusted Publisher must be configured at
  pypi.org before the first publish; the existing `scihub-mcp` publisher does not carry over.
- The console script changes `scihub-mcp` -> `scholar-mcp`. Every existing user's
  `mcpServers` config breaks. README must carry a prominent migration note showing the old and
  new config blocks side by side.
- `CHANGELOG.md` gets a `1.0.0` entry documenting the rename as a breaking change and listing
  the renamed env var `FORCE_SCIHUB` -> `PREFER_SCIHUB_OVER_UNPAYWALL`.
- Version resets to `1.0.0` in `pyproject.toml` and `src/scholar_mcp/__init__.py` (a new project
  starting at `0.4.0` would be confusing).
- The GitHub repository is renamed `w8s/scihub-mcp` -> `w8s/scholar-mcp`, and every
  `[project.urls]` entry in `pyproject.toml` is updated to the new path. GitHub redirects the old
  URL, so existing clones and inbound links keep working.

- [ ] **Step 1: Update `README.md`**

Cover: the waterfall order and what each tier requires; the full env var table including the new
`SCHOLAR_DOWNLOAD_DIR`, `SCHOLAR_MAX_CHARS`, `SCHOLAR_TOTAL_BUDGET`, `SCHOLAR_MAX_CONCURRENCY`,
`SCHOLAR_CACHE_TTL`, `SCHOLAR_TITLE_MATCH_THRESHOLD`; all six tools with example calls; and the
migration note from `scihub-mcp`.

Add a credit for JackKuo666 (MIT, Copyright (c) 2025) beside the existing CyberKrypton credit,
covering the PubMed search logic ported from `PubMed-MCP-Server/`. Retain the MIT copyright notice
in any module that reuses a substantial portion of that code.

- [ ] **Step 2: Update `AGENTS.md`**

The current file documents the old two-module layout and states two design decisions that this
plan reverses. Rewrite: the new package tree; **async-first on `httpx`** replacing the
"`asyncio.to_thread` bridge" decision; provider-per-tier structure replacing "CrossRef first";
the caching policy (IDs and metadata cached, bodies never); and remove the "No test suite yet"
limitation now that one exists.

- [ ] **Step 3: Add a test step to `.github/workflows/ci.yml`**

The job currently installs the package and runs an import check only. Change the install to
`pip install -e ".[dev]"`, update the import check to `scholar_mcp.server`, and add:

```yaml
      - name: Run tests
        run: pytest -v
```

- [ ] **Step 4: Delete the legacy package and vendored reference**

```bash
git rm -r src/scihub_mcp
git rm -r --cached PubMed-MCP-Server   # if it was ever tracked; otherwise delete locally
```

- [ ] **Step 5: Run the full suite and verify the server starts**

```bash
pytest -v
python -c "from scholar_mcp.server import main; print('Import OK')"
```
Expected: all tests PASS, import OK.

- [ ] **Step 6: Manual smoke test against live services (not part of CI)**

With `PUBMED_EMAIL` and `UNPAYWALL_EMAIL` set, confirm by hand:
- a known OA PMC article returns `source="pmc"` with real Markdown
- a closed-access DOI reaches the Sci-Hub or abstract tier and reports a coherent `attempts` trace
- `download_paper` writes inside `SCHOLAR_DOWNLOAD_DIR` and refuses `../` escape

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "chore!: migrate to scholar-mcp unified architecture

BREAKING CHANGE: package renamed scihub-mcp -> scholar-mcp, console script
renamed, FORCE_SCIHUB renamed to PREFER_SCIHUB_OVER_UNPAYWALL."
```

---

## Resolved Items (decided 2026-08-28)

Every item Revision 2 left open is now decided. Implement against these answers.

1. **GitHub repository rename — yes.** Rename `w8s/scihub-mcp` -> `w8s/scholar-mcp` and update
   `[project.urls]` in Task 1 to the new path (Task 10 records the consequence).
2. **Sci-Hub default — unchanged.** `ENABLE_SCIHUB` stays `default=True` in `Settings`. Do not
   flip it. The gating table already guarantees that setting it to `false` leaves Unpaywall
   running.
3. **`PubMed-MCP-Server/` provenance — confirmed MIT** (Copyright (c) 2025 JackKuo666), compatible
   with this project's MIT licence. Porting its PubMed search logic is permitted. Task 7 may reuse
   it; Task 10 adds the README credit and retains the copyright notice in any module carrying a
   substantial portion. The directory stays untracked and is deleted once porting is complete.
4. **Europe PMC before PMC — yes.** Waterfall steps 1 and 2 are swapped: Europe PMC OA is tier 1,
   PMC OA is tier 2. Europe PMC mirrors the PMC corpus and adds preprints and non-US Open Access
   records, serves full text and Open Access status from one REST endpoint, and is outside the
   NCBI rate limit that every E-utilities call must share with identifier resolution and the
   abstract fallback. Task 8 builds the tier list in this order; the Task 8 tests assert it.
