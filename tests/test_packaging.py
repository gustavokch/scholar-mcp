"""Packaging regression tests.

Camoufox is the last-resort browser fallback for guideline scraping and
Sci-Hub PDF fetch (see `_camoufox_scrape`, `_fetch_via_camoufox`). It must be
a core dependency so a plain install ships it; the historical `medical`
extra-only placement left installs without it.
"""

import re
from pathlib import Path

PYPROJECT = Path(__file__).parents[1] / "pyproject.toml"


def _core_dependencies() -> str:
    text = PYPROJECT.read_text()
    match = re.search(r"^dependencies = \[(.*?)^\]", text, re.S | re.M)
    assert match, "pyproject.toml missing core dependencies array"
    return match.group(1)


def test_camoufox_is_core_dependency():
    assert re.search(r'"camoufox', _core_dependencies()), (
        "camoufox must stay a core dependency: browser fallback imports it "
        "unconditionally at runtime"
    )


def test_core_dependencies_parse_into_list():
    entries = re.findall(r'"([^"]+)"', _core_dependencies())
    assert len(entries) >= 7
    assert any(e.startswith("httpx") for e in entries)
