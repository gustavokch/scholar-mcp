import asyncio
from datetime import datetime, timezone
import email.utils
import logging
import math
import random
import re
import threading
from typing import Any
import urllib.parse

import httpx

from scholar_mcp.config import Settings
from scholar_mcp.utils.rate_limit import AsyncRateLimiter

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

DEFAULT_HOST_RATES: dict[str, float] = {
    "arxiv.org": 0.33,
    "api.fda.gov": 4.0,
    "api.crossref.org": 10.0,
    "api.openalex.org": 10.0,
    "www.ebi.ac.uk": 10.0,
    "clinicaltrials.gov": 5.0,
    "rxnav.nlm.nih.gov": 5.0,
}
DEFAULT_FALLBACK_RATE = 5.0

# Upper bound on any server-supplied Retry-After. Without it a hostile or
# misconfigured host can park a request -- and, via limiter.throttle, every
# other request to that host -- for hours.
MAX_RETRY_AFTER = 60.0


def _host_key(host: str | None) -> str:
    """Group a bare hostname into its rate-limiting bucket.

    Takes a hostname with no port and no userinfo -- use ``_limiter_for_url`` to
    get one out of a URL. Splitting a port off here would corrupt a bare IPv6
    literal, and splitting userinfo off would key the bucket on the username.
    """
    hostname = (host or "").lower().strip()
    if hostname == "ncbi.nlm.nih.gov" or hostname.endswith(".ncbi.nlm.nih.gov"):
        return "ncbi.nlm.nih.gov"
    if hostname == "arxiv.org" or hostname.endswith(".arxiv.org"):
        return "arxiv.org"
    return hostname


def _parse_retry_after(resp: httpx.Response) -> float | None:
    """Extract Retry-After header value as duration in seconds."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        # float() also accepts "inf"/"nan"; neither is a usable duration.
        if not math.isfinite(seconds):
            return None
        return min(max(0.0, seconds), MAX_RETRY_AFTER)
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return min(max(0.0, delta), MAX_RETRY_AFTER)

# How much of a failing response body to quote in the log. Bytes are sliced before
# decoding so a multi-megabyte PDF or XML body is never decoded in full.
ERROR_BODY_LOG_CHARS = 500

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_error_body(content: bytes, content_type: str = "") -> str:
    """Format and sanitize an HTTP error response body for logging."""
    if not content:
        return ""
    sample = content[: ERROR_BODY_LOG_CHARS * 2].decode("utf-8", "replace")
    is_html = (
        "text/html" in (content_type or "").lower()
        or "<html" in sample.lower()
        or "<!doctype" in sample.lower()
    )
    if is_html:
        cleaned = _HTML_COMMENT_RE.sub(" ", sample)
        cleaned = _HTML_SCRIPT_STYLE_RE.sub(" ", cleaned)
        cleaned = _HTML_TAG_RE.sub(" ", cleaned)
        cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
        return cleaned[:ERROR_BODY_LOG_CHARS]
    cleaned = _WHITESPACE_RE.sub(" ", sample).strip()
    return cleaned[:ERROR_BODY_LOG_CHARS]

def _log_expected_status(log_url: str, resp: httpx.Response) -> None:
    """Record an error status the caller declared expected.

    A registry answering 404 for a DOI it does not hold is routine, but so is a
    404 caused by broken URL quoting or a wrong base path. Dropping the record
    entirely makes the two indistinguishable, so the miss is kept at DEBUG.
    """
    logger.debug(
        "HTTP GET %s returned expected status %d: %s",
        log_url,
        resp.status_code,
        _sanitize_error_body(resp.content, resp.headers.get("content-type", "")),
    )

# Query parameters whose values must never reach the log stream. Kept narrow:
# only credential-bearing keys. `email` is here because NCBI's contact
# address identifies the operator, not because it is a key.
SENSITIVE_QUERY_PARAMS = frozenset(
    {"api_key", "apikey", "email", "token", "access_token"}
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
        max_retries: int = 4,
        backoff_base: float = 0.5,
        min_429_wait: float = 1.0,
    ) -> None:
        self.settings = settings or Settings.load()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        # Floor on the pause after a 429. NCBI counts requests in a 1.0s sliding
        # window, so anything shorter can retry inside the window that rejected
        # us. Tests set it to 0.0 to keep the suite fast.
        self.min_429_wait = min_429_wait
        self.client = httpx.AsyncClient(
            timeout=float(self.settings.request_timeout),
            follow_redirects=True,
            headers={
                "User-Agent": f"ScholarMCP/1.0.0 (mailto:{self.settings.pubmed_email or 'scholar-mcp@example.com'})"
            },
        )
        self._limiters: dict[str, AsyncRateLimiter] = {}
        self._limiters_lock = threading.Lock()

    def _limiter_for_url(self, url: str) -> AsyncRateLimiter:
        """Limiter for ``url``'s host, with the port and any userinfo stripped."""
        parsed = urllib.parse.urlparse(url)
        return self._limiter_for(parsed.hostname or parsed.netloc)

    def _limiter_for(self, host: str) -> AsyncRateLimiter:
        key = _host_key(host)
        with self._limiters_lock:
            if key not in self._limiters:
                if key == "ncbi.nlm.nih.gov":
                    rate = self.settings.ncbi_rate_limit
                elif key == "api.semanticscholar.org":
                    # S2 shared pool without a key; dedicated quota with one.
                    rate = 5.0 if self.settings.s2_api_key else 1.0
                elif key in DEFAULT_HOST_RATES:
                    rate = DEFAULT_HOST_RATES[key]
                else:
                    rate = DEFAULT_FALLBACK_RATE
                self._limiters[key] = AsyncRateLimiter(rate_per_sec=rate)
            return self._limiters[key]

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
            if value is None:
                # Match httpx, which drops None-valued params from the wire.
                continue
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
        quiet_statuses: frozenset[int] | set[int] | None = None,
    ) -> httpx.Response | None:
        """GET with rate-limiting and retries.

        Non-retryable failures report as ``None``.

        ``ok_statuses`` lists statuses the caller must inspect itself, so they are
        returned as-is instead (e.g. api.fda.gov uses 404 for "no matches found",
        a valid answer rather than a fetch failure).

        ``quiet_statuses`` lists statuses that are an expected miss rather than a
        defect, so they report as ``None`` like any other failure but without the
        warning (e.g. a scholarly registry answering 404 for a DOI it does not
        hold). Callers needing the response object want ``ok_statuses`` instead.

        A status in either set is still logged at DEBUG when it is ``>= 400``, so
        a 404 caused by a bad URL or a misconfigured parameter stays recoverable
        at ``LOG_LEVEL=DEBUG`` rather than vanishing.
        """
        target_url = self._inject_credentials(self._merge_params(url, params))
        log_url = redact_url(target_url)
        limiter = self._limiter_for_url(target_url)

        for attempt in range(self.max_retries):
            await limiter.acquire()
            try:
                resp = await self.client.get(target_url, headers=headers)
                if resp.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries - 1:
                    retry_after = _parse_retry_after(resp)
                    calc_wait = self.backoff_base * (2**attempt) + random.uniform(
                        0, 0.1 * self.backoff_base
                    )
                    if resp.status_code == 429:
                        wait_time = max(
                            retry_after or 0.0, calc_wait, self.min_429_wait
                        )
                        limiter.throttle(wait_time)
                    else:
                        wait_time = max(retry_after or 0.0, calc_wait)
                        if retry_after is not None:
                            limiter.throttle(wait_time)

                    # Routine on rate-limited hosts; only terminal failure is a warning.
                    logger.info(
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
                    if resp.status_code >= 400:
                        _log_expected_status(log_url, resp)
                    return resp
                if resp.status_code >= 400:
                    if quiet_statuses and resp.status_code in quiet_statuses:
                        _log_expected_status(log_url, resp)
                        return None
                    logger.warning(
                        "HTTP GET %s failed with status %d: %s",
                        log_url,
                        resp.status_code,
                        _sanitize_error_body(resp.content, resp.headers.get("content-type", "")),
                    )
                    return None
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < self.max_retries - 1:
                    wait_time = self.backoff_base * (2**attempt) + random.uniform(
                        0, 0.1 * self.backoff_base
                    )
                    logger.info(
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
