from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.who import WHOClient, indicator_variations
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

GHO_BASE = "https://ghoapi.azureedge.net/api"


def _client(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    return (
        WHOClient(http_client=http_client, cache=cache, settings=settings),
        cache,
        http_client,
    )


def test_indicator_variations():
    assert "expectancy" in indicator_variations("life expectancy")
    assert "life expectancy" in indicator_variations("Life Expectancy")  # original term kept
    assert indicator_variations("made-up-metric") == ["made-up-metric"]


@respx.mock
async def test_get_health_statistics(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)
    respx.get(f"{GHO_BASE}/Indicator").respond(
        json={
            "value": [
                {"IndicatorCode": "WHOSIS_000001", "IndicatorName": "Life expectancy at birth (years)"}
            ]
        }
    )
    respx.get(f"{GHO_BASE}/WHOSIS_000001").respond(
        json={
            "value": [
                {
                    "SpatialDim": "USA",
                    "TimeDim": "2020",
                    "NumericValue": 78.5,
                    "Unit": "years",
                    "Sex": "BTSX",
                },
                {
                    "SpatialDim": "USA",
                    "TimeDim": "2019",
                    "NumericValue": 78.3,
                    "Unit": "years",
                    "Sex": "BTSX",
                },
            ]
        }
    )

    records, meta = await client.get_health_statistics("life expectancy", country="USA", limit=5)
    assert records[0].indicator_code == "WHOSIS_000001"
    assert records[0].numeric_value == 78.5  # most recent year kept per country
    assert records[0].spatial_dim == "USA"
    assert records[0].unit == "years"
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_indicator_discovery_falls_back_to_variations(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)

    indicator_route = respx.get(f"{GHO_BASE}/Indicator")
    # First filter (primary term): empty. Variation filters then hit.
    indicator_route.side_effect = [
        respx.MockResponse(json={"value": []}),
        respx.MockResponse(
            json={"value": [{"IndicatorCode": "WHS9_86", "IndicatorName": "Exclusive breastfeeding"}]}
        ),
    ]
    respx.get(f"{GHO_BASE}/WHS9_86").respond(
        json={"value": [{"SpatialDim": "USA", "TimeDim": "2021", "NumericValue": 0.42}]}
    )

    records, meta = await client.get_health_statistics("breastfeeding")
    assert records[0].indicator_code == "WHS9_86"
    assert indicator_route.call_count == 2
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_get_health_statistics_non_numeric_fields(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)
    respx.get(f"{GHO_BASE}/Indicator").respond(
        json={
            "value": [
                {"IndicatorCode": "WHOSIS_000002", "IndicatorName": "Malformed indicator"}
            ]
        }
    )
    respx.get(f"{GHO_BASE}/WHOSIS_000002").respond(
        json={
            "value": [
                {
                    "SpatialDim": "USA",
                    "TimeDim": "2020",
                    "NumericValue": "",
                    "Low": "N/A",
                    "High": None,
                    "Unit": "years",
                },
            ]
        }
    )

    records, meta = await client.get_health_statistics("malformed indicator", country="USA", limit=5)
    assert records[0].numeric_value is None
    assert records[0].low == 0.0
    assert records[0].high == 0.0
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_get_child_health_statistics(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)
    respx.get(f"{GHO_BASE}/MDG_0000000029").respond(
        json={"value": [{"SpatialDim": "USA", "TimeDim": "2021", "NumericValue": 6.2}]}
    )
    # All other child-health codes return empty values.
    for code in (
        "MDG_0000000030",
        "MDG_0000000031",
        "MDG_0000000032",
        "MDG_0000000033",
        "MDG_0000000034",
        "WHS4_544",
        "WHS9_86",
    ):
        respx.get(f"{GHO_BASE}/{code}").respond(json={"value": []})

    records, meta = await client.get_child_health_statistics("mortality", limit=5)
    assert records[0].indicator_code == "MDG_0000000029"
    assert records[0].numeric_value == 6.2
    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_get_health_statistics_marks_error_and_skips_cache_on_failure(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)
    try:
        route = respx.get(url__startswith=GHO_BASE).mock(
            side_effect=httpx.ConnectError("boom")
        )

        records, meta = await client.get_health_statistics("life expectancy")
        assert records == []
        assert meta.error is True

        # A failed indicator lookup must not be cached as "no such indicator".
        after_first = route.call_count
        await client.get_health_statistics("life expectancy")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_get_child_health_statistics_marks_error_on_failure(tmp_path: Path):
    client, cache, http_client = _client(tmp_path)
    try:
        route = respx.get(url__startswith=GHO_BASE).mock(
            side_effect=httpx.ConnectError("boom")
        )

        records, meta = await client.get_child_health_statistics("mortality")
        assert records == []
        assert meta.error is True

        after_first = route.call_count
        await client.get_child_health_statistics("mortality")
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


# --- Indicator name matching ------------------------------------------------
#
# The GHO filter used to be one literal substring test over the whole query:
# ``contains(IndicatorName, 'suicide mortality rate')``. WHO names the suicide
# indicators "Age-standardized suicide rates (per 100 000 population)" and
# "Crude suicide rates (per 100 000 population)", so that phrasing is not a
# substring of either and the lookup returned nothing at all -- while the bare
# word "suicide" matched both. Whether a caller found the data came down to how
# it happened to word the query.

SUICIDE_NAMES = [
    "Age-standardized suicide rates (per 100 000 population)",
    "Crude suicide rates (per 100 000 population)",
]


def _matches(filter_expr: str, name: str) -> bool:
    """Evaluate an OData ``contains(...) and contains(...)`` filter locally."""
    import re

    terms = re.findall(r"contains\(IndicatorName, '([^']*)'\)", filter_expr)
    assert terms, f"no contains() terms in {filter_expr!r}"
    return all(t.lower() in name.lower() for t in terms)


def test_indicator_filter_matches_despite_extra_query_words():
    """A caller writing "suicide mortality rate" must still reach an indicator
    named "...suicide rates...". Words the name does not carry are dropped
    rather than failing the whole match."""
    from scholar_mcp.medical.who import indicator_filter

    expr = indicator_filter("suicide mortality rate")
    assert all(_matches(expr, name) for name in SUICIDE_NAMES)


def test_indicator_filter_matches_the_official_indicator_name():
    """The indicator's own name must match itself -- it did not before, because
    unit punctuation and the singular/plural of "rate" made the literal
    substring test fail."""
    from scholar_mcp.medical.who import indicator_filter

    expr = indicator_filter("Age-standardized suicide rates (per 100 000 population)")
    assert _matches(expr, SUICIDE_NAMES[0])


def test_indicator_filter_still_matches_a_bare_term():
    """The phrasing that always worked must keep working."""
    from scholar_mcp.medical.who import indicator_filter

    assert all(_matches(indicator_filter("suicide"), n) for n in SUICIDE_NAMES)


def test_indicator_filter_does_not_match_an_unrelated_indicator():
    """Dropping noise words must not turn the filter into a wildcard: every
    surviving token still has to appear in the name."""
    from scholar_mcp.medical.who import indicator_filter

    expr = indicator_filter("tuberculosis incidence")
    assert not any(_matches(expr, name) for name in SUICIDE_NAMES)


def test_indicator_filter_keeps_a_query_that_is_all_noise_words():
    """"mortality rate" is entirely stopwords and unit noise under the token
    rule. Emptying the filter would match every indicator WHO publishes, so the
    query must fall back to its own text."""
    from scholar_mcp.medical.who import indicator_filter

    expr = indicator_filter("mortality rate")
    assert _matches(expr, "Maternal mortality ratio (per 100 000 live births)") or _matches(
        expr, "Adult mortality rate (probability of dying between 15 and 60 years)"
    )
    assert not _matches(expr, "Population using safely managed drinking-water services (%)")


def test_indicator_filter_escapes_quotes():
    """A query with an apostrophe is interpolated into an OData string literal;
    unescaped it produces a malformed filter."""
    from scholar_mcp.medical.who import indicator_filter

    expr = indicator_filter("women's health")
    assert "''" in expr or "'" not in expr.split("contains(IndicatorName, '")[1].rsplit("')", 1)[0]


def test_indicator_filter_anchors_on_the_subject_not_the_measure():
    """"mortality" appears in a large share of WHO's indicator names, so
    anchoring there returns everything about death. The subject beside it is
    what makes the query specific."""
    from scholar_mcp.medical.who import indicator_filter

    assert indicator_filter("suicide mortality rate") == indicator_filter("suicide")
    assert "neonatal" in indicator_filter("neonatal mortality rate")
    assert "tuberculosis" in indicator_filter("tuberculosis incidence rate")


def test_indicator_filter_keeps_hyphenated_subjects_intact():
    """"under-five" is one subject. Split on the hyphen it anchors on "under",
    a word that appears in unrelated indicator names and identifies nothing."""
    from scholar_mcp.medical.who import indicator_filter

    expr = indicator_filter("under-five mortality rate")
    assert _matches(
        expr, "Under-five mortality rate (probability of dying by age 5 per 1000 live births)"
    )
    assert not _matches(expr, "Population under the age of 25 (%)")


def test_rank_indicators_puts_the_closest_name_first():
    """The subject filter is broad on purpose, so the caller's remaining words
    decide which candidate is wanted -- an adult indicator must not win a query
    that says "neonatal"."""
    from scholar_mcp.medical.who import _rank_indicators

    candidates = [
        {"IndicatorCode": "X", "IndicatorName": "Adult mortality rate"},
        {"IndicatorCode": "Y", "IndicatorName": "Neonatal mortality rate (per 1000 live births)"},
    ]
    ranked = _rank_indicators(candidates, "neonatal mortality rate")
    assert ranked[0]["IndicatorCode"] == "Y"


def test_rank_indicators_keeps_every_candidate():
    """Ranking reorders; it must not drop candidates, or a query whose wording
    matches nothing well would return empty instead of merely imperfect."""
    from scholar_mcp.medical.who import _rank_indicators

    candidates = [
        {"IndicatorCode": "A", "IndicatorName": "Something unrelated"},
        {"IndicatorCode": "B", "IndicatorName": "Neonatal mortality rate"},
    ]
    assert len(_rank_indicators(candidates, "neonatal")) == 2


def test_rank_indicators_prefers_the_general_indicator_over_a_narrower_one():
    """WHO publishes sub-population variants whose names are supersets of the
    general one: "In-prison suicide mortality rate" contains every word of
    "suicide mortality rate", while the indicator actually wanted --
    "Age-standardized suicide rates" -- matches fewer of them. Scoring on
    overlap alone therefore ranks the narrow variant first and, with only the
    top few candidates queried, the general indicator is never fetched.
    """
    from scholar_mcp.medical.who import _rank_indicators

    candidates = [
        {
            "IndicatorCode": "PRISON_D3",
            "IndicatorName": (
                "In-prison suicide mortality rate "
                "(per 100 000 incarcerated persons per year)"
            ),
        },
        {
            "IndicatorCode": "MH_12",
            "IndicatorName": "Age-standardized suicide rates (per 100 000 population)",
        },
    ]
    ranked = _rank_indicators(candidates, "suicide mortality rate")
    assert ranked[0]["IndicatorCode"] == "MH_12"
