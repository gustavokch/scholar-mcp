import re
from typing import Any
from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap, PaperMetadata, RelatedPaper
from scholar_mcp.providers.base import failure_reason
from scholar_mcp.query_relax import MAX_RELAX_EXTRA_CALLS, relax_ladder
from scholar_mcp.ranking import classify_evidence_grade
from scholar_mcp.utils.ctxstate import ContextScoped
from scholar_mcp.utils.http import AsyncHttpClient

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
ELINK_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"



class PubMedProvider:
    """PubMed discovery and abstract provider via NCBI E-utilities."""

    # Set to a short reason immediately before failure returns in search();
    # None after success or a genuine empty result. Read by the resolver's
    # per-source degradation map. Context-scoped so one request's failure is
    # invisible to a concurrent request sharing this singleton provider.
    last_error: str | None = ContextScoped(lambda: None)

    # Set to the relaxed variant that produced hits when the full query
    # returned none and relax=True walked the ladder; None when the full
    # query answered directly or relax=False. The resolver reads this so
    # search_papers can tell the caller it answered a different question
    # than the one asked (finding 3: relax=True with no metadata channel
    # made a relaxed answer indistinguishable from an exact one).
    last_relaxed_query: str | None = ContextScoped(lambda: None)

    def __init__(self, http_client: AsyncHttpClient, settings: Settings | None = None) -> None:
        self.http_client = http_client
        self.settings = settings or Settings.load()

    def _base_params(self) -> dict[str, Any]:
        """NCBI credentials for E-utilities.

        Missing until now: without an api_key NCBI serves 2.8 req/s, with
        one 9 req/s (Settings.ncbi_rate_limit, already honored by the shared
        http client). Only set keys are sent, so keyless callers behave as
        before.
        """
        params: dict[str, Any] = {}
        if self.settings.pubmed_api_key:
            params["api_key"] = self.settings.pubmed_api_key
        if self.settings.pubmed_email:
            params["email"] = self.settings.pubmed_email
        if self.settings.pubmed_tool:
            params["tool"] = self.settings.pubmed_tool
        return params

    @staticmethod
    def build_query(
        query: str,
        author: str | None = None,
        journal: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
    ) -> str:
        parts: list[str] = [query.strip()]
        if author:
            parts.append(f'"{author.strip()}"[Author]')
        if journal:
            parts.append(f'"{journal.strip()}"[Journal]')
        if year_start or year_end:
            start = str(year_start) if year_start else "1800"
            end = str(year_end) if year_end else "3000"
            parts.append(f'("{start}"[PDAT] : "{end}"[PDAT])')
        return " AND ".join(parts)

    async def search(
        self,
        query: str,
        num_results: int = 10,
        author: str | None = None,
        journal: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
        sort: str = "relevance",
        relax: bool = True,
    ) -> list[PaperMetadata]:
        term = self.build_query(query, author, journal, year_start, year_end)
        self.last_error = None
        self.last_relaxed_query = None
        search_params: dict[str, Any] = {
            **self._base_params(),
            "db": "pubmed",
            "term": term,
            "retmax": min(num_results, 200),
            "retmode": "json",
        }
        if sort in ("pub_date", "date"):
            search_params["sort"] = "pub_date"
        elif sort == "relevance":
            # NCBI's default (no sort param) is most-recent-first; relevance must be explicit.
            search_params["sort"] = "relevance"
        elif sort:
            search_params["sort"] = sort

        try:
            resp = await self.http_client.get(ESEARCH_URL, params=search_params)
            if resp is None or resp.status_code != 200:
                self.last_error = failure_reason(self.http_client, resp=resp)
                return []

            data = resp.json()
            id_list = data.get("esearchresult", {}).get("idlist", [])
            if not id_list and relax:
                # PubMed ANDs every term: a long natural-language query
                # over-constrains esearch to zero hits while a leading-token
                # prefix returns hits. Walk the shared ladder past the
                # already tried full query, rebuilding the caller's filters
                # around each relaxed variant, stopping at the first hit.
                # At most MAX_RELAX_EXTRA_CALLS extra esearch calls; a fetch
                # failure is not a zero-hit and ends the walk.
                for variant in relax_ladder(query)[1 : 1 + MAX_RELAX_EXTRA_CALLS]:
                    step_term = self.build_query(
                        variant, author, journal, year_start, year_end
                    )
                    if step_term == term:
                        continue
                    step_resp = await self.http_client.get(
                        ESEARCH_URL,
                        params={**search_params, "term": step_term},
                    )
                    if step_resp is None or step_resp.status_code != 200:
                        self.last_error = failure_reason(
                            self.http_client, resp=step_resp
                        )
                        return []
                    id_list = step_resp.json().get("esearchresult", {}).get("idlist", [])
                    if id_list:
                        self.last_relaxed_query = variant
                        break
            if not id_list:
                return []

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
        except Exception as exc:
            self.last_error = failure_reason(self.http_client, exc=exc)
            return []

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
        citation = record.find("MedlineCitation", recursive=False) or record.find(
            "BookDocument", recursive=False
        )
        pmid_elem = citation.find("PMID", recursive=False) if citation is not None else None
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

    async def fetch_abstract(self, ids: IdentifierMap) -> PaperMetadata | None:
        """Fetch abstract and metadata for paper via PubMed efetch."""
        pmid = ids.pmid
        if not pmid and ids.doi:
            # Try searching pmid by doi
            try:
                s_resp = await self.http_client.get(
                    ESEARCH_URL,
                    params={
                        **self._base_params(),
                        "db": "pubmed",
                        "term": f'"{ids.doi}"[Location ID]',
                        "retmode": "json",
                    },
                )
                if s_resp and s_resp.status_code == 200:
                    id_list = s_resp.json().get("esearchresult", {}).get("idlist", [])
                    if id_list:
                        pmid = id_list[0]
            except Exception:
                pass

        if not pmid:
            return None

        try:
            resp = await self.http_client.get(
                EFETCH_URL,
                params={
                    **self._base_params(),
                    "db": "pubmed",
                    "id": pmid,
                    "rettype": "xml",
                    "retmode": "xml",
                },
            )
            if resp is None or resp.status_code != 200 or not resp.content:
                return None

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
        except Exception:
            return None

    async def fetch_related_papers(
        self,
        pmid: str,
        limit: int = 10,
    ) -> list[RelatedPaper]:
        clean_pmid = pmid.strip()
        params = {
            **self._base_params(),
            "dbfrom": "pubmed",
            "id": clean_pmid,
            "cmd": "neighbor_score",
            "linkname": "pubmed_pubmed",
            "retmode": "json",
        }
        try:
            resp = await self.http_client.get(ELINK_URL, params=params)
            if resp is None or resp.status_code != 200:
                return []

            data = resp.json()
            linksets = data.get("linksets", [])
            if not linksets:
                return []

            linksetdbs = linksets[0].get("linksetdbs", [])
            if not linksetdbs:
                return []

            links = linksetdbs[0].get("links", [])
            if not links:
                return []

            # Filter out null IDs and the source pmid itself, then limit
            candidate_links = [
                l for l in links
                if l.get("id") is not None and str(l.get("id")) != clean_pmid
            ][:limit]
            if not candidate_links:
                return []

            target_ids = [str(l["id"]) for l in candidate_links]
            scores = {}
            for l in candidate_links:
                raw_score = l.get("score")
                if raw_score is not None:
                    try:
                        scores[str(l["id"])] = float(raw_score) / 1000000.0
                    except Exception:
                        scores[str(l["id"])] = None

            # Fetch metadata via esummary
            summary_params = {
                **self._base_params(),
                "db": "pubmed",
                "id": ",".join(target_ids),
                "retmode": "json",
            }
            sum_resp = await self.http_client.get(ESUMMARY_URL, params=summary_params)
            if sum_resp is None or sum_resp.status_code != 200:
                return []

            sum_data = sum_resp.json()
            results_dict = sum_data.get("result", {})
            related_papers: list[RelatedPaper] = []

            for uid in target_ids:
                rec = results_dict.get(str(uid), {})
                if not rec or not isinstance(rec, dict):
                    continue

                title = rec.get("title", "").rstrip(".")
                authors: list[str] = []
                for a in rec.get("authors", []):
                    if isinstance(a, dict) and a.get("name"):
                        authors.append(a["name"])

                pubdate = rec.get("pubdate", "")
                year_match = re.search(r"\b(19\d\d|20\d\d)\b", pubdate)
                year = year_match.group(1) if year_match else pubdate

                venue = rec.get("fulljournalname") or rec.get("source") or ""

                doi = None
                eloc = rec.get("elocationid", "")
                if "doi:" in eloc.lower():
                    doi = re.sub(r"^doi:\s*", "", eloc, flags=re.IGNORECASE).strip()
                for aid in rec.get("articleids", []):
                    if isinstance(aid, dict) and aid.get("idtype") == "doi":
                        doi = aid.get("value")

                related_papers.append(
                    RelatedPaper(
                        title=title,
                        authors=authors,
                        year=year,
                        venue=venue,
                        doi=doi,
                        pmid=str(uid),
                        score=scores.get(str(uid)),
                    )
                )

            return related_papers
        except Exception:
            return []

