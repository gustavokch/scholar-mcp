import logging
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import WHOGuideline
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

IRIS_API_BASE = "https://iris.who.int/server/api"
IRIS_BROWSE_TITLE_URL = f"{IRIS_API_BASE}/discover/browses/title/items"
IRIS_SEARCH_URL = f"{IRIS_API_BASE}/discover/search/objects"
IRIS_HANDLE_BASE = "https://iris.who.int/handle"
IRIS_ITEM_TYPE_FILTER = "f.itemtype=Publications,equals"
MAX_PAGE_SIZE = 100

logger = logging.getLogger(__name__)


def _first_meta(metadata: dict[str, list[dict[str, Any]]], key: str) -> str:
    """Return metadata[key][0]["value"], or "" if the key/value is absent."""
    values = metadata.get(key) or []
    if not values:
        return ""
    first = values[0]
    if not isinstance(first, dict):
        return ""
    return first.get("value") or ""


def _all_meta(metadata: dict[str, list[dict[str, Any]]], key: str) -> list[str]:
    """Return every non-empty value string for a multi-valued metadata entry."""
    return [
        v.get("value", "")
        for v in (metadata.get(key) or [])
        if isinstance(v, dict) and v.get("value")
    ]
