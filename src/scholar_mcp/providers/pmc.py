from scholar_mcp.models import FullTextResponse, IdentifierMap
from scholar_mcp.parsers.jats import jats_to_markdown, list_sections
from scholar_mcp.providers.base import BaseProvider, MIN_USEFUL_CHARS
from scholar_mcp.utils.http import AsyncHttpClient

EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"


class PMCProvider(BaseProvider):
    """PubMed Central (PMC) JATS XML provider via NCBI E-utilities."""

    tier: str = "pmc"

    def __init__(self, http_client: AsyncHttpClient) -> None:
        super().__init__(http_client)

    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        if not ids.pmcid:
            return None

        # Standardize PMCID format (e.g. PMC123456)
        pmcid = ids.pmcid if ids.pmcid.upper().startswith("PMC") else f"PMC{ids.pmcid}"

        try:
            resp = await self.http_client.get(
                EFETCH_URL,
                params={"db": "pmc", "id": pmcid, "rettype": "xml"},
            )
            if resp is None or resp.status_code != 200:
                return None

            xml_content = resp.content
            if not xml_content:
                return None

            md = jats_to_markdown(xml_content)
            if len(md.strip()) < MIN_USEFUL_CHARS:
                return None

            sections = list_sections(md)
            return FullTextResponse(
                status="full_text",
                source="pmc",
                format="markdown",
                content=md,
                total_chars=len(md),
                sections_available=sections,
                doi=ids.doi,
                pmid=ids.pmid,
                pmcid=pmcid,
                url=f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/",
            )
        except Exception:
            return None
