"""The per-repo contract: ``.claude/autofix.toml``.

Language-agnostic by design — the plugin never "understands" a language, it
runs the commands the contract declares.  Every key is optional with a
documented default, so a bare repo degrades gracefully instead of failing.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONTRACT_RELPATH = ".claude/autofix.toml"
MAX_ROUNDS_CEILING = 3  # enforced in code; a contract cannot raise it

# When a contract sets lock_tests but names no test files, fall back to the
# conventional layouts most languages use.  Without this, lock_tests silently
# protects nothing — the gate would never fire in a repo that did not spell out
# its own globs, which is the common case and exactly when it is needed.
DEFAULT_TEST_GLOBS: tuple[str, ...] = (
    "test/**",
    "tests/**",
    "testing/**",
    "spec/**",
    "specs/**",
    "t/**",
    "__tests__/**",
    "*_test.go",
    "*_test.py",
    "test_*.py",
    "*Test.java",
    "*Tests.java",
    "*Test.kt",
    "*_test.rb",
    "*_spec.rb",
    "*.test.js",
    "*.test.jsx",
    "*.test.ts",
    "*.test.tsx",
    "*.spec.js",
    "*.spec.jsx",
    "*.spec.ts",
    "*.spec.tsx",
    "*_spec.lua",
    "*_test.c",
    "*_test.cc",
    "*_test.cpp",
    "*Tests.cs",
)

# Build output the orchestrator's own test run can produce in the worktree.
# These are never part of a fix, so they are kept out of the diff even when a
# repo forgets to declare them in `paths.ignore`.
ALWAYS_IGNORED: tuple[str, ...] = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.tox/**",
    "**/.venv/**",
    "**/venv/**",
    "**/node_modules/**",
    "**/coverage/**",
    "**/htmlcov/**",
    "**/.coverage",
    "**/*.coverage",
    "**/target/**",
    "**/.gradle/**",
    "**/build/**",
    "**/dist/**",
    "**/.next/**",
    "**/.turbo/**",
)


class ContractError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commands:
    setup: tuple[str, ...] = ()
    build: tuple[str, ...] = ()
    test: tuple[str, ...] = ()
    lint: tuple[str, ...] = ()
    timeout_s: int = 900
    retries: int = 1


@dataclass(frozen=True)
class Limits:
    max_rounds: int = MAX_ROUNDS_CEILING
    max_files_changed: int = 40
    max_diff_bytes: int = 400_000
    lock_tests: bool = True


@dataclass(frozen=True)
class PathSpec:
    protected: tuple[str, ...] = ()
    test_globs: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()


@dataclass(frozen=True)
class Models:
    fixer: str = "default"
    reviewer: str = "default"
    allow_self_review: bool = False


@dataclass(frozen=True)
class Review:
    blocking_severity: str = "major"
    require_tests_pass: bool = True
    max_review_diff_bytes: int = 200_000
    max_review_file_bytes: int = 40_000


@dataclass(frozen=True)
class PRSpec:
    draft: bool = True
    branch_prefix: str = "autofix/issue-"
    assignees: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Contract:
    version: int
    language: str
    base_branch: str
    commands: Commands
    limits: Limits
    paths: PathSpec
    models: Models
    review: Review
    pr: PRSpec
    source_hash: str
    degraded: tuple[str, ...] = ()

    @property
    def has_tests(self) -> bool:
        return bool(self.commands.test)

    def degraded_blocks_convergence(self) -> bool:
        """A run with no test command cannot be 'verified' — only reviewed."""
        return not self.has_tests

    def protected_globs(self) -> tuple[str, ...]:
        """The contract file itself is always protected, even if unlisted."""
        if CONTRACT_RELPATH in self.paths.protected:
            return self.paths.protected
        return (CONTRACT_RELPATH, *self.paths.protected)

    def ignore_globs(self) -> tuple[str, ...]:
        """Build output that must never enter the reviewed diff.

        The orchestrator runs the project's test command *inside* the worktree,
        which drops bytecode caches, coverage data, and dependency trees there.
        Left alone, ``git add -A`` stages that noise into the next round's diff:
        the reviewer sees a diff the fixer never wrote, and the hash the fixer
        was told to echo no longer matches.  The contract's own ``ignore`` list
        is honoured on top of these, never instead of them.
        """
        return (*self.paths.ignore, *ALWAYS_IGNORED)


_SEVERITIES = ("blocker", "major", "minor", "nit")


def _as_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def load_contract(repo_root: Path, *, strict: bool = False) -> Contract:
    """Load the contract, or return defaults if the repo has none."""
    path = repo_root / CONTRACT_RELPATH
    if not path.exists():
        log_degraded("no_contract")
        return _defaults(source_hash="", degraded=("no_contract",))

    raw = path.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ContractError(f"{CONTRACT_RELPATH} is not valid TOML: {exc}") from exc

    return parse_contract(data, source_hash=source_hash, strict=strict)


def parse_contract(data: dict, *, source_hash: str, strict: bool = False) -> Contract:
    if strict:
        _reject_unknown(data)

    degraded: list[str] = []

    cmds_raw = data.get("commands", {})
    commands = Commands(
        setup=_as_tuple(cmds_raw.get("setup")),
        build=_as_tuple(cmds_raw.get("build")),
        test=_as_tuple(cmds_raw.get("test")),
        lint=_as_tuple(cmds_raw.get("lint")),
        timeout_s=int(cmds_raw.get("timeout_s", 900)),
        retries=int(cmds_raw.get("retries", 1)),
    )
    if not commands.test:
        degraded.append("no_test_command")

    limits_raw = data.get("limits", {})
    requested_rounds = int(limits_raw.get("max_rounds", MAX_ROUNDS_CEILING))
    # The 3-round ceiling is policy, enforced here, not in the contract.
    max_rounds = max(1, min(requested_rounds, MAX_ROUNDS_CEILING))
    limits = Limits(
        max_rounds=max_rounds,
        max_files_changed=int(limits_raw.get("max_files_changed", 40)),
        max_diff_bytes=int(limits_raw.get("max_diff_bytes", 400_000)),
        lock_tests=bool(limits_raw.get("lock_tests", True)),
    )

    paths_raw = data.get("paths", {})
    test_globs = _as_tuple(paths_raw.get("test_globs"))
    if not test_globs and limits.lock_tests and commands.test:
        # A test command with locked tests but no globs is a repo we would
        # otherwise leave unprotected.  Apply the conventional layouts.
        test_globs = DEFAULT_TEST_GLOBS
    paths = PathSpec(
        protected=_as_tuple(paths_raw.get("protected")),
        test_globs=test_globs,
        ignore=_as_tuple(paths_raw.get("ignore")),
    )

    models_raw = data.get("models", {})
    models = Models(
        fixer=str(models_raw.get("fixer", "default")),
        reviewer=str(models_raw.get("reviewer", "default")),
        allow_self_review=bool(models_raw.get("allow_self_review", False)),
    )

    review_raw = data.get("review", {})
    blocking = str(review_raw.get("blocking_severity", "major")).lower()
    if blocking not in _SEVERITIES:
        raise ContractError(
            f"review.blocking_severity must be one of {_SEVERITIES}, got {blocking!r}"
        )
    review = Review(
        blocking_severity=blocking,
        require_tests_pass=bool(review_raw.get("require_tests_pass", True)),
        max_review_diff_bytes=int(review_raw.get("max_review_diff_bytes", 200_000)),
        max_review_file_bytes=int(review_raw.get("max_review_file_bytes", 40_000)),
    )
    # No test command means tests cannot gate convergence; say so explicitly.
    if not commands.test:
        object.__setattr__(review, "require_tests_pass", False)

    pr_raw = data.get("pr", {})
    pr = PRSpec(
        draft=bool(pr_raw.get("draft", True)),
        branch_prefix=str(pr_raw.get("branch_prefix", "autofix/issue-")),
        assignees=_as_tuple(pr_raw.get("assignees")),
        labels=dict(pr_raw.get("labels", {})),
    )

    return Contract(
        version=int(data.get("version", 1)),
        language=str(data.get("language", "unknown")),
        base_branch=str(data.get("base_branch", "main")),
        commands=commands,
        limits=limits,
        paths=paths,
        models=models,
        review=review,
        pr=pr,
        source_hash=source_hash,
        degraded=tuple(degraded),
    )


def _defaults(*, source_hash: str, degraded: tuple[str, ...]) -> Contract:
    return Contract(
        version=1,
        language="unknown",
        base_branch="main",
        commands=Commands(),
        limits=Limits(),
        paths=PathSpec(),
        models=Models(),
        review=Review(require_tests_pass=False),
        pr=PRSpec(),
        source_hash=source_hash,
        degraded=degraded,
    )


def _reject_unknown(data: dict) -> None:
    allowed = {
        "version",
        "language",
        "base_branch",
        "commands",
        "limits",
        "paths",
        "models",
        "review",
        "pr",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ContractError(f"unknown keys in {CONTRACT_RELPATH}: {sorted(unknown)}")


def log_degraded(reason: str) -> None:
    from . import log

    log.warn(f"no {CONTRACT_RELPATH} found ({reason}); running with defaults")
