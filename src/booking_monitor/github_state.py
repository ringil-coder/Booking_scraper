"""Gałąź danych `monitor-data` jako `git worktree` (A§8, PLAN_DZIALANIA.md P1, P5).

Wyłącznie zwykły `git` — bez API GitHub — więc działa tak samo lokalnie i w Actions.

- Pobieramy płytko (`--depth=1`) tylko gałąź danych: do odtworzenia potrzebne jest
  bieżące drzewo, nie historia.
- Worktree pracuje na odłączonym HEAD (detached). Dzięki temu gałąź lokalna nie
  blokuje innego worktree, a push zawsze ma jawny refspec `HEAD:refs/heads/<gałąź>`.
- Push nigdy nie dotyka innych gałęzi (w szczególności `main`) i nie używa `--force`.
- Odrzucony push (non-fast-forward): pobranie nowej głowy i `rebase --onto` tylko
  naszych commitów, potem ponowienie. Konflikt przerywa rebase i zgłasza błąd.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

DEFAULT_USER_NAME = "github-actions[bot]"
DEFAULT_USER_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
_REJECTED_MARKERS = ("non-fast-forward", "fetch first", "[rejected]", "failed to update ref")


class GitStateError(Exception):
    """Operacja na gałęzi danych nie powiodła się."""


class DataBranchMissingError(GitStateError):
    """Zdalna gałąź danych nie istnieje (wymaga jawnej inicjalizacji)."""


class PushFailedError(GitStateError):
    """Nie udało się wypchnąć checkpointu; lokalny commit pozostaje do ponowienia."""


class _GitFailed(GitStateError):
    def __init__(self, args: Sequence[str], result: subprocess.CompletedProcess[str]):
        self.returncode = result.returncode
        self.stderr = result.stderr or ""
        self.stdout = result.stdout or ""
        super().__init__(
            f"git {' '.join(args)} zakończył się kodem {result.returncode}: {self.stderr.strip()}"
        )


class DataRepo:
    """Worktree gałęzi danych z atomowym commitem i pushem checkpointów."""

    def __init__(
        self,
        repo_root: Path,
        worktree_dir: Path,
        *,
        branch: str = "monitor-data",
        remote: str = "origin",
        depth: int | None = 1,
        user_name: str = DEFAULT_USER_NAME,
        user_email: str = DEFAULT_USER_EMAIL,
    ):
        self.repo_root = Path(repo_root).resolve()
        self.worktree_dir = Path(worktree_dir).resolve()
        self.branch = branch
        self.remote = remote
        self.depth = depth
        self.user_name = user_name
        self.user_email = user_email
        # Zdalna głowa, na której opierają się lokalne, niewypchnięte commity.
        self._base: str | None = None

    # ------------------------------------------------------------------ git

    @property
    def _remote_ref(self) -> str:
        return f"refs/remotes/{self.remote}/{self.branch}"

    @property
    def _push_refspec(self) -> str:
        return f"HEAD:refs/heads/{self.branch}"

    def _git(
        self, *args: str, cwd: Path | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "git",
            "-c",
            f"user.name={self.user_name}",
            "-c",
            f"user.email={self.user_email}",
            "-c",
            "commit.gpgsign=false",
            *args,
        ]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        result = subprocess.run(
            command,
            cwd=cwd or self.repo_root,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if check and result.returncode != 0:
            raise _GitFailed(args, result)
        return result

    def _remote_branch_exists(self) -> bool:
        result = self._git(
            "ls-remote",
            "--exit-code",
            "--heads",
            self.remote,
            f"refs/heads/{self.branch}",
            check=False,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 2:
            return False
        raise _GitFailed(("ls-remote", self.remote), result)

    def _fetch(self) -> str:
        args = ["fetch", "--no-tags"]
        if self.depth is not None:
            args.append(f"--depth={self.depth}")
        args += [self.remote, f"+refs/heads/{self.branch}:{self._remote_ref}"]
        self._git(*args)
        return self._git("rev-parse", self._remote_ref).stdout.strip()

    def _is_worktree(self) -> bool:
        if not (self.worktree_dir / ".git").exists():
            return False
        result = self._git("rev-parse", "--show-toplevel", cwd=self.worktree_dir, check=False)
        return result.returncode == 0 and Path(result.stdout.strip()).resolve() == self.worktree_dir

    def _prepare_worktree(self, commitish: str) -> None:
        self._git("worktree", "prune")
        if self._is_worktree():
            self._git("checkout", "--force", "--detach", commitish, cwd=self.worktree_dir)
            self._git("reset", "--hard", commitish, cwd=self.worktree_dir)
            self._git("clean", "-fdx", cwd=self.worktree_dir)
            return
        if self.worktree_dir.exists() and any(self.worktree_dir.iterdir()):
            raise GitStateError(f"katalog {self.worktree_dir} istnieje i nie jest worktree danych")
        self.worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        self._git("worktree", "add", "--force", "--detach", str(self.worktree_dir), commitish)

    # ------------------------------------------------------------------ API

    @classmethod
    def open(
        cls,
        repo_root: Path,
        worktree_dir: Path,
        *,
        branch: str = "monitor-data",
        remote: str = "origin",
        depth: int | None = 1,
        **identity: str,
    ) -> DataRepo:
        """Pobiera aktualną głowę gałęzi danych i ustawia na niej worktree.

        Istniejący worktree jest resetowany — niewypchnięte zmiany z poprzedniego
        procesu przepadają (nie są trwałym checkpointem).
        """
        repo = cls(repo_root, worktree_dir, branch=branch, remote=remote, depth=depth, **identity)
        if not repo._remote_branch_exists():
            raise DataBranchMissingError(
                f"zdalna gałąź {remote}/{branch} nie istnieje — wymagana jawna inicjalizacja"
            )
        head = repo._fetch()
        repo._prepare_worktree(head)
        repo._base = head
        return repo

    @classmethod
    def initialize(
        cls,
        repo_root: Path,
        worktree_dir: Path,
        *,
        branch: str = "monitor-data",
        remote: str = "origin",
        populate: Callable[[Path], None],
        message: str,
        **identity: str,
    ) -> DataRepo:
        """Tworzy osieroconą gałąź danych z początkowym stanem i wypycha ją."""
        repo = cls(repo_root, worktree_dir, branch=branch, remote=remote, **identity)
        if repo._remote_branch_exists():
            raise GitStateError(f"gałąź {remote}/{branch} już istnieje — inicjalizacja odrzucona")
        repo._prepare_worktree("HEAD")
        temp_branch = f"{branch}-init-{os.getpid()}"
        repo._git("checkout", "--orphan", temp_branch, cwd=repo.worktree_dir)
        repo._git("rm", "-r", "-q", "--cached", "--ignore-unmatch", ".", cwd=repo.worktree_dir)
        repo._git("clean", "-fdx", cwd=repo.worktree_dir)
        populate(repo.worktree_dir)
        repo._git("add", "-A", cwd=repo.worktree_dir)
        repo._git("commit", "--no-verify", "-q", "-m", message, cwd=repo.worktree_dir)
        repo._git("push", repo.remote, repo._push_refspec, cwd=repo.worktree_dir)
        head = repo._fetch()
        repo._git("checkout", "--detach", head, cwd=repo.worktree_dir)
        repo._git("branch", "-D", temp_branch, cwd=repo.worktree_dir)
        repo._base = head
        return repo

    @property
    def path(self) -> Path:
        return self.worktree_dir

    def head_sha(self) -> str:
        return self._git("rev-parse", "HEAD", cwd=self.worktree_dir).stdout.strip()

    def _unpushed_commits(self) -> int:
        if self._base is None:
            raise GitStateError("worktree danych nie jest zsynchronizowany ze zdalną gałęzią")
        result = self._git("rev-list", "--count", f"{self._base}..HEAD", cwd=self.worktree_dir)
        return int(result.stdout.strip())

    def _rebase_onto_remote(self) -> None:
        old_base = self._base
        new_head = self._fetch()
        if new_head == old_base:
            return
        result = self._git(
            "rebase", "--onto", new_head, str(old_base), cwd=self.worktree_dir, check=False
        )
        if result.returncode != 0:
            self._git("rebase", "--abort", cwd=self.worktree_dir, check=False)
            raise PushFailedError(
                "konflikt podczas rebase na nową głowę gałęzi danych — "
                f"lokalny commit zachowany: {result.stderr.strip()}"
            )
        self._base = new_head

    def commit_and_push(
        self,
        message: str,
        *,
        max_attempts: int = 5,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> str | None:
        """Zatwierdza wszystkie zmiany worktree jednym commitem i wypycha je.

        Zwraca SHA wypchniętej głowy albo None, gdy nie było nic do zapisania.
        Dopiero potwierdzony push oznacza trwały checkpoint (A§8).
        """
        self._git("add", "-A", cwd=self.worktree_dir)
        staged = self._git("diff", "--cached", "--quiet", cwd=self.worktree_dir, check=False)
        if staged.returncode not in (0, 1):
            raise _GitFailed(("diff", "--cached"), staged)
        if staged.returncode == 1:
            self._git("commit", "--no-verify", "-q", "-m", message, cwd=self.worktree_dir)
        if self._unpushed_commits() == 0:
            return None

        last_error = ""
        for attempt in range(1, max_attempts + 1):
            result = self._git(
                "push",
                "--porcelain",
                self.remote,
                self._push_refspec,
                cwd=self.worktree_dir,
                check=False,
            )
            if result.returncode == 0:
                head = self.head_sha()
                self._git("update-ref", self._remote_ref, head, cwd=self.worktree_dir)
                self._base = head
                return head
            output = f"{result.stdout}\n{result.stderr}"
            last_error = output.strip()
            if attempt == max_attempts:
                break
            if any(marker in output for marker in _REJECTED_MARKERS):
                self._rebase_onto_remote()
                continue
            sleep(backoff_seconds * 2 ** (attempt - 1))
        raise PushFailedError(
            f"push do {self.remote}/{self.branch} nieudany po {max_attempts} próbach: {last_error}"
        )

    def refresh(self) -> str:
        """Ustawia czysty worktree na aktualnej zdalnej głowie; zwraca jej SHA."""
        status = self._git("status", "--porcelain", cwd=self.worktree_dir).stdout.strip()
        if status:
            raise GitStateError("worktree danych ma niezatwierdzone zmiany — odświeżenie odrzucone")
        if self._base is not None and self._unpushed_commits() > 0:
            raise GitStateError("worktree danych ma niewypchnięte commity — odświeżenie odrzucone")
        head = self._fetch()
        self._git("checkout", "--detach", head, cwd=self.worktree_dir)
        self._base = head
        return head
