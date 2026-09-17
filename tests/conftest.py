import pytest

from scholar_mcp.utils.http import AsyncHttpClient


@pytest.fixture(autouse=True)
def reset_limiter_registry():
    """Keep the process-global limiter registry out of the next test.

    Buckets created inside a test's own event loop die with that loop, but a
    limiter built without a running loop -- from a worker thread, or from a
    sync helper -- lands in the loopless map and would otherwise carry its
    token level and ``throttled_until`` for the rest of the session.
    """
    AsyncHttpClient.reset_limiters()
    yield
    AsyncHttpClient.reset_limiters()
