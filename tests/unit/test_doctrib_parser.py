"""DocTrib prassi search answers (ambito P): port of DocTribParser.ParseSearch / ParseEstremi / ExtractContentId on recorded answers."""
from __future__ import annotations

import re
from datetime import date

import pytest

from conftest import fixture_bytes, fixture_text
from dp_it_prassi_grab import doctrib

STRUTTURA_PF = "Direzione Centrale Persone Fisiche, Lavoratori Autonomi ed Enti non Commerciali"


@pytest.mark.unit
def test_should_read_the_seventeen_interpelli_of_october_2025() -> None:
    answer = doctrib.parse_search(fixture_bytes("doctrib-search-2025-10.html"))

    assert answer.declared == 17
    assert len(answer.items) == 17
    assert answer.complete
    first = answer.items[0]
    assert first.prov_guid == "{D12541AF-65CB-4CC5-843D-DF108E6D52E8}"
    assert (first.tipo, first.data, first.numero, first.ente) == ("Interpello", date(2025, 10, 28), "274", "Agenzia delle Entrate")
    assert first.struttura == STRUTTURA_PF
    assert first.oggetto.startswith("Regime speciale per lavoratori impatriati")
    assert [i.numero for i in answer.items] == [str(n) for n in range(274, 257, -1)]
    assert answer.items[-2].struttura == "Divisione Contribuenti"


@pytest.mark.unit
def test_should_report_a_truncated_answer_as_incomplete_like_the_legacy_golden() -> None:
    year = doctrib.parse_search(fixture_bytes("doctrib-legacy-search-anno.raw.html"))
    window = doctrib.parse_search(fixture_bytes("doctrib-legacy-search-fetta-completa.raw.html"))

    assert (year.declared, len(year.items), year.complete) == (263, 50, False)
    assert (window.declared, len(window.items), window.complete) == (41, 41, True)
    assert all(re.fullmatch(r"\{[0-9A-F-]{36}\}", i.prov_guid) for i in year.items)
    assert all(i.tipo and i.ente and i.data != date.min for i in year.items)


@pytest.mark.unit
def test_should_strip_tags_from_the_oggetto_and_decode_the_accents() -> None:
    items = doctrib.parse_search(fixture_bytes("doctrib-legacy-search-anno.raw.html")).items
    text = " ".join(f"{i.oggetto} {i.ente}" for i in items)

    assert not any("<" in i.oggetto or ">" in i.oggetto for i in items)
    assert any(len(i.oggetto) > 10 for i in items)
    assert "Ã" not in text, "ISO-8859-1 read as UTF-8"
    assert re.search("[àèéìòù]", text)


@pytest.mark.unit
def test_should_parse_the_canonical_estremi_with_struttura() -> None:
    got = doctrib.parse_estremi("Interpello del 23/12/2024 n. 269 - Agenzia delle Entrate - Direzione Centrale Persone Fisiche")

    assert got == doctrib.Estremi("Interpello", date(2024, 12, 23), "269", "Agenzia delle Entrate", "Direzione Centrale Persone Fisiche")


@pytest.mark.unit
def test_should_leave_the_number_empty_when_the_estremi_have_none() -> None:
    got = doctrib.parse_estremi("Nota del 05/03/2020 - Agenzia delle Dogane e dei Monopoli - Direzione Antifrode")

    assert (got.tipo, got.data, got.numero, got.ente, got.struttura) == ("Nota", date(2020, 3, 5), None, "Agenzia delle Dogane e dei Monopoli", "Direzione Antifrode")
    assert doctrib.parse_estremi("Interpello del 05/03/2020 n. 1 - Agenzia delle Entrate").struttura is None


@pytest.mark.unit
def test_should_refuse_estremi_it_does_not_recognise() -> None:
    with pytest.raises(ValueError, match="estremi"):
        doctrib.parse_estremi("something else entirely")


@pytest.mark.unit
def test_should_give_an_empty_answer_when_the_page_carries_no_result() -> None:
    answer = doctrib.parse_search(b"<html>errore</html>")

    assert (answer.declared, answer.items, answer.complete) == (0, [], True)


@pytest.mark.unit
def test_should_extract_a_content_id_that_is_not_the_provvedimento_id() -> None:
    legacy = doctrib.extract_content_id(fixture_text("doctrib-legacy-detail-interpello.html", "iso-8859-1"))
    recorded = doctrib.extract_content_id(fixture_text("doctrib-detail-274.html", "iso-8859-1"))

    assert legacy is not None
    assert recorded == "{4FF576C3-4153-41BD-B572-F6AE2F8F4F30}"
    assert recorded != "{D12541AF-65CB-4CC5-843D-DF108E6D52E8}", "the content id is not the id of the provvedimento"
    assert doctrib.extract_content_id("<html><body>nessun documento allegato</body></html>") is None
