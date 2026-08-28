# Advanced Extraction & Citation Graph (`scholar-mcp`) Design Specification

**Date:** 2026-08-28  
**Status:** Validated Design  
**Module:** `scholar_mcp`  

---

## 1. Overview & Objectives

Expand `scholar-mcp` with:
1. **Mathematical Formula & Table Preservation in JATS:** Extract LaTeX `<tex-math>` to `$math$` / `$$math$$` in Markdown output without breaking text layout.
2. **Cited References Discovery (`get_references`):** Extract bibliographies of cited papers from full JATS XML, Europe PMC References REST API, or CrossRef Works API.
3. **Forward Citations Discovery (`get_citations`):** Query papers that cite a target paper via Europe PMC Citations API and NCBI E-Link.
4. **Related / Co-Cited Literature Discovery (`get_related_papers`):** Retrieve computationally related papers via NCBI E-Link `pubmed_pubmed` neighbors.

---

## 2. Data Models (`src/scholar_mcp/models.py`)

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

---

## 3. JATS Formula & Math Extraction (`src/scholar_mcp/parsers/jats.py`)

### Requirements
- Strip `DECOMPOSE_TAGS` but KEEP `<tex-math>`, `<mml:math>`, `<inline-formula>`, `<disp-formula>`.
- In `_render_node`:
  - `inline-formula`: Look for inner `<tex-math>`. If present, wrap trimmed content in `$content$`. Fallback to `<mml:math alt="...">` or text.
  - `disp-formula`: Look for inner `<tex-math>`. If present, wrap in `\n\n$$\ncontent\n$$\n\n`. Fallback to plain text.
- Preserve table parsing as standard markdown tables (`| header |`, `|---|`, `| cell |`).

---

## 4. Providers & Resolution Pipelines

### 4.1 References (`resolver.get_references(identifier, limit=50)`)
1. Resolve identifier to `IdentifierMap(pmid, pmcid, doi)`.
2. **Tier 1 (Europe PMC References):**
   - If `pmid` or `pmcid` available:
     - Endpoint: `https://www.ebi.ac.uk/europepmc/webservices/rest/{src}/{id}/references?format=json&pageSize={limit}`
     - Parse reference items (title, authorString, pubYear, journalTitle, doi, pmid).
3. **Tier 2 (CrossRef Works References):**
   - If `doi` available:
     - Endpoint: `https://api.crossref.org/works/{doi}`
     - Parse `message.reference` array.

### 4.2 Forward Citations (`resolver.get_citations(identifier, limit=50)`)
1. Resolve identifier to `IdentifierMap(pmid, pmcid, doi)`.
2. **Europe PMC Citations API:**
   - Endpoint: `https://www.ebi.ac.uk/europepmc/webservices/rest/{src}/{id}/citations?format=json&pageSize={limit}`
   - Extract citing papers with authors, title, year, venue, DOI, and PMID.
3. **NCBI E-Link Cited-In Fallback:**
   - If PMID exists: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&id={pmid}&linkname=pubmed_pubmed_citedin&retmode=json`
   - Retrieve UIDs and fetch metadata via `esummary.fcgi`.

### 4.3 Related Works (`resolver.get_related_papers(identifier, limit=10)`)
1. Resolve identifier to `IdentifierMap(pmid, pmcid, doi)`.
2. If PMID is missing but DOI exists, resolve PMID via PubMed ESearch.
3. Query NCBI E-Link `pubmed_pubmed` neighbors:
   - Endpoint: `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&id={pmid}&cmd=neighbor_score&linkname=pubmed_pubmed&retmode=json`
   - Retrieve neighbor PMIDs and similarity scores.
4. Fetch paper metadata via PubMed `esummary.fcgi`.

---

## 5. FastMCP Server Tools (`src/scholar_mcp/server.py`)

Expose 3 new tools with boundary clamping:
1. `get_references(identifier: str, limit: int = 50) -> list[dict]`
   - Clamp `limit` to `[1, 100]`.
2. `get_citations(identifier: str, limit: int = 50) -> list[dict]`
   - Clamp `limit` to `[1, 100]`.
3. `get_related_papers(identifier: str, limit: int = 10) -> list[dict]`
   - Clamp `limit` to `[1, 25]`.

---

## 6. Testing Strategy
- Unit tests for JATS MathML & LaTeX formula rendering (`tests/test_jats_parser.py`).
- Unit/Mock tests with `respx` for `get_references`, `get_citations`, `get_related_papers` across Europe PMC, CrossRef, and NCBI E-Link (`tests/test_citations_references.py`).
- Server tool dispatch and boundary tests (`tests/test_server_tools.py`).
