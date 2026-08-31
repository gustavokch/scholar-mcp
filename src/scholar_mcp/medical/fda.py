import logging
import re
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.medical.models import DrugLabel, OpenFDAData
from scholar_mcp.utils.http import AsyncHttpClient, FetchError
from scholar_mcp.utils.sqlite_cache import CacheMetadata, SQLiteCacheManager

FDA_LABEL_URL = "https://api.fda.gov/drug/label.json"

logger = logging.getLogger(__name__)

COMMON_DRUG_WORDS = {
    "medication",
    "medicine",
    "drug",
    "pill",
    "tablet",
    "capsule",
    "injection",
    "dose",
    "dosage",
}

PEDIATRIC_TERMS = ("pediatric", "child", "infant", "neonatal", "pediatric dosing")


def is_valid_drug_query(query: str) -> bool:
    trimmed = query.strip()
    lower = trimmed.lower()
    if lower in COMMON_DRUG_WORDS:
        return False
    if len(trimmed) < 3:
        return False
    if re.fullmatch(r"[a-z]+-\d+", lower) or re.search(r"\d{3,}", trimmed):
        return len(trimmed) >= 5
    return True


def _parse_drug_label(raw: dict[str, Any]) -> DrugLabel:
    openfda_raw = raw.get("openfda", {})
    openfda = OpenFDAData.from_dict(openfda_raw)

    def _get_list(key: str) -> list[str]:
        val = raw.get(key, [])
        if isinstance(val, list):
            return [str(v) for v in val if v]
        if isinstance(val, str) and val:
            return [val]
        return []

    effective_time = str(raw.get("effective_time", ""))
    purpose = _get_list("purpose")
    warnings = _get_list("warnings")
    adverse_reactions = _get_list("adverse_reactions")
    drug_interactions = _get_list("drug_interactions")
    dosage_and_administration = _get_list("dosage_and_administration")
    indications_and_usage = _get_list("indications_and_usage")
    contraindications = _get_list("contraindications")
    use_in_specific_populations = _get_list("use_in_specific_populations")
    clinical_pharmacology = _get_list("clinical_pharmacology")

    pediatric_dosing = None
    pediatric_use = _get_list("pediatric_use")
    if pediatric_use:
        pediatric_dosing = "; ".join(pediatric_use)

    pediatric_warnings = None
    boxed_warning = _get_list("boxed_warning")
    if boxed_warning:
        pediatric_warnings = "; ".join(boxed_warning)

    return DrugLabel(
        openfda=openfda,
        effective_time=effective_time,
        purpose=purpose,
        warnings=warnings,
        adverse_reactions=adverse_reactions,
        drug_interactions=drug_interactions,
        dosage_and_administration=dosage_and_administration,
        indications_and_usage=indications_and_usage,
        contraindications=contraindications,
        use_in_specific_populations=use_in_specific_populations,
        clinical_pharmacology=clinical_pharmacology,
        pediatric_dosing=pediatric_dosing,
        pediatric_warnings=pediatric_warnings,
        raw_sections=raw,
    )


class FDAClient:
    def __init__(
        self,
        http_client: AsyncHttpClient,
        cache: SQLiteCacheManager,
        settings: Settings,
    ) -> None:
        self.http_client = http_client
        self.cache = cache
        self.settings = settings

    async def search_drugs(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[list[DrugLabel], CacheMetadata]:
        if not is_valid_drug_query(query):
            return [], CacheMetadata(cached=False, cache_age=0)

        cache_key = f"fda:search:{query}:{limit}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [DrugLabel.from_dict(d) for d in cached_data], meta

        search_queries = [
            f'openfda.brand_name:"{query}"',
            f'openfda.generic_name:"{query}"',
            f'openfda.substance_name:"{query}"',
            f"openfda.brand_name:{query}",
            # Unfielded full-text fallback: a multi-word query can never match
            # a field-restricted quoted phrase, but api.fda.gov's plain search
            # still finds the label (e.g. "ibuprofen dosing children").
            query,
        ]

        all_results: list[DrugLabel] = []
        seen_ndcs: set[str] = set()
        errored = False

        for sq in search_queries:
            try:
                resp = await self.http_client.get(
                    FDA_LABEL_URL,
                    params={"search": sq, "limit": str(limit)},
                    ok_statuses={404},
                )
                if resp is None:
                    raise FetchError("fda label request failed")
                if resp.status_code == 404:
                    # api.fda.gov answers 404 for "no matches found" — a valid
                    # empty answer for this variant, not a fetch failure.
                    continue
                data = resp.json()
                results = data.get("results", [])
                for raw in results:
                    drug = _parse_drug_label(raw)
                    ndc = drug.openfda.product_ndc[0] if drug.openfda.product_ndc else None
                    if ndc:
                        if ndc in seen_ndcs:
                            continue
                        seen_ndcs.add(ndc)
                    all_results.append(drug)
                    if len(all_results) >= limit:
                        break
                if len(all_results) >= limit:
                    break
            except Exception:
                logger.warning("FDA label search failed for %r", sq, exc_info=True)
                errored = True
                continue

        # Caching an empty list produced by a failed fetch would serve that
        # failure for the whole TTL, so skip the write when nothing was found
        # and every query variant errored.
        if errored and not all_results:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [d.to_dict() for d in all_results],
            source="fda",
        )
        return all_results, CacheMetadata(cached=False, cache_age=0, error=errored)

    async def get_drug_by_ndc(
        self,
        ndc: str,
    ) -> tuple[DrugLabel | None, CacheMetadata]:
        cache_key = f"fda:ndc:{ndc}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached:
            if cached_data is not None:
                return DrugLabel.from_dict(cached_data), meta
            return None, meta

        errored = False

        try:
            resp = await self.http_client.get(
                FDA_LABEL_URL,
                params={"search": f'openfda.product_ndc:"{ndc}"', "limit": "1"},
                ok_statuses={404},
            )
            if resp is None:
                raise FetchError("fda ndc request failed")
            # A 404 here is "no such label" — genuine absence, not an error.
            # Skip body parsing: a proxy/CDN error page is not JSON.
            if resp.status_code == 404:
                results = []
            else:
                data = resp.json()
                results = data.get("results", [])
            if results:
                drug = _parse_drug_label(results[0])
                await self.cache.set(cache_key, drug.to_dict(), source="fda")
                return drug, CacheMetadata(cached=False, cache_age=0)
        except Exception:
            logger.warning("FDA exact NDC lookup failed for %r", ndc, exc_info=True)
            errored = True

        # Try fallback query without exact quotes
        try:
            resp = await self.http_client.get(
                FDA_LABEL_URL,
                params={"search": f"openfda.product_ndc:{ndc}", "limit": "1"},
                ok_statuses={404},
            )
            if resp is None:
                raise FetchError("fda ndc fallback request failed")
            if resp.status_code == 404:
                results = []
            else:
                data = resp.json()
                results = data.get("results", [])
            if results:
                drug = _parse_drug_label(results[0])
                await self.cache.set(cache_key, drug.to_dict(), source="fda")
                return drug, CacheMetadata(cached=False, cache_age=0)
        except Exception:
            logger.warning("FDA fallback NDC lookup failed for %r", ndc, exc_info=True)
            errored = True

        # Only a genuine "no such label" answer is worth caching; caching a
        # fetch failure would suppress the lookup for the whole TTL.
        if errored:
            return None, CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(cache_key, None, source="fda")
        return None, CacheMetadata(cached=False, cache_age=0)

    async def search_pediatric_drugs(
        self,
        query: str,
        limit: int = 10,
    ) -> tuple[list[DrugLabel], CacheMetadata]:
        cache_key = f"pediatric_drugs:{query}:{limit}"
        cached_data, meta = await self.cache.get(cache_key)
        if meta.cached and cached_data is not None:
            return [DrugLabel.from_dict(d) for d in cached_data], meta

        base_drugs, base_meta = await self.search_drugs(query, limit=limit * 2)

        pediatric_drugs: list[DrugLabel] = []
        for drug in base_drugs:
            purpose = " ".join(drug.purpose).lower()
            warnings = " ".join(drug.warnings).lower()
            dosage = " ".join(drug.dosage_and_administration).lower()
            indications = " ".join(drug.indications_and_usage).lower()
            populations = " ".join(drug.use_in_specific_populations).lower()

            has_pediatric = (
                bool(drug.pediatric_dosing)
                or bool(drug.pediatric_warnings)
                or any(
                    term in text
                    for term in PEDIATRIC_TERMS
                    for text in (purpose, warnings, dosage, indications, populations)
                )
            )
            if has_pediatric:
                pediatric_drugs.append(drug)

        final_drugs = pediatric_drugs[:limit]
        if base_meta.error and not final_drugs:
            return [], CacheMetadata(cached=False, cache_age=0, error=True)

        await self.cache.set(
            cache_key,
            [d.to_dict() for d in final_drugs],
            source="pediatric_drugs",
        )
        return final_drugs, CacheMetadata(cached=False, cache_age=0, error=base_meta.error)
