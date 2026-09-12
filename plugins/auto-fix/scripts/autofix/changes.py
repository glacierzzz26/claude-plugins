"""What gets reviewed, and the structural policy gate.

A fixer with Edit can rewrite the contract, delete tests, or weaken assertions
to fake a pass.  This module is the gate that makes those moves impossible
rather than merely discouraged.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import gitops
from .contract import Contract


@dataclass
class ChangeSet:
    base_sha: str
    diff_text: str
    diff_hash: str
    files: list[str] = field(default_factory=list)
    bytes_total: int = 0
    protected_hits: list[str] = field(default_factory=list)
    weakened_tests: list[str] = field(default_factory=list)
    contract_changed: bool = False
    truncated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.files and not self.diff_text.strip()


def diff_hash(base_sha: str, diff_text: str) -> str:
    h = hashlib.sha256()
    h.update(base_sha.encode())
    h.update(b"\0")
    h.update(diff_text.encode("utf-8", errors="replace"))
    return "sha256:" + h.hexdigest()


def snapshot_changeset(workdir: Path, base_sha: str, contract: Contract) -> ChangeSet:
    """Stage everything, then diff the index against base.

    ``git add -A`` first is essential; see ``gitops.stage_all``.  Build output
    the orchestrator's own test run created is then unstaged, so it cannot
    pollute the diff the reviewer sees.
    """
    gitops.stage_all(workdir)
    for path in _build_output_paths(workdir, contract):
        gitops.unstage(workdir, path)
    text = gitops.staged_diff(workdir)
    files = gitops.changed_files(workdir)
    return ChangeSet(
        base_sha=base_sha,
        diff_text=text,
        diff_hash=diff_hash(base_sha, text),
        files=files,
        bytes_total=len(text.encode("utf-8", errors="replace")),
    )


def _build_output_paths(workdir: Path, contract: Contract) -> list[str]:
    """Ignored paths in the index that did not exist at base.

    A tracked file that merely *lives* under an ignored directory (a repo that
    commits its own build output, say) is left alone: only files new to this
    run are dropped, so nothing a human committed is hidden from review.
    """
    globs = contract.ignore_globs()
    if not globs:
        return []
    staged = gitops.changed_files(workdir)
    return [p for p in staged if _match_any(p, globs) and not gitops.tracked_at_base(workdir, p)]


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


def enforce_policy(workdir: Path, cs: ChangeSet, contract: Contract, *, contract_hash: str) -> str | None:
    """Return a failure classification, or ``None`` if the round is acceptable."""
    # Contract tampering is checked first: .claude/autofix.toml is always
    # force-protected, so the glob check would otherwise mask this with the
    # vaguer "protected_path_violation".  Tampering is the more actionable of
    # the two — the fixer tried to rewrite the rules of its own gate.
    cs.contract_changed = _contract_changed(workdir, contract_hash)
    if cs.contract_changed:
        return "contract_tampered"

    cs.protected_hits = _protected_hits(cs.files, contract)
    if cs.protected_hits:
        return "protected_path_violation"

    if len(cs.files) > contract.limits.max_files_changed:
        return "scope_too_broad"
    if cs.bytes_total > contract.limits.max_diff_bytes:
        return "diff_too_large"

    if contract.limits.lock_tests and contract.paths.test_globs:
        cs.weakened_tests = _weakened_tests(cs.diff_text, contract)
        if cs.weakened_tests:
            return "tests_weakened"

    return None


def _contract_changed(workdir: Path, expected_hash: str) -> bool:
    from .contract import CONTRACT_RELPATH

    path = workdir / CONTRACT_RELPATH
    if not path.exists():
        # Deleted outright — that is a change.
        return bool(expected_hash)
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    return current != expected_hash


def _expand_glob(pattern: str) -> list[str]:
    """gitignore-ish expansions for the ``**/`` prefix and ``/**`` suffix.

    ``fnmatch`` treats ``*`` as matching ``/`` too, so ``a/**`` already covers
    ``a/b/c``.  What it does not do is make a leading ``**/`` optional, which
    is the one bit of gitignore intuition a contract author will assume.
    """
    out = {pattern}
    body = pattern
    if body.startswith("**/"):
        body = body[3:]
        out.add(body)
    if body.endswith("/**"):
        stem = body[:-3].rstrip("/")
        out.add(stem)
        out.add(stem + "/*")
    return list(out)


def _match_any(path: str, globs: tuple[str, ...]) -> str | None:
    from fnmatch import fnmatch

    for pattern in globs:
        normalized = pattern.rstrip("/")
        if path == normalized:
            return pattern
        for candidate in _expand_glob(pattern):
            if fnmatch(path, candidate):
                return pattern
    return None


def _protected_hits(files: list[str], contract: Contract) -> list[str]:
    hits: list[str] = []
    for path in files:
        pattern = _match_any(path, contract.protected_globs())
        if pattern is not None:
            hits.append(f"{path} (matches {pattern})")
    return hits


_ASSERT_RE = re.compile(
    r"^\s*(assert\b|expect\(|assert_|def test_|it\(|test\(|@Test|#\[test\])", re.MULTILINE
)


def _weakened_tests(diff_text: str, contract: Contract) -> list[str]:
    """Detect hunks that remove assertions or test definitions from test files."""
    weakened: list[str] = []
    current_file: str | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[len("+++ b/"):].strip()
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        if current_file is None or not _match_any(current_file, contract.paths.test_globs):
            continue
        if raw.startswith("-") and not raw.startswith("---"):
            if _ASSERT_RE.search(raw[1:]):
                weakened.append(current_file)
    return sorted(set(weakened))


# --------------------------------------------------------------------------
# Reviewer-facing diff assembly
# --------------------------------------------------------------------------


def assemble_review_payload(cs: ChangeSet, contract: Contract) -> tuple[str, bool]:
    """Return (diff text for the reviewer, truncated?).

    Over the reviewer's budget we send a stat summary plus truncated files and
    flag it, so the caller can block convergence on partial context rather than
    let the model approve a diff it never fully saw.
    """
    if cs.bytes_total <= contract.review.max_review_diff_bytes:
        return cs.diff_text, False

    head = (
        "NOTE: the diff exceeded the review budget and has been truncated.\n"
        f"files: {', '.join(cs.files[:50])}\n"
        f"total bytes: {cs.bytes_total}\n\n"
    )
    per_file = max(1000, contract.review.max_review_file_bytes)
    budget = contract.review.max_review_diff_bytes - len(head)
    chunks: list[str] = []
    used = 0
    for chunk in _split_by_file(cs.diff_text):
        if used + len(chunk) > budget:
            chunks.append("\n[... truncated ...]\n")
            break
        chunks.append(chunk)
        used += len(chunk)
    return head + "".join(chunks), True


def _split_by_file(diff_text: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current:
            parts.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        parts.append("".join(current))
    return parts
