import asyncio
import logging
import random
import threading
from typing import Any
import urllib.parse

import httpx

from scholar_mcp.config import Settings
from scholar_mcp.utils.rate_limit import AsyncRateLimiter

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

# Query parameters whose values must never reach the log stream. `email` is here
# because NCBI's contact address identifies the operator, not because it is a key.
SENSITIVE_QUERY_PARAMS = frozenset(
    {"api_key", "apikey", "key", "email", "token", "access_token", "mailto"}
)


def redact_url(url: str) -> str:
    """Return ``url`` with the values of sensitive query parameters masked."""
    parsed = urllib.parse.urlparse(url)
    if not parsed.query:
        return url
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key.lower() in SENSITIVE_QUERY_PARAMS for key, _ in pairs):
        return url
    masked = [
        (key, "REDACTED" if key.lower() in SENSITIVE_QUERY_PARAMS else value)
        for key, value in pairs
    ]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(masked)))


class FetchError(RuntimeError):
    """Raised by callers when AsyncHttpClient returns None for a request.

    `AsyncHttpClient.get` reports failure by returning None, so a caller that
    ignores it only discovers the miss when the response is dereferenced. Client
    code raises this instead, so the failure is explicit and can be logged and
    flagged as `CacheMetadata.error`.
    """


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

    def _merge_params(self, url: str, params: dict[str, Any] | None) -> str:
        """Fold ``params`` into the URL query.

        httpx replaces the whole query string when ``params`` is passed to
        ``client.get``, which would discard anything ``_inject_credentials`` adds.
        Merging first, then handing httpx a single URL, keeps both.
        """
        if not params:
            return url
        parsed = urllib.parse.urlparse(url)
        query_dict = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        for key, value in params.items():
            if isinstance(value, (list, tuple)):
                query_dict[key] = [str(item) for item in value]
            else:
                query_dict[key] = [str(value)]
        new_query = urllib.parse.urlencode(query_dict, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))

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
        ok_statuses: frozenset[int] | set[int] | None = None,
    ) -> httpx.Response | None:
        """GET with rate-limiting and retries.

        Non-retryable failures report as ``None``. Statuses listed in
        ``ok_statuses`` are returned as-is instead, for callers that must
        distinguish them (e.g. api.fda.gov uses 404 for "no matches found",
        a valid answer rather than a fetch failure).
        """
        target_url = self._inject_credentials(self._merge_params(url, params))
        log_url = redact_url(target_url)
        parsed = urllib.parse.urlparse(target_url)
        limiter = self._limiter_for(parsed.netloc)

        for attempt in range(self.max_retries):
            await limiter.acquire()
            try:
                resp = await self.client.get(target_url, headers=headers)
                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries - 1:
                    wait_time = self.backoff_base * (2**attempt) + random.uniform(
                        0, 0.1 * self.backoff_base
                    )
                    logger.warning(
                        "HTTP GET %s returned retryable status %d (attempt %d/%d), retrying in %.2fs",
                        log_url,
                        resp.status_code,
                        attempt + 1,
                        self.max_retries,
                        wait_time,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                if ok_statuses and resp.status_code in ok_statuses:
                    return resp
                if resp.status_code >= 400:
                    logger.warning(
                        "HTTP GET %s failed with status %d: %s",
                        log_url,
                        resp.status_code,
                        resp.text[:500],
                    )
                    return None
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < self.max_retries - 1:
                    wait_time = self.backoff_base * (2**attempt) + random.uniform(
                        0, 0.1 * self.backoff_base
                    )
                    logger.warning(
                        "HTTP GET %s raised %s (attempt %d/%d), retrying in %.2fs: %s",
                        log_url,
                        type(exc).__name__,
                        attempt + 1,
                        self.max_retries,
                        wait_time,
                        exc,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.warning(
                    "HTTP GET %s failed after %d attempts: %s",
                    log_url,
                    self.max_retries,
                    exc,
                )
                return None
            except Exception as exc:
                logger.warning(
                    "HTTP GET %s raised unexpected exception: %s",
                    log_url,
                    exc,
                    exc_info=True,
                )
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
