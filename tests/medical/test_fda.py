from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.fda import FDAClient, is_valid_drug_query
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

FDA_URL = "https://api.fda.gov/drug/label.json"


def _label_payload(brand, generic=None, ndc="50580-488", dosage=None):
    return {
        "results": [
            {
                "openfda": {
                    "brand_name": [brand],
                    "generic_name": [generic] if generic else [],
                    "manufacturer_name": ["Johnson & Johnson"],
                    "product_ndc": [ndc],
                },
                "effective_time": "20230101",
                "purpose": ["Pain reliever/fever reducer"],
                "dosage_and_administration": [dosage] if dosage else [],
            }
        ]
    }


async def _make_client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = FDAClient(http_client=http_client, cache=cache, settings=settings)
    return client, cache, http_client


def test_is_valid_drug_query():
    assert is_valid_drug_query("medication") is False
    assert is_valid_drug_query("pill") is False
    assert is_valid_drug_query("ab") is False
    assert is_valid_drug_query("aspirin") is True


@respx.mock
async def test_search_drugs_success(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(json=_label_payload("Tylenol", "Acetaminophen"))

    drugs, meta = await client.search_drugs("tylenol", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name == ["Tylenol"]
    assert drugs[0].openfda.generic_name == ["Acetaminophen"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drugs_invalid_query_returns_empty(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    drugs, meta = await client.search_drugs("medication")
    assert drugs == []
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_get_drug_by_ndc(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(json=_label_payload("Advil", ndc="0573-0164"))

    drug, meta = await client.get_drug_by_ndc("0573-0164")
    assert drug is not None
    assert drug.openfda.brand_name == ["Advil"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_drugs(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload(
            "Children's Motrin",
            generic="Ibuprofen",
            ndc="50580-601",
            dosage="Pediatric dosing: 10mg/kg every 6-8 hours for children",
        )
    )

    drugs, meta = await client.search_pediatric_drugs("motrin", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name == ["Children's Motrin"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_drugs_filters_adult_labels(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload("Adult Formula", dosage="Take one tablet with water")
    )

    drugs, meta = await client.search_pediatric_drugs("adult formula", limit=5)
    assert drugs == []
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_drugs_matches_use_in_specific_populations(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    payload = _label_payload("Kid Relief", generic="Acetaminophen", ndc="99999-001")
    payload["results"][0]["use_in_specific_populations"] = [
        "Safety and effectiveness in pediatric patients have been established."
    ]

    respx.get(FDA_URL).respond(json=payload)

    drugs, meta = await client.search_pediatric_drugs("kid relief", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name == ["Kid Relief"]
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drugs_marks_error_and_skips_cache_when_all_queries_fail(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = FDAClient(http_client=http_client, cache=cache, settings=settings)
    try:
        route = respx.get(FDA_URL).mock(side_effect=httpx.ConnectError("boom"))

        drugs, meta = await client.search_drugs("ibuprofen")
        assert drugs == []
        assert meta.error is True

        # Caching a network failure would serve the empty list for the whole
        # TTL, so a second call must re-issue the request.
        after_first = route.call_count
        await client.search_drugs("ibuprofen")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_drug_by_ndc_marks_error_and_skips_cache_on_failure(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = FDAClient(http_client=http_client, cache=cache, settings=settings)
    try:
        route = respx.get(FDA_URL).mock(side_effect=httpx.ConnectError("boom"))

        drug, meta = await client.get_drug_by_ndc("50580-488")
        assert drug is None
        assert meta.error is True

        after_first = route.call_count
        await client.get_drug_by_ndc("50580-488")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_drug_by_ndc_still_caches_genuine_absence(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = FDAClient(http_client=http_client, cache=cache, settings=settings)
    try:
        route = respx.get(FDA_URL).respond(json={"results": []})

        drug, meta = await client.get_drug_by_ndc("50580-488")
        assert drug is None
        assert meta.error is False

        after_first = route.call_count
        drug2, meta2 = await client.get_drug_by_ndc("50580-488")
        assert drug2 is None
        assert meta2.cached is True
        assert route.call_count == after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_drug_by_ndc_treats_404_as_genuine_absence(tmp_path: Path):
    """A 404 NDC miss is 'no such label', not a fetch failure — cacheable."""
    client, cache, http_client = await _make_client(tmp_path)
    route = respx.get(FDA_URL).respond(status_code=404, json={"error": {"code": "NOT_FOUND"}})

    drug, meta = await client.get_drug_by_ndc("99999-999")
    assert drug is None
    assert meta.error is False

    after_first = route.call_count
    drug2, meta2 = await client.get_drug_by_ndc("99999-999")
    assert drug2 is None
    assert meta2.cached is True
    assert route.call_count == after_first
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_pediatric_drugs_propagates_fetch_error(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = FDAClient(http_client=http_client, cache=cache, settings=settings)
    try:
        respx.get(FDA_URL).mock(side_effect=httpx.ConnectError("boom"))

        drugs, meta = await client.search_pediatric_drugs("ibuprofen")
        assert drugs == []
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_drugs_treats_404_as_no_match_not_error(tmp_path: Path):
    """api.fda.gov answers 404 for 'no matches found'; that is a valid empty
    answer, not a fetch failure."""
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(status_code=404, json={"error": {"code": "NOT_FOUND"}})

    drugs, meta = await client.search_drugs("xylophone")
    assert drugs == []
    assert meta.error is False
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drugs_caches_genuine_404_absence(tmp_path: Path):
    client, cache, http_client = await _make_client(tmp_path)
    route = respx.get(FDA_URL).respond(status_code=404, json={"error": {"code": "NOT_FOUND"}})

    drugs, meta = await client.search_drugs("xylophone")
    assert drugs == []
    assert meta.error is False

    after_first = route.call_count
    drugs2, meta2 = await client.search_drugs("xylophone")
    assert drugs2 == []
    assert meta2.cached is True
    assert route.call_count == after_first
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drugs_unfielded_results_must_match_drug_token(tmp_path: Path):
    """The unfielded full-text fallback can match a label that happens to
    contain every word of a multi-word query (e.g. SILICEA matching
    'ibuprofen pediatric dosing children' on 'pediatric' + 'dosage' + 'children'
    in the label body). Filter the fallback to require the lead drug token to
    actually appear in the label, so unrelated labels cannot leak into the
    pediatric_drugs result set.
    """
    client, cache, http_client = await _make_client(tmp_path)

    def _fda_router(request: httpx.Request) -> httpx.Response:
        search = request.url.params.get("search", "")
        if "openfda." in search:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        # Unfielded returns SILICEA — must be filtered out because the lead
        # token 'ibuprofen' is nowhere in this label.
        return httpx.Response(
            200,
            json=_label_payload("SILICEA", "SILICEA", ndc="12345-001"),
        )

    respx.get(FDA_URL).mock(side_effect=_fda_router)

    drugs, meta = await client.search_drugs(
        "ibuprofen pediatric dosing children", limit=5
    )
    assert drugs == []
    assert meta.error is False
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drugs_unfielded_filter_ignores_stopword_lead_tokens(tmp_path: Path):
    """A natural-language query whose first tokens are stopwords ('what',
    'is', 'the') must not defeat the unfielded-fallback filter by
    substring-matching unrelated name fields ('the' matches THEOPHYLLINE).
    Stopwords and short tokens are ignored, and remaining tokens must match
    on word boundaries."""
    client, cache, http_client = await _make_client(tmp_path)

    def _fda_router(request: httpx.Request) -> httpx.Response:
        search = request.url.params.get("search", "")
        if "openfda." in search:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        # Unfielded returns THEOPHYLLINE — today the substring 'the' inside
        # 'theophylline' matches the stopword lead token 'the', letting the
        # junk label through.
        return httpx.Response(
            200,
            json=_label_payload("THEOPHYLLINE", "THEOPHYLLINE", ndc="12345-001"),
        )

    respx.get(FDA_URL).mock(side_effect=_fda_router)

    try:
        drugs, meta = await client.search_drugs(
            "what is the dose of aspirin", limit=5
        )
        assert drugs == []
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_drugs_falls_back_to_unfielded_query(tmp_path: Path):
    """A multi-word query can never match a field-restricted quoted phrase;
    the unfielded full-text variant must still find the label."""
    client, cache, http_client = await _make_client(tmp_path)
    def _fda_router(request: httpx.Request) -> httpx.Response:
        search = request.url.params.get("search", "")
        if "openfda." in search:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        return httpx.Response(200, json=_label_payload("Advil", "Ibuprofen"))

    respx.get(FDA_URL).mock(side_effect=_fda_router)

    drugs, meta = await client.search_drugs("ibuprofen dosing children", limit=5)
    assert len(drugs) == 1
    assert drugs[0].openfda.brand_name == ["Advil"]
    assert meta.error is False
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_drugs_does_not_cache_partial_result_on_variant_error(tmp_path: Path):
    """When some query variants fail but others return results, the partial
    set must be returned (with error=True) but NOT cached — a partial set
    pinned for the whole TTL would hide the missing variants."""
    client, cache, http_client = await _make_client(tmp_path)

    def _fda_router(request: httpx.Request) -> httpx.Response:
        search = request.url.params.get("search", "")
        if "openfda." in search:
            # 400 -> non-retryable failure for the fielded variants
            return httpx.Response(400, json={"error": {"code": "BAD_REQUEST"}})
        return httpx.Response(200, json=_label_payload("Advil", "Ibuprofen"))

    route = respx.get(FDA_URL).mock(side_effect=_fda_router)

    try:
        drugs, meta = await client.search_drugs("ibuprofen dosing children", limit=5)
        assert len(drugs) == 1
        assert meta.error is True

        after_first = route.call_count
        drugs2, meta2 = await client.search_drugs("ibuprofen dosing children", limit=5)
        assert len(drugs2) == 1
        assert meta2.cached is False, "partial result set must not be cached"
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_drugs_multiword_query_drops_unrelated_labels(tmp_path: Path):
    """Reproduces the live defect: the sentence query 'ibuprofen pregnancy
    third trimester FDA label' used to hit the unquoted/unfielded variants
    and return SILICEA-class junk. Every route here serves a label that does
    not name any query token, so the only correct answer is []."""
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload("SILICEA", "SILICEA", ndc="12345-001")
    )

    try:
        drugs, meta = await client.search_drugs(
            "ibuprofen pregnancy third trimester FDA label", limit=5
        )
        assert drugs == []
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_drugs_never_issues_unquoted_field_variant(tmp_path: Path):
    """The unquoted variant `openfda.brand_name:{query}` lets Elasticsearch
    bind the field to the first term only; the rest become unfielded OR
    terms — provably identical to the unfielded fallback while bypassing the
    drug-name guard. It must never be sent."""
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload("SILICEA", "SILICEA", ndc="12345-001")
    )

    query = "ibuprofen pregnancy third trimester FDA label"
    try:
        await client.search_drugs(query, limit=5)

        bad = f"openfda.brand_name:{query}"
        for call in respx.calls:
            assert call.request.url.params.get("search", "") != bad
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_drugs_resolves_drug_name_from_natural_language(tmp_path: Path):
    """A sentence query must produce a quoted, fielded request for the drug
    token (openfda.generic_name:"ibuprofen"), not just unfielded noise."""
    client, cache, http_client = await _make_client(tmp_path)
    respx.get(FDA_URL).respond(
        json=_label_payload("ADVIL", generic="IBUPROFEN", ndc="0573-0164")
    )

    try:
        await client.search_drugs("ibuprofen pregnancy third trimester FDA label", limit=5)

        searches = [c.request.url.params.get("search", "") for c in respx.calls]
        assert any('openfda.generic_name:"ibuprofen"' in s for s in searches)
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_drugs_context_refinement_falls_back_on_404(tmp_path: Path):
    """The context-refined variant (name AND context terms) may 404 when no
    label of the drug mentions the context words; the plain name clause must
    then still return the label."""
    client, cache, http_client = await _make_client(tmp_path)

    def _fda_router(request: httpx.Request) -> httpx.Response:
        search = request.url.params.get("search", "")
        if "trimester" in search:
            return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})
        if 'openfda.generic_name:"ibuprofen"' in search:
            return httpx.Response(
                200,
                json=_label_payload("MOTRIN IB", generic="IBUPROFEN", ndc="0573-0164"),
            )
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}})

    respx.get(FDA_URL).mock(side_effect=_fda_router)

    try:
        drugs, meta = await client.search_drugs(
            "ibuprofen third trimester pregnancy", limit=5
        )
        assert len(drugs) == 1
        assert drugs[0].openfda.generic_name == ["IBUPROFEN"]
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_drug_by_ndc_404_non_json_body_is_absence_not_error(tmp_path: Path):
    """A 404 with a non-JSON body (proxy/CDN error page) is still
    'no such label' — the body must not be parsed, and the result must not
    surface as a fetch error."""
    client, cache, http_client = await _make_client(tmp_path)
    route = respx.get(FDA_URL).respond(status_code=404, text="<html>Gateway timeout</html>")

    drug, meta = await client.get_drug_by_ndc("99999-999")
    assert drug is None
    assert meta.error is False
    assert route.call_count == 2  # quoted + unquoted variants both tried

    await cache.close()
    await http_client.aclose()
