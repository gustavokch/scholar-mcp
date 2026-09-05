import logging
import re
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import WHOIndicatorRecord
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

WHO_API_BASE = "https://ghoapi.azureedge.net/api"

logger = logging.getLogger(__name__)

INDICATOR_SYNONYMS: dict[str, list[str]] = {
    "maternal mortality": ["maternal", "mortality", "maternal death"],
    "infant mortality": ["infant", "mortality", "infant death", "child mortality"],
    "life expectancy": ["life expectancy", "expectancy", "life"],
    "mortality rate": ["mortality", "death rate", "mortality rate"],
    "birth rate": ["birth", "fertility", "birth rate"],
    "death rate": ["death", "mortality", "death rate"],
    "population": ["population", "demographics"],
    "health expenditure": ["health", "expenditure", "spending"],
    "immunization": ["immunization", "vaccination", "vaccine"],
    "malnutrition": ["malnutrition", "nutrition", "undernutrition"],
    "diabetes": ["diabetes", "diabetic"],
    "hypertension": ["hypertension", "blood pressure", "high blood pressure"],
    "cancer": ["cancer", "neoplasm", "tumor"],
    "hiv": ["hiv", "aids", "hiv/aids"],
    "tuberculosis": ["tuberculosis", "tb"],
    "malaria": ["malaria"],
    "obesity": ["obesity", "overweight"],
}

WHO_CHILD_HEALTH_INDICATORS = [
    "MDG_0000000029",  # Under-five mortality rate
    "MDG_0000000030",  # Infant mortality rate
    "MDG_0000000031",  # Neonatal mortality rate
    "MDG_0000000032",  # Child mortality rate (1-4 years)
    "MDG_0000000033",  # Measles immunization coverage
    "MDG_0000000034",  # DPT3 immunization coverage
    "WHS4_544",        # Child malnutrition
    "WHS9_86",         # Exclusive breastfeeding
]


# Words that carry no indicator identity. WHO writes its indicator names as
# "<subject> <measure> (per <unit> <denominator>)", so a caller's natural
# phrasing tends to differ from the stored name in exactly these words --
# "mortality rate" against "rates", a spelled-out unit against "(per 100 000
# population)" -- while agreeing on the subject.
_INDICATOR_NOISE = frozenset(
    """a an the of in for and or by per to with at from on
    rate rates ratio ratios level levels value values number numbers
    total estimate estimates estimated prevalence percent percentage
    population populations year years age aged
    live birth births person persons people
    100 000 100000 1000 10000""".split()
)

# Words that name a measure rather than a subject. Almost every mortality
# indicator contains "mortality", so anchoring a query there returns hundreds of
# unrelated rows; the subject beside it is what makes the query specific.
_INDICATOR_MEASURES = frozenset(
    """mortality incidence death deaths dying probability expectancy
    standardized standardised crude adjusted""".split()
)

_INDICATOR_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _is_measure(token: str) -> bool:
    """True for a token that names a measure rather than a subject.

    Tested per hyphen part, because WHO hyphenates measures as readily as
    subjects: "age-standardized" is a measure whose parts are both generic,
    while "under-five" is a subject that must survive.
    """
    parts = token.split("-")
    return all(p in _INDICATOR_MEASURES or p in _INDICATOR_NOISE for p in parts)


def _indicator_tokens(indicator_name: str) -> list[str]:
    """Content tokens of a query, longest first.

    Plurals are folded to their singular so "rates" and "rate" agree, and the
    noise words above are dropped -- they are what make a natural phrasing miss
    a stored name that means the same thing.
    """
    tokens: list[str] = []
    for raw in _INDICATOR_TOKEN_RE.findall(indicator_name.lower()):
        if raw in _INDICATOR_NOISE:
            continue
        # Fold a simple plural ("rates" -> "rate"). Words whose singular is not
        # formed this way ("tuberculosis", "diabetes") end in "is"/"es" after a
        # consonant and must be left alone, or the filter searches for a string
        # that appears in no indicator name at all.
        token = (
            raw[:-1]
            if len(raw) > 3
            and raw.endswith("s")
            and not raw.endswith(("ss", "is", "us", "es"))
            else raw
        )
        if token not in tokens:
            tokens.append(token)
    return tokens


def indicator_filter(indicator_name: str) -> str:
    """OData filter selecting indicators whose name carries every content word.

    A single ``contains()`` over the whole query is a literal substring test, so
    "suicide mortality rate" could not reach "Age-standardized suicide rates
    (per 100 000 population)" -- the stored name carries neither "mortality" nor
    the singular "rate" in that order. Requiring each content token separately
    matches on what the two phrasings agree about, and still excludes an
    unrelated indicator, since every token must appear.
    """
    tokens = _indicator_tokens(indicator_name)
    if not tokens:
        # An all-noise query ("mortality rate") would otherwise produce an empty
        # filter, which selects every indicator WHO publishes. Fall back to the
        # caller's own text, restoring the old literal behaviour for that case.
        tokens = [indicator_name.strip().lower()]
    # Query on the subject alone, then rank locally (see _rank_indicators).
    # WHO names an indicator by subject and measure, and a caller often supplies
    # a measure the stored name does not use -- "suicide mortality rate" against
    # "Age-standardized suicide rates". Requiring every token would reject that
    # match; requiring the subject keeps the candidate set small enough to rank.
    # Generic measure words are skipped so "suicide mortality rate" anchors on
    # "suicide" rather than on "mortality", which every death indicator carries.
    anchor = next((t for t in tokens if not _is_measure(t)), tokens[0])
    return "contains(IndicatorName, '{}')".format(anchor.replace("'", "''"))


def _rank_indicators(
    indicators: list[dict[str, Any]], indicator_name: str
) -> list[dict[str, Any]]:
    """Indicators ordered by how much of the query their name accounts for.

    The subject-only filter is deliberately broad, so the caller's remaining
    words decide which candidates are actually wanted. Ties keep WHO's own
    order, and nothing is discarded -- a low-scoring candidate is still better
    than the arbitrary first three the caller would otherwise have taken.
    """
    wanted = set(_indicator_tokens(indicator_name))
    if not wanted:
        return indicators

    def score(ind: dict[str, Any]) -> float:
        name_tokens = set(_indicator_tokens(str(ind.get("IndicatorName") or "")))
        hits = len(wanted & name_tokens)
        # Extra words are not all equal. A measure or unit word ("live births",
        # "age-standardized") only says how the same quantity is expressed, but
        # an extra subject word changes what is being counted: "In-prison
        # suicide mortality rate" matches every word of "suicide mortality rate"
        # yet measures a different population. Only the latter is penalised, so
        # a fully-qualified name still beats a merely shorter one.
        extra_subjects = sum(
            1 for t in name_tokens - wanted if not _is_measure(t)
        )
        return hits - 1.5 * extra_subjects

    return sorted(indicators, key=score, reverse=True)


def indicator_variations(indicator_name: str) -> list[str]:
    lower = indicator_name.lower()
    variations: list[str] = []
    for key, values in INDICATOR_SYNONYMS.items():
        if key in lower:
            variations.extend(values)
    variations.extend([indicator_name, lower])
    return list(dict.fromkeys(variations))


def extract_age_group(indicator_name: str) -> str:
    age_patterns = [
        r"(\d+\s*(?:-|\s*to\s*)\s*\d+\s*(?:months?|years?|days?))",
        r"(infant|toddler|preschool|school-age|adolescent)",
        r"(under-five|under 5|under-five years)",
        r"(neonatal|newborn)",
    ]
    for pat in age_patterns:
        match = re.search(pat, indicator_name, flags=re.IGNORECASE)
        if match:
            return match.group(0)
    return ""


def _safe_float(value: Any, default: float | None = 0.0) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_indicator_record(
    row: dict[str, Any],
    code: str,
    name: str = "",
) -> WHOIndicatorRecord:
    spatial_dim = str(row.get("SpatialDim") or "Global")
    spatial_dim_type = str(row.get("SpatialDimType") or "Country")
    time_dim = str(row.get("TimeDim") or "")
    time_dim_type = str(row.get("TimeDimType") or "Year")
    numeric_val = row.get("NumericValue")
    val_float = _safe_float(numeric_val, default=None)
    value_str = str(numeric_val) if numeric_val is not None else str(row.get("Value") or "")
    low = _safe_float(row.get("Low"), default=0.0)
    high = _safe_float(row.get("High"), default=0.0)
    unit = str(row.get("Unit") or "")
    sex = str(row.get("Sex") or "")
    age_group = str(row.get("AgeGroup") or extract_age_group(name))
    comments = str(row.get("Comments") or "")
    date = str(row.get("Date") or "")

    return WHOIndicatorRecord(
        indicator_code=code,
        indicator_name=name or code,
        spatial_dim=spatial_dim,
        spatial_dim_type=spatial_dim_type,
        time_dim=time_dim,
        time_dim_type=time_dim_type,
        value=value_str,
        numeric_value=val_float,
        low=low,
        high=high,
        unit=unit,
        age_group=age_group,
        sex=sex,
        comments=comments,
        date=date,
    )


class WHOClient:
    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    async def get_health_statistics(
        self,
        indicator: str,
        country: str | None = None,
        limit: int = 10,
    ) -> tuple[list[WHOIndicatorRecord], CacheMetadata]:
        cache_key = f"who:{indicator}:{country}:{limit}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [WHOIndicatorRecord.from_dict(d) for d in cached_data], meta

        # Find indicators matching search term
        indicators: list[dict[str, Any]] = []
        errored = False

        # 1. Try primary query
        try:
            resp = await self.http_client.get(
                f"{WHO_API_BASE}/Indicator",
                params={
                    "$filter": indicator_filter(indicator),
                    "$format": "json",
                },
            )
            if resp is None:
                raise FetchError("who indicator request failed")
            vals = resp.json().get("value", [])
            if vals:
                indicators = vals
        except Exception:
            logger.warning("WHO indicator lookup failed for %r", indicator, exc_info=True)
            errored = True

        # 2. Fallback to variations if primary query returned no indicators
        if not indicators:
            variations = indicator_variations(indicator)
            for term in variations:
                try:
                    resp = await self.http_client.get(
                        f"{WHO_API_BASE}/Indicator",
                        params={
                            "$filter": indicator_filter(term),
                            "$format": "json",
                        },
                    )
                    if resp is None:
                        raise FetchError("who indicator variation request failed")
                    vals = resp.json().get("value", [])
                    if vals:
                        indicators = vals
                        break
                except Exception:
                    logger.warning(
                        "WHO indicator variation lookup failed for %r", term, exc_info=True
                    )
                    errored = True
                    continue

        if not indicators:
            # Only a genuine "no matching indicator" answer is cacheable; caching
            # a failed lookup would suppress the query for the whole TTL.
            if errored:
                return [], CacheMetadata(cached=False, cache_age=0, error=True)
            await self.cache.set(cache_key, [], source="who")
            return [], CacheMetadata(cached=False, cache_age=0)

        all_records: list[WHOIndicatorRecord] = []

        for ind in _rank_indicators(indicators, indicator)[:3]:
            code = ind.get("IndicatorCode")
            name = ind.get("IndicatorName", "")
            if not code:
                continue

            params: dict[str, str] = {
                "$format": "json",
                "$top": "50",
            }
            if country:
                params["$filter"] = f"SpatialDim eq '{country}'"

            try:
                resp = await self.http_client.get(
                    f"{WHO_API_BASE}/{code}",
                    params=params,
                )
                if resp is None:
                    raise FetchError("who record request failed")
                rows = resp.json().get("value", [])
                # Deduplicate by SpatialDim keeping most recent TimeDim
                by_spatial: dict[str, dict[str, Any]] = {}
                for row in rows:
                    spatial = row.get("SpatialDim") or "Global"
                    time_dim = str(row.get("TimeDim") or "")
                    if spatial not in by_spatial or time_dim > str(by_spatial[spatial].get("TimeDim") or ""):
                        by_spatial[spatial] = row

                for r in by_spatial.values():
                    all_records.append(_parse_indicator_record(r, code, name))
            except Exception:
                logger.warning("WHO record fetch failed for %r", code, exc_info=True)
                errored = True
                continue

        # Sort by TimeDim desc
        all_records.sort(key=lambda r: r.time_dim, reverse=True)
        final_records = all_records[:limit]

        if errored and not final_records:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [r.to_dict() for r in final_records],
            source="who",
        )
        return final_records, CacheMetadata(cached=False, cache_age=0, error=errored)

    async def get_child_health_statistics(
        self,
        indicator: str,
        country: str | None = None,
        limit: int = 10,
    ) -> tuple[list[WHOIndicatorRecord], CacheMetadata]:
        cache_key = f"child_health:{indicator}:{country}:{limit}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [WHOIndicatorRecord.from_dict(d) for d in cached_data], meta

        all_records: list[WHOIndicatorRecord] = []
        errored = False

        for code in WHO_CHILD_HEALTH_INDICATORS:
            params: dict[str, str] = {
                "$format": "json",
                "$top": str(limit),
            }
            if country:
                params["$filter"] = f"SpatialDim eq '{country}'"

            try:
                resp = await self.http_client.get(
                    f"{WHO_API_BASE}/{code}",
                    params=params,
                )
                if resp is None:
                    raise FetchError("who child-health record request failed")
                rows = resp.json().get("value", [])
                for row in rows:
                    record = _parse_indicator_record(row, code)
                    all_records.append(record)
            except Exception:
                logger.warning(
                    "WHO child-health record fetch failed for %r", code, exc_info=True
                )
                errored = True
                continue

        all_records.sort(key=lambda r: r.time_dim, reverse=True)
        final_records = all_records[:limit]

        if errored and not final_records:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [r.to_dict() for r in final_records],
            source="child_health",
        )
        return final_records, CacheMetadata(cached=False, cache_age=0, error=errored)
