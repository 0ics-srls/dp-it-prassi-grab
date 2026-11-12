"""Shared test ports: a fake clock (sleep advances time), a recording scripted transport, a scripted DocTrib server and fixture loaders.

No test uses the real network: the socket connect is refused for every unit test.
"""
from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_BYTES = b"%PDF-1.4\n" + b"0" * 4000


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("integration") is not None:
        return

    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "tests must not use the network"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)


def fixture_text(name: str, encoding: str = "utf-8") -> str:
    return (FIXTURES / name).read_text(encoding=encoding)


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeTime:
    """clock() and sleep(): sleeping advances the clock, so pacing is asserted without waiting."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Recorded:
    method: str
    url: str
    headers: httpx.Headers
    at: float
    body: str = ""


@dataclass
class Server:
    """A scripted transport: ``route`` maps a request to a response; every request is recorded with the fake time it was made."""

    route: Callable[[httpx.Request], httpx.Response]
    time: FakeTime
    requests: list[Recorded] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(Recorded(request.method, str(request.url), request.headers, self.time.now, request.content.decode("utf-8", "replace")))
        # a request takes a little time, like a real one
        self.time.now += 0.05
        return self.route(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def urls(self) -> list[str]:
        return [r.url for r in self.requests]


# ---- DocTrib ----

DOCTRIB = "https://def.finanze.it/DocTribFrontend/"
ENTE = "Agenzia delle Entrate"
STRUTTURA = "Direzione Centrale Persone Fisiche, Lavoratori Autonomi ed Enti non Commerciali"


@dataclass(frozen=True)
class Doc:
    """One interpello of the scripted source."""

    day: date
    numero: int | None
    guid: str


def make_docs(year: int, month: int, per_day: dict[int, int], first_number: int = 1) -> list[Doc]:
    """Interpelli numbered in date order: ``per_day`` maps a day of the month to how many answers it has."""
    docs: list[Doc] = []
    number = first_number
    for day in sorted(per_day):
        for _ in range(per_day[day]):
            docs.append(Doc(date(year, month, day), number, "{00000000-0000-4000-8000-%012d}" % (year * 100000 + month * 10000 + number)))  # noqa: UP031
            number += 1
    return docs


def search_page(declared: int, docs: list[Doc]) -> bytes:
    """A DocTrib answer: HTML in ISO-8859-1 carrying the XML in a JS string with escaped quotes and slashes."""
    items = ""
    for doc in docs:
        estremi = f"Interpello del {doc.day:%d/%m/%Y}" + (f" n. {doc.numero}" if doc.numero is not None else "") + f" - {ENTE} - {STRUTTURA}"
        items += (
            f'<Provvedimento idProvvedimento=\\"{doc.guid}\\"><estremi link=\\"true\\">{estremi.replace("/", chr(92) + "/")}<\\/estremi>'
            f"<titoliProvvedimento><titoloProvvedimento><![CDATA[ <p>Oggetto dell'interpello {doc.numero}<\\/p> ]]><\\/titoloProvvedimento><\\/titoliProvvedimento>"
            "<\\/Provvedimento>"
        )
    xml = (
        '<?xml version=\\"1.0\\" encoding=\\"UTF-8\\"?><risultatiRicerca><contatori><contatoreNormativa>0<\\/contatoreNormativa>'
        f"<contatorePrassi>{declared}<\\/contatorePrassi><\\/contatori><risultati>{items}<\\/risultati><\\/risultatiRicerca>"
    )
    return f"<html><script>var xmlResult = '{xml}';</script></html>".encode("iso-8859-1")


@dataclass
class DocTribFake:
    """A scripted DocTrib: home page with a session cookie, search by window or by number, a cap of 50 served per answer."""

    docs: list[Doc]
    declared_override: dict[str, int] = field(default_factory=dict)
    cap: int = 50
    forms: list[dict[str, str]] = field(default_factory=list)
    home_requests: int = 0
    cookies_seen: list[str | None] = field(default_factory=list)

    def route(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("RS2_HomePage.jsp"):
            self.home_requests += 1
            return httpx.Response(200, text="<html>home</html>", headers={"set-cookie": "JSESSIONID=abc123; Path=/DocTribFrontend"})
        if request.url.path.endswith("executeAdvancedPrassiSearch.do"):
            self.cookies_seen.append(request.headers.get("cookie"))
            form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8"), keep_blank_values=True).items()}
            self.forms.append(form)
            return httpx.Response(200, content=self._answer(form), headers={"content-type": "text/html;charset=ISO-8859-1"})
        return httpx.Response(404)

    def _answer(self, form: dict[str, str]) -> bytes:
        year = int(form["annoDataEmissioneDa"])
        if form["numero"]:
            hits = [d for d in self.docs if d.numero == int(form["numero"]) and d.day.year == year]
            label = f"n{form['numero']}/{year}"
        else:
            month = int(form["meseDataEmissioneDa"]) if form["meseDataEmissioneDa"] else None
            day = int(form["giornoDataEmissioneDa"]) if form["giornoDataEmissioneDa"] else None
            hits = [d for d in self.docs if d.day.year == year and (month is None or d.day.month == month) and (day is None or d.day.day == day)]
            label = f"{year}-{month:02d}" + (f"-{day:02d}" if day else "") if month else str(year)
        hits.sort(key=lambda d: (d.day, d.numero or 0), reverse=True)
        return search_page(self.declared_override.get(label, len(hits)), hits[: self.cap])

    def server(self, time: FakeTime) -> Server:
        return Server(self.route, time)
