"""Paced, retried requests and PDF downloads shared by the AdE client and the DocTrib session.

``attempts`` yields up to three tries with a back-off pause and the pacer's pause before each; ``check_response`` separates the answers retrying can fix
(5xx, 429) from the ones it cannot (a 403 of the portal's Akamai, any 4xx); ``download_pdf`` streams a PDF to a temporary file, checks magic and size and
renames atomically, leaving nothing behind on a failure.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from dp_it_prassi_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_prassi_grab.http import status_error

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from dp_it_prassi_grab.ade_client import Pacer

MAX_ATTEMPTS = 3
BACKOFF_SEC = 5.0
MIN_PDF_BYTES = 1024
CHUNK = 64 * 1024


def attempts(pacer: Pacer, sleep: Callable[[float], None]) -> Iterator[int]:
    """Up to ``MAX_ATTEMPTS`` tries; every one is preceded by the pacer's pause, every retry by a growing back-off."""
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            sleep(BACKOFF_SEC * attempt)
        pacer.wait()
        yield attempt


def check_response(url: str, response: httpx.Response) -> GrabError | None:
    """None when the answer is usable; a retryable GrabError for 5xx/429; raises for an answer that retrying cannot fix."""
    if response.status_code == httpx.codes.FORBIDDEN:
        msg = f"HTTP 403 from {url}: the source (Akamai) refused this client; use a browser User-Agent, a lower rate or another egress"
        raise GrabError(CONFIG_ERROR, msg)
    if response.status_code == httpx.codes.OK:
        return None
    failure = status_error(url, response.status_code)
    if failure.code == CONFIG_ERROR:
        raise failure
    return failure


def _download_once(http: httpx.Client, url: str, temp: Path, params: dict[str, str] | None, headers: dict[str, str] | None) -> GrabError | None:
    try:
        with http.stream("GET", url, params=params, headers=headers) as response:
            failure = check_response(url, response) if response.status_code != httpx.codes.NOT_FOUND else GrabError(CONFIG_ERROR, f"HTTP 404 from {url}")
            if failure is not None:
                if failure.code == CONFIG_ERROR:
                    raise failure
                return failure
            size = 0
            with temp.open("wb") as handle:
                for block in response.iter_bytes(CHUNK):
                    handle.write(block)
                    size += len(block)
    except httpx.TransportError as error:
        return GrabError(TRANSIENT_ERROR, f"download of {url} failed: {error}")
    with temp.open("rb") as handle:
        magic = handle.read(5)
    if magic != b"%PDF-":
        return GrabError(TRANSIENT_ERROR, f"{url} did not answer a PDF (no %PDF- magic): an interstitial page, retry later")
    if size < MIN_PDF_BYTES:
        return GrabError(TRANSIENT_ERROR, f"{url} answered a PDF of {size} bytes, below the {MIN_PDF_BYTES} byte sanity limit")
    return None


def download_pdf(  # noqa: PLR0913
    http: httpx.Client, pacer: Pacer, sleep: Callable[[float], None], url: str, target: Path, *, params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    """Streams the PDF to ``target`` (atomic rename; nothing is left behind on a failure)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    last: GrabError | None = None
    try:
        for _ in attempts(pacer, sleep):
            last = _download_once(http, url, temp, params, headers)
            if last is None:
                temp.replace(target)
                return
        assert last is not None  # the loop ran at least once
        raise last
    finally:
        temp.unlink(missing_ok=True)
