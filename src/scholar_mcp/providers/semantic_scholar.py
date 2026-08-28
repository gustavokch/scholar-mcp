from typing import Any
import urllib.parse

from scholar_mcp.models import PaperMetadata, RelatedPaper
from scholar_mcp.utils.http import AsyncHttpClient

S2_BASE = "https://api.semanticscholar.org/graph/v1"
S2_RECS_BASE = "https://api.semanticscholar.org/recommendations/v1"

PAPER_FIELDS = "title,authors,year,venue,abstract,externalIds,citationCount,openAccessPdf"


def _extract_authors(authors_raw: Any) -> list[str]:
    if not isinstance(authors_raw, list):
        return []
    authors: list[str] = []
    for a in authors_raw:
        if isinstance(a, dict):
            name = a.get("name")
            if isinstance(name, str) and name.strip():
                authors.append(name.strip())
        elif isinstance(a, str) and a.strip():
            authors.append(a.strip())
    return authors


def _paper_to_metadata(p: dict[str, Any]) -> PaperMetadata:
    ext = p.get("externalIds") or {}
    oa_pdf = (p.get("openAccessPdf") or {}).get("url")
    return PaperMetadata(
        title=p.get("title") or "",
        authors=_extract_authors(p.get("authors")),
        year=str(p.get("year") or ""),
        venue=p.get("venue") or "",
        doi=ext.get("DOI"),
        abstract=p.get("abstract") or "",
        oa_status="oa" if oa_pdf else "unknown",
        citation_count=p.get("citationCount"),
        oa_url=oa_pdf,
    )


class SemanticScholarProvider:
    """Semantic Scholar topic search and embedding-based recommendations."""

    def __init__(self, http_client: AsyncHttpClient, api_key: str | None = None) -> None:
        self.http_client = http_client
        self.api_key = api_key

    def _headers(self) -> dict[str, str] | None:
        return {"x-api-key": self.api_key} if self.api_key else None

    async def search(
        self,
        query: str,
        num_results: int = 10,
        author: str | None = None,
        journal: str | None = None,
        year_start: int | None = None,
        year_end: int | None = None,
    ) -> list[PaperMetadata]:
        # S2 graph search has no author/journal filter, so the caller's constraint
        # is applied to the returned page instead of being silently dropped.
        params: dict[str, Any] = {
            "query": query,
            "limit": min(max(1, num_results), 100),
            "fields": PAPER_FIELDS,
        }
        if year_start or year_end:
            params["year"] = f"{year_start or ''}-{year_end or ''}"

        try:
            resp = await self.http_client.get(
                f"{S2_BASE}/paper/search", params=params, headers=self._headers()
            )
            if resp is None or resp.status_code != 200:
                return []
            papers = [_paper_to_metadata(p) for p in resp.json().get("data", [])]
        except Exception:
            return []

        if author:
            needle = author.lower()
            papers = [p for p in papers if any(needle in a.lower() for a in p.authors)]
        if journal:
            needle = journal.lower()
            papers = [p for p in papers if needle in p.venue.lower()]
        return papers

    async def fetch_recommendations(
        self, paper_id: str, limit: int = 10
    ) -> list[RelatedPaper]:
        try:
            quoted_id = urllib.parse.quote(paper_id, safe="")
            resp = await self.http_client.get(
                f"{S2_RECS_BASE}/papers/forpaper/{quoted_id}",
                params={"limit": min(max(1, limit), 100), "fields": PAPER_FIELDS},
                headers=self._headers(),
            )
            if resp is None or resp.status_code != 200:
                return []
            recs: list[RelatedPaper] = []
            for p in resp.json().get("recommendedPapers", []):
                ext = p.get("externalIds") or {}
                recs.append(
                    RelatedPaper(
                        title=p.get("title") or "",
                        authors=_extract_authors(p.get("authors")),
                        year=str(p.get("year") or ""),
                        venue=p.get("venue") or "",
                        doi=ext.get("DOI"),
                    )
                )
            return recs[:limit]
        except Exception:
            return []
