"""Shared test ports: a fake clock (sleep advances time), a recording scripted transport and fixture loaders. No real network in any test."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_BYTES = b"%PDF-1.4\n" + b"0" * 4000


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


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


@dataclass
class Server:
    """A scripted transport: ``route`` maps a request to a response; every request is recorded with the fake time it was made."""

    route: Callable[[httpx.Request], httpx.Response]
    time: FakeTime
    requests: list[Recorded] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(Recorded(request.method, str(request.url), request.headers, self.time.now))
        # a request takes a little time, like a real one
        self.time.now += 0.05
        return self.route(request)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def urls(self) -> list[str]:
        return [r.url for r in self.requests]
