"""DocTrib month slicing: month window, day windows when the cap truncates, recovery by number, honest coverage (declared vs taken)."""
from __future__ import annotations

import pytest

from conftest import DocTribFake, FakeTime, Server, make_docs
from dp_it_prassi_grab import ade_client, doctrib
from dp_it_prassi_grab.http import build_client

TYPE = "Interpello"
ENTE = "Agenzia delle Entrate"


def build(fake: DocTribFake) -> tuple[doctrib.DocTribSession, Server]:
    clock = FakeTime()
    server = fake.server(clock)
    session = doctrib.DocTribSession(
        build_client(["def.finanze.it"], server.transport()), ade_client.Pacer(0.5, clock=clock.clock, sleep=clock.sleep), TYPE, ENTE, sleep=clock.sleep,
    )
    return session, server


def posts(server: Server) -> list[str]:
    return [r.body for r in server.requests if r.method == "POST"]


@pytest.mark.unit
def test_should_take_a_month_whose_counter_equals_the_served_in_one_request() -> None:
    fake = DocTribFake(make_docs(2025, 10, {3: 10, 10: 10, 20: 10, 28: 10}))
    session, server = build(fake)

    month = doctrib.slice_month(session, 2025, 10)

    assert len(posts(server)) == 1
    assert (month.dichiarati, month.presi, month.mancanti, month.troncati) == (40, 40, 0, 0)
    assert len(month.items) == 40


@pytest.mark.unit
def test_should_split_a_month_into_days_when_the_cap_hides_documents() -> None:
    fake = DocTribFake(make_docs(2025, 10, dict.fromkeys(range(1, 24), 3)))  # 69 answers over 23 days
    session, server = build(fake)

    month = doctrib.slice_month(session, 2025, 10)

    assert month.dichiarati == 69
    assert (month.presi, month.mancanti, month.troncati) == (69, 0, 0)
    assert len(posts(server)) == 1 + 31, "the month, then every day of October"
    assert {w["window"] for w in month.windows} >= {"2025-10", "2025-10-01", "2025-10-31"}


@pytest.mark.unit
def test_should_count_a_day_at_the_cap_as_truncated_and_recover_the_gap_by_number() -> None:
    per_day = {5: 60, 6: 10, 7: 10}  # day 5 alone exceeds the 50 served
    fake = DocTribFake(make_docs(2025, 10, per_day))
    session, server = build(fake)

    month = doctrib.slice_month(session, 2025, 10)

    assert month.troncati == 1
    assert month.dichiarati == 80
    assert month.presi == 80, "the ten answers the cap hid are found through the number search"
    assert month.mancanti == 0
    assert month.recuperati == 10
    by_number = [b for b in posts(server) if "numero=" in b and "numero=&" not in b]
    assert len(by_number) <= 10 + 2, "the recovery stops as soon as the count returns"
    assert len(posts(server)) < 1 + 31 + 80


@pytest.mark.unit
def test_should_record_missing_documents_and_not_fail_when_the_source_declares_more_than_it_serves() -> None:
    fake = DocTribFake(make_docs(2025, 10, {2: 6, 3: 6}), declared_override={"2025-10": 15})
    session, _ = build(fake)

    month = doctrib.slice_month(session, 2025, 10)

    assert (month.dichiarati, month.presi, month.mancanti) == (15, 12, 3)
    assert month.warnings
    assert "3" in month.warnings[0]


@pytest.mark.unit
def test_should_send_parole_and_numero_as_empty_keys_in_every_form() -> None:
    fake = DocTribFake(make_docs(2025, 10, {2: 3}))
    session, server = build(fake)

    doctrib.slice_month(session, 2025, 10)

    body = posts(server)[0]
    assert "parole=&" in body or body.endswith("parole=")
    assert "numero=" in body
    form = fake.forms[0]
    assert form["parole"] == ""
    assert form["numero"] == ""
    assert (form["ambitoRicerca"], form["tipoEstremi"], form["ente"]) == ("P", TYPE, ENTE)
    assert (form["meseDataEmissioneDa"], form["annoDataEmissioneDa"], form["giornoDataEmissioneDa"]) == ("10", "2025", "")
    assert (form["tipo_ord"], form["tipoComplessitaRicerca"], form["tipoRicerca"], form["device"], form["js_enabled"]) == ("DATA", "avanzata", "RA", "D", "1")


@pytest.mark.unit
def test_should_use_day_fields_on_a_day_window_and_no_date_on_a_number_search() -> None:
    fake = DocTribFake(make_docs(2025, 10, {5: 60}))
    session, _ = build(fake)

    doctrib.slice_month(session, 2025, 10)

    day_forms = [f for f in fake.forms if f["giornoDataEmissioneDa"]]
    assert day_forms
    assert day_forms[0]["giornoDataEmissioneA"] == day_forms[0]["giornoDataEmissioneDa"]
    number_forms = [f for f in fake.forms if f["numero"]]
    assert number_forms
    assert all(f["giornoDataEmissioneDa"] == "" and f["meseDataEmissioneDa"] == "" and f["annoDataEmissioneDa"] == "2025" for f in number_forms)


@pytest.mark.unit
def test_should_not_take_the_same_document_twice() -> None:
    fake = DocTribFake(make_docs(2025, 10, dict.fromkeys(range(1, 24), 3)))
    session, _ = build(fake)

    month = doctrib.slice_month(session, 2025, 10)

    guids = [i.prov_guid for i in month.items]
    assert len(guids) == len(set(guids))


@pytest.mark.unit
def test_should_stop_after_max_items() -> None:
    fake = DocTribFake(make_docs(2025, 10, dict.fromkeys(range(1, 24), 3)))
    session, server = build(fake)

    month = doctrib.slice_month(session, 2025, 10, max_items=5)

    assert len(month.items) >= 5
    assert len(posts(server)) < 1 + 31
    assert month.sample_limited


@pytest.mark.unit
def test_should_give_an_empty_month_when_the_source_has_nothing() -> None:
    session, server = build(DocTribFake([]))

    month = doctrib.slice_month(session, 2025, 11)

    assert (month.dichiarati, month.presi, month.mancanti) == (0, 0, 0)
    assert len(posts(server)) == 1
