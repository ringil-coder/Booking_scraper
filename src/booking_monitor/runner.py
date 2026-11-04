"""Wykonanie pozostałych zadań cyklu w jednym oknie roboczym (ARCHITEKTURA.md §7, §10).

Okno:
- pracuje najwyżej `collection_budget_minutes_per_job` i kończy się kontrolowanie,
- co `checkpoint_interval_minutes` lub `checkpoint_max_observations` prób zapisuje
  partię zdarzeń i postęp w JEDNYM commicie na gałęzi danych,
- przy CAPTCHA/blokadzie albo serii błędów parsera oznacza cykl jako `blocked`
  i natychmiast kończy (kolejne okna nie wchodzą na Booking),
- przy długim Retry-After zapisuje `not_before` i kończy zamiast czekać.

Stan zadań trzymamy w pamięci (kopiec), a każde zdarzenie od razu trafia do SQLite.
"""

from __future__ import annotations

import heapq
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from booking_monitor.config import ExecutionSettings
from booking_monitor.dates import is_expired
from booking_monitor.github_state import PushFailedError
from booking_monitor.models import (
    CycleStatus,
    JobStatus,
    ObservationOutcome,
    TechnicalReason,
    make_attempt_id,
)
from booking_monitor.observation import Observer
from booking_monitor.queue import JobRow, job_counts, load_open_jobs
from booking_monitor.storage.batches import new_batch_id, write_batch
from booking_monitor.storage.database import apply_event, mark_batch_imported
from booking_monitor.storage.events import (
    AttemptEvent,
    JobExpiredEvent,
    ObservationRecord,
    RunContext,
)
from booking_monitor.storage.files import write_model
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.state import BlockInfo, CycleManifest, CycleProgress

log = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 500


class Committer(Protocol):
    def commit_and_push(self, message: str) -> str | None: ...


class WindowTerminated(Exception):
    """Sygnał zakończenia (np. SIGTERM przy anulowaniu joba) — próbujemy zapisać checkpoint."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _truncate(message: str | None) -> str | None:
    if message is None or len(message) <= MAX_MESSAGE_LENGTH:
        return message
    return message[: MAX_MESSAGE_LENGTH - 1] + "…"


@dataclass
class WindowResult:
    attempts: int = 0
    completed: int = 0
    retry_wait: int = 0
    failed: int = 0
    expired: int = 0
    checkpoints: int = 0
    push_failures: int = 0
    stop_reason: str = ""
    blocked: bool = False
    durations: list[float] = field(default_factory=list)


class _JobQueue:
    """Kolejka zadań: spośród gotowych zawsze najwcześniejsze w planie (P4).

    Zadania czekające na ponowienie leżą w osobnym kopcu według terminu i wracają
    do gotowych, gdy termin minie — ponowienie bliskiej daty wyprzedza dalsze daty.
    """

    def __init__(self, jobs: list[JobRow], now: datetime):
        self._ready: list[tuple[int, JobRow]] = []
        self._waiting: list[tuple[datetime, int, JobRow]] = []
        for job in jobs:
            self.push(job)
        self.release(now)

    def push(self, job: JobRow) -> None:
        if job.next_attempt_at is None:
            heapq.heappush(self._ready, (job.sort_key, job))
        else:
            heapq.heappush(self._waiting, (job.next_attempt_at, job.sort_key, job))

    def release(self, now: datetime) -> None:
        while self._waiting and self._waiting[0][0] <= now:
            _at, key, job = heapq.heappop(self._waiting)
            heapq.heappush(self._ready, (key, job))

    def pop_ready(self) -> JobRow | None:
        return heapq.heappop(self._ready)[1] if self._ready else None

    def next_wait(self) -> datetime | None:
        return self._waiting[0][0] if self._waiting else None

    def __bool__(self) -> bool:
        return bool(self._ready or self._waiting)


class CycleRunner:
    def __init__(
        self,
        *,
        conn,
        layout: DataLayout,
        committer: Committer,
        manifest: CycleManifest,
        progress: CycleProgress,
        observer: Observer,
        execution: ExecutionSettings,
        run: RunContext,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.conn = conn
        self.layout = layout
        self.committer = committer
        self.manifest = manifest
        self.progress = progress
        self.observer = observer
        self.execution = execution
        self.run = run
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.tz = ZoneInfo(manifest.report_timezone)
        self.competitors = {c.competitor_id: c for c in manifest.competitors}
        self._buffer: list[AttemptEvent | JobExpiredEvent] = []
        self._progress_dirty = False
        self._attempts_since_checkpoint = 0
        self._last_checkpoint = 0.0
        self._consecutive_parse_errors = 0

    # ------------------------------------------------------------------ pętla

    def run_window(self) -> WindowResult:
        result = WindowResult()
        if self.progress.status is CycleStatus.BLOCKED:
            result.stop_reason = "cykl zablokowany — okno kończy się bez wejścia na Booking"
            result.blocked = True
            return result
        now = self.clock()
        if self.progress.not_before and self.progress.not_before > now:
            result.stop_reason = f"źródło prosi o przerwę do {self.progress.not_before.isoformat()}"
            return result

        start = self.monotonic()
        self._last_checkpoint = start
        budget = self.execution.collection_budget_minutes_per_job * 60
        estimate = self.execution.estimated_observation_seconds_p95
        queue = _JobQueue(load_open_jobs(self.conn, self.manifest), now)
        if self.progress.status is not CycleStatus.RUNNING:
            self._update_progress(status=CycleStatus.RUNNING)

        try:
            while queue:
                elapsed = self.monotonic() - start
                if elapsed + estimate > budget:
                    result.stop_reason = "budżet okna wyczerpany — reszta dla kolejnego okna"
                    break
                now = self.clock()
                queue.release(now)
                job = queue.pop_ready()
                if job is None:
                    wake_at = queue.next_wait()
                    wait = (wake_at - now).total_seconds()
                    if (
                        wait > self.execution.max_inline_retry_wait_seconds
                        or elapsed + wait + estimate > budget
                    ):
                        result.stop_reason = (
                            "pozostałe zadania czekają na ponowienie "
                            f"(najwcześniej {wake_at.isoformat()})"
                        )
                        break
                    self._checkpoint(result)
                    self.sleep(wait)
                    continue
                if is_expired(job.query.checkin, now, self.tz):
                    self._expire(job, now)
                    result.expired += 1
                    self._maybe_checkpoint(result)
                    continue

                event = self._attempt(job, now, result)
                if event.job_status_after is JobStatus.RETRY_WAIT:
                    queue.push(job.with_attempt(event.attempt_no, event.next_attempt_at))
                if self.progress.status is CycleStatus.BLOCKED:
                    result.blocked = True
                    result.stop_reason = f"cykl zablokowany: {self.progress.blocked.reason}"
                    break
                if self.progress.not_before and self.progress.not_before > self.clock():
                    result.stop_reason = (
                        f"źródło prosi o przerwę do {self.progress.not_before.isoformat()}"
                    )
                    break
                self._maybe_checkpoint(result)
            else:
                result.stop_reason = "brak zadań do wykonania w tym oknie"
        finally:
            self._checkpoint(result, final=True)
        return result

    # ------------------------------------------------------------------ próba

    def _attempt(self, job: JobRow, started_at: datetime, result: WindowResult) -> AttemptEvent:
        competitor = self.competitors[job.query.competitor_id]
        t0 = self.monotonic()
        outcome = self.observer(competitor, job.query)
        result.durations.append(self.monotonic() - t0)
        finished_at = self.clock()
        attempt_no = job.attempts + 1
        status, next_at = self._next_state(outcome, attempt_no, finished_at)
        attempt_id = make_attempt_id(job.job_id, attempt_no)
        observation = None
        if outcome.availability is not None:
            observation = ObservationRecord(
                observation_id=f"obs:{attempt_id}",
                observed_at=outcome.observed_at,
                availability=outcome.availability,
                parser_version=outcome.parser_version or "unknown",
                source_url=outcome.source_url,
                message=_truncate(outcome.message),
                offers=outcome.offers,
            )
        event = AttemptEvent(
            attempt_id=attempt_id,
            cycle_id=self.manifest.cycle_id,
            job_id=job.job_id,
            query=job.query,
            attempt_no=attempt_no,
            started_at=started_at,
            finished_at=finished_at,
            run=self.run,
            technical_reason=outcome.technical_reason,
            message=_truncate(outcome.message),
            job_status_after=status,
            next_attempt_at=next_at,
            observation=observation,
        )
        self._record(event)
        result.attempts += 1
        self._attempts_since_checkpoint += 1
        if status is JobStatus.COMPLETED:
            result.completed += 1
        elif status is JobStatus.FAILED:
            result.failed += 1
        else:
            result.retry_wait += 1
        self._react_to(outcome, event, finished_at)
        log.info(
            "%s %s %s noce=%s → %s%s",
            job.query.competitor_id,
            job.query.checkin,
            f"#{attempt_no}",
            job.query.nights,
            outcome.availability or outcome.technical_reason,
            f" ({status})" if status is not JobStatus.COMPLETED else "",
        )
        return event

    def _next_state(
        self, outcome: ObservationOutcome, attempt_no: int, now: datetime
    ) -> tuple[JobStatus, datetime | None]:
        if outcome.technical_reason is None:
            return JobStatus.COMPLETED, None
        if attempt_no >= self.execution.max_attempts:
            return JobStatus.FAILED, None
        if outcome.technical_reason is TechnicalReason.BLOCKED:
            # Po ręcznym odblokowaniu zadanie jest od razu gotowe.
            return JobStatus.RETRY_WAIT, now
        delay = self.execution.retry_base_delay_seconds * 2 ** (attempt_no - 1)
        delay = max(delay, outcome.retry_after_seconds or 0)
        return JobStatus.RETRY_WAIT, now + timedelta(seconds=delay)

    def _react_to(self, outcome: ObservationOutcome, event: AttemptEvent, now: datetime) -> None:
        reason = outcome.technical_reason
        if reason is TechnicalReason.PARSE_ERROR:
            self._consecutive_parse_errors += 1
        elif reason is None:
            self._consecutive_parse_errors = 0
        if reason is TechnicalReason.BLOCKED:
            self._block("blocked", outcome.message, event.job_id, now)
        elif self._consecutive_parse_errors >= self.execution.max_consecutive_parse_errors:
            self._block(
                "parse_errors",
                f"{self._consecutive_parse_errors} kolejnych błędów parsera — "
                "prawdopodobna zmiana strony; sprawdź diagnostykę",
                event.job_id,
                now,
            )
        elif (
            outcome.retry_after_seconds
            and outcome.retry_after_seconds > self.execution.max_inline_retry_wait_seconds
        ):
            self._update_progress(not_before=now + timedelta(seconds=outcome.retry_after_seconds))

    def _block(self, reason: str, message: str | None, job_id: str, now: datetime) -> None:
        log.warning("Blokada cyklu %s: %s — %s", self.manifest.cycle_id, reason, message)
        self._update_progress(
            status=CycleStatus.BLOCKED,
            blocked=BlockInfo(at=now, reason=reason, message=_truncate(message), job_id=job_id),
        )

    def _expire(self, job: JobRow, now: datetime) -> None:
        self._record(
            JobExpiredEvent(
                cycle_id=self.manifest.cycle_id,
                job_id=job.job_id,
                query=job.query,
                at=now,
                run=self.run,
            )
        )

    # ------------------------------------------------------------------ zapis

    def _record(self, event: AttemptEvent | JobExpiredEvent) -> None:
        apply_event(self.conn, event)
        self.conn.commit()
        self._buffer.append(event)

    def _update_progress(self, **changes) -> None:
        self.progress = self.progress.model_copy(update=changes)
        self._progress_dirty = True

    def _maybe_checkpoint(self, result: WindowResult) -> None:
        due_by_time = (
            self.monotonic() - self._last_checkpoint
            >= self.execution.checkpoint_interval_minutes * 60
        )
        due_by_count = self._attempts_since_checkpoint >= self.execution.checkpoint_max_observations
        if due_by_time or due_by_count:
            self._checkpoint(result)

    def _checkpoint(self, result: WindowResult, *, final: bool = False) -> None:
        """Partia + postęp w jednym commicie; dopiero push oznacza trwały checkpoint (A§8).

        Nieudany push w trakcie okna nie przerywa pracy: commit zostaje lokalnie i trafi
        na serwer z następnym checkpointem. Nieudany push końcowy jest błędem okna.
        """
        if not self._buffer and not self._progress_dirty:
            return
        now = self.clock()
        batches = list(self.progress.batches)
        if self._buffer:
            ref = write_batch(
                self.layout,
                self.manifest.cycle_id,
                new_batch_id(now),
                self._buffer,
                created_at=now,
                run=self.run,
            )
            mark_batch_imported(self.conn, self.manifest.cycle_id, ref)
            self.conn.commit()
            batches.append(ref)
        self.progress = self.progress.model_copy(
            update={
                "batches": tuple(batches),
                "job_counts": job_counts(self.conn, self.manifest.cycle_id),
                "updated_at": now,
            }
        )
        write_model(self.layout.progress_file(self.manifest.cycle_id), self.progress)
        events = len(self._buffer)
        self._buffer = []
        self._progress_dirty = False
        self._attempts_since_checkpoint = 0
        self._last_checkpoint = self.monotonic()
        message = (
            f"checkpoint {self.manifest.cycle_id}: {events} zdarzeń, status {self.progress.status}"
        )
        try:
            self.committer.commit_and_push(message)
        except PushFailedError:
            result.push_failures += 1
            if final:
                raise
            log.warning("Push checkpointu nieudany — ponowię przy następnym checkpoincie")
            return
        result.checkpoints += 1


def expire_overdue_jobs(
    conn, manifest: CycleManifest, *, now: datetime, run: RunContext
) -> list[JobExpiredEvent]:
    """Zdarzenia wygaśnięcia dla otwartych zadań z przeszłą datą przyjazdu (finalizacja)."""
    tz = ZoneInfo(manifest.report_timezone)
    events = [
        JobExpiredEvent(
            cycle_id=manifest.cycle_id, job_id=job.job_id, query=job.query, at=now, run=run
        )
        for job in load_open_jobs(conn, manifest)
        if is_expired(job.query.checkin, now, tz)
    ]
    for event in events:
        apply_event(conn, event)
    conn.commit()
    return events


__all__ = [
    "Committer",
    "CycleRunner",
    "WindowResult",
    "WindowTerminated",
    "expire_overdue_jobs",
]
