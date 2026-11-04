"""Odczyt kolejki zadań cyklu z roboczej bazy SQLite."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import date, datetime

from booking_monitor.models import JobStatus, StayQuery
from booking_monitor.storage.state import CycleManifest

OPEN_STATUSES = (JobStatus.PENDING, JobStatus.RETRY_WAIT, JobStatus.RUNNING)


@dataclass(frozen=True)
class JobRow:
    job_id: str
    status: JobStatus
    attempts: int
    next_attempt_at: datetime | None
    sort_key: int
    query: StayQuery

    def with_attempt(self, attempt_no: int, next_attempt_at: datetime | None) -> JobRow:
        return replace(
            self,
            status=JobStatus.RETRY_WAIT,
            attempts=attempt_no,
            next_attempt_at=next_attempt_at,
        )


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def load_open_jobs(conn: sqlite3.Connection, manifest: CycleManifest) -> list[JobRow]:
    """Niezakończone zadania cyklu w kolejności planu (`sort_key`)."""
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    rows = conn.execute(
        f"""
        SELECT j.id, j.status, j.attempts, j.next_attempt_at, j.sort_key,
               q.id AS query_id, q.competitor_id, q.checkin, q.checkout, q.nights,
               q.profile_hash, q.source
        FROM jobs j JOIN stay_queries q ON q.id = j.query_id
        WHERE j.cycle_id = ? AND j.status IN ({placeholders})
        ORDER BY j.sort_key
        """,
        (manifest.cycle_id, *[s.value for s in OPEN_STATUSES]),
    ).fetchall()
    return [
        JobRow(
            job_id=row[0],
            status=JobStatus(row[1]),
            attempts=row[2],
            next_attempt_at=_parse_dt(row[3]),
            sort_key=row[4],
            query=StayQuery(
                query_id=row[5],
                competitor_id=row[6],
                checkin=date.fromisoformat(row[7]),
                checkout=date.fromisoformat(row[8]),
                nights=row[9],
                profile_hash=row[10],
                source=row[11],
            ),
        )
        for row in rows
    ]


def job_counts(conn: sqlite3.Connection, cycle_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM jobs WHERE cycle_id = ? GROUP BY status ORDER BY status",
        (cycle_id,),
    ).fetchall()
    return {status: count for status, count in rows}


@dataclass(frozen=True)
class QueueSummary:
    open_jobs: int
    due_jobs: int
    waiting_jobs: int
    next_attempt_at: datetime | None


def summarize_queue(
    conn: sqlite3.Connection,
    manifest: CycleManifest,
    *,
    now: datetime,
    not_before: datetime | None = None,
) -> QueueSummary:
    """Ile zadań można wykonać teraz, a ile czeka na ponowienie (dla kontroli terminu)."""
    jobs = load_open_jobs(conn, manifest)
    waiting = [j for j in jobs if j.next_attempt_at is not None and j.next_attempt_at > now]
    due = len(jobs) - len(waiting)
    next_at = min((j.next_attempt_at for j in waiting), default=None)
    if not_before is not None and not_before > now and jobs:
        return QueueSummary(len(jobs), 0, len(jobs), not_before)
    return QueueSummary(len(jobs), due, len(waiting), next_at)
