"""Parsers of the Agenzia delle Entrate portal month pages (ports of AdeMonthlyParser and AdeProvvedimentiParser of branch 53). Pure functions over strings.

A month page lists "cards" (``data-analytics-asset-title``). Every card is found FIRST and classified afterwards, so a title in an unforeseen shape can never
merge its window with the previous card's and attach a PDF to the wrong document. The source is not coherent with its own titles ("Risoluzione 39 del ...",
"Ris 37 del ...", "Risoluzione_34_21.05.2021"): the number is read when present, the document enters anyway, and everything the page contained is COUNTED
(cards, headers, cards without a PDF) so that nothing is lost silently.
"""
from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import unquote_plus

_CARD = re.compile(r'data-analytics-asset-title="(?P<title>[^"]*)"', re.IGNORECASE)
_DOC_TITLE = re.compile(r"^\s*(?:circolare|circ|risoluzione|ris)\.?[\s_]*(?:n\.?[\s_]*)?(?P<num>\d+)?", re.IGNORECASE)
_MONTHLY_HEADER = re.compile(
    r"^\s*(foglia|titolo|elenco)\b|provvedimenti del direttore|^\s*\w+\s+\d{4}\s+(circolari|risoluzioni|provvedimenti)\s*$", re.IGNORECASE
)
_PDF = re.compile(
    r"https://www\.agenziaentrate\.gov\.it/portale/documents/\d+/\d+/[^\"' )]+?\.pdf(?:/[0-9a-f-]+)?(?:\?[^\"' )]*)?",
    re.IGNORECASE,
)
_FILE_NAME = re.compile(r"/([^/]+\.pdf)", re.IGNORECASE)
_SPACES = re.compile(r"[+_]+")
_PREFIX = re.compile(r"^(circolare|circ|risoluzione|ris)\.?\s*", re.IGNORECASE)
_LINK = re.compile(
    r'href="(?P<link>https://www\.agenziaentrate\.gov\.it/portale/(?:documents/[^"]+?\.pdf(?:/[^"]*)?|-/[^"]+))"',
    re.IGNORECASE,
)
_DETAIL_PDF = re.compile(r'<a[^>]+href="(?P<href>[^"]*?/documents/[^"]+?\.pdf(?:/[^"]*)?)"[^>]*>(?P<text>[^<]*)', re.IGNORECASE)
_PROV_HEADER = re.compile(
    r"^\s*(foglia|titolo|elenco)\b|provvedimenti del direttore|^\s*\w+\s+\d{4}\s+provvediment|^\s*provvedimenti\s+\w+\s+\d{4}", re.IGNORECASE
)
_DETAIL_BASE = "https://www.agenziaentrate.gov.it/portale/-/"
_WINDOW_FALLBACK = 4000

_MESI = {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10,
         "novembre": 11, "dicembre": 12}
_DATE_WORD_DEL = re.compile(r"del\s+(\d{1,2})\s+([a-zàèéìòù]+)\s+(\d{4})", re.IGNORECASE)
_DATE_WORD = re.compile(r"(\d{1,2})\s+([a-zàèéìòù]+)\s+(\d{4})", re.IGNORECASE)
_DATE_SLASH = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_DATE_UNDERSCORE = re.compile(r"(?<!\d)(\d{1,2})_(\d{1,2})_(\d{2,4})(?!\d)")
_DATE_DOT = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?!\d)")
_DATE_COMPACT = re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{4})(?!\d)")
_SHORT_YEAR = 2


@dataclass(frozen=True)
class AdeDoc:
    """One document of a month page: the full PDF url (with its guid and ``?t=``), the on-disk file name (the ``.pdf`` segment, not the guid), the number."""

    url: str
    file_name: str
    numero: int | None
    oggetto: str
    title: str = ""


@dataclass(frozen=True)
class ParseCount:
    """What a month page contained: cards seen, documents taken, header rows skipped, cards with no PDF at all."""

    card: int
    documenti: int
    intestazioni: int
    senza_pdf: int


@dataclass(frozen=True)
class MonthlyParse:
    """Documents of a circolari/risoluzioni page and the count of everything the page held."""

    docs: list[AdeDoc]
    count: ParseCount
    senza_pdf_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProvvedimentoEntry:
    """A provvedimento of the listing: title, link (a PDF or a detail page), whether the link is a detail page, emission date (ISO) when the title has one."""

    title: str
    link: str
    is_detail: bool
    data_iso: str | None


@dataclass(frozen=True)
class ProvvedimentiParse:
    """Entries of a provvedimenti page and the count of everything the page held."""

    entries: list[ProvvedimentoEntry]
    count: ParseCount
    senza_pdf_titles: tuple[str, ...] = ()


def _windows(html: str, cards: list[re.Match[str]]) -> list[str]:
    """The text after each card up to the next card, whether or not the title is recognised: that is what keeps a PDF attached to its own document."""
    out: list[str] = []
    for index, card in enumerate(cards):
        end = cards[index + 1].start() if index + 1 < len(cards) else min(len(html), card.end() + _WINDOW_FALLBACK)
        out.append(html[card.end() : end])
    return out


def file_name_of(pdf_url: str) -> str | None:
    """The ``X.pdf`` segment of a PDF url, url-decoded; None when the url has none."""
    match = _FILE_NAME.search(pdf_url)
    return unquote_plus(match.group(1)) if match else None


def parse_monthly(html: str, tipo: str, anno: int | None = None) -> MonthlyParse:
    """Documents of a circolari or risoluzioni month page. ``tipo`` and ``anno`` only name a document whose url has no ``.pdf`` segment."""
    cards = list(_CARD.finditer(html))
    windows = _windows(html, cards)
    docs: list[AdeDoc] = []
    seen: set[str] = set()
    intestazioni = 0
    senza_pdf = 0
    lost: list[str] = []
    for card, window in zip(cards, windows, strict=True):
        title = html_lib.unescape(card.group("title")).strip()
        if _MONTHLY_HEADER.search(title):
            intestazioni += 1
            continue
        doc_title = _DOC_TITLE.match(title)
        if doc_title is None:
            intestazioni += 1  # neither a document nor a known card: not an act
            continue
        pdf = _PDF.search(window)
        if pdf is None:
            senza_pdf += 1
            lost.append(title)
            continue
        url = html_lib.unescape(pdf.group(0))
        if url in seen:
            continue
        seen.add(url)
        numero = int(doc_title.group("num")) if doc_title.group("num") else None
        named = _FILE_NAME.search(url)
        raw_name = named.group(1) if named else f"{tipo}_{anno}_{numero if numero is not None else 'sn'}.pdf"
        file_name = unquote_plus(raw_name)
        slug = _SPACES.sub(" ", unquote_plus(raw_name[: -len(".pdf")])).strip()
        docs.append(AdeDoc(url, file_name, numero, _PREFIX.sub("", slug).strip(), title))
    return MonthlyParse(docs, ParseCount(len(cards), len(docs), intestazioni, senza_pdf), tuple(lost))


def is_header(title: str) -> bool:
    """A header row of a provvedimenti page ("Foglia ...", "Titolo ...", "Ottobre 2025 Provvedimenti"), not an act.

    The rule used to be the reverse ("not starting with Provvedimento is not an act") and threw away real acts such as "Provv cambio valute 09/02/2021"
    or "Atto congiunto del 13 marzo 2024 - Agenzia delle Entrate e Inps": headers are recognised, everything else that carries a PDF is an act.
    """
    return _PROV_HEADER.search(title) is not None


def _iso(day: str, month: int, year: str) -> str | None:
    full_year = int(year)
    if len(year) == _SHORT_YEAR:
        full_year += 2000
    try:
        return date(full_year, month, int(day)).isoformat()
    except ValueError:
        return None


def parse_data(title: str) -> str | None:
    """Emission date (yyyy-MM-dd) from a provvedimento title, or None when it carries no full date: without a day a date is never written."""
    word_del = _DATE_WORD_DEL.search(title)
    if word_del and word_del.group(2).lower() in _MESI:
        return _iso(word_del.group(1), _MESI[word_del.group(2).lower()], word_del.group(3))
    slash = _DATE_SLASH.search(title)
    if slash:
        return _iso(slash.group(1), int(slash.group(2)), slash.group(3))
    for pattern in (_DATE_UNDERSCORE, _DATE_DOT, _DATE_COMPACT):
        found = pattern.search(title)
        if found:
            return _iso(found.group(1), int(found.group(2)), found.group(3))
    word = _DATE_WORD.search(title)
    if word and word.group(2).lower() in _MESI:
        return _iso(word.group(1), _MESI[word.group(2).lower()], word.group(3))
    return None


def parse_provvedimenti(html: str) -> ProvvedimentiParse:
    """Entries of a provvedimenti month page (a direct PDF or a detail page each) with the count of everything the page held."""
    cards = list(_CARD.finditer(html))
    windows = _windows(html, cards)
    entries: list[ProvvedimentoEntry] = []
    intestazioni = 0
    senza_pdf = 0
    lost: list[str] = []
    for card, window in zip(cards, windows, strict=True):
        title = html_lib.unescape(card.group("title")).strip()
        if is_header(title):
            intestazioni += 1
            continue
        link = _LINK.search(window)
        if link is None:
            senza_pdf += 1
            lost.append(title)
            continue
        url = html_lib.unescape(link.group("link"))
        is_detail = url.lower().startswith(_DETAIL_BASE) or "fa-folder-o" in window.lower()
        entries.append(ProvvedimentoEntry(title, url, is_detail, parse_data(title)))
    return ProvvedimentiParse(entries, ParseCount(len(cards), len(entries), intestazioni, senza_pdf), tuple(lost))


def parse_provvedimenti_listing(html: str) -> list[ProvvedimentoEntry]:
    """Only the entries of ``parse_provvedimenti``."""
    return parse_provvedimenti(html).entries


def _stem_of(href: str) -> str:
    pdf = href.lower().find(".pdf")
    sliced = href[:pdf] if pdf > 0 else href
    return unquote_plus(sliced[sliced.rfind("/") + 1 :]).replace("_", " ")


def extract_main_pdf(detail_html: str) -> str | None:
    """Main provvedimento PDF of a detail page: link text or file name starting with "Provvedimento", skipping attachments; first PDF as fallback."""
    first: str | None = None
    for match in _DETAIL_PDF.finditer(detail_html):
        href = html_lib.unescape(match.group("href"))
        if first is None:
            first = href
        if match.group("text").lstrip().lower().startswith("provvedimento") or _stem_of(href).lower().startswith("provvedimento"):
            return href
    return first
