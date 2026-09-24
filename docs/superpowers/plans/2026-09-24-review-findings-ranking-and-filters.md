# Review findings: PubMed abstracts, journal filters, Brazil ranking, SJR data, FDA pediatric filter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five review findings where scholar-mcp behavior contradicts its own comments or intent, and measure the Brazilian-ranking pathology before and after the fix.

**Architecture:** Each finding is fixed at the layer that owns it. The scholar-path PubMed provider switches from ESummary to EFetch through one shared record parser. `MedicalPubMedClient.search_articles` takes a `filters` clause that the relaxation ladder never touches. The Brazilian ranker scopes its position prior to BVS records and gives undated records a neutral recency. The SCImago table ships populated. The FDA pediatric filter matches whole words after stripping the OTC child-safety line.

**Tech Stack:** Python 3.10+, httpx + respx, BeautifulSoup (`lxml-xml`), pytest (`asyncio_mode = "auto"`), uv.

**Spec:** No spec document. The review findings, as verified below, are the requirements. Upstream evidence: `zimqa/docs/handoff_enamed_2025_misses_run_analysis.md` §3.4 (off-topic Brazilian top hits).

## Verification of the review findings

Checked against `main` @ `f9d9df27`. zimqa's installed scholar-mcp is `4ec11d2`, two docs-only commits behind, with the same code.

| # | Finding | Verdict | Evidence |
|---|---|---|---|
| 1 | `search_scholar` PubMed hits carry no abstract | **Confirmed.** | `providers/pubmed.py:144-210` calls ESummary and hard-codes `abstract=""`. `ScoringEngine.text_coverage` (`ranking.py:206-229`) then scores those hits on title alone. `RankingPipeline.enrich_citations` fills in citations only, never abstracts. zimqa `scholar.py:72` ships `abstract_snippet: ""`. The medical path fetches abstracts via EFetch (`medical/pubmed.py:219-230`). **Do not reuse `parse_pubmed_xml`**: it reads `citation.find("Year")`, which is `DateCompleted`, and it scans the whole document for `ArticleId` (the reference-list bug that `_own_article_id` fixed). Reuse the provider's own EFetch parsing in `fetch_abstract` instead. |
| 2 | `journals` / `pediatric_literature` lose their journal filter when relaxed | **Confirmed.** Worse than reported for short topics. | Measured with `relax_ladder` on the composed term. For a long topic, every rung is topic-only, with no journal restriction. For `bronchiolitis`, the rungs are `bronchiolitis pediatrics journal jama …`: the `[Journal]` tags become free-text words ANDed onto the query. `medical/databases.py:203-205` and `medical/pediatrics.py:528-530` compose the term, then `medical/pubmed.py:202` ladders it. Reproduced end to end: the Task 2 caller tests fail on `main`. Side effect: `pediatric_literature` also ranks on the composed term, journal names included, because it never re-ranks. |
| 3a | Source-order bonus is applied to the merged local-catalog + BVS list | **Confirmed.** The merge is at `brazil_moh.py:1443-1463`, not `:1495`. | `_rank_records` (`medical/ranking.py:86-89`) says to leave `position_weight` at 0.0 for merged multi-source pools. `rank_brazil_guidelines` passes 0.35 over `local_records + bvs_records`, so local index 0 gets the full prior: +0.171 final score over index 10 at equal lexical coverage. The local-fallback comment at `brazil_moh.py:1397-1399` already concedes that the prior misleads on catalog rows. |
| 3b | Any record with a body sorts above every body-less record | **Confirmed, but intended.** | `medical/ranking.py:210-218` is ENAMED plan B4's hard tier, pinned by `test_rank_brazil_guidelines_tier_holds_for_strong_bodiless_card`. Every gov.br catalog row has a body (`url_trusted=True` plus a description), so every local hit is tier 1. **Decision (user): keep the tier and measure (Tasks 3 and 4).** |
| 3c | PCDT records carry no year, so they always get a low recency score | **Confirmed.** The direction matters. | 0 of 179 PCDT catalog rows have a `year`, and `govbr_pcdt._dict_to_guideline` never sets one. They take the 10-year default: recency 0.371 against 0.820 for a 2024 record, a gap of 0.135 in final score. This *lowers* PCDT scores, so it cannot explain PCDTs being over-ranked. It is a real defect that pushes the other way. |
| 3∑ | Together these explain `pcdt-acidentes-ofidicos` in top 5 for three unrelated questions | **Partially measured.** | Offline, using the real catalog scorer: for Q026, Q044 and Q016, the handoff's recorded top hit equals that query's local-catalog index-0 row. Two of those match on one generic token only (lexical 0.10: `manejo`, `conduta`). The PCDT catalog admits any row that shares one token (`govbr_pcdt.py:358`, `score > 0.0`). The BVS pool is not reproduced offline, so Task 3 measures it live. |
| 4 | Journal-impact weight 0.10 does nothing | **Confirmed. By design until now.** | The committed and installed `scimago_sjr.json` is 31 bytes (`{"issn":{},"name":{}}`). `data/SOURCES.md` says "Ships empty" pending a check of SCImago's terms. With an all-zero feature, the z-scores are 0 (`ranking.py:191-192`), so the term is inert rather than distorting. The working tree holds an uncommitted populated table: 52,793 ISSN and 31,753 name entries from the SJR 2025 CSV. SCImago's terms: "can be used for non-commercial purposes as long as it is cited" ([scimagojr.com/help.php](https://www.scimagojr.com/help.php)). **Decision (user): commit the data.** |
| 5 | FDA pediatric filter matches any label containing "child" | **Confirmed. Two more admission paths in the same predicate.** | `fda.py:31,400-408`: the substring test `"child" in text` also matches `childbearing` ("females of childbearing potential"). `bool(drug.pediatric_warnings)` admits any label with a boxed warning, because `pediatric_warnings` is the whole `boxed_warning` (`fda.py:183-186`). Task 6 tests reproduce all three on `main`. |

Prototype evidence: every task's tests below were run in a throwaway worktree. They fail on `main` (Task 1: 6 of 7, Task 2: 3 of 3, Task 4: 4 of 4, Task 6: 3 of 5; the rest are guards that pass on both) and pass with the code shown. The existing tests each change breaks are named in its task.

## Global Constraints

- Repo: `/Users/gus/Git/scholar-mcp`. Work in a worktree off `main`, never in the main checkout, which carries unrelated uncommitted edits (`AGENTS.md` graft block, `uv.lock`, `govbr_az_catalog.json`):
  `git -C /Users/gus/Git/scholar-mcp worktree add .worktrees/review-findings -b fix/review-findings-2026-09-24 main`
  This plan is untracked in the main checkout. Bring it in as the branch's first commit:
  `cp /Users/gus/Git/scholar-mcp/docs/superpowers/plans/2026-09-24-review-findings-ranking-and-filters.md docs/superpowers/plans/ && git add docs/superpowers/plans/2026-09-24-review-findings-ranking-and-filters.md && git commit -m "docs: plan for the 2026-09-24 review findings"`
- Test command, run from the worktree: `uv run --extra dev pytest -q <paths>`. The default run excludes `-m network`.
- CI gates (`.github/workflows/ci.yml`): `pytest -v` and `python -c "from scholar_mcp.server import main; print('Import OK')"`. No linter runs in CI.
- Commit style: Conventional Commits with a scope (e.g. `fix(pubmed): …`), one commit per task, staging only the files the task names.
- Every behavior change gets a line under `## [Unreleased]` → `### Fixed` (or `### Changed`) in `CHANGELOG.md`, in the same commit.
- Cache rows whose meaning changes get a key bump in the same task. The bump is written with a short comment, following the existing `CACHE_SCHEMA` convention (`brazil_moh.py:122-127`).
- Out of scope: the hard body tier (3b), which stays; `medical/pubmed.py:parse_pubmed_xml`; zimqa sources.

## File map

| File | Task | Responsibility after the change |
|---|---|---|
| `src/scholar_mcp/providers/pubmed.py` | 1 | `search` fetches via EFetch. `_parse_record` is the single EFetch→`PaperMetadata` mapper (articles and Bookshelf records), shared by `search` and `fetch_abstract`. |
| `tests/test_search_scihub_providers.py` | 1 | PubMed search tests mock EFetch, not ESummary. |
| `src/scholar_mcp/medical/pubmed.py` | 2 | `search_articles(..., filters=)` ANDs the filter onto every rung. |
| `src/scholar_mcp/medical/{databases,pediatrics,guidelines}.py` | 2 | Callers pass `filters=` instead of composing the term. |
| `tests/medical/{test_databases,test_pediatrics}.py` | 2 | End-to-end filter-survival tests replace the call-shape tests. |
| `scripts/probe_brazil_ranking.py` | 3 | Live probe: watch-listed off-topic catalog rows in the top 5. |
| `src/scholar_mcp/medical/models.py` | 4 | `BrazilGuideline.origin`. |
| `src/scholar_mcp/medical/{brazil_moh,govbr_pcdt,govbr_az}.py` | 4 | Set `origin`. Cache schema bumps. |
| `src/scholar_mcp/medical/govbr_common.py` | 4 | Cache schema bump. |
| `src/scholar_mcp/medical/ranking.py` | 4 | `_rank_records(source_rank=)` and the undated-recency rule. |
| `tests/medical/{test_medical_ranking,test_brazil_moh}.py` | 4 | Ranking tests. |
| `src/scholar_mcp/data/scimago_sjr.json`, `data/SOURCES.md` | 5 | Populated SJR 2025 table with provenance and terms. |
| `README.md`, `AGENTS.md`, `tests/test_scimago_data.py` | 5 | Docs no longer say "ships empty". A test guards against shipping empty again. |
| `src/scholar_mcp/medical/fda.py`, `tests/medical/test_fda.py` | 6 | Whole-word pediatric matcher. |

---

### Task 1: `search_scholar` PubMed hits carry abstracts (finding 1)

**Files:**
- Modify: `src/scholar_mcp/providers/pubmed.py:144-212` (search body after esearch), `:217-241` (`_own_article_id`), `:282-358` (`fetch_abstract` parsing)
- Test: `tests/test_search_scihub_providers.py:17-18,40-169,1006-1065` plus new tests

**Interfaces:**
- Consumes: `classify_evidence_grade(pubtypes: list[str] | None) -> str | None` (`scholar_mcp.ranking`), `failure_reason(client, resp=, exc=)` (`providers/base.py`).
- Produces: `PubMedProvider._parse_record(record: bs4.Tag) -> PaperMetadata | None` (classmethod). `PubMedProvider.search` returns `PaperMetadata` with `abstract`, `issn`, `study_type`, `evidence_grade`, `source="pubmed"`, in esearch order. Author strings change from ESummary's `"Doudna J"` to EFetch's `"Jennifer Doudna"`, the format `fetch_abstract` already returns. `ESUMMARY_URL` stays, because `fetch_related_papers` (`:419`) still uses it.

- [ ] **Step 1: Write the failing tests**

In `tests/test_search_scihub_providers.py`, replace line 18 (`ESUMMARY = …`) with:

```python
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
```

Insert after the `client` fixture (after line 27):

```python
def _pubmed_article(
    pmid: str,
    title: str = "A PubMed Paper.",
    abstract: str = "",
    journal: str = "Nature",
    year: str = "2020",
    issn: str = "",
    pubtypes: tuple[str, ...] = ("Journal Article",),
    doi: str = "",
) -> str:
    """One EFetch PubmedArticle.

    DateCompleted precedes PubDate on purpose: a parser that takes the first
    <Year> in the record reads the indexing year, not the publication year.
    """
    abstract_xml = (
        f"<Abstract><AbstractText>{abstract}</AbstractText></Abstract>" if abstract else ""
    )
    issn_xml = f'<ISSN IssnType="Print">{issn}</ISSN>' if issn else ""
    types = "".join(f"<PublicationType>{t}</PublicationType>" for t in pubtypes)
    doi_xml = f'<ArticleId IdType="doi">{doi}</ArticleId>' if doi else ""
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <DateCompleted><Year>2001</Year></DateCompleted>
      <Article>
        <Journal>{issn_xml}<JournalIssue><PubDate><Year>{year}</Year></PubDate></JournalIssue>
          <Title>{journal}</Title></Journal>
        <ArticleTitle>{title}</ArticleTitle>
        {abstract_xml}
        <AuthorList><Author><LastName>Doudna</LastName><ForeName>Jennifer</ForeName></Author></AuthorList>
        <PublicationTypeList>{types}</PublicationTypeList>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList><ArticleId IdType="pubmed">{pmid}</ArticleId>{doi_xml}</ArticleIdList></PubmedData>
  </PubmedArticle>"""


# NCBI Bookshelf chapter (StatPearls): Book lists editors before the chapter's
# authors, and the record has no Journal.
_STATPEARLS_RECORD = """
  <PubmedBookArticle>
    <BookDocument>
      <PMID Version="1">28613625</PMID>
      <ArticleIdList><ArticleId IdType="bookaccession">NBK430732</ArticleId></ArticleIdList>
      <Book>
        <Publisher><PublisherName>StatPearls Publishing</PublisherName></Publisher>
        <BookTitle book="statpearls">StatPearls</BookTitle>
        <PubDate><Year>2025</Year><Month>01</Month></PubDate>
        <AuthorList Type="editors"><Author><LastName>Editor</LastName><ForeName>Ed</ForeName></Author></AuthorList>
      </Book>
      <ArticleTitle book="statpearls" part="article-20379">Dengue Fever</ArticleTitle>
      <AuthorList Type="authors"><Author><LastName>Schaefer</LastName><ForeName>Thomas J</ForeName></Author></AuthorList>
      <PublicationType UI="D016454">Review</PublicationType>
      <Abstract><AbstractText>Dengue is a mosquito-borne viral infection.</AbstractText></Abstract>
    </BookDocument>
    <PubmedBookData><ArticleIdList><ArticleId IdType="pubmed">28613625</ArticleId></ArticleIdList></PubmedBookData>
  </PubmedBookArticle>"""


def _efetch_set(*records: str) -> str:
    return f'<?xml version="1.0"?><PubmedArticleSet>{"".join(records)}</PubmedArticleSet>'
```

`_mock_efetch(xml)` already exists at line 857. Reuse it. Python resolves it at call time.

Rewrite the ESummary-based tests:

1. `test_pubmed_search_surfaces_relaxed_variant`: replace the `respx.get(url__startswith=ESUMMARY).mock(...)` block (lines 53-68) with:
   ```python
       _mock_efetch(_efetch_set(_pubmed_article("32000000", title="A Relaxed Paper")))
   ```
2. Replace `test_pubmed_search_returns_metadata` (lines 79-108) entirely with:
   ```python
   @respx.mock
   async def test_pubmed_search_returns_abstract_and_metadata(client):
       """ESummary carries no abstract: every scholar-path PubMed hit used to be
       scored on its title alone and reached the agent with an empty snippet."""
       esearch_route = respx.get(url__startswith=ESEARCH).mock(
           return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["32000000"]}})
       )
       _mock_efetch(
           _efetch_set(
               _pubmed_article("32000000", abstract="Cas9 edits genomes.", doi="10.1038/nature123")
           )
       )
       results = await PubMedProvider(client, Settings()).search("crispr", num_results=5, sort="relevance")
       assert len(results) == 1
       paper = results[0]
       assert paper.abstract == "Cas9 edits genomes."
       assert paper.title == "A PubMed Paper"
       assert paper.pmid == "32000000"
       assert paper.doi == "10.1038/nature123"
       assert paper.year == "2020"  # PubDate, not DateCompleted
       assert paper.venue == "Nature"
       assert paper.source == "pubmed"
       # NCBI's default esearch order is date, not relevance.
       assert esearch_route.calls.last.request.url.params.get("sort") == "relevance"
   ```
3. `test_pubmed_search_sort_date`: replace its ESummary mock (lines 116-132) with:
   ```python
       _mock_efetch(_efetch_set(_pubmed_article("32000000")))
   ```
4. Replace `test_pubmed_search_captures_pubtype_and_issn` (lines 139-169) with:
   ```python
   @respx.mock
   async def test_pubmed_search_captures_pubtype_and_issn(client):
       respx.get(url__startswith=ESEARCH).mock(
           return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["111"]}})
       )
       _mock_efetch(
           _efetch_set(
               _pubmed_article(
                   "111",
                   title="A Randomized Trial of X.",
                   journal="New England Journal of Medicine",
                   year="2024",
                   issn="0028-4793",
                   pubtypes=("Journal Article", "Randomized Controlled Trial"),
               )
           )
       )
       results = await PubMedProvider(client, Settings()).search("x trial", num_results=5)
       assert len(results) == 1
       assert results[0].study_type == "Journal Article; Randomized Controlled Trial"
       assert results[0].evidence_grade == "1b"
       assert results[0].issn == "0028-4793"
   ```
5. Delete `_ESUMMARY_RECORD` (lines 1006-1017).
6. Replace `test_pubmed_search_sends_ncbi_credentials` (lines 1020-1037) with:
   ```python
   @respx.mock
   async def test_pubmed_search_sends_ncbi_credentials(client):
       """Both E-utility calls of a search must carry api_key/email/tool
       (2.8 req/s without a key, 9 with one)."""
       esearch_route = respx.get(url__startswith=ESEARCH).mock(
           return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["32000000"]}})
       )
       efetch_route = respx.get(url__startswith=EFETCH).mock(
           return_value=httpx.Response(200, text=_efetch_set(_pubmed_article("32000000")))
       )
       settings = Settings(pubmed_api_key="KEY123", pubmed_email="a@b.c", pubmed_tool="T")
       results = await PubMedProvider(client, settings).search("crispr", num_results=5)
       assert len(results) == 1
       for route in (esearch_route, efetch_route):
           params = route.calls.last.request.url.params
           assert params.get("api_key") == "KEY123"
           assert params.get("email") == "a@b.c"
           assert params.get("tool") == "T"
   ```
7. `test_pubmed_search_relaxes_long_query_preserving_filters`: replace its ESummary mock (lines 1051-1053) with:
   ```python
       _mock_efetch(_efetch_set(_pubmed_article("32000000")))
   ```

Append the new tests at the end of the file:

```python
@respx.mock
async def test_pubmed_search_parses_bookshelf_records(client):
    """StatPearls and other Bookshelf chapters are frequent Best Match hits;
    EFetch returns them as PubmedBookArticle, not PubmedArticle."""
    respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["28613625"]}})
    )
    _mock_efetch(_efetch_set(_STATPEARLS_RECORD))
    results = await PubMedProvider(client, Settings()).search("dengue", num_results=5)
    assert len(results) == 1
    book = results[0]
    assert book.title == "Dengue Fever"
    assert book.venue == "StatPearls"
    assert book.year == "2025"
    assert book.abstract == "Dengue is a mosquito-borne viral infection."
    assert book.authors == ["Thomas J Schaefer"]  # chapter authors, not book editors
    assert book.study_type == "Review"


@respx.mock
async def test_pubmed_search_keeps_esearch_relevance_order(client):
    """esearch's order is NCBI's relevance ranking; EFetch does not promise it."""
    respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["2", "1"]}})
    )
    _mock_efetch(_efetch_set(_pubmed_article("1"), _pubmed_article("2")))
    results = await PubMedProvider(client, Settings()).search("crispr", num_results=5)
    assert [p.pmid for p in results] == ["2", "1"]


@respx.mock
async def test_pubmed_search_efetch_failure_reports_error(client):
    respx.get(url__startswith=ESEARCH).mock(
        return_value=httpx.Response(200, json={"esearchresult": {"idlist": ["32000000"]}})
    )
    respx.get(url__startswith=EFETCH).mock(return_value=httpx.Response(500))
    provider = PubMedProvider(client, Settings())
    assert await provider.search("crispr", num_results=5) == []
    assert provider.last_error is not None


@respx.mock
async def test_pubmed_fetch_abstract_reads_bookshelf_record(client):
    _mock_efetch(_efetch_set(_STATPEARLS_RECORD))
    meta = await PubMedProvider(client, Settings()).fetch_abstract(IdentifierMap(pmid="28613625"))
    assert meta is not None
    assert meta.abstract == "Dengue is a mosquito-borne viral infection."
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev pytest -q tests/test_search_scihub_providers.py -k "pubmed"`
Expected: FAIL. Every search test gets `assert 0 == 1` or an empty list, because `search` still calls the unmocked ESummary endpoint. `test_pubmed_fetch_abstract_reads_bookshelf_record` fails with `assert None is not None`. `test_pubmed_search_efetch_failure_reports_error` passes: it guards the new call's failure path.

- [ ] **Step 3: Implement**

In `src/scholar_mcp/providers/pubmed.py`, replace lines 144-212 (from `summary_params = {` through `return papers`) with:

```python
            # EFetch, not ESummary: ESummary carries no abstract, which left
            # every scholar-path PubMed hit scored by ScoringEngine on its
            # title alone and shipped to the agent with an empty snippet.
            # One call either way; EFetch carries everything ESummary did.
            fetch_resp = await self.http_client.get(
                EFETCH_URL,
                params={
                    **self._base_params(),
                    "db": "pubmed",
                    "id": ",".join(id_list),
                    "rettype": "xml",
                    "retmode": "xml",
                },
            )
            if fetch_resp is None or fetch_resp.status_code != 200:
                self.last_error = failure_reason(self.http_client, resp=fetch_resp)
                return []

            soup = BeautifulSoup(fetch_resp.content, "lxml-xml")
            by_pmid: dict[str, PaperMetadata] = {}
            for record in soup.find_all(["PubmedArticle", "PubmedBookArticle"]):
                paper = self._parse_record(record)
                if paper is not None and paper.pmid:
                    by_pmid[paper.pmid] = paper
            # esearch's order is NCBI's relevance ranking; EFetch does not
            # promise to preserve it.
            return [by_pmid[str(pmid)] for pmid in id_list if str(pmid) in by_pmid]
```

Replace `_own_article_id` (lines 217-241) with the version below, which reaches `PubmedBookData`. Add `_parse_record` directly after it:

```python
    @staticmethod
    def _own_article_id(article: Any, id_type: str) -> str:
        """Return the record's own ArticleId of ``id_type``, or "" if absent.

        A record's identifiers live in the ArticleIdList that is a direct child
        of PubmedData (PubmedBookData for a Bookshelf record). Every cited
        reference under ReferenceList carries its own
        nested ArticleIdList, so a document-wide scan picks up a cited
        reference's identifier instead of the record's (observed live: PMID
        39770434 yielded the DOI of a 2015 paper it cites). Selecting only the
        direct child cannot reach a nested list, whatever containers NCBI adds
        later. Empty elements are skipped rather than ending the scan.
        """
        pubmed_data = article.find(["PubmedData", "PubmedBookData"], recursive=False)
        if pubmed_data is None:
            return ""
        id_list = pubmed_data.find("ArticleIdList", recursive=False)
        if id_list is None:
            return ""
        for aid in id_list.find_all("ArticleId", recursive=False):
            if aid.get("IdType") != id_type:
                continue
            value = aid.get_text(" ", strip=True)
            if value:
                return value
        return ""

    @classmethod
    def _parse_record(cls, record: Any) -> PaperMetadata | None:
        """Map one EFetch ``PubmedArticle`` or ``PubmedBookArticle`` to PaperMetadata.

        Shared by ``search`` (a batch) and ``fetch_abstract`` (one record), so
        the two cannot disagree on a field. A Bookshelf record (StatPearls and
        other NCBI books, frequent Best Match hits for clinical queries) keeps
        its title, authors, date and abstract under ``BookDocument``; it has no
        ``Journal``, so its venue is the book title and its ISSN is absent.
        Returns None for a record without a PMID.
        """
        pmid_elem = record.find("PMID")
        pmid = pmid_elem.get_text(strip=True) if pmid_elem is not None else ""
        if not pmid:
            return None

        title_elem = record.find("ArticleTitle") or record.find("BookTitle")
        title = title_elem.get_text(" ", strip=True).rstrip(".") if title_elem is not None else ""

        abstract_texts: list[str] = []
        abstract_elem = record.find("Abstract")
        if abstract_elem is not None:
            for p in abstract_elem.find_all("AbstractText"):
                txt = p.get_text(" ", strip=True)
                if not txt:
                    continue
                label = p.get("Label") or p.get("label")
                abstract_texts.append(f"{label.strip()}: {txt}" if label else txt)

        # A Bookshelf record lists the book's editors before the chapter's
        # authors; prefer the authors list when the two are typed.
        authors: list[str] = []
        author_list = record.find("AuthorList", attrs={"Type": "authors"}) or record.find(
            "AuthorList"
        )
        if author_list is not None:
            for author in author_list.find_all("Author", recursive=False):
                collective = author.find("CollectiveName")
                if collective is not None and collective.get_text(strip=True):
                    authors.append(collective.get_text(" ", strip=True))
                    continue
                last = author.find("LastName")
                fore = author.find("ForeName") or author.find("Initials")
                full = " ".join(
                    part.get_text(" ", strip=True) for part in (fore, last) if part is not None
                ).strip()
                if full:
                    authors.append(full)

        year = ""
        pub_date = record.find("PubDate")
        if pub_date is not None:
            year_elem = pub_date.find("Year")
            if year_elem is not None:
                year = year_elem.get_text(strip=True)
            else:
                # MedlineDate: free text such as "2019 Nov-Dec".
                match = re.search(r"\b(?:19|20)\d{2}\b", pub_date.get_text(" ", strip=True))
                year = match.group(0) if match else ""

        journal = record.find("Journal")
        venue_elem = journal.find("Title") if journal is not None else record.find("BookTitle")
        venue = venue_elem.get_text(" ", strip=True) if venue_elem is not None else ""
        issn_elem = journal.find("ISSN") if journal is not None else None
        issn = issn_elem.get_text(strip=True) if issn_elem is not None else ""

        pubtypes = [
            text
            for text in (pt.get_text(" ", strip=True) for pt in record.find_all("PublicationType"))
            if text
        ]

        # A record states its own DOI in its own ArticleIdList or, when that
        # entry is absent, in Article/ELocationID.
        doi = cls._own_article_id(record, "doi")
        if not doi:
            article_elem = record.find("Article")
            elocation = (
                article_elem.find("ELocationID", attrs={"EIdType": "doi"})
                if article_elem is not None
                else None
            )
            if elocation is not None:
                doi = elocation.get_text(" ", strip=True)

        return PaperMetadata(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            doi=doi or None,
            pmid=pmid,
            pmcid=cls._own_article_id(record, "pmc") or None,
            abstract="\n\n".join(abstract_texts),
            oa_status="unknown",
            issn=issn or None,
            study_type="; ".join(pubtypes) or None,
            evidence_grade=classify_evidence_grade(pubtypes),
            source="pubmed",
        )
```

In `fetch_abstract`, replace lines 282-358 (from `soup = BeautifulSoup(resp.content, "lxml-xml")` through the closing `)` of the `return PaperMetadata(...)`) with:

```python
            soup = BeautifulSoup(resp.content, "lxml-xml")
            record = soup.find(["PubmedArticle", "PubmedBookArticle"])
            if record is None:
                return None
            paper = self._parse_record(record)
            if paper is None:
                return None
            # ids.* is the caller's echo: it fills a field only when the
            # record itself states none.
            paper.doi = paper.doi or ids.doi
            paper.pmcid = paper.pmcid or ids.pmcid
            return paper
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q tests/test_search_scihub_providers.py tests/test_invalid_doi_logging.py tests/test_waterfall_resolver.py tests/test_citations_references.py tests/test_server_tools.py tests/test_ranking.py`
Expected: PASS. In the prototype, the only failures across these files were the six ESummary-mocking tests this task rewrites. `test_invalid_doi_logging.py`'s ESummary mock is never reached (its esearch returns no ids) and stays untouched.

- [ ] **Step 5: CHANGELOG and commit**

Add under `## [Unreleased]` → `### Fixed` in `CHANGELOG.md`:

```markdown
- **`search_papers` PubMed hits carry abstracts**: `PubMedProvider.search` fetches records with EFetch instead of ESummary, which has no abstract field. PubMed hits were being re-ranked on their title alone and returned with an empty abstract. Bookshelf chapters (e.g. StatPearls) are parsed too. Author names now use the `"Forename Lastname"` form that `fetch_abstract` already returned.
```

```bash
git add src/scholar_mcp/providers/pubmed.py tests/test_search_scihub_providers.py CHANGELOG.md
git commit -m "fix(pubmed): fetch search hits via EFetch so they carry abstracts"
```

---

### Task 2: Journal filters survive query relaxation (finding 2)

**Files:**
- Modify: `src/scholar_mcp/medical/pubmed.py:161-170,190,205`
- Modify: `src/scholar_mcp/medical/databases.py:198,203-211`
- Modify: `src/scholar_mcp/medical/pediatrics.py:523,528-530`
- Modify: `src/scholar_mcp/medical/guidelines.py:163-167,180-182,212-214`
- Test: `tests/medical/test_databases.py:443-458` (replace), `tests/medical/test_pediatrics.py:246-264` (replace)

**Interfaces:**
- Produces: `MedicalPubMedClient.search_articles(query: str, max_results: int = 10, relax: bool = True, filters: str | None = None) -> tuple[list[MedicalArticle], CacheMetadata]`. Every esearch term is `f"({topic}) AND ({filters})"`, where `topic` is `query` or a rung of `relax_ladder(query)`. Results are ranked against `query` alone. `CacheMetadata.relaxed_query` is a topic-only rung. The cache key gains `:filters=<clause>` when `filters` is set.

- [ ] **Step 1: Write the failing tests**

In `tests/medical/test_databases.py`, replace `test_search_medical_journals_composes_query` (lines 443-458). It only asserts the call shape the fix removes. Put these in its place:

```python
_LONG_TOPIC = (
    "Epstein-Barr virus infectious mononucleosis exudative tonsillitis "
    "posterior cervical lymphadenopathy rash adolescent"
)


@respx.mock
async def test_pubmed_client_filters_survive_every_relaxed_rung(tmp_path: Path):
    """A filter composed into the query was fed to relax_ladder, which strips
    quotes, parentheses and OR: the relaxed rungs lost the [Journal] clause or
    turned it into free-text words. ``filters`` is ANDed onto every rung."""
    from scholar_mcp.query_relax import relax_ladder

    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    ladder = relax_ladder(_LONG_TOPIC)
    clause = '"Lancet"[Journal] OR "BMJ"[Journal]'
    try:
        def _router(request: httpx.Request) -> httpx.Response:
            # Only the 4-token rung answers.
            hit = request.url.params.get("term", "").startswith(f"({ladder[2]}) AND")
            return httpx.Response(200, json={"esearchresult": {"idlist": ["888"] if hit else []}})

        respx.get(EU_SEARCH_URL).mock(side_effect=_router)
        respx.get(EU_FETCH_URL).respond(content=_EFETCH_ARTICLE)

        articles, meta = await client.search_articles(_LONG_TOPIC, max_results=5, filters=clause)

        assert _esearch_terms() == [
            f"({_LONG_TOPIC}) AND ({clause})",
            f"({ladder[1]}) AND ({clause})",
            f"({ladder[2]}) AND ({clause})",
        ]
        assert len(articles) == 1
        assert meta.relaxed_query == ladder[2]
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_medical_journals_keeps_journal_filter_when_relaxing(tmp_path: Path):
    from scholar_mcp.medical.clinical_trials import ClinicalTrialsClient

    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    engine = MedicalDatabasesEngine(
        pubmed=MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings),
        clinical_trials=ClinicalTrialsClient(http_client=http_client, cache=cache, settings=settings),
        http_client=http_client,
        cache=cache,
        settings=settings,
        jitter_range=None,
    )
    try:
        respx.get(EU_SEARCH_URL).respond(json={"esearchresult": {"idlist": []}})
        await engine.search_medical_journals(_LONG_TOPIC)
        terms = _esearch_terms()
        assert len(terms) == 4  # initial + 3 relaxed rungs
        assert all('"New England Journal of Medicine"[Journal]' in t for t in terms)
    finally:
        await cache.close()
        await http_client.aclose()
```

(`EU_SEARCH_URL`, `EU_FETCH_URL`, `_EFETCH_ARTICLE` and `_esearch_terms` are module-level in this file, at lines 14-15 and 508-522.)

In `tests/medical/test_pediatrics.py`, replace `test_search_pediatric_literature_composes_journal_query` (lines 246-264) with:

```python
@respx.mock
async def test_search_pediatric_literature_keeps_journal_filter_when_relaxing(tmp_path: Path):
    from scholar_mcp.medical.pubmed import MedicalPubMedClient

    engine, cache, http_client = await _engine(tmp_path)
    engine.pubmed = MedicalPubMedClient(
        http_client=http_client, cache=cache, settings=engine.settings
    )
    esearch = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    try:
        respx.get(esearch).respond(json={"esearchresult": {"idlist": []}})
        await engine.search_pediatric_literature(
            "Epstein-Barr virus infectious mononucleosis exudative tonsillitis "
            "posterior cervical lymphadenopathy rash adolescent",
            max_results=5,
        )
        terms = [
            c.request.url.params.get("term", "")
            for c in respx.calls
            if str(c.request.url).startswith(esearch)
        ]
        assert len(terms) == 4  # initial + 3 relaxed rungs
        assert all('"JAMA Pediatrics"[Journal]' in t for t in terms)
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev pytest -q tests/medical/test_databases.py tests/medical/test_pediatrics.py -k "filter"`
Expected: FAIL. The client test raises `TypeError: … unexpected keyword argument 'filters'`. Both caller tests fail at `assert all(...)`, because the relaxed rungs carry no `[Journal]`. That is the finding, reproduced.

- [ ] **Step 3: Implement**

`src/scholar_mcp/medical/pubmed.py`: replace the signature and cache key (lines 161-170) with:

```python
    async def search_articles(
        self,
        query: str,
        max_results: int = 10,
        relax: bool = True,
        filters: str | None = None,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        """Search PubMed for ``query``, optionally restricted by ``filters``.

        ``filters`` is a PubMed clause (e.g. ``"NEJM"[Journal] OR ...``)
        ANDed onto the topic on every esearch, the relaxed rungs included:
        the ladder only ever shortens ``query``. Composing the clause into
        ``query`` instead would hand it to ``relax_ladder``, which strips the
        quotes, parentheses and ``OR`` and turns the filter into free-text
        words or drops it. Results are ranked against ``query`` alone.
        """
        # The cache key stays the original query: a relaxed hit is still the
        # answer to what the caller asked, and the key must not fan out per
        # ladder step.
        cache_key = f"pubmed:search:{query}:{max_results}"
        if filters:
            cache_key = f"{cache_key}:filters={filters}"

        def _term(topic: str) -> str:
            return f"({topic}) AND ({filters})" if filters else topic

```

Line 190: `idlist, errored = await self._esearch(query, max_results)` → `idlist, errored = await self._esearch(_term(query), max_results)`.
Line 205: `idlist, errored = await self._esearch(variant, max_results)` → `idlist, errored = await self._esearch(_term(variant), max_results)`.

`src/scholar_mcp/medical/databases.py`: line 198 becomes

```python
        # v2: rows written before the journal filter survived relaxation hold
        # unfiltered PubMed results.
        cache_key = f"medical_journals:v2:{query}"
```

and lines 203-211 (from `journal_filters = …` through the end of the ranking comment) become

```python
        journal_filters = " OR ".join(f'"{j}"[Journal]' for j in TOP_JOURNALS)
        articles, pubmed_meta = await self.pubmed.search_articles(
            query, max_results=15, filters=journal_filters
        )

        deduped, _ = deduplicate_papers([a.to_dict() for a in articles])
        # Rank before slicing so the cap keeps the best 15, not the first 15.
```

`src/scholar_mcp/medical/pediatrics.py`: line 523 becomes

```python
        # v2: rows written before the journal filter survived relaxation hold
        # unfiltered PubMed results.
        cache_key = f"pediatric_journals:v2:{query}:{max_results}"
```

and lines 528-530 become

```python
        journal_filters = " OR ".join(f'"{j}"[Journal]' for j in PEDIATRIC_JOURNALS)
        articles, pubmed_meta = await self.pubmed.search_articles(
            query, max_results=max_results, filters=journal_filters
        )
```

`src/scholar_mcp/medical/guidelines.py`: one convention for filtered PubMed terms. The esearch terms this path sends do not change. Lines 163-167 (the Layer 1 comment up to "…would mangle it.") become

```python
        # Layer 1: Search with formal publication type filters, relaxed down
        # the ladder while the query keeps over-constraining PubMed to too
        # few results. Results accumulate across ladder steps, so this path
        # walks the ladder itself (relax=False) instead of the client's
        # stop-at-first-hit walk; ``filters`` keeps the publication-type
        # clause on every step.
```

lines 180-182 become

```python
            articles_step, meta_step = await self.pubmed.search_articles(
                q, max_results=20, relax=False, filters=pt_query
            )
```

and lines 212-214 become

```python
                articles_l2, meta_l2 = await self.pubmed.search_articles(
                    q, max_results=20, relax=False, filters=kw_terms
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q tests/medical/test_databases.py tests/medical/test_pediatrics.py tests/medical/test_guidelines.py tests/medical/test_pubmed.py tests/medical/test_enamed_2026_misses_engines.py tests/test_server_medical.py`
Expected: PASS. In the prototype, the only failures across these files were the two call-shape tests this task replaces. `test_guidelines.py` checks wire-level `[pt]`/`[tiab]` terms, which are unchanged.

- [ ] **Step 5: CHANGELOG and commit**

`### Fixed`:

```markdown
- **Journal filters survive query relaxation**: `search_medical_journals` and `search_pediatric_literature` pass their `[Journal]` clause as `MedicalPubMedClient.search_articles(filters=)`. The client ANDs it onto every relaxation rung. Composed into the query, it went through `relax_ladder`, which stripped it, so relaxed rungs searched all of PubMed. `pediatric_literature` now also ranks on the user query instead of the composed term. Cache keys `medical_journals:v2:` and `pediatric_journals:v2:` retire rows cached from unfiltered searches.
```

```bash
git add src/scholar_mcp/medical/pubmed.py src/scholar_mcp/medical/databases.py \
  src/scholar_mcp/medical/pediatrics.py src/scholar_mcp/medical/guidelines.py \
  tests/medical/test_databases.py tests/medical/test_pediatrics.py CHANGELOG.md
git commit -m "fix(pubmed): keep journal filters on every relaxation rung"
```

---

### Task 3: Brazilian ranking probe and baseline (finding 3, measurement)

The review marks the causal link as unmeasured. This task records the live baseline *before* Task 4 changes the ranker. Task 4 reruns the same probe.

**Files:**
- Create: `scripts/probe_brazil_ranking.py`
- Modify: this plan, `## Measurements` at the end (record the output)

**Interfaces:**
- Consumes: `BrazilMoHEngine(http_client=, cache=, settings=)`, `search_guidelines(query, limit=10, collection="all") -> (list[BrazilGuideline], CacheMetadata)`, and `CacheMetadata.error_kind`.
- Produces: exit status 0 when no watch-listed row is in any top 5, 1 when one is, 2 when any query hit a backend error (inconclusive).

- [ ] **Step 1: Write the probe**

```python
#!/usr/bin/env python3
"""Count off-topic gov.br catalog rows in live brazil_guidelines top-5s.

zimqa's ENAMED 2025 misses run (docs/handoff_enamed_2025_misses_run_analysis.md
§3.4) recorded catalog rows topping unrelated queries, e.g.
pcdt-acidentes-ofidicos on three of them. This replays that run's
brazil_guidelines queries against live BVS with a fresh cache, so no row
cached under an older ranker is read, and prints each top 5 with its origin,
body flag and score.

Exit status: 0 no watch-listed row in any top 5; 1 at least one; 2 a query
hit a backend error (the merged path was not measured -- rerun).
"""

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scholar_mcp.config import Settings  # noqa: E402
from scholar_mcp.medical.brazil_moh import BrazilMoHEngine  # noqa: E402
from scholar_mcp.utils.http import AsyncHttpClient  # noqa: E402
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager  # noqa: E402

TOP_N = 5

# The handoff's §3.4 table (as recorded, truncated at the ellipsis), then the
# run trace's own queries (zimqa eval/results/enamed-2025-misses-run/answers.jsonl).
QUERIES = [
    "citologia oncótica LSIL lesão intraepitelial",
    "retenção de placenta conduta 30 minutos",
    "dengue grupo B manejo hidratação parenteral",
    "tuberculose retomada tratamento abandono",
    "curvas de crescimento síndrome de Down",
    "curvas crescimento síndrome Down recém-nascido puericultura",
    "dengue grupo B manejo hidratação parenteral antígeno NS1 leito de observação",
    "dengue sinais de alerta grupo B manejo hidratação",
    "terceiro estágio trabalho de parto conduta placenta retida",
    "lesão intraepitelial de baixo grau conduta colposcopia",
]

# Record-id fragments of the off-topic top hits the handoff recorded.
WATCH = (
    "acidentes-ofidicos",
    "sindrome-mielodisplasica-de-baixo-risco",
    "disturbio-mineral-osseo-na-doenca-renal-cronica",
    "deficiencia-do-hormonio-de-crescimento-hipopituitarismo",
    "acidentes-por-animais-peconhentos",
)


async def main() -> int:
    settings = Settings.load()
    watched = 0
    errored = 0
    with tempfile.TemporaryDirectory() as tmp:
        http_client = AsyncHttpClient(settings)
        cache = SQLiteCacheManager(db_path=Path(tmp) / "cache.db", settings=settings)
        engine = BrazilMoHEngine(http_client=http_client, cache=cache, settings=settings)
        try:
            for query in QUERIES:
                records, meta = await engine.search_guidelines(query, limit=10, collection="all")
                errored += bool(meta.error)
                print(f"\n{query!r}  error={meta.error} error_kind={meta.error_kind or '-'}")
                for rank, r in enumerate(records[:TOP_N], 1):
                    hit = any(w in r.record_id for w in WATCH)
                    watched += hit
                    score = "-" if r.score is None else f"{r.score:.3f}"
                    origin = getattr(r, "origin", "") or "-"
                    print(
                        f"  {rank}. {r.record_id[:70]:<70} origin={origin:<13} "
                        f"body={r.has_full_text!s:<5} score={score}"
                        + ("   <-- watch-listed" if hit else "")
                    )
        finally:
            await cache.close()
            await http_client.aclose()
    print(f"\nwatch-listed rows in a top {TOP_N}: {watched}; errored queries: {errored}")
    if errored:
        return 2
    return 1 if watched else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

`getattr(r, "origin", "")` lets the same script run on the baseline, before Task 4 adds the field.

- [ ] **Step 2: Record the baseline (network)**

Run: `uv run python scripts/probe_brazil_ranking.py | tee /tmp/brazil-probe-before.txt; echo "exit=$?"`
Expected: exit 1 (watch-listed rows present) or 2 (BVS degraded). If exit is 2, rerun later. The measurement is only valid when every query's `error` is `False`.
Paste the per-query top 5 and the summary line under `## Measurements → Baseline` at the end of this plan.

- [ ] **Step 3: Commit**

```bash
git add scripts/probe_brazil_ranking.py docs/superpowers/plans/2026-09-24-review-findings-ranking-and-filters.md
git commit -m "chore(brazil_moh): probe off-topic catalog rows in live top-5s"
```

---

### Task 4: Position prior scoped to BVS; undated records take neutral recency (findings 3a, 3c)

**Files:**
- Modify: `src/scholar_mcp/medical/models.py:262-265,272` (`BrazilGuideline`)
- Modify: `src/scholar_mcp/medical/brazil_moh.py:122-128` (`CACHE_SCHEMA`), `:630` (`_build_record`), `:1397-1401`, `:1443-1444` (comments)
- Modify: `src/scholar_mcp/medical/govbr_pcdt.py:210`, `src/scholar_mcp/medical/govbr_az.py:196`
- Modify: `src/scholar_mcp/medical/govbr_common.py:239-240` (`CACHE_SCHEMA`)
- Modify: `src/scholar_mcp/medical/ranking.py:68-143` (`_rank_records`), `:169-209` (`rank_brazil_guidelines`)
- Test: `tests/medical/test_medical_ranking.py:303-316` (replace), `tests/medical/test_brazil_moh.py` (append after line 3083)

**Interfaces:**
- Produces: `BrazilGuideline.origin: str = ""`, with values `"bvs"` (set by `brazil_moh._build_record`) and `"govbr_catalog"` (set by both gov.br `_dict_to_guideline` converters). `_rank_records(..., source_rank: Callable[[R], int | None] | None = None)`. When `source_rank` is omitted, every record's rank is its input index (unchanged for PubMed callers). A `None` rank means lexical-only relevance. New recency rule for every medical ranked path: a missing or unparseable year takes the mean recency of the dated records in the same call, and the 10-year default applies only when none is dated.
- Kept: the hard body tier (`medical/ranking.py:210-218`) and `NO_FULL_TEXT_SCORE_FACTOR`. This is the user's decision.

- [ ] **Step 1: Write the failing tests**

In `tests/medical/test_medical_ranking.py`, replace `test_rank_brazil_guidelines_missing_year_uses_default_age` (lines 303-316). It pins the contract finding 3c rejects. Put these in its place:

```python
def test_rank_brazil_guidelines_undated_record_takes_pool_mean_recency():
    # The PCDT catalog carries no year at all. A missing year is absent
    # metadata, not evidence of age: the record scores as an average-aged
    # member of its pool instead of a 10-year-old one.
    guidelines = [
        _guideline("Manejo da dengue", record_id="undated", year=""),
        _guideline("Manejo da dengue", record_id="y2022", year="2022"),
        _guideline("Manejo da dengue", record_id="y2024", year="2024"),
    ]
    ranked = rank_brazil_guidelines(guidelines, "dengue", current_year=2026)
    assert [g.record_id for g in ranked] == ["y2024", "undated", "y2022"]

    # No dated record in the pool: the default age still yields a score.
    garbage = [_guideline("Manejo da dengue", year="n/a")]
    assert rank_brazil_guidelines(garbage, "dengue", current_year=2026)[0].score is not None


def test_rank_brazil_guidelines_catalog_row_does_not_inherit_bvs_head_position():
    # The merge prepends gov.br catalog rows to the BVS list; the Solr
    # position prior belongs to the first BVS record, not to list index 0.
    catalog = _guideline(
        "Manejo da dengue", record_id="catalog", origin="govbr_catalog",
        has_full_text=True, year="2020",
    )
    bvs = _guideline(
        "Manejo da dengue", record_id="bvs", origin="bvs",
        has_full_text=True, year="2020",
    )
    ranked = rank_brazil_guidelines([catalog, bvs], "manejo dengue grave", current_year=2026)
    assert [g.record_id for g in ranked] == ["bvs", "catalog"]


def test_rank_brazil_guidelines_single_token_catalog_match_below_on_topic_bvs():
    # Q026 shape (zimqa handoff §3.4): the snakebite PCDT matches the dengue
    # query on "manejo" alone, sits at merged index 0, and used to outrank an
    # on-topic BVS record five places down.
    ofidicos = _guideline(
        "Acidentes Ofídicos",
        record_id="pcdt-acidentes-ofidicos",
        origin="govbr_catalog",
        has_full_text=True,
        abstract=(
            "Orienta diagnóstico, classificação e tratamento de picadas de serpentes "
            "no SUS, definindo uso racional de soros, monitoramento clínico e manejo "
            "de complicações."
        ),
    )
    fillers = [
        _guideline(f"Boletim epidemiológico {i}", record_id=f"bvs-{i}", origin="bvs",
                   has_full_text=True, year="2020")
        for i in range(4)
    ]
    target = _guideline("Dengue: diagnóstico e manejo clínico", record_id="bvs-dengue",
                        origin="bvs", has_full_text=True, year="2016")
    ranked = rank_brazil_guidelines(
        [ofidicos, *fillers, target],
        "dengue grupo B manejo hidratação parenteral",
        current_year=2026,
    )
    ids = [g.record_id for g in ranked]
    assert ids.index("bvs-dengue") < ids.index("pcdt-acidentes-ofidicos")
```

In `tests/medical/test_brazil_moh.py`, append after `test_pcdt_record_wins_dedupe_against_az_duplicate` (after line 3083):

```python
@respx.mock
async def test_merged_search_scores_catalog_rows_without_bvs_position(tmp_path: Path):
    """End to end through the real converters: the PCDT row sits first in
    the merge, but only the BVS record gets the Solr position prior. The BVS
    record is dated 2000 so recency cannot decide the order, whatever year the
    suite runs in."""
    from scholar_mcp.medical.govbr_pcdt import _dict_to_guideline as pcdt_row

    engine, cache, http_client = await _engine(tmp_path)
    try:
        catalog = pcdt_row(
            {
                "record_id": "pcdt-dengue",
                "title": "Manejo da dengue",
                "download_url": "https://www.gov.br/saude/pt-br/assuntos/pcdt/d/dengue/@@download/file",
            }
        )
        engine.pcdt_engine.search = AsyncMock(
            return_value=([catalog], CacheMetadata(cached=False, cache_age=0, error=False))
        )
        engine.az_engine.search = AsyncMock(
            return_value=([], CacheMetadata(cached=False, cache_age=0, error=False))
        )
        respx.get(url__startswith=BVS_SEARCH_URL).mock(
            return_value=httpx.Response(
                200,
                json=_bvs_response(
                    [_bvs_doc("biblio-dengue", title="Manejo da dengue", da="200001")]
                ),
            )
        )
        records, meta = await engine.search_guidelines("manejo dengue grave", collection="all")
        assert meta.error is False
        assert [r.record_id for r in records] == ["biblio-dengue", "pcdt-dengue"]
    finally:
        await cache.close()
        await http_client.aclose()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev pytest -q tests/medical/test_medical_ranking.py tests/medical/test_brazil_moh.py -k "undated or bvs_head or single_token or without_bvs_position"`
Expected: FAIL. The two `origin=` tests raise `TypeError: … unexpected keyword argument 'origin'`. The undated test gets `['y2024', 'y2022', 'undated']`. The end-to-end test gets `['pcdt-dengue', 'biblio-dengue']`.

- [ ] **Step 3: Implement**

`src/scholar_mcp/medical/models.py`: after line 264 (the end of the `abstract_synthetic` paragraph in the `BrazilGuideline` docstring), insert

```python
    ``origin`` names the list a record came from: ``"bvs"`` for a BVS/iAHx
    Solr hit (relevance-ordered, so ``rank_brazil_guidelines`` applies its
    position prior), ``"govbr_catalog"`` for a gov.br PCDT/A-Z/extended
    catalog row (no relevance order). Empty on rows cached before the field
    existed; the search caches' ``CACHE_SCHEMA`` bump keeps those unread.
```

and after line 272 (`has_full_text: bool = False`) insert `    origin: str = ""`.

`src/scholar_mcp/medical/brazil_moh.py`:
- In `_build_record`, after `fulltext_id=fulltext_id,` (line 630), insert `        origin="bvs",`.
- Line 127 ends the schema comment. Append ` v3: origin (position prior scoped to BVS records).` to its last sentence, and change line 128 to `CACHE_SCHEMA = "v3"`. The same constant also keys `brazil_moh_fulltext:`, so full-text rows are refetched once on demand. That follows the file's one-constant convention.
- Lines 1397-1401 become:
  ```python
              # Standing in for a failed BVS, these rows carry no relevance
              # order of their own, so a lexical near-miss can top the list.
              # Gate on topic first: an empty result is a truthful "not
              # covered", while four unrelated syndromes read as Brazilian
              # evidence downstream.
  ```
- Lines 1443-1444 become:
  ```python
          # Merge local gov.br records (first) and BVS records, deduplicating
          # by record_id. List order does not score: rank_brazil_guidelines
          # applies its position prior by each record's rank among BVS rows.
  ```

`src/scholar_mcp/medical/govbr_pcdt.py`: after `source="brazil-moh",` (line 210), insert `        origin="govbr_catalog",`.
`src/scholar_mcp/medical/govbr_az.py`: after `source="brazil-moh",` (line 196), insert `        origin="govbr_catalog",`.
`src/scholar_mcp/medical/govbr_common.py`: line 239 becomes `# v2: has_full_text (ENAMED misses plan B4). v3: origin.` and line 240 becomes `CACHE_SCHEMA = "v3"`.

`src/scholar_mcp/medical/ranking.py`: replace `_rank_records` (lines 68-143) with:

```python
def _rank_records(
    records: list[R],
    query: str,
    *,
    tokenizer: Callable[[str | None], list[str]],
    text_fields: Callable[[R], tuple[str, str]],
    position_weight: float,
    current_year: int | None = None,
    score_factor: Callable[[R], float] | None = None,
    source_rank: Callable[[R], int | None] | None = None,
) -> list[R]:
    """Score and order records by lexical coverage, source position, and recency.

    Shared by every ranked medical path. ``tokenizer`` must be the same one
    used for both the query and the document text, so the two sides compare.
    ``text_fields`` selects the (title, abstract) text for a record, letting a
    caller widen either side -- the Brazilian path unions the Portuguese and
    English titles, and the abstract with the DeCS descriptors.

    ``position_weight`` blends a relevance-ordered source's own ranking into
    relevance using the ``1/sqrt(rank + 1)`` prior. ``source_rank`` names
    that rank per record: omitted, the input index is every record's rank,
    which holds only for a single relevance-sorted source. A merged pool
    passes ``source_rank`` and returns ``None`` for records from a source
    with no relevance order; those score on lexical coverage alone, exactly
    as a ``position_weight`` of 0.0 would score them.

    ``score_factor``, when given, multiplies each record's score as it is
    assigned -- a call re-scores every record from its raw fields each time,
    so folding a factor in here (rather than mutating ``.score`` after this
    function returns) is idempotent by construction: a second call recomputes
    the same score, it never compounds a prior call's damping.

    Makes no network calls. Assigns ``score`` on the given objects in place and
    returns a new list ordered by it, source order breaking ties. A query that
    tokenizes to nothing leaves ``score`` untouched.

    Scoring contract: ``RELEVANCE_WEIGHT * relevance + RECENCY_WEIGHT * recency``
    (0.7 / 0.3), with a 7-year recency half-life. A record whose year is
    missing or unparseable takes the mean recency of the dated records in the
    same call: absent metadata is not evidence of age. The 10-year default
    age applies only when no record in the call carries a year.
    """
    if not records:
        return []

    terms = tokenizer(query)
    if not terms:
        return list(records)

    now_year = current_year if current_year is not None else datetime.datetime.now().year
    lexical_weight = 1.0 - position_weight

    recencies = [
        ScoringEngine.calculate_recency_feature(
            record.year,
            current_year=now_year,
            half_life_years=RECENCY_HALF_LIFE_YEARS,
            default_age=DEFAULT_AGE_YEARS,
        )
        for record in records
    ]
    dated = [value for value, year in recencies if year is not None]
    undated_recency = sum(dated) / len(dated) if dated else None

    scored: list[tuple[float, int, R]] = []
    for idx, record in enumerate(records):
        title_text, abstract_text = text_fields(record)
        lexical = ScoringEngine.text_coverage(
            terms, title_text, abstract_text, tokenizer=tokenizer
        )

        rank = idx if source_rank is None else source_rank(record)
        if position_weight and rank is not None:
            position = ScoringEngine.calculate_relevance(rank)
            relevance = lexical_weight * lexical + position_weight * position
        else:
            relevance = lexical

        recency, year = recencies[idx]
        if year is None and undated_recency is not None:
            recency = undated_recency

        final_score = RELEVANCE_WEIGHT * relevance + RECENCY_WEIGHT * recency
        if score_factor is not None:
            final_score *= score_factor(record)
        record.score = final_score
        scored.append((final_score, idx, record))

    # Stable: equal scores keep source order (idx).
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [record for _, _, record in scored]
```

In `rank_brazil_guidelines`, replace the docstring paragraph at lines 189-190 (`` ``position_weight`` is non-zero because BVS returns a single … ``) with

```python
    The position prior applies to BVS records only, ranked by their order
    among the BVS records in the input: BVS returns a single relevance-sorted
    list, which is the condition that prior is meant for. Gov.br catalog rows
    (``origin != "bvs"``) are prepended to that list by the merge and carry
    no relevance order, so they score on lexical coverage alone.
```

and replace the `_rank_records(...)` call (lines 198-209) with

```python
    bvs_rank = {
        id(g): rank for rank, g in enumerate(g for g in guidelines if g.origin == "bvs")
    }
    ranked = _rank_records(
        guidelines,
        query,
        tokenizer=tokenize_portuguese,
        text_fields=lambda g: (
            f"{g.title or ''} {g.title_en or ''}",
            " ".join([g.abstract or "", *(g.mesh_subjects or [])]),
        ),
        position_weight=SOURCE_POSITION_WEIGHT,
        current_year=current_year,
        score_factor=lambda g: 1.0 if g.has_full_text else NO_FULL_TEXT_SCORE_FACTOR,
        source_rank=lambda g: bvs_rank.get(id(g)),
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q tests/medical/test_medical_ranking.py tests/medical/test_brazil_moh.py tests/medical/test_govbr_pcdt.py tests/medical/test_govbr_az.py tests/medical/test_extended_moh_search.py tests/medical/test_enamed_2026_misses_engines.py tests/medical/test_models_formatters.py tests/medical/test_databases.py`
Expected: PASS. In the prototype, the only failure across these files was the default-age test this task replaces. The tier tests (`…_bodyless_card_sinks_below_body`, `…_tier_holds_for_strong_bodiless_card`, `…_factor_does_not_affect_order`) still pass unchanged: the tier is kept.

- [ ] **Step 5: Rerun the probe (network) and apply the tier rule**

Run: `uv run python scripts/probe_brazil_ranking.py | tee /tmp/brazil-probe-after.txt; echo "exit=$?"`
Record the output under `## Measurements → After Task 4`, next to the baseline.
Decision rule (the user's "keep tier, measure"):
- exit 0: done. The two defects were the cause. Note that in the PR.
- exit 1: do **not** change the tier here. For each remaining watch-listed row, record in `## Measurements` whether it sits above body-less BVS cards that the probe output shows are on topic. That is the evidence a follow-up needs to choose between damping-only and demoting off-topic catalog rows.
- exit 2: BVS is degraded. Rerun later. Never record a degraded run as the result.

- [ ] **Step 6: CHANGELOG and commit**

`### Fixed`:

```markdown
- **Brazilian guideline ranking — position prior scoped to BVS**: `rank_brazil_guidelines` applied the BVS Solr position prior over the merged gov.br catalog + BVS list, so the catalog row at index 0 took the top-rank bonus whatever its relevance. The prior now follows each record's rank among BVS records (`BrazilGuideline.origin`). Catalog rows score on lexical coverage.
- **Undated records take a neutral recency**: across the medical rankers, a record with no year (every PCDT catalog row) took the mean recency of the dated records in its pool instead of a 10-year default age. `CACHE_SCHEMA` v3 (`brazil_moh`, `govbr_common`) retires rows cached without `origin`.
```

```bash
git add src/scholar_mcp/medical/models.py src/scholar_mcp/medical/brazil_moh.py \
  src/scholar_mcp/medical/govbr_pcdt.py src/scholar_mcp/medical/govbr_az.py \
  src/scholar_mcp/medical/govbr_common.py src/scholar_mcp/medical/ranking.py \
  tests/medical/test_medical_ranking.py tests/medical/test_brazil_moh.py \
  CHANGELOG.md docs/superpowers/plans/2026-09-24-review-findings-ranking-and-filters.md
git commit -m "fix(ranking): scope the BVS position prior; neutral recency for undated rows"
```

---

### Task 5: Ship the SCImago SJR table (finding 4)

**Decision (user): commit the data.** SCImago's terms: non-commercial use, with citation. The table is SCImago's data, not MIT-licensed code. `SOURCES.md` and the README say so.

**Files:**
- Modify: `src/scholar_mcp/data/scimago_sjr.json` (regenerated)
- Modify: `src/scholar_mcp/data/SOURCES.md`, `README.md:188,206-208,294`, `AGENTS.md:19,81`
- Test: `tests/test_scimago_data.py` (append)

**Interfaces:**
- Consumes: `scripts/update_scimago_data.py` (unchanged) and `lookup_journal_impact(issn, venue) -> float | None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scimago_data.py`:

```python
def test_bundled_table_is_populated():
    """The journal_impact weight (0.10) is inert against an empty table: every
    candidate's z-score is 0. The package must ship real SJR values."""
    assert lookup_journal_impact("0028-4793", None) > 0  # NEJM, by ISSN
    assert lookup_journal_impact(None, "The Lancet") > 0  # by normalized name
```

(The autouse fixture clears the `lru_cache`, and this test does not monkeypatch `_SCIMAGO_DATA_PATH`, so it reads the bundled file.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --extra dev pytest -q tests/test_scimago_data.py::test_bundled_table_is_populated`
Expected: FAIL with `TypeError: '>' not supported between instances of 'NoneType' and 'int'`. The worktree has the committed empty table.

- [ ] **Step 3: Regenerate the table from the SCImago CSV**

The CSV is gitignored and exists only in the main checkout (`data/raw/scimago_journal_rank.csv`, the SJR 2025 edition, downloaded 2026-08-31).

```bash
mkdir -p data/raw
cp /Users/gus/Git/scholar-mcp/data/raw/scimago_journal_rank.csv data/raw/
uv run python scripts/update_scimago_data.py
cmp src/scholar_mcp/data/scimago_sjr.json /Users/gus/Git/scholar-mcp/src/scholar_mcp/data/scimago_sjr.json && echo identical
```

Expected: `Wrote 52793 ISSN entries and 31753 name entries …`, then `identical`. If `cmp` differs, keep the regenerated file (it is reproducible from the CSV) and note the difference in the commit body.

- [ ] **Step 4: Update the docs**

Replace `src/scholar_mcp/data/SOURCES.md` lines 1-22 with:

```markdown
# scimago_sjr.json

Journal-impact proxy data for the `journal_impact` ranking signal (no free
official Journal Impact Factor API exists; SCImago Journal Rank stands in).

## Provenance

- Source: SCImago Journal & Country Rank, journal ranking CSV export —
  "All subject areas", "All regions", **2025** edition, downloaded 2026-08-31.
- Generated by `scripts/update_scimago_data.py` → 52,793 ISSN keys and
  31,753 normalized-name keys.

## Terms of use

This file is SCImago's data, not part of the MIT-licensed code. SCImago:
"All the information shown in the SCImago Journal & Country Rank website can
be used for non-commercial purposes as long as it is cited"
(https://www.scimagojr.com/help.php). Citation:

> SCImago, (n.d.). SJR — SCImago Journal & Country Rank [Portal]. Retrieved
> 2026-08-31, from https://www.scimagojr.com

## Refreshing it

1. Go to https://www.scimagojr.com/journalrank.php
2. Select "All subject areas", "All regions", the latest year, output
   format CSV, and download it.
3. Save it to `data/raw/scimago_journal_rank.csv` (gitignored).
4. Run: `python scripts/update_scimago_data.py`
5. Update the edition, download date and key counts above.
```

`README.md`:
- line 188 table row: replace `neutral \`0.0\` until \`scimago_sjr.json\` is populated` with `neutral \`0.0\` when neither ISSN nor name is in the bundled SJR 2025 table`.
- lines 206-208 become:
  ```markdown
  ### Journal impact data (Scimago SJR)

  The `journal_impact` signal reads `src/scholar_mcp/data/scimago_sjr.json`, generated from the SCImago Journal Rank 2025 CSV. That file is SCImago's data, licensed for non-commercial use with citation, not under this repository's MIT license. Provenance, citation and refresh steps: [`src/scholar_mcp/data/SOURCES.md`](src/scholar_mcp/data/SOURCES.md).
  ```
- line 294: replace `(neutral \`0.0\` until \`scimago_sjr.json\` is populated)` with `(neutral \`0.0\` for journals missing from the bundled table)`.

`AGENTS.md`:
- line 19: `# Journal-impact lookup table for the ranking signal (ships empty)` → `# SCImago SJR 2025 journal-impact table (non-commercial terms; see SOURCES.md)`.
- line 81: `(ships empty; see \`src/scholar_mcp/data/SOURCES.md\`)` → `(SCImago SJR 2025; terms and provenance in \`src/scholar_mcp/data/SOURCES.md\`)`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q tests/test_scimago_data.py tests/test_ranking.py tests/test_waterfall_resolver.py tests/test_packaging.py`
Expected: PASS. The Task 1-4 baselines were measured with this table present in the main checkout, and no test depended on it being empty.

- [ ] **Step 6: CHANGELOG and commit**

`### Changed`:

```markdown
- **Journal-impact signal is live**: `scimago_sjr.json` now ships the SCImago Journal Rank 2025 table (52,793 ISSNs). It shipped empty before, so the 0.10 `journal_impact` weight contributed nothing. The data is SCImago's, for non-commercial use with citation (see `src/scholar_mcp/data/SOURCES.md`).
```

```bash
git add src/scholar_mcp/data/scimago_sjr.json src/scholar_mcp/data/SOURCES.md \
  README.md AGENTS.md tests/test_scimago_data.py CHANGELOG.md
git commit -m "feat(ranking): ship the SCImago SJR 2025 table for journal impact"
```

(`data/raw/` stays untracked; it is gitignored.)

---

### Task 6: FDA pediatric filter matches pediatric use, not boilerplate (finding 5)

**Files:**
- Modify: `src/scholar_mcp/medical/fda.py:31` (`PEDIATRIC_TERMS`, removed), `:385` (cache key), `:392-410` (predicate)
- Test: `tests/medical/test_fda.py` (append)

**Interfaces:**
- Produces: module-private `_mentions_pediatric_use(text: str) -> bool`. `PEDIATRIC_TERMS` is deleted: it has no other reader in `src/` or `tests/`.
- Unchanged: `DrugLabel.pediatric_warnings` still holds the whole boxed warning (see Out of scope).

- [ ] **Step 1: Write the failing tests**

Add `import pytest` to `tests/medical/test_fda.py` after `import httpx` (line 3); the file does not import it today. Then append:

```python
def _sectioned_label(**sections):
    result = {
        "openfda": {
            "brand_name": ["Acme Relief"],
            "generic_name": ["Acetaminophen"],
            "manufacturer_name": ["Acme"],
            "product_ndc": ["12345-678"],
        },
        "effective_time": "20240101",
        "purpose": ["Pain reliever"],
        "dosage_and_administration": ["Adults: take 2 tablets every 6 hours."],
    }
    result.update(sections)
    return {"results": [result]}


async def _pediatric_hits(tmp_path: Path, payload):
    client, cache, http_client = await _make_client(tmp_path)
    try:
        respx.get(FDA_URL).respond(json=payload)
        drugs, _ = await client.search_pediatric_drugs("acme relief", limit=5)
        return drugs
    finally:
        await cache.close()
        await http_client.aclose()


@pytest.mark.parametrize(
    "sections",
    [
        {"warnings": ["Keep out of reach of children. In case of overdose, get medical help."]},
        {"use_in_specific_populations": ["Females of childbearing potential should use contraception."]},
        {"boxed_warning": ["WARNING: Risk of serious cardiovascular thrombotic events."]},
    ],
    ids=["otc-out-of-reach", "childbearing", "adult-boxed-warning"],
)
@respx.mock
async def test_search_pediatric_drugs_rejects_non_pediatric_mentions(tmp_path: Path, sections):
    """Each is an adult label the old predicate admitted: substring "child"
    hit the OTC child-safety line and "childbearing", and any boxed warning
    counted because pediatric_warnings holds the whole boxed warning."""
    assert await _pediatric_hits(tmp_path, _sectioned_label(**sections)) == []


@pytest.mark.parametrize(
    "sections",
    [
        {
            "warnings": ["Keep out of reach of children."],
            "dosage_and_administration": ["children under 12 years: ask a doctor"],
        },
        {"boxed_warning": ["WARNING: Suicidal thoughts in children and adolescents."]},
    ],
    ids=["otc-child-directions", "pediatric-boxed-warning"],
)
@respx.mock
async def test_search_pediatric_drugs_keeps_pediatric_labels(tmp_path: Path, sections):
    drugs = await _pediatric_hits(tmp_path, _sectioned_label(**sections))
    assert [d.openfda.brand_name for d in drugs] == [["Acme Relief"]]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --extra dev pytest -q tests/medical/test_fda.py -k "non_pediatric or keeps_pediatric"`
Expected: 3 FAILED (`otc-out-of-reach`, `childbearing`, `adult-boxed-warning`: the adult label is returned), 2 passed (guards for legitimate pediatric labels).

- [ ] **Step 3: Implement**

`src/scholar_mcp/medical/fda.py`: replace line 31 (`PEDIATRIC_TERMS = (...)`) with:

```python
# Whole words only: a substring test let "child" match "childbearing"
# ("females of childbearing potential") and every OTC Drug Facts label.
_PEDIATRIC_RE = re.compile(r"\b(?:pediatric|child(?:ren|hood)?|infants?|neonat(?:e|es|al))\b")

# The OTC Drug Facts child-safety line ("Keep out of reach of children"),
# printed on nearly every OTC label. A storage instruction, not pediatric use.
_OUT_OF_REACH_RE = re.compile(
    r"keep\s+(?:this\s+and\s+all\s+(?:drugs|medicines|medications)\s+)?"
    r"out\s+of\s+(?:the\s+)?(?:sight\s+and\s+)?reach\s+of\s+children"
)


def _mentions_pediatric_use(text: str) -> bool:
    """True when ``text`` names a pediatric population outside the OTC
    child-safety boilerplate."""
    return bool(_PEDIATRIC_RE.search(_OUT_OF_REACH_RE.sub(" ", text.lower())))
```

Line 385 becomes:

```python
        # v2: rows written before whole-word matching admitted any OTC label
        # through its "Keep out of reach of children" line.
        cache_key = f"pediatric_drugs:v2:{query}:{limit}"
```

Replace lines 392-410 (from `pediatric_drugs: list[DrugLabel] = []` through `pediatric_drugs.append(drug)`) with:

```python
        pediatric_drugs: list[DrugLabel] = []
        for drug in base_drugs:
            label_text = " ".join(
                [
                    *drug.purpose,
                    *drug.warnings,
                    *drug.dosage_and_administration,
                    *drug.indications_and_usage,
                    *drug.use_in_specific_populations,
                ]
            )
            # ``pediatric_dosing`` is the label's own Pediatric Use section.
            # ``pediatric_warnings`` holds the whole boxed warning, whatever
            # population it concerns, so it counts only when it names one.
            has_pediatric = (
                bool(drug.pediatric_dosing)
                or _mentions_pediatric_use(drug.pediatric_warnings or "")
                or _mentions_pediatric_use(label_text)
            )
            if has_pediatric:
                pediatric_drugs.append(drug)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --extra dev pytest -q tests/medical/test_fda.py tests/medical/test_models_formatters.py tests/test_server_medical.py`
Expected: PASS. The existing `test_search_pediatric_drugs` ("…for children" in dosage), `…_filters_adult_labels` and `…_matches_use_in_specific_populations` still pass.

- [ ] **Step 5: CHANGELOG and commit**

`### Fixed`:

```markdown
- **`search_pediatric_drugs` admits pediatric labels only**: the filter substring-matched `"child"`, so every OTC label passed on "Keep out of reach of children", and so did adult labels mentioning "childbearing". Any boxed warning also counted. It now matches whole words after removing the OTC child-safety line, and a boxed warning counts only when it names a pediatric population. Cache key `pediatric_drugs:v2:`.
```

```bash
git add src/scholar_mcp/medical/fda.py tests/medical/test_fda.py CHANGELOG.md
git commit -m "fix(fda): match pediatric use by whole word, not OTC boilerplate"
```

---

### Task 7: Full verification and zimqa pickup

**Files:** none new. zimqa: `uv.lock` (upgrade only).

- [ ] **Step 1: Full suite and import check (scholar-mcp worktree)**

Run: `uv run --extra dev pytest -q && uv run python -c "from scholar_mcp.server import main; print('Import OK')"`
Expected: all pass, then `Import OK`. The combined prototype of Tasks 1, 2, 4 and 6 broke only the tests this plan rewrites or replaces.

- [ ] **Step 2: Live smoke of finding 1 (network)**

```bash
uv run python - <<'EOF'
import asyncio
from scholar_mcp.resolver import WaterfallResolver

async def main():
    papers = await WaterfallResolver().search(query="dengue warning signs fluid management", num_results=5)
    for p in papers:
        print(p.source, len(p.abstract), p.issn, (p.ranking_metrics or {}).get("raw_impact"), p.title[:60])
    assert any(p.source == "pubmed" and p.abstract for p in papers), "no PubMed abstract"

asyncio.run(main())
EOF
```

Expected: PubMed rows print a non-zero abstract length, and a non-zero `raw_impact` wherever the ISSN is in the SJR table (findings 1 and 4 live).

- [ ] **Step 3: Push and open the PR**

```bash
PLAN=docs/superpowers/plans/2026-09-24-review-findings-ranking-and-filters.md
{ sed -n '/^## Verification of the review findings/,/^## Global Constraints/p' "$PLAN" | sed '$d'
  sed -n '/^## Measurements/,$p' "$PLAN"; } > /tmp/pr-body.md
git push -u origin fix/review-findings-2026-09-24
gh pr create --title "fix: review findings — PubMed abstracts, journal filters, Brazil ranking, SJR data, FDA pediatric filter" \
  --body-file /tmp/pr-body.md
```

- [ ] **Step 4: zimqa pickup (after the PR merges to `main`)**

In `/Users/gus/Git/zimqa`:

```bash
uv lock --upgrade-package scholar-mcp && uv sync
uv run python - <<'EOF'
from zimqa.config import Settings
from zimqa.scholar import ScholarAdapter
hits = ScholarAdapter(Settings()).search("dengue warning signs fluid management", 5)
print([(h.get("source"), len(h["abstract_snippet"])) for h in hits])
assert any(h["abstract_snippet"] for h in hits if h.get("source") == "pubmed")
EOF
```

Expected: PubMed hits carry non-empty `abstract_snippet`s. `Settings()` defaults are enough: `ScholarAdapter` reads only `scholar_timeout_s` and `scholar_num_results`. Commit `uv.lock` in zimqa: `git add uv.lock && git commit -m "chore(deps): pick up scholar-mcp review-findings fixes"`.

---

## Out of scope (observed during verification; follow-ups, not in this plan)

- **Body tier (3b)** is kept by decision. Revisit only with the Task 4 Step 5 evidence.
- **`medical/pubmed.py:parse_pubmed_xml`** (the medical PubMed path) reads `citation.find("Year")`. That is `DateCompleted`/`DateRevised`, which precede `PubDate` in EFetch XML, so every medical-path article carries its indexing year into recency. It also scans the whole record for `ArticleId` DOI/PMC ids, which is the reference-list bug `_own_article_id` fixed on the scholar path. A candidate follow-up is to route it through `PubMedProvider._parse_record`.
- **`pediatric_use` presence** still admits most prescription labels: Pediatric Use (8.4) is a required PLR subsection, even when it says safety "has not been established" `[INFERENCE: not measured on live openFDA data]`.
- **`DrugLabel.pediatric_warnings`** holds the whole boxed warning, and `formatters.py:58-59,119-122` renders it under the heading "Pediatric Warnings".
- **`scripts/probe_enamed_queries.py:30`** imports `scholar_mcp.medical.query_relax`, which moved to `scholar_mcp/query_relax.py`. The probe fails at import.
- **PCDT catalog years**: the crawler records none. Backfilling real years from the PCDT pages would replace Task 4's neutral-recency stand-in with data.

## Measurements

### Baseline (Task 3, before Task 4)

_Paste `/tmp/brazil-probe-before.txt` here._

### After Task 4

_Paste `/tmp/brazil-probe-after.txt` here, with the exit code and the Step 5 decision._
