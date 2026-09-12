"""Best-effort project detection for `auto-fix init`.

Never authoritative: it proposes a contract that the user confirms.  A wrong
guess is cheap to correct because the contract is a plain editable file.
"""

from __future__ import annotations

import json
from pathlib import Path

_DETECTORS = (
    ("python", ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")),
    ("node", ("package.json",)),
    ("rust", ("Cargo.toml",)),
    ("go", ("go.mod",)),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts")),
    ("ruby", ("Gemfile",)),
    ("c", ("Makefile", "CMakeLists.txt")),
)


def detect(repo_root: Path) -> dict[str, str]:
    language = _detect_language(repo_root)
    return {
        "language": language,
        "setup": _setup_command(repo_root, language),
        "test": _test_command(repo_root, language),
        "lint": _lint_command(repo_root, language),
        "test_globs": _test_globs(language),
    }


def _detect_language(repo_root: Path) -> str:
    for name, markers in _DETECTORS:
        if any((repo_root / marker).exists() for marker in markers):
            return name
    return "unknown"


def _node_scripts(repo_root: Path) -> dict:
    path = repo_root / "package.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("scripts", {}) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _setup_command(repo_root: Path, language: str) -> str:
    if language == "node":
        return "npm ci" if (repo_root / "package-lock.json").exists() else "npm install"
    if language == "python":
        return "pip install -e .[dev]" if (repo_root / "pyproject.toml").exists() else ""
    if language == "rust":
        return ""  # cargo fetches on demand
    if language == "go":
        return "go mod download"
    if language == "ruby":
        return "bundle install"
    return ""


def _test_command(repo_root: Path, language: str) -> str:
    if language == "node":
        scripts = _node_scripts(repo_root)
        return "npm test" if "test" in scripts else ""
    if language == "python":
        return "pytest -q"
    if language == "rust":
        return "cargo test"
    if language == "go":
        return "go test ./..."
    if language == "java":
        return "mvn -q test" if (repo_root / "pom.xml").exists() else "./gradlew test"
    if language == "ruby":
        return "bundle exec rspec"
    if language == "c":
        return "make test"
    return ""


def _lint_command(repo_root: Path, language: str) -> str:
    if language == "node":
        scripts = _node_scripts(repo_root)
        return "npm run lint" if "lint" in scripts else ""
    if language == "python":
        return "ruff check ."
    if language == "rust":
        return "cargo clippy -- -D warnings"
    if language == "go":
        return "go vet ./..."
    return ""


def _test_globs(language: str) -> str:
    # Rendered into the TOML array form.
    if language == "python":
        return '["tests/**", "**/test_*.py", "**/*_test.py"]'
    if language == "node":
        return '["test/**", "tests/**", "**/*.test.js", "**/*.test.ts", "**/*.spec.ts"]'
    if language == "rust":
        return '["tests/**"]'
    if language == "go":
        return '["**/*_test.go"]'
    if language == "ruby":
        return '["spec/**"]'
    return '["tests/**", "test/**"]'
