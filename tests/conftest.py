import pytest

from scholar_mcp.utils.http import AsyncHttpClient


@pytest.fixture(autouse=True)
def reset_limiter_registry():
    """Keep the process-global limiter registry out of the next test.

    ``AsyncHttpClient._limiters`` is a class-level dict shared by every
    client in the process. A limiter created inside a test's own event loop
    keeps its token level and ``throttled_until`` after that loop closes,
    so without this reset a slow test could throttle the next one.
    """
    AsyncHttpClient.reset_limiters()
    yield
    AsyncHttpClient.reset_limiters()
