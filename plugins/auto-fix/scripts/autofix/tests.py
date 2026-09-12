"""Contract command execution.

The orchestrator runs the tests, never the fixer — a fixer's self-report is not
ground truth.  Test processes get a credential-free environment, since they run
repo-controlled code.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from . import isolation, log
from .contract import Contract
from .proc import ProcResult, run_proc


@dataclass
class CommandRun:
    name: str
    argv: list[str]
    exit_code: int
    wall_ms: int
    stdout_tail: str
    fingerprint: str
    retried: bool = False
    retry_exit_code: int | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


_NUMBERS = re.compile(r"\d+")
_PATHS = re.compile(r"(/[\w./-]+|[A-Za-z]:\\[\w.\\-]+)")
_TIMES = re.compile(r"\b\d+\.\d+s\b|\b\d+ms\b")


def failure_fingerprint(stdout: str) -> str:
    """Normalize a failure so 'same failure, different round' is detectable."""
    text = stdout[-4000:]
    text = _TIMES.sub("<t>", text)
    text = _PATHS.sub("<p>", text)
    text = _NUMBERS.sub("<n>", text)
    text = re.sub(r"\s+", " ", text).strip()
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _run_command(
    name: str,
    command: str,
    *,
    workdir: Path,
    timeout_s: int,
    extra_env: dict[str, str] | None = None,
) -> CommandRun:
    argv = shlex.split(command)
    if not argv:
        return CommandRun(name, [], 0, 0, "", "")
    env = isolation.build_phase_env(None, extra=extra_env, phase="tests")
    result: ProcResult = run_proc(argv, env=env, cwd=workdir, timeout_s=timeout_s)
    combined = result.stdout + "\n" + result.stderr
    return CommandRun(
        name=name,
        argv=argv,
        exit_code=result.exit_code,
        wall_ms=result.wall_ms,
        stdout_tail=combined[-6000:],
        fingerprint=failure_fingerprint(combined) if result.exit_code != 0 else "",
    )


def run_command(
    name: str,
    command: str,
    *,
    workdir: Path,
    timeout_s: int,
    retries: int = 0,
    extra_env: dict[str, str] | None = None,
) -> CommandRun:
    """Run one contract command, retrying once on failure to absorb flakes."""
    run = _run_command(name, command, workdir=workdir, timeout_s=timeout_s, extra_env=extra_env)
    if run.exit_code == 0 or retries <= 0:
        return run
    log.warn(f"{name} failed (exit {run.exit_code}); retrying once to rule out a flake")
    retry = _run_command(name, command, workdir=workdir, timeout_s=timeout_s, extra_env=extra_env)
    retry.retried = True
    retry.retry_exit_code = retry.exit_code
    return retry


def run_setup(contract: Contract, *, workdir: Path) -> CommandRun | None:
    for command in contract.commands.setup:
        run = run_command(
            "setup",
            command,
            workdir=workdir,
            timeout_s=contract.commands.timeout_s,
            retries=0,
        )
        if not run.passed:
            return run
    return None


def run_tests(contract: Contract, *, workdir: Path) -> CommandRun | None:
    """Run every declared test command; return the first failure, else None."""
    if not contract.commands.test:
        return None
    for command in contract.commands.test:
        run = run_command(
            "test",
            command,
            workdir=workdir,
            timeout_s=contract.commands.timeout_s,
            retries=contract.commands.retries,
        )
        if not run.passed:
            return run
    return None


def run_lint(contract: Contract, *, workdir: Path) -> CommandRun | None:
    if not contract.commands.lint:
        return None
    for command in contract.commands.lint:
        run = run_command(
            "lint",
            command,
            workdir=workdir,
            timeout_s=contract.commands.timeout_s,
            retries=0,
        )
        if not run.passed:
            return run
    return None


def test_summary(run: CommandRun | None, *, has_tests: bool) -> str:
    if not has_tests:
        return "No test command is configured for this repo."
    if run is None:
        return "All configured test commands passed."
    return f"Tests FAILED (exit {run.exit_code}):\n{run.stdout_tail[-3000:]}"
