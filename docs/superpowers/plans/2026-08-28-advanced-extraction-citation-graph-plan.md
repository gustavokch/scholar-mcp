# Advanced Extraction & Citation Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `scholar-mcp` with LaTeX/MathML math extraction in JATS full-text, plus three new discovery tools: `get_references`, `get_citations`, and `get_related_papers`.

**Architecture:** Extend `scholar_mcp.models` with reference/citation dataclasses -> Upgrade `scholar_mcp.parsers.jats` to render `<tex-math>` to `$math$` / `$$math$$` without breaking tables -> Implement provider methods in `EuropePMCProvider`, `CrossRefProvider`, and `PubMedProvider` -> Wire resolution in `WaterfallResolver` -> Register new tools on FastMCP `mcp` server.

**Tech Stack:** Python >= 3.10, `fastmcp>=3.0.0`, `httpx>=0.27.0`, `beautifulsoup4>=4.12.0`, `lxml>=5.0.0`, `pytest>=8.0.0`, `pytest-asyncio>=0.23.0`, `respx>=0.21.0`.

**Spec:** `docs/superpowers/specs/2026-08-28-advanced-extraction-citation-graph-design.md`

## Global Constraints

- All I/O must remain async using the shared `AsyncHttpClient`.
- Mock all network calls in tests using `respx` (no live network).
- Zero unhandled network exceptions; tool layer returns structured error payloads.
- Boundaries: clamp `get_references` limit to [1, 100], `get_citations` limit to [1, 100], and `get_related_papers` limit to [1, 25].

---

### Task 1: Reference, Citation, and Related Paper Data Models

**Files:**
- Modify: `src/scholar_mcp/models.py`
- Test: `tests/test_citation_models.py`

**Interfaces:**
- Produces: `ReferenceItem`, `CitationItem`, `RelatedPaper`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_citation_models.py
from scholar_mcp.models import CitationItem, ReferenceItem, RelatedPaper


def test_reference_item_defaults_and_dict():
    ref = ReferenceItem(
        id="ref1",
        title="Attention Is All You Need",
        authors=["Vaswani A", "Shazeer N"],
        year="2017",
        venue="NeurIPS",
        doi="10.48550/arXiv.1706.03762",
    )
    d = ref.to_dict()
    assert d["id"] == "ref1"
    assert d["title"] == "Attention Is All You Need"
    assert d["doi"] == "10.48550/arXiv.1706.03762"
    assert d["pmid"] is None


def test_citation_item_defaults_and_dict():
    cit = CitationItem(
        title="BERT: Pre-training of Deep Bidirectional Transformers",
        authors=["Devlin J"],
        year="2018",
        doi="10.18653/v1/N19-1423",
        citation_count=50000,
    )
    d = cit.to_dict()
    assert d["citation_count"] == 50000
    assert d["title"] == "BERT: Pre-training of Deep Bidirectional Transformers"


def test_related_paper_defaults_and_dict():
    rel = RelatedPaper(
        title="RoBERTa: A Robustly Optimized BERT Approach",
        authors=["Liu Y"],
        year="2019",
        score=98.5,
    )
    d = rel.to_dict()
    assert d["score"] == 98.5
    assert d["pmid"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_citation_models.py -v`  
Expected: FAIL (ImportError: cannot import name 'ReferenceItem' from 'scholar_mcp.models')

- [ ] **Step 3: Implement data models in `src/scholar_mcp/models.py`**

Add `ReferenceItem`, `CitationItem`, and `RelatedPaper` dataclasses.

```python
@dataclass
class ReferenceItem:
    id: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CitationItem:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    citation_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RelatedPaper:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    venue: str = ""
    doi: str | None = None
    pmid: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_citation_models.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/models.py tests/test_citation_models.py
git commit -m "feat: add reference, citation, and related paper models"
```

---

### Task 2: JATS XML Math & Formula Markdown Rendering

**Files:**
- Modify: `src/scholar_mcp/parsers/jats.py`
- Modify: `tests/test_jats_parser.py`

**Interfaces:**
- Produces: Enhanced `jats_to_markdown` converting `<inline-formula>` and `<disp-formula>` with `<tex-math>` to `$math$` and `$$\nmath\n$$`

- [ ] **Step 1: Write the failing test**

```python
# In tests/test_jats_parser.py
def test_jats_formula_rendering():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<article>
  <front><article-meta><title-group><article-title>Math Paper</article-title></title-group></article-meta></front>
  <body>
    <sec>
      <title>Methods</title>
      <p>Here is inline equation <inline-formula><tex-math>E = mc^2</tex-math></inline-formula> and display:
        <disp-formula>
          <tex-math>\\int_{-\\infty}^{\\infty} e^{-x^2} dx = \\sqrt{\\pi}</tex-math>
        </disp-formula>
      </p>
    </sec>
  </body>
</article>"""
    md = jats_to_markdown(xml)
    assert "$E = mc^2$" in md
    assert "$$\\n\\int_{-\\infty}^{\\infty} e^{-x^2} dx = \\sqrt{\\pi}\\n$$" in md or "$$\n\\int" in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_jats_parser.py -k test_jats_formula_rendering -v`  
Expected: FAIL

- [ ] **Step 3: Update `src/scholar_mcp/parsers/jats.py`**

- Remove `"tex-math"`, `"mml:math"`, `"math"` from `DECOMPOSE_TAGS`.
- Remove the blanket decomposition of `mml:` tags.
- In `_render_node`:
  - Handle `inline-formula`: extract `<tex-math>` content, strip and return `$content$`. If not found, return text.
  - Handle `disp-formula`: extract `<tex-math>` content, strip and return `\n\n$$\ncontent\n$$\n\n`.
  - Handle `mml:math` / `math`: if `alttext` attribute present, return `$alttext$`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_jats_parser.py -v`  
Expected: PASS (All JATS tests pass)

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/parsers/jats.py tests/test_jats_parser.py
git commit -m "feat(parsers): preserve LaTeX formulas as Markdown math in JATS parser"
```

---

### Task 3: References Discovery Provider Methods

**Files:**
- Modify: `src/scholar_mcp/providers/europe_pmc.py`
- Modify: `src/scholar_mcp/providers/crossref.py`
- Test: `tests/test_citations_references.py`

**Interfaces:**
- Produces:
  - `EuropePMCProvider.fetch_references(ids: IdentifierMap, limit: int = 50) -> list[ReferenceItem]`
  - `CrossRefProvider.fetch_references(doi: str, limit: int = 50) -> list[ReferenceItem]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_citations_references.py
import httpx
import pytest
import respx

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap
from scholar_mcp.providers.crossref import CrossRefProvider
from scholar_mcp.providers.europe_pmc import EuropePMCProvider
from scholar_mcp.utils.http import AsyncHttpClient

EPMC_REF = "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/12345/references"
CROSSREF_WORKS = "https://api.crossref.org/works/10.1038/nature123"


@pytest.fixture
async def client():
    c = AsyncHttpClient(settings=Settings(), max_retries=1, backoff_base=0.01)
    yield c
    await c.aclose()


@respx.mock
async def test_europe_pmc_fetch_references(client):
    respx.get(url__startswith=EPMC_REF).mock(
        return_value=httpx.Response(
            200,
            json={
                "referenceList": {
                    "reference": [
                        {
                            "id": "1",
                            "title": "Foundational Paper",
                            "authorString": "Smith J, Doe A",
                            "pubYear": "2020",
                            "journalTitle": "Nature",
                            "doi": "10.1038/ref1",
                            "pmid": "30000001",
                        }
                    ]
                }
            },
        )
    )
    provider = EuropePMCProvider(client)
    refs = await provider.fetch_references(IdentifierMap(pmid="12345"), limit=10)
    assert len(refs) == 1
    assert refs[0].title == "Foundational Paper"
    assert refs[0].doi == "10.1038/ref1"
    assert refs[0].pmid == "30000001"
    assert refs[0].year == "2020"


@respx.mock
async def test_crossref_fetch_references(client):
    respx.get(CROSSREF_WORKS).mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "reference": [
                        {
                            "key": "ref1",
                            "article-title": "CrossRef Cited Article",
                            "author": "Lovelace A",
                            "year": "2019",
                            "journal-title": "Science",
                            "DOI": "10.1126/science.ref1",
                        }
                    ]
                }
            },
        )
    )
    provider = CrossRefProvider(client)
    refs = await provider.fetch_references("10.1038/nature123", limit=10)
    assert len(refs) == 1
    assert refs[0].title == "CrossRef Cited Article"
    assert refs[0].doi == "10.1126/science.ref1"
    assert refs[0].authors == ["Lovelace A"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_citations_references.py -v`  
Expected: FAIL (AttributeError: 'EuropePMCProvider' object has no attribute 'fetch_references')

- [ ] **Step 3: Implement reference fetching in providers**

Implement `fetch_references` in `EuropePMCProvider` and `CrossRefProvider`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_citations_references.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/providers/europe_pmc.py src/scholar_mcp/providers/crossref.py tests/test_citations_references.py
git commit -m "feat(providers): implement bibliography reference retrieval in Europe PMC and CrossRef"
```

---

### Task 4: Citations & Related Literature Provider Methods

**Files:**
- Modify: `src/scholar_mcp/providers/europe_pmc.py`
- Modify: `src/scholar_mcp/providers/pubmed.py`
- Test: `tests/test_citations_references.py`

**Interfaces:**
- Produces:
  - `EuropePMCProvider.fetch_citations(ids: IdentifierMap, limit: int = 50) -> list[CitationItem]`
  - `PubMedProvider.fetch_related_papers(pmid: str, limit: int = 10) -> list[RelatedPaper]`

- [ ] **Step 1: Write the failing tests**

```python
# In tests/test_citations_references.py
EPMC_CIT = "https://www.ebi.ac.uk/europepmc/webservices/rest/MED/12345/citations"
ELINK_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"


@respx.mock
async def test_europe_pmc_fetch_citations(client):
    respx.get(url__startswith=EPMC_CIT).mock(
        return_value=httpx.Response(
            200,
            json={
                "citationList": {
                    "citation": [
                        {
                            "title": "Citing Paper Alpha",
                            "authorString": "Doe J",
                            "pubYear": "2022",
                            "journalTitle": "Cell",
                            "doi": "10.1016/j.cell.2022.01",
                            "pmid": "35000001",
                            "citedByCount": 12,
                        }
                    ]
                }
            },
        )
    )
    provider = EuropePMCProvider(client)
    cits = await provider.fetch_citations(IdentifierMap(pmid="12345"), limit=10)
    assert len(cits) == 1
    assert cits[0].title == "Citing Paper Alpha"
    assert cits[0].citation_count == 12
    assert cits[0].doi == "10.1016/j.cell.2022.01"


@respx.mock
async def test_pubmed_fetch_related_papers(client):
    respx.get(url__startswith=ELINK_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "linksets": [
                    {
                        "linksetdbs": [
                            {
                                "linkname": "pubmed_pubmed",
                                "links": [
                                    {"id": "31000001", "score": "95000000"},
                                    {"id": "31000002", "score": "82000000"},
                                ],
                            }
                        ]
                    }
                ]
            },
        )
    )
    respx.get(url__startswith=ESUMMARY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "uids": ["31000001", "31000002"],
                    "31000001": {
                        "title": "Related Paper 1",
                        "authors": [{"name": "Author One"}],
                        "pubdate": "2021",
                        "fulljournalname": "Genetics",
                        "elocationid": "doi: 10.1000/1",
                    },
                    "31000002": {
                        "title": "Related Paper 2",
                        "authors": [{"name": "Author Two"}],
                        "pubdate": "2021",
                        "fulljournalname": "Genomics",
                        "elocationid": "doi: 10.1000/2",
                    },
                }
            },
        )
    )
    provider = PubMedProvider(client, Settings())
    related = await provider.fetch_related_papers("12345", limit=2)
    assert len(related) == 2
    assert related[0].title == "Related Paper 1"
    assert related[0].score == 95.0
    assert related[0].doi == "10.1000/1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_citations_references.py -k "test_europe_pmc_fetch_citations or test_pubmed_fetch_related_papers" -v`  
Expected: FAIL

- [ ] **Step 3: Implement methods in providers**

- Add `fetch_citations` in `EuropePMCProvider`.
- Add `fetch_related_papers` in `PubMedProvider` via `elink.fcgi` and batch metadata via `esummary.fcgi`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_citations_references.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/providers/europe_pmc.py src/scholar_mcp/providers/pubmed.py tests/test_citations_references.py
git commit -m "feat(providers): implement citation lookup and related literature discovery via E-Link"
```

---

### Task 5: Resolver Integration & Server Tools Registration

**Files:**
- Modify: `src/scholar_mcp/resolver.py`
- Modify: `src/scholar_mcp/server.py`
- Modify: `tests/test_server_tools.py`
- Modify: `README.md`

**Interfaces:**
- Produces:
  - `WaterfallResolver.get_references(identifier, limit=50) -> list[ReferenceItem]`
  - `WaterfallResolver.get_citations(identifier, limit=50) -> list[CitationItem]`
  - `WaterfallResolver.get_related_papers(identifier, limit=10) -> list[RelatedPaper]`
  - Tools on MCP server: `get_references`, `get_citations`, `get_related_papers`

- [ ] **Step 1: Write the failing server tool tests**

```python
# In tests/test_server_tools.py
from scholar_mcp.models import CitationItem, ReferenceItem, RelatedPaper


async def test_get_references_tool(resolver):
    resolver.get_references.return_value = [
        ReferenceItem(id="1", title="Ref Paper", doi="10.1/ref")
    ]
    res = await srv.get_references("32000000", limit=10)
    assert len(res) == 1
    assert res[0]["title"] == "Ref Paper"
    assert res[0]["doi"] == "10.1/ref"


async def test_get_citations_tool(resolver):
    resolver.get_citations.return_value = [
        CitationItem(title="Citing Paper", doi="10.1/cite", citation_count=5)
    ]
    res = await srv.get_citations("32000000", limit=10)
    assert len(res) == 1
    assert res[0]["title"] == "Citing Paper"
    assert res[0]["citation_count"] == 5


async def test_get_related_papers_tool(resolver):
    resolver.get_related_papers.return_value = [
        RelatedPaper(title="Related Paper", score=90.0, pmid="33000000")
    ]
    res = await srv.get_related_papers("32000000", limit=5)
    assert len(res) == 1
    assert res[0]["title"] == "Related Paper"
    assert res[0]["score"] == 90.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_server_tools.py -k "test_get_references_tool or test_get_citations_tool or test_get_related_papers_tool" -v`  
Expected: FAIL

- [ ] **Step 3: Implement methods in `resolver.py` and register tools in `server.py`**

- In `resolver.py`:
  - `get_references`: resolve ID -> call Europe PMC references -> fallback to CrossRef.
  - `get_citations`: resolve ID -> call Europe PMC citations.
  - `get_related_papers`: resolve ID -> call PubMed `fetch_related_papers`.
- In `server.py`:
  - `@mcp.tool() async def get_references(identifier: str, limit: int = 50)`
  - `@mcp.tool() async def get_citations(identifier: str, limit: int = 50)`
  - `@mcp.tool() async def get_related_papers(identifier: str, limit: int = 10)`
- In `README.md`: Document the 3 new tools with signatures and examples.

- [ ] **Step 4: Run full test suite to verify 100% green**

Run: `pytest -v`  
Expected: 100% PASS

- [ ] **Step 5: Commit**

```bash
git add src/scholar_mcp/resolver.py src/scholar_mcp/server.py tests/test_server_tools.py README.md
git commit -m "feat: add get_references, get_citations, and get_related_papers tools"
```
