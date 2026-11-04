"""Modele plików stanu na gałęzi `monitor-data` (A§6, A§8).

- `state/scheduler.json`   — SchedulerState
- `state/schema-version.json` — SchemaVersionFile
- `cycles/<id>/manifest.json` — CycleManifest (niezmienny po utworzeniu)
- `cycles/<id>/progress.json` — CycleProgress (lista zatwierdzonych partii i status)
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import Field

from booking_monitor.config import OwnApartment, Profile
from booking_monitor.models import CycleStatus, FrozenModel
from booking_monitor.storage.events import RunContext

STATE_SCHEMA_VERSION = 1


class SchemaVersionFile(FrozenModel):
    schema_version: int = STATE_SCHEMA_VERSION
    event_schema_version: int = 1
    initialized_at: datetime


class SchedulerState(FrozenModel):
    schema_version: int = STATE_SCHEMA_VERSION
    anchor_at_utc: datetime
    interval_hours: int
    last_started_slot: int | None = None
    active_cycle_id: str | None = None
    last_finished_cycle_id: str | None = None
    updated_at: datetime


class CompetitorSnapshot(FrozenModel):
    """Konkurent zamrożony w manifeście cyklu."""

    competitor_id: str
    name: str
    booking_url: str
    canonical_url: str
    unit_filter: str | None = None


class CycleManifest(FrozenModel):
    schema_version: int = STATE_SCHEMA_VERSION
    cycle_id: str
    schedule_slot: int
    cycle_date: date
    start_date: date
    end_date_exclusive: date
    stay_nights: tuple[int, ...]
    report_timezone: str
    own_apartment: OwnApartment
    profile: Profile
    profile_hash: str
    source: str
    competitors: tuple[CompetitorSnapshot, ...]
    config_hash: str
    code_sha: str | None = None
    parser_version: str
    created_at: datetime
    created_by: RunContext
    query_count: int = Field(ge=0)
    # SHA-256 posortowanych identyfikatorów zapytań — weryfikacja odtworzenia planu.
    plan_digest: str


class BatchRef(FrozenModel):
    batch_id: str
    path: str  # względem katalogu cyklu, np. "batches/<batch_id>.jsonl.gz"
    sha256: str  # z nieskompresowanej zawartości JSONL
    events: int = Field(ge=0)
    created_at: datetime
    run: RunContext


class BlockInfo(FrozenModel):
    at: datetime
    reason: str
    message: str | None = None
    job_id: str | None = None


class CycleProgress(FrozenModel):
    schema_version: int = STATE_SCHEMA_VERSION
    cycle_id: str
    status: CycleStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    batches: tuple[BatchRef, ...] = ()
    blocked: BlockInfo | None = None
    windows_planned_total: int = 0
    # Podsumowanie liczby zadań wg statusu po ostatnim checkpointcie (informacyjnie).
    job_counts: dict[str, int] = Field(default_factory=dict)
    updated_at: datetime
