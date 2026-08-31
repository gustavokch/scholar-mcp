import asyncio
import re
from typing import Any
import urllib.parse

from scholar_mcp.models import CitationItem, PaperMetadata
from scholar_mcp.utils.http import AsyncHttpClient

OPENALEX_BASE = "https://api.openalex.org"
DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def _reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted or not isinstance(inverted, dict):
        return ""
    positions: dict[int, str] = {}
    for word, idxs in inverted.items():
        if isinstance(idxs, list):
            for i in idxs:
                if isinstance(i, int):
                    positions[i] = str(word)
    return " ".join(positions[i] for i in sorted(positions))


def _strip_doi_url(doi_url: str | None) -> str | None:
    if not doi_url or not isinstance(doi_url, str):
        return None
    cleaned = DOI_PREFIX_RE.sub("", doi_url.strip()).strip()
    return cleaned if cleaned else None


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
        clean_doi = _strip_doi_url(doi) or doi.strip()
        url = f"{OPENALEX_BASE}/works/https://doi.org/{urllib.parse.quote(clean_doi, safe='')}"
        resp = await self.http_client.get(url, params=self._params())
        if resp is None or resp.status_code != 200:
            return None
        return resp.json()

    async def fetch_metadata(self, doi: str) -> PaperMetadata | None:
        try:
            work = await self._get_work(doi)
            if not work or not isinstance(work, dict):
                return None

            authors: list[str] = []
            institutions: list[str] = []
            authorships = work.get("authorships")
            if isinstance(authorships, list):
                for a in authorships:
                    if not isinstance(a, dict):
                        continue
                    author_obj = a.get("author")
                    if isinstance(author_obj, dict):
                        name = author_obj.get("display_name")
                        if isinstance(name, str) and name.strip():
                            authors.append(name.strip())
                    elif isinstance(a.get("display_name"), str):
                        authors.append(a["display_name"].strip())

                    insts = a.get("institutions")
                    if isinstance(insts, list):
                        for inst in insts:
                            if isinstance(inst, dict):
                                inst_name = inst.get("display_name")
                                if isinstance(inst_name, str) and inst_name.strip():
                                    trimmed = inst_name.strip()
                                    if trimmed not in institutions:
                                        institutions.append(trimmed)

            oa = work.get("open_access") if isinstance(work.get("open_access"), dict) else {}
            loc = work.get("primary_location") if isinstance(work.get("primary_location"), dict) else {}
            source = loc.get("source") if isinstance(loc.get("source"), dict) else {}

            clean_doi = _strip_doi_url(doi) or doi.strip()
            return PaperMetadata(
                title=work.get("title") or "",
                authors=authors,
                year=str(work.get("publication_year") or ""),
                venue=source.get("display_name") or "",
                doi=_strip_doi_url(work.get("doi")) or clean_doi,
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
            if not work or not isinstance(work, dict) or not work.get("id"):
                return []
            openalex_id = str(work["id"]).rsplit("/", 1)[-1]
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
            results = resp.json().get("results", []) if isinstance(resp.json(), dict) else []
            for w in results:
                if not isinstance(w, dict):
                    continue
                authors = []
                authorships = w.get("authorships")
                if isinstance(authorships, list):
                    for a in authorships:
                        if isinstance(a, dict):
                            author_obj = a.get("author")
                            if isinstance(author_obj, dict) and author_obj.get("display_name"):
                                authors.append(author_obj["display_name"])
                            elif isinstance(a.get("display_name"), str):
                                authors.append(a["display_name"])

                loc = w.get("primary_location") if isinstance(w.get("primary_location"), dict) else {}
                source = loc.get("source") if isinstance(loc.get("source"), dict) else {}
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

    async def _fetch_works_raw(self, filter_str: str) -> list[dict[str, Any]]:
        params = self._params({"filter": filter_str, "per-page": 50})
        try:
            resp = await self.http_client.get(f"{OPENALEX_BASE}/works", params=params)
            if resp is None or resp.status_code != 200:
                return []
            data = resp.json()
            results = data.get("results", [])
            return [w for w in results if isinstance(w, dict)]
        except Exception:
            return []

    async def _fetch_citation_counts_single_filter(
        self,
        filter_str: str,
    ) -> dict[str, int]:
        works = await self._fetch_works_raw(filter_str)
        counts: dict[str, int] = {}
        for work in works:
            c = work.get("cited_by_count")
            if c is None or not isinstance(c, int):
                continue

            # Map work DOI
            w_doi = _strip_doi_url(work.get("doi"))
            if w_doi:
                counts[w_doi.lower()] = c

            # Map work PMID and other IDs
            ids_dict = work.get("ids", {})
            if isinstance(ids_dict, dict):
                raw_pmid = ids_dict.get("pmid")
                if raw_pmid:
                    pmid_val = str(raw_pmid).split("/")[-1].strip()
                    if pmid_val:
                        counts[pmid_val] = c
                raw_doi = ids_dict.get("doi")
                if raw_doi:
                    d_val = _strip_doi_url(str(raw_doi))
                    if d_val:
                        counts[d_val.lower()] = c

        return counts

    async def fetch_citation_counts_batch(
        self,
        dois: list[str] | None = None,
        pmids: list[str] | None = None,
    ) -> dict[str, int]:
        """Fetch citation counts for multiple DOIs and PMIDs via batched OpenAlex query."""
        clean_dois = [
            _strip_doi_url(d) or d.strip()
            for d in (dois or [])
            if d and (_strip_doi_url(d) or d.strip())
        ]
        clean_pmids = [p.strip() for p in (pmids or []) if p and p.strip()]

        if not clean_dois and not clean_pmids:
            return {}

        tasks = []
        if clean_dois:
            filter_str = f"doi:{'|'.join(clean_dois[:50])}"
            tasks.append(self._fetch_citation_counts_single_filter(filter_str))
        if clean_pmids:
            filter_str = f"pmid:{'|'.join(clean_pmids[:50])}"
            tasks.append(self._fetch_citation_counts_single_filter(filter_str))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        combined: dict[str, int] = {}
        for res in results:
            if isinstance(res, dict):
                combined.update(res)
        return combined

    async def _fetch_work_details_single_filter(
        self,
        filter_str: str,
    ) -> dict[str, dict[str, Any]]:
        works = await self._fetch_works_raw(filter_str)
        details: dict[str, dict[str, Any]] = {}
        for work in works:
            c = work.get("cited_by_count")
            if c is None or not isinstance(c, int):
                continue

            last_author_id = None
            authorships = work.get("authorships")
            if isinstance(authorships, list) and authorships:
                last = authorships[-1]
                if isinstance(last, dict):
                    author_obj = last.get("author")
                    if isinstance(author_obj, dict):
                        raw_id = author_obj.get("id")
                        if raw_id:
                            last_author_id = str(raw_id).rsplit("/", 1)[-1]

            entry = {"citation_count": c, "last_author_id": last_author_id}

            # Map work DOI
            w_doi = _strip_doi_url(work.get("doi"))
            if w_doi:
                details[w_doi.lower()] = entry

            # Map work PMID and other IDs
            ids_dict = work.get("ids", {})
            if isinstance(ids_dict, dict):
                raw_pmid = ids_dict.get("pmid")
                if raw_pmid:
                    pmid_val = str(raw_pmid).split("/")[-1].strip()
                    if pmid_val:
                        details[pmid_val] = entry
                raw_doi = ids_dict.get("doi")
                if raw_doi:
                    d_val = _strip_doi_url(str(raw_doi))
                    if d_val:
                        details[d_val.lower()] = entry

        return details

    async def fetch_work_details_batch(
        self,
        dois: list[str] | None = None,
        pmids: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch citation count and last-author OpenAlex ID for multiple DOIs/PMIDs."""
        clean_dois = [
            _strip_doi_url(d) or d.strip()
            for d in (dois or [])
            if d and (_strip_doi_url(d) or d.strip())
        ]
        clean_pmids = [p.strip() for p in (pmids or []) if p and p.strip()]

        if not clean_dois and not clean_pmids:
            return {}

        tasks = []
        if clean_dois:
            tasks.append(self._fetch_work_details_single_filter(f"doi:{'|'.join(clean_dois[:50])}"))
        if clean_pmids:
            tasks.append(self._fetch_work_details_single_filter(f"pmid:{'|'.join(clean_pmids[:50])}"))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        combined: dict[str, dict[str, Any]] = {}
        for res in results:
            if isinstance(res, dict):
                combined.update(res)
        return combined

    async def fetch_author_h_indices_batch(self, author_ids: list[str]) -> dict[str, int]:
        """Fetch h-index for multiple OpenAlex author IDs via a batched query."""
        clean_ids = [a.strip() for a in (author_ids or []) if a and a.strip()]
        if not clean_ids:
            return {}

        params = self._params({
            "filter": f"openalex_id:{'|'.join(clean_ids[:50])}",
            "per-page": 50,
        })
        try:
            resp = await self.http_client.get(f"{OPENALEX_BASE}/authors", params=params)
            if resp is None or resp.status_code != 200:
                return {}

            data = resp.json()
            results = data.get("results", [])
            h_indices: dict[str, int] = {}
            for author in results:
                if not isinstance(author, dict):
                    continue
                raw_id = author.get("id")
                stats = author.get("summary_stats")
                if raw_id and isinstance(stats, dict):
                    h_index = stats.get("h_index")
                    if isinstance(h_index, int):
                        bare_id = str(raw_id).rsplit("/", 1)[-1]
                        h_indices[bare_id] = h_index
            return h_indices
        except Exception:
            return {}

