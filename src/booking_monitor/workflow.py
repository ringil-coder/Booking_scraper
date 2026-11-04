"""Operacje cyklu: gate → prepare → collect → finalize oraz init-data (A§6–§8, A§13).

Każda operacja zaczyna od świeżego stanu gałęzi `monitor-data` (A§7 pkt 4)
i zapisuje zmiany wyłącznie tam. Moduł nie zależy od GitHub Actions (P1):
te same funkcje działają lokalnie z dowolnym zdalnym repozytorium git.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from booking_monitor.config import AppConfig, Settings, load_config
from booking_monitor.github_state import DataRepo
from booking_monitor.models import FINISHED_CYCLE_STATUSES, CycleStatus, JobStatus
from booking_monitor.observation import Observer
from booking_monitor.planner import create_manifest, windows_needed
from booking_monitor.queue import job_counts, summarize_queue
from booking_monitor.runner import CycleRunner, WindowResult, expire_overdue_jobs
from booking_monitor.schedule import ActiveCycle, GateAction, GateDecision, GateMode, decide
from booking_monitor.storage.batches import new_batch_id, write_batch
from booking_monitor.storage.database import (
    mark_batch_imported,
    open_database,
    set_cycle_status,
)
from booking_monitor.storage.events import RunContext
from booking_monitor.storage.files import write_model
from booking_monitor.storage.init import initialize_data_dir
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.restore import (
    check_schema_version,
    load_cycle,
    load_scheduler_state,
    rebuild_database,
)
from booking_monitor.storage.state import CycleManifest, CycleProgress

log = logging.getLogger(__name__)


class WorkflowError(RuntimeError):
    """Błąd operacji, który ma zatrzymać workflow z czytelnym komunikatem."""


@dataclass(frozen=True)
class Workspace:
    repo_root: Path
    config_dir: Path
    runtime_dir: Path

    @property
    def data_worktree(self) -> Path:
        return self.runtime_dir / "monitor-data"

    @property
    def db_path(self) -> Path:
        return self.runtime_dir / "monitor.sqlite3"

    def fresh_db(self):
        """Robocza baza od zera — SQLite jest indeksem, nie źródłem prawdy (A§8)."""
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)
        return open_database(self.db_path)


def current_code_sha(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def open_data(ws: Workspace, settings: Settings) -> tuple[DataRepo, DataLayout]:
    repo = DataRepo.open(ws.repo_root, ws.data_worktree, branch=settings.storage.state_branch)
    layout = DataLayout(repo.path)
    check_schema_version(layout)
    return repo, layout


# --------------------------------------------------------------------------- init-data


def init_data(ws: Workspace, settings: Settings, *, now: datetime) -> str:
    """Jawna, jednorazowa inicjalizacja gałęzi danych (A§6: pierwsza inicjalizacja)."""
    if ws.data_worktree.exists():
        shutil.rmtree(ws.data_worktree)
    repo = DataRepo.initialize(
        ws.repo_root,
        ws.data_worktree,
        branch=settings.storage.state_branch,
        remote="origin",
        populate=lambda path: initialize_data_dir(DataLayout(path), settings, now),
        message=f"inicjalizacja stanu monitora ({now.isoformat()})",
    )
    return repo.head_sha()


# --------------------------------------------------------------------------- gate


def _rebuild(layout: DataLayout, conn, settings: Settings, now: datetime, cycle_ids=None):
    return rebuild_database(
        layout,
        conn,
        now=now,
        retention_days=settings.storage.history_retention_days,
        cycle_ids=cycle_ids,
    )


def gate(ws: Workspace, settings: Settings, *, mode: GateMode, now: datetime) -> GateDecision:
    """Kontrola terminu. Tylko czyta stan; nie instaluje przeglądarki i nie otwiera Booking."""
    if mode is GateMode.DUE and not settings.schedule.enabled:
        return GateDecision(GateAction.IDLE, "harmonogram wyłączony (schedule.enabled)")
    _repo, layout = open_data(ws, settings)
    state = load_scheduler_state(layout)
    active = None
    if state.active_cycle_id:
        manifest, progress = load_cycle(layout, state.active_cycle_id)
        conn = open_database(":memory:")
        _rebuild(layout, conn, settings, now, [manifest.cycle_id])
        summary = summarize_queue(conn, manifest, now=now, not_before=progress.not_before)
        active = ActiveCycle(
            cycle_id=manifest.cycle_id,
            slot=manifest.schedule_slot,
            status=progress.status,
            due_jobs=summary.due_jobs,
            waiting_jobs=summary.waiting_jobs,
            next_attempt_at=summary.next_attempt_at,
        )
    return decide(
        now=now,
        schedule=settings.schedule,
        state=state,
        active=active,
        mode=mode,
        tz=settings.tz,
    )


# --------------------------------------------------------------------------- prepare


@dataclass(frozen=True)
class PrepareResult:
    cycle_id: str
    code_sha: str | None
    windows: list[int]
    open_jobs: int


def prepare(
    ws: Workspace,
    *,
    action: GateAction,
    now: datetime,
    run: RunContext,
    parser_version: str,
    new_slot: int | None = None,
    cycle_date: date | None = None,
    unblock: bool = False,
    config_loader: Callable[[Path], AppConfig] = load_config,
) -> PrepareResult:
    """Zapisuje manifest nowego cyklu albo przygotowuje wznowienie i liczy okna (A§7)."""
    config = config_loader(ws.config_dir)  # placeholdery zatrzymują workflow przed przeglądarką
    settings = config.settings
    repo, layout = open_data(ws, settings)
    state = load_scheduler_state(layout)

    if action is GateAction.START:
        if new_slot is None or cycle_date is None:
            raise WorkflowError("start cyklu wymaga numeru slotu i daty cyklu")
        if state.active_cycle_id is not None:
            _m, active_progress = load_cycle(layout, state.active_cycle_id)
            if active_progress.status not in FINISHED_CYCLE_STATUSES:
                raise WorkflowError(
                    f"cykl {state.active_cycle_id} nadal trwa — nie otwieram drugiego"
                )
        if state.last_started_slot is not None and new_slot <= state.last_started_slot:
            raise WorkflowError(f"slot {new_slot} był już rozpoczęty")
        manifest = create_manifest(
            config,
            slot=new_slot,
            cycle_date=cycle_date,
            created_at=now,
            run=run,
            parser_version=parser_version,
        )
        if layout.manifest_file(manifest.cycle_id).exists():
            raise WorkflowError(f"manifest {manifest.cycle_id} już istnieje")
        progress = CycleProgress(
            cycle_id=manifest.cycle_id,
            status=CycleStatus.RUNNING,
            started_at=now,
            updated_at=now,
        )
        write_model(layout.manifest_file(manifest.cycle_id), manifest)
        state = state.model_copy(
            update={
                "last_started_slot": new_slot,
                "active_cycle_id": manifest.cycle_id,
                "updated_at": now,
            }
        )
        message = f"start cyklu {manifest.cycle_id} ({manifest.query_count} zapytań)"
    elif action in (GateAction.RESUME, GateAction.FINALIZE):
        if state.active_cycle_id is None:
            raise WorkflowError("brak aktywnego cyklu do wznowienia")
        manifest, progress = load_cycle(layout, state.active_cycle_id)
        if progress.status is CycleStatus.BLOCKED:
            if not unblock:
                raise WorkflowError("cykl zablokowany — wznowienie wymaga trybu 'unblock'")
            log.warning("Odblokowanie cyklu %s (było: %s)", manifest.cycle_id, progress.blocked)
            progress = progress.model_copy(update={"blocked": None, "not_before": None})
        if action is GateAction.RESUME:
            progress = progress.model_copy(update={"status": CycleStatus.RUNNING})
        message = f"wznowienie cyklu {manifest.cycle_id}"
    else:
        raise WorkflowError(f"prepare nie obsługuje akcji {action}")

    conn = open_database(":memory:")
    if layout.progress_file(manifest.cycle_id).exists():
        _rebuild(layout, conn, settings, now, [manifest.cycle_id])
        summary = summarize_queue(conn, manifest, now=now)
        open_jobs = summary.open_jobs
    else:
        open_jobs = manifest.query_count
    windows = 0
    if action is not GateAction.FINALIZE:
        windows = windows_needed(
            open_jobs,
            seconds_per_job_p95=settings.execution.estimated_observation_seconds_p95,
            margin=settings.execution.planning_margin,
            budget_minutes=settings.execution.collection_budget_minutes_per_job,
            max_windows=settings.execution.max_windows_per_run,
        )
    progress = progress.model_copy(
        update={
            "windows_planned_total": progress.windows_planned_total + windows,
            "updated_at": now,
        }
    )
    write_model(layout.progress_file(manifest.cycle_id), progress)
    write_model(layout.scheduler_file, state)
    repo.commit_and_push(message)
    return PrepareResult(
        cycle_id=manifest.cycle_id,
        code_sha=manifest.code_sha,
        windows=list(range(1, windows + 1)),
        open_jobs=open_jobs,
    )


# --------------------------------------------------------------------------- collect


def collect(
    ws: Workspace,
    settings: Settings,
    *,
    cycle_id: str,
    now: datetime,
    run: RunContext,
    observer_factory: Callable[[CycleManifest], Observer],
    runner_options: dict | None = None,
) -> WindowResult:
    """Jedno okno robocze. Stan pobierany na początku okna, nie z `prepare` (A§7 pkt 4)."""
    repo, layout = open_data(ws, settings)
    state = load_scheduler_state(layout)
    if state.active_cycle_id != cycle_id:
        return WindowResult(stop_reason=f"cykl {cycle_id} nie jest już aktywny — nic do zrobienia")
    manifest, progress = load_cycle(layout, cycle_id)
    if progress.status is CycleStatus.BLOCKED:
        return WindowResult(stop_reason="cykl zablokowany — bez wejścia na Booking", blocked=True)
    conn = ws.fresh_db()
    _rebuild(layout, conn, settings, now, [cycle_id])
    observer = observer_factory(manifest)
    runner = CycleRunner(
        conn=conn,
        layout=layout,
        committer=repo,
        manifest=manifest,
        progress=progress,
        observer=observer,
        execution=settings.execution,
        run=run,
        **(runner_options or {}),
    )
    try:
        return runner.run_window()
    finally:
        close = getattr(observer, "close", None)
        if close:
            close()


# --------------------------------------------------------------------------- finalize


@dataclass
class FinalizeResult:
    cycle_id: str
    status: CycleStatus
    job_counts: dict[str, int]
    expired_now: int
    report_dir: Path | None = None
    summary_markdown: str = ""
    warnings: list[str] = field(default_factory=list)


def final_status(progress: CycleProgress, counts: dict[str, int]) -> CycleStatus:
    if progress.status is CycleStatus.BLOCKED:
        return CycleStatus.BLOCKED
    open_jobs = sum(
        counts.get(s.value, 0) for s in (JobStatus.PENDING, JobStatus.RETRY_WAIT, JobStatus.RUNNING)
    )
    if open_jobs:
        return CycleStatus.PARTIAL
    if counts.get(JobStatus.FAILED.value, 0) or counts.get(JobStatus.EXPIRED.value, 0):
        return CycleStatus.COMPLETED_WITH_ERRORS
    return CycleStatus.COMPLETED


def finalize(
    ws: Workspace,
    settings: Settings,
    *,
    cycle_id: str,
    now: datetime,
    run: RunContext,
    out_dir: Path | None,
    artifact_name: str | None = None,
) -> FinalizeResult:
    """Ustala status cyklu z zapisanych danych i publikuje raport pełny albo częściowy."""
    repo, layout = open_data(ws, settings)
    state = load_scheduler_state(layout)
    manifest, progress = load_cycle(layout, cycle_id)
    conn = ws.fresh_db()
    _rebuild(layout, conn, settings, now)

    expired = expire_overdue_jobs(conn, manifest, now=now, run=run)
    batches = list(progress.batches)
    if expired:
        ref = write_batch(layout, cycle_id, new_batch_id(now), expired, created_at=now, run=run)
        mark_batch_imported(conn, cycle_id, ref)
        conn.commit()
        batches.append(ref)

    counts = job_counts(conn, cycle_id)
    status = final_status(progress, counts)
    finished = status in FINISHED_CYCLE_STATUSES
    progress = progress.model_copy(
        update={
            "status": status,
            "batches": tuple(batches),
            "job_counts": counts,
            "finished_at": now if finished else None,
            "updated_at": now,
        }
    )
    set_cycle_status(conn, cycle_id, progress)
    conn.commit()
    write_model(layout.progress_file(cycle_id), progress)
    if finished and state.active_cycle_id == cycle_id:
        state = state.model_copy(
            update={"active_cycle_id": None, "last_finished_cycle_id": cycle_id, "updated_at": now}
        )
        write_model(layout.scheduler_file, state)

    result = FinalizeResult(
        cycle_id=cycle_id, status=status, job_counts=counts, expired_now=len(expired)
    )
    if out_dir is not None:
        from booking_monitor.reporting.bundle import build_report_bundle, latest_report_entry
        from booking_monitor.reporting.summary import render_summary_markdown

        tz = ZoneInfo(manifest.report_timezone)
        bundle = build_report_bundle(conn, cycle_id, out_dir, now=now, tz=tz, run=run)
        write_model(layout.latest_report_file, latest_report_entry(bundle))
        from booking_monitor.analytics import build_cycle_report

        report = build_cycle_report(conn, cycle_id, now=now, tz=tz)
        result.summary_markdown = render_summary_markdown(report, artifact_name=artifact_name)
        result.report_dir = out_dir
    repo.commit_and_push(f"finalizacja {cycle_id}: {status}")
    return result
