from typing import Any
import urllib.parse

from scholar_mcp.models import CitationItem, PaperMetadata
from scholar_mcp.utils.http import AsyncHttpClient

OPENALEX_BASE = "https://api.openalex.org"


def _reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        for i in idxs:
            positions[i] = word
    return " ".join(positions[i] for i in sorted(positions))


def _strip_doi_url(doi_url: str | None) -> str | None:
    if not doi_url:
        return None
    return doi_url.replace("https://doi.org/", "")


class OpenAlexProvider:
    """OpenAlex metadata, citation counts, OA URLs, institutions, citing works."""

    def __init__(self, http_client: AsyncHttpClient, email: str | None = None) -> None:
        self.http_client = http_client
        self.email = email

    def _params(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = dict(extra or {})
        if self.email:
            params["mailto"] = self.email
        return params

    async def _get_work(self, doi: str) -> dict[str, Any] | None:
        url = f"{OPENALEX_BASE}/works/https://doi.org/{urllib.parse.quote(doi, safe='')}"
        resp = await self.http_client.get(url, params=self._params())
        if resp is None or resp.status_code != 200:
            return None
        return resp.json()

    async def fetch_metadata(self, doi: str) -> PaperMetadata | None:
        try:
            work = await self._get_work(doi)
            if not work:
                return None

            authors: list[str] = []
            institutions: list[str] = []
            for a in work.get("authorships", []):
                name = (a.get("author") or {}).get("display_name")
                if name:
                    authors.append(name)
                for inst in a.get("institutions", []):
                    inst_name = inst.get("display_name")
                    if inst_name and inst_name not in institutions:
                        institutions.append(inst_name)

            oa = work.get("open_access") or {}
            source = (work.get("primary_location") or {}).get("source") or {}

            return PaperMetadata(
                title=work.get("title") or "",
                authors=authors,
                year=str(work.get("publication_year") or ""),
                venue=source.get("display_name") or "",
                doi=_strip_doi_url(work.get("doi")) or doi,
                abstract=_reconstruct_abstract(work.get("abstract_inverted_index")),
                oa_status="oa" if oa.get("is_oa") else "closed",
                citation_count=work.get("cited_by_count"),
                oa_url=oa.get("oa_url"),
                institutions=institutions,
            )
        except Exception:
            return None

    async def fetch_citations(self, doi: str, limit: int = 50) -> list[CitationItem]:
        try:
            work = await self._get_work(doi)
            if not work or not work.get("id"):
                return []
            openalex_id = work["id"].rsplit("/", 1)[-1]
            resp = await self.http_client.get(
                f"{OPENALEX_BASE}/works",
                params=self._params({
                    "filter": f"cites:{openalex_id}",
                    "per-page": min(max(1, limit), 100),
                }),
            )
            if resp is None or resp.status_code != 200:
                return []

            citations: list[CitationItem] = []
            for w in resp.json().get("results", []):
                authors = [
                    (a.get("author") or {}).get("display_name")
                    for a in w.get("authorships", [])
                ]
                source = (w.get("primary_location") or {}).get("source") or {}
                citations.append(
                    CitationItem(
                        title=w.get("title") or "",
                        authors=[a for a in authors if a],
                        year=str(w.get("publication_year") or ""),
                        venue=source.get("display_name") or "",
                        doi=_strip_doi_url(w.get("doi")),
                        citation_count=w.get("cited_by_count"),
                    )
                )
            return citations[:limit]
        except Exception:
            return []
