"""Interfejs wiersza poleceń `booking-monitor` (PLAN_DZIALANIA.md P1).

Workflow GitHub Actions tylko wywołuje te komendy; logika nie zależy od Actions.
Wyniki dla kolejnych kroków workflow trafiają do $GITHUB_OUTPUT, a podsumowania
do $GITHUB_STEP_SUMMARY (jeśli zmienne istnieją).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from booking_monitor.config import ConfigError, load_config, load_settings
from booking_monitor.dates import CycleWindow, local_date
from booking_monitor.schedule import GateAction, GateMode

log = logging.getLogger("booking_monitor")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2


# --------------------------------------------------------------------------- pomocnicze


def _now(args: argparse.Namespace) -> datetime:
    if getattr(args, "now", None):
        moment = datetime.fromisoformat(args.now)
        if moment.tzinfo is None:
            raise SystemExit("--now musi zawierać strefę, np. 2026-11-04T05:17:00+00:00")
        return moment.astimezone(UTC)
    return datetime.now(UTC)


def github_output(**values: object) -> None:
    lines = [f"{key}={'' if value is None else value}" for key, value in values.items()]
    for line in lines:
        print(f"[output] {line}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def github_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown.rstrip() + "\n")
    else:
        print(markdown)


def _workspace(args: argparse.Namespace):
    from booking_monitor.workflow import Workspace

    root = Path(args.repo_root).resolve()
    return Workspace(
        repo_root=root,
        config_dir=(root / args.config_dir).resolve(),
        runtime_dir=(root / args.runtime_dir).resolve(),
    )


def _run_context():
    from booking_monitor.storage.events import RunContext
    from booking_monitor.workflow import current_code_sha

    return RunContext.from_environment(code_sha=current_code_sha(Path.cwd()))


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "tak", "yes"}


# --------------------------------------------------------------------------- komendy


def cmd_validate_config(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config_dir))
    print(f"OK: {len(config.competitors)} konkurentów, {config.config_hash()}")
    return EXIT_OK


def cmd_estimate(args: argparse.Namespace) -> int:
    """Skala cyklu i zużycie minut (PLAN_DZIALANIA.md §3) dla bieżącej konfiguracji."""
    settings = load_settings(Path(args.config_dir))
    competitors = args.competitors
    if competitors is None:
        competitors = len(load_config(Path(args.config_dir)).competitors)
    window = CycleWindow.for_cycle(
        local_date(_now(args), settings.tz), settings.collection.horizon_months
    )
    queries = competitors * len(window) * len(settings.collection.stay_nights)
    seconds = args.seconds or settings.execution.estimated_observation_seconds_p95
    minutes = queries * seconds / 60
    cycles_per_30_days = 30 * 24 / settings.schedule.interval_hours
    print(
        f"Konkurenci: {competitors}, daty przyjazdu: {len(window)}, "
        f"długości: {settings.collection.stay_nights}"
    )
    print(f"Pomiary w cyklu: {queries}")
    print(
        f"Czas pomiaru (z przerwą): {seconds:.1f} s → {minutes:.0f} min/cykl ({minutes / 60:.1f} h)"
    )
    print(
        f"Cykle na 30 dni: {cycles_per_30_days:.0f} → "
        f"ok. {minutes * cycles_per_30_days:.0f} min/30 dni"
    )
    if minutes / 60 > settings.schedule.interval_hours * 2 / 3:
        print("UWAGA: cykl zajmuje ponad 2/3 interwału — brak zapasu na opóźnienia (cel ≤ 48 h).")
    return EXIT_OK


def cmd_init_data(args: argparse.Namespace) -> int:
    from booking_monitor.workflow import init_data

    settings = load_settings(Path(args.config_dir))
    sha = init_data(_workspace(args), settings, now=_now(args))
    print(f"Zainicjalizowano gałąź {settings.storage.state_branch} ({sha})")
    return EXIT_OK


def cmd_gate(args: argparse.Namespace) -> int:
    from booking_monitor.workflow import gate

    mode = GateMode(args.mode)
    if mode is GateMode.UNBLOCK and not _bool(args.confirm_unblock):
        print("Tryb 'unblock' wymaga potwierdzenia (confirm_unblock=true).", file=sys.stderr)
        return EXIT_CONFIG
    settings = load_settings(Path(args.config_dir))
    decision = gate(_workspace(args), settings, mode=mode, now=_now(args))
    for warning in decision.warnings:
        print(f"::warning::{warning}")
    github_output(
        action=decision.action.value,
        cycle_id=decision.cycle_id or "",
        new_slot="" if decision.new_slot is None else decision.new_slot,
        cycle_date=decision.cycle_date.isoformat() if decision.cycle_date else "",
        unblock="true" if mode is GateMode.UNBLOCK else "false",
    )
    lines = [
        "## Kontrola terminu",
        "",
        f"- Decyzja: **{decision.action.value}** — {decision.reason}",
        f"- Bieżący slot: {decision.current_slot if decision.current_slot is not None else '—'}",
    ]
    if decision.cycle_id:
        lines.append(
            f"- Aktywny cykl: `{decision.cycle_id}`" + (" (opóźniony)" if decision.overdue else "")
        )
    github_summary("\n".join(lines))
    if decision.action is GateAction.BLOCKED:
        print(f"::error::{decision.reason}")
        return EXIT_FAILED  # nieudane uruchomienie = powiadomienie e-mail od GitHub (D7)
    return EXIT_OK


def cmd_prepare(args: argparse.Namespace) -> int:
    from booking_monitor.parsing import PARSER_VERSION
    from booking_monitor.workflow import prepare

    result = prepare(
        _workspace(args),
        action=GateAction(args.action),
        now=_now(args),
        run=_run_context(),
        parser_version=PARSER_VERSION,
        new_slot=int(args.new_slot) if args.new_slot not in (None, "") else None,
        cycle_date=date.fromisoformat(args.cycle_date) if args.cycle_date else None,
        unblock=_bool(args.unblock),
    )
    github_output(
        cycle_id=result.cycle_id,
        code_sha=result.code_sha or "",
        windows=json.dumps(result.windows),
    )
    github_summary(
        f"## Przygotowanie cyklu `{result.cycle_id}`\n\n"
        f"- Otwarte zadania: {result.open_jobs}\n- Okna robocze: {len(result.windows)}"
    )
    return EXIT_OK


def cmd_collect(args: argparse.Namespace) -> int:
    from booking_monitor.observation import BrowserObserver
    from booking_monitor.providers.booking_browser import BookingBrowserProvider
    from booking_monitor.runner import WindowTerminated
    from booking_monitor.workflow import collect

    settings = load_settings(Path(args.config_dir))
    diagnostics = Path(args.diagnostics_dir).resolve() if args.diagnostics_dir else None

    def factory(manifest):
        provider = BookingBrowserProvider(settings.execution, diagnostics_dir=diagnostics)
        return BrowserObserver(provider, manifest.profile)

    def on_term(signum, frame):  # anulowanie joba: spróbuj zapisać checkpoint
        raise WindowTerminated(f"sygnał {signum}")

    signal.signal(signal.SIGTERM, on_term)
    result = collect(
        _workspace(args),
        settings,
        cycle_id=args.cycle_id,
        now=_now(args),
        run=_run_context(),
        observer_factory=factory,
    )
    durations = sorted(result.durations)
    p95 = durations[int(0.95 * (len(durations) - 1))] if durations else None
    github_summary(
        f"## Okno {args.window or ''} cyklu `{args.cycle_id}`\n\n"
        f"- Próby: {result.attempts} (ukończone {result.completed}, do ponowienia "
        f"{result.retry_wait}, nieudane {result.failed}, wygasłe {result.expired})\n"
        f"- Checkpointy: {result.checkpoints}\n"
        f"- p95 czasu próby: {f'{p95:.1f} s' if p95 is not None else '—'}\n"
        f"- Zakończenie: {result.stop_reason}"
    )
    return EXIT_OK


def cmd_finalize(args: argparse.Namespace) -> int:
    from booking_monitor.workflow import finalize

    settings = load_settings(Path(args.config_dir))
    result = finalize(
        _workspace(args),
        settings,
        cycle_id=args.cycle_id,
        now=_now(args),
        run=_run_context(),
        out_dir=Path(args.out_dir).resolve() if args.out_dir else None,
        artifact_name=args.artifact_name,
    )
    github_output(status=result.status.value)
    if result.summary_markdown:
        github_summary(result.summary_markdown)
    else:
        github_summary(f"## Cykl `{result.cycle_id}`: {result.status.value}")
    return EXIT_OK


def cmd_restore(args: argparse.Namespace) -> int:
    """Lokalna kopia bazy z gałęzi danych (np. do podglądu w Streamlit)."""
    from booking_monitor.storage.database import backup_database, open_database
    from booking_monitor.workflow import _rebuild, open_data

    settings = load_settings(Path(args.config_dir))
    _repo, layout = open_data(_workspace(args), settings)
    conn = open_database(":memory:")
    report = _rebuild(layout, conn, settings, _now(args))
    backup_database(conn, Path(args.db).resolve())
    print(f"Odtworzono {report} → {args.db}")
    return EXIT_OK


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booking-monitor", description=__doc__)
    parser.add_argument("--config-dir", default="config", help="katalog z settings.yaml")
    parser.add_argument("--repo-root", default=".", help="katalog repozytorium z kodem")
    parser.add_argument("--runtime-dir", default=".runtime", help="katalog roboczy (nie w git)")
    parser.add_argument("--now", help="czas UTC do testów, np. 2026-11-04T05:17:00+00:00")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate-config", help="sprawdź konfigurację").set_defaults(
        func=cmd_validate_config
    )

    p = sub.add_parser("estimate", help="liczba pomiarów i minut Actions na cykl")
    p.add_argument("--competitors", type=int, help="domyślnie liczba z competitors.csv")
    p.add_argument(
        "--seconds", type=float, help="czas pomiaru z przerwą (domyślnie p95 z ustawień)"
    )
    p.set_defaults(func=cmd_estimate)

    sub.add_parser("init-data", help="jednorazowa inicjalizacja gałęzi danych").set_defaults(
        func=cmd_init_data
    )

    p = sub.add_parser("gate", help="kontrola terminu cyklu")
    p.add_argument("--mode", choices=[m.value for m in GateMode], default=GateMode.DUE.value)
    p.add_argument("--confirm-unblock", default="false")
    p.set_defaults(func=cmd_gate)

    p = sub.add_parser("prepare", help="manifest nowego cyklu lub wznowienie; liczba okien")
    p.add_argument("--action", required=True, choices=["start", "resume", "finalize"])
    p.add_argument("--new-slot")
    p.add_argument("--cycle-date")
    p.add_argument("--unblock", default="false")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("collect", help="jedno okno robocze zbierania cen")
    p.add_argument("--cycle-id", required=True)
    p.add_argument("--window", help="numer okna (informacyjnie)")
    p.add_argument("--diagnostics-dir", default="diagnostics")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("finalize", help="status cyklu i raport")
    p.add_argument("--cycle-id", required=True)
    p.add_argument("--out-dir", default="report-out")
    p.add_argument("--artifact-name")
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("restore", help="odtwórz bazę SQLite z gałęzi danych do pliku")
    p.add_argument("--db", default="monitor.sqlite3")
    p.set_defaults(func=cmd_restore)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception as exc:
        from booking_monitor.workflow import WorkflowError

        if isinstance(exc, WorkflowError):
            print(f"::error::{exc}", file=sys.stderr)
            return EXIT_FAILED
        raise


if __name__ == "__main__":
    raise SystemExit(main())
