"""DocTrib session: home page once, cookie reused, browser headers, retries, TLS failures named by the setting."""
from __future__ import annotations

import itertools
from collections.abc import Callable

import httpx
import pytest

from conftest import DocTribFake, FakeTime, make_docs
from dp_it_prassi_grab import ade_client, doctrib
from dp_it_prassi_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_prassi_grab.http import build_client


def session_for(route: Callable[[httpx.Request], httpx.Response], clock: FakeTime) -> tuple[doctrib.DocTribSession, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        clock.now += 0.05
        return route(request)

    client = build_client(["def.finanze.it"], httpx.MockTransport(handler))
    return doctrib.DocTribSession(client, ade_client.Pacer(0.5, clock=clock.clock, sleep=clock.sleep), "Interpello", "Agenzia delle Entrate", sleep=clock.sleep), seen


@pytest.mark.unit
def test_should_request_the_home_page_once_and_reuse_the_session_cookie() -> None:
    clock = FakeTime()
    fake = DocTribFake(make_docs(2025, 10, {2: 3}))
    session, seen = session_for(fake.route, clock)

    session.search(doctrib.Window(2025, 10))
    session.search(doctrib.Window(2025, 10, 2))

    assert fake.home_requests == 1
    assert [r.url.path.rsplit("/", 1)[1] for r in seen] == ["RS2_HomePage.jsp", "executeAdvancedPrassiSearch.do", "executeAdvancedPrassiSearch.do"]
    assert all(c is not None and "JSESSIONID=abc123" in c for c in fake.cookies_seen)


@pytest.mark.unit
def test_should_send_browser_headers_and_the_home_page_as_referer() -> None:
    clock = FakeTime()
    fake = DocTribFake(make_docs(2025, 10, {2: 3}))
    session, seen = session_for(fake.route, clock)

    session.search(doctrib.Window(2025, 10))

    post = seen[1]
    assert "Mozilla/5.0" in post.headers["user-agent"]
    assert post.headers["accept-language"].startswith("it")
    assert post.headers["referer"] == "https://def.finanze.it/DocTribFrontend/RS2_HomePage.jsp"
    assert post.method == "POST"
    assert str(post.url) == "https://def.finanze.it/DocTribFrontend/executeAdvancedPrassiSearch.do"


@pytest.mark.unit
def test_should_pause_at_least_half_a_second_between_requests() -> None:
    clock = FakeTime()
    fake = DocTribFake(make_docs(2025, 10, {2: 3}))
    session, _ = session_for(fake.route, clock)
    stamps: list[float] = []
    original = fake.route

    def recording(request: httpx.Request) -> httpx.Response:
        stamps.append(clock.now)
        return original(request)

    session, _ = session_for(recording, clock)

    session.search(doctrib.Window(2025, 10))
    session.search(doctrib.Window(2025, 10, 2))

    assert all(b - a >= 0.5 - 1e-6 for a, b in itertools.pairwise(stamps)), stamps


@pytest.mark.unit
def test_should_retry_a_5xx_twice_and_then_succeed() -> None:
    clock = FakeTime()
    fake = DocTribFake(make_docs(2025, 10, {2: 3}))
    failures = {"left": 2}

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("executeAdvancedPrassiSearch.do") and failures["left"] > 0:
            failures["left"] -= 1
            return httpx.Response(500)
        return fake.route(request)

    session, _ = session_for(route, clock)

    assert session.search(doctrib.Window(2025, 10))
    assert failures["left"] == 0


@pytest.mark.unit
def test_should_end_transient_after_three_failures() -> None:
    clock = FakeTime()
    session, seen = session_for(lambda _r: httpx.Response(503), clock)

    with pytest.raises(GrabError) as caught:
        session.search(doctrib.Window(2025, 10))

    assert caught.value.code == TRANSIENT_ERROR
    assert len(seen) == 3, "the home page is retried like any other request and fails"


@pytest.mark.unit
def test_should_name_the_verify_tls_setting_when_the_certificate_cannot_be_verified() -> None:
    clock = FakeTime()

    def route(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate", request=request)

    session, _ = session_for(route, clock)

    with pytest.raises(GrabError) as caught:
        session.search(doctrib.Window(2025, 10))

    assert caught.value.code == CONFIG_ERROR
    assert "verifyTls" in str(caught.value)


@pytest.mark.unit
def test_should_treat_a_403_as_a_config_error_not_to_be_retried() -> None:
    clock = FakeTime()
    session, seen = session_for(lambda _r: httpx.Response(403, text="denied"), clock)

    with pytest.raises(GrabError) as caught:
        session.search(doctrib.Window(2025, 10))

    assert caught.value.code == CONFIG_ERROR
    assert len(seen) == 1
