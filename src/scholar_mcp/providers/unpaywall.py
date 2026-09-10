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

    async def _lookup(self, ids: IdentifierMap) -> dict[str, Any] | None:
        """Fetch the Unpaywall record for ``ids.doi``, or None if there is none.

        A 404 here means Unpaywall does not hold the DOI -- routine for a
        preprint or a very recent article -- so it is an expected miss rather
        than a fetch failure.
        """
        if not self.email:
            self.last_skip_reason = "UNPAYWALL_EMAIL not configured"
            return None

        if not ids.doi:
            return None

        clean_doi = ids.doi.strip()
        resp = await self.http_client.get(
            f"{UNPAYWALL_BASE}/{clean_doi}",
            params={"email": self.email},
            quiet_statuses={404},
        )
        if resp is None or resp.status_code != 200:
            return None
        return resp.json()

    @staticmethod
    def _best_pdf_url(data: dict[str, Any]) -> str | None:
        """Pick the best open-access URL from an Unpaywall record."""
        if not data.get("is_oa"):
            return None
        best_loc = data.get("best_oa_location") or {}
        return best_loc.get("url_for_pdf") or best_loc.get("url")

    async def fetch_oa_pdf_url(self, ids: IdentifierMap) -> str | None:
        """Locate an open-access PDF for ``ids`` without downloading it."""
        try:
            data = await self._lookup(ids)
            if data is None:
                return None
            return self._best_pdf_url(data)
        except Exception:
            return None

    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        try:
            data = await self._lookup(ids)
            if data is None:
                return None

            pdf_url = self._best_pdf_url(data)
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
