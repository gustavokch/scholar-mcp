import re
from typing import Any
from bs4 import BeautifulSoup

from scholar_mcp.models import PaperMetadata, ReferenceItem
from scholar_mcp.utils.http import AsyncHttpClient


CROSSREF_BASE = "https://api.crossref.org/works"


def _clean_abstract(raw: str) -> str:
    if not raw:
        return ""
    if "<" in raw and ">" in raw:
        try:
            soup = BeautifulSoup(raw, "html.parser")
            return soup.get_text(" ", strip=True)
        except Exception:
            return re.sub(r"<[^>]+>", "", raw).strip()
    return raw.strip()


class CrossRefProvider:
    """CrossRef search and metadata provider."""

    def __init__(self, http_client: AsyncHttpClient) -> None:
        self.http_client = http_client

    async def search(
        self,
        query: str,
        num_results: int = 10,
        author: str | None = None,
        journal: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
    ) -> list[PaperMetadata]:
        params: dict[str, Any] = {
            "query.bibliographic": query.strip(),
            "rows": min(num_results, 50),
        }
        if author:
            params["query.author"] = author.strip()
        if journal:
            params["query.container-title"] = journal.strip()

        filters: list[str] = []
        if year_start:
            filters.append(f"from-pub-date:{year_start}-01-01")
        if year_end:
            filters.append(f"until-pub-date:{year_end}-12-31")
        if filters:
            params["filter"] = ",".join(filters)

        try:
            resp = await self.http_client.get(CROSSREF_BASE, params=params)
            if resp is None or resp.status_code != 200:
                return []

            data = resp.json()
            items = data.get("message", {}).get("items", [])
            papers: list[PaperMetadata] = []

            for item in items:
                doi = item.get("DOI")
                titles = item.get("title", [])
                title = titles[0] if isinstance(titles, list) and titles else str(item.get("title") or "")
                title = title.rstrip(".")

                authors: list[str] = []
                for a in item.get("author", []):
                    if isinstance(a, dict):
                        given = a.get("given", "")
                        family = a.get("family", "")
                        name = f"{given} {family}".strip() or a.get("name", "")
                        if name:
                            authors.append(name)

                year = ""
                issued = item.get("issued", {}).get("date-parts", [])
                if issued and isinstance(issued, list) and issued[0] and issued[0][0]:
                    year = str(issued[0][0])

                venues = item.get("container-title", [])
                venue = venues[0] if isinstance(venues, list) and venues else str(item.get("container-title") or "")

                abstract = _clean_abstract(item.get("abstract", ""))

                papers.append(
                    PaperMetadata(
                        title=title,
                        authors=authors,
                        year=year,
                        venue=venue,
                        doi=doi,
                        pmid=None,
                        pmcid=None,
                        abstract=abstract,
                        oa_status="unknown",
                    )
                )

            return papers
        except Exception:
            return []

    async def fetch_metadata(self, doi: str) -> PaperMetadata | None:
        clean_doi = doi.strip()
        url = f"{CROSSREF_BASE}/{clean_doi}"
        try:
            resp = await self.http_client.get(url)
            if resp is None or resp.status_code != 200:
                return None

            data = resp.json()
            item = data.get("message", {})
            if not item:
                return None

            titles = item.get("title", [])
            title = titles[0] if isinstance(titles, list) and titles else str(item.get("title") or "")
            title = title.rstrip(".")

            authors: list[str] = []
            for a in item.get("author", []):
                if isinstance(a, dict):
                    given = a.get("given", "")
                    family = a.get("family", "")
                    name = f"{given} {family}".strip() or a.get("name", "")
                    if name:
                        authors.append(name)

            year = ""
            issued = item.get("issued", {}).get("date-parts", [])
            if issued and isinstance(issued, list) and issued[0] and issued[0][0]:
                year = str(issued[0][0])

            venues = item.get("container-title", [])
            venue = venues[0] if isinstance(venues, list) and venues else str(item.get("container-title") or "")

            abstract = _clean_abstract(item.get("abstract", ""))

            return PaperMetadata(
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                doi=item.get("DOI") or clean_doi,
                abstract=abstract,
                oa_status="unknown",
            )
        except Exception:
            return None

    async def fetch_references(
        self,
        doi: str,
        limit: int = 50,
    ) -> list[ReferenceItem]:
        clean_doi = doi.strip()
        url = f"{CROSSREF_BASE}/{clean_doi}"
        try:
            resp = await self.http_client.get(url)
            if resp is None or resp.status_code != 200:
                return []

            data = resp.json()
            items = data.get("message", {}).get("reference", [])
            references: list[ReferenceItem] = []

            for r in items:
                authors: list[str] = []
                author = r.get("author")
                if author:
                    authors = [author.strip()]

                title = r.get("article-title") or r.get("volume-title") or ""
                year = str(r.get("year") or "")
                venue = r.get("journal-title") or ""
                ref_doi = r.get("DOI")

                references.append(
                    ReferenceItem(
                        id=str(r.get("key") or ""),
                        title=title.rstrip("."),
                        authors=authors,
                        year=year,
                        venue=venue,
                        doi=ref_doi,
                        pmid=None,
                        raw_text=r.get("unstructured") or "",
                    )
                )

            return references[:limit]
        except Exception:
            return []

