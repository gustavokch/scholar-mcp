import asyncio
import random
import threading
from typing import Any
import urllib.parse

import httpx

from scholar_mcp.config import Settings
from scholar_mcp.utils.rate_limit import AsyncRateLimiter

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class AsyncHttpClient:
    """Shared HTTP client with rate-limiting, retries, and NCBI credential injection."""

    def __init__(
        self,
        settings: Settings | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> None:
        self.settings = settings or Settings.load()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.client = httpx.AsyncClient(
            timeout=float(self.settings.request_timeout),
            follow_redirects=True,
            headers={
                "User-Agent": f"ScholarMCP/1.0.0 (mailto:{self.settings.pubmed_email or 'scholar-mcp@example.com'})"
            },
        )
        self._limiters: dict[str, AsyncRateLimiter] = {}
        self._limiters_lock = threading.Lock()

    def _limiter_for(self, host: str) -> AsyncRateLimiter:
        host = host.lower()
        with self._limiters_lock:
            if host not in self._limiters:
                if host == "eutils.ncbi.nlm.nih.gov":
                    rate = self.settings.ncbi_rate_limit
                elif host == "api.semanticscholar.org":
                    # S2 shared pool without a key; dedicated quota with one.
                    rate = 5.0 if self.settings.s2_api_key else 1.0
                else:
                    rate = 10.0
                self._limiters[host] = AsyncRateLimiter(rate_per_sec=rate)
            return self._limiters[host]

    def _inject_credentials(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if hostname == "eutils.ncbi.nlm.nih.gov":
            query_dict = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if self.settings.pubmed_api_key and "api_key" not in query_dict:
                query_dict["api_key"] = [self.settings.pubmed_api_key]
            if self.settings.pubmed_email and "email" not in query_dict:
                query_dict["email"] = [self.settings.pubmed_email]
            if self.settings.pubmed_tool and "tool" not in query_dict:
                query_dict["tool"] = [self.settings.pubmed_tool]
            new_query = urllib.parse.urlencode(query_dict, doseq=True)
            return urllib.parse.urlunparse(parsed._replace(query=new_query))
        return url

    def _is_unexpected_html(self, resp: httpx.Response) -> bool:
        content_type = resp.headers.get("content-type", "").lower()
        if "text/html" in content_type:
            text_sample = resp.text[:1000].lower()
            if any(
                marker in text_sample
                for marker in (
                    "cloudflare",
                    "ddg",
                    "challenge-platform",
                    "just a moment",
                    "captcha",
                    "attention required",
                )
            ):
                return True
        return False

    async def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        target_url = self._inject_credentials(url)
        parsed = urllib.parse.urlparse(target_url)
        limiter = self._limiter_for(parsed.netloc)

        for attempt in range(self.max_retries):
            await limiter.acquire()
            try:
                resp = await self.client.get(target_url, headers=headers, params=params)
                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries - 1:
                    wait_time = self.backoff_base * (2**attempt) + random.uniform(
                        0, 0.1 * self.backoff_base
                    )
                    await asyncio.sleep(wait_time)
                    continue
                if resp.status_code >= 400:
                    return None
                return resp
            except (httpx.TransportError, httpx.TimeoutException):
                if attempt < self.max_retries - 1:
                    wait_time = self.backoff_base * (2**attempt) + random.uniform(
                        0, 0.1 * self.backoff_base
                    )
                    await asyncio.sleep(wait_time)
                    continue
                return None
            except Exception:
                return None
        return None

    async def get_bytes(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> bytes | None:
        resp = await self.get(url, headers=headers, params=params)
        if resp is not None and resp.status_code == 200:
            if not self._is_unexpected_html(resp):
                return resp.content
        return None

    async def aclose(self) -> None:
        await self.client.aclose()
