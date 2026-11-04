"""Robocza baza SQLite (A§9) — indeks odtwarzany z manifestów i partii.

Konwencje zapisu:
- momenty: ISO 8601 w UTC ze stałą szerokością, np. `2026-11-04T05:17:00.000000+00:00`
  (`to_db_timestamp`) — porządek leksykograficzny = chronologiczny;
- daty pobytów: `YYYY-MM-DD`;
- wyliczenia: wartość tekstowa (`StrEnum.value`); listy: JSON; `rate_percent`: tekst Decimal;
- wartości logiczne: 0/1.

Funkcje publiczne zatwierdzają własne transakcje (`with conn:`), więc runner może
stosować zdarzenia na bieżąco, a import partii jest atomowy.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from booking_monitor.models import (
    CycleStatus,
    JobStatus,
    Offer,
    StayQuery,
)
from booking_monitor.planner import PlannedQuery
from booking_monitor.storage.batches import AnyEvent, BatchIntegrityError, read_batch
from booking_monitor.storage.events import AttemptEvent, JobExpiredEvent
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.state import BatchRef, CycleManifest, CycleProgress


def to_db_timestamp(moment: datetime) -> str:
    """Moment jako tekst ISO 8601 w UTC o stałej szerokości (z mikrosekundami)."""
    if moment.tzinfo is None:
        raise ValueError("oczekiwano czasu ze strefą (UTC)")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def from_db_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _ts(moment: datetime | None) -> str | None:
    return to_db_timestamp(moment) if moment is not None else None


def _bool(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _schema_sql() -> str:
    return resources.files("booking_monitor.storage").joinpath("schema.sql").read_text("utf-8")


def open_database(path: Path | str) -> sqlite3.Connection:
    """Otwiera (lub tworzy) bazę i nakłada schemat. Obsługuje `":memory:"`."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_schema_sql())
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --------------------------------------------------------------------------- manifest


def import_manifest(
    conn: sqlite3.Connection,
    manifest: CycleManifest,
    planned: Sequence[PlannedQuery],
    progress: CycleProgress | None = None,
) -> None:
    """Zakłada cykl, zapytania i zadania (`pending`); idempotentne dla tego samego manifestu."""
    if len(planned) != manifest.query_count:
        raise ValueError(
            f"cykl {manifest.cycle_id}: {len(planned)} zapytań, manifest: {manifest.query_count}"
        )
    with conn:
        existing = conn.execute(
            "SELECT config_hash, query_count FROM cycles WHERE id = ?", (manifest.cycle_id,)
        ).fetchone()
        if existing is not None and (
            existing["config_hash"] != manifest.config_hash
            or existing["query_count"] != manifest.query_count
        ):
            raise ValueError(f"cykl {manifest.cycle_id} już istnieje z innym manifestem")

        apartment = manifest.own_apartment
        conn.execute(
            """INSERT INTO own_apartment (id, name, booking_url) VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name,
                                             booking_url = excluded.booking_url""",
            (apartment.id, apartment.name, apartment.booking_url),
        )
        for competitor in manifest.competitors:
            conn.execute(
                """INSERT INTO competitors
                       (id, name, canonical_url, booking_url, unit_filter, enabled)
                   VALUES (?, ?, ?, ?, ?, 1)
                   ON CONFLICT(id) DO UPDATE SET name = excluded.name,
                       canonical_url = excluded.canonical_url,
                       booking_url = excluded.booking_url,
                       unit_filter = excluded.unit_filter""",
                (
                    competitor.competitor_id,
                    competitor.name,
                    competitor.canonical_url,
                    competitor.booking_url,
                    competitor.unit_filter,
                ),
            )
        profile = manifest.profile
        conn.execute(
            """INSERT OR IGNORE INTO search_profiles
                   (profile_hash, adults, children_ages, units, currency, locale, device,
                    logged_in, accommodation_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                manifest.profile_hash,
                profile.adults,
                json.dumps(list(profile.children_ages)),
                profile.units,
                profile.currency,
                profile.locale,
                profile.device,
                int(profile.logged_in),
                profile.accommodation_type,
            ),
        )
        status = progress.status if progress is not None else CycleStatus.PLANNED
        conn.execute(
            """INSERT INTO cycles
                   (id, schedule_slot, cycle_date, start_date, end_date_exclusive, stay_nights,
                    status, profile_hash, config_hash, code_sha, parser_version, query_count,
                    created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (
                manifest.cycle_id,
                manifest.schedule_slot,
                manifest.cycle_date.isoformat(),
                manifest.start_date.isoformat(),
                manifest.end_date_exclusive.isoformat(),
                json.dumps(list(manifest.stay_nights)),
                str(status),
                manifest.profile_hash,
                manifest.config_hash,
                manifest.code_sha,
                manifest.parser_version,
                manifest.query_count,
                to_db_timestamp(manifest.created_at),
            ),
        )
        if progress is not None:
            _set_cycle_status(conn, manifest.cycle_id, progress)
        conn.executemany(
            """INSERT OR IGNORE INTO cycle_competitors (cycle_id, competitor_id, name, unit_filter)
               VALUES (?, ?, ?, ?)""",
            [
                (manifest.cycle_id, c.competitor_id, c.name, c.unit_filter)
                for c in manifest.competitors
            ],
        )
        conn.executemany(
            """INSERT OR IGNORE INTO stay_queries
                   (id, competitor_id, checkin, checkout, nights, profile_hash, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [_query_row(p.query) for p in planned],
        )
        conn.executemany(
            """INSERT OR IGNORE INTO jobs (id, cycle_id, query_id, status, attempts, sort_key)
               VALUES (?, ?, ?, 'pending', 0, ?)""",
            [
                (
                    f"{manifest.cycle_id}:{p.query.query_id}",
                    manifest.cycle_id,
                    p.query.query_id,
                    p.sort_key,
                )
                for p in planned
            ],
        )


def _query_row(query: StayQuery) -> tuple[object, ...]:
    return (
        query.query_id,
        query.competitor_id,
        query.checkin.isoformat(),
        query.checkout.isoformat(),
        query.nights,
        query.profile_hash,
        query.source,
    )


# --------------------------------------------------------------------------- zdarzenia


def _job_row(conn: sqlite3.Connection, job_id: str, query: StayQuery) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise ValueError(f"zdarzenie dla nieznanego zadania {job_id} (brak manifestu cyklu?)")
    if row["query_id"] != query.query_id:
        raise ValueError(f"zadanie {job_id}: zapytanie zdarzenia nie zgadza się z planem")
    return row


def _insert_offer(conn: sqlite3.Connection, observation_id: str, offer: Offer) -> None:
    cursor = conn.execute(
        """INSERT INTO offers
               (observation_id, offer_index, unit_name, unit_id, is_entire_apartment,
                max_occupancy, units, rate_name, rate_category, free_cancellation_until,
                meal_plan, prepayment, discounts, currency, displayed_price_grosze,
                original_price_grosze, total_price_grosze, avg_night_price_grosze,
                price_completeness, deposit_grosze, matches_unit_filter, selected_for_category)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            observation_id,
            offer.offer_index,
            offer.unit_name,
            offer.unit_id,
            _bool(offer.is_entire_apartment),
            offer.max_occupancy,
            offer.units,
            offer.rate_name,
            str(offer.rate_category),
            offer.free_cancellation_until.isoformat() if offer.free_cancellation_until else None,
            str(offer.meal_plan),
            str(offer.prepayment),
            json.dumps(list(offer.discounts), ensure_ascii=False),
            offer.currency,
            offer.displayed_price_grosze,
            offer.original_price_grosze,
            offer.total_price_grosze,
            offer.avg_night_price_grosze,
            str(offer.price_completeness),
            offer.deposit_grosze,
            int(offer.matches_unit_filter),
            int(offer.selected_for_category),
        ),
    )
    offer_id = cursor.lastrowid
    conn.executemany(
        """INSERT INTO price_components
               (offer_id, name, kind, basis, amount_grosze, rate_percent, mandatory, included,
                stay_amount_grosze)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                offer_id,
                c.name,
                str(c.kind),
                str(c.basis),
                c.amount_grosze,
                str(c.rate_percent) if c.rate_percent is not None else None,
                int(c.mandatory),
                int(c.included),
                c.stay_amount_grosze,
            )
            for c in offer.components
        ],
    )


def _apply_attempt(conn: sqlite3.Connection, event: AttemptEvent, batch_id: str | None) -> None:
    job = _job_row(conn, event.job_id, event.query)
    if job["cycle_id"] != event.cycle_id:
        raise ValueError(f"zadanie {event.job_id} należy do innego cyklu")
    conn.execute(
        """INSERT INTO attempts
               (id, job_id, cycle_id, attempt_no, started_at, finished_at, workflow_run_id,
                workflow_run_attempt, code_sha, runner_environment, technical_reason, message,
                job_status_after, batch_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event.attempt_id,
            event.job_id,
            event.cycle_id,
            event.attempt_no,
            to_db_timestamp(event.started_at),
            to_db_timestamp(event.finished_at),
            event.run.workflow_run_id,
            event.run.workflow_run_attempt,
            event.run.code_sha,
            event.run.runner_environment,
            str(event.technical_reason) if event.technical_reason else None,
            event.message,
            str(event.job_status_after),
            batch_id,
        ),
    )
    observation = event.observation
    if observation is not None:
        conn.execute(
            """INSERT INTO observations
                   (id, attempt_id, query_id, cycle_id, observed_at, availability,
                    parser_version, source_url, message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                observation.observation_id,
                event.attempt_id,
                event.query.query_id,
                event.cycle_id,
                to_db_timestamp(observation.observed_at),
                str(observation.availability),
                observation.parser_version,
                observation.source_url,
                observation.message,
            ),
        )
        for offer in observation.offers:
            _insert_offer(conn, observation.observation_id, offer)
    conn.execute(
        """UPDATE jobs
              SET status = ?, attempts = MAX(attempts, ?), next_attempt_at = ?,
                  last_reason = ?, last_message = ?, lease_until = NULL
            WHERE id = ?""",
        (
            str(event.job_status_after),
            event.attempt_no,
            _ts(event.next_attempt_at),
            str(event.technical_reason) if event.technical_reason else None,
            event.message,
            event.job_id,
        ),
    )


def _apply_expired(conn: sqlite3.Connection, event: JobExpiredEvent) -> None:
    job = _job_row(conn, event.job_id, event.query)
    if job["status"] == JobStatus.COMPLETED:
        raise ValueError(f"zadanie {event.job_id} jest ukończone — nie może wygasnąć")
    conn.execute(
        """UPDATE jobs SET status = 'expired', lease_until = NULL, next_attempt_at = NULL,
                           last_reason = NULL, last_message = ?
            WHERE id = ?""",
        (event.reason, event.job_id),
    )


def _apply(conn: sqlite3.Connection, event: AnyEvent, batch_id: str | None) -> None:
    if isinstance(event, AttemptEvent):
        _apply_attempt(conn, event, batch_id)
    else:
        _apply_expired(conn, event)


def apply_event(conn: sqlite3.Connection, event: AnyEvent, *, batch_id: str | None = None) -> None:
    """Stosuje jedno zdarzenie w osobnej transakcji.

    Powtórzona próba (ten sam `attempt_id` lub para zadanie + numer próby) zgłasza
    `sqlite3.IntegrityError` — to gwarancja braku zdublowanych prób (A§15 pkt 5).
    """
    with conn:
        _apply(conn, event, batch_id)


# --------------------------------------------------------------------------- partie


def _imported_sha(conn: sqlite3.Connection, batch_id: str) -> str | None:
    row = conn.execute(
        "SELECT sha256 FROM imported_batches WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    return None if row is None else str(row["sha256"])


def _record_batch(conn: sqlite3.Connection, cycle_id: str, ref: BatchRef) -> None:
    conn.execute(
        """INSERT INTO imported_batches (batch_id, cycle_id, sha256, events, imported_at)
           VALUES (?, ?, ?, ?, ?)""",
        (ref.batch_id, cycle_id, ref.sha256, ref.events, to_db_timestamp(datetime.now(UTC))),
    )


def import_batch(
    conn: sqlite3.Connection, layout: DataLayout, cycle_id: str, ref: BatchRef
) -> bool:
    """Importuje partię atomowo. Zwraca False, jeśli była już zaimportowana."""
    known = _imported_sha(conn, ref.batch_id)
    if known is not None:
        if known != ref.sha256:
            raise BatchIntegrityError(
                f"partia {ref.batch_id} była zaimportowana z inną sumą kontrolną"
            )
        return False
    events = read_batch(layout, cycle_id, ref)
    with conn:
        for event in events:
            _apply(conn, event, ref.batch_id)
        _record_batch(conn, cycle_id, ref)
    return True


def mark_batch_imported(
    conn: sqlite3.Connection,
    cycle_id: str,
    ref: BatchRef,
    events: Sequence[AnyEvent] | None = None,
) -> None:
    """Rejestruje partię, której zdarzenia runner zastosował już na bieżąco.

    Opcjonalnie uzupełnia `attempts.batch_id` dla prób z tej partii.
    """
    known = _imported_sha(conn, ref.batch_id)
    if known is not None:
        if known != ref.sha256:
            raise BatchIntegrityError(
                f"partia {ref.batch_id} była zarejestrowana z inną sumą kontrolną"
            )
        return
    with conn:
        _record_batch(conn, cycle_id, ref)
        for event in events or ():
            if isinstance(event, AttemptEvent):
                conn.execute(
                    "UPDATE attempts SET batch_id = ? WHERE id = ?",
                    (ref.batch_id, event.attempt_id),
                )


# --------------------------------------------------------------------------- cykle


def _set_cycle_status(conn: sqlite3.Connection, cycle_id: str, progress: CycleProgress) -> None:
    if progress.cycle_id != cycle_id:
        raise ValueError(f"postęp cyklu {progress.cycle_id} podany dla {cycle_id}")
    blocked = progress.blocked
    cursor = conn.execute(
        """UPDATE cycles
              SET status = ?, started_at = ?, finished_at = ?, blocked_reason = ?,
                  blocked_message = ?
            WHERE id = ?""",
        (
            str(progress.status),
            _ts(progress.started_at),
            _ts(progress.finished_at),
            blocked.reason if blocked else None,
            blocked.message if blocked else None,
            cycle_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError(f"nieznany cykl {cycle_id}")


def set_cycle_status(conn: sqlite3.Connection, cycle_id: str, progress: CycleProgress) -> None:
    """Przepisuje status, czasy i informację o blokadzie z `progress.json`."""
    with conn:
        _set_cycle_status(conn, cycle_id, progress)


def backup_database(conn: sqlite3.Connection, dest: Path) -> None:
    """Spójna kopia bazy przez SQLite Online Backup API (A§8)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
