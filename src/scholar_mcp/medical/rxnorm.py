import logging
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import RxNormDrug
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

RXNORM_DRUGS_URL = "https://rxnav.nlm.nih.gov/REST/drugs.json"

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if v]
    val_str = str(value).strip()
    return [val_str] if val_str else []


class RxNormClient:
    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    async def search_drug_nomenclature(
        self,
        query: str,
    ) -> tuple[list[RxNormDrug], CacheMetadata]:
        cache_key = f"rxnorm:{query}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [RxNormDrug.from_dict(d) for d in cached_data], meta

        drugs: list[RxNormDrug] = []
        try:
            resp = await self.http_client.get(
                RXNORM_DRUGS_URL,
                params={"name": query},
            )
            if resp is None:
                raise FetchError("rxnorm request failed")
            data = resp.json()
            concept_groups = data.get("drugGroup", {}).get("conceptGroup", [])
            for group in concept_groups:
                props = group.get("conceptProperties", [])
                for p in props:
                    if not p or not isinstance(p, dict):
                        continue
                    rxcui = str(p.get("rxcui", ""))
                    name = str(p.get("name", ""))
                    if not rxcui or not name:
                        continue
                    tty = str(p.get("tty", ""))
                    language = str(p.get("language", "ENG"))
                    suppress = str(p.get("suppress", ""))
                    synonyms = _as_list(p.get("synonym"))
                    umlscui = _as_list(p.get("umlscui"))

                    drugs.append(
                        RxNormDrug(
                            rxcui=rxcui,
                            name=name,
                            tty=tty,
                            language=language,
                            suppress=suppress,
                            synonyms=synonyms,
                            umlscui=umlscui,
                        )
                    )
        except Exception:
            logger.warning("RxNorm lookup failed for %r", query, exc_info=True)
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [d.to_dict() for d in drugs],
            source="rxnorm",
        )
        return drugs, CacheMetadata(cached=False, cache_age=0)
