import re
from typing import Any

from scholar_mcp.models import CitationItem, FullTextResponse, IdentifierMap, PaperMetadata, ReferenceItem
from scholar_mcp.parsers.jats import jats_to_markdown, list_sections

from scholar_mcp.providers.base import BaseProvider, MIN_USEFUL_CHARS
from scholar_mcp.utils.http import AsyncHttpClient, RETRYABLE_STATUS_CODES

EPMC_REST_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
OAI_PMH_URL = "https://pmc.ncbi.nlm.nih.gov/api/oai/v1/mh/"
OAI_QUIET = frozenset({400, 404})

# EPMC_XML_QUIET and EPMC_XML_RETRYABLE travel together as one policy for fullTextXML.
EPMC_XML_QUIET = frozenset({404, 500})
EPMC_XML_RETRYABLE = RETRYABLE_STATUS_CODES - {500}


class EuropePMCProvider(BaseProvider):
    """Europe PMC open-access provider with JATS XML support."""

    tier: str = "europepmc"

    def __init__(self, http_client: AsyncHttpClient) -> None:
        super().__init__(http_client)

    async def fetch_metadata(self, ids: IdentifierMap) -> PaperMetadata | None:
        """Fetch core metadata by PMID. Returns None on any miss or failure.

        PubMed efetch is the primary PMID metadata source, but its failure
        (transport error, empty efetch) is indistinguishable from an absent
        record. Europe PMC search by EXT_ID is the fallback that keeps a
        transient PubMed failure from surfacing as not_found.
        """
        if not ids.pmid:
            return None
        try:
            resp = await self.http_client.get(
                f"{EPMC_REST_BASE}/search",
                params={
                    "query": f"EXT_ID:{ids.pmid} AND SRC:MED",
                    "format": "json",
                    "resultType": "core",
                },
            )
            if resp is None or resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("resultList", {}).get("result", [])
            if not results:
                return None
            rec = results[0]
            authors: list[str] = []
            author_str = rec.get("authorString", "")
            if author_str:
                authors = [a.strip() for a in author_str.split(",") if a.strip()]
            return PaperMetadata(
                title=rec.get("title", "").rstrip("."),
                authors=authors,
                year=str(rec.get("pubYear") or ""),
                venue=rec.get("journalTitle") or "",
                doi=rec.get("doi"),
                pmid=str(rec.get("pmid") or ids.pmid),
                pmcid=rec.get("pmcid"),
                abstract=rec.get("abstractText") or "",
            )
        except Exception:
            return None

    async def _fetch_full_text_xml(
        self, pmcid: str, ids: IdentifierMap, pmid: str | None = None
    ) -> FullTextResponse | None:
        """Fetch and parse JATS XML from Europe PMC fullTextXML endpoint."""
        if not pmcid.upper().startswith("PMC"):
            pmcid = f"PMC{pmcid}"
        url = f"{EPMC_REST_BASE}/{pmcid}/fullTextXML"
        resp = await self.http_client.get(
            url,
            quiet_statuses=EPMC_XML_QUIET,
            retryable_statuses=EPMC_XML_RETRYABLE,
        )
        if resp is not None and resp.status_code == 200 and resp.content:
            md = jats_to_markdown(resp.content)
            if len(md.strip()) >= MIN_USEFUL_CHARS:
                return FullTextResponse(
                    status="full_text",
                    source="europepmc",
                    format="markdown",
                    content=md,
                    total_chars=len(md),
                    sections_available=list_sections(md),
                    doi=ids.doi,
                    pmid=pmid or ids.pmid,
                    pmcid=pmcid,
                    url=f"https://europepmc.org/article/PMC/{pmcid}",
                )
        return None

    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        self.last_skip_reason = ""
        pmcid = ids.pmcid
        if pmcid:
            if not pmcid.upper().startswith("PMC"):
                pmcid = f"PMC{pmcid}"
            try:
                res = await self._fetch_full_text_xml(pmcid, ids)
                if res is not None:
                    return res
            except Exception:
                pass
            # Spec §4 Investigate 3 fallback: a fullTextXML 404 (the run-9
            # PMC11390030 case) can still carry OA XML through PMC OAI-PMH.
            oai = await self._fetch_via_oai(pmcid, ids)
            if oai is not None:
                return oai

        # If no PMCID or PMCID XML failed, try resolving via DOI on Europe PMC Search
        if ids.doi:
            try:
                search_url = f"{EPMC_REST_BASE}/search"
                resp = await self.http_client.get(
                    search_url,
                    params={
                        "query": f'DOI:"{ids.doi}"',
                        "format": "json",
                        "resultType": "core",
                    },
                )
                if resp is not None and resp.status_code == 200:
                    data = resp.json()
                    results = data.get("resultList", {}).get("result", [])
                    if results:
                        rec = results[0]
                        found_pmcid = rec.get("pmcid")
                        has_xml = rec.get("hasXML") == "Y" or rec.get("isOpenAccess") == "Y"
                        if found_pmcid and has_xml:
                            if not found_pmcid.upper().startswith("PMC"):
                                found_pmcid = f"PMC{found_pmcid}"
                            full_text = await self._fetch_full_text_xml(
                                found_pmcid, ids, pmid=ids.pmid or rec.get("pmid")
                            )
                            if full_text is not None:
                                return full_text
                            oai = await self._fetch_via_oai(found_pmcid, ids)
                            if oai is not None:
                                return oai
            except Exception:
                pass

        # Terminal miss: both XML routes (and the OAI fallback where a PMCID
        # was known) came up empty. Tell the waterfall this was a deliberate,
        # reasoned skip rather than an empty-reason miss.
        self.last_skip_reason = "EUROPEPMC_FULLTEXT_UNAVAILABLE"
        return None

    async def _fetch_via_oai(
        self, pmcid: str, ids: IdentifierMap
    ) -> FullTextResponse | None:
        """Fetch JATS through PMC OAI-PMH GetRecord for a fullTextXML 404.

        The response wraps the article in
        ``<OAI-PMH><GetRecord><record><metadata>``; ``jats_to_markdown``
        searches the whole parsed document tree and the XML parser keeps local
        tag names, so the wrapper needs no unwrapping.
        """
        numeric = pmcid.upper().removeprefix("PMC")
        try:
            resp = await self.http_client.get(
                OAI_PMH_URL,
                params={
                    "verb": "GetRecord",
                    "identifier": f"oai:pubmedcentral.nih.gov:{numeric}",
                    "metadataPrefix": "pmc",
                },
                quiet_statuses=OAI_QUIET,
            )
            if resp is None or resp.status_code != 200 or not resp.content:
                return None
            md = jats_to_markdown(resp.content)
            if len(md.strip()) < MIN_USEFUL_CHARS:
                return None
            return FullTextResponse(
                status="full_text",
                source="pmc-oai",
                format="markdown",
                content=md,
                total_chars=len(md),
                sections_available=list_sections(md),
                doi=ids.doi,
                pmid=ids.pmid,
                pmcid=pmcid,
                url=f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/",
            )
        except Exception:
            return None

    async def _resolve_source_and_ext_id(
        self,
        ids: IdentifierMap,
    ) -> tuple[str | None, str | None]:
        if ids.pmid:
            return "MED", ids.pmid
        if ids.pmcid:
            return "PMC", ids.pmcid.upper().replace("PMC", "")
        if ids.doi:
            try:
                search_url = f"{EPMC_REST_BASE}/search"
                resp = await self.http_client.get(
                    search_url,
                    params={
                        "query": f'DOI:"{ids.doi}"',
                        "format": "json",
                        "resultType": "lite",
                    },
                )
                if resp and resp.status_code == 200:
                    data = resp.json()
                    results = data.get("resultList", {}).get("result", [])
                    if results:
                        rec = results[0]
                        if rec.get("pmid"):
                            return "MED", rec.get("pmid")
                        elif rec.get("pmcid"):
                            return "PMC", rec.get("pmcid").upper().replace("PMC", "")
            except Exception:
                pass
        return None, None

    async def fetch_references(
        self,
        ids: IdentifierMap,
        limit: int = 50,
    ) -> list[ReferenceItem]:
        source, ext_id = await self._resolve_source_and_ext_id(ids)
        if not source or not ext_id:
            return []

        url = f"{EPMC_REST_BASE}/{source}/{ext_id}/references"
        try:
            resp = await self.http_client.get(
                url,
                params={"format": "json", "pageSize": min(max(1, limit), 100)},
            )
            if resp is None or resp.status_code != 200:
                return []

            data = resp.json()
            ref_list = data.get("referenceList", {}).get("reference", [])
            references: list[ReferenceItem] = []

            for r in ref_list:
                authors: list[str] = []
                author_str = r.get("authorString", "")
                if author_str:
                    authors = [a.strip() for a in author_str.split(",") if a.strip()]

                references.append(
                    ReferenceItem(
                        id=str(r.get("id") or ""),
                        title=r.get("title", "").rstrip("."),
                        authors=authors,
                        year=str(r.get("pubYear") or ""),
                        venue=r.get("journalTitle") or "",
                        doi=r.get("doi"),
                        pmid=r.get("pmid"),
                        raw_text=r.get("citationString") or "",
                    )
                )

            return references[:limit]
        except Exception:
            return []

    async def fetch_citations(
        self,
        ids: IdentifierMap,
        limit: int = 50,
    ) -> list[CitationItem]:
        source, ext_id = await self._resolve_source_and_ext_id(ids)
        if not source or not ext_id:
            return []

        url = f"{EPMC_REST_BASE}/{source}/{ext_id}/citations"
        try:
            resp = await self.http_client.get(
                url,
                params={"format": "json", "pageSize": min(max(1, limit), 100)},
            )
            if resp is None or resp.status_code != 200:
                return []

            data = resp.json()
            cit_list = data.get("citationList", {}).get("citation", [])
            citations: list[CitationItem] = []

            for c in cit_list:
                authors: list[str] = []
                author_str = c.get("authorString", "")
                if author_str:
                    authors = [a.strip() for a in author_str.split(",") if a.strip()]

                citations.append(
                    CitationItem(
                        title=c.get("title", "").rstrip("."),
                        authors=authors,
                        year=str(c.get("pubYear") or ""),
                        venue=c.get("journalTitle") or "",
                        doi=c.get("doi"),
                        pmid=c.get("pmid"),
                        citation_count=c.get("citedByCount"),
                    )
                )

            return citations[:limit]
        except Exception:
            return []




async def annotate_oa_status(
    papers: list[PaperMetadata],
    http_client: AsyncHttpClient,
) -> None:
    """Annotate a batch of papers with Europe PMC isOpenAccess status in a single query."""
    if not papers:
        return

    doi_map: dict[str, PaperMetadata] = {}
    pmid_map: dict[str, PaperMetadata] = {}

    query_parts: list[str] = []
    for p in papers:
        if p.doi:
            clean_d = p.doi.lower()
            doi_map[clean_d] = p
            query_parts.append(f'DOI:"{p.doi}"')
        elif p.pmid:
            pmid_map[p.pmid] = p
            query_parts.append(f'EXT_ID:"{p.pmid}"')

    if not query_parts:
        return

    # Build batched OR query
    query_str = " OR ".join(query_parts)
    search_url = f"{EPMC_REST_BASE}/search"

    try:
        resp = await http_client.get(
            search_url,
            params={
                "query": query_str,
                "format": "json",
                "pageSize": min(len(query_parts), 100),
                "resultType": "lite",
            },
        )
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            results = data.get("resultList", {}).get("result", [])
            for r in results:
                is_oa = r.get("isOpenAccess") == "Y"
                status_str = "oa" if is_oa else "closed"

                r_doi = (r.get("doi") or "").lower()
                r_pmid = r.get("pmid")

                if r_doi in doi_map:
                    doi_map[r_doi].oa_status = status_str
                    if r.get("pmcid") and not doi_map[r_doi].pmcid:
                        doi_map[r_doi].pmcid = r.get("pmcid")
                elif r_pmid in pmid_map:
                    pmid_map[r_pmid].oa_status = status_str
                    if r.get("pmcid") and not pmid_map[r_pmid].pmcid:
                        pmid_map[r_pmid].pmcid = r.get("pmcid")
    except Exception:
        pass
