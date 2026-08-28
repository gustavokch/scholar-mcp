import re
from typing import Any

from scholar_mcp.models import FullTextResponse, IdentifierMap, PaperMetadata
from scholar_mcp.parsers.jats import jats_to_markdown, list_sections
from scholar_mcp.providers.base import BaseProvider, MIN_USEFUL_CHARS
from scholar_mcp.utils.http import AsyncHttpClient

EPMC_REST_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"


class EuropePMCProvider(BaseProvider):
    """Europe PMC open-access provider with JATS XML support."""

    tier: str = "europepmc"

    def __init__(self, http_client: AsyncHttpClient) -> None:
        super().__init__(http_client)

    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        pmcid = ids.pmcid
        if pmcid:
            if not pmcid.upper().startswith("PMC"):
                pmcid = f"PMC{pmcid}"
            url = f"{EPMC_REST_BASE}/{pmcid}/fullTextXML"
            try:
                resp = await self.http_client.get(url)
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
                            pmid=ids.pmid,
                            pmcid=pmcid,
                            url=f"https://europepmc.org/article/PMC/{pmcid}",
                        )
            except Exception:
                pass

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
                            xml_url = f"{EPMC_REST_BASE}/{found_pmcid}/fullTextXML"
                            xml_resp = await self.http_client.get(xml_url)
                            if xml_resp is not None and xml_resp.status_code == 200 and xml_resp.content:
                                md = jats_to_markdown(xml_resp.content)
                                if len(md.strip()) >= MIN_USEFUL_CHARS:
                                    return FullTextResponse(
                                        status="full_text",
                                        source="europepmc",
                                        format="markdown",
                                        content=md,
                                        total_chars=len(md),
                                        sections_available=list_sections(md),
                                        doi=ids.doi,
                                        pmid=ids.pmid or rec.get("pmid"),
                                        pmcid=found_pmcid,
                                        url=f"https://europepmc.org/article/PMC/{found_pmcid}",
                                    )
            except Exception:
                pass

        return None


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
