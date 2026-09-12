"""Git operations.

All git work is done by the orchestrator, never by a model.  Every call runs
with a credential-free environment except the single push, where the token is
supplied as an argv-level header on a short-lived subprocess.
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from . import isolation, log
from .proc import run_proc

GIT_TIMEOUT_S = 300


class GitError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    """Credential-free env for ordinary git work."""
    env = isolation.build_phase_env(None, phase="git")
    # Keep git non-interactive so it never hangs waiting for a prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(
    workdir: Path,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout_s: int = GIT_TIMEOUT_S,
) -> subprocess.CompletedProcess:
    result = run_proc(
        ["git", *args],
        env=env or _git_env(),
        cwd=workdir,
        timeout_s=timeout_s,
    )
    if check and result.exit_code != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({result.exit_code}): "
            f"{log.redact(result.stderr.strip() or result.stdout.strip())}"
        )
    return subprocess.CompletedProcess(args, result.exit_code, result.stdout, result.stderr)


def repo_root(start: Path) -> Path:
    result = _git(start, ["rev-parse", "--show-toplevel"], check=False)
    if result.returncode != 0:
        raise GitError(f"{start} is not inside a git repository")
    return Path(result.stdout.strip())


def current_branch(workdir: Path) -> str:
    return _git(workdir, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()


def head_sha(workdir: Path) -> str:
    return _git(workdir, ["rev-parse", "HEAD"]).stdout.strip()


def is_dirty(workdir: Path) -> bool:
    return bool(_git(workdir, ["status", "--porcelain"]).stdout.strip())


def resolve_base_sha(workdir: Path, base_branch: str) -> str:
    """Fetch the base branch and return its tip, so the diff has a fixed anchor."""
    _git(workdir, ["fetch", "--quiet", "origin", base_branch], check=False)
    for ref in (f"origin/{base_branch}", base_branch, "HEAD"):
        result = _git(workdir, ["rev-parse", "--verify", "--quiet", ref], check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    raise GitError(f"could not resolve base branch {base_branch!r}")


def create_branch(workdir: Path, branch: str, base_sha: str) -> None:
    _git(workdir, ["checkout", "-B", branch, base_sha])


def stage_all(workdir: Path) -> None:
    """Stage everything, including untracked files.

    A plain ``git diff`` omits untracked files, so a fixer that only *creates*
    files would look like it changed nothing.  Always stage first.
    """
    _git(workdir, ["add", "-A"])


def unstage(workdir: Path, path: str) -> None:
    """Drop one path from the index without touching the working tree.

    Used to keep build output the orchestrator's test run created out of the
    reviewed diff.  ``--ignore-unmatch`` so a race that already removed the
    path is not an error.
    """
    _git(workdir, ["rm", "--cached", "--quiet", "--ignore-unmatch", "--", path], check=False)


def tracked_at_base(workdir: Path, path: str) -> bool:
    """Whether ``path`` is tracked at HEAD, i.e. the fixer did not create it.

    Must consult the HEAD tree, not ``git ls-files``: the path is already in
    the index by the time this runs, so the index would report every staged
    file as tracked and the check would never drop anything.
    """
    result = _git(workdir, ["cat-file", "-e", f"HEAD:{path}"], check=False)
    return result.returncode == 0


def staged_diff(workdir: Path) -> str:
    return _git(workdir, ["diff", "--cached", "--binary", "--no-color"]).stdout


def changed_files(workdir: Path) -> list[str]:
    out = _git(workdir, ["diff", "--cached", "--name-only"]).stdout
    return [line for line in out.splitlines() if line.strip()]


def check_ignored(workdir: Path, path: str) -> bool:
    result = _git(workdir, ["check-ignore", "-q", path], check=False)
    return result.returncode == 0


def commit_round(workdir: Path, round_no: int, issue: int) -> str:
    """Commit the reviewed artifact so the review is anchored to a git object."""
    message = f"auto-fix: round {round_no} (#{issue})"
    env = _git_env()
    # Commit identity, so the commit does not depend on global git config.
    env.setdefault("GIT_AUTHOR_NAME", "auto-fix")
    env.setdefault("GIT_AUTHOR_EMAIL", "auto-fix@users.noreply.github.com")
    env.setdefault("GIT_COMMITTER_NAME", "auto-fix")
    env.setdefault("GIT_COMMITTER_EMAIL", "auto-fix@users.noreply.github.com")
    _git(workdir, ["commit", "--no-verify", "-m", message], env=env)
    return head_sha(workdir)


def has_commits_beyond(workdir: Path, base_sha: str) -> bool:
    result = _git(workdir, ["rev-list", "--count", f"{base_sha}..HEAD"], check=False)
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def remote_branch_sha(workdir: Path, branch: str) -> str | None:
    result = _git(workdir, ["rev-parse", "--verify", "--quiet", f"origin/{branch}"], check=False)
    if result.returncode == 0:
        return result.stdout.strip() or None
    return None


def push_branch(workdir: Path, branch: str, *, token: str) -> None:
    """Push with force-with-lease so a human's commits on the branch are never lost."""
    isolation.register_known_secret(token)
    lease = remote_branch_sha(workdir, branch)
    # The token lives only in this argv, only for this subprocess.
    header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = _git_env()
    args = [
        "-c",
        f"http.extraheader={header}",
        "push",
        "--set-upstream",
        "origin",
        branch,
    ]
    if lease:
        args.insert(-2, f"--force-with-lease={branch}:{lease}")
    try:
        result = run_proc(["git", *args], env=env, cwd=workdir, timeout_s=GIT_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - never let a traceback print the argv
        raise GitError("git push failed") from None
    if result.exit_code != 0:
        raise GitError(
            f"git push failed ({result.exit_code}): "
            f"{log.redact(result.stderr.strip() or result.stdout.strip())}"
        )


def worktree_add(repo: Path, dest: Path, branch: str, base_sha: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, ["worktree", "add", "-b", branch, str(dest), base_sha])


def worktree_remove(repo: Path, dest: Path) -> None:
    _git(repo, ["worktree", "remove", "--force", str(dest)], check=False)
    _git(repo, ["worktree", "prune"], check=False)
