"""The Proc-protocol entrypoint: both kinds through `main` with scripted transports, the result keys, null config keys and error codes."""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import main as grab_main
from conftest import PDF_BYTES, DocTribFake, FakeTime, fixture_text, make_docs
from dp_it_prassi_grab import http as http_module
from dp_it_prassi_grab.grab import GrabDeps

DETAIL = fixture_text("doctrib-detail-274.html", "iso-8859-1")
RISOLUZIONI = fixture_text("ade-ottobre-2025-risoluzioni.html")
ADE_ITEM = {
    "key": "ade:risoluzioni:2025:10|v1",
    "meta": {"kind": "ade", "itemId": "ade:risoluzioni:2025:10", "version": "1", "tipologia": "risoluzioni", "anno": 2025, "mese": 10},
}
DOCTRIB_ITEM = {
    "key": "doctrib:interpello:2025-10|v1",
    "meta": {"kind": "doctrib", "itemId": "doctrib:interpello:2025-10", "version": "1", "tipoEstremi": "Interpello", "anno": 2025, "mese": 10},
}


def transport(fake: DocTribFake) -> httpx.MockTransport:
    def route(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "www.agenziaentrate.gov.it":
            return httpx.Response(200, content=PDF_BYTES) if ".pdf" in path else httpx.Response(200, text=RISOLUZIONI)
        if path.endswith("getPrassiDetail.do"):
            return httpx.Response(200, content=DETAIL.encode("iso-8859-1"))
        if path.endswith("getContent.do"):
            return httpx.Response(200, content=PDF_BYTES)
        return fake.route(request)

    return httpx.MockTransport(route)


def request_line(params: dict[str, Any], action: str = "execute") -> str:
    return json.dumps({"_action": action, "_meta": {"executionId": "e1"}, **params})


def run(params: dict[str, Any], tmp_path: Path, fake: DocTribFake | None = None) -> list[dict[str, Any]]:
    clock = FakeTime()
    deps = GrabDeps(transport=transport(fake or DocTribFake(make_docs(2025, 10, {2: 1, 3: 1}))), sleep=clock.sleep, clock=clock.clock)
    out = io.StringIO()
    grab_main.main(io.StringIO(request_line({"dir": str(tmp_path / "raw"), **params}) + "\n"), out, deps)
    return [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.mark.unit
def test_should_grab_both_kinds_and_report_the_result_keys(tmp_path: Path) -> None:
    events = run({"items": [ADE_ITEM, DOCTRIB_ITEM], "maxDocs": 2, "maxItems": 5}, tmp_path)

    result = events[-1]
    assert result["type"] == "result", events
    data = result["data"]
    assert {"downloaded", "skipped_existing", "senza_pdf", "mancanti", "truncated", "files"} <= set(data)
    assert data["downloaded"] == 2 + 2
    assert [f["key"] for f in data["files"]] == [ADE_ITEM["key"], DOCTRIB_ITEM["key"]]
    assert any(e["type"] == "progress" for e in events)


@pytest.mark.unit
def test_should_treat_null_config_keys_as_absent(tmp_path: Path) -> None:
    nulls = {"maxDocs": None, "maxItems": None, "only": None, "baseUrl": None, "delaySeconds": None, "verifyTls": None, "allowedHosts": None, "dryRun": None}

    events = run({"items": [ADE_ITEM, DOCTRIB_ITEM], **nulls}, tmp_path)

    assert events[-1]["type"] == "result", events[-1]
    assert events[-1]["data"]["downloaded"] == 12 + 2


@pytest.mark.unit
def test_should_plan_only_on_a_dry_run(tmp_path: Path) -> None:
    events = run({"items": [ADE_ITEM, DOCTRIB_ITEM], "dryRun": True}, tmp_path)

    data = events[-1]["data"]
    assert data["downloaded"] == 0
    assert {f["status"] for f in data["files"]} == {"planned"}
    assert not (tmp_path / "raw").exists()


@pytest.mark.unit
def test_should_refuse_an_unknown_kind_as_a_config_error(tmp_path: Path) -> None:
    events = run({"items": [{"key": "x", "meta": {"kind": "gazzetta", "itemId": "x"}}]}, tmp_path)

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "config_error"
    assert "kind" in events[-1]["message"]


@pytest.mark.unit
def test_should_refuse_a_missing_dir_and_empty_items(tmp_path: Path) -> None:
    clock = FakeTime()
    out = io.StringIO()
    grab_main.main(io.StringIO(request_line({"items": [ADE_ITEM]}) + "\n"), out, GrabDeps(sleep=clock.sleep, clock=clock.clock))
    assert json.loads(out.getvalue().splitlines()[-1])["code"] == "config_error"

    events = run({"items": []}, tmp_path)
    assert events[-1]["code"] == "config_error"


@pytest.mark.unit
def test_should_pass_verify_tls_to_the_client_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []
    real = http_module.build_client

    def spy(hosts: Any, transport: Any = None, *, verify_tls: bool = True) -> httpx.Client:
        seen.append(verify_tls)
        return real(hosts, transport, verify_tls=verify_tls)

    monkeypatch.setattr("dp_it_prassi_grab.grab.build_client", spy)

    run({"items": [DOCTRIB_ITEM], "verifyTls": False}, tmp_path)
    run({"items": [DOCTRIB_ITEM]}, tmp_path / "again")

    assert seen == [False, True]


@pytest.mark.unit
def test_should_refuse_a_host_that_is_not_allowed(tmp_path: Path) -> None:
    events = run({"items": [ADE_ITEM], "allowedHosts": ["def.finanze.it"]}, tmp_path)

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "config_error"


@pytest.mark.unit
def test_should_end_with_the_worst_error_code_when_items_fail(tmp_path: Path) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="denied") if request.url.host == "www.agenziaentrate.gov.it" else httpx.Response(200, text="x")

    clock = FakeTime()
    out = io.StringIO()
    deps = GrabDeps(transport=httpx.MockTransport(failing), sleep=clock.sleep, clock=clock.clock)
    grab_main.main(io.StringIO(request_line({"dir": str(tmp_path / "raw"), "items": [ADE_ITEM]}) + "\n"), out, deps)

    last = json.loads(out.getvalue().splitlines()[-1])
    assert (last["type"], last["code"]) == ("error", "config_error")
    assert ADE_ITEM["key"] in last["message"]


@pytest.mark.unit
def test_should_stop_on_terminate_and_refuse_unknown_actions() -> None:
    out = io.StringIO()
    code = grab_main.main(io.StringIO(request_line({}, "dance") + "\n" + request_line({}, "terminate") + "\n" + request_line({}, "execute") + "\n"), out)

    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert code == 1
    assert len(lines) == 1
    assert lines[0]["code"] == "config_error"


REPLAY_PAGE = (
    '<div data-analytics-asset-title="Risoluzione n. 118 del 17 febbraio 2022"></div>'
    '<a href="http://replay.test:8765/portale/documents/1/2/Risoluzione_n_118_2022.pdf/8c3d1f2e-0000-4000-8000-000000000001?t=1">scarica</a>'
)
REPLAY_ITEM = {
    "key": "ade:risoluzioni:2022:02|v1",
    "meta": {"kind": "ade", "itemId": "ade:risoluzioni:2022:02", "version": "1", "tipologia": "risoluzioni", "anno": 2022, "mese": 2},
}


def replay_run(params: dict[str, Any], tmp_path: Path, seen: list[str]) -> list[dict[str, Any]]:
    def route(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=PDF_BYTES) if ".pdf" in request.url.path else httpx.Response(200, text=REPLAY_PAGE)

    clock = FakeTime()
    deps = GrabDeps(transport=httpx.MockTransport(route), sleep=clock.sleep, clock=clock.clock)
    out = io.StringIO()
    grab_main.main(io.StringIO(request_line({"dir": str(tmp_path / "raw"), "items": [REPLAY_ITEM], **params}) + "\n"), out, deps)
    return [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.mark.unit
def test_should_download_the_pdf_and_write_the_sidecar_from_a_replay_base_url(tmp_path: Path) -> None:
    seen: list[str] = []

    events = replay_run({"baseUrl": "http://replay.test:8765/portale"}, tmp_path, seen)

    assert events[-1]["type"] == "result", events
    assert events[-1]["data"]["downloaded"] == 1
    pdf = next((tmp_path / "raw").rglob("Risoluzione_n_118_2022.pdf"))
    sidecar = json.loads(Path(str(pdf) + ".meta.json").read_text(encoding="utf-8"))
    assert (sidecar["numero"], sidecar["data"]) == ("118", "2022-02-17")
    assert all(url.startswith("http://replay.test:8765/") for url in seen)


@pytest.mark.unit
def test_should_not_fetch_a_pdf_whose_host_is_neither_allowed_nor_the_replay_host(tmp_path: Path) -> None:
    seen: list[str] = []

    events = replay_run({"baseUrl": "http://other.test:9000/portale"}, tmp_path, seen)

    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "config_error"
    assert not any("replay.test" in url for url in seen)
    assert not list((tmp_path / "raw").rglob("*.pdf"))
