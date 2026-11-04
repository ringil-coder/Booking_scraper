"""Odtwarzanie roboczej bazy z gałęzi `monitor-data` (A§8, A§15 pkt 5–6).

Źródłem prawdy są wyłącznie zatwierdzone pliki: `state/`, manifesty i partie
wymienione w `progress.json`. Pliki partii spoza `progress.json` nie są trwałymi
checkpointami — pomijamy je i zgłaszamy jako osierocone.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from booking_monitor.dates import local_date
from booking_monitor.planner import verify_manifest_plan
from booking_monitor.storage.batches import BatchIntegrityError
from booking_monitor.storage.database import import_batch, import_manifest, set_cycle_status
from booking_monitor.storage.events import EVENT_SCHEMA_VERSION
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.state import (
    STATE_SCHEMA_VERSION,
    CycleManifest,
    CycleProgress,
    SchedulerState,
    SchemaVersionFile,
)


class RestoreError(Exception):
    """Stanu nie da się wiarygodnie odtworzyć — zatrzymujemy pracę."""


class StateMissingError(RestoreError):
    """Brak stanu w istniejącym wdrożeniu (to błąd, nie nowy start)."""


class SchemaMismatchError(RestoreError):
    """Niezgodna wersja schematu danych."""


@dataclass
class RestoreReport:
    cycles: list[str] = field(default_factory=list)
    batches_imported: int = 0
    events: int = 0
    orphan_batches: list[str] = field(default_factory=list)
    skipped_cycles: list[str] = field(default_factory=list)


def _load_json[M: BaseModel](path: Path, model: type[M], *, missing: type[RestoreError]) -> M:
    if not path.is_file():
        raise missing(f"brak pliku stanu: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RestoreError(f"nieczytelny plik stanu {path}: {exc}") from exc
    version = raw.get("schema_version") if isinstance(raw, dict) else None
    if version != STATE_SCHEMA_VERSION:
        raise SchemaMismatchError(
            f"{path}: wersja schematu {version!r}, obsługiwana {STATE_SCHEMA_VERSION}"
        )
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise RestoreError(f"nieprawidłowy plik stanu {path}: {exc}") from exc


def check_schema_version(layout: DataLayout) -> SchemaVersionFile:
    if not layout.state_dir.is_dir():
        raise StateMissingError(f"brak katalogu stanu {layout.state_dir}")
    info = _load_json(layout.schema_version_file, SchemaVersionFile, missing=StateMissingError)
    if info.event_schema_version != EVENT_SCHEMA_VERSION:
        raise SchemaMismatchError(
            f"wersja schematu zdarzeń {info.event_schema_version}, "
            f"obsługiwana {EVENT_SCHEMA_VERSION}"
        )
    return info


def load_scheduler_state(layout: DataLayout) -> SchedulerState:
    return _load_json(layout.scheduler_file, SchedulerState, missing=StateMissingError)


def load_cycle(layout: DataLayout, cycle_id: str) -> tuple[CycleManifest, CycleProgress]:
    manifest = _load_json(layout.manifest_file(cycle_id), CycleManifest, missing=RestoreError)
    progress = _load_json(layout.progress_file(cycle_id), CycleProgress, missing=RestoreError)
    if manifest.cycle_id != cycle_id or progress.cycle_id != cycle_id:
        raise RestoreError(f"katalog {cycle_id} zawiera stan innego cyklu")
    return manifest, progress


def _orphans(layout: DataLayout, cycle_id: str, progress: CycleProgress) -> list[str]:
    batches_dir = layout.batches_dir(cycle_id)
    if not batches_dir.is_dir():
        return []
    referenced = {ref.path for ref in progress.batches}
    return sorted(
        f"{cycle_id}/batches/{p.name}"
        for p in batches_dir.iterdir()
        if p.is_file() and f"batches/{p.name}" not in referenced and not p.name.startswith(".")
    )


def _within_retention(manifest: CycleManifest, now: datetime, retention_days: int) -> bool:
    today = local_date(now, ZoneInfo(manifest.report_timezone))
    return manifest.cycle_date >= today - timedelta(days=retention_days)


def rebuild_database(
    layout: DataLayout,
    conn: sqlite3.Connection,
    *,
    now: datetime,
    retention_days: int,
    cycle_ids: Sequence[str] | None = None,
) -> RestoreReport:
    """Odbudowuje bazę z manifestów i zatwierdzonych partii.

    Bez `cycle_ids` obejmuje wszystkie cykle w okresie retencji (wg `cycle_date`).
    Import jest idempotentny — partie już zaimportowane są pomijane.
    """
    check_schema_version(layout)
    load_scheduler_state(layout)
    available = layout.cycle_ids()
    if cycle_ids is not None:
        missing = [cid for cid in cycle_ids if cid not in available]
        if missing:
            raise RestoreError(f"brak katalogów cykli: {', '.join(missing)}")
        selected = list(cycle_ids)
    else:
        selected = available

    report = RestoreReport()
    for cycle_id in selected:
        manifest, progress = load_cycle(layout, cycle_id)
        if cycle_ids is None and not _within_retention(manifest, now, retention_days):
            report.skipped_cycles.append(cycle_id)
            continue
        try:
            planned = verify_manifest_plan(manifest)
        except ValueError as exc:
            raise RestoreError(str(exc)) from exc
        try:
            import_manifest(conn, manifest, planned, progress)
            for ref in progress.batches:
                if import_batch(conn, layout, cycle_id, ref):
                    report.batches_imported += 1
                    report.events += ref.events
            set_cycle_status(conn, cycle_id, progress)
        except BatchIntegrityError as exc:
            raise RestoreError(f"cykl {cycle_id}: {exc}") from exc
        except (sqlite3.IntegrityError, ValueError) as exc:
            raise RestoreError(f"cykl {cycle_id}: niespójne dane ({exc})") from exc
        report.cycles.append(cycle_id)
        report.orphan_batches.extend(_orphans(layout, cycle_id, progress))
    return report
