"""AdE portal month pages: circolari / risoluzioni (ports of AdeMonthlyParser) and provvedimenti (AdeProvvedimentiParser), on real recorded pages."""
from __future__ import annotations

from pathlib import Path

import pytest

from dp_it_prassi_grab import ade

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PDF_BASE = "https://www.agenziaentrate.gov.it/portale/documents"
DETAIL_BASE = "https://www.agenziaentrate.gov.it/portale/-"


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _card(title: str, link: str | None) -> str:
    return f'<div data-analytics-asset-title="{title}"></div>' + (f'<a href="{link}">scarica</a>' if link else "<p>no link</p>")


def _prov_card(title: str, icon: str, link: str) -> str:
    return (
        f'<div class="journal-content-article " data-analytics-asset-id="1" data-analytics-asset-title="{title}" data-analytics-asset-type="web-content">'
        f'<div class="indcart_gn"><p><i class="fa {icon}" aria-hidden="true"></i><strong> {title}</strong></p><a href="{link}">apri</a></div></div>'
    )


# ---- circolari / risoluzioni ----


@pytest.mark.unit
def test_should_take_the_twelve_risoluzioni_of_october_2025_with_their_own_pdf() -> None:
    parsed = ade.parse_monthly(_page("ade-ottobre-2025-risoluzioni.html"), "risoluzione", 2025)

    assert parsed.count == ade.ParseCount(card=13, documenti=12, intestazioni=1, senza_pdf=0)
    assert sorted(d.numero for d in parsed.docs if d.numero is not None) == [50, *range(52, 63)]
    by_number = {d.numero: d for d in parsed.docs}
    assert by_number[52].file_name == "RIS_n_52_del_7_10_2025.pdf"
    assert by_number[62].file_name == "Risoluzione n. 62_2025.pdf"
    assert all(d.url.startswith("https://www.agenziaentrate.gov.it/portale/documents/") and ".pdf/" in d.url and "?t=" in d.url for d in parsed.docs)
    assert len({d.url for d in parsed.docs}) == 12


@pytest.mark.unit
def test_should_take_both_circolari_of_july_2025_and_count_the_foglia_header() -> None:
    parsed = ade.parse_monthly(_page("ade-luglio-2025-circolari.html"), "circolare", 2025)

    assert sorted(d.numero for d in parsed.docs if d.numero is not None) == [10, 11]
    assert parsed.count == ade.ParseCount(card=3, documenti=2, intestazioni=1, senza_pdf=0)
    assert not any("foglia" in d.oggetto.lower() for d in parsed.docs)


@pytest.mark.unit
def test_should_treat_an_empty_month_as_zero_documents_and_not_as_an_error() -> None:
    parsed = ade.parse_monthly(_page("ade-ottobre-2025-circolari.html"), "circolare", 2025)

    assert parsed.docs == []
    assert parsed.count == ade.ParseCount(card=1, documenti=0, intestazioni=1, senza_pdf=0)


@pytest.mark.unit
def test_should_take_every_title_shape_of_the_real_risoluzioni_of_may_2021() -> None:
    parsed = ade.parse_monthly(_page("ade-risoluzioni-2021-05.reale.html"), "risoluzione", 2021)

    assert len(parsed.docs) == 10
    assert len({d.url for d in parsed.docs}) == 10
    numbers = {d.numero for d in parsed.docs}
    assert {39, 37, 35, 34} <= numbers  # without "n.", abbreviated "Ris", double space, underscores
    for doc in parsed.docs:
        if doc.numero is not None:
            assert str(doc.numero) in doc.file_name, "the window of a card must not leak into the next card"


@pytest.mark.unit
def test_should_take_the_underscore_circolare_of_may_2021_and_the_canonical_risoluzioni_of_2024() -> None:
    circolari = ade.parse_monthly(_page("ade-circolari-2021-05.reale.html"), "circolare", 2021)
    risoluzioni = ade.parse_monthly(_page("ade-risoluzioni-2024-05.reale.html"), "risoluzione", 2024)

    assert sorted(d.numero for d in circolari.docs if d.numero is not None) == [4, 5]
    assert sorted(d.numero for d in risoluzioni.docs if d.numero is not None) == [22, 23, 24, 25, 26, 27, 28]


@pytest.mark.unit
def test_should_count_a_card_without_pdf_as_senza_pdf_and_never_drop_it_silently() -> None:
    html = _card("Circolare n. 1 del 20 gennaio 2025", f"{PDF_BASE}/20143/0/Circolare_n_1.pdf/aaaaaaaa-1111-2222-3333-444444444444?t=1") + _card(
        "Circolare n. 99 senza allegato", None
    )

    parsed = ade.parse_monthly(html, "circolare", 2025)

    assert [d.numero for d in parsed.docs] == [1]
    assert parsed.count == ade.ParseCount(card=2, documenti=1, intestazioni=0, senza_pdf=1)


@pytest.mark.unit
def test_should_dedupe_two_cards_with_the_same_pdf_url() -> None:
    url = f"{PDF_BASE}/20143/0/RIS_n_5.pdf/bbbbbbbb-1111-2222-3333-444444444444?t=1"

    parsed = ade.parse_monthly(_card("Risoluzione n. 5 del 24 gennaio 2025", url) + _card("Risoluzione n. 5 del 24 gennaio 2025", url), "risoluzione", 2025)

    assert len(parsed.docs) == 1


@pytest.mark.unit
def test_should_build_the_oggetto_slug_without_prefix_and_without_underscores() -> None:
    url = f"{PDF_BASE}/20143/0/Circolare_n_1_del_20_01_2025.pdf/cccccccc-1111-2222-3333-444444444444?t=1"

    docs = ade.parse_monthly(_card("Circolare n. 1 del 20 gennaio 2025", url), "circolare", 2025).docs

    assert not docs[0].oggetto.startswith("Circolare")
    assert "_" not in docs[0].oggetto


@pytest.mark.unit
def test_should_not_throw_on_html_without_cards() -> None:
    parsed = ade.parse_monthly("<html><body>nothing here</body></html>", "circolare", 2025)

    assert parsed.docs == []
    assert parsed.count == ade.ParseCount(card=0, documenti=0, intestazioni=0, senza_pdf=0)


# ---- provvedimenti ----


@pytest.mark.unit
def test_should_list_four_direct_pdfs_and_three_detail_pages_of_october_2025() -> None:
    parsed = ade.parse_provvedimenti(_page("ade-ottobre-2025-provvedimenti.html"))

    assert parsed.count.card == 8
    assert parsed.count.intestazioni == 1, "the 'Ottobre 2025 Provvedimenti' row is a header"
    assert len(parsed.entries) == 7
    assert sum(1 for e in parsed.entries if not e.is_detail) == 4
    assert sum(1 for e in parsed.entries if e.is_detail) == 3
    assert all(e.link.startswith("https://www.agenziaentrate.gov.it/portale/") for e in parsed.entries)
    assert [e.data_iso for e in parsed.entries][:3] == ["2025-10-31", "2025-10-23", "2025-10-20"]
    assert all(e.link.startswith(f"{DETAIL_BASE}/") for e in parsed.entries if e.is_detail)


@pytest.mark.unit
def test_should_count_nine_entries_in_march_2024_with_the_atto_congiunto_and_without_the_header() -> None:
    entries = ade.parse_provvedimenti_listing(_page("ade-provvedimenti-2024-03.reale.html"))

    assert len(entries) == 9, "10 cards, one is the month header"
    assert any(e.title.startswith("Atto congiunto") for e in entries)
    assert not any("Provvedimenti del Direttore" in e.title for e in entries)


@pytest.mark.unit
@pytest.mark.parametrize(
    "title",
    ["Foglia Novembre 2022 - Provvedimenti del Direttore soggetti a pubblicità legale", "Titolo Luglio 2019 - x", "Elenco x", "Settembre 2020 provvedimenti",
     "Provvedimenti Ottobre 2025"],
)
def test_should_recognise_the_header_rows(title: str) -> None:
    assert ade.is_header(title)


@pytest.mark.unit
@pytest.mark.parametrize("title", ["Provv cambio valute 09/02/2021", "Atto congiunto del 13 marzo 2024 - Agenzia delle Entrate e Inps", "Provvedimento del 31 ottobre 2025"])
def test_should_keep_acts_that_are_not_called_provvedimento(title: str) -> None:
    assert not ade.is_header(title)


@pytest.mark.unit
def test_should_classify_direct_and_detail_cards_and_skip_headers() -> None:
    pdf = f"{PDF_BASE}/20143/1/X.pdf"
    detail = f"{DETAIL_BASE}/provvedimento-del-30-novembre-2022"
    html = (
        _prov_card("Foglia Novembre 2022 - Provvedimenti del Direttore", "fa-folder-o", f"{DETAIL_BASE}/foglia")
        + _prov_card("Provvedimento del 14 novembre 2022 - Cambio valute", "fa fa-file-pdf-o", pdf)
        + _prov_card("Provvedimento del 29 novembre 2022", "fa-folder-o", detail)
    )

    entries = ade.parse_provvedimenti_listing(html)

    assert [(e.link, e.is_detail, e.data_iso) for e in entries] == [(pdf, False, "2022-11-14"), (detail, True, "2022-11-29")]


@pytest.mark.unit
def test_should_count_a_provvedimento_card_without_link_as_senza_pdf() -> None:
    html = _prov_card("Provvedimento del 14 novembre 2022", "fa fa-file-pdf-o", f"{PDF_BASE}/20143/1/X.pdf") + (
        '<div data-analytics-asset-title="Provvedimento del 15 novembre 2022"></div><p>nothing</p>'
    )

    parsed = ade.parse_provvedimenti(html)

    assert len(parsed.entries) == 1
    assert parsed.count == ade.ParseCount(card=2, documenti=1, intestazioni=0, senza_pdf=1)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Provvedimento del 26/06/2025", "2025-06-26"),
        ("Provvedimento del 6/12/2019", "2019-12-06"),
        ("Provvedimento 21_3_24 - cambio valute", "2024-03-21"),
        ("Provvedimento  21_3_24 - cambio valute estere Feb 24", "2024-03-21"),
        ("Provvedimento_20.12.2019_Erario", "2019-12-20"),
        ("Provvedimento del 10072019 cambio valute", "2019-07-10"),
        ("Provvedimento del 3 ottobre 2025", "2025-10-03"),
        ("Provvedimento 8 marzo 2024", "2024-03-08"),
        ("Provv del 30 giugno 2021 Compliance", "2021-06-30"),
        ("Provvedimento del 30 ottobre 2023 Adesione agevolata", "2023-10-30"),
        ("Provvedimento del 3 giugno", None),
        ("Provvedimento cambio valute febbraio 2020", None),
    ],
)
def test_should_read_every_date_shape_of_the_source_and_never_invent_one(title: str, expected: str | None) -> None:
    assert ade.parse_data(title) == expected


@pytest.mark.unit
def test_should_pick_the_provvedimento_and_not_the_allegato_from_a_real_detail_page() -> None:
    link = ade.extract_main_pdf(_page("ade-provvedimento-dettaglio.reale.html"))

    assert link is not None
    assert "Provvedimento_29_03_2024.pdf" in link
    assert "Modello" not in link
    assert "Istruzioni" not in link


@pytest.mark.unit
def test_should_prefer_the_provvedimento_link_text_skip_allegati_and_fall_back_to_the_first_pdf() -> None:
    main = "/portale/documents/1/2/Provvedimento+modifica+AVI_2022.pdf/g1"
    allegato = "/portale/documents/1/2/Allegato.pdf/g2"
    assert ade.extract_main_pdf(f'<a href="{allegato}">Modello di avviso</a><a href="{main}">Provvedimento</a>') == main

    first = "/portale/documents/1/2/Documento_AGE.pdf/ga"
    assert ade.extract_main_pdf(f'<a href="{first}">Documento</a><a href="{allegato}">Allegato</a>') == first
    assert ade.extract_main_pdf("<p>nessun pdf</p>") is None


@pytest.mark.unit
def test_should_name_the_download_by_the_pdf_segment_and_not_by_the_guid() -> None:
    assert ade.file_name_of(f"{PDF_BASE}/20143/1/Provvedimento+del+3+ottobre.pdf/49729f04-b1d7?t=1") == "Provvedimento del 3 ottobre.pdf"
    assert ade.file_name_of("https://www.agenziaentrate.gov.it/portale/documents/20143/1/noname") is None


# ---- F3: a replay server stands in for the portal ----
REPLAY_PDF_BASE = "http://replay.test:8765/portale/documents"


@pytest.mark.unit
def test_should_take_the_pdf_of_a_card_whose_link_is_on_a_replay_host() -> None:
    page = _card("Risoluzione n. 118 del 17 febbraio 2022", f"{REPLAY_PDF_BASE}/1/2/Risoluzione_n_118_2022.pdf/8c3d1f2e-0000-4000-8000-000000000001?t=1")

    parsed = ade.parse_monthly(page, "risoluzioni", 2022)

    assert [(d.numero, d.file_name) for d in parsed.docs] == [(118, "Risoluzione_n_118_2022.pdf")]
    assert parsed.docs[0].url.startswith("http://replay.test:8765/portale/documents/")


@pytest.mark.unit
def test_should_still_take_the_pdf_of_the_official_host() -> None:
    page = _card("Risoluzione n. 7 del 4 marzo 2025", f"{PDF_BASE}/1/2/Risoluzione_n_7.pdf/8c3d1f2e-0000-4000-8000-000000000002?t=1")

    parsed = ade.parse_monthly(page, "risoluzioni", 2025)

    assert [d.numero for d in parsed.docs] == [7]


@pytest.mark.unit
def test_should_take_a_provvedimento_link_on_a_replay_host() -> None:
    page = _prov_card("Provvedimento del 20 ottobre 2025", "fa-file-pdf-o", f"{REPLAY_PDF_BASE}/1/2/Provvedimento_20.pdf")

    entries = ade.parse_provvedimenti(page).entries

    assert [e.is_detail for e in entries] == [False]
    assert entries[0].link.startswith("http://replay.test:8765/portale/documents/")
