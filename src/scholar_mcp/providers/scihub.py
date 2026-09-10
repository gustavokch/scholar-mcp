import asyncio
import re
from typing import Any
from urllib.parse import urljoin
from bs4 import BeautifulSoup

from scholar_mcp.config import DEFAULT_SCIHUB_MIRRORS, Settings
from scholar_mcp.models import FullTextResponse, IdentifierMap
from scholar_mcp.parsers.pdf import pdf_bytes_to_text
from scholar_mcp.providers.base import BaseProvider, MIN_USEFUL_CHARS
from scholar_mcp.utils.http import AsyncHttpClient

_CAMOUFOX_MAX_MIRRORS = 3
_CAMOUFOX_TOTAL_TIMEOUT = 20

# Statuses that mean "this host looked at the Referer and said no". Anything else
# (transport error, timeout, 5xx, 429, other 4xx) is a failed request, not a
# rejected header, and must not buy a second retry ladder.
_REFERER_REJECTED_STATUSES = frozenset({401, 403})


def _normalize_pdf_url(url: str, base_url: str | None = None) -> str:
    url = url.split("#")[0]
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if base_url:
        return urljoin(base_url, url)
    return url


def _landing_referer(page_url: str | None, fallback: str) -> str:
    """URL to send as ``Referer``, or ``fallback`` when ``page_url`` is unusable.

    ``page.url`` is ``"about:blank"`` when navigation landed nowhere, and that
    value would both send a meaningless header and mis-resolve a relative PDF
    path through ``urljoin``.
    """
    if page_url and page_url.startswith(("http://", "https://")):
        return page_url
    return fallback


def _extract_pdf_url(html: str, base_url: str | None = None) -> str | None:
    """Extract PDF URL from Sci-Hub HTML response."""
    if not html:
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")

        iframe = soup.find("iframe")
        if iframe and iframe.get("src") and ".pdf" in iframe["src"]:
            return _normalize_pdf_url(iframe["src"], base_url)

        embed = soup.find("embed")
        if embed and embed.get("src") and ".pdf" in embed["src"]:
            return _normalize_pdf_url(embed["src"], base_url)

        for tag in soup.find_all(attrs={"onclick": True}):
            m = re.search(
                r"location\.href=['\"]([^'\"]+\.pdf[^'\"]*)['\"]",
                tag["onclick"].replace("\\/", "/"),
            )
            if m:
                return _normalize_pdf_url(m.group(1), base_url)

        for match in re.findall(r'((?:https?:)?//[^\s"\'<>]+\.pdf)', html):
            return _normalize_pdf_url(match, base_url)
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
        settings: Settings | None = None,
    ) -> None:
        super().__init__(http_client)
        self.settings = settings or Settings.load()
        self.mirrors = mirrors if mirrors is not None else list(self.settings.scihub_mirrors)

    async def _get_pdf_bytes(self, pdf_url: str, referer: str | None) -> bytes | None:
        """Fetch raw PDF bytes, preferring a landing-page ``Referer``.

        Hosts such as sci.bban.top require the header; others use hotlink
        protection that rejects a foreign one while accepting a bare request.
        Only a refusal -- 401/403, or a 200 bot-challenge page -- gets one retry
        without the header. A request that never completed is not a Referer
        problem, and ``get`` has already spent its own retry ladder on it, so it
        falls through to the next mirror instead.
        """
        if not referer:
            return await self.http_client.get_bytes(pdf_url)

        resp = await self.http_client.get(
            pdf_url,
            headers={"Referer": referer},
            ok_statuses=_REFERER_REJECTED_STATUSES,
        )
        if resp is None:
            return None
        if resp.status_code in _REFERER_REJECTED_STATUSES or self.http_client.is_unexpected_html(
            resp
        ):
            return await self.http_client.get_bytes(pdf_url)
        return resp.content

    async def _fetch_via_camoufox(
        self,
        clean_doi: str,
    ) -> tuple[bytes | None, str | None]:
        """Browser-driven scrape using Camoufox (anti-detection Firefox)
        when plain HTTP requests are blocked by upstream Cloudflare/bot-guards."""
        try:
            from camoufox.async_api import AsyncCamoufox
        except ImportError:
            return None, None

        async def _try_mirrors() -> tuple[bytes | None, str | None]:
            async with AsyncCamoufox(headless=True) as browser:
                page = await browser.new_page()
                for mirror in self.mirrors[:_CAMOUFOX_MAX_MIRRORS]:
                    mirror_url = f"{mirror.rstrip('/')}/{clean_doi}"
                    try:
                        await page.goto(
                            mirror_url,
                            wait_until="domcontentloaded",
                            timeout=15000,
                        )
                        content = await page.content()
                        page_referer = _landing_referer(page.url, mirror_url)
                        pdf_url = _extract_pdf_url(content, base_url=page_referer)
                        if not pdf_url:
                            continue

                        pdf_headers = {"Referer": page_referer}
                        try:
                            resp = await page.request.get(pdf_url, headers=pdf_headers, timeout=15000)
                            if resp.status == 200:
                                b = await resp.body()
                                if b and b.startswith(b"%PDF-"):
                                    return b, pdf_url
                        except Exception:
                            pass

                        pdf_bytes = await self._get_pdf_bytes(pdf_url, page_referer)
                        if pdf_bytes and pdf_bytes.startswith(b"%PDF-"):
                            return pdf_bytes, pdf_url
                    except Exception:
                        continue
            return None, None

        try:
            return await asyncio.wait_for(
                _try_mirrors(), timeout=_CAMOUFOX_TOTAL_TIMEOUT
            )
        except (asyncio.TimeoutError, Exception):
            return None, None

    async def fetch_pdf_bytes(
        self,
        ids: IdentifierMap,
    ) -> tuple[bytes | None, str | None]:
        """Attempt to fetch raw PDF bytes across Sci-Hub mirrors, returning (bytes, pdf_url)."""
        if not ids.doi or not ids.doi.strip():
            return None, None

        clean_doi = ids.doi.strip()
        for mirror in self.mirrors:
            mirror_url = f"{mirror.rstrip('/')}/{clean_doi}"
            try:
                resp = await self.http_client.get(mirror_url)
                if resp is None or resp.status_code != 200 or not resp.text:
                    continue

                final_page_url = _landing_referer(str(resp.url), mirror_url)
                pdf_url = _extract_pdf_url(resp.text, base_url=final_page_url)
                if not pdf_url:
                    continue

                pdf_bytes = await self._get_pdf_bytes(pdf_url, final_page_url)
                if pdf_bytes and pdf_bytes.startswith(b"%PDF-"):
                    return pdf_bytes, pdf_url
            except Exception:
                continue

        if self.settings.enable_browser_fallback:
            camoufox_bytes, camoufox_url = await self._fetch_via_camoufox(clean_doi)
            if camoufox_bytes:
                return camoufox_bytes, camoufox_url

        return None, None

    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        if not ids.doi or not ids.doi.strip():
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
