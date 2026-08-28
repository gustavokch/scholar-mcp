from typing import Any

from scholar_mcp.models import FullTextResponse, IdentifierMap
from scholar_mcp.parsers.pdf import pdf_bytes_to_text
from scholar_mcp.providers.base import BaseProvider, MIN_USEFUL_CHARS
from scholar_mcp.utils.http import AsyncHttpClient

UNPAYWALL_BASE = "https://api.unpaywall.org/v2"


class UnpaywallProvider(BaseProvider):
    """Unpaywall open-access PDF locator and extractor."""

    tier: str = "unpaywall"

    def __init__(self, http_client: AsyncHttpClient, email: str | None = None) -> None:
        super().__init__(http_client)
        self.email = email

    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        if not self.email:
            self.last_skip_reason = "UNPAYWALL_EMAIL not configured"
            return None

        if not ids.doi:
            return None

        clean_doi = ids.doi.strip()
        url = f"{UNPAYWALL_BASE}/{clean_doi}"

        try:
            resp = await self.http_client.get(url, params={"email": self.email})
            if resp is None or resp.status_code != 200:
                return None

            data = resp.json()
            if not data.get("is_oa"):
                return None

            best_loc = data.get("best_oa_location") or {}
            pdf_url = best_loc.get("url_for_pdf") or best_loc.get("url")
            if not pdf_url:
                return None

            pdf_bytes = await self.http_client.get_bytes(pdf_url)
            if not pdf_bytes:
                return None

            text = pdf_bytes_to_text(pdf_bytes)
            if len(text.strip()) < MIN_USEFUL_CHARS:
                return None

            title = data.get("title") or ""
            return FullTextResponse(
                status="full_text",
                source="unpaywall",
                format="text",
                title=title,
                content=text,
                total_chars=len(text),
                doi=ids.doi,
                pmid=ids.pmid,
                pmcid=ids.pmcid,
                url=pdf_url,
            )
        except Exception:
            return None
