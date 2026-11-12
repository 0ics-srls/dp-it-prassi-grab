"""MEF DocTrib prassi (interpelli of the Agenzia delle Entrate): search client, answer parser and month slicing (ports of DocTribPages / DocTribParser /
DocTribGrab of branch 53, for the prassi scope `P`).

The source answers at most 50 documents per query, has no pagination and declares the real total in ``contatorePrassi``. A month is asked as one window; if
the served documents are fewer than the declared ones, every day of the month is asked; a day that is still truncated is taken as it is and counted
(``troncati``). What the dates cannot reach is looked for BY NUMBER (the sequence of the numbers seen has holes), and whatever is still missing is recorded as
``mancanti``: honest, never forced. The session is a cookie from the home page; the pause between requests is injected.
"""
from __future__ import annotations

import calendar
import html as html_lib
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import httpx
from defusedxml import ElementTree as SafeElementTree

from dp_it_prassi_grab import pdfdl
from dp_it_prassi_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_prassi_grab.http import status_error

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from dp_it_prassi_grab.ade_client import Pacer

BASE_URL = "https://def.finanze.it/DocTribFrontend/"
HOME_URL = BASE_URL + "RS2_HomePage.jsp"
SEARCH_URL = BASE_URL + "executeAdvancedPrassiSearch.do"
DETAIL_URL = BASE_URL + "getPrassiDetail.do"
CONTENT_URL = BASE_URL + "getContent.do"
MAX_ATTEMPTS = 3
BACKOFF_SEC = 5.0
HEADERS = {"Accept-Language": "it", "Referer": HOME_URL}

_XML_RESULT = re.compile(r"var xmlResult\s*=\s*'(.*?)';", re.DOTALL)
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
# estremi line: tipo, "del", date, optional "n.", number, ente, optional struttura
_ESTREMI = re.compile(
    r"^(?P<tipo>.+?)\s+del\s+(?P<data>\d{2}/\d{2}/\d{4})(?:\s+n\.\s*(?P<num>\S+))?\s*-\s*(?P<ente>[^-]+?)(?:\s*-\s*(?P<org>.*))?$",
)
_CONTENT_ID = re.compile(r"getContent\.do\?id=(\{[0-9A-Fa-f-]+\})")
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_JS_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_HEX = re.compile(r"[0-9a-fA-F]+")
_UNICODE_ESCAPE, _HEX_ESCAPE = 4, 2


@dataclass(frozen=True)
class Estremi:
    """The parts of a DocTrib "estremi" line."""

    tipo: str
    data: date
    numero: str | None
    ente: str
    struttura: str | None


@dataclass(frozen=True)
class DocTribItem:
    """One document of a search answer (no text yet): ``prov_guid`` is the id of the provvedimento, NOT of its PDF."""

    prov_guid: str
    tipo: str
    data: date
    numero: str | None
    ente: str
    struttura: str | None
    oggetto: str
    estremi: str


@dataclass(frozen=True)
class SearchSlice:
    """One answer: the declared total of the window and the served documents."""

    declared: int
    items: list[DocTribItem]

    @property
    def complete(self) -> bool:
        return len(self.items) >= self.declared


def js_unescape(literal: str) -> str:
    """Decodes the body of a JavaScript single-quoted string literal (``\\uXXXX``, ``\\xHH``, ``\\n``..., any other ``\\c`` is ``c``)."""
    out: list[str] = []
    i = 0
    size = len(literal)
    while i < size:
        char = literal[i]
        if char != "\\" or i + 1 >= size:
            out.append(char)
            i += 1
            continue
        nxt = literal[i + 1]
        if nxt == "u" and len(literal[i + 2 : i + 6]) == _UNICODE_ESCAPE and _HEX.fullmatch(literal[i + 2 : i + 6]):
            out.append(chr(int(literal[i + 2 : i + 6], 16)))
            i += 6
        elif nxt == "x" and len(literal[i + 2 : i + 4]) == _HEX_ESCAPE and _HEX.fullmatch(literal[i + 2 : i + 4]):
            out.append(chr(int(literal[i + 2 : i + 4], 16)))
            i += 4
        elif nxt in _JS_SIMPLE:
            out.append(_JS_SIMPLE[nxt])
            i += 2
        elif nxt == "\n":
            i += 2  # line continuation
        else:
            out.append(nxt)  # identity: \' \" \\ \/
            i += 2
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16")  # joins surrogate pairs written as two \uXXXX


def _iso(day: str) -> date:
    return datetime.strptime(day, "%d/%m/%Y").date()  # noqa: DTZ007 - a calendar date, no time zone involved


def parse_estremi(estremi: str) -> Estremi:
    """Splits "Interpello del 28/10/2025 n. 274 - Agenzia delle Entrate - Direzione Centrale ..."; ValueError when the line has another shape."""
    match = _ESTREMI.match(estremi.strip())
    if match is None:
        msg = f"estremi non riconosciuti: {estremi!r}"
        raise ValueError(msg)
    organ = match.group("org")
    return Estremi(match.group("tipo").strip(), _iso(match.group("data")), match.group("num"), match.group("ente").strip(), organ.strip() if organ else None)


def _plain(cdata: str) -> str:
    return _SPACES.sub(" ", html_lib.unescape(_TAG.sub(" ", cdata)).replace(chr(0xA0), " ")).strip()


def parse_search(response: bytes) -> SearchSlice:
    """Reads one search answer (HTML in ISO-8859-1 carrying the XML in a JS string): the declared total and the served documents."""
    page = response.decode("iso-8859-1")
    match = _XML_RESULT.search(page)
    if match is None:
        return SearchSlice(0, [])
    xml = _XML_DECLARATION.sub("", js_unescape(match.group(1)))
    root = SafeElementTree.fromstring(xml.encode("utf-8"))
    counter = root.find(".//contatorePrassi")
    declared = int(counter.text) if counter is not None and counter.text and counter.text.strip().isdigit() else 0
    items: list[DocTribItem] = []
    for provvedimento in root.iter("Provvedimento"):
        guid = provvedimento.get("idProvvedimento")
        estremi_node = provvedimento.find("estremi")
        estremi = (estremi_node.text or "").strip() if estremi_node is not None else ""
        if not guid or not estremi:
            continue
        parsed = parse_estremi(estremi)
        title = provvedimento.find(".//titoloProvvedimento")
        oggetto = _plain(title.text or "") if title is not None else ""
        items.append(DocTribItem(guid, parsed.tipo, parsed.data, parsed.numero, parsed.ente, parsed.struttura, oggetto, estremi))
    return SearchSlice(declared, items)


def extract_content_id(detail_html: str) -> str | None:
    """The id of the PDF (``getContent.do?id={...}``) in a detail page, or None when the page links no document."""
    match = _CONTENT_ID.search(detail_html)
    return match.group(1) if match else None


@dataclass(frozen=True)
class Window:
    """A month (``day`` None), a day, or a whole year (``month`` None, used by the search by number)."""

    year: int
    month: int | None = None
    day: int | None = None

    @property
    def label(self) -> str:
        if self.month is None:
            return str(self.year)
        return f"{self.year}-{self.month:02d}" + (f"-{self.day:02d}" if self.day else "")


def search_form(window: Window, tipo: str, ente: str, numero: str = "") -> dict[str, str]:
    """The advanced-search form of the legacy DocTribPages; ``parole`` and ``numero`` are present although empty (omitting them makes the POST fail)."""
    day = str(window.day) if window.day else ""
    month = str(window.month) if window.month else ""
    return {
        "js_enabled": "1",
        "tipoComplessitaRicerca": "avanzata",
        "tipoRicerca": "RA",
        "device": "D",
        "ambitoRicerca": "P",
        "tipoCriterioRicerca": "0",
        "tipo_ord": "DATA",
        "tipoEstremi": tipo,
        "ente": ente,
        "numero": numero,
        "parole": "",
        "giornoDataEmissioneDa": day,
        "meseDataEmissioneDa": month,
        "annoDataEmissioneDa": str(window.year),
        "giornoDataEmissioneA": day,
        "meseDataEmissioneA": month,
        "annoDataEmissioneA": str(window.year),
    }


class DocTribSession:
    """The DocTrib session: the home page sets the JSESSIONID that the client's cookie jar re-sends on every request; one session per item."""

    def __init__(self, client: httpx.Client, pacer: Pacer, tipo: str, ente: str, sleep: Callable[[float], None] = time.sleep) -> None:
        self._client = client
        self._pacer = pacer
        self._tipo = tipo
        self._ente = ente
        self._sleep = sleep
        self._ready = False

    @property
    def client(self) -> httpx.Client:
        return self._client

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last: GrabError | None = None
        for attempt in range(MAX_ATTEMPTS):
            if attempt:
                self._sleep(BACKOFF_SEC * attempt)
            self._pacer.wait()
            try:
                response = self._client.request(method, url, headers=HEADERS, **kwargs)
            except httpx.TransportError as error:
                if "CERTIFICATE" in str(error).upper():
                    msg = (
                        f"the TLS certificate of {httpx.URL(url).host} could not be verified ({error}); the grab class pins the missing intermediate, "
                        "so this is a new problem: check the certificate chain, and only as a documented escape set the Source setting verifyTls to false"
                    )
                    raise GrabError(CONFIG_ERROR, msg) from error
                last = GrabError(TRANSIENT_ERROR, f"request to {url} failed: {error}")
                continue
            if response.status_code == httpx.codes.OK:
                return response
            failure = status_error(url, response.status_code)
            if failure.code == CONFIG_ERROR:
                raise failure
            last = failure
        assert last is not None  # the loop ran at least once
        raise last

    def _ensure(self) -> None:
        if not self._ready:
            self._send("GET", HOME_URL)
            self._ready = True

    def search(self, window: Window, numero: str = "") -> bytes:
        """The raw answer of one search window (or, with ``numero``, of a search by number and year)."""
        self._ensure()
        return self._send("POST", SEARCH_URL, data=search_form(window, self._tipo, self._ente, numero)).content

    def detail(self, prov_guid: str) -> str:
        """The detail page of a provvedimento (HTML, ISO-8859-1)."""
        self._ensure()
        return self._send("GET", DETAIL_URL, params={"id": prov_guid}).content.decode("iso-8859-1")

    def download_content(self, content_id: str, target: Path) -> None:
        """Streams the PDF behind a content id (NOT the id of the provvedimento) to ``target``."""
        self._ensure()
        pdfdl.download_pdf(self._client, self._pacer, self._sleep, CONTENT_URL, target, params={"id": content_id}, headers=HEADERS)


@dataclass
class MonthResult:
    """A month of DocTrib: the documents taken and an honest account of what the source declared against what could be taken."""

    year: int
    month: int
    items: list[DocTribItem] = field(default_factory=list)
    dichiarati: int = 0
    presi: int = 0
    mancanti: int = 0
    troncati: int = 0
    recuperati: int = 0
    windows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sample_limited: bool = False

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}"


def _in_month(item: DocTribItem, year: int, month: int) -> bool:
    return item.data.year == year and item.data.month == month


class _Collector:
    """Items of one month in the order first seen, without duplicates."""

    def __init__(self, year: int, month: int) -> None:
        self.result = MonthResult(year, month)
        self._seen: set[str] = set()

    def add(self, items: list[DocTribItem], *, only_this_month: bool = False) -> int:
        added = 0
        for item in items:
            if item.prov_guid in self._seen or (only_this_month and not _in_month(item, self.result.year, self.result.month)):
                continue
            self._seen.add(item.prov_guid)
            self.result.items.append(item)
            added += 1
        return added

    def note(self, window: Window, answer: SearchSlice) -> None:
        self.result.windows.append({"window": window.label, "declared": answer.declared, "served": len(answer.items)})


def _candidates(numbers_seen: set[int], missing: int) -> list[int]:
    """The holes of the sequence seen so far, plus a margin of ``missing`` numbers on each side (a month does not start at 1)."""
    if not numbers_seen:
        return []
    low = max(1, min(numbers_seen) - missing)
    high = max(numbers_seen) + missing
    return [n for n in range(low, high + 1) if n not in numbers_seen]


def _recover_by_number(session: DocTribSession, collector: _Collector, missing: int) -> None:
    result = collector.result
    seen_numbers = {int(i.numero) for i in result.items if i.numero is not None and i.numero.isdigit()}
    recovered = 0
    for number in _candidates(seen_numbers, missing):
        if recovered >= missing:
            break  # the count returns: stop asking
        answer = parse_search(session.search(Window(result.year), numero=str(number)))
        recovered += collector.add(answer.items, only_this_month=True)
    result.recuperati = recovered


def slice_month(session: DocTribSession, year: int, month: int, max_items: int | None = None) -> MonthResult:
    """Takes the documents of one calendar month: the month window, the day windows when the cap truncates, the search by number for what is still missing."""
    collector = _Collector(year, month)
    result = collector.result
    first = parse_search(session.search(Window(year, month)))
    collector.note(Window(year, month), first)
    result.dichiarati = first.declared
    collector.add(first.items)
    if first.declared > 0 and not first.complete:
        for day in range(1, calendar.monthrange(year, month)[1] + 1):
            if max_items is not None and len(result.items) >= max_items:
                break
            window = Window(year, month, day)
            answer = parse_search(session.search(window))
            collector.note(window, answer)
            if not answer.complete:
                result.troncati += 1  # a day cannot be split: what was served is taken and the truncation is declared
            collector.add(answer.items)
    limited = max_items is not None and len(result.items) >= max_items
    if not limited:
        missing = result.dichiarati - len(result.items)
        if missing > 0:
            _recover_by_number(session, collector, missing)
    elif max_items is not None:
        result.sample_limited = True
        del result.items[max_items:]
    result.presi = len(result.items)
    result.mancanti = 0 if result.sample_limited else max(0, result.dichiarati - result.presi)
    if result.mancanti:
        result.warnings.append(f"{result.mancanti} of {result.dichiarati} documents declared for {result.label} could not be obtained from the source")
    return result
