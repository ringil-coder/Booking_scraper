"""Planowanie cyklu: zamrożony manifest, rozwinięcie zapytań i liczba okien (A§7).

Plan nie jest zapisywany jako lista zapytań — jest deterministycznie odtwarzany
z manifestu (`expand_queries`) i weryfikowany skrótem `plan_digest`.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from booking_monitor.config import AppConfig
from booking_monitor.dates import CycleWindow
from booking_monitor.models import StayQuery
from booking_monitor.storage.events import RunContext
from booking_monitor.storage.state import CompetitorSnapshot, CycleManifest


@dataclass(frozen=True)
class PlannedQuery:
    """Zapytanie z kluczem kolejności wykonywania (PLAN_DZIALANIA.md P4)."""

    sort_key: int
    query: StayQuery


def make_cycle_id(cycle_date: date, slot: int) -> str:
    return f"c{cycle_date:%Y%m%d}-s{slot:04d}"


def expand_queries(manifest: CycleManifest) -> list[PlannedQuery]:
    """Wszystkie zapytania cyklu w kolejności wykonywania.

    Kolejność: najbliższe daty przyjazdu najpierw (wygasają najwcześniej), potem
    długość pobytu, a w obrębie (data, noce) przeplot konkurentów, żeby kolejne
    nawigacje nie trafiały seriami do tej samej oferty.
    """
    window = CycleWindow(start=manifest.start_date, end_exclusive=manifest.end_date_exclusive)
    planned: list[PlannedQuery] = []
    for checkin, _checkout, nights in window.stays(manifest.stay_nights):
        for competitor in manifest.competitors:
            query = StayQuery.build(
                competitor_id=competitor.competitor_id,
                checkin=checkin,
                nights=nights,
                profile_hash=manifest.profile_hash,
                source=manifest.source,
            )
            planned.append(PlannedQuery(sort_key=len(planned), query=query))
    return planned


def plan_digest(queries: Sequence[StayQuery]) -> str:
    ids = sorted(q.query_id for q in queries)
    if len(ids) != len(set(ids)):
        raise ValueError("plan zawiera zduplikowane zapytania")
    return "sha256:" + hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()


def create_manifest(
    config: AppConfig,
    *,
    slot: int,
    cycle_date: date,
    created_at: datetime,
    run: RunContext,
    parser_version: str,
) -> CycleManifest:
    """Zamraża zakres, konkurentów i profil dla całego cyklu (A§2, A§7 pkt 1)."""
    settings = config.settings
    window = CycleWindow.for_cycle(cycle_date, settings.collection.horizon_months)
    draft = CycleManifest(
        cycle_id=make_cycle_id(cycle_date, slot),
        schedule_slot=slot,
        cycle_date=cycle_date,
        start_date=window.start,
        end_date_exclusive=window.end_exclusive,
        stay_nights=settings.collection.stay_nights,
        report_timezone=settings.report_timezone,
        own_apartment=settings.own_apartment,
        profile=settings.profile,
        profile_hash=config.profile_hash,
        source=settings.collection.source,
        competitors=tuple(
            CompetitorSnapshot(
                competitor_id=c.competitor_id,
                name=c.name,
                booking_url=c.booking_url,
                canonical_url=c.canonical_url,
                unit_filter=c.unit_filter,
            )
            for c in config.competitors
        ),
        config_hash=config.config_hash(),
        code_sha=run.code_sha,
        parser_version=parser_version,
        created_at=created_at,
        created_by=run,
        query_count=0,
        plan_digest="",
    )
    queries = [p.query for p in expand_queries(draft)]
    return draft.model_copy(
        update={"query_count": len(queries), "plan_digest": plan_digest(queries)}
    )


def verify_manifest_plan(manifest: CycleManifest) -> list[PlannedQuery]:
    """Odtwarza plan i sprawdza zgodność z manifestem; niezgodność = błąd odtworzenia."""
    planned = expand_queries(manifest)
    queries = [p.query for p in planned]
    if len(queries) != manifest.query_count or plan_digest(queries) != manifest.plan_digest:
        raise ValueError(
            f"plan cyklu {manifest.cycle_id} nie zgadza się z manifestem "
            "(inna wersja kodu planowania?)"
        )
    return planned


def windows_needed(
    remaining_jobs: int,
    *,
    seconds_per_job_p95: float,
    margin: float,
    budget_minutes: int,
    max_windows: int,
) -> int:
    """Liczba okien roboczych (jobów macierzy) dla pozostałej pracy (A§7 pkt 2)."""
    if remaining_jobs <= 0:
        return 0
    needed_seconds = remaining_jobs * seconds_per_job_p95 * (1 + margin)
    windows = math.ceil(needed_seconds / (budget_minutes * 60))
    return max(1, min(windows, max_windows))
