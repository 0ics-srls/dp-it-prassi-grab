"""DocTrib item: one calendar month of interpelli into ``<base>/<itemId>``.

Per document: the detail page (``getPrassiDetail.do?id={provGUID}``) holds the id of the PDF (``getContent.do?id={contentGUID}``, a DIFFERENT id: the PDF does
not open with the id of the provvedimento), the PDF is named by the legacy convention ``<tipo>_<yyyy-MM-dd>_<numero|nd>__<guid>.pdf`` and a sidecar carries the
estremi read from the search list (the PDF has no header to re-parse). The source link stored is the detail page (P80-13), the only link that opens.
A document whose detail page links no PDF is counted (``senza_pdf``) and skipped; a detail page that keeps failing is a transient failure of the item AFTER the
others were written, so a retry only repeats what is missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dp_it_prassi_grab import doctrib
from dp_it_prassi_grab.ade_client import ENTE, safe_name, valid_pdf, write_json
from dp_it_prassi_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError

if TYPE_CHECKING:
    from pathlib import Path

FONTE = "doctrib"
_YEAR_MIN, _YEAR_MAX = 2000, 2100
_UNSAFE_NUMBER = re.compile(r"[^A-Za-z0-9.-]+")


@dataclass(frozen=True)
class DocTribOptions:
    """The Source settings a DocTrib item honours (sample filters): numbers to keep, maximum number of documents."""

    max_items: int | None = None
    only: tuple[str, ...] = ()

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> DocTribOptions:
        data = {name: value for name, value in params.items() if value is not None}
        max_items = data.get("maxItems")
        if max_items is not None and (not isinstance(max_items, int) or isinstance(max_items, bool) or max_items < 1):
            raise GrabError(CONFIG_ERROR, "maxItems must be an integer >= 1")
        only = data.get("only", [])
        if not isinstance(only, list) or not all(isinstance(n, str | int) and not isinstance(n, bool) for n in only):
            raise GrabError(CONFIG_ERROR, "only must be an array of numbers (interpello numbers)")
        return cls(max_items, tuple(str(n) for n in only))


def validate_meta(key: str, meta: dict[str, Any]) -> None:
    if not isinstance(meta.get("itemId"), str) or not meta["itemId"]:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.itemId is required")
    anno, mese = meta.get("anno"), meta.get("mese")
    if not isinstance(anno, int) or isinstance(anno, bool) or not _YEAR_MIN <= anno <= _YEAR_MAX:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.anno must be a year, got {anno!r}")
    if not isinstance(mese, int) or isinstance(mese, bool) or not 1 <= mese <= 12:  # noqa: PLR2004
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.mese must be 1-12, got {mese!r}")


def planned(base: Path, entry: dict[str, Any]) -> dict[str, str]:
    return {"key": entry["key"], "path": str(base / safe_name(entry["meta"]["itemId"])), "status": "planned"}


def pdf_name(item: doctrib.DocTribItem) -> str:
    """``Interpello_2025-10-28_274__D12541AF-65CB-4CC5-843D-DF108E6D52E8.pdf`` (``nd`` when the estremi carry no number)."""
    numero = _UNSAFE_NUMBER.sub("-", item.numero) if item.numero else "nd"
    return f"{item.tipo}_{item.data:%Y-%m-%d}_{numero}__{item.prov_guid.strip('{}')}.pdf"


def _sidecar(item: doctrib.DocTribItem) -> dict[str, Any]:
    return {
        "kind": "doctrib",
        "tipo": item.tipo.lower(),
        "titolo": item.oggetto,
        "numero": item.numero,
        "data": item.data.isoformat(),
        "struttura": item.struttura,
        "ente": item.ente or ENTE,
        "sourceUrl": f"{doctrib.DETAIL_URL}?id={item.prov_guid}",
        "fonte": FONTE,
        "sourceId": item.prov_guid.strip("{}"),
    }


@dataclass
class _Tally:
    """What an item has done so far: counters, the documents skipped (with the reason) and the ones that failed."""

    downloaded: int = 0
    skipped_existing: int = 0
    senza_pdf: int = 0
    skipped: list[dict[str, str]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def lost(self, ref: str) -> None:
        self.senza_pdf += 1
        self.skipped.append({"item_ref": ref, "reason": "senza_pdf"})  # nessun link getContent nel dettaglio


def grab_doctrib_item(entry: dict[str, Any], base: Path, session: doctrib.DocTribSession, options: DocTribOptions) -> dict[str, Any]:
    """One month window into ``<base>/<itemId>``: ``<name>.pdf`` and ``<name>.pdf.meta.json`` per document and ``_coverage.json``."""
    meta = entry["meta"]
    item_dir = base / safe_name(meta["itemId"])
    tipo_estremi = meta.get("tipoEstremi") or "Interpello"
    sample = bool(options.only or options.max_items)
    month = doctrib.slice_month(session, meta["anno"], meta["mese"], None if options.only else options.max_items)
    wanted = [i for i in month.items if not options.only or i.numero in options.only]
    if options.max_items is not None:
        wanted = wanted[: options.max_items]

    tally = _Tally()
    for item in wanted:
        _take(item, meta["itemId"], item_dir, session, tally)

    coverage = {
        "fonte": FONTE, "tipo": str(tipo_estremi).lower(), "anno": meta["anno"], "mese": meta["mese"], "dichiarati": month.dichiarati,
        "presi": tally.downloaded + tally.skipped_existing, "mancanti": 0 if sample else month.mancanti, "troncati": month.troncati,
        "recuperati": month.recuperati, "senza_pdf": tally.senza_pdf, "sample_filter": sample, "windows": month.windows, "warnings": month.warnings,
        "skipped": tally.skipped,
    }
    write_json(item_dir / "_coverage.json", coverage)
    if tally.failures:
        failed = ", ".join(tally.failures)
        msg = f"item {entry['key']}: {len(tally.failures)} of {len(wanted)} documents failed ({failed}); the others were written, retry the item"
        raise GrabError(TRANSIENT_ERROR, msg)
    return {
        "key": entry["key"], "path": str(item_dir), "status": "downloaded", "downloaded": tally.downloaded, "skipped_existing": tally.skipped_existing,
        "senza_pdf": tally.senza_pdf, "mancanti": coverage["mancanti"], "truncated": month.troncati, "dichiarati": month.dichiarati, "kept": len(wanted),
    }


def _take(item: doctrib.DocTribItem, item_id: str, item_dir: Path, session: doctrib.DocTribSession, tally: _Tally) -> None:
    name = pdf_name(item)
    target = item_dir / name
    ref = f"{item_id}/{item.prov_guid.strip('{}')}"
    try:
        if valid_pdf(target):
            tally.skipped_existing += 1
        else:
            content_id = doctrib.extract_content_id(session.detail(item.prov_guid))
            if content_id is None:
                tally.lost(ref)
                return
            session.download_content(content_id, target)
            tally.downloaded += 1
    except GrabError as error:
        if error.code == CONFIG_ERROR and "404" in str(error):
            tally.lost(ref)
            return
        if error.code == CONFIG_ERROR:
            raise
        tally.failures.append(item.prov_guid.strip("{}"))
        return
    write_json(item_dir / f"{name}.meta.json", _sidecar(item))
