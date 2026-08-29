from pathlib import Path

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
