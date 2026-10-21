import logging
import re
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import MedicalArticle
from scholar_mcp.query_relax import ENGLISH_STOPWORDS
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

CT_URL = "https://clinicaltrials.gov/api/v2/studies"

# The ClinicalTrials.gov query parser rejects over-complex free-text queries
# with HTTP 400 "Too complicated query"; ~13 plain terms is enough to trip it.
# Agent-composed queries routinely exceed that, so cap the term count — the
# cap only ever fires on queries the API would reject outright.
CT_MAX_QUERY_TERMS = 10

# Studies fetched when an intervention is given: query.intr also matches arm
# descriptions and synonyms, so the pool must cover the trials the
# intervention filter drops before ``limit`` is applied.
CT_INTERVENTION_POOL = 50

# Comparison words name no concept; query.term would AND them as literals.
_TERM_STOPWORDS = ENGLISH_STOPWORDS | {"vs", "versus"}
_WORD_RE = re.compile(r"[\w'-]+")

logger = logging.getLogger(__name__)


def _cap_query_terms(query: str, max_terms: int = CT_MAX_QUERY_TERMS) -> str:
    terms = query.split()
    if not terms or max_terms <= 0:
        return ""
    if len(terms) <= max_terms:
        capped = query.strip()
    else:
        # Truncation can strand a boolean connector at the end, which the
        # Essie parser also rejects. Only strip on the truncation path —
        # queries within the limit go out verbatim.
        kept = terms[:max_terms]
        # Essie operators are uppercase; lowercase "and"/"or" is content.
        while kept and kept[-1] in {"AND", "OR", "NOT"}:
            kept.pop()
        capped = " ".join(kept)

    if capped.count('"') % 2 != 0:
        capped += '"'
    return capped


def _residual_term(query: str, intervention: str | None, condition: str | None) -> str:
    """The query's words not already sent as query.intr/query.cond, without
    stopwords: ClinicalTrials.gov ANDs every query.term word."""
    taken = {w.lower() for w in _WORD_RE.findall(f"{intervention or ''} {condition or ''}")}
    kept = [
        w for w in _WORD_RE.findall(query)
        if w.lower() not in taken and w.lower() not in _TERM_STOPWORDS
    ]
    return _cap_query_terms(" ".join(kept))


def _names_intervention(study: dict[str, Any], intervention: str) -> bool:
    """True when one of the study's interventions (name or other name) is
    ``intervention`` as a whole phrase."""
    pattern = re.compile(
        r"\b" + r"\s+".join(map(re.escape, intervention.split())) + r"\b", re.IGNORECASE
    )
    arms = (study.get("protocolSection") or {}).get("armsInterventionsModule") or {}
    for iv in arms.get("interventions") or []:
        names = [iv.get("name") or "", *(iv.get("otherNames") or [])]
        if any(pattern.search(n) for n in names):
            return True
    return False


def _is_unknown_status(study: dict[str, Any]) -> bool:
    status = (study.get("protocolSection") or {}).get("statusModule") or {}
    return status.get("overallStatus") == "UNKNOWN"


def _study_to_article(study: dict[str, Any]) -> MedicalArticle:
    ps = study.get("protocolSection") or {}
    im = ps.get("identificationModule") or {}
    dm = ps.get("descriptionModule") or {}
    status = ps.get("statusModule") or {}
    sponsors = ps.get("sponsorCollaboratorsModule") or {}

    lead = (
        (im.get("leadSponsor") or {}).get("name")
        or (sponsors.get("leadSponsor") or {}).get("name")
    )
    nct_id = im.get("nctId", "")
    return MedicalArticle(
        title=im.get("briefTitle") or im.get("officialTitle") or "Clinical Trial",
        authors=[lead] if lead else [],
        abstract=dm.get("briefSummary") or im.get("briefSummary") or "",
        journal="ClinicalTrials.gov",
        year=(status.get("startDateStruct") or {}).get("date", ""),
        url=f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
        nct_id=nct_id or None,
        source_database="ClinicalTrials.gov",
    )


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
        intervention: str | None = None,
        condition: str | None = None,
    ) -> tuple[list[MedicalArticle], CacheMetadata]:
        """Search ClinicalTrials.gov.

        Without ``intervention``/``condition`` the whole query goes to
        query.term, which ANDs every word. With them, they go to query.intr
        and query.cond and only the query's remaining words, stopwords
        removed, go to query.term. An ``intervention`` also keeps only
        trials whose interventions name it, with status UNKNOWN last.
        """
        intervention = (intervention or "").strip() or None
        condition = (condition or "").strip() or None
        structured = bool(intervention or condition)
        term = (
            _residual_term(query, intervention, condition)
            if structured
            else _cap_query_terms(query)
        )
        cache_key = (
            f"clinical_trials:v2:{term}|{intervention or ''}|{condition or ''}:{limit}"
        )
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [MedicalArticle.from_dict(d) for d in cached_data], meta

        params = {
            "pageSize": str(max(limit, CT_INTERVENTION_POOL) if intervention else limit),
            "format": "json",
        }
        if term or not structured:
            params["query.term"] = term
        if intervention:
            params["query.intr"] = intervention
        if condition:
            params["query.cond"] = condition
        try:
            resp = await self.http_client.get(CT_URL, params=params)
            # AsyncHttpClient.get returns None once retries are exhausted or on
            # a >=400 status; check it explicitly rather than letting the miss
            # surface as an AttributeError below.
            if resp is None:
                raise FetchError("clinical trials request failed")
            studies = [
                s for s in resp.json().get("studies", []) if s and isinstance(s, dict)
            ]
        except Exception:
            logger.warning("Clinical trials search failed for %r", query, exc_info=True)
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        if intervention:
            studies = [s for s in studies if _names_intervention(s, intervention)]
            # Stable: API relevance order holds within each group.
            studies.sort(key=_is_unknown_status)
        articles = [_study_to_article(s) for s in studies[:limit]]

        await self.cache.set(
            cache_key,
            [a.to_dict() for a in articles],
            source="clinical_trials",
        )
        return articles, CacheMetadata(cached=False, cache_age=0)
