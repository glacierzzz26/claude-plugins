"""The two model phases: fixer and reviewer.

Each is a separate ``claude -p`` process with its own environment.  That is what
makes the isolation in :mod:`autofix.isolation` physically enforceable — a
subagent in a shared session could not be given a different endpoint.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import isolation, log, paths, prompts
from .isolation import Profile, IsolationError
from .proc import ProcResult, extract_result_json, run_proc

# A bare "401"/"429" substring test is wrong: the CLI echoes the reviewed
# artifact in its JSON, and a commit sha or diff hash that happens to contain
# those digits would abort an otherwise healthy run as an auth/rate failure.
# Anchor on word boundaries so only the HTTP status itself matches — but keep
# the shape that upstream errors actually take ("status 429", "HTTP 429",
# "error 429", "429 Too Many Requests"), not just a lone number.
_AUTH_RE = re.compile(r"\b(?:401|403)\b|unauthorized|forbidden|invalid api key|authentication_error")
_RATE_RE = re.compile(
    r"\b429\b|\btoo many requests\b|rate[_ -]?limit|overloaded_error|quota exceeded"
)

# Fixer: needs the repo's own discovery, so no --bare.
FIXER_ALLOWED = "Read,Grep,Glob,Edit,MultiEdit,Write,Bash"
FIXER_DISALLOWED = ",".join(
    [
        "WebFetch",
        "WebSearch",
        "Bash(git push*)",  # git/PR belong to the orchestrator
        "Bash(git commit*)",
        "Bash(git reset*)",
        "Bash(git checkout*)",
        "Bash(gh pr*)",
        "Bash(gh api*)",
    ]
)

# Reviewer: read-only by construction.  No Bash, no Write.
REVIEWER_ALLOWED = "Read,Grep,Glob"
REVIEWER_DISALLOWED = "Edit,MultiEdit,Write,NotebookEdit,Bash"


@dataclass
class PhaseResult:
    phase: str
    profile: Profile
    proc: ProcResult
    result: dict | None
    classification: str  # ok | cli_error | timeout | auth_error | bad_json | rate_limited
    structured: dict | None = None

    @property
    def ok(self) -> bool:
        return self.classification == "ok"

    @property
    def session_id(self) -> str | None:
        return (self.result or {}).get("session_id")


def cli_version(claude_bin: str) -> str:
    env = isolation.build_phase_env(None, phase="probe")
    result = run_proc([claude_bin, "--version"], env=env, cwd=Path.cwd(), timeout_s=30)
    return result.stdout.strip() or "unknown"


def _classify(proc: ProcResult, result: dict | None) -> str:
    if proc.timed_out:
        return "timeout"
    # A clean success is never re-read for diagnostics: the CLI echoes the whole
    # reviewed artifact in its JSON, so scanning that text for "429"/"401" would
    # match a sha, a diff hash, or a source line number and abort a healthy run.
    # Only a failing invocation is diagnosed by what it printed.
    failed = proc.exit_code != 0 or result is None or bool(result.get("is_error"))
    if failed:
        combined = (proc.stdout + "\n" + proc.stderr).lower()
        if _AUTH_RE.search(combined):
            return "auth_error"
        if _RATE_RE.search(combined):
            return "rate_limited"
    if proc.exit_code != 0:
        return "cli_error"
    if result is None:
        return "bad_json"
    if result.get("is_error"):
        return "cli_error"
    return "ok"


def _invoke(
    *,
    phase: str,
    prompt: str,
    profile: Profile,
    env_extra: dict[str, str],
    workdir: Path,
    timeout_s: int,
    args: list[str],
    schema: dict | None = None,
) -> PhaseResult:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise RuntimeError("the `claude` CLI is not on PATH")

    env = isolation.build_phase_env(profile, extra=env_extra, phase=phase)
    # Fail before spawning rather than leak.
    isolation.assert_no_foreign_secrets(
        env,
        allowed={profile.auth_value} if profile.auth_value else set(),
        phase=phase,
    )

    argv = [claude_bin, "-p", "--output-format", "json", *args]
    if schema is not None:
        argv += ["--json-schema", json.dumps(schema)]
    argv += profile.extra_args

    log.debug(f"{phase}: invoking {' '.join(argv[:6])} …")
    proc = run_proc(argv, env=env, cwd=workdir, timeout_s=timeout_s, stdin_text=prompt)
    result = extract_result_json(proc.stdout)
    classification = _classify(proc, result)

    structured = None
    if result is not None and schema is not None:
        structured = result.get("structured_output")
        if classification == "ok" and not isinstance(structured, dict):
            classification = "bad_json"

    return PhaseResult(
        phase=phase,
        profile=profile,
        proc=proc,
        result=result,
        classification=classification,
        structured=structured,
    )


def invoke_fixer(
    prompt: str,
    *,
    profile: Profile,
    workdir: Path,
    timeout_s: int,
    env_extra: dict[str, str] | None = None,
) -> PhaseResult:
    return _invoke(
        phase="fixer",
        prompt=prompt,
        profile=profile,
        env_extra=env_extra or {},
        workdir=workdir,
        timeout_s=timeout_s,
        args=[
            "--permission-mode",
            "bypassPermissions",
            "--allowedTools",
            FIXER_ALLOWED,
            "--disallowedTools",
            FIXER_DISALLOWED,
        ],
    )


def load_reviewer_schema() -> dict:
    return json.loads(prompts.load("reviewer_schema.json"))


def invoke_reviewer(
    prompt: str,
    *,
    profile: Profile,
    workdir: Path,
    timeout_s: int,
    env_extra: dict[str, str] | None = None,
) -> PhaseResult:
    return _invoke(
        phase="reviewer",
        prompt=prompt,
        profile=profile,
        env_extra=env_extra or {},
        workdir=workdir,
        timeout_s=timeout_s,
        args=[
            "--bare",
            "--allowedTools",
            REVIEWER_ALLOWED,
            "--disallowedTools",
            REVIEWER_DISALLOWED,
        ],
        schema=load_reviewer_schema(),
    )


__all__ = [
    "PhaseResult",
    "invoke_fixer",
    "invoke_reviewer",
    "cli_version",
    "IsolationError",
    "paths",
]
