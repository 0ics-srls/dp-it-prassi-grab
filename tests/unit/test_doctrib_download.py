"""DocTrib item: month slicing, detail page, content id, PDF download named by the legacy convention, sidecar, coverage."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import PDF_BYTES, Doc, DocTribFake, FakeTime, Server, fixture_text, make_docs
from dp_it_prassi_grab import ade_client, doctrib, doctrib_download
from dp_it_prassi_grab.errors import TRANSIENT_ERROR, GrabError
from dp_it_prassi_grab.http import build_client

ITEM = {
    "key": "doctrib:interpello:2025-10|v1",
    "meta": {"kind": "doctrib", "itemId": "doctrib:interpello:2025-10", "version": "1", "tipoEstremi": "Interpello", "anno": 2025, "mese": 10},
}
DETAIL_TEMPLATE = fixture_text("doctrib-detail-274.html", "iso-8859-1")


def content_id(guid: str) -> str:
    return "{C" + guid[2:]  # a different id from the one of the provvedimento


class World:
    """A scripted DocTrib with detail and content endpoints; ``broken`` maps a prov-GUID to what its detail page does."""

    def __init__(self, docs: list[Doc], broken: dict[str, str] | None = None) -> None:
        self.fake = DocTribFake(docs)
        self.broken = broken or {}
        self.clock = FakeTime()
        self.detail_calls: list[str] = []
        self.content_calls: list[str] = []

    def route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("getPrassiDetail.do"):
            guid = request.url.params["id"]
            self.detail_calls.append(guid)
            mode = self.broken.get(guid)
            if mode == "nolink":
                return httpx.Response(200, text="<html><body>nessun documento allegato</body></html>")
            if mode == "500":
                return httpx.Response(500)
            html = DETAIL_TEMPLATE.replace("{4FF576C3-4153-41BD-B572-F6AE2F8F4F30}", content_id(guid))
            return httpx.Response(200, content=html.encode("iso-8859-1"), headers={"content-type": "text/html;charset=ISO-8859-1"})
        if path.endswith("getContent.do"):
            requested = request.url.params["id"]
            self.content_calls.append(requested)
            if requested in {d.guid for d in self.fake.docs}:
                return httpx.Response(200, text="<html>errore: id non valido</html>")  # the legacy finding: the prov-GUID does not open the document
            return httpx.Response(
                200, content=PDF_BYTES, headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="Risposta n. 274_2025 del 28 ottobre 2025.pdf"'},
            )
        return self.fake.route(request)

    def session(self) -> tuple[doctrib.DocTribSession, Server]:
        server = Server(self.route, self.clock)
        pacer = ade_client.Pacer(0.5, clock=self.clock.clock, sleep=self.clock.sleep)
        session = doctrib.DocTribSession(build_client(["def.finanze.it"], server.transport()), pacer, "Interpello", "Agenzia delle Entrate", sleep=self.clock.sleep)
        return session, server


def three_docs() -> list[Doc]:
    return make_docs(2025, 10, {2: 1, 3: 1, 9: 1})


@pytest.mark.unit
def test_should_open_the_pdf_with_the_content_id_and_not_with_the_provvedimento_id(tmp_path: Path) -> None:
    world = World(three_docs())
    session, _ = world.session()

    result = doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions())

    assert result["downloaded"] == 3
    assert len(world.content_calls) == 3
    assert all(c.startswith("{C") for c in world.content_calls), "the request uses the content id found in the detail page"
    assert not set(world.content_calls) & {d.guid for d in world.fake.docs}


@pytest.mark.unit
def test_should_name_the_pdf_by_the_legacy_convention_and_not_by_content_disposition(tmp_path: Path) -> None:
    world = World(three_docs())
    session, _ = world.session()

    result = doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions())

    names = sorted(p.name for p in Path(result["path"]).glob("*.pdf"))
    guid = world.fake.docs[0].guid.strip("{}")
    assert names[0] == f"Interpello_2025-10-02_1__{guid}.pdf"
    assert len(names) == 3
    assert not any("Risposta" in n for n in names)


@pytest.mark.unit
def test_should_write_the_exact_sidecar_with_the_detail_page_as_source_url(tmp_path: Path) -> None:
    world = World(three_docs())
    session, _ = world.session()

    result = doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions())

    first = world.fake.docs[0]
    sidecar = json.loads((Path(result["path"]) / f"Interpello_2025-10-02_1__{first.guid.strip('{}')}.pdf.meta.json").read_text(encoding="utf-8"))
    assert sidecar == {
        "kind": "doctrib",
        "tipo": "interpello",
        "titolo": "Oggetto dell'interpello 1",
        "numero": "1",
        "data": "2025-10-02",
        "struttura": "Direzione Centrale Persone Fisiche, Lavoratori Autonomi ed Enti non Commerciali",
        "ente": "Agenzia delle Entrate",
        "sourceUrl": f"https://def.finanze.it/DocTribFrontend/getPrassiDetail.do?id={first.guid}",
        "fonte": "doctrib",
        "sourceId": first.guid.strip("{}"),
    }


@pytest.mark.unit
def test_should_name_a_document_without_number_nd(tmp_path: Path) -> None:
    docs = [*three_docs(), doctrib_doc_without_number()]
    world = World(docs)
    session, _ = world.session()

    result = doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions())

    assert any("_nd__" in p.name for p in Path(result["path"]).glob("*.pdf"))
    nd = json.loads(next(Path(result["path"]).glob("*_nd__*.meta.json")).read_text(encoding="utf-8"))
    assert nd["numero"] is None


def doctrib_doc_without_number() -> Doc:
    from datetime import date  # noqa: PLC0415



    return Doc(date(2025, 10, 14), None, "{00000000-0000-4000-8000-0000000000ff}")


@pytest.mark.unit
def test_should_count_a_detail_page_without_link_as_senza_pdf_and_continue(tmp_path: Path) -> None:
    docs = three_docs()
    world = World(docs, broken={docs[1].guid: "nolink"})
    session, _ = world.session()

    result = doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions())

    assert (result["downloaded"], result["senza_pdf"]) == (2, 1)
    coverage = json.loads((Path(result["path"]) / "_coverage.json").read_text(encoding="utf-8"))
    assert coverage["senza_pdf"] == 1
    assert coverage["skipped"] == [{"item_ref": f"doctrib:interpello:2025-10/{docs[1].guid.strip('{}')}", "reason": "senza_pdf"}]


@pytest.mark.unit
def test_should_skip_what_is_on_disk_on_a_second_run(tmp_path: Path) -> None:
    world = World(three_docs())
    session, _ = world.session()
    base = tmp_path / "raw"
    doctrib_download.grab_doctrib_item(ITEM, base, session, doctrib_download.DocTribOptions())
    detail_before, content_before = len(world.detail_calls), len(world.content_calls)

    second = doctrib_download.grab_doctrib_item(ITEM, base, session, doctrib_download.DocTribOptions())

    assert (second["downloaded"], second["skipped_existing"]) == (0, 3)
    assert (len(world.detail_calls), len(world.content_calls)) == (detail_before, content_before)


@pytest.mark.unit
def test_should_honour_the_only_list_of_numbers(tmp_path: Path) -> None:
    world = World(three_docs())
    session, _ = world.session()

    result = doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions(only=("2",)))

    assert result["downloaded"] == 1
    assert len(world.detail_calls) == 1
    coverage = json.loads((Path(result["path"]) / "_coverage.json").read_text(encoding="utf-8"))
    assert coverage["sample_filter"] is True


@pytest.mark.unit
def test_should_honour_max_items(tmp_path: Path) -> None:
    world = World(three_docs())
    session, _ = world.session()

    result = doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions(max_items=2))

    assert result["downloaded"] == 2


@pytest.mark.unit
def test_should_write_the_coverage_of_the_month(tmp_path: Path) -> None:
    world = World(make_docs(2025, 10, {2: 6, 3: 6}))
    world.fake.declared_override["2025-10"] = 15
    session, _ = world.session()

    result = doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions())

    coverage = json.loads((Path(result["path"]) / "_coverage.json").read_text(encoding="utf-8"))
    assert (coverage["fonte"], coverage["tipo"], coverage["anno"], coverage["mese"]) == ("doctrib", "interpello", 2025, 10)
    assert (coverage["dichiarati"], coverage["presi"], coverage["mancanti"], coverage["troncati"]) == (15, 12, 3, 0)
    assert coverage["sample_filter"] is False
    assert result["mancanti"] == 3
    assert result["status"] == "downloaded", "missing documents are a warning metric, not a failure"


@pytest.mark.unit
def test_should_write_the_others_and_end_transient_when_one_detail_page_keeps_failing(tmp_path: Path) -> None:
    docs = three_docs()
    world = World(docs, broken={docs[0].guid: "500"})
    session, _ = world.session()

    with pytest.raises(GrabError) as caught:
        doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions())

    assert caught.value.code == TRANSIENT_ERROR
    assert len(list((tmp_path / "raw").rglob("*.pdf"))) == 2
    assert docs[0].guid.strip("{}") in str(caught.value)


@pytest.mark.unit
def test_should_reject_a_content_answer_that_is_not_a_pdf(tmp_path: Path) -> None:
    docs = three_docs()
    world = World(docs)
    original = world.route

    def html_content(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("getContent.do"):
            return httpx.Response(200, text="<html>" + "x" * 3000 + "</html>")
        return original(request)

    world.route = html_content  # type: ignore[method-assign]
    session, _ = world.session()

    with pytest.raises(GrabError):
        doctrib_download.grab_doctrib_item(ITEM, tmp_path / "raw", session, doctrib_download.DocTribOptions())

    assert not list((tmp_path / "raw").rglob("*.pdf"))


@pytest.mark.unit
def test_should_validate_the_item_meta() -> None:
    bad: dict[str, Any] = {"kind": "doctrib", "itemId": "doctrib:interpello:2025-13", "anno": 2025, "mese": 13}

    with pytest.raises(GrabError):
        doctrib_download.validate_meta("k", bad)
    assert parse_qs("a=1")  # keeps the import used
