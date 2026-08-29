import re
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import WHOIndicatorRecord
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

WHO_API_BASE = "https://ghoapi.azureedge.net/api"

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
    val_float = float(numeric_val) if numeric_val is not None else None
    value_str = str(numeric_val) if numeric_val is not None else str(row.get("Value") or "")
    low = float(row.get("Low") or 0.0)
    high = float(row.get("High") or 0.0)
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

        # 1. Try primary query
        try:
            resp = await self.http_client.get(
                f"{WHO_API_BASE}/Indicator",
                params={
                    "$filter": f"contains(IndicatorName, '{indicator}')",
                    "$format": "json",
                },
            )
            vals = resp.json().get("value", [])
            if vals:
                indicators = vals
        except Exception:
            pass

        # 2. Fallback to variations if primary query returned no indicators
        if not indicators:
            variations = indicator_variations(indicator)
            for term in variations:
                try:
                    resp = await self.http_client.get(
                        f"{WHO_API_BASE}/Indicator",
                        params={
                            "$filter": f"contains(IndicatorName, '{term}')",
                            "$format": "json",
                        },
                    )
                    vals = resp.json().get("value", [])
                    if vals:
                        indicators = vals
                        break
                except Exception:
                    continue

        if not indicators:
            await self.cache.set(cache_key, [], source="who")
            return [], CacheMetadata(cached=False, cache_age=0)

        all_records: list[WHOIndicatorRecord] = []

        for ind in indicators[:3]:
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
                continue

        # Sort by TimeDim desc
        all_records.sort(key=lambda r: r.time_dim, reverse=True)
        final_records = all_records[:limit]

        await self.cache.set(
            cache_key,
            [r.to_dict() for r in final_records],
            source="who",
        )
        return final_records, CacheMetadata(cached=False, cache_age=0)

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
                rows = resp.json().get("value", [])
                for row in rows:
                    record = _parse_indicator_record(row, code)
                    all_records.append(record)
            except Exception:
                continue

        all_records.sort(key=lambda r: r.time_dim, reverse=True)
        final_records = all_records[:limit]

        await self.cache.set(
            cache_key,
            [r.to_dict() for r in final_records],
            source="child_health",
        )
        return final_records, CacheMetadata(cached=False, cache_age=0)
