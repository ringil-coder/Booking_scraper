"""Gałąź danych na lokalnym repozytorium bare jako `origin` (bez sieci)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import booking_monitor.github_state as github_state
from booking_monitor.github_state import (
    DataBranchMissingError,
    DataRepo,
    GitStateError,
    PushFailedError,
)

BRANCH = "monitor-data"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path) -> Path:
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(bare), str(seed))
    (seed / "README.md").write_text("kod\n")
    git(seed, "add", "README.md")
    git(seed, "commit", "-m", "init")
    git(seed, "push", "origin", "HEAD:refs/heads/main")
    return bare


def clone(origin: Path, path: Path) -> Path:
    git(path.parent, "clone", "--depth=1", f"file://{origin}", str(path))
    return path


def remote_sha(origin: Path, ref: str) -> str:
    return git(origin, "rev-parse", ref)


def populate(root: Path) -> None:
    (root / "state").mkdir()
    (root / "state" / "scheduler.json").write_text('{"schema_version": 1}\n')


@pytest.fixture
def initialized(origin, tmp_path):
    code = clone(origin, tmp_path / "code-a")
    repo = DataRepo.initialize(
        code, tmp_path / "wt-a", branch=BRANCH, populate=populate, message="Inicjalizacja danych"
    )
    return origin, repo


def test_open_missing_branch(origin, tmp_path):
    code = clone(origin, tmp_path / "code")
    with pytest.raises(DataBranchMissingError):
        DataRepo.open(code, tmp_path / "wt", branch=BRANCH)


def test_initialize_creates_orphan_branch(initialized, tmp_path):
    origin, repo = initialized
    head = remote_sha(origin, f"refs/heads/{BRANCH}")
    assert repo.head_sha() == head
    # Gałąź osierocona: jeden commit bez rodzica, tylko pliki danych.
    assert git(origin, "rev-list", "--count", BRANCH) == "1"
    assert git(origin, "ls-tree", "-r", "--name-only", BRANCH) == "state/scheduler.json"
    assert git(origin, "log", "-1", "--format=%an", BRANCH) == "github-actions[bot]"
    assert not (repo.path / "README.md").exists()


def test_initialize_refuses_existing_branch(initialized, tmp_path):
    origin, _ = initialized
    code = clone(origin, tmp_path / "code-b")
    with pytest.raises(GitStateError, match="już istnieje"):
        DataRepo.initialize(code, tmp_path / "wt-b", branch=BRANCH, populate=populate, message="x")


def test_open_from_second_clone_and_push(initialized, tmp_path):
    origin, _ = initialized
    main_before = remote_sha(origin, "refs/heads/main")
    code = clone(origin, tmp_path / "code-b")
    repo = DataRepo.open(code, tmp_path / "wt-b", branch=BRANCH)
    assert (repo.path / "state" / "scheduler.json").exists()
    (repo.path / "cycles").mkdir()
    (repo.path / "cycles" / "a.json").write_text("{}\n")
    sha = repo.commit_and_push("Checkpoint 1")
    assert sha == remote_sha(origin, f"refs/heads/{BRANCH}")
    assert remote_sha(origin, "refs/heads/main") == main_before
    assert repo.commit_and_push("Nic") is None


def test_concurrent_push_is_rebased(initialized, tmp_path):
    origin, _ = initialized
    a = DataRepo.open(clone(origin, tmp_path / "code-1"), tmp_path / "wt-1", branch=BRANCH)
    b = DataRepo.open(clone(origin, tmp_path / "code-2"), tmp_path / "wt-2", branch=BRANCH)
    (b.path / "b.txt").write_text("b\n")
    b.commit_and_push("Zapis B")
    (a.path / "a.txt").write_text("a\n")
    sleeps: list[float] = []
    sha = a.commit_and_push("Zapis A", sleep=sleeps.append)
    assert sha == remote_sha(origin, f"refs/heads/{BRANCH}")
    assert sleeps == []  # odrzucenie non-fast-forward nie czeka, tylko robi rebase
    files = git(origin, "ls-tree", "-r", "--name-only", BRANCH).splitlines()
    assert {"a.txt", "b.txt", "state/scheduler.json"} <= set(files)
    assert git(origin, "log", "-1", "--format=%s", BRANCH) == "Zapis A"


def test_rebase_conflict_keeps_local_commit(initialized, tmp_path):
    origin, _ = initialized
    a = DataRepo.open(clone(origin, tmp_path / "code-1"), tmp_path / "wt-1", branch=BRANCH)
    b = DataRepo.open(clone(origin, tmp_path / "code-2"), tmp_path / "wt-2", branch=BRANCH)
    (b.path / "state" / "scheduler.json").write_text('{"b": 1}\n')
    b.commit_and_push("Zapis B")
    (a.path / "state" / "scheduler.json").write_text('{"a": 1}\n')
    with pytest.raises(PushFailedError, match="konflikt"):
        a.commit_and_push("Zapis A", sleep=lambda _: None)
    assert git(a.path, "log", "-1", "--format=%s") == "Zapis A"
    assert git(a.path, "status", "--porcelain") == ""


def test_push_failure_retries_with_backoff_and_keeps_commit(initialized, tmp_path):
    origin, _ = initialized
    code = clone(origin, tmp_path / "code-1")
    repo = DataRepo.open(code, tmp_path / "wt-1", branch=BRANCH)
    (repo.path / "x.txt").write_text("x\n")
    git(code, "remote", "set-url", "origin", str(tmp_path / "nie-istnieje.git"))
    sleeps: list[float] = []
    with pytest.raises(PushFailedError):
        repo.commit_and_push("Checkpoint", max_attempts=3, backoff_seconds=1.5, sleep=sleeps.append)
    assert sleeps == [1.5, 3.0]
    # Lokalny commit zostaje — ponowienie zapisu nie wymaga ponownego pobierania cen.
    git(code, "remote", "set-url", "origin", f"file://{origin}")
    sha = repo.commit_and_push("Checkpoint")
    assert sha == remote_sha(origin, f"refs/heads/{BRANCH}")
    assert git(origin, "log", "-1", "--format=%s", BRANCH) == "Checkpoint"


def test_push_refspec_targets_only_data_branch(initialized, tmp_path, monkeypatch):
    origin, _ = initialized
    repo = DataRepo.open(clone(origin, tmp_path / "code-1"), tmp_path / "wt-1", branch=BRANCH)
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(github_state.subprocess, "run", spy)
    (repo.path / "y.txt").write_text("y\n")
    repo.commit_and_push("Checkpoint")
    pushes = [c for c in calls if "push" in c]
    assert pushes, "oczekiwano wywołania git push"
    for command in pushes:
        assert command[-1] == f"HEAD:refs/heads/{BRANCH}"
        assert "--force" not in command and "-f" not in command
        assert not any("main" in part for part in command)


def test_reopen_resets_existing_worktree(initialized, tmp_path):
    origin, _ = initialized
    code = clone(origin, tmp_path / "code-1")
    repo = DataRepo.open(code, tmp_path / "wt-1", branch=BRANCH)
    (repo.path / "smiec.txt").write_text("niezatwierdzone\n")
    again = DataRepo.open(code, tmp_path / "wt-1", branch=BRANCH)
    assert not (again.path / "smiec.txt").exists()
    assert again.head_sha() == remote_sha(origin, f"refs/heads/{BRANCH}")


def test_open_refuses_foreign_directory(initialized, tmp_path):
    origin, _ = initialized
    target = tmp_path / "zajety"
    target.mkdir()
    (target / "plik.txt").write_text("x")
    with pytest.raises(GitStateError, match="nie jest worktree"):
        DataRepo.open(clone(origin, tmp_path / "code-1"), target, branch=BRANCH)


def test_refresh(initialized, tmp_path):
    origin, _ = initialized
    a = DataRepo.open(clone(origin, tmp_path / "code-1"), tmp_path / "wt-1", branch=BRANCH)
    b = DataRepo.open(clone(origin, tmp_path / "code-2"), tmp_path / "wt-2", branch=BRANCH)
    (b.path / "b.txt").write_text("b\n")
    sha = b.commit_and_push("Zapis B")
    assert a.refresh() == sha
    assert (a.path / "b.txt").exists()
    (a.path / "brudny.txt").write_text("x\n")
    with pytest.raises(GitStateError, match="niezatwierdzone"):
        a.refresh()
