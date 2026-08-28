from abc import ABC, abstractmethod
from typing import Any

from scholar_mcp.models import FullTextResponse, IdentifierMap
from scholar_mcp.utils.http import AsyncHttpClient

MIN_USEFUL_CHARS = 10


class BaseProvider(ABC):
    """Abstract base class for full-text and discovery providers."""

    tier: str = "base"

    def __init__(self, http_client: AsyncHttpClient) -> None:
        self.http_client = http_client
        self.last_skip_reason: str = ""

    @abstractmethod
    async def fetch_full_text(self, ids: IdentifierMap) -> FullTextResponse | None:
        """Fetch full text for the given identifiers, or return None if not available."""
        pass
