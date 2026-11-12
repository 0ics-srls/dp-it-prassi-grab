"""AdE portal client and per-item grab: one month page per item, the PDFs it lists, a sidecar per PDF and a counts file.

Politeness (P80-16): browser User-Agent and Accept-Language, at least 0.6 s between two requests, one request at a time, 5xx and timeouts retried twice with
back-off, an Akamai 403 ends the item with a config_error that says so (never retried blindly). The clock and the sleep are injected, so tests do not wait.
A month without a page (404) is counted, not an error: the pattern exists only from mid-2019. Everything the page held is counted in ``_coverage.json``.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import httpx

from dp_it_prassi_grab import ade, pdfdl
from dp_it_prassi_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DEFAULT_BASE_URL = "https://www.agenziaentrate.gov.it/portale"
ENTE = "Agenzia delle Entrate"
FONTE = "ade-portale"
PAUSE_SEC = 0.6
MIN_PDF_BYTES = pdfdl.MIN_PDF_BYTES
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre"]
TIPOLOGIE = {"circolari": "circolare", "risoluzioni": "risoluzione", "provvedimenti": "provvedimento"}
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_FILE_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_YEAR_MIN, _YEAR_MAX = 2000, 2100


def safe_name(item_id: str) -> str:
    """Directory name of an item id (convert and ingest derive the same name): ``ade:circolari:2025:07`` -> ``ade_circolari_2025_07``."""
    return _UNSAFE.sub("_", item_id.replace(" ", "-"))


class Pacer:
    """Keeps at least ``interval`` seconds between the starts of two requests (the first one does not wait)."""

    def __init__(self, interval: float = PAUSE_SEC, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            remaining = self._interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


@dataclass(frozen=True)
class AdeSettings:
    """The Source settings an AdE item honours. A key the Host resolved to JSON null counts as absent."""

    base_url: str = DEFAULT_BASE_URL
    only: tuple[tuple[str, str], ...] = ()
    max_docs: int | None = None
    delay: float = PAUSE_SEC

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> AdeSettings:
        data = {name: value for name, value in params.items() if value is not None}
        base_url = data.get("baseUrl", DEFAULT_BASE_URL)
        if not isinstance(base_url, str) or urlsplit(base_url).scheme not in {"http", "https"}:
            raise GrabError(CONFIG_ERROR, f"baseUrl must be an http(s) URL, got {base_url!r}")
        max_docs = data.get("maxDocs")
        if max_docs is not None and (not isinstance(max_docs, int) or isinstance(max_docs, bool) or max_docs < 1):
            raise GrabError(CONFIG_ERROR, "maxDocs must be an integer >= 1")
        delay = data.get("delaySeconds", PAUSE_SEC)
        if not isinstance(delay, int | float) or isinstance(delay, bool) or delay < PAUSE_SEC:
            raise GrabError(CONFIG_ERROR, f"delaySeconds must be a number >= {PAUSE_SEC} (respect for the source)")
        only_raw = data.get("only", [])
        if not isinstance(only_raw, list):
            raise GrabError(CONFIG_ERROR, "only must be an array of {tipo, stem}")
        only: list[tuple[str, str]] = []
        for entry in only_raw:
            if not isinstance(entry, dict) or entry.get("tipo") not in TIPOLOGIE or not isinstance(entry.get("stem"), str) or not entry["stem"]:
                raise GrabError(CONFIG_ERROR, f"only entries need tipo (circolari|risoluzioni|provvedimenti) and stem: {entry!r}")
            only.append((entry["tipo"], entry["stem"]))
        return cls(base_url.rstrip("/"), tuple(only), max_docs, float(delay))


class AdeClient:
    """Month pages, detail pages and PDFs of the portal, paced and retried."""

    def __init__(self, http: httpx.Client, pacer: Pacer, base_url: str = DEFAULT_BASE_URL, sleep: Callable[[float], None] = time.sleep) -> None:
        self._http = http
        self._pacer = pacer
        self._base = base_url.rstrip("/")
        self._sleep = sleep

    def _get(self, url: str) -> httpx.Response:
        last: GrabError | None = None
        for _ in pdfdl.attempts(self._pacer, self._sleep):
            try:
                response = self._http.get(url)
            except httpx.TransportError as error:
                last = GrabError(TRANSIENT_ERROR, f"request to {url} failed: {error}")
                continue
            if response.status_code == httpx.codes.NOT_FOUND:
                return response
            last = pdfdl.check_response(url, response)
            if last is None:
                return response
        assert last is not None  # the loop ran at least once
        raise last

    def month_page(self, tipologia: str, anno: int, mese: int) -> str | None:
        """The HTML of a month page, or None when the portal has no such page (404)."""
        response = self._get(f"{self._base}/{MESI[mese - 1]}-{anno}-{tipologia}")
        return None if response.status_code == httpx.codes.NOT_FOUND else response.text

    def detail_page(self, url: str) -> str | None:
        """The HTML of a provvedimento detail page, or None when it is gone (404)."""
        response = self._get(url)
        return None if response.status_code == httpx.codes.NOT_FOUND else response.text

    def download_pdf(self, url: str, target: Path) -> None:
        """Streams the PDF to ``target`` (atomic rename; nothing is left behind on a failure)."""
        pdfdl.download_pdf(self._http, self._pacer, self._sleep, url, target)


def validate_meta(key: str, meta: dict[str, Any]) -> None:
    if not isinstance(meta.get("itemId"), str) or not meta["itemId"]:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.itemId is required")
    if meta.get("tipologia") not in TIPOLOGIE:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.tipologia must be one of {sorted(TIPOLOGIE)}, got {meta.get('tipologia')!r}")
    anno, mese = meta.get("anno"), meta.get("mese")
    if not isinstance(anno, int) or isinstance(anno, bool) or not _YEAR_MIN <= anno <= _YEAR_MAX:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.anno must be a year, got {anno!r}")
    if not isinstance(mese, int) or isinstance(mese, bool) or not 1 <= mese <= len(MESI):
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.mese must be 1-12, got {mese!r}")


def planned(base: Path, entry: dict[str, Any]) -> dict[str, str]:
    return {"key": entry["key"], "path": str(base / safe_name(entry["meta"]["itemId"])), "status": "planned"}


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


@dataclass(frozen=True)
class _Candidate:
    """A document of the page: its card title, the PDF url (or the detail page that holds it), number and date."""

    title: str
    link: str
    is_detail: bool
    numero: str | None
    data: str | None


def _candidates(tipologia: str, page: str, anno: int) -> tuple[list[_Candidate], ade.ParseCount, tuple[str, ...]]:
    if tipologia == "provvedimenti":
        parsed = ade.parse_provvedimenti(page)
        return [_Candidate(e.title, e.link, is_detail=e.is_detail, numero=None, data=e.data_iso) for e in parsed.entries], parsed.count, parsed.senza_pdf_titles
    monthly = ade.parse_monthly(page, TIPOLOGIE[tipologia], anno)
    docs = [
        _Candidate(d.title, d.url, is_detail=False, numero=str(d.numero) if d.numero is not None else None, data=ade.parse_data(d.title))
        for d in monthly.docs
    ]
    return docs, monthly.count, monthly.senza_pdf_titles


def _stem_and_name(pdf_url: str) -> tuple[str, str]:
    named = ade.file_name_of(pdf_url)
    if named is None:
        named = f"doc_{hashlib.sha1(pdf_url.encode('utf-8'), usedforsecurity=False).hexdigest()[:12]}.pdf"
    name = _FILE_UNSAFE.sub("_", named)
    return name.removesuffix(".pdf").removesuffix(".PDF"), name


def valid_pdf(path: Path) -> bool:
    try:
        if path.stat().st_size < MIN_PDF_BYTES:
            return False
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


@dataclass
class _ItemGrab:
    """The state of one item: where it writes, what it wants, what it has counted."""

    meta: dict[str, Any]
    item_dir: Path
    client: AdeClient
    wanted: set[str]
    restricted: bool
    coverage: dict[str, Any]
    downloaded: int = 0
    skipped_existing: int = 0

    def take(self, candidate: _Candidate) -> None:
        if self.restricted and not self.wanted:
            return  # the only list names other document types: nothing of this type is wanted, and nothing is requested
        link = candidate.link
        if candidate.is_detail:
            detail = self.client.detail_page(link)
            main = ade.extract_main_pdf(detail) if detail else None
            if main is None:
                self.coverage["skipped"].append({"item_ref": f"{self.meta['itemId']}/{candidate.title}", "reason": "dettaglio_senza_pdf"})
                return
            link = urljoin(link, main)
        stem, name = _stem_and_name(link)
        if self.restricted and stem not in self.wanted:
            return
        target = self.item_dir / name
        if valid_pdf(target):
            self.skipped_existing += 1
        else:
            self.client.download_pdf(link, target)
            self.downloaded += 1
        sidecar = {
            "kind": "ade", "tipo": TIPOLOGIE[self.meta["tipologia"]], "titolo": candidate.title, "numero": candidate.numero, "data": candidate.data,
            "sourceUrl": link.split("?", 1)[0], "fonte": FONTE, "sourceId": stem, "ente": ENTE,
        }
        write_json(self.item_dir / f"{name}.meta.json", sidecar)
        self.coverage["documents"] += 1


def grab_ade_item(entry: dict[str, Any], base: Path, client: AdeClient, settings: AdeSettings) -> dict[str, Any]:
    """One month page into ``<base>/<itemId>``: ``<stem>.pdf`` and ``<stem>.pdf.meta.json`` per document and ``_coverage.json``."""
    meta = entry["meta"]
    tipologia, anno, mese = meta["tipologia"], meta["anno"], meta["mese"]
    item_dir = base / safe_name(meta["itemId"])
    coverage: dict[str, Any] = {
        "fonte": FONTE, "tipo": TIPOLOGIE[tipologia], "anno": anno, "mese": mese, "cards": 0, "listed": 0, "documents": 0, "intestazioni": 0,
        "senza_pdf": 0, "dettagli_persi": 0, "pagina_404": False, "sample_filter": bool(settings.only or settings.max_docs), "skipped": [],
    }
    wanted = {stem for kind, stem in settings.only if kind == tipologia}
    item = _ItemGrab(meta, item_dir, client, wanted, bool(settings.only), coverage)
    page = client.month_page(tipologia, anno, mese)
    if page is None:
        coverage["pagina_404"] = True
    else:
        candidates, count, lost_titles = _candidates(tipologia, page, anno)
        coverage.update(cards=count.card, listed=count.documenti, intestazioni=count.intestazioni, senza_pdf=count.senza_pdf)
        coverage["skipped"].extend({"item_ref": f"{meta['itemId']}/{title}", "reason": "senza_pdf"} for title in lost_titles)
        for candidate in candidates:
            if settings.max_docs is not None and coverage["documents"] >= settings.max_docs:
                break
            item.take(candidate)
        coverage["dettagli_persi"] = sum(1 for s in coverage["skipped"] if s["reason"] == "dettaglio_senza_pdf")
    write_json(item_dir / "_coverage.json", coverage)
    return {
        "key": entry["key"], "path": str(item_dir), "status": "downloaded", "downloaded": item.downloaded, "skipped_existing": item.skipped_existing,
        "documents": coverage["documents"], "senza_pdf": coverage["senza_pdf"], "dettagli_persi": coverage["dettagli_persi"],
    }
