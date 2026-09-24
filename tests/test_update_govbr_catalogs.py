"""The offline seed writer must never write a partial crawl."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "update_govbr_catalogs.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("update_govbr_catalogs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(n: int) -> dict:
    return {f"row-{i}": {"record_id": f"row-{i}"} for i in range(n)}


def _stub(script, monkeypatch, catalog: dict, complete: bool) -> None:
    async def _build(name: str):
        return catalog, complete

    monkeypatch.setattr(script, "build_catalog", _build)


@pytest.mark.parametrize("name", ["az", "pcdt"])
def test_incomplete_crawl_writes_nothing(script, monkeypatch, tmp_path, name):
    _stub(script, monkeypatch, _rows(500), complete=False)
    out = tmp_path / "seed.json"

    assert script.main(["--catalog", name, "--output", str(out)]) == 1
    assert not out.exists()


def test_complete_but_tiny_crawl_writes_nothing(script, monkeypatch, tmp_path):
    _stub(script, monkeypatch, _rows(script.MIN_EXPECTED_ROWS - 1), complete=True)
    out = tmp_path / "seed.json"

    assert script.main(["--catalog", "az", "--output", str(out)]) == 1
    assert not out.exists()


@pytest.mark.parametrize("name", ["az", "pcdt"])
def test_complete_crawl_is_written(script, monkeypatch, tmp_path, name):
    rows = _rows(script.MIN_EXPECTED_ROWS)
    _stub(script, monkeypatch, rows, complete=True)
    out = tmp_path / "seed.json"

    assert script.main(["--catalog", name, "--output", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == rows


def test_each_catalog_defaults_to_its_own_seed(script):
    assert script.CATALOGS["az"][2].name == "govbr_az_catalog.json"
    assert script.CATALOGS["pcdt"][2].name == "govbr_pcdt_catalog.json"
