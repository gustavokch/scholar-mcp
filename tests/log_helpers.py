"""Helpers for asserting on log output from the shared HTTP client.

`caplog.records` collects every logger that propagates to root, so asserting
`len(caplog.records) == 0` couples a test to unrelated modules staying quiet and
does not actually pin the claim to the logger under test. These helpers scope the
assertion to `scholar_mcp.utils.http`.
"""

HTTP_LOGGER = "scholar_mcp.utils.http"


def http_records(caplog, level: str | None = None) -> list:
    """Records emitted by the HTTP client, optionally filtered to one level."""
    return [
        r
        for r in caplog.records
        if r.name == HTTP_LOGGER and (level is None or r.levelname == level)
    ]


def assert_no_http_warnings(caplog) -> None:
    """Fail with the offending messages rather than a bare count mismatch."""
    warnings = http_records(caplog, "WARNING")
    assert warnings == [], [r.getMessage() for r in warnings]
