"""AdE portal client: month pages, PDF download, sidecars and the coverage file, against a scripted transport and a fake clock."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import PDF_BYTES, FakeTime, Server, fixture_text
from dp_it_prassi_grab import ade, ade_client
from dp_it_prassi_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_prassi_grab.http import build_client

BASE = "https://www.agenziaentrate.gov.it/portale"
RISOLUZIONI = fixture_text("ade-ottobre-2025-risoluzioni.html")


def entry(tipologia: str = "risoluzioni", anno: int = 2025, mese: int = 10, **meta: Any) -> dict[str, Any]:
    item_id = f"ade:{tipologia}:{anno}:{mese:02d}"
    return {"key": f"{item_id}|v1", "meta": {"kind": "ade", "itemId": item_id, "version": "1", "tipologia": tipologia, "anno": anno, "mese": mese, **meta}}


def page_route(page: str, status: int = 200) -> Any:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if ".pdf" in path:
            return httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"})
        return httpx.Response(status, text=page if status == 200 else "x")

    return route


def make(route: Any, tmp_path: Path) -> tuple[Server, FakeTime, ade_client.AdeClient, Path]:
    fake = FakeTime()
    server = Server(route, fake)
    http = build_client(["www.agenziaentrate.gov.it"], server.transport())
    client = ade_client.AdeClient(http, ade_client.Pacer(clock=fake.clock, sleep=fake.sleep), BASE, sleep=fake.sleep)
    return server, fake, client, tmp_path / "raw"


@pytest.mark.unit
def test_should_request_the_month_page_with_a_browser_user_agent_and_italian_language(tmp_path: Path) -> None:
    server, _, client, base = make(page_route(RISOLUZIONI), tmp_path)

    ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(max_docs=1))

    first = server.requests[0]
    assert first.url == f"{BASE}/ottobre-2025-risoluzioni"
    assert "Mozilla/5.0" in first.headers["user-agent"]
    assert first.headers["accept-language"].startswith("it")


@pytest.mark.unit
def test_should_pause_at_least_six_tenths_of_a_second_between_requests(tmp_path: Path) -> None:
    server, _, client, base = make(page_route(RISOLUZIONI), tmp_path)

    ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(max_docs=3))

    assert len(server.requests) == 4  # the page and three PDFs
    gaps = [b.at - a.at for a, b in zip(server.requests, server.requests[1:], strict=False)]
    assert all(gap >= 0.6 - 1e-6 for gap in gaps), gaps


@pytest.mark.unit
def test_should_count_a_404_month_as_no_page_and_not_as_an_error(tmp_path: Path) -> None:
    server, _, client, base = make(page_route("", status=404), tmp_path)

    result = ade_client.grab_ade_item(entry("risoluzioni", 2018, 3), base, client, ade_client.AdeSettings())

    assert len(server.requests) == 1
    coverage = json.loads((Path(result["path"]) / "_coverage.json").read_text(encoding="utf-8"))
    assert coverage["pagina_404"] is True
    assert (coverage["cards"], coverage["documents"]) == (0, 0)
    assert result["status"] == "downloaded"


@pytest.mark.unit
def test_should_end_with_a_config_error_naming_akamai_on_a_403(tmp_path: Path) -> None:
    _, _, client, base = make(page_route("blocked", status=403), tmp_path)

    with pytest.raises(GrabError) as caught:
        ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings())

    assert caught.value.code == CONFIG_ERROR
    assert "403" in str(caught.value)
    assert "Akamai" in str(caught.value)


@pytest.mark.unit
def test_should_retry_a_503_twice_and_then_succeed(tmp_path: Path) -> None:
    attempts = {"n": 0}

    def route(request: httpx.Request) -> httpx.Response:
        if ".pdf" in request.url.path:
            return httpx.Response(200, content=PDF_BYTES)
        attempts["n"] += 1
        return httpx.Response(503) if attempts["n"] <= 2 else httpx.Response(200, text=RISOLUZIONI)

    server, fake, client, base = make(route, tmp_path)

    ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(max_docs=1))

    assert attempts["n"] == 3
    assert len(fake.sleeps) >= 2, "a back-off pause precedes each retry"
    assert len(server.requests) == 4


@pytest.mark.unit
def test_should_end_transient_after_three_failed_attempts(tmp_path: Path) -> None:
    server, _, client, base = make(page_route("", status=503), tmp_path)

    with pytest.raises(GrabError) as caught:
        ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings())

    assert caught.value.code == TRANSIENT_ERROR
    assert len(server.requests) == 3


@pytest.mark.unit
def test_should_retry_a_timeout_and_end_transient(tmp_path: Path) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    server, _, client, base = make(route, tmp_path)

    with pytest.raises(GrabError) as caught:
        ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings())

    assert caught.value.code == TRANSIENT_ERROR
    assert len(server.requests) == 3


@pytest.mark.unit
def test_should_reject_a_pdf_response_that_is_html_and_leave_nothing_on_disk(tmp_path: Path) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if ".pdf" in request.url.path:
            return httpx.Response(200, content=b"<html>" + b"x" * 3000 + b"</html>", headers={"content-type": "text/html"})
        return httpx.Response(200, text=RISOLUZIONI)

    _, _, client, base = make(route, tmp_path)

    with pytest.raises(GrabError) as caught:
        ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(max_docs=1))

    assert "PDF" in str(caught.value)
    assert not any(p.suffix in {".pdf", ".tmp"} for p in base.rglob("*"))


@pytest.mark.unit
def test_should_reject_a_tiny_pdf(tmp_path: Path) -> None:
    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.4 tiny") if ".pdf" in request.url.path else httpx.Response(200, text=RISOLUZIONI)

    _, _, client, base = make(route, tmp_path)

    with pytest.raises(GrabError):
        ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(max_docs=1))

    assert not list(base.rglob("*.pdf"))


@pytest.mark.unit
def test_should_remove_a_partial_download(tmp_path: Path) -> None:
    class Broken(httpx.SyncByteStream):
        def __iter__(self) -> Any:
            yield b"%PDF-1.4\n" + b"0" * 2000
            raise httpx.ReadError("connection reset")

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Broken()) if ".pdf" in request.url.path else httpx.Response(200, text=RISOLUZIONI)

    _, _, client, base = make(route, tmp_path)

    with pytest.raises(GrabError) as caught:
        ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(max_docs=1))

    assert caught.value.code == TRANSIENT_ERROR
    assert not [p for p in base.rglob("*") if p.is_file() and p.suffix in {".pdf", ".tmp"}]


@pytest.mark.unit
def test_should_not_download_again_what_is_already_on_disk(tmp_path: Path) -> None:
    server, _, client, base = make(page_route(RISOLUZIONI), tmp_path)

    first = ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings())
    after_first = len(server.requests)
    second = ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings())

    assert first["downloaded"] == 12
    assert (second["downloaded"], second["skipped_existing"]) == (0, 12)
    assert len(server.requests) - after_first == 1, "the second run only reads the month page"


@pytest.mark.unit
def test_should_honour_only_and_max_docs(tmp_path: Path) -> None:
    server, _, client, base = make(page_route(RISOLUZIONI), tmp_path)

    only = ade_client.grab_ade_item(
        entry(), base, client, ade_client.AdeSettings(only=(("risoluzioni", "RIS_n_52_del_7_10_2025"),)),
    )
    assert only["downloaded"] == 1
    assert [p.name for p in (Path(only["path"])).glob("*.pdf")] == ["RIS_n_52_del_7_10_2025.pdf"]

    capped = ade_client.grab_ade_item(entry(), tmp_path / "other", client, ade_client.AdeSettings(max_docs=2))
    assert capped["downloaded"] == 2
    assert sum(1 for u in server.urls() if ".pdf" in u) == 3


@pytest.mark.unit
def test_should_skip_every_document_of_a_type_the_only_list_does_not_name(tmp_path: Path) -> None:
    server, _, client, base = make(page_route(RISOLUZIONI), tmp_path)

    result = ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(only=(("circolari", "x"),)))

    assert result["downloaded"] == 0
    assert not any(".pdf" in u for u in server.urls())


@pytest.mark.unit
def test_should_write_the_exact_sidecar_next_to_each_pdf(tmp_path: Path) -> None:
    _, _, client, base = make(page_route(RISOLUZIONI), tmp_path)

    result = ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(only=(("risoluzioni", "RIS_n_52_del_7_10_2025"),)))

    sidecar = json.loads((Path(result["path"]) / "RIS_n_52_del_7_10_2025.pdf.meta.json").read_text(encoding="utf-8"))
    listed = next(d for d in ade.parse_monthly(RISOLUZIONI, "risoluzione", 2025).docs if d.numero == 52)
    assert sidecar == {
        "kind": "ade",
        "tipo": "risoluzione",
        "titolo": "Risoluzione n. 52 del 7 ottobre 2025",
        "numero": "52",
        "data": "2025-10-07",
        "sourceUrl": listed.url.split("?t=")[0],
        "fonte": "ade-portale",
        "sourceId": "RIS_n_52_del_7_10_2025",
        "ente": "Agenzia delle Entrate",
    }
    assert sidecar["sourceUrl"].startswith(f"{BASE}/documents/20143/8413112/RIS_n_52_del_7_10_2025.pdf/")
    assert "?t=" not in sidecar["sourceUrl"]


@pytest.mark.unit
def test_should_write_the_exact_counts_file(tmp_path: Path) -> None:
    _, _, client, base = make(page_route(RISOLUZIONI), tmp_path)

    result = ade_client.grab_ade_item(entry(), base, client, ade_client.AdeSettings(max_docs=2))

    coverage = json.loads((Path(result["path"]) / "_coverage.json").read_text(encoding="utf-8"))
    assert coverage == {
        "fonte": "ade-portale",
        "tipo": "risoluzione",
        "anno": 2025,
        "mese": 10,
        "cards": 13,
        "listed": 12,
        "documents": 2,
        "intestazioni": 1,
        "senza_pdf": 0,
        "dettagli_persi": 0,
        "pagina_404": False,
        "sample_filter": True,
        "skipped": [],
    }


@pytest.mark.unit
def test_should_treat_json_null_settings_as_absent() -> None:
    settings = ade_client.AdeSettings.from_params({"only": None, "maxDocs": None, "baseUrl": None, "delaySeconds": None})

    assert settings == ade_client.AdeSettings()
    assert settings.base_url == BASE
    assert ade_client.AdeSettings.from_params({"maxDocs": 3, "only": [{"tipo": "circolari", "stem": "A"}]}) == ade_client.AdeSettings(
        max_docs=3, only=(("circolari", "A"),)
    )


@pytest.mark.unit
def test_should_refuse_a_pause_below_six_tenths_of_a_second() -> None:
    with pytest.raises(GrabError) as caught:
        ade_client.AdeSettings.from_params({"delaySeconds": 0.2})

    assert caught.value.code == CONFIG_ERROR
