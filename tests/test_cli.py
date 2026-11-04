"""Komendy CLI wywoływane przez workflow: wyjścia $GITHUB_OUTPUT i kody wyjścia."""

from __future__ import annotations

import json

from booking_monitor.cli import main
from booking_monitor.workflow import init_data
from tests.test_workflow import env  # noqa: F401 — fixture

NOW = "2026-11-04T05:17:00+00:00"


def cli(env, *args, monkeypatch, tmp_path):  # noqa: F811
    output = tmp_path / "gh_output"
    summary = tmp_path / "gh_summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    base = [
        "--repo-root",
        str(env.ws.repo_root),
        "--config-dir",
        str(env.ws.config_dir),
        "--runtime-dir",
        str(env.ws.runtime_dir),
        "--now",
        NOW,
    ]
    code = main([*base, *args])
    values = {}
    if output.exists():
        for line in output.read_text().splitlines():
            key, _, value = line.partition("=")
            values[key] = value
    return code, values, summary.read_text() if summary.exists() else ""


def test_gate_and_prepare_outputs(env, monkeypatch, tmp_path):  # noqa: F811
    init_data(env.ws, env.settings, now=env.time.now)
    code, out, summary = cli(
        env, "gate", "--mode", "due", monkeypatch=monkeypatch, tmp_path=tmp_path
    )
    assert code == 0
    assert out["action"] == "start" and out["new_slot"] == "0"
    assert out["cycle_date"] == "2026-11-04"
    assert "Kontrola terminu" in summary

    code, out, _ = cli(
        env,
        "prepare",
        "--action",
        "start",
        "--new-slot",
        "0",
        "--cycle-date",
        "2026-11-04",
        "--unblock",
        "false",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert code == 0
    assert out["cycle_id"] == "c20261104-s0000"
    assert json.loads(out["windows"]) == [1, 2, 3]


def test_unblock_requires_confirmation(env, monkeypatch, tmp_path):  # noqa: F811
    code, _out, _ = cli(
        env, "gate", "--mode", "unblock", monkeypatch=monkeypatch, tmp_path=tmp_path
    )
    assert code == 2


def test_prepare_with_placeholders_fails_cleanly(env, monkeypatch, tmp_path):  # noqa: F811
    (env.ws.config_dir / "competitors.csv").write_text(
        "competitor_key;name;booking_url;unit_filter;enabled\nx;X;UZUPELNIJ;;true\n"
    )
    code, _out, _ = cli(
        env,
        "prepare",
        "--action",
        "start",
        "--new-slot",
        "0",
        "--cycle-date",
        "2026-11-04",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert code == 2


def test_estimate(env, monkeypatch, tmp_path, capsys):  # noqa: F811
    code, _out, _ = cli(
        env,
        "estimate",
        "--competitors",
        "20",
        "--seconds",
        "15",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert "Pomiary w cyklu: 1800" in printed  # horyzont testowy 1 miesiąc: 20 × 30 × 3
