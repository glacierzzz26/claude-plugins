"""Path resolution.

Deliberately does not shell out, so it can be imported from anywhere without
creating a cycle with ``isolation`` or ``gitops``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def plugin_root() -> Path:
    """Absolute path to the plugin directory.

    Resolved from ``__file__``, never from ``CLAUDE_PLUGIN_ROOT``: that variable
    is set for the slash command but is *not* set for the nested ``claude -p``
    children, and we scrub it from child environments anyway.
    """
    override = os.environ.get("AUTOFIX_PLUGIN_ROOT")
    if override:
        return Path(override).resolve()
    # scripts/autofix/paths.py -> scripts/autofix -> scripts -> <plugin root>
    return Path(__file__).resolve().parents[2]


def data_home() -> Path:
    """Shared, repo-independent config + metrics location."""
    override = os.environ.get("AUTOFIX_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "auto-fix"


def profiles_path() -> Path:
    return data_home() / "profiles.toml"


def secrets_path() -> Path:
    return data_home() / "env"


def local_metrics_dir() -> Path:
    return data_home() / "metrics"


def state_dir(repo_root: Path, *, ci: bool) -> Path:
    """Where run state, locks, and CI metrics live for a run."""
    if ci:
        base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
        return base / "autofix"
    return repo_root / ".git" / "auto-fix"


def ci_metrics_file(repo_root: Path) -> Path:
    return state_dir(repo_root, ci=True) / "metrics.jsonl"


def prompt_path(name: str) -> Path:
    return plugin_root() / "prompts" / name


def price_table_path() -> Path:
    return plugin_root() / "price_table.toml"


def template_path(name: str) -> Path:
    return plugin_root() / "templates" / name
