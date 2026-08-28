import re
from typing import Any
from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.models import IdentifierMap, PaperMetadata
from scholar_mcp.utils.http import AsyncHttpClient

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ESUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class PubMedProvider:
    """PubMed discovery and abstract provider via NCBI E-utilities."""

    def __init__(self, http_client: AsyncHttpClient, settings: Settings | None = None) -> None:
        self.http_client = http_client
        self.settings = settings or Settings.load()

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
    ) -> list[PaperMetadata]:
        term = self.build_query(query, author, journal, year_start, year_end)
        search_params = {
            "db": "pubmed",
            "term": term,
            "retmax": min(num_results, 50),
            "retmode": "json",
            "sort": "pub_date",
        }

        try:
            resp = await self.http_client.get(ESEARCH_URL, params=search_params)
            if resp is None or resp.status_code != 200:
                return []

            data = resp.json()
            id_list = data.get("esearchresult", {}).get("idlist", [])
            if not id_list:
                return []

            summary_params = {
                "db": "pubmed",
                "id": ",".join(id_list),
                "retmode": "json",
            }
            sum_resp = await self.http_client.get(ESUMMARY_URL, params=summary_params)
            if sum_resp is None or sum_resp.status_code != 200:
                return []

            sum_data = sum_resp.json()
            results_dict = sum_data.get("result", {})
            uids = results_dict.get("uids", id_list)

            papers: list[PaperMetadata] = []
            for uid in uids:
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
                # Check elocationid
                eloc = rec.get("elocationid", "")
                if "doi:" in eloc.lower():
                    doi = re.sub(r"^doi:\s*", "", eloc, flags=re.IGNORECASE).strip()
                # Check articleids
                for aid in rec.get("articleids", []):
                    if isinstance(aid, dict) and aid.get("idtype") == "doi":
                        doi = aid.get("value")

                papers.append(
                    PaperMetadata(
                        title=title,
                        authors=authors,
                        year=year,
                        venue=venue,
                        doi=doi,
                        pmid=str(uid),
                        abstract="",
                        oa_status="unknown",
                    )
                )

            return papers
        except Exception:
            return []

    async def fetch_abstract(self, ids: IdentifierMap) -> PaperMetadata | None:
        """Fetch abstract and metadata for paper via PubMed efetch."""
        pmid = ids.pmid
        if not pmid and ids.doi:
            # Try searching pmid by doi
            try:
                s_resp = await self.http_client.get(
                    ESEARCH_URL,
                    params={"db": "pubmed", "term": f'"{ids.doi}"[Location ID]', "retmode": "json"},
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
                params={"db": "pubmed", "id": pmid, "rettype": "xml", "retmode": "xml"},
            )
            if resp is None or resp.status_code != 200 or not resp.content:
                return None

            soup = BeautifulSoup(resp.content, "lxml-xml")
            article = soup.find("PubmedArticle")
            if article is None:
                return None

            title_elem = article.find("ArticleTitle")
            title = title_elem.get_text(" ", strip=True) if title_elem is not None else ""

            abstract_elem = article.find("Abstract")
            abstract = ""
            if abstract_elem is not None:
                abstract_texts = [
                    p.get_text(" ", strip=True)
                    for p in abstract_elem.find_all("AbstractText")
                ]
                abstract = "\n\n".join(t for t in abstract_texts if t)

            authors: list[str] = []
            author_list = article.find("AuthorList")
            if author_list is not None:
                for author in author_list.find_all("Author"):
                    last = author.find("LastName")
                    fore = author.find("ForeName") or author.find("Initials")
                    last_str = last.get_text(" ", strip=True) if last is not None else ""
                    fore_str = fore.get_text(" ", strip=True) if fore is not None else ""
                    full = f"{fore_str} {last_str}".strip()
                    if full:
                        authors.append(full)

            year = ""
            pub_date = article.find("PubDate")
            if pub_date is not None:
                year_elem = pub_date.find("Year")
                if year_elem is not None:
                    year = year_elem.get_text(" ", strip=True)

            venue = ""
            journal = article.find("Journal")
            if journal is not None:
                journal_elem = journal.find("Title")
                if journal_elem is not None:
                    venue = journal_elem.get_text(" ", strip=True)

            doi = ids.doi
            for aid in article.find_all("ArticleId"):
                if aid.get("IdType") == "doi":
                    doi = aid.get_text(" ", strip=True)

            return PaperMetadata(
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                doi=doi,
                pmid=pmid,
                pmcid=ids.pmcid,
                abstract=abstract,
                oa_status="unknown",
            )
        except Exception:
            return None
