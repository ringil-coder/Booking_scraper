"""Statyczne kontrole workflow GitHub Actions (A§6, A§12, A§13, R7, D1)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from booking_monitor.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(workflow: dict) -> dict:
    # PyYAML (YAML 1.1) czyta klucz `on` jako True.
    return workflow.get("on") or workflow.get(True) or {}


def test_gate_cron_matches_settings():
    collect = load(ROOT / ".github" / "workflows" / "collect.yml")
    crons = [entry["cron"] for entry in triggers(collect)["schedule"]]
    assert crons == [load_settings(ROOT / "config").schedule.gate_cron_utc]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_actions_pinned_to_commit_sha(path):
    text = path.read_text(encoding="utf-8")
    uses = re.findall(r"uses:\s*([^\s#]+)", text)
    assert uses, "workflow bez akcji?"
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_default_permissions_read_only_and_no_direct_push(path):
    workflow = load(path)
    assert workflow["permissions"] == {"contents": "read"}
    text = path.read_text(encoding="utf-8")
    # Zapis na gałąź danych idzie wyłącznie przez github_state (refspec HEAD:refs/heads/<dane>).
    assert "git push" not in text


def test_only_state_jobs_can_write():
    jobs = load(ROOT / ".github" / "workflows" / "collect.yml")["jobs"]
    writers = {
        name for name, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"
    }
    assert writers == {"prepare", "collect", "finalize"}
    assert "permissions" not in jobs["gate"]


def test_collect_contract():
    workflow = load(ROOT / ".github" / "workflows" / "collect.yml")
    assert workflow["concurrency"] == {
        "group": "booking-monitor-production",
        "cancel-in-progress": False,
    }
    collect = workflow["jobs"]["collect"]
    settings = load_settings(ROOT / "config")
    assert collect["strategy"]["max-parallel"] == 1
    assert collect["timeout-minutes"] == settings.execution.job_timeout_minutes
    inputs = triggers(workflow)["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"] == ["due", "resume", "unblock"]
    # Gate nie instaluje przeglądarki (A§6: zwykły dzień kontroli bez Booking).
    gate_steps = " ".join(str(step) for step in workflow["jobs"]["gate"]["steps"])
    assert "playwright" not in gate_steps


def test_data_branch_is_never_code_branch():
    assert load_settings(ROOT / "config").storage.state_branch == "monitor-data"


# Repozytorium jest publiczne (D1): każdy może otworzyć PR z forka.
BOOKING_WORKFLOWS = ("collect.yml", "spike.yml")


@pytest.mark.parametrize("name", BOOKING_WORKFLOWS)
def test_booking_workflows_only_manual_or_scheduled(name):
    # PR z forka nie może uruchomić wejścia na Booking ani zapisu gałęzi danych.
    workflow = load(ROOT / ".github" / "workflows" / name)
    assert set(triggers(workflow)) <= {"schedule", "workflow_dispatch"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_privileged_pull_request_triggers(path):
    # pull_request_target / workflow_run uruchamiają kod z uprawnieniami repozytorium.
    assert not {"pull_request_target", "workflow_run"} & set(triggers(load(path)))


def test_ci_for_pull_requests_is_read_only_without_credentials():
    workflow = load(ROOT / ".github" / "workflows" / "ci.yml")
    assert "pull_request" in triggers(workflow)
    for job in workflow["jobs"].values():
        assert "permissions" not in job
        checkout = next(
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout.get("with", {}).get("persist-credentials") is False
