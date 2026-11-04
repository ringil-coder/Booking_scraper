"""Kontrola terminu cyklu (ARCHITEKTURA.md §6).

    slot = floor((now_utc - anchor_at) / interval)

Funkcja `decide` jest czysta: dostaje czas, ustawienia, zapisany stan i skrót
aktywnego cyklu, a zwraca decyzję. Nie otwiera Booking i nie dotyka plików.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from booking_monitor.config import ScheduleSettings
from booking_monitor.dates import local_date
from booking_monitor.models import FINISHED_CYCLE_STATUSES, CycleStatus
from booking_monitor.storage.state import SchedulerState


class GateMode(StrEnum):
    DUE = "due"  # zwykła kontrola terminu (cron lub ręcznie)
    RESUME = "resume"  # wznowienie istniejącego cyklu
    UNBLOCK = "unblock"  # świadome wznowienie cyklu zablokowanego (D7)


class GateAction(StrEnum):
    IDLE = "idle"
    START = "start"
    RESUME = "resume"
    FINALIZE = "finalize"  # wszystkie zadania zakończone, brakuje tylko finalizacji
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ActiveCycle:
    """Skrót aktywnego cyklu odtworzony z gałęzi danych."""

    cycle_id: str
    slot: int
    status: CycleStatus
    due_jobs: int  # zadania gotowe do wykonania teraz
    waiting_jobs: int  # zadania czekające na ponowienie
    next_attempt_at: datetime | None = None


@dataclass(frozen=True)
class GateDecision:
    action: GateAction
    reason: str
    current_slot: int | None = None
    cycle_id: str | None = None
    new_slot: int | None = None
    cycle_date: date | None = None
    overdue: bool = False
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_collection(self) -> bool:
        return self.action in {GateAction.START, GateAction.RESUME}


def _require_aware(moment: datetime) -> None:
    if moment.tzinfo is None:
        raise ValueError("oczekiwano czasu ze strefą (UTC)")


def current_slot(now: datetime, anchor: datetime, interval_hours: int) -> int | None:
    """Numer slotu dla chwili `now`; None przed datą bazową."""
    _require_aware(now)
    _require_aware(anchor)
    if now < anchor:
        return None
    return int((now - anchor) // timedelta(hours=interval_hours))


def slot_start(anchor: datetime, interval_hours: int, slot: int) -> datetime:
    return anchor + slot * timedelta(hours=interval_hours)


def decide(
    *,
    now: datetime,
    schedule: ScheduleSettings,
    state: SchedulerState,
    active: ActiveCycle | None,
    mode: GateMode,
    tz: ZoneInfo,
) -> GateDecision:
    """Decyzja kontroli terminu zgodna z regułami A§6 i decyzją D7."""
    _require_aware(now)
    warnings: list[str] = []
    # Po inicjalizacji kalendarz slotów wyznacza stan; zmiana daty bazowej w ustawieniach
    # nie przesuwa istniejącego kalendarza (A§6: „błąd lub opóźnienie nie przesuwa daty bazowej”).
    anchor, interval = state.anchor_at_utc, state.interval_hours
    if (anchor, interval) != (schedule.anchor_at_utc, schedule.interval_hours):
        warnings.append(
            "settings.yaml ma inną datę bazową lub interwał niż zapisany stan; "
            "obowiązuje stan z gałęzi danych"
        )
    slot = current_slot(now, anchor, interval)
    common = {"current_slot": slot, "warnings": tuple(warnings)}

    if mode is GateMode.DUE and not schedule.enabled:
        return GateDecision(GateAction.IDLE, "harmonogram wyłączony (schedule.enabled)", **common)

    if active is not None and active.status not in FINISHED_CYCLE_STATUSES:
        overdue = slot is not None and slot > active.slot
        base = {"cycle_id": active.cycle_id, "overdue": overdue, **common}
        if active.status is CycleStatus.BLOCKED:
            if mode is GateMode.UNBLOCK:
                return GateDecision(GateAction.RESUME, "ręczne odblokowanie cyklu", **base)
            return GateDecision(
                GateAction.BLOCKED,
                "cykl zablokowany (CAPTCHA/blokada) — wymaga sprawdzenia i trybu 'unblock'",
                **base,
            )
        if active.due_jobs > 0:
            reason = "wznowienie niedokończonego cyklu"
            if overdue:
                reason += " (opóźniony względem bieżącego slotu — nowy cykl poczeka)"
            return GateDecision(GateAction.RESUME, reason, **base)
        if active.waiting_jobs > 0:
            when = active.next_attempt_at.isoformat() if active.next_attempt_at else "?"
            return GateDecision(
                GateAction.IDLE, f"zadania czekają na ponowienie (najwcześniej {when})", **base
            )
        return GateDecision(GateAction.FINALIZE, "brak zadań do wykonania — finalizacja", **base)

    if mode is GateMode.RESUME:
        return GateDecision(GateAction.IDLE, "brak rozpoczętego cyklu do wznowienia", **common)
    if mode is GateMode.UNBLOCK:
        return GateDecision(GateAction.IDLE, "brak zablokowanego cyklu", **common)
    if slot is None:
        return GateDecision(GateAction.IDLE, "przed datą bazową harmonogramu", **common)
    if state.last_started_slot is not None and slot <= state.last_started_slot:
        return GateDecision(GateAction.IDLE, f"cykl dla slotu {slot} już rozpoczęty", **common)
    skipped = slot - (state.last_started_slot + 1) if state.last_started_slot is not None else 0
    reason = f"nowy cykl dla slotu {slot}"
    if skipped > 0:
        # Pominiętych slotów nie nadrabiamy — ceny z przeszłości są nieaktualne (A§6).
        reason += f" (pominięte sloty: {skipped}, bez nadrabiania)"
    return GateDecision(
        GateAction.START,
        reason,
        new_slot=slot,
        cycle_date=local_date(now, tz),
        **common,
    )
