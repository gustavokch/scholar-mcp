import re
from typing import Any
from bs4 import BeautifulSoup

from scholar_mcp.config import DEFAULT_SCIHUB_MIRRORS
from scholar_mcp.models import FullTextResponse, IdentifierMap
from scholar_mcp.parsers.pdf import pdf_bytes_to_text
from scholar_mcp.providers.base import BaseProvider, MIN_USEFUL_CHARS
from scholar_mcp.utils.http import AsyncHttpClient


def _extract_pdf_url(html: str) -> str | None:
    """Extract PDF URL from Sci-Hub HTML response."""
    if not html:
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")

        iframe = soup.find("iframe")
        if iframe and iframe.get("src") and ".pdf" in iframe["src"]:
            url = iframe["src"].split("#")[0]
            return "https:" + url if url.startswith("//") else url

        embed = soup.find("embed")
        if embed and embed.get("src") and ".pdf" in embed["src"]:
            url = embed["src"].split("#")[0]
            return "https:" + url if url.startswith("//") else url

        for tag in soup.find_all(attrs={"onclick": True}):
            m = re.search(
                r"location\.href=['\"]([^'\"]+\.pdf[^'\"]*)['\"]",
                tag["onclick"].replace("\\/", "/"),
            )
            if m:
                url = m.group(1).split("#")[0]
                return "https:" + url if url.startswith("//") else url

        for match in re.findall(r'((?:https?:)?//[^\s"\'<>]+\.pdf)', html):
            url = match if match.startswith("http") else "https:" + match
            return url.split("#")[0]
    except Exception:
        pass

    return None


class SciHubProvider(BaseProvider):
    """Sci-Hub multi-mirror scraper and PDF text extractor."""

    tier: str = "scihub"

    def __init__(
        self,
        http_client: AsyncHttpClient,
        mirrors: list[str] | None = None,
    ) -> None:
        super().__init__(http_client)
        self.mirrors = mirrors if mirrors is not None else list(DEFAULT_SCIHUB_MIRRORS)

    async def fetch_pdf_bytes(
        self,
        ids: IdentifierMap,
    ) -> tuple[bytes | None, str | None]:
        """Attempt to fetch raw PDF bytes across Sci-Hub mirrors, returning (bytes, pdf_url)."""
        if not ids.doi:
            return None, None

        clean_doi = ids.doi.strip()
        for mirror in self.mirrors:
            mirror_url = f"{mirror.rstrip('/')}/{clean_doi}"
            try:
                resp = await self.http_client.get(mirror_url)
                if resp is None or resp.status_code != 200 or not resp.text:
                    continue

                pdf_url = _extract_pdf_url(resp.text)
                if not pdf_url:
                    continue

                pdf_bytes = await self.http_client.get_bytes(pdf_url)
                if pdf_bytes:
                    return pdf_bytes, pdf_url
            except Exception:
                continue

        return None, None

    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        if not ids.doi:
            return None

        pdf_bytes, pdf_url = await self.fetch_pdf_bytes(ids)
        if not pdf_bytes:
            return None

        text = pdf_bytes_to_text(pdf_bytes)
        if len(text.strip()) < MIN_USEFUL_CHARS:
            return None

        return FullTextResponse(
            status="full_text",
            source="scihub",
            format="text",
            content=text,
            total_chars=len(text),
            doi=ids.doi,
            pmid=ids.pmid,
            pmcid=ids.pmcid,
            url=pdf_url,
        )
