# Brazilian MoH Guidelines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two MCP tools that discover Brazilian Ministry of Health technical publications through the BVS/iAHx search API and retrieve their full text as Markdown.

**Architecture:** One new engine module, `medical/brazil_moh.py`, structured like the existing `medical/who_iris.py`. It composes Solr filters into the `q` parameter, deduplicates heavily-duplicated results, and resolves full text through a host-allowlisted `fi-admin.bvsalud.org` redirect to a PDF. Registration follows the established medical pattern: dataclass in `medical/models.py`, formatter in `medical/formatters.py`, TTL in `config.py`, two tools in `server.py`.

**Tech Stack:** Python 3.10+, `httpx` via `AsyncHttpClient`, `aiosqlite` via `SQLiteCacheManager`, `pypdf` via `parsers/pdf.py`, `pytest` + `respx` for tests.

**Spec:** `docs/superpowers/specs/2026-09-07-brazil-moh-guidelines-design.md`

## Global Constraints

- Async-first on `httpx`. No `requests`, no `urllib3`, no new `asyncio.to_thread` calls.
- Providers never raise on network failure or unexpected payloads. They report a miss and degrade.
- Both MCP tools catch `Exception` and return `{"status": "error", "error": str(ex), "source": "brazil-moh"}`.
- Every async test closes the cache and HTTP client in `try/finally`. A test that fails without closing them hangs pytest at finalize instead of reporting the failure.
- Tests make no network calls. All HTTP is mocked with `respx`.
- Run the full suite with `pytest -v` from the repo root, with the venv active (`source .venv/bin/activate`).

## Three Spec Corrections

All three were found by reading the code the spec said to rely on. Implement the plan's version, not the spec's.

**1. Do not use `utils/deduplication.py`.** The spec says to reuse it. `deduplicate_papers(papers, similarity_threshold=0.9)` does fuzzy title matching with Levenshtein distance over paper dicts carrying `doi`/`title`/`authors`, and returns a `(list, stats)` tuple. Our dedup is exact equality on the `id` field. Running Levenshtein over 150 records to compare strings that are already exact keys is both slower and wrong — two genuinely distinct guidelines with similar titles would be collapsed. Task 4 uses a local ordered pass instead.

**2. Tests use `respx`, not a fake client object.** The spec describes asserting against "the fake client's call log". `tests/medical/test_who_iris.py` uses `@respx.mock` with a real `AsyncHttpClient`. The equivalent assertion is `route.called is False` on a mocked route.

**3. `_is_unexpected_html` does not gate on content type.** The spec claims it "already treats an HTML body as a miss, which is what a WAF interstitial returns". It does not. Reading `utils/http.py:127`, it returns `True` only when the body also contains one of `cloudflare`, `ddg`, `challenge-platform`, `just a moment`, `captcha`, or `attention required`. A plain `Estamos em manutenção` page — exactly what gov.br served during the probes — passes straight through, and `pdf_bytes_to_text` would then be handed HTML. Task 5 therefore uses `http_client.get()` and gates on `content-type` explicitly, rather than `get_bytes()`.

Confirmed while checking the above, and relied on by Task 5: `AsyncHttpClient` is constructed with `follow_redirects=True`, so the fi-admin 302 to `docs.bvsalud.org` is followed automatically. Per-request `headers` override the client's default `User-Agent`, which is what lets `BVS_HEADERS` defeat the 403.

## File Structure

| File | Responsibility |
|---|---|
| `src/scholar_mcp/medical/brazil_moh.py` | **Create.** Module constants, pure parse helpers, query builder, `BrazilMoHEngine`. |
| `src/scholar_mcp/medical/models.py` | **Modify.** Add `BrazilGuideline` dataclass. |
| `src/scholar_mcp/medical/formatters.py` | **Modify.** Add `format_brazil_moh_guidelines`. |
| `src/scholar_mcp/config.py` | **Modify.** Add `cache_ttl_brazil_moh` field and env load. |
| `src/scholar_mcp/utils/sqlite_cache.py` | **Modify.** Add `brazil_moh` to the `_ttl_for` source map. |
| `src/scholar_mcp/server.py` | **Modify.** Construct engine, register two tools. |
| `tests/medical/test_brazil_moh.py` | **Create.** Engine and helper tests. |
| `tests/test_config_medical.py` | **Modify.** Assert the new default and env override. |
| `tests/test_server_medical.py` | **Modify.** Add both tool names to `MEDICAL_TOOLS`. |

---

### Task 1: Cache TTL setting

**Files:**
- Modify: `src/scholar_mcp/config.py:68` (after `cache_ttl_who_iris`), `src/scholar_mcp/config.py:166` (after the `cache_ttl_who_iris` env load)
- Modify: `src/scholar_mcp/utils/sqlite_cache.py:64` (inside `_ttl_for`'s `by_source` dict)
- Test: `tests/test_config_medical.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Settings.cache_ttl_brazil_moh: int` (default `2592000`), environment variable `CACHE_TTL_BRAZIL_MOH`, and the cache source key `"brazil_moh"`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_config_medical.py`, add this assertion inside the existing `test_medical_settings_defaults` function, immediately after the `assert settings.cache_ttl_clinical_trials == 86400` line:

```python
    assert settings.cache_ttl_brazil_moh == 2592000
```

Then add this new test at the end of the file:

```python
def test_brazil_moh_ttl_env_override(monkeypatch):
    monkeypatch.setenv("CACHE_TTL_BRAZIL_MOH", "600")
    settings = Settings.load()
    assert settings.cache_ttl_brazil_moh == 600


def test_brazil_moh_cache_source_uses_its_own_ttl(tmp_path):
    from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

    settings = Settings.load()
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    assert cache._ttl_for("brazil_moh", None) == 2592000
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_medical.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'cache_ttl_brazil_moh'`

- [ ] **Step 3: Add the setting**

In `src/scholar_mcp/config.py`, add this line directly after `cache_ttl_who_iris: int = 2592000`:

```python
    cache_ttl_brazil_moh: int = 2592000
```

In the same file, inside `Settings.load()`, add this line directly after the `cache_ttl_who_iris=int(os.getenv("CACHE_TTL_WHO_IRIS", "2592000")),` line:

```python
            cache_ttl_brazil_moh=int(os.getenv("CACHE_TTL_BRAZIL_MOH", "2592000")),
```

In `src/scholar_mcp/utils/sqlite_cache.py`, inside `_ttl_for`, add this entry to the `by_source` dict after the `"who_iris"` entry:

```python
            "brazil_moh": self.settings.cache_ttl_brazil_moh,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config_medical.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/config.py src/scholar_mcp/utils/sqlite_cache.py tests/test_config_medical.py
git commit -m "feat(config): add brazil_moh cache TTL setting"
```

---

### Task 2: BrazilGuideline model and pure parse helpers

**Files:**
- Create: `src/scholar_mcp/medical/brazil_moh.py`
- Modify: `src/scholar_mcp/medical/models.py` (append after `WHOGuideline`)
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BrazilGuideline` dataclass with `to_dict() -> dict[str, Any]` and `from_dict(data) -> BrazilGuideline`.
  - `_first(value) -> str`, `_as_list(value) -> list[str]`, `_parse_issued(value) -> tuple[str, str]`, `_parse_country(value) -> str`, `_derive_fulltext_id(url) -> str` in `brazil_moh.py`.
  - Constants `BVS_SEARCH_URL`, `BVS_HEADERS`, `FI_ADMIN_DOC_RE`, `FULLTEXT_ALLOWED_HOSTS`, `MAX_RESULTS`, `OVERFETCH_FACTOR`, `MAX_PAGE_SIZE`, `MAX_FULL_TEXT_CHARS`, `BASE_FILTER`, `BRISA_FILTER`, `VALID_COLLECTIONS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/medical/test_brazil_moh.py`:

```python
from scholar_mcp.medical.brazil_moh import (
    _as_list,
    _derive_fulltext_id,
    _first,
    _parse_country,
    _parse_issued,
)
from scholar_mcp.medical.models import BrazilGuideline


def test_first_returns_first_list_element():
    assert _first(["a", "b"]) == "a"


def test_first_returns_scalar_as_string():
    assert _first(202609) == "202609"


def test_first_returns_empty_string_for_empty_or_none():
    assert _first([]) == ""
    assert _first(None) == ""


def test_as_list_wraps_scalar_and_drops_empties():
    assert _as_list("pt") == ["pt"]
    assert _as_list(["pt", "", "en"]) == ["pt", "en"]
    assert _as_list(None) == []


def test_parse_issued_splits_year_and_month():
    assert _parse_issued("202609") == ("2026", "2026-09")


def test_parse_issued_accepts_year_only():
    assert _parse_issued("2026") == ("2026", "2026")


def test_parse_issued_returns_empty_for_missing_or_malformed():
    assert _parse_issued("") == ("", "")
    assert _parse_issued(None) == ("", "")
    assert _parse_issued("n/d") == ("", "")


def test_parse_country_reads_the_e_subfield():
    raw = "^iBrazil^eBrasil^pBrasil^fBrésil"
    assert _parse_country([raw]) == "Brasil"


def test_parse_country_handles_multiword_value():
    raw = "^iEl Salvador^eEl Salvador^pEl Salvador"
    assert _parse_country([raw]) == "El Salvador"


def test_parse_country_returns_empty_when_absent():
    assert _parse_country([]) == ""
    assert _parse_country(["no subfields here"]) == ""


def test_derive_fulltext_id_matches_fi_admin_url():
    url = "https://fi-admin.bvsalud.org/document/view/cfpaj"
    assert _derive_fulltext_id(url) == "cfpaj"


def test_derive_fulltext_id_is_empty_for_offsite_url():
    assert _derive_fulltext_id("https://www.sciencedirect.com/science/article/pii/S123") == ""
    assert _derive_fulltext_id("") == ""


def test_brazil_guideline_roundtrips_and_ignores_unknown_keys():
    guideline = BrazilGuideline(
        title="Protocolo Clínico",
        record_id="biblio-1701387",
        document_url="https://fi-admin.bvsalud.org/document/view/cfpaj",
    )
    data = guideline.to_dict()
    assert data["source"] == "brazil-moh"
    assert data["score"] is None
    restored = BrazilGuideline.from_dict({**data, "unexpected_key": 1})
    assert restored.record_id == "biblio-1701387"


def test_brazil_guideline_from_dict_handles_none():
    assert BrazilGuideline.from_dict(None).title == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/medical/test_brazil_moh.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scholar_mcp.medical.brazil_moh'`

- [ ] **Step 3: Add the model**

In `src/scholar_mcp/medical/models.py`, append after the `WHOGuideline` class:

```python
@dataclass
class BrazilGuideline:
    """A Brazilian Ministry of Health technical publication from BVS/iAHx.

    ``fulltext_id`` is set only when ``document_url`` points at a
    fi-admin document view; it is empty when the record links off-site.
    ``score`` is reserved for a future ranking pass and is unset in v1.
    """

    title: str = ""
    title_en: str = ""
    record_id: str = ""
    document_url: str = ""
    fulltext_id: str = ""
    source: str = "brazil-moh"
    abstract: str = ""
    year: str = ""
    issued: str = ""
    country: str = ""
    authors: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    mesh_subjects: list[str] = field(default_factory=list)
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BrazilGuideline":
        if not data or not isinstance(data, dict):
            return cls()
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**fields)
```

- [ ] **Step 4: Create the module with constants and helpers**

Create `src/scholar_mcp/medical/brazil_moh.py`:

```python
"""Brazilian Ministry of Health technical publications via BVS/iAHx.

Discovery uses the BVS portal search API. Several of its behaviours are
counter-intuitive and are load-bearing for this module:

* ``fq`` is silently ignored, so every filter is composed into ``q``.
* The default boolean operator is OR, so user tokens are joined with AND.
* ``pais_publicacao`` is subfield-encoded and is neither exact-matchable
  nor wildcard-searchable, so Brazil scoping is ``la:"pt"`` server-side
  plus a client-side assertion on the parsed country.
* Records are duplicated across indexing collections at roughly 2.1-2.3x,
  so the engine over-fetches and trims after deduplication.

Results are served in the order BVS returns them. No re-ranking is applied:
``ScoringEngine`` does not fold accents or strip Portuguese stopwords, so
blending it against a Solr ordering tuned for this corpus would degrade it.
``BrazilGuideline.score`` is the seam for adding that later.
"""

import logging
import re
import urllib.parse
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import BrazilGuideline
from scholar_mcp.parsers.pdf import pdf_bytes_to_text
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager
from scholar_mcp.utils.text import truncate_content

BVS_SEARCH_URL = "https://pesquisa.bvsalud.org/portal/"

# The repo default User-Agent receives HTTP 403 from this host.
BVS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

FI_ADMIN_DOC_RE = re.compile(
    r"^https?://fi-admin\.bvsalud\.org/document/view/([A-Za-z0-9._-]+)/?$"
)

# The full-text fetch follows a URL taken from record content while the
# record id is caller-controlled. Without this allowlist the tool would act
# as a general-purpose request proxy.
FULLTEXT_ALLOWED_HOSTS = frozenset({"fi-admin.bvsalud.org", "docs.bvsalud.org"})

MAX_RESULTS = 50
OVERFETCH_FACTOR = 3
MAX_PAGE_SIZE = 200
MAX_FULL_TEXT_CHARS = 50_000

BASE_FILTER = 'type:"non-conventional" AND la:"pt"'
BRISA_FILTER = 'db:"BRISA"'
VALID_COLLECTIONS = frozenset({"all", "brisa"})

BRAZIL_COUNTRY = "Brasil"

logger = logging.getLogger(__name__)


def _first(value: Any) -> str:
    """First element of a multi-valued Solr field, or the scalar, as a string."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value is not None else ""


def _as_list(value: Any) -> list[str]:
    """Every non-empty value of a Solr field as a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value if v]
    return [str(value)] if value else []


def _parse_issued(value: Any) -> tuple[str, str]:
    """Split a Solr ``da`` value ("202609") into ("2026", "2026-09").

    Returns ("", "") when the value is absent or not year-shaped.
    """
    raw = _first(value).strip()
    if len(raw) >= 6 and raw[:6].isdigit():
        return raw[:4], f"{raw[:4]}-{raw[4:6]}"
    if len(raw) >= 4 and raw[:4].isdigit():
        return raw[:4], raw[:4]
    return "", ""


def _parse_country(value: Any) -> str:
    """Read the ``^e`` subfield out of a ``pais_publicacao`` value.

    The field is encoded as "^iBrazil^eBrasil^pBrasil^fBrésil"; ``^e`` holds
    the Portuguese name. Returns "" when no ``^e`` subfield is present.
    """
    for raw in _as_list(value):
        for part in raw.split("^"):
            if part[:1] == "e" and part[1:].strip():
                return part[1:].strip()
    return ""


def _derive_fulltext_id(url: str) -> str:
    """Short slug of a fi-admin document view URL, or "" for any other URL."""
    match = FI_ADMIN_DOC_RE.match(url or "")
    return match.group(1) if match else ""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/medical/test_brazil_moh.py -v`
Expected: PASS, 13 tests

- [ ] **Step 6: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py src/scholar_mcp/medical/models.py tests/medical/test_brazil_moh.py
git commit -m "feat(brazil-moh): add BrazilGuideline model and BVS parse helpers"
```

---

### Task 3: Query builder and record parsing

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py`
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Consumes: `BASE_FILTER`, `BRISA_FILTER`, `_first`, `_as_list`, `_parse_issued`, `_parse_country`, `_derive_fulltext_id`, `BrazilGuideline` from Task 2.
- Produces: `_build_query(query: str, collection: str) -> str`, `_build_record(doc: dict[str, Any]) -> BrazilGuideline`, `_extract_docs(data: Any) -> list[dict[str, Any]]`, `_is_brazilian(record: BrazilGuideline) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/medical/test_brazil_moh.py`:

```python
from scholar_mcp.medical.brazil_moh import (
    _build_query,
    _build_record,
    _extract_docs,
    _is_brazilian,
)


def test_build_query_joins_user_tokens_with_and():
    built = _build_query("tratamento tuberculose", "all")
    assert built == 'type:"non-conventional" AND la:"pt" AND (tratamento AND tuberculose)'


def test_build_query_appends_brisa_filter():
    built = _build_query("dengue", "brisa")
    assert 'db:"BRISA"' in built
    assert built.startswith('type:"non-conventional" AND la:"pt"')


def test_build_query_omits_brisa_filter_for_all():
    assert 'db:"BRISA"' not in _build_query("dengue", "all")


def test_build_query_with_blank_query_is_filters_only():
    assert _build_query("   ", "all") == 'type:"non-conventional" AND la:"pt"'


def test_extract_docs_reads_nested_envelope():
    payload = {"diaServerResponse": [{"response": {"numFound": 2, "docs": [{"id": "a"}, {"id": "b"}]}}]}
    assert _extract_docs(payload) == [{"id": "a"}, {"id": "b"}]


def test_extract_docs_returns_empty_for_malformed_payload():
    assert _extract_docs({}) == []
    assert _extract_docs({"diaServerResponse": []}) == []
    assert _extract_docs(None) == []


def test_build_record_maps_solr_fields():
    doc = {
        "id": "biblio-1701387",
        "ti": ["Protocolo Clínico e Diretrizes Terapêuticas"],
        "ti_en": ["Clinical Protocol"],
        "ab": ["Resumo do protocolo."],
        "au": ["Brasil. Ministério da Saúde"],
        "da": "202609",
        "db": ["BRISA", "LILACS"],
        "mh": ["Tuberculose"],
        "la": ["pt"],
        "ur": ["https://fi-admin.bvsalud.org/document/view/cfpaj"],
        "pais_publicacao": ["^iBrazil^eBrasil^pBrasil^fBrésil"],
    }
    record = _build_record(doc)
    assert record.record_id == "biblio-1701387"
    assert record.title == "Protocolo Clínico e Diretrizes Terapêuticas"
    assert record.title_en == "Clinical Protocol"
    assert record.abstract == "Resumo do protocolo."
    assert record.authors == ["Brasil. Ministério da Saúde"]
    assert record.year == "2026"
    assert record.issued == "2026-09"
    assert record.collections == ["BRISA", "LILACS"]
    assert record.mesh_subjects == ["Tuberculose"]
    assert record.languages == ["pt"]
    assert record.country == "Brasil"
    assert record.document_url == "https://fi-admin.bvsalud.org/document/view/cfpaj"
    assert record.fulltext_id == "cfpaj"
    assert record.source == "brazil-moh"
    assert record.score is None


def test_build_record_offsite_url_has_no_fulltext_id():
    doc = {
        "id": "biblio-1",
        "ti": ["Artigo"],
        "ur": ["https://www.sciencedirect.com/science/article/pii/S123"],
    }
    record = _build_record(doc)
    assert record.document_url == "https://www.sciencedirect.com/science/article/pii/S123"
    assert record.fulltext_id == ""


def test_build_record_tolerates_missing_fields():
    record = _build_record({"id": "biblio-2"})
    assert record.record_id == "biblio-2"
    assert record.title == ""
    assert record.year == ""
    assert record.authors == []


def test_is_brazilian_keeps_brasil_and_drops_others():
    brazilian = _build_record({"id": "a", "pais_publicacao": ["^iBrazil^eBrasil"]})
    portuguese = _build_record({"id": "b", "pais_publicacao": ["^iPortugal^ePortugal"]})
    unknown = _build_record({"id": "c"})
    assert _is_brazilian(brazilian) is True
    assert _is_brazilian(portuguese) is False
    assert _is_brazilian(unknown) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/medical/test_brazil_moh.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_query'`

- [ ] **Step 3: Implement the builders**

Append to `src/scholar_mcp/medical/brazil_moh.py`:

```python
def _build_query(query: str, collection: str) -> str:
    """Compose every filter into ``q``.

    ``fq`` is silently ignored by this API, and the default operator is OR,
    so user tokens are explicitly ANDed inside their own group.
    """
    clauses = [BASE_FILTER]
    if collection == "brisa":
        clauses.append(BRISA_FILTER)
    tokens = [token for token in (query or "").split() if token]
    if tokens:
        clauses.append("(" + " AND ".join(tokens) + ")")
    return " AND ".join(clauses)


def _extract_docs(data: Any) -> list[dict[str, Any]]:
    """Pull the document list out of the nested BVS envelope."""
    if not isinstance(data, dict):
        return []
    responses = data.get("diaServerResponse") or []
    if not isinstance(responses, list) or not responses:
        return []
    first = responses[0]
    if not isinstance(first, dict):
        return []
    response = first.get("response") or {}
    docs = response.get("docs") or []
    return [doc for doc in docs if isinstance(doc, dict)]


def _build_record(doc: dict[str, Any]) -> BrazilGuideline:
    """Map one Solr document onto a BrazilGuideline."""
    document_url = _first(doc.get("ur"))
    year, issued = _parse_issued(doc.get("da"))
    return BrazilGuideline(
        title=_first(doc.get("ti")),
        title_en=_first(doc.get("ti_en")),
        record_id=_first(doc.get("id")),
        document_url=document_url,
        fulltext_id=_derive_fulltext_id(document_url),
        abstract=_first(doc.get("ab")),
        year=year,
        issued=issued,
        country=_parse_country(doc.get("pais_publicacao")),
        authors=_as_list(doc.get("au")),
        languages=_as_list(doc.get("la")),
        collections=_as_list(doc.get("db")),
        mesh_subjects=_as_list(doc.get("mh")),
    )


def _is_brazilian(record: BrazilGuideline) -> bool:
    """Client-side Brazil assertion.

    ``pais_publicacao`` cannot be filtered server-side, so ``la:"pt"``
    narrows the pool and this drops the Portuguese-language records
    published elsewhere.
    """
    return record.country == BRAZIL_COUNTRY
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/medical/test_brazil_moh.py -v`
Expected: PASS, 24 tests

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "feat(brazil-moh): add BVS query builder and record parsing"
```

---

### Task 4: search_guidelines

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py`
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Consumes: everything from Tasks 2 and 3.
- Produces: `class BrazilMoHEngine` with `__init__(self, http_client: AsyncHttpClient, cache: SQLiteCacheManager, settings: Settings)` and `async search_guidelines(self, query: str, limit: int = 10, collection: str = "all") -> tuple[list[BrazilGuideline], CacheMetadata]`; module function `_dedupe_by_id(docs) -> list[dict[str, Any]]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/medical/test_brazil_moh.py`. Note the shared helpers at the top — later tasks reuse them.

```python
from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import (
    BVS_SEARCH_URL,
    BrazilMoHEngine,
    _dedupe_by_id,
)
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager


async def _engine(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)
    return engine, cache, http_client


def _bvs_doc(record_id="biblio-1", title="Protocolo", country="^iBrazil^eBrasil", **extra):
    doc = {
        "id": record_id,
        "ti": [title],
        "la": ["pt"],
        "da": "202609",
        "pais_publicacao": [country],
        "ur": ["https://fi-admin.bvsalud.org/document/view/cfpaj"],
    }
    doc.update(extra)
    return doc


def _bvs_response(docs, num_found=None):
    return {
        "diaServerResponse": [
            {
                "responseHeader": {"status": 0},
                "response": {
                    "numFound": len(docs) if num_found is None else num_found,
                    "docs": docs,
                },
            }
        ]
    }


def test_dedupe_by_id_keeps_first_occurrence_and_order():
    docs = [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"id": "c"}]
    assert [d["id"] for d in _dedupe_by_id(docs)] == ["a", "b", "c"]


def test_dedupe_by_id_keeps_records_without_id():
    docs = [{"id": ""}, {"id": ""}]
    assert len(_dedupe_by_id(docs)) == 2


@respx.mock
async def test_search_deduplicates_and_trims_to_limit(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        docs = []
        for index in range(6):
            doc = _bvs_doc(record_id=f"biblio-{index}")
            docs.extend([doc, doc])  # every record duplicated
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response(docs))
        )
        records, meta = await engine.search_guidelines("dengue", limit=4)
        assert len(records) == 4
        assert [r.record_id for r in records] == [
            "biblio-0",
            "biblio-1",
            "biblio-2",
            "biblio-3",
        ]
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_requests_overfetched_count(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("dengue", limit=10)
        requested = str(route.calls[0].request.url)
        assert "count=30" in requested
        assert "output=json" in requested
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_caps_requested_count_at_page_size(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("dengue", limit=50)
        assert "count=150" in str(route.calls[0].request.url)
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_composes_filters_into_q_and_never_fq(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("tratamento tuberculose", limit=5, collection="brisa")
        requested = str(route.calls[0].request.url)
        assert "fq=" not in requested
        assert "non-conventional" in requested
        assert "BRISA" in requested
        assert "AND" in requested
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_sends_browser_headers(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("dengue", limit=5)
        headers = route.calls[0].request.headers
        assert "Mozilla/5.0" in headers["user-agent"]
        assert headers["accept-language"].startswith("pt-BR")
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_drops_non_brazilian_records(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        docs = [
            _bvs_doc(record_id="biblio-br", country="^iBrazil^eBrasil"),
            _bvs_doc(record_id="biblio-pt", country="^iPortugal^ePortugal"),
        ]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response(docs))
        )
        records, _ = await engine.search_guidelines("dengue", limit=10)
        assert [r.record_id for r in records] == ["biblio-br"]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_returns_short_list_as_success(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        records, meta = await engine.search_guidelines("dengue", limit=25)
        assert len(records) == 1
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


async def test_search_rejects_unknown_collection(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        records, meta = await engine.search_guidelines("dengue", collection="everything")
        assert records == []
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_clamps_limit_inside_engine(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        await engine.search_guidelines("dengue", limit=9999)
        assert "count=150" in str(route.calls[0].request.url)
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_network_failure_is_error_and_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            side_effect=httpx.ConnectError("reset by peer")
        )
        records, meta = await engine.search_guidelines("dengue", limit=5)
        assert records == []
        assert meta.error is True
        cached, cache_meta = await cache.get("brazil_moh_search:all:5:dengue")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_caches_success_and_serves_from_cache(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        route = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([_bvs_doc()]))
        )
        first, first_meta = await engine.search_guidelines("dengue", limit=5)
        second, second_meta = await engine.search_guidelines("dengue", limit=5)
        assert route.call_count == 1
        assert first_meta.cached is False
        assert second_meta.cached is True
        assert [r.record_id for r in second] == [r.record_id for r in first]
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/medical/test_brazil_moh.py -v`
Expected: FAIL with `ImportError: cannot import name 'BrazilMoHEngine'`

- [ ] **Step 3: Implement dedup and the engine's search**

Append to `src/scholar_mcp/medical/brazil_moh.py`:

```python
def _dedupe_by_id(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exact deduplication on the Solr ``id``, keeping first occurrence.

    BVS emits one row per indexing collection, inflating results by roughly
    2.1-2.3x. Exact-key dedup is deliberate: the fuzzy title matching in
    utils/deduplication.py would collapse genuinely distinct guidelines with
    similar titles.
    """
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for doc in docs:
        record_id = str(doc.get("id") or "")
        if record_id:
            if record_id in seen:
                continue
            seen.add(record_id)
        unique.append(doc)
    return unique


class BrazilMoHEngine:
    """Search and full-text retrieval for Brazilian MoH publications."""

    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    async def search_guidelines(
        self,
        query: str,
        limit: int = 10,
        collection: str = "all",
    ) -> tuple[list[BrazilGuideline], CacheMetadata]:
        if collection not in VALID_COLLECTIONS:
            logger.warning("unknown brazil_moh collection %r", collection)
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        clamped = min(max(1, limit), MAX_RESULTS)
        cache_key = f"brazil_moh_search:{collection}:{clamped}:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [BrazilGuideline.from_dict(item) for item in cached_data], meta

        count = min(clamped * OVERFETCH_FACTOR, MAX_PAGE_SIZE)
        resp = await self.http_client.get(
            BVS_SEARCH_URL,
            headers=BVS_HEADERS,
            params={
                "q": _build_query(query, collection),
                "output": "json",
                "count": count,
            },
        )
        if resp is None:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        try:
            data = resp.json()
        except ValueError:
            logger.warning("brazil_moh search returned non-JSON payload")
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        records = [_build_record(doc) for doc in _dedupe_by_id(_extract_docs(data))]
        records = [record for record in records if _is_brazilian(record)][:clamped]

        await self.cache.set(
            cache_key,
            [record.to_dict() for record in records],
            source="brazil_moh",
        )
        return records, CacheMetadata(cached=False, cache_age=0, error=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/medical/test_brazil_moh.py -v`
Expected: PASS, 37 tests

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "feat(brazil-moh): add search with overfetch, dedup and Brazil filter"
```

---

### Task 5: get_full_text

**Files:**
- Modify: `src/scholar_mcp/medical/brazil_moh.py`
- Test: `tests/medical/test_brazil_moh.py`

**Interfaces:**
- Consumes: everything from Tasks 2-4.
- Produces: `BrazilMoHEngine.get_full_text(self, record_id: str, max_chars: int | None = None) -> tuple[dict[str, Any], CacheMetadata]`; module function `_is_allowed_host(url: str) -> bool`.

The returned payload keys are `source`, `record_id`, `status`, `title`, `content_type`, `content`, `document_url`, `truncated`, and `error` on failure paths. `content_type` is one of `"pdf"`, `"abstract"`, `"none"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/medical/test_brazil_moh.py`:

```python
from scholar_mcp.medical.brazil_moh import _is_allowed_host

PDF_URL = "https://docs.bvsalud.org/biblioref/2026/08/1708363/protocolo.pdf"
FI_ADMIN_URL = "https://fi-admin.bvsalud.org/document/view/cfpaj"


def test_is_allowed_host_accepts_bvs_hosts_only():
    assert _is_allowed_host(FI_ADMIN_URL) is True
    assert _is_allowed_host(PDF_URL) is True
    assert _is_allowed_host("https://www.sciencedirect.com/x") is False
    assert _is_allowed_host("https://evil.example.com/fi-admin.bvsalud.org") is False
    assert _is_allowed_host("") is False


@respx.mock
async def test_get_full_text_extracts_pdf(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text",
        lambda _: "Texto integral do protocolo.",
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1", ab=["Resumo."])])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF-1.5 fake", headers={"content-type": "application/pdf"}
            )
        )
        payload, meta = await engine.get_full_text("biblio-1")
        assert payload["status"] == "success"
        assert payload["content_type"] == "pdf"
        assert payload["content"] == "Texto integral do protocolo."
        assert payload["source"] == "brazil-moh"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_offsite_url_degrades_without_fetching(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-1", ab=["Resumo apenas."])
        doc["ur"] = ["https://www.sciencedirect.com/science/article/pii/S123"]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        offsite = respx.get(url__startswith="https://www.sciencedirect.com").mock(
            return_value=httpx.Response(200, content=b"should never be requested")
        )
        payload, _ = await engine.get_full_text("biblio-1")
        assert offsite.called is False
        assert payload["content_type"] == "abstract"
        assert payload["content"] == "Resumo apenas."
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_html_response_degrades_to_abstract(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1", ab=["Resumo."])])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, text="<html>Estamos em manutenção</html>",
                headers={"content-type": "text/html"},
            )
        )
        payload, _ = await engine.get_full_text("biblio-1")
        assert payload["content_type"] == "abstract"
        assert payload["content"] == "Resumo."
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_no_pdf_and_no_abstract_is_not_found(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        doc = _bvs_doc(record_id="biblio-1")
        doc["ur"] = ["https://www.sciencedirect.com/x"]
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([doc]))
        )
        payload, _ = await engine.get_full_text("biblio-1")
        assert payload["status"] == "not_found"
        assert payload["content_type"] == "none"
        assert payload["content"] == ""
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_unknown_record_is_not_found(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(200, json=_bvs_response([]))
        )
        payload, meta = await engine.get_full_text("biblio-missing")
        assert payload["status"] == "not_found"
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


async def test_get_full_text_requires_record_id(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        payload, meta = await engine.get_full_text("")
        assert payload["status"] == "error"
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_pdf_failure_degrades_and_is_not_cached(tmp_path: Path):
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1", ab=["Resumo."])])
            )
        )
        respx.get(FI_ADMIN_URL).mock(side_effect=httpx.ConnectError("blocked"))
        payload, meta = await engine.get_full_text("biblio-1")
        assert payload["content_type"] == "abstract"
        assert meta.error is True
        _, cache_meta = await cache.get("brazil_moh_fulltext:biblio-1")
        assert cache_meta.cached is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_caches_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: "Conteúdo."
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        search = respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        await engine.get_full_text("biblio-1")
        payload, meta = await engine.get_full_text("biblio-1")
        assert search.call_count == 1
        assert meta.cached is True
        assert payload["content"] == "Conteúdo."
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_full_text_truncates_served_not_cached(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "scholar_mcp.medical.brazil_moh.pdf_bytes_to_text", lambda _: "x" * 500
    )
    engine, cache, http_client = await _engine(tmp_path)
    try:
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200, json=_bvs_response([_bvs_doc(record_id="biblio-1")])
            )
        )
        respx.get(FI_ADMIN_URL).mock(
            return_value=httpx.Response(
                200, content=b"%PDF", headers={"content-type": "application/pdf"}
            )
        )
        payload, _ = await engine.get_full_text("biblio-1", max_chars=100)
        assert len(payload["content"]) <= 100
        assert payload["truncated"] is True
        cached, _ = await cache.get("brazil_moh_fulltext:biblio-1")
        assert len(cached["content"]) == 500
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/medical/test_brazil_moh.py -v`
Expected: FAIL with `ImportError: cannot import name '_is_allowed_host'`

- [ ] **Step 3: Implement the allowlist and full-text retrieval**

Append the module function to `src/scholar_mcp/medical/brazil_moh.py`, before the class definition or after it — placement does not matter, but keep it beside the other module helpers:

```python
def _is_allowed_host(url: str) -> bool:
    """True only for the BVS hosts this module is permitted to fetch."""
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower()
    except ValueError:
        return False
    return host in FULLTEXT_ALLOWED_HOSTS
```

Add these methods to `BrazilMoHEngine`:

```python
    async def _lookup_record(self, record_id: str) -> tuple[BrazilGuideline | None, bool]:
        """Resolve one record by its Solr id. Returns (record, errored)."""
        resp = await self.http_client.get(
            BVS_SEARCH_URL,
            headers=BVS_HEADERS,
            params={"q": f'id:"{record_id}"', "output": "json", "count": 5},
        )
        if resp is None:
            return None, True
        try:
            data = resp.json()
        except ValueError:
            return None, True
        docs = _dedupe_by_id(_extract_docs(data))
        if not docs:
            return None, False
        return _build_record(docs[0]), False

    async def _extract_pdf_text(self, document_url: str) -> tuple[str, bool]:
        """Fetch and extract the document PDF. Returns (text, errored).

        Gates on content-type explicitly. ``get_bytes`` is not used here:
        its HTML guard only catches Cloudflare-style challenge pages, so a
        plain "Estamos em manutenção" WAF page would reach the PDF parser.
        """
        if not _is_allowed_host(document_url):
            return "", False
        resp = await self.http_client.get(document_url, headers=BVS_HEADERS)
        if resp is None:
            return "", True
        content_type = resp.headers.get("content-type", "").lower()
        if "application/pdf" not in content_type:
            logger.info(
                "brazil_moh full text is not a PDF (content-type=%r)", content_type
            )
            return "", True
        try:
            return pdf_bytes_to_text(resp.content), False
        except Exception as exc:
            logger.warning("brazil_moh PDF extraction failed: %s", exc)
            return "", True

    async def get_full_text(
        self,
        record_id: str,
        max_chars: int | None = None,
    ) -> tuple[dict[str, Any], CacheMetadata]:
        normalized = (record_id or "").strip()
        base = {
            "source": "brazil-moh",
            "record_id": normalized,
            "document_url": "",
            "truncated": False,
        }
        if not normalized:
            return (
                {**base, "status": "error", "error": "record_id is required",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=True),
            )

        cache_key = f"brazil_moh_fulltext:{normalized}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return self._serve_full_text(cached_data, max_chars), meta

        record, errored = await self._lookup_record(normalized)
        if errored:
            return (
                {**base, "status": "error", "error": "bvs request failed",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=True),
            )
        if record is None:
            return (
                {**base, "status": "not_found", "error": "no record for id",
                 "title": "", "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=False),
            )

        base["document_url"] = record.document_url
        pdf_text, errored = await self._extract_pdf_text(record.document_url)

        if pdf_text:
            result = {"content_type": "pdf", "content": pdf_text}
        elif record.abstract:
            result = {"content_type": "abstract", "content": record.abstract}
        else:
            return (
                {**base, "status": "not_found",
                 "error": "no full text or abstract available",
                 "title": record.title, "content_type": "none", "content": ""},
                CacheMetadata(cached=False, cache_age=0, error=errored),
            )

        payload = {**base, "status": "success", "title": record.title, **result}
        # An errored payload is never cached: a transient block must not
        # poison a 30-day TTL.
        if not errored:
            await self.cache.set(cache_key, payload, source="brazil_moh")
        return (
            self._serve_full_text(payload, max_chars),
            CacheMetadata(cached=False, cache_age=0, error=errored),
        )

    @staticmethod
    def _serve_full_text(payload: dict[str, Any], max_chars: int | None) -> dict[str, Any]:
        limit = MAX_FULL_TEXT_CHARS if max_chars is None else max(1, max_chars)
        content, truncated = truncate_content(payload.get("content", ""), limit)
        return {**payload, "content": content, "truncated": truncated}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/medical/test_brazil_moh.py -v`
Expected: PASS, 47 tests

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/medical/brazil_moh.py tests/medical/test_brazil_moh.py
git commit -m "feat(brazil-moh): add allowlisted full-text retrieval with degradation"
```

---

### Task 6: Response formatter

**Files:**
- Modify: `src/scholar_mcp/medical/formatters.py`
- Test: `tests/medical/test_models_formatters.py`

**Interfaces:**
- Consumes: `BrazilGuideline` from Task 2.
- Produces: `format_brazil_moh_guidelines(guidelines: list[BrazilGuideline], query: str, meta: CacheMetadata) -> dict[str, Any]` returning `{"data": [...], "markdown": str}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/medical/test_models_formatters.py`:

```python
def test_format_brazil_moh_guidelines_renders_fields():
    from scholar_mcp.medical.formatters import format_brazil_moh_guidelines
    from scholar_mcp.medical.models import BrazilGuideline
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    guideline = BrazilGuideline(
        title="Protocolo Clínico",
        record_id="biblio-1",
        document_url="https://fi-admin.bvsalud.org/document/view/cfpaj",
        fulltext_id="cfpaj",
        year="2026",
        authors=["Brasil. Ministério da Saúde"],
        collections=["BRISA"],
        abstract="Resumo do protocolo.",
    )
    result = format_brazil_moh_guidelines(
        [guideline], "tuberculose", CacheMetadata(cached=False, cache_age=0)
    )
    assert result["data"][0]["record_id"] == "biblio-1"
    markdown = result["markdown"]
    assert "Protocolo Clínico" in markdown
    assert "biblio-1" in markdown
    assert "2026" in markdown
    assert "BRISA" in markdown
    assert "Resumo do protocolo." in markdown
    assert "[Fresh response]" in markdown


def test_format_brazil_moh_guidelines_empty_success_says_no_results():
    from scholar_mcp.medical.formatters import format_brazil_moh_guidelines
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    result = format_brazil_moh_guidelines([], "x", CacheMetadata(cached=False, cache_age=0))
    assert "No Brazilian Ministry of Health documents found" in result["markdown"]


def test_format_brazil_moh_guidelines_empty_error_does_not_claim_absence():
    from scholar_mcp.medical.formatters import (
        FETCH_FAILED_LINE,
        format_brazil_moh_guidelines,
    )
    from scholar_mcp.utils.sqlite_cache import CacheMetadata

    result = format_brazil_moh_guidelines(
        [], "x", CacheMetadata(cached=False, cache_age=0, error=True)
    )
    assert FETCH_FAILED_LINE in result["markdown"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/medical/test_models_formatters.py -v -k brazil`
Expected: FAIL with `ImportError: cannot import name 'format_brazil_moh_guidelines'`

- [ ] **Step 3: Implement the formatter**

In `src/scholar_mcp/medical/formatters.py`, add `BrazilGuideline` to the existing import from `scholar_mcp.medical.models` (keep the list alphabetical — it goes first, before `ClinicalGuideline`). Then append at the end of the file:

```python
def format_brazil_moh_guidelines(
    guidelines: list[BrazilGuideline],
    query: str,
    meta: CacheMetadata,
) -> dict[str, Any]:
    lines = [f"## Brazilian Ministry of Health Documents: {query}", ""]
    if not guidelines:
        lines.append(
            _empty_state(
                f"No Brazilian Ministry of Health documents found for: {query}", meta
            )
        )
    else:
        for g in guidelines:
            lines.append(f"### {g.title}")
            lines.append(f"- **Record ID:** {g.record_id}")
            lines.append(f"- **Source:** {g.source}")
            if g.year:
                lines.append(f"- **Year:** {g.year}")
            if g.authors:
                lines.append(f"- **Authors:** {', '.join(g.authors)}")
            if g.collections:
                lines.append(f"- **Collections:** {', '.join(g.collections)}")
            if g.mesh_subjects:
                lines.append(f"- **DeCS/MeSH:** {', '.join(g.mesh_subjects)}")
            if g.document_url:
                lines.append(f"- **URL:** {g.document_url}")
            if not g.fulltext_id:
                lines.append("- **Full text:** not retrievable; document is hosted off-site")
            if g.abstract:
                lines.append("")
                lines.append(g.abstract)
            lines.append("")

    markdown = append_cache_info("\n".join(lines).strip(), meta)
    return {"data": [g.to_dict() for g in guidelines], "markdown": markdown}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/medical/test_models_formatters.py -v -k brazil`
Expected: PASS, 3 tests

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/medical/formatters.py tests/medical/test_models_formatters.py
git commit -m "feat(brazil-moh): add guideline response formatter"
```

---

### Task 7: MCP tool registration and documentation

**Files:**
- Modify: `src/scholar_mcp/server.py:47` (engine construction), `src/scholar_mcp/server.py:570` (after `get_who_iris_full_text`)
- Modify: `tests/test_server_medical.py:19-20` (`MEDICAL_TOOLS` set)
- Modify: `AGENTS.md`, `README.md`, `CHANGELOG.md`
- Test: `tests/test_server_medical.py`

**Interfaces:**
- Consumes: `BrazilMoHEngine` (Task 4-5), `format_brazil_moh_guidelines` (Task 6), `MAX_RESULTS` (Task 2).
- Produces: MCP tools `search_brazil_moh_guidelines` and `get_brazil_moh_full_text`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_server_medical.py`, add both names to the `MEDICAL_TOOLS` set, after `"get_who_iris_full_text",`:

```python
    "search_brazil_moh_guidelines",
    "get_brazil_moh_full_text",
```

Then append these tests to the same file:

```python
async def test_search_brazil_moh_guidelines_tool(monkeypatch):
    from scholar_mcp.medical.models import BrazilGuideline

    mock = AsyncMock(
        return_value=(
            [BrazilGuideline(title="Protocolo", record_id="biblio-1")],
            CacheMetadata(cached=False, cache_age=0),
        )
    )
    monkeypatch.setattr(srv.brazil_moh_engine, "search_guidelines", mock)
    result = await srv.search_brazil_moh_guidelines("tuberculose", limit=5)
    assert result["data"][0]["record_id"] == "biblio-1"
    assert mock.await_args.kwargs["limit"] == 5


async def test_search_brazil_moh_guidelines_clamps_limit(monkeypatch):
    mock = AsyncMock(return_value=([], CacheMetadata(cached=False, cache_age=0)))
    monkeypatch.setattr(srv.brazil_moh_engine, "search_guidelines", mock)
    await srv.search_brazil_moh_guidelines("x", limit=9999)
    assert mock.await_args.kwargs["limit"] == 50


async def test_search_brazil_moh_guidelines_rejects_unknown_collection(monkeypatch):
    mock = AsyncMock(return_value=([], CacheMetadata(cached=False, cache_age=0)))
    monkeypatch.setattr(srv.brazil_moh_engine, "search_guidelines", mock)
    result = await srv.search_brazil_moh_guidelines("x", collection="everything")
    assert result["status"] == "error"
    assert result["source"] == "brazil-moh"
    assert mock.await_count == 0


async def test_search_brazil_moh_guidelines_returns_error_envelope(monkeypatch):
    mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(srv.brazil_moh_engine, "search_guidelines", mock)
    result = await srv.search_brazil_moh_guidelines("x")
    assert result["status"] == "error"
    assert result["source"] == "brazil-moh"


async def test_get_brazil_moh_full_text_tool(monkeypatch):
    mock = AsyncMock(
        return_value=(
            {"status": "success", "content": "texto", "content_type": "pdf"},
            CacheMetadata(cached=True, cache_age=42),
        )
    )
    monkeypatch.setattr(srv.brazil_moh_engine, "get_full_text", mock)
    result = await srv.get_brazil_moh_full_text("biblio-1")
    assert result["content"] == "texto"
    assert result["cache"] == {"cached": True, "cache_age": 42}


async def test_get_brazil_moh_full_text_returns_error_envelope(monkeypatch):
    mock = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(srv.brazil_moh_engine, "get_full_text", mock)
    result = await srv.get_brazil_moh_full_text("biblio-1")
    assert result["status"] == "error"
    assert result["content"] == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_server_medical.py -v`
Expected: FAIL with `AttributeError: module 'scholar_mcp.server' has no attribute 'brazil_moh_engine'`

- [ ] **Step 3: Register the engine and tools**

In `src/scholar_mcp/server.py`, add to the imports alongside the other medical imports:

```python
from scholar_mcp.medical.brazil_moh import MAX_RESULTS as BRAZIL_MAX_RESULTS
from scholar_mcp.medical.brazil_moh import VALID_COLLECTIONS as BRAZIL_VALID_COLLECTIONS
from scholar_mcp.medical.brazil_moh import BrazilMoHEngine
from scholar_mcp.medical.formatters import format_brazil_moh_guidelines
```

Add the engine construction directly after the `who_iris_engine = ...` line:

```python
brazil_moh_engine = BrazilMoHEngine(http_client=http_client, cache=medical_cache, settings=settings)
```

Add both tools inside the `if settings.enable_medical_tools:` block, directly after `get_who_iris_full_text`:

```python
    @mcp.tool()
    async def search_brazil_moh_guidelines(
        query: str,
        limit: int = 10,
        collection: str = "all",
    ) -> dict[str, Any]:
        """Search Brazilian Ministry of Health technical publications (BVS/iAHx).

        Covers PCDT (Protocolos Clínicos e Diretrizes Terapêuticas), CONITEC
        health-technology assessments, cadernos de atenção básica, manuais
        técnicos, and normas de vigilância. Results are in Portuguese.

        Args:
            query: Free-text search terms. Portuguese terms match best;
                every token is required (they are ANDed).
            limit: Maximum number of results to return (max 50).
            collection: 'all' (default, all Brazilian grey literature) or
                'brisa' (health-technology assessments and PCDT only).
        """
        clamped = min(max(1, limit), BRAZIL_MAX_RESULTS)
        if collection not in BRAZIL_VALID_COLLECTIONS:
            return {
                "status": "error",
                "error": f"unknown collection {collection!r}; expected 'all' or 'brisa'",
                "source": "brazil-moh",
            }
        try:
            guidelines, meta = await brazil_moh_engine.search_guidelines(
                query, limit=clamped, collection=collection
            )
            return format_brazil_moh_guidelines(guidelines, query, meta)
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "brazil-moh"}

    @mcp.tool()
    async def get_brazil_moh_full_text(
        record_id: str,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Retrieve full text of a Brazilian Ministry of Health document.

        Downloads the document PDF from the BVS repository and extracts its
        text. Falls back to the record abstract when the document is hosted
        off-site or the PDF cannot be retrieved.

        Args:
            record_id: The `record_id` field returned by
                search_brazil_moh_guidelines (e.g. 'biblio-1701387').
            max_chars: Maximum character limit for the returned text
                (defaults to 50,000).
        """
        try:
            payload, meta = await brazil_moh_engine.get_full_text(
                record_id, max_chars=max_chars
            )
            payload["cache"] = {"cached": meta.cached, "cache_age": meta.cache_age}
            return payload
        except Exception as ex:
            return {"status": "error", "error": str(ex), "source": "brazil-moh", "content": ""}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_server_medical.py -v`
Expected: PASS

- [ ] **Step 5: Verify the server imports cleanly**

Run: `python -c "from scholar_mcp.server import main; print('Import OK')"`
Expected: `Import OK`

- [ ] **Step 6: Run the full suite**

Run: `pytest -v`
Expected: PASS, no regressions

- [ ] **Step 7: Update documentation**

In `AGENTS.md`, add this line to the architecture tree inside the `medical/` section, after the `who_iris.py` entry:

```
│   ├── brazil_moh.py     # Brazilian MoH publications via BVS/iAHx search + PDF full text
```

In `README.md`, add both tools to the medical tool table:

| Tool | Description |
|---|---|
| `search_brazil_moh_guidelines` | Search Brazilian Ministry of Health technical publications (PCDT, CONITEC, manuais, cadernos) via BVS. |
| `get_brazil_moh_full_text` | Retrieve full text of a Brazilian MoH document, degrading to its abstract. |

Also document the new environment variable alongside the other `CACHE_TTL_*` entries:

```
CACHE_TTL_BRAZIL_MOH   # Brazilian MoH document cache TTL in seconds (default 2592000)
```

In `CHANGELOG.md`, add under the unreleased heading:

```markdown
### Added
- `search_brazil_moh_guidelines` and `get_brazil_moh_full_text` MCP tools for
  Brazilian Ministry of Health technical publications, sourced from BVS/iAHx
  with host-allowlisted PDF full-text retrieval.
```

- [ ] **Step 8: Commit**

```bash
git add src/scholar_mcp/server.py tests/test_server_medical.py AGENTS.md README.md CHANGELOG.md
git commit -m "feat(brazil-moh): register MCP tools and document them"
```

---

## Manual Verification

After Task 7, confirm the tools work against the live API. This is not a test — it makes real network calls and is expected to be run once, by hand.

```bash
python - <<'EOF'
import asyncio
from scholar_mcp.config import Settings
from scholar_mcp.medical.brazil_moh import BrazilMoHEngine
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

async def main():
    settings = Settings.load()
    http = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=settings.cache_db_path, settings=settings)
    engine = BrazilMoHEngine(http_client=http, cache=cache, settings=settings)
    try:
        records, meta = await engine.search_guidelines("tuberculose", limit=5)
        print("results:", len(records), "error:", meta.error)
        for r in records:
            print(" -", r.record_id, "|", r.year, "|", r.title[:60])
        if records:
            payload, _ = await engine.get_full_text(records[0].record_id, max_chars=500)
            print("full text:", payload["content_type"], len(payload["content"]), "chars")
    finally:
        await cache.close()
        await http.aclose()

asyncio.run(main())
EOF
```

Expected: a non-zero result count with `error: False`, Portuguese titles, and a `content_type` of `pdf` or `abstract`.

If results come back empty with `error: False`, the most likely cause is that the BVS filter syntax has drifted; check `_build_query` output against a browser request before changing anything else.
