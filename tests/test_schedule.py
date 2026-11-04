import itertools
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from booking_monitor.config import ScheduleSettings
from booking_monitor.models import CycleStatus
from booking_monitor.schedule import (
    ActiveCycle,
    GateAction,
    GateMode,
    current_slot,
    decide,
    slot_start,
)
from booking_monitor.storage.state import SchedulerState
from tests.helpers import utc

WAW = ZoneInfo("Europe/Warsaw")
ANCHOR = utc(2026, 11, 4, 5, 0)
SCHEDULE = ScheduleSettings(enabled=True, interval_hours=72, anchor_at_utc=ANCHOR)


def state(last_slot: int | None = None, active: str | None = None) -> SchedulerState:
    return SchedulerState(
        anchor_at_utc=ANCHOR,
        interval_hours=72,
        last_started_slot=last_slot,
        active_cycle_id=active,
        updated_at=ANCHOR,
    )


def run(now: datetime, st: SchedulerState, active: ActiveCycle | None = None, **kw):
    params = {"schedule": SCHEDULE, "mode": GateMode.DUE, "tz": WAW} | kw
    return decide(now=now, state=st, active=active, **params)


def test_slots_keep_72h_spacing_across_month_end():
    starts = [slot_start(ANCHOR, 72, s) for s in range(12)]
    assert [s.date() for s in starts[:4]] == [
        date(2026, 11, 4),
        date(2026, 11, 7),
        date(2026, 11, 10),
        date(2026, 11, 13),
    ]
    assert all(b - a == timedelta(hours=72) for a, b in itertools.pairwise(starts))
    # przełom miesiąca: odstęp nadal 72 h niezależnie od długości miesiąca
    assert starts[8].date() == date(2026, 11, 28) and starts[9].date() == date(2026, 12, 1)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (utc(2026, 11, 4, 4, 59), None),
        (utc(2026, 11, 4, 5, 0), 0),
        (utc(2026, 11, 7, 4, 59), 0),
        (utc(2026, 11, 7, 5, 17), 1),
        (utc(2027, 1, 1, 5, 17), 19),
    ],
)
def test_current_slot(now, expected):
    assert current_slot(now, ANCHOR, 72) == expected


def test_daily_gates_start_exactly_one_cycle_per_slot():
    st = state()
    started = []
    for day in range(0, 15):
        now = utc(2026, 11, 4, 5, 17) + timedelta(days=day)
        decision = run(now, st)
        if decision.action is GateAction.START:
            started.append(decision.cycle_date)
            st = st.model_copy(update={"last_started_slot": decision.new_slot})
        else:
            assert decision.action is GateAction.IDLE
    assert started == [date(2026, 11, d) for d in (4, 7, 10, 13, 16)]


def test_same_slot_checked_twice_does_not_duplicate():
    first = run(utc(2026, 11, 7, 5, 17), state(last_slot=0))
    assert first.action is GateAction.START and first.new_slot == 1
    second = run(utc(2026, 11, 7, 18, 0), state(last_slot=1))
    assert second.action is GateAction.IDLE


def test_missed_trigger_starts_current_slot_without_catch_up():
    decision = run(utc(2026, 11, 14, 9, 0), state(last_slot=0))
    assert decision.action is GateAction.START
    assert decision.new_slot == 3
    assert "pominięte sloty: 2" in decision.reason


def test_delayed_run_keeps_calendar():
    # Uruchomienie opóźnione o 20 h nadal należy do slotu 1; następny slot to 10.11 05:00.
    late = run(utc(2026, 11, 8, 1, 0), state(last_slot=0))
    assert late.new_slot == 1 and late.cycle_date == date(2026, 11, 8)
    assert current_slot(utc(2026, 11, 10, 5, 0), ANCHOR, 72) == 2


def test_before_anchor_and_disabled():
    assert run(utc(2026, 11, 1), state()).action is GateAction.IDLE
    disabled = ScheduleSettings(enabled=False, interval_hours=72, anchor_at_utc=ANCHOR)
    decision = run(utc(2026, 11, 7, 6), state(), schedule=disabled)
    assert decision.action is GateAction.IDLE and "wyłączony" in decision.reason


def active(status=CycleStatus.RUNNING, due=10, waiting=0, slot=0, next_at=None):
    return ActiveCycle("c20261104-s0000", slot, status, due, waiting, next_at)


def test_active_unfinished_cycle_is_resumed_and_blocks_new_cycle():
    decision = run(utc(2026, 11, 7, 5, 17), state(0, "c20261104-s0000"), active())
    assert decision.action is GateAction.RESUME
    assert decision.cycle_id == "c20261104-s0000"
    assert decision.overdue is True
    assert decision.new_slot is None


def test_waiting_only_jobs_mean_idle_and_finished_jobs_mean_finalize():
    st = state(0, "c20261104-s0000")
    waiting = run(
        utc(2026, 11, 5, 5, 17), st, active(due=0, waiting=3, next_at=utc(2026, 11, 5, 9))
    )
    assert waiting.action is GateAction.IDLE and "ponowienie" in waiting.reason
    done = run(utc(2026, 11, 5, 5, 17), st, active(due=0, waiting=0))
    assert done.action is GateAction.FINALIZE


def test_blocked_cycle_requires_unblock():
    st = state(0, "c20261104-s0000")
    blocked = active(status=CycleStatus.BLOCKED)
    assert run(utc(2026, 11, 7, 6), st, blocked).action is GateAction.BLOCKED
    assert run(utc(2026, 11, 7, 6), st, blocked, mode=GateMode.RESUME).action is GateAction.BLOCKED
    unblock = run(utc(2026, 11, 7, 6), st, blocked, mode=GateMode.UNBLOCK)
    assert unblock.action is GateAction.RESUME


def test_manual_resume_without_cycle_is_idle_and_never_starts_new():
    decision = run(utc(2026, 11, 7, 6), state(0), mode=GateMode.RESUME)
    assert decision.action is GateAction.IDLE


def test_manual_resume_works_when_schedule_disabled():
    disabled = ScheduleSettings(enabled=False, interval_hours=72, anchor_at_utc=ANCHOR)
    decision = run(
        utc(2026, 11, 5),
        state(0, "c20261104-s0000"),
        active(),
        schedule=disabled,
        mode=GateMode.RESUME,
    )
    assert decision.action is GateAction.RESUME


def test_finished_active_cycle_is_ignored():
    decision = run(
        utc(2026, 11, 7, 6), state(0, "c20261104-s0000"), active(status=CycleStatus.COMPLETED)
    )
    assert decision.action is GateAction.START and decision.new_slot == 1


def test_changed_anchor_in_settings_warns_and_state_wins():
    moved = ScheduleSettings(enabled=True, interval_hours=72, anchor_at_utc=utc(2026, 11, 5))
    decision = run(utc(2026, 11, 4, 6), state(), schedule=moved)
    assert decision.action is GateAction.START and decision.new_slot == 0
    assert decision.warnings
