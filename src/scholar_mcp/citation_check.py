import asyncio
from typing import Any

from scholar_mcp.config import Settings
from scholar_mcp.ranking import ScoringEngine
from scholar_mcp.resolver import WaterfallResolver

MAX_CLAIMS = 25


def _error_result(identifier: str, message: str) -> dict[str, Any]:
    return {
        "identifier": identifier,
        "verdict": "ERROR",
        "coverage_score": 0.0,
        "best_evidence_sentence": "",
        "resolved_title": "",
        "error": message,
    }


async def _resolve_claim_source(
    resolver: WaterfallResolver,
    identifier: str,
    deep: bool,
) -> tuple[str, str, str]:
    """Returns (title, content, mode).

    mode is "ok" when there is text to verify against, "not_found" when the
    identifier resolved to nothing, and "no_text" when the paper exists but
    carries no abstract (non-deep mode only).
    """
    if deep:
        resp = await resolver.resolve_full_text(identifier)
        if bool(resp.content) and resp.status != "not_found":
            return resp.title, resp.content, "ok"
        return resp.title, resp.content, "not_found"

    meta = await resolver.get_metadata(identifier)
    if meta is None:
        return "", "", "not_found"
    if not meta.abstract:
        return meta.title, meta.abstract, "no_text"
    return meta.title, meta.abstract, "ok"


async def check_claim(
    resolver: WaterfallResolver,
    claim_text: str,
    identifier: str,
    deep: bool,
    settings: Settings,
) -> dict[str, Any]:
    try:
        title, content, mode = await _resolve_claim_source(resolver, identifier, deep)
    except Exception as ex:
        return _error_result(identifier, str(ex))

    if mode != "ok":
        return {
            "identifier": identifier,
            "verdict": "NOT_FOUND" if mode == "not_found" else "NO_TEXT",
            "coverage_score": 0.0,
            "best_evidence_sentence": "",
            "resolved_title": title,
        }

    query_terms = ScoringEngine.tokenize(claim_text)
    coverage = ScoringEngine.text_coverage(query_terms, title, content)
    sentence, _ = ScoringEngine.best_matching_sentence(query_terms, content)

    if coverage >= settings.citation_check_supported_threshold:
        verdict = "SUPPORTED"
    elif coverage >= settings.citation_check_weak_threshold:
        verdict = "WEAK"
    else:
        verdict = "UNSUPPORTED"

    return {
        "identifier": identifier,
        "verdict": verdict,
        "coverage_score": round(coverage, 4),
        "best_evidence_sentence": sentence,
        "resolved_title": title,
    }


async def check_citations(
    resolver: WaterfallResolver,
    claims: list[dict[str, str]],
    deep: bool = False,
) -> list[dict[str, Any]]:
    if len(claims) > MAX_CLAIMS:
        return [
            {
                "identifier": "",
                "verdict": "ERROR",
                "coverage_score": 0.0,
                "best_evidence_sentence": "",
                "resolved_title": "",
                "error": f"Batch size exceeds maximum limit of {MAX_CLAIMS} claims",
            }
        ]

    settings = resolver.settings
    semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def _check_one(claim: dict[str, str]) -> dict[str, Any]:
        async with semaphore:
            return await check_claim(
                resolver,
                claim.get("text", ""),
                claim.get("identifier", ""),
                deep,
                settings,
            )

    return await asyncio.gather(*(_check_one(c) for c in claims))
