"""Crash-resume state and per-issue locking."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import log

STATE_SCHEMA = 1
STALE_LOCK_S = 60 * 90


class LockError(RuntimeError):
    """Another run holds this issue's lock.  Expected, not a crash."""


@dataclass
class RunState:
    schema: int = STATE_SCHEMA
    run_id: str = ""
    issue: int = 0
    repo: str = ""
    base_sha: str = ""
    branch: str = ""
    contract_hash: str = ""
    issue_title: str = ""
    issue_body: str = ""
    issue_sha256: str = ""
    lang: str = "en"  # language of the issue, so comments answer in kind
    round: int = 0
    phase: str = "init"
    head_sha: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    open_issues: list = field(default_factory=list)
    seen_fingerprints: list = field(default_factory=list)
    history: list = field(default_factory=list)
    started_at: str = ""
    commented: bool = False  # one issue comment per run, even across a resume

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


class StateStore:
    def __init__(self, directory: Path, issue: int):
        self.directory = directory
        self.path = directory / f"issue-{issue}.json"

    def load(self) -> RunState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            log.warn(f"state file {self.path} is unreadable; starting fresh")
            return None
        if data.get("schema") != STATE_SCHEMA:
            log.warn("state file schema mismatch; starting fresh")
            return None
        state = RunState()
        for key, value in data.items():
            if hasattr(state, key):
                setattr(state, key, value)
        return state

    def save(self, state: RunState) -> None:
        """Atomic write: temp file + rename, so a crash never leaves a partial file."""
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(state.to_json(), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class IssueLock:
    """Per-issue lock using ``O_EXCL``, with stale-PID takeover."""

    def __init__(self, directory: Path, issue: int):
        self.path = directory / f"issue-{issue}.lock"
        self._acquired = False

    def __enter__(self) -> "IssueLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._try_acquire():
            return self
        if self._is_stale():
            log.warn(f"taking over stale lock {self.path}")
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            if self._try_acquire():
                return self
        raise LockError(
            f"another auto-fix run holds the lock for this issue ({self.path}); "
            f"if that is wrong, delete the file and retry"
        )

    def _try_acquire(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
        os.close(fd)
        self._acquired = True
        return True

    def _is_stale(self) -> bool:
        try:
            pid_str, ts_str = self.path.read_text().split()
            pid, ts = int(pid_str), float(ts_str)
        except (OSError, ValueError):
            return True
        if time.time() - ts > STALE_LOCK_S:
            return True
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return True
        return False

    def __exit__(self, *exc) -> None:
        if self._acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
