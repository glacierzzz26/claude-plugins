"""Per-phase environment construction — the security core.

The rule this module exists to enforce: a child process gets *only* the
credentials it needs, and nothing else.  In particular the fixer — which runs
with Bash against an attacker-controlled issue body — must not be able to read
the reviewer's API key or the GitHub token, even by running ``env``.

That is achieved by building every child environment from an allowlist rather
than copying ``os.environ``.  Nothing in this codebase may call
``os.environ.copy()`` (asserted by a test), and every ``run_proc`` call site
must pass an env produced here.

Secrets are registered with :mod:`autofix.log` as they are resolved so they are
redacted from logs, comments, and metrics automatically.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from . import log

# Names that are always safe to pass through to a child process.
BASE_ALLOW: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TERM",
        "TZ",
        "SHELL",
        "USER",
        "LOGNAME",
        # Windows
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMDATA",
        # Python / toolchain hints that are not secrets
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "GOPATH",
        "GOROOT",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "NODE_PATH",
        "npm_config_cache",
    }
)

# Defence in depth: even if a name slips into BASE_ALLOW, a secret-looking name
# is dropped.  The allowlist is the primary mechanism; this catches mistakes.
_DENY_NAME = re.compile(
    r"(?i)(token|secret|password|passwd|credential|api[_-]?key|auth|cookie|session|private[_-]?key)"
)

# Every secret value the current process knows about, for the pre-spawn check.
_KNOWN_SECRETS: set[str] = set()


class IsolationError(RuntimeError):
    """Raised when a child environment would carry a credential it must not."""


@dataclass(frozen=True)
class Profile:
    """A model endpoint + the name of the env var holding its credential."""

    name: str
    model: str
    base_url: str | None = None
    auth_value: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)

    def __repr__(self) -> str:  # never leak the credential in a traceback
        return f"Profile(name={self.name!r}, model={self.model!r}, base_url={self.base_url!r}, auth=<redacted>)"


def register_known_secret(value: str | None) -> None:
    """Record a secret value so both redaction and the pre-spawn check see it."""
    if value:
        _KNOWN_SECRETS.add(value)
        log.register_secret(value)


def known_secrets() -> frozenset[str]:
    return frozenset(_KNOWN_SECRETS)


def build_phase_env(
    profile: Profile | None,
    *,
    extra: dict[str, str] | None = None,
    phase: str,
) -> dict[str, str]:
    """Build a child environment from scratch.

    ``profile=None`` yields a credential-free environment, used for the
    repo-controlled test commands and for every git operation.

    The returned dict is a fresh object every call, so mutating it cannot leak
    into another phase.
    """
    env: dict[str, str] = {}
    for name in BASE_ALLOW:
        if _DENY_NAME.search(name):
            continue
        value = os.environ.get(name)
        if value is not None:
            env[name] = value

    # Recursion guard: a contract test command that re-enters the harness is a
    # runaway, not a test.
    depth = int(os.environ.get("AUTOFIX_DEPTH", "0") or "0")
    env["AUTOFIX_DEPTH"] = str(depth + 1)
    env["AUTOFIX_PHASE"] = phase

    if profile is not None:
        if profile.base_url:
            env["ANTHROPIC_BASE_URL"] = profile.base_url
        if profile.model:
            env["ANTHROPIC_MODEL"] = profile.model
        if profile.auth_value:
            env["ANTHROPIC_AUTH_TOKEN"] = profile.auth_value
        env.update(profile.extra_env)

    if extra:
        env.update(extra)

    return env


def assert_no_foreign_secrets(
    env: dict[str, str],
    *,
    allowed: frozenset[str] | set[str] = frozenset(),
    phase: str,
) -> None:
    """Fail before spawning if an env carries a secret it was not granted.

    This checks *values*, not names, so a hand-added variable is caught even if
    its name looks innocuous.
    """
    allowed_values = {v for v in allowed if v}
    for name, value in env.items():
        if not value or value in allowed_values:
            continue
        if value in _KNOWN_SECRETS:
            raise IsolationError(
                f"phase {phase!r}: env var {name!r} carries a credential it was not "
                f"granted; refusing to spawn"
            )


def parse_secrets_file(path: Path) -> dict[str, str]:
    """Read ``KEY=VALUE`` lines (``#`` comments, ``shlex`` quoting).

    Warns if the file is readable by anyone but the owner.
    """
    if not path.exists():
        return {}
    try:
        mode = path.stat().st_mode
    except OSError:
        mode = 0
    if mode & 0o077:
        log.warn(f"{path} is readable by other users; run: chmod 600 {path}")

    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        try:
            parsed = shlex.split(value.strip(), comments=True)
        except ValueError:
            parsed = [value.strip()]
        values[key] = parsed[0] if parsed else ""
    return values


def resolve_auth(name: str, *, env_file_values: dict[str, str]) -> str | None:
    """Resolve a credential: process env first (CI), then the local secrets file."""
    value = os.environ.get(name)
    if value:
        return value
    return env_file_values.get(name) or None
