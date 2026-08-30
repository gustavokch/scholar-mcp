import re
from bs4 import BeautifulSoup

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.medical.ranking import rank_medical_articles
from scholar_mcp.utils.deduplication import deduplicate_papers
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


def parse_pubmed_xml(xml_text: str) -> list[MedicalArticle]:
    soup = BeautifulSoup(xml_text, "lxml-xml")
    articles: list[MedicalArticle] = []

    for citation in soup.find_all("PubmedArticle"):
        pmid_elem = citation.find("PMID")
        if pmid_elem is None or not pmid_elem.get_text(strip=True):
            continue
        pmid = pmid_elem.get_text(strip=True)

        title_elem = citation.find("ArticleTitle")
        title = title_elem.get_text(" ", strip=True) if title_elem is not None else ""

        # Extract abstract
        abstract_texts = citation.find_all("AbstractText")
        if abstract_texts:
            abstract = " ".join(t.get_text(" ", strip=True) for t in abstract_texts if t.get_text(strip=True))
        else:
            abstract = ""

        # Extract authors
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

        # Extract journal title
        journal = ""
        journal_elem = citation.find("Journal")
        if journal_elem is not None:
            jt_elem = journal_elem.find("Title") or journal_elem.find("ISOAbbreviation")
            if jt_elem is not None:
                journal = jt_elem.get_text(strip=True)

        # Extract DOI
        doi = None
        eloc = citation.find("ELocationID", EIdType="doi")
        if eloc is not None and eloc.get_text(strip=True):
            doi = eloc.get_text(strip=True)
        else:
            aid_doi = citation.find("ArticleId", IdType="doi")
            if aid_doi is not None and aid_doi.get_text(strip=True):
                doi = aid_doi.get_text(strip=True)

        # Extract PMC ID
        pmc_id = None
        aid_pmc = citation.find("ArticleId", IdType="pmc")
        if aid_pmc is not None and aid_pmc.get_text(strip=True):
            pmc_id = re.sub(r"^PMC", "", aid_pmc.get_text(strip=True), flags=re.IGNORECASE)

        # Extract publication year
        year = ""
        year_elem = citation.find("Year")
        if year_elem is not None and year_elem.get_text(strip=True):
            year = year_elem.get_text(strip=True)
        else:
            medline_date = citation.find("MedlineDate")
            if medline_date is not None and medline_date.get_text(strip=True):
                year_match = re.search(r"\b(19|20)\d{2}\b", medline_date.get_text(strip=True))
                if year_match:
                    year = year_match.group(0)

        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

        articles.append(
            MedicalArticle(
                title=title,
                authors=authors,
                journal=journal,
                year=year,
                abstract=abstract,
                pmid=pmid,
                pmc_id=pmc_id,
                doi=doi,
                url=url,
                source_database="PubMed",
            )
        )

    return articles


class MedicalPubMedClient:
    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    def _base_params(self) -> dict[str, str]:
        params: dict[str, str] = {}
        if self.settings.pubmed_api_key:
            params["api_key"] = self.settings.pubmed_api_key
        if self.settings.pubmed_email:
            params["email"] = self.settings.pubmed_email
        if self.settings.pubmed_tool:
            params["tool"] = self.settings.pubmed_tool
        return params

    async def search_articles(
        self,
        query: str,
        max_results: int = 10,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        cache_key = f"pubmed:search:{query}:{max_results}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [MedicalArticle.from_dict(d) for d in cached_data], meta

        search_params = {
            **self._base_params(),
            "db": "pubmed",
            "term": query,
            "retmode": "json",
            "retmax": str(max_results),
            # NCBI's default (no sort param) is most-recent-first; ask for Best Match.
            "sort": "relevance",
        }

        try:
            resp = await self.http_client.get(ESEARCH_URL, params=search_params)
            data = resp.json()
            idlist = data.get("esearchresult", {}).get("idlist", [])
        except Exception:
            return [], CacheMetadata(cached=False, cache_age=0)

        if not idlist:
            await self.cache.set(cache_key, [], source="pubmed")
            return [], CacheMetadata(cached=False, cache_age=0)

        fetch_params = {
            **self._base_params(),
            "db": "pubmed",
            "id": ",".join(idlist),
            "retmode": "xml",
        }

        try:
            fetch_resp = await self.http_client.get(EFETCH_URL, params=fetch_params)
            articles = parse_pubmed_xml(fetch_resp.text)
        except Exception:
            return [], CacheMetadata(cached=False, cache_age=0)

        # Deduplicate
        deduped_dicts, _ = deduplicate_papers([a.to_dict() for a in articles])
        final_articles = rank_medical_articles(
            [MedicalArticle.from_dict(d) for d in deduped_dicts], query
        )

        await self.cache.set(
            cache_key,
            [a.to_dict() for a in final_articles],
            source="pubmed",
        )
        return final_articles, CacheMetadata(cached=False, cache_age=0)
