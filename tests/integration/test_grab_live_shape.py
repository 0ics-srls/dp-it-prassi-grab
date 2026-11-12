"""LIVE shape check of both sources: one real month page of the AdE portal and one real DocTrib window, a handful of documents each.

Never runs in CI and never in the image build: it needs ``P80_LIVE=1`` and reaches the real hosts (browser User-Agent, 0.6 s between requests).
Command (from the class directory, after ``pip install -r requirements-dev.txt``)::

    P80_LIVE=1 python -m pytest -m integration tests/integration/test_grab_live_shape.py -q -s

It is the proof used by plan-d block 28 (reachability from the host and from a ProcMon container); it writes into pytest's tmp_path only.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

import main as grab_main

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("P80_LIVE") != "1", reason="live check: set P80_LIVE=1 to reach the real hosts"),
]


def _run(params: dict[str, Any]) -> list[dict[str, Any]]:
    out = io.StringIO()
    line = json.dumps({"_action": "execute", "_meta": {"executionId": "live"}, **params})
    grab_main.main(io.StringIO(line + "\n"), out)
    return [json.loads(row) for row in out.getvalue().splitlines()]


def test_should_take_one_risoluzione_of_a_real_ade_month(tmp_path: Path) -> None:
    item = {"key": "ade:risoluzioni:2025:10|live", "meta": {"kind": "ade", "itemId": "ade:risoluzioni:2025:10", "tipologia": "risoluzioni", "anno": 2025, "mese": 10}}

    events = _run({"dir": str(tmp_path / "raw"), "items": [item], "maxDocs": 1})

    assert events[-1]["type"] == "result", events[-1]
    assert events[-1]["data"]["downloaded"] == 1
    assert list((tmp_path / "raw").rglob("*.pdf"))


def test_should_take_one_interpello_of_a_real_doctrib_month(tmp_path: Path) -> None:
    item = {"key": "doctrib:interpello:2025-10|live", "meta": {"kind": "doctrib", "itemId": "doctrib:interpello:2025-10", "tipoEstremi": "Interpello", "anno": 2025, "mese": 10}}

    events = _run({"dir": str(tmp_path / "raw"), "items": [item], "maxItems": 1})

    assert events[-1]["type"] == "result", events[-1]
    assert events[-1]["data"]["downloaded"] == 1
