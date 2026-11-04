"""Interfejs wiersza poleceń `booking-monitor` (PLAN_DZIALANIA.md P1).

Workflow GitHub Actions tylko wywołuje te komendy; logika nie zależy od Actions.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from booking_monitor.config import ConfigError, load_config


def _cmd_validate_config(args: argparse.Namespace) -> int:
    try:
        config = load_config(Path(args.config_dir))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"OK: {len(config.competitors)} konkurentów, {config.config_hash()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="booking-monitor")
    parser.add_argument("--config-dir", default="config", help="katalog z settings.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate-config", help="sprawdź konfigurację").set_defaults(
        func=_cmd_validate_config
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
