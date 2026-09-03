import logging
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

CT_URL = "https://clinicaltrials.gov/api/v2/studies"

# The ClinicalTrials.gov query parser rejects over-complex free-text queries
# with HTTP 400 "Too complicated query"; ~13 plain terms is enough to trip it.
# Agent-composed queries routinely exceed that, so cap the term count — the
# cap only ever fires on queries the API would reject outright.
CT_MAX_QUERY_TERMS = 10

logger = logging.getLogger(__name__)


def _cap_query_terms(query: str, max_terms: int = CT_MAX_QUERY_TERMS) -> str:
    terms = query.split()
    if not terms or max_terms <= 0:
        return ""
    if len(terms) <= max_terms:
        capped = query.strip()
    else:
        capped = " ".join(terms[:max_terms])

    if capped.count('"') % 2 != 0:
        capped += '"'
    return capped


class ClinicalTrialsClient:
    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    async def search_clinical_trials(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        capped_query = _cap_query_terms(query)
        cache_key = f"clinical_trials:{capped_query}:{limit}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [MedicalArticle.from_dict(d) for d in cached_data], meta

        articles: list[MedicalArticle] = []
        try:
            resp = await self.http_client.get(
                CT_URL,
                params={
                    "query.term": capped_query,
                    "pageSize": str(limit),
                    "format": "json",
                },
            )
            # AsyncHttpClient.get returns None once retries are exhausted or on
            # a >=400 status; check it explicitly rather than letting the miss
            # surface as an AttributeError below.
            if resp is None:
                raise FetchError("clinical trials request failed")
            data = resp.json()
            studies = data.get("studies", [])
            for study in studies:
                if not study or not isinstance(study, dict):
                    continue
                ps = study.get("protocolSection") or {}
                im = ps.get("identificationModule") or {}
                dm = ps.get("descriptionModule") or {}
                status = ps.get("statusModule") or {}
                sponsors = ps.get("sponsorCollaboratorsModule") or {}

                lead = (
                    (im.get("leadSponsor") or {}).get("name")
                    or (sponsors.get("leadSponsor") or {}).get("name")
                )
                authors = [lead] if lead else []

                title = im.get("briefTitle") or im.get("officialTitle") or "Clinical Trial"
                abstract = dm.get("briefSummary") or im.get("briefSummary") or ""
                nct_id = im.get("nctId", "")
                year = (status.get("startDateStruct") or {}).get("date", "")
                url = f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else ""

                articles.append(
                    MedicalArticle(
                        title=title,
                        authors=authors,
                        abstract=abstract,
                        journal="ClinicalTrials.gov",
                        year=year,
                        url=url,
                        nct_id=nct_id or None,
                        source_database="ClinicalTrials.gov",
                    )
                )
        except Exception:
            logger.warning("Clinical trials search failed for %r", query, exc_info=True)
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [a.to_dict() for a in articles],
            source="clinical_trials",
        )
        return articles, CacheMetadata(cached=False, cache_age=0)
