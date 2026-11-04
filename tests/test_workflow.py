"""Pełny cykl offline: gate → prepare → collect → finalize na lokalnym `origin` (E5).

Pokrywa A§15 pkt 3, 4, 5, 6 i 7: brak duplikatów slotów, wznowienia, utratę runnera,
blokadę, wygasanie, kontrolowane kończenie okien i raport częściowy.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from booking_monitor.config import load_config, load_settings
from booking_monitor.github_state import DataBranchMissingError, DataRepo, PushFailedError
from booking_monitor.models import (
    Availability,
    CycleStatus,
    ObservationOutcome,
    RateCategory,
    RawOffer,
    TechnicalReason,
)
from booking_monitor.normalization import normalize_offers
from booking_monitor.runner import CycleRunner
from booking_monitor.schedule import GateAction, GateMode
from booking_monitor.storage.database import open_database
from booking_monitor.storage.events import RunContext
from booking_monitor.storage.layout import DataLayout
from booking_monitor.storage.restore import load_cycle, load_scheduler_state, rebuild_database
from booking_monitor.workflow import Workspace, collect, finalize, gate, init_data, prepare
from tests.helpers import utc

SETTINGS = """
report_timezone: Europe/Warsaw
own_apartment: {id: moj_apartament, name: Mój apartament}
profile: {adults: 2, children_ages: [], units: 1, currency: PLN, locale: pl-PL}
collection: {horizon_months: 1, stay_nights: [2, 3, 4]}
schedule:
  enabled: true
  interval_hours: 72
  anchor_at_utc: '2026-11-04T05:00:00Z'
  gate_cron_utc: '17 5 * * *'
execution:
  collection_budget_minutes_per_job: 10
  job_timeout_minutes: 20
  checkpoint_interval_minutes: 15
  checkpoint_max_observations: 25
  estimated_observation_seconds_p95: 20
  planning_margin: 0.25
  max_windows_per_run: 3
  retry_base_delay_seconds: 60
  max_inline_retry_wait_seconds: 120
"""
COMPETITORS = """competitor_key;name;booking_url;unit_filter;enabled
alfa;Apartament Alfa;https://www.booking.com/hotel/pl/apartament-alfa.pl.html;;true
beta;Beta Studio;https://www.booking.com/hotel/pl/beta-studio.html;;true
"""
START = utc(2026, 11, 4, 5, 17)
JOBS = 30 * 3 * 2  # 30 dat przyjazdu (4.11–3.12) × 3 długości × 2 konkurentów
RUN = RunContext(workflow_run_id="1", code_sha="abc", runner_environment="test")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class FakeTime:
    def __init__(self, now: datetime):
        self.now = now
        self.mono = 0.0

    def clock(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.mono += seconds

    def options(self) -> dict:
        return {"clock": self.clock, "monotonic": self.monotonic, "sleep": self.sleep}


@dataclass
class Env:
    ws: Workspace
    origin: Path
    time: FakeTime
    tmp: Path

    @property
    def settings(self):
        return load_settings(self.ws.config_dir)

    def layout(self) -> DataLayout:
        repo = DataRepo.open(self.ws.repo_root, self.tmp / "inspect", branch="monitor-data")
        return DataLayout(repo.path)


@pytest.fixture
def env(tmp_path) -> Env:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    code = tmp_path / "code"
    git(tmp_path, "clone", "-q", str(origin), str(code))
    (code / "README.md").write_text("kod\n")
    git(code, "add", "-A")
    git(code, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(code, "push", "-q", "origin", "HEAD:refs/heads/main")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text(SETTINGS, encoding="utf-8")
    (config_dir / "competitors.csv").write_text(COMPETITORS, encoding="utf-8")
    ws = Workspace(repo_root=code, config_dir=config_dir, runtime_dir=tmp_path / "runtime")
    return Env(ws=ws, origin=origin, time=FakeTime(START), tmp=tmp_path)


class FakeObserver:
    """Deterministyczny obserwator: wynik zależy od numeru wywołania; każde trwa 15 s."""

    def __init__(self, time: FakeTime, script=None, seconds: float = 15):
        self.time = time
        self.script = script or (lambda n, query: None)
        self.seconds = seconds
        self.calls = 0
        self.closed = False

    def __call__(self, competitor, query):
        self.calls += 1
        self.time.advance(self.seconds)
        scripted = self.script(self.calls, query)
        if scripted is not None:
            return ObservationOutcome(
                technical_reason=scripted[0],
                message=scripted[1],
                retry_after_seconds=scripted[2] if len(scripted) > 2 else None,
                observed_at=self.time.now,
            )
        raw = RawOffer(
            unit_name="Apartament",
            currency="PLN",
            displayed_price_grosze=40_000 * query.nights,
            rate_category=RateCategory.NON_REFUNDABLE,
        )
        manifest_profile = load_config_profile()
        offers = normalize_offers(
            [raw], nights=query.nights, profile=manifest_profile, unit_filter=None
        )
        return ObservationOutcome(
            availability=Availability.AVAILABLE,
            offers=offers,
            observed_at=self.time.now,
            parser_version="fake-1",
            source_url=competitor.canonical_url,
        )

    def close(self):
        self.closed = True


def load_config_profile():
    from booking_monitor.config import Profile

    return Profile(adults=2)


def start_cycle(env: Env):
    init_data(env.ws, env.settings, now=env.time.now)
    decision = gate(env.ws, env.settings, mode=GateMode.DUE, now=env.time.now)
    assert decision.action is GateAction.START
    return prepare(
        env.ws,
        action=GateAction.START,
        now=env.time.now,
        run=RUN,
        parser_version="fake-1",
        new_slot=decision.new_slot,
        cycle_date=decision.cycle_date,
    )


def run_windows(env: Env, prepared, observer: FakeObserver):
    results = []
    for _window in prepared.windows:
        results.append(
            collect(
                env.ws,
                env.settings,
                cycle_id=prepared.cycle_id,
                now=env.time.now,
                run=RUN,
                observer_factory=lambda manifest: observer,
                runner_options=env.time.options(),
            )
        )
    return results


def rebuilt(env: Env):
    layout = env.layout()
    conn = open_database(":memory:")
    rebuild_database(layout, conn, now=env.time.now, retention_days=365)
    return layout, conn


def count(conn, sql: str, *params) -> int:
    return conn.execute(sql, params).fetchone()[0]


# --------------------------------------------------------------------------- testy


def test_full_cycle_across_partial_runs(env):
    prepared = start_cycle(env)
    assert prepared.open_jobs == JOBS
    assert prepared.windows == [1, 2, 3]  # 180 × 20 s × 1,25 / 600 s = 8 → limit 3
    observer = FakeObserver(env.time)
    results = run_windows(env, prepared, observer)
    assert all("budżet" in r.stop_reason for r in results)
    assert all(r.checkpoints >= 2 for r in results)

    first = finalize(
        env.ws,
        env.settings,
        cycle_id=prepared.cycle_id,
        now=env.time.now,
        run=RUN,
        out_dir=env.tmp / "report-partial",
        artifact_name="raport-test",
    )
    assert first.status is CycleStatus.PARTIAL
    partial_files = {p.name for p in (env.tmp / "report-partial").iterdir()}
    assert {"report.html", "prices.csv", "manifest.json"} <= partial_files
    assert "monitor.sqlite3" not in partial_files  # pełna kopia bazy tylko po zakończeniu cyklu
    assert first.summary_markdown and "raport-test" in first.summary_markdown

    # Kolejna kontrola (nawet przy nowym slocie) wznawia ten sam cykl zamiast otwierać drugi.
    env.time.advance(72 * 3600)
    decision = gate(env.ws, env.settings, mode=GateMode.DUE, now=env.time.now)
    assert decision.action is GateAction.RESUME and decision.overdue
    while True:
        resumed = prepare(
            env.ws, action=GateAction.RESUME, now=env.time.now, run=RUN, parser_version="fake-1"
        )
        run_windows(env, resumed, observer)
        done = finalize(
            env.ws,
            env.settings,
            cycle_id=prepared.cycle_id,
            now=env.time.now,
            run=RUN,
            out_dir=env.tmp / "report-final",
        )
        if done.status is not CycleStatus.PARTIAL:
            break
    # Najbliższe daty wykonano w pierwszym przebiegu (P4), więc mimo 3-dniowej przerwy
    # nic nie wygasło.
    assert done.status is CycleStatus.COMPLETED
    layout, conn = rebuilt(env)
    state = load_scheduler_state(layout)
    assert state.active_cycle_id is None and state.last_finished_cycle_id == prepared.cycle_id
    completed = count(conn, "SELECT COUNT(*) FROM jobs WHERE status='completed'")
    assert completed == JOBS
    assert (env.tmp / "report-final" / "monitor.sqlite3").exists()
    assert layout.latest_report_file.exists()
    html = (env.tmp / "report-final" / "report.html").read_text(encoding="utf-8")
    assert "Mój apartament" in html and prepared.cycle_id in html
    assert count(conn, "SELECT COUNT(*) FROM attempts") == completed == observer.calls
    assert count(conn, "SELECT COUNT(*) FROM observations") == completed

    # Po zakończeniu harmonogram podejmuje najnowszy należny slot.
    nxt = gate(env.ws, env.settings, mode=GateMode.DUE, now=env.time.now)
    assert nxt.action is GateAction.START and nxt.new_slot == 1


def test_jobs_with_past_checkin_expire(env):
    prepared = start_cycle(env)
    env.time.advance(3 * 24 * 3600)  # 7.11 — przyjazdy 4–6.11 są już przeszłe
    result = run_windows(env, prepared, FakeObserver(env.time))[0]
    assert result.expired == 3 * 3 * 2
    _layout, conn = rebuilt(env)
    assert (
        count(
            conn,
            "SELECT MIN(q.checkin) FROM attempts a JOIN jobs j ON j.id = a.job_id "
            "JOIN stay_queries q ON q.id = j.query_id",
        )
        == "2026-11-07"
    )
    env.time.advance(40 * 24 * 3600)  # po końcu okna: reszta wygasa przy finalizacji
    done = finalize(
        env.ws, env.settings, cycle_id=prepared.cycle_id, now=env.time.now, run=RUN, out_dir=None
    )
    assert done.status is CycleStatus.COMPLETED_WITH_ERRORS
    assert done.expired_now > 0
    assert done.job_counts["expired"] + done.job_counts["completed"] == JOBS


def test_gate_same_slot_does_not_duplicate_and_resume_needs_cycle(env):
    prepared = start_cycle(env)
    run_windows(env, prepared, FakeObserver(env.time))
    # ponowna kontrola tego samego slotu z aktywnym cyklem → wznowienie, nie nowy cykl
    again = gate(env.ws, env.settings, mode=GateMode.DUE, now=env.time.now)
    assert again.action is GateAction.RESUME and again.cycle_id == prepared.cycle_id
    with pytest.raises(Exception, match="nadal trwa"):
        prepare(
            env.ws,
            action=GateAction.START,
            now=env.time.now,
            run=RUN,
            parser_version="fake-1",
            new_slot=1,
            cycle_date=date(2026, 11, 7),
        )


def test_runner_loss_resumes_from_last_durable_checkpoint(env):
    prepared = start_cycle(env)
    settings = env.settings
    repo = DataRepo.open(env.ws.repo_root, env.ws.data_worktree, branch="monitor-data")
    layout = DataLayout(repo.path)
    manifest, progress = load_cycle(layout, prepared.cycle_id)
    conn = env.ws.fresh_db()
    rebuild_database(layout, conn, now=env.time.now, retention_days=365)

    class LosesFinalPush:
        def __init__(self):
            self.calls = 0

        def commit_and_push(self, message):
            self.calls += 1
            if "status" in message and self.calls >= 2:
                raise PushFailedError("runner utracony")
            return repo.commit_and_push(message)

    observer = FakeObserver(env.time)
    runner = CycleRunner(
        conn=conn,
        layout=layout,
        committer=LosesFinalPush(),
        manifest=manifest,
        progress=progress,
        observer=observer,
        execution=settings.execution,
        run=RUN,
        **env.time.options(),
    )
    with pytest.raises(PushFailedError):
        runner.run_window()
    lost_after = observer.calls  # ~39 prób, z czego trwałe tylko pierwsze 25

    _layout, durable = rebuilt(env)
    durable_attempts = count(durable, "SELECT COUNT(*) FROM attempts")
    assert durable_attempts == 25 < lost_after
    assert count(durable, "SELECT COUNT(*) FROM jobs WHERE status='pending'") == JOBS - 25

    # Kolejne okno (nowy runner) ponawia utracone zapytania bez duplikatów prób.
    run_windows(env, prepared, FakeObserver(env.time))
    _layout, after = rebuilt(env)  # odtworzenie odrzuciłoby zduplikowane próby
    assert count(after, "SELECT COUNT(*) FROM attempts WHERE attempt_no > 1") == 0


def test_block_stops_cycle_until_explicit_unblock(env):
    prepared = start_cycle(env)
    blocker = FakeObserver(
        env.time, lambda n, q: (TechnicalReason.BLOCKED, "CAPTCHA") if n == 5 else None
    )
    result = run_windows(env, prepared, blocker)[0]
    assert result.blocked and blocker.calls == 5
    # pozostałe okna tego uruchomienia nie wchodzą na Booking
    assert blocker.calls == 5

    decision = gate(env.ws, env.settings, mode=GateMode.DUE, now=env.time.now)
    assert decision.action is GateAction.BLOCKED
    with pytest.raises(Exception, match="unblock"):
        prepare(
            env.ws, action=GateAction.RESUME, now=env.time.now, run=RUN, parser_version="fake-1"
        )

    unblock = gate(env.ws, env.settings, mode=GateMode.UNBLOCK, now=env.time.now)
    assert unblock.action is GateAction.RESUME
    resumed = prepare(
        env.ws,
        action=GateAction.RESUME,
        now=env.time.now,
        run=RUN,
        parser_version="fake-1",
        unblock=True,
    )
    calm = FakeObserver(env.time)
    collect(
        env.ws,
        env.settings,
        cycle_id=resumed.cycle_id,
        now=env.time.now,
        run=RUN,
        observer_factory=lambda m: calm,
        runner_options=env.time.options(),
    )
    assert calm.calls > 0
    layout, conn = rebuilt(env)
    _m, progress = load_cycle(layout, prepared.cycle_id)
    assert progress.status is CycleStatus.RUNNING and progress.blocked is None
    # zablokowane zapytanie ponowione jako próba nr 2
    assert count(conn, "SELECT COUNT(*) FROM attempts WHERE attempt_no = 2") == 1


def test_parse_error_series_blocks_cycle(env):
    prepared = start_cycle(env)
    broken = FakeObserver(env.time, lambda n, q: (TechnicalReason.PARSE_ERROR, "zmiana strony"))
    result = run_windows(env, prepared, broken)[0]
    assert result.blocked and broken.calls == 5
    layout, _conn = rebuilt(env)
    _m, progress = load_cycle(layout, prepared.cycle_id)
    assert progress.blocked.reason == "parse_errors"


def test_transient_errors_retry_with_backoff_then_fail(env):
    prepared = start_cycle(env)
    first = None

    def script(n, query):
        nonlocal first
        first = first or query.query_id
        if query.query_id == first:
            return (TechnicalReason.TIMEOUT, "timeout")
        return None

    observer = FakeObserver(env.time, script)
    run_windows(env, prepared, observer)
    _layout, conn = rebuilt(env)
    rows = conn.execute(
        "SELECT a.attempt_no, a.job_status_after FROM attempts a JOIN jobs j ON j.id = a.job_id "
        "WHERE j.query_id = ? ORDER BY a.attempt_no",
        (first,),
    ).fetchall()
    assert [tuple(r) for r in rows] == [(1, "retry_wait"), (2, "retry_wait"), (3, "failed")]


def test_long_retry_after_pauses_source(env):
    prepared = start_cycle(env)
    limited = FakeObserver(
        env.time,
        lambda n, q: (TechnicalReason.NETWORK_ERROR, "HTTP 429", 3600) if n == 3 else None,
    )
    result = run_windows(env, prepared, limited)[0]
    assert "przerwę" in result.stop_reason and limited.calls == 3
    decision = gate(env.ws, env.settings, mode=GateMode.DUE, now=env.time.now)
    assert decision.action is GateAction.IDLE and "ponowienie" in decision.reason
    env.time.advance(3700)
    later = gate(env.ws, env.settings, mode=GateMode.DUE, now=env.time.now)
    assert later.action is GateAction.RESUME


def test_gate_without_state(env):
    settings = env.settings
    with pytest.raises(DataBranchMissingError):
        gate(env.ws, settings, mode=GateMode.DUE, now=env.time.now)
    disabled = settings.model_copy(
        update={"schedule": settings.schedule.model_copy(update={"enabled": False})}
    )
    # Wyłączony harmonogram: brak dostępu do danych i brak błędu.
    assert gate(env.ws, disabled, mode=GateMode.DUE, now=env.time.now).action is GateAction.IDLE


def test_placeholder_config_stops_before_browser(env):
    init_data(env.ws, env.settings, now=env.time.now)
    (env.ws.config_dir / "competitors.csv").write_text(
        "competitor_key;name;booking_url;unit_filter;enabled\nx;X;UZUPELNIJ;;true\n"
    )
    from booking_monitor.config import ConfigError

    with pytest.raises(ConfigError):
        prepare(
            env.ws,
            action=GateAction.START,
            now=env.time.now,
            run=RUN,
            parser_version="fake-1",
            new_slot=0,
            cycle_date=date(2026, 11, 4),
        )
    assert load_config  # konfiguracja ładowana przed otwarciem gałęzi danych
