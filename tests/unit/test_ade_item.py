"""AdE item with provvedimenti: direct PDFs and detail pages (two requests: the detail, then the PDF); what is lost is counted, never silent."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from conftest import PDF_BYTES, FakeTime, Server, fixture_text
from dp_it_prassi_grab import ade, ade_client
from dp_it_prassi_grab.http import build_client

BASE = "https://www.agenziaentrate.gov.it/portale"
PROVVEDIMENTI = fixture_text("ade-ottobre-2025-provvedimenti.html")
ITEM = {
    "key": "ade:provvedimenti:2025:10|v1",
    "meta": {"kind": "ade", "itemId": "ade:provvedimenti:2025:10", "version": "1", "tipologia": "provvedimenti", "anno": 2025, "mese": 10},
}


def detail_page(slug: str) -> str:
    return f'<html><a href="{BASE}/documents/20143/777/Provvedimento_{slug}.pdf/aaaaaaaa-0000-0000-0000-000000000001?t=1">Provvedimento</a></html>'


def route_with(empty_detail: str | None = None) -> object:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if ".pdf" in path:
            return httpx.Response(200, content=PDF_BYTES)
        if "/portale/-/" in path:
            slug = path.rsplit("/", 1)[1]
            return httpx.Response(200, text="<p>nessun allegato</p>" if slug == empty_detail else detail_page(slug))
        return httpx.Response(200, text=PROVVEDIMENTI)

    return route


def run(route: object, tmp_path: Path) -> tuple[dict[str, object], Server]:
    fake = FakeTime()
    server = Server(route, fake)  # type: ignore[arg-type]
    client = ade_client.AdeClient(build_client(["www.agenziaentrate.gov.it"], server.transport()), ade_client.Pacer(clock=fake.clock, sleep=fake.sleep), BASE, sleep=fake.sleep)
    return ade_client.grab_ade_item(ITEM, tmp_path / "raw", client, ade_client.AdeSettings()), server


@pytest.mark.unit
def test_should_fetch_the_detail_page_and_then_the_pdf_for_a_detail_entry(tmp_path: Path) -> None:
    result, server = run(route_with(), tmp_path)

    urls = server.urls()
    assert len(urls) == 1 + 4 + 3 * 2, "the page, 4 direct PDFs, and detail + PDF for each of the 3 detail entries"
    detail_positions = [i for i, u in enumerate(urls) if "/portale/-/" in u]
    assert all(".pdf" in urls[i + 1] for i in detail_positions), "a PDF request follows each detail request"
    assert result["downloaded"] == 4 + 3


@pytest.mark.unit
def test_should_count_a_detail_page_without_pdf_and_still_take_the_others(tmp_path: Path) -> None:
    entries = ade.parse_provvedimenti(PROVVEDIMENTI).entries
    lost = next(e for e in entries if e.is_detail)

    result, _ = run(route_with(empty_detail=lost.link.rsplit("/", 1)[1]), tmp_path)

    coverage = json.loads((Path(str(result["path"])) / "_coverage.json").read_text(encoding="utf-8"))
    assert coverage["dettagli_persi"] == 1
    assert result["downloaded"] == 4 + 2
    assert result["status"] == "downloaded"
    assert coverage["tipo"] == "provvedimento"
    assert coverage["intestazioni"] == 1
    assert [s["reason"] for s in coverage["skipped"]] == ["dettaglio_senza_pdf"]
    assert lost.title in coverage["skipped"][0]["item_ref"]


@pytest.mark.unit
def test_should_write_a_provvedimento_sidecar_without_number_and_with_the_listing_date(tmp_path: Path) -> None:
    result, _ = run(route_with(), tmp_path)

    sidecars = sorted(Path(str(result["path"])).glob("*.pdf.meta.json"))
    assert len(sidecars) == 7
    meta = [json.loads(p.read_text(encoding="utf-8")) for p in sidecars]
    assert {m["tipo"] for m in meta} == {"provvedimento"}
    assert {m["numero"] for m in meta} == {None}
    assert {m["data"] for m in meta} >= {"2025-10-31", "2025-10-23", "2025-10-09", "2025-10-03"}
    assert all(m["sourceId"] + ".pdf" == p.name.removesuffix(".meta.json") for m, p in zip(meta, sidecars, strict=True))


@pytest.mark.unit
def test_should_record_a_card_without_pdf_as_skipped_with_its_title(tmp_path: Path) -> None:
    page = (
        '<div data-analytics-asset-title="Circolare n. 1 del 20 gennaio 2025"></div>'
        f'<a href="{BASE}/documents/20143/0/Circolare_n_1.pdf/aaaaaaaa-1111-2222-3333-444444444444?t=1">x</a>'
        '<div data-analytics-asset-title="Circolare n. 99 senza allegato"></div><p>no link</p>'
    )

    def route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PDF_BYTES) if ".pdf" in request.url.path else httpx.Response(200, text=page)

    fake = FakeTime()
    server = Server(route, fake)
    client = ade_client.AdeClient(build_client(["www.agenziaentrate.gov.it"], server.transport()), ade_client.Pacer(clock=fake.clock, sleep=fake.sleep), BASE, sleep=fake.sleep)
    item = {"key": "k", "meta": {"kind": "ade", "itemId": "ade:circolari:2025:01", "tipologia": "circolari", "anno": 2025, "mese": 1}}

    result = ade_client.grab_ade_item(item, tmp_path / "raw", client, ade_client.AdeSettings())

    coverage = json.loads((Path(str(result["path"])) / "_coverage.json").read_text(encoding="utf-8"))
    assert (coverage["cards"], coverage["documents"], coverage["senza_pdf"]) == (2, 1, 1)
    assert coverage["skipped"] == [{"item_ref": "ade:circolari:2025:01/Circolare n. 99 senza allegato", "reason": "senza_pdf"}]
