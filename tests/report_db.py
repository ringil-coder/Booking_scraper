"""Budowanie syntetycznej bazy SQLite do testów raportu.

Wstawia wiersze bezpośrednio wg `storage/schema.sql` — niezależnie od kodu importu
partii (rozwijanego osobno), żeby testy raportu sprawdzały wyłącznie analitykę.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import booking_monitor.storage as storage_pkg
from booking_monitor.models import make_attempt_id, make_job_id, make_query_id

SCHEMA_PATH = Path(storage_pkg.__file__).parent / "schema.sql"
SOURCE = "booking_browser"


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


@dataclass
class OfferSpec:
    total: int | None
    nights: int
    category: str = "free_cancellation"
    meal_plan: str = "room_only"
    prepayment: str = "no_prepayment"
    complete: bool = True
    selected: bool = True
    matches: bool = True
    currency: str = "PLN"
    unit_name: str = "Apartament z 1 sypialnią"
    displayed: int | None = None
    avg: int | None = None
    free_cancellation_until: date | None = None


def offer(total: int | None, nights: int, **kwargs) -> OfferSpec:
    return OfferSpec(total=total, nights=nights, **kwargs)


@dataclass
class ReportDb:
    conn: sqlite3.Connection = field(default_factory=lambda: sqlite3.connect(":memory:"))
    profile_hash: str = "p_test"
    _observation_seq: int = 0

    def __post_init__(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------------ słowniki
    def add_apartment(self, apartment_id: str = "moj_apartament", name: str = "Mój apartament"):
        self.conn.execute(
            "INSERT INTO own_apartment (id, name, booking_url) VALUES (?, ?, NULL)",
            (apartment_id, name),
        )

    def add_profile(self, *, adults: int = 2, currency: str = "PLN") -> str:
        self.conn.execute(
            """INSERT INTO search_profiles (profile_hash, adults, children_ages, units, currency,
               locale, device, logged_in, accommodation_type)
               VALUES (?, ?, '[]', 1, ?, 'pl-PL', 'desktop', 0, 'entire_apartment')""",
            (self.profile_hash, adults, currency),
        )
        return self.profile_hash

    def add_competitor(self, competitor_id: str, name: str | None = None) -> None:
        url = f"https://www.booking.com/hotel/pl/{competitor_id.replace('_', '-')}.html"
        self.conn.execute(
            """INSERT INTO competitors (id, name, canonical_url, booking_url, unit_filter, enabled)
               VALUES (?, ?, ?, ?, NULL, 1)""",
            (competitor_id, name or competitor_id.upper(), url, url),
        )

    # ------------------------------------------------------------------ cykle
    def add_cycle(
        self,
        cycle_id: str,
        *,
        slot: int,
        cycle_date: date,
        end_exclusive: date,
        competitors: list[str],
        status: str = "completed",
        nights: tuple[int, ...] = (2, 3, 4),
        created_at: datetime | None = None,
        blocked_reason: str | None = None,
    ) -> None:
        created = created_at or datetime.combine(cycle_date, datetime.min.time(), tzinfo=UTC)
        self.conn.execute(
            """INSERT INTO cycles (id, schedule_slot, cycle_date, start_date, end_date_exclusive,
               stay_nights, status, profile_hash, config_hash, code_sha, parser_version,
               query_count, created_at, started_at, finished_at, blocked_reason, blocked_message)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'cfg_test', 'abc123', 'booking-0.1.0', 0, ?, ?, ?,
                       ?, ?)""",
            (
                cycle_id,
                slot,
                cycle_date.isoformat(),
                cycle_date.isoformat(),
                end_exclusive.isoformat(),
                json.dumps(list(nights)),
                status,
                self.profile_hash,
                created.isoformat(),
                created.isoformat(),
                (created + timedelta(hours=20)).isoformat(),
                blocked_reason,
                "CAPTCHA na stronie" if blocked_reason else None,
            ),
        )
        for competitor_id in competitors:
            row = self.conn.execute(
                "SELECT name FROM competitors WHERE id = ?", (competitor_id,)
            ).fetchone()
            self.conn.execute(
                "INSERT INTO cycle_competitors (cycle_id, competitor_id, name, unit_filter) "
                "VALUES (?, ?, ?, NULL)",
                (cycle_id, competitor_id, row["name"]),
            )

    def add_job(
        self,
        cycle_id: str,
        competitor_id: str,
        checkin: date,
        nights: int,
        *,
        status: str = "completed",
        attempts: int = 1,
        last_reason: str | None = None,
        last_message: str | None = None,
    ) -> tuple[str, str]:
        checkout = checkin + timedelta(days=nights)
        query_id = make_query_id(competitor_id, checkin, checkout, self.profile_hash, SOURCE)
        self.conn.execute(
            """INSERT OR IGNORE INTO stay_queries
               (id, competitor_id, checkin, checkout, nights, profile_hash, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                query_id,
                competitor_id,
                checkin.isoformat(),
                checkout.isoformat(),
                nights,
                self.profile_hash,
                SOURCE,
            ),
        )
        job_id = make_job_id(cycle_id, query_id)
        self.conn.execute(
            """INSERT INTO jobs (id, cycle_id, query_id, status, attempts, last_reason,
               last_message, sort_key) VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (job_id, cycle_id, query_id, status, attempts, last_reason, last_message),
        )
        return job_id, query_id

    def observe(
        self,
        cycle_id: str,
        competitor_id: str,
        checkin: date,
        nights: int,
        *,
        observed_at: datetime,
        availability: str = "available",
        offers: list[OfferSpec] | tuple[OfferSpec, ...] = (),
        message: str | None = None,
    ) -> str:
        """Zadanie zakończone obserwacją (job + próba + obserwacja + oferty)."""
        job_id, query_id = self.add_job(cycle_id, competitor_id, checkin, nights)
        attempt_id = make_attempt_id(job_id, 1)
        self.conn.execute(
            """INSERT INTO attempts (id, job_id, cycle_id, attempt_no, started_at, finished_at,
               workflow_run_id, workflow_run_attempt, code_sha, runner_environment,
               technical_reason, message, job_status_after, batch_id)
               VALUES (?, ?, ?, 1, ?, ?, '1', 1, 'abc123', 'test', NULL, NULL, 'completed',
                       'b1')""",
            (attempt_id, job_id, cycle_id, observed_at.isoformat(), observed_at.isoformat()),
        )
        self._observation_seq += 1
        observation_id = f"o{self._observation_seq:05d}"
        self.conn.execute(
            """INSERT INTO observations (id, attempt_id, query_id, cycle_id, observed_at,
               availability, parser_version, source_url, message)
               VALUES (?, ?, ?, ?, ?, ?, 'booking-0.1.0', NULL, ?)""",
            (
                observation_id,
                attempt_id,
                query_id,
                cycle_id,
                observed_at.isoformat(),
                availability,
                message,
            ),
        )
        for index, spec in enumerate(offers):
            avg = spec.avg
            if avg is None and spec.total is not None and spec.complete:
                avg = (spec.total + spec.nights // 2) // spec.nights
            total = spec.total if spec.complete else None
            self.conn.execute(
                """INSERT INTO offers (observation_id, offer_index, unit_name, unit_id,
                   is_entire_apartment, max_occupancy, units, rate_name, rate_category,
                   free_cancellation_until, meal_plan, prepayment, discounts, currency,
                   displayed_price_grosze, original_price_grosze, total_price_grosze,
                   avg_night_price_grosze, price_completeness, deposit_grosze,
                   matches_unit_filter, selected_for_category)
                   VALUES (?, ?, ?, NULL, 1, 4, 1, NULL, ?, ?, ?, ?, '[]', ?, ?, NULL, ?, ?, ?,
                           NULL, ?, ?)""",
                (
                    observation_id,
                    index,
                    spec.unit_name,
                    spec.category,
                    spec.free_cancellation_until.isoformat()
                    if spec.free_cancellation_until
                    else None,
                    spec.meal_plan,
                    spec.prepayment,
                    spec.currency,
                    spec.displayed if spec.displayed is not None else (spec.total or 0),
                    total,
                    avg if spec.complete else None,
                    "complete" if spec.complete else "partial",
                    int(spec.matches),
                    int(spec.selected),
                ),
            )
        return observation_id


# --------------------------------------------------------------------------- scenariusz

COMPETITORS = ["k1", "k2", "k3", "k4", "k5", "k6"]
PREVIOUS_CYCLE = "c20261101-s0000"
CURRENT_CYCLE = "c20261104-s0001"
D4 = date(2026, 11, 4)
D5 = date(2026, 11, 5)


def build_scenario(current_status: str = "partial") -> ReportDb:
    """Dwa cykle, 6 konkurentów, mieszanka wyników z opisu testów E6."""
    db = ReportDb()
    db.add_apartment()
    db.add_profile()
    for competitor_id in COMPETITORS:
        db.add_competitor(competitor_id, name=f"Konkurent {competitor_id[1:]}")

    # --- poprzedni cykl
    db.add_cycle(
        PREVIOUS_CYCLE,
        slot=0,
        cycle_date=date(2026, 11, 1),
        end_exclusive=date(2026, 11, 6),
        competitors=COMPETITORS,
        status="completed",
    )
    t_prev = utc(2026, 11, 1, 8)
    db.observe(PREVIOUS_CYCLE, "k1", D4, 2, observed_at=t_prev, offers=[offer(42000, 2)])
    db.observe(PREVIOUS_CYCLE, "k5", D4, 2, observed_at=t_prev, offers=[offer(30000, 2)])
    db.observe(PREVIOUS_CYCLE, "k6", D4, 2, observed_at=t_prev, offers=[offer(39000, 2)])

    # --- bieżący cykl
    db.add_cycle(
        CURRENT_CYCLE,
        slot=1,
        cycle_date=D4,
        end_exclusive=date(2026, 11, 6),
        competitors=COMPETITORS,
        status=current_status,
    )
    t1 = utc(2026, 11, 4, 6, 0)
    t_last = utc(2026, 11, 4, 19, 30)
    # 04.11 × 2 noce: dwie porównywalne ceny (mała próba)
    db.observe(
        CURRENT_CYCLE,
        "k1",
        D4,
        2,
        observed_at=t1,
        offers=[
            offer(40000, 2, category="non_refundable", prepayment="prepayment_required"),
            offer(46000, 2, category="free_cancellation", free_cancellation_until=D4),
        ],
    )
    db.observe(
        CURRENT_CYCLE,
        "k2",
        D4,
        2,
        observed_at=t1 + timedelta(minutes=1),
        offers=[
            offer(50000, 2, category="free_cancellation"),
            # droższa oferta ze śniadaniem — niewybrana w kategorii
            offer(52000, 2, category="free_cancellation", meal_plan="breakfast", selected=False),
        ],
    )
    db.observe(
        CURRENT_CYCLE,
        "k3",
        D4,
        2,
        observed_at=t1 + timedelta(minutes=2),
        offers=[offer(35000, 2, category="non_refundable", complete=False, displayed=35000)],
    )
    db.observe(
        CURRENT_CYCLE,
        "k4",
        D4,
        2,
        observed_at=t1 + timedelta(minutes=3),
        availability="no_matching_offer",
        offers=[offer(20000, 2, matches=False, unit_name="Pokój dwuosobowy")],
    )
    db.observe(
        CURRENT_CYCLE,
        "k5",
        D4,
        2,
        observed_at=t1 + timedelta(minutes=4),
        availability="unavailable",
        message="Brak wolnych miejsc w wybranym terminie",
    )
    db.add_job(
        CURRENT_CYCLE,
        "k6",
        D4,
        2,
        status="failed",
        attempts=3,
        last_reason="blocked",
        last_message="=CAPTCHA na stronie",
    )

    # 05.11 × 3 noce: sześć porównywalnych cen
    totals = [60000, 61000, 62000, 63000, 64000, 65001]
    avgs = [20000, 20333, 20667, 21000, 21333, 21667]
    for i, competitor_id in enumerate(COMPETITORS):
        db.observe(
            CURRENT_CYCLE,
            competitor_id,
            D5,
            3,
            observed_at=t1 + timedelta(hours=1, minutes=i),
            offers=[offer(totals[i], 3, avg=avgs[i], category="non_refundable")],
        )

    # 05.11 × 2 noce: braki i błędy
    db.add_job(CURRENT_CYCLE, "k1", D5, 2, status="expired", attempts=0)
    db.add_job(CURRENT_CYCLE, "k2", D5, 2, status="pending", attempts=0)
    db.add_job(CURRENT_CYCLE, "k3", D5, 2, status="retry_wait", attempts=1, last_reason="timeout")
    db.add_job(CURRENT_CYCLE, "k4", D5, 2, status="failed", attempts=3, last_reason="parse_error")
    db.observe(CURRENT_CYCLE, "k5", D5, 2, observed_at=t_last, offers=[offer(55000, 2)])
    db.observe(
        CURRENT_CYCLE,
        "k6",
        D5,
        2,
        observed_at=t_last - timedelta(hours=1),
        offers=[offer(57000, 2, currency="EUR")],
    )
    db.conn.commit()
    return db
