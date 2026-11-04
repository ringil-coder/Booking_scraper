"""Wspólne dane testowe dla testów warstwy trwałej (E4)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from booking_monitor.config import AppConfig, parse_competitors, parse_settings
from booking_monitor.models import (
    Availability,
    ChargeBasis,
    ComponentKind,
    CycleStatus,
    JobStatus,
    MealPlan,
    Offer,
    Prepayment,
    PriceCompleteness,
    PriceComponent,
    RateCategory,
    TechnicalReason,
    make_attempt_id,
    make_job_id,
)
from booking_monitor.planner import PlannedQuery, create_manifest, expand_queries
from booking_monitor.storage.batches import write_batch
from booking_monitor.storage.events import (
    AttemptEvent,
    JobExpiredEvent,
    ObservationRecord,
    RunContext,
)
from booking_monitor.storage.files import write_model
from booking_monitor.storage.init import initialize_data_dir
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.state import BatchRef, CycleManifest, CycleProgress

SETTINGS_YAML = """
report_timezone: Europe/Warsaw
own_apartment:
  id: moj_apartament
  name: Mój apartament
profile:
  adults: 2
  children_ages: []
  units: 1
  currency: PLN
  locale: pl-PL
collection:
  horizon_months: 1
  stay_nights: [2, 3, 4]
schedule:
  enabled: true
  interval_hours: 72
  anchor_at_utc: '2026-11-04T05:00:00Z'
"""

COMPETITORS_CSV = """competitor_key;name;booking_url;unit_filter;enabled
morskie_oko;Apartamenty Morskie Oko;https://www.booking.com/hotel/pl/apartamenty-morskie-oko.pl.html?aid=1;;true
stare_miasto;Studio Stare Miasto;https://www.booking.com/hotel/pl/studio-stare-miasto.html;Studio;true
"""

RUN = RunContext(
    workflow_run_id="123",
    workflow_run_attempt=1,
    workflow_job="collect",
    code_sha="abc123",
    runner_environment="github-hosted:ubuntu24",
)
T0 = datetime(2026, 11, 4, 5, 17, tzinfo=UTC)


def make_config() -> AppConfig:
    return AppConfig(
        settings=parse_settings(SETTINGS_YAML), competitors=parse_competitors(COMPETITORS_CSV)
    )


def make_manifest(cycle_date: date = date(2026, 11, 4), slot: int = 0) -> CycleManifest:
    return create_manifest(
        make_config(),
        slot=slot,
        cycle_date=cycle_date,
        created_at=datetime.combine(cycle_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=5, minutes=17),
        run=RUN,
        parser_version="booking-test",
    )


def make_offer(index: int = 0, total: int = 64_000, nights: int = 2) -> Offer:
    return Offer(
        offer_index=index,
        unit_name="Apartament z 1 sypialnią",
        unit_id="123401",
        is_entire_apartment=True,
        max_occupancy=4,
        rate_name="Bezzwrotna",
        rate_category=RateCategory.NON_REFUNDABLE,
        meal_plan=MealPlan.ROOM_ONLY,
        prepayment=Prepayment.PREPAYMENT_REQUIRED,
        discounts=("Oferta specjalna",),
        currency="PLN",
        displayed_price_grosze=total - 2_000,
        total_price_grosze=total,
        avg_night_price_grosze=total // nights,
        price_completeness=PriceCompleteness.COMPLETE,
        deposit_grosze=50_000,
        matches_unit_filter=True,
        selected_for_category=True,
        components=(
            PriceComponent(
                name="Opłata klimatyczna",
                kind=ComponentKind.CITY_TAX,
                basis=ChargeBasis.PER_PERSON_PER_NIGHT,
                amount_grosze=500,
                mandatory=True,
                included=False,
                stay_amount_grosze=2_000,
            ),
            PriceComponent(
                name="VAT",
                kind=ComponentKind.VAT,
                basis=ChargeBasis.PERCENT,
                rate_percent=Decimal("8"),
                mandatory=True,
                included=True,
                stay_amount_grosze=None,
            ),
        ),
    )


def attempt_event(
    manifest: CycleManifest,
    planned: PlannedQuery,
    *,
    attempt_no: int = 1,
    status: JobStatus = JobStatus.COMPLETED,
    at: datetime = T0,
    offers: int = 1,
) -> AttemptEvent:
    query = planned.query
    job_id = make_job_id(manifest.cycle_id, query.query_id)
    attempt_id = make_attempt_id(job_id, attempt_no)
    observation = None
    reason = None
    next_at = None
    if status == JobStatus.COMPLETED:
        observation = ObservationRecord(
            observation_id=f"o:{attempt_id}",
            observed_at=at + timedelta(seconds=10),
            availability=Availability.AVAILABLE if offers else Availability.UNAVAILABLE,
            parser_version="booking-test",
            source_url=f"https://www.booking.com/hotel/pl/x.html?checkin={query.checkin}",
            offers=tuple(make_offer(i, nights=query.nights) for i in range(offers)),
        )
    else:
        reason = TechnicalReason.TIMEOUT
        if status == JobStatus.RETRY_WAIT:
            next_at = at + timedelta(minutes=5)
    return AttemptEvent(
        attempt_id=attempt_id,
        cycle_id=manifest.cycle_id,
        job_id=job_id,
        query=query,
        attempt_no=attempt_no,
        started_at=at,
        finished_at=at + timedelta(seconds=12),
        run=RUN,
        technical_reason=reason,
        message="przekroczono czas" if reason else None,
        job_status_after=status,
        next_attempt_at=next_at,
        observation=observation,
    )


def expired_event(manifest: CycleManifest, planned: PlannedQuery) -> JobExpiredEvent:
    return JobExpiredEvent(
        cycle_id=manifest.cycle_id,
        job_id=make_job_id(manifest.cycle_id, planned.query.query_id),
        query=planned.query,
        at=T0 + timedelta(days=1),
        run=RUN,
    )


def init_layout(tmp_path) -> DataLayout:
    layout = DataLayout(tmp_path / "data")
    initialize_data_dir(layout, make_config().settings, T0)
    return layout


def write_cycle(
    layout: DataLayout,
    manifest: CycleManifest,
    batches: list[list[AttemptEvent | JobExpiredEvent]],
    *,
    status: CycleStatus = CycleStatus.RUNNING,
) -> list[BatchRef]:
    """Zapisuje manifest, partie i `progress.json` tak, jak robi to checkpoint."""
    write_model(layout.manifest_file(manifest.cycle_id), manifest)
    refs = [
        write_batch(
            layout,
            manifest.cycle_id,
            f"b{manifest.cycle_id}-{i:03d}",
            events,
            created_at=T0 + timedelta(minutes=15 * i),
            run=RUN,
        )
        for i, events in enumerate(batches)
    ]
    write_model(
        layout.progress_file(manifest.cycle_id),
        CycleProgress(
            cycle_id=manifest.cycle_id,
            status=status,
            started_at=T0,
            batches=tuple(refs),
            updated_at=T0 + timedelta(hours=1),
        ),
    )
    return refs


def planned_for(manifest: CycleManifest) -> list[PlannedQuery]:
    return expand_queries(manifest)
