from abc import ABC, abstractmethod
from typing import Any

import httpx

from scholar_mcp.models import FullTextResponse, IdentifierMap
from scholar_mcp.utils.ctxstate import ContextScoped
from scholar_mcp.utils.http import AsyncHttpClient

MIN_USEFUL_CHARS = 10


def failure_reason(
    client: AsyncHttpClient,
    resp: httpx.Response | None = None,
    exc: Exception | None = None,
) -> str:
    """Derive the ``last_error`` string from the client's typed failure record.

    Prefers ``client.last_failure`` (set by ``AsyncHttpClient.get`` at every
    terminal failure): ``"<kind>_<status>"`` when a status is known, else the
    bare kind. Falls back to ``http_<code>`` for a non-200 response returned
    via ``ok_statuses`` (which clears the typed record), then to
    ``exception:<Class>`` for a raised error. The returned shapes are the
    strings the resolver has always consumed.
    """
    fail = client.last_failure
    if fail is not None:
        if fail.status is not None:
            return f"{fail.kind}_{fail.status}"
        return fail.kind
    if resp is not None:
        return f"http_{resp.status_code}"
    if exc is not None:
        return f"exception:{type(exc).__name__}"
    return "transport"


class BaseProvider(ABC):
    """Abstract base class for full-text and discovery providers."""

    tier: str = "base"

    # Set to a short reason immediately before a deliberate self-skip or a
    # terminal miss; read by the waterfall's per-tier attempt log. A singleton-
    # written string is exactly the cross-request bleed ContextScoped exists
    # for, so this is a context-scoped descriptor, not a plain attribute.
    last_skip_reason: ContextScoped[str] = ContextScoped(str)

    def __init__(self, http_client: AsyncHttpClient) -> None:
        self.http_client = http_client

    @abstractmethod
    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        """Fetch full text for the given identifiers, or return None if not available."""
        pass
