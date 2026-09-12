"""Process execution with isolation-friendly defaults.

Nothing here builds an environment: callers must pass one produced by
``isolation.build_phase_env``.  That is deliberate — it keeps every child
process's credentials visible at the call site.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProcResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    wall_ms: int
    timed_out: bool
    meta: dict = field(default_factory=dict)


def _kill_group(proc: subprocess.Popen, grace_s: float = 10.0) -> None:
    """SIGTERM then SIGKILL the whole process group.

    ``claude`` is a node process that spawns more node processes.  Without
    killing the group, a timed-out phase leaves orphans holding the worktree
    and the next round fails in a confusing way.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_proc(
    argv: list[str],
    *,
    env: dict[str, str],
    cwd: Path | str,
    timeout_s: float,
    stdin_text: str | None = None,
) -> ProcResult:
    """Run ``argv`` to completion and capture its output.

    The prompt is fed on stdin rather than argv so untrusted text never appears
    in ``ps`` output or shell history.
    """
    started = time.monotonic()
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,  # own process group, so _kill_group can reap it
    )
    timed_out = False
    try:
        # communicate() drains both pipes concurrently, so a chatty child
        # cannot deadlock on a full pipe.
        out, err = proc.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            out, err = "", ""
    wall_ms = int((time.monotonic() - started) * 1000)
    return ProcResult(
        argv=list(argv),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=out or "",
        stderr=err or "",
        wall_ms=wall_ms,
        timed_out=timed_out,
    )


def extract_result_json(stdout: str) -> dict | None:
    """Pull the ``claude -p --output-format json`` result object out of stdout.

    ``-p`` can emit progress or warning lines before the result, so a plain
    ``json.loads`` is not enough.  Scan from the end for the first parseable
    object that looks like a result, and return ``None`` if there is none —
    callers must treat that as an error, never as success.
    """
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and (obj.get("type") == "result" or "duration_ms" in obj):
            return obj
    return None
