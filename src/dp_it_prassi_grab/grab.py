"""Per-item grab into ``<dir>``; the item's ``meta.kind`` picks the source: ``ade`` (Agenzia delle Entrate portal month pages) or ``doctrib`` (MEF DocTrib
interpelli). One request at a time, paced, browser User-Agent. Source config keys the Host resolved to JSON null count as absent."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from dp_it_prassi_grab import ade_client, doctrib, doctrib_download
from dp_it_prassi_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_prassi_grab.http import DEFAULT_ALLOWED_HOSTS, build_client

if TYPE_CHECKING:
    import httpx

Progress = Callable[[int, str], None]
KIND_ADE = "ade"
KIND_DOCTRIB = "doctrib"
KINDS = (KIND_ADE, KIND_DOCTRIB)


@dataclass(frozen=True)
class GrabDeps:
    """The ports of a run: transport (None = real network), sleep and clock (pacing)."""

    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)
    clock: Callable[[], float] = field(default=time.monotonic)


def validate_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise GrabError(CONFIG_ERROR, "items must be a non-empty array")
    for entry in items:
        if not isinstance(entry, dict) or not entry.get("key"):
            raise GrabError(CONFIG_ERROR, f"item needs a key: {entry!r}")
        meta = entry.get("meta")
        if not isinstance(meta, dict) or meta.get("kind") not in KINDS:
            raise GrabError(CONFIG_ERROR, f"item {entry['key']}: meta.kind must be one of {list(KINDS)}")
        if meta["kind"] == KIND_ADE:
            ade_client.validate_meta(entry["key"], meta)
        else:
            doctrib_download.validate_meta(entry["key"], meta)
    return items


def _host_of(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise GrabError(CONFIG_ERROR, f"baseUrl must be an http(s) URL, got {base_url!r}")
    return parts.hostname.lower()


def _planned(base: Path, entry: dict[str, Any]) -> dict[str, str]:
    planner = ade_client.planned if entry["meta"]["kind"] == KIND_ADE else doctrib_download.planned
    return planner(base, entry)


def _delay(params: dict[str, Any]) -> float:
    delay = params.get("delaySeconds", ade_client.PAUSE_SEC)
    if not isinstance(delay, int | float) or isinstance(delay, bool) or delay < ade_client.PAUSE_SEC:
        raise GrabError(CONFIG_ERROR, f"delaySeconds must be a number >= {ade_client.PAUSE_SEC} (respect for the source)")
    return float(delay)


def run_grab(raw_params: dict[str, Any], deps: GrabDeps, on_progress: Progress) -> dict[str, Any]:
    """Tries every item; if any failed raises GrabError (config_error if any failure is a config error, else transient)."""
    params = {name: value for name, value in raw_params.items() if value is not None}  # the Host resolves an undeclared Source config key to null
    items = validate_items(params.get("items"))
    directory = params.get("dir")
    if not isinstance(directory, str) or not directory:
        raise GrabError(CONFIG_ERROR, "dir is required")
    hosts = params.get("allowedHosts", list(DEFAULT_ALLOWED_HOSTS))
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise GrabError(CONFIG_ERROR, "allowedHosts must be an array of host names")
    verify_tls = params.get("verifyTls", True)
    if not isinstance(verify_tls, bool):
        raise GrabError(CONFIG_ERROR, "verifyTls must be a boolean")
    kinds = {entry["meta"]["kind"] for entry in items}
    ade_settings = ade_client.AdeSettings.from_params(params) if KIND_ADE in kinds else ade_client.AdeSettings()
    doctrib_options = doctrib_download.DocTribOptions.from_params(params) if KIND_DOCTRIB in kinds else doctrib_download.DocTribOptions()
    delay = _delay(params)
    if "baseUrl" in params and "allowedHosts" not in params:
        hosts = [*hosts, _host_of(ade_settings.base_url)]  # a replay server stands in for the portal: the Source config names it, so its host is allowed
    base = Path(directory)

    if params.get("dryRun") is True:
        planned = [_planned(base, e) for e in items]
        return {"downloaded": 0, "skipped_existing": 0, "senza_pdf": 0, "mancanti": 0, "truncated": 0, "files": planned, "dryRun": True}

    client = build_client(hosts, deps.transport, verify_tls=verify_tls)
    pacer = ade_client.Pacer(delay, clock=deps.clock, sleep=deps.sleep)
    ade = ade_client.AdeClient(client, pacer, ade_settings.base_url, sleep=deps.sleep)
    ente = str(params.get("ente", ade_client.ENTE))
    files: list[dict[str, Any]] = []
    failures: list[tuple[str, GrabError]] = []
    for position, entry in enumerate(items):
        try:
            if entry["meta"]["kind"] == KIND_ADE:
                files.append(ade_client.grab_ade_item(entry, base, ade, ade_settings))
            else:
                client.cookies.clear()  # one DocTrib session (cookie jar) per item
                tipo = str(entry["meta"].get("tipoEstremi") or "Interpello")
                session = doctrib.DocTribSession(client, pacer, tipo, ente, sleep=deps.sleep)
                files.append(doctrib_download.grab_doctrib_item(entry, base, session, doctrib_options))
        except GrabError as error:
            failures.append((entry["key"], error))
        on_progress(int((position + 1) * 100 / len(items)), f"{position + 1}/{len(items)} {entry['key']}")
    if failures:
        code = CONFIG_ERROR if any(error.code == CONFIG_ERROR for _, error in failures) else TRANSIENT_ERROR
        detail = "; ".join(f"{key} ({error})" for key, error in failures)
        msg = f"{len(failures)} of {len(items)} items failed: {detail}"
        raise GrabError(code, msg)
    return {
        "downloaded": sum(f.get("downloaded", 0) for f in files),
        "skipped_existing": sum(f.get("skipped_existing", 0) for f in files),
        "senza_pdf": sum(f.get("senza_pdf", 0) for f in files),
        "mancanti": sum(f.get("mancanti", 0) for f in files),
        "truncated": sum(f.get("truncated", 0) for f in files),
        "files": files,
    }
