import asyncio
import email.utils
import logging
import math
import random
import re
import socket
import ssl
import threading
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar, Literal

import httpx

from scholar_mcp.config import Settings
from scholar_mcp.utils.ctxstate import ContextScoped
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
    "pesquisa.bvsalud.org": 1.0,
}
DEFAULT_FALLBACK_RATE = 5.0

# Hosts whose 403 is a bot shield reacting to bursts, not a real "forbidden".
# pesquisa.bvsalud.org rejected ~30 requests during an eval run even under the
# configured rate; for these hosts a 403 must back the whole host bucket off
# and retry, like a 429. Elsewhere a 403 stays fatal.
BOT_SHIELD_403_HOSTS = frozenset({"pesquisa.bvsalud.org"})

# A 403 whose body is one of these shield/challenge pages cannot be cleared by a
# plain HTTP retry -- it wants a real browser. Retrying only burns the stage
# budget and throttles the shared host bucket for every concurrent coroutine.
BOT_SHIELD_HTML_MARKERS = (
    "shield-templates-prod",
    "b-cdn.net",
    "block.html",
    "challenge-platform",
    "just a moment",
    "captcha",
    "attention required",
)

UNEXPECTED_HTML_MARKERS = (
    "cloudflare",
    "ddg",
    "challenge-platform",
    "just a moment",
    "captcha",
    "attention required",
)


def _matches_html_markers(
    resp: httpx.Response,
    markers: Sequence[str],
    max_chars: int = 1000,
) -> bool:
    """Return True if resp has text/html content-type and matches any marker."""
    if "text/html" not in resp.headers.get("content-type", "").lower():
        return False
    sample = resp.text[:max_chars].lower()
    return any(marker in sample for marker in markers)

# Upper bound on any server-supplied Retry-After. Without it a hostile or
# misconfigured host can park a request -- and, via limiter.throttle, every
# other request to that host -- for hours.
MAX_RETRY_AFTER = 60.0

# How long a "permanently dead" host verdict (DNS failure, bad cert) is
# trusted before the next request re-probes the host. Long enough that a
# stage chain does not eat the retry cost mid-run, short enough that a fixed
# mirror recovers within one server uptime window instead of needing a
# restart.
DEAD_HOST_TTL_S = 1800.0


@dataclass(frozen=True)
class FetchFailure:
    """Typed record of why ``AsyncHttpClient.get`` returned ``None``.

    ``kind`` discriminates the terminal failure: an HTTP status (``"http"``),
    a transport/timeout error after retries (``"transport"``), or any other
    unexpected exception (``"exception"``). ``status`` carries the HTTP status
    for ``"http"`` failures only; ``detail`` is the reason phrase or the
    exception class name. Consumers map this to vocabulary like
    "blocked"/"failed" instead of sniffing free-form strings.
    """

    kind: Literal["transport", "http", "exception"]
    status: int | None
    detail: str


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
    if hostname == "pesquisa.bvsalud.org" or hostname.endswith(".bvsalud.org"):
        return "pesquisa.bvsalud.org"
    return hostname


_PERMANENT_TRANSPORT_EXCEPTIONS = (
    socket.gaierror,
    ssl.SSLCertVerificationError,
)


def _is_permanent_transport_error(exc: BaseException) -> bool:
    """True when exc or any chained cause/context is a permanent network failure.

    socket.gaierror (DNS lookup failure) and ssl.SSLCertVerificationError (untrusted
    or self-signed certificate) do not clear on retry with backoff.

    The chain walk keeps a visited set: ``__context__`` can link exceptions in a
    cycle (respx wraps handler exceptions that way), which would hang the walk.
    """
    seen: set[int] = set()
    curr: BaseException | None = exc
    while curr is not None and id(curr) not in seen:
        seen.add(id(curr))
        if isinstance(curr, _PERMANENT_TRANSPORT_EXCEPTIONS):
            return True
        curr = curr.__cause__ or curr.__context__
    return False


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

    # Process-global limiter registry, bounded by the number of distinct hosts.
    # A second client built from the same settings (the resolver's fallback
    # path) must not double the effective rate against a host, and a caller
    # that builds a client per request in its own asyncio.run (zimqa) must
    # still share the process-wide budget rather than mint a fresh bucket per
    # call. Both shapes need one map keyed by host.
    #
    # The limiter carries no loop-bound state (its lock is a threading.Lock
    # held across arithmetic only), so loops, threads, and client instances
    # can all share one bucket safely.
    #
    # Keyed and unkeyed NCBI callers share one bucket at the most conservative
    # rate seen for the host: NCBI counts requests per IP, not per client or
    # per key, so two buckets would put the sum of both rates against one
    # ceiling. Flooring the rate loses throughput only in a process mixing
    # credentials, which is not a deployment shape we ship.
    _limiters: dict[str, AsyncRateLimiter] = {}
    _limiters_lock = threading.Lock()

    # Cache of hosts with permanent transport failures (DNS resolution or SSL
    # certificate verification errors). Subsequent requests to these hosts
    # short-circuit immediately without making network calls. Keyed on host,
    # valued on the monotonic deadline the verdict expires -- a DNS blip or a
    # cert renewal window must not blackhole a host for the rest of the
    # process's life, so the verdict is re-probed after DEAD_HOST_TTL_S.
    _dead_hosts: ClassVar[dict[str, float]] = {}
    _dead_hosts_lock = threading.Lock()

    # Typed record of the most recent terminal failure, per requesting task.
    # ContextScoped: the client is a shared singleton, so a plain attribute
    # would leak one request's failure into every concurrent call.
    last_failure: ContextScoped[FetchFailure | None] = ContextScoped(lambda: None)

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
        # Floor on the pause after a 429, or after a bot-shield 403 (BVS).
        # NCBI counts requests in a 1.0s sliding window, so anything shorter
        # can retry inside the window that rejected us. Tests set it to 0.0
        # to keep the suite fast.
        self.min_429_wait = min_429_wait
        self.client = httpx.AsyncClient(
            timeout=float(self.settings.request_timeout),
            follow_redirects=True,
            headers={
                "User-Agent": f"ScholarMCP/1.0.0 (mailto:{self.settings.pubmed_email or 'scholar-mcp@example.com'})"
            },
        )

    def _limiter_for_url(self, url: str) -> AsyncRateLimiter:
        """Limiter for ``url``'s host, with the port and any userinfo stripped."""
        parsed = urllib.parse.urlparse(url)
        return self._limiter_for(parsed.hostname or parsed.netloc)

    def _limiter_for(self, host: str) -> AsyncRateLimiter:
        host_key = _host_key(host)
        if host_key == "ncbi.nlm.nih.gov":
            rate = self.settings.ncbi_rate_limit
        elif host_key == "api.semanticscholar.org":
            # S2 shared pool without a key; dedicated quota with one.
            rate = 5.0 if self.settings.s2_api_key else 1.0
        elif host_key in DEFAULT_HOST_RATES:
            rate = DEFAULT_HOST_RATES[host_key]
        else:
            rate = DEFAULT_FALLBACK_RATE
        with self._limiters_lock:
            limiter = self._limiters.get(host_key)
            if limiter is None:
                limiter = AsyncRateLimiter(rate_per_sec=rate)
                self._limiters[host_key] = limiter
            elif limiter.rate_per_sec > rate:
                limiter.rate_per_sec = rate
            return limiter

    @classmethod
    def limiter_bucket_count(cls) -> int:
        """Total live limiters. For tests and diagnostics."""
        with cls._limiters_lock:
            return len(cls._limiters)

    @classmethod
    def reset_limiters(cls) -> None:
        """Drop every limiter bucket and dead host. For tests; never call it on a live server."""
        with cls._limiters_lock:
            cls._limiters.clear()
        with cls._dead_hosts_lock:
            cls._dead_hosts.clear()

    @classmethod
    def reset_dead_hosts(cls) -> None:
        """Drop every dead-host verdict. For tests; never call it on a live server."""
        with cls._dead_hosts_lock:
            cls._dead_hosts.clear()

    @classmethod
    def is_dead_host(cls, host: str) -> bool:
        """True when host suffered a permanent transport failure within DEAD_HOST_TTL_S."""
        key = _host_key(host)
        with cls._dead_hosts_lock:
            deadline = cls._dead_hosts.get(key)
            if deadline is None:
                return False
            if deadline <= time.monotonic():
                del cls._dead_hosts[key]
                return False
            return True

    @classmethod
    def mark_dead_host(cls, host: str) -> None:
        """Record host as dead until DEAD_HOST_TTL_S from now."""
        with cls._dead_hosts_lock:
            cls._dead_hosts[_host_key(host)] = time.monotonic() + DEAD_HOST_TTL_S

    def is_throttled(self, host: str) -> bool:
        """True when ``host``'s limiter holds a throttle deadline in the future.

        Read-only: an unknown host is not throttled and gets no bucket.
        """
        with self._limiters_lock:
            limiter = self._limiters.get(_host_key(host))
        return limiter is not None and limiter.throttled_until > time.monotonic()

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

    @staticmethod
    def _takes_eutils_params(parsed: urllib.parse.ParseResult) -> bool:
        """Whether ``parsed`` names an endpoint that reads api_key/email/tool.

        Only E-utilities and the PMC ID converter accept these. Every other
        NCBI host serves ordinary pages and files that ignore them -- and the
        resolver hands this client whatever OA location Unpaywall reports,
        which for PMC is routinely ``www.ncbi.nlm.nih.gov/pmc/articles/...``.
        Matching the whole domain would append the API key to those downloads
        for no benefit, so the match stays on the endpoints that use it.
        """
        hostname = (parsed.hostname or "").lower()
        if hostname == "eutils.ncbi.nlm.nih.gov":
            return True
        is_ncbi = hostname == "ncbi.nlm.nih.gov" or hostname.endswith(".ncbi.nlm.nih.gov")
        return is_ncbi and "/pmc/utils/idconv/" in parsed.path

    def _inject_credentials(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if self._takes_eutils_params(parsed):
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

    def is_unexpected_html(self, resp: httpx.Response) -> bool:
        return _matches_html_markers(resp, UNEXPECTED_HTML_MARKERS)

    def _is_challenge_html(self, resp: httpx.Response) -> bool:
        return _matches_html_markers(resp, BOT_SHIELD_HTML_MARKERS)

    _is_unexpected_html = is_unexpected_html

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
        host_key = _host_key(urllib.parse.urlparse(target_url).hostname)

        if self.is_dead_host(host_key):
            self.last_failure = FetchFailure("transport", None, "DeadHostCached")
            logger.info("HTTP GET %s skipped: host %s is marked permanently dead", log_url, host_key)
            return None

        for attempt in range(self.max_retries):
            await limiter.acquire()
            try:
                resp = await self.client.get(target_url, headers=headers)
                # A bot-shield 403 normally behaves like a 429: throttle the whole
                # host bucket and retry. The exception is a 403 carrying a JS
                # challenge page, which no number of plain HTTP retries can pass.
                # A 403 from any other host stays fatal.
                is_challenge_html = (
                    resp.status_code == 403
                    and host_key in BOT_SHIELD_403_HOSTS
                    and self._is_challenge_html(resp)
                )
                shielded_403 = (
                    resp.status_code == 403
                    and host_key in BOT_SHIELD_403_HOSTS
                    and not is_challenge_html
                )
                # NCBI E-utilities reports an internal viewer timeout as HTTP 400:
                # 'Error: External viewer error: Empty Response. Bytes read: 0 Status: Timeout'
                # Only the timeout variant is transient -- a viewer error without it
                # is a permanent request defect and must stay fatal on the first try.
                ncbi_viewer_timeout = (
                    resp.status_code == 400
                    and host_key == "ncbi.nlm.nih.gov"
                    and b"External viewer error" in resp.content
                    and b"Status: Timeout" in resp.content
                )
                if (
                    resp.status_code in RETRYABLE_STATUS_CODES
                    or shielded_403
                    or ncbi_viewer_timeout
                ) and attempt < self.max_retries - 1:
                    retry_after = _parse_retry_after(resp)
                    calc_wait = self.backoff_base * (2**attempt) + random.uniform(
                        0, 0.1 * self.backoff_base
                    )
                    if resp.status_code == 429 or shielded_403:
                        wait_time = max(
                            retry_after or 0.0, calc_wait, self.min_429_wait
                        )
                        limiter.throttle(wait_time)
                    else:
                        wait_time = max(retry_after or 0.0, calc_wait)
                        if retry_after is not None:
                            limiter.throttle(wait_time)

                    self.last_failure = FetchFailure(
                        "http", resp.status_code, resp.reason_phrase or ""
                    )

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
                    self.last_failure = None
                    return resp
                if resp.status_code >= 400:
                    self.last_failure = FetchFailure(
                        "http", resp.status_code, resp.reason_phrase or ""
                    )
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
                self.last_failure = None
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if _is_permanent_transport_error(exc):
                    self.mark_dead_host(host_key)
                    self.last_failure = FetchFailure("transport", None, type(exc).__name__)
                    logger.warning(
                        "HTTP GET %s failed permanently on attempt %d: %s",
                        log_url,
                        attempt + 1,
                        exc,
                    )
                    return None
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
                self.last_failure = FetchFailure("transport", None, type(exc).__name__)
                logger.warning(
                    "HTTP GET %s failed after %d attempts: %s",
                    log_url,
                    self.max_retries,
                    exc,
                )
                return None
            except Exception as exc:
                self.last_failure = FetchFailure("exception", None, type(exc).__name__)
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
        quiet_statuses: frozenset[int] | set[int] | None = None,
    ) -> bytes | None:
        resp = await self.get(
            url, headers=headers, params=params, quiet_statuses=quiet_statuses
        )
        if resp is not None and resp.status_code == 200:
            if not self._is_unexpected_html(resp):
                return resp.content
        return None

    async def aclose(self) -> None:
        await self.client.aclose()
