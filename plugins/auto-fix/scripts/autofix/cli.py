"""Command-line entry points."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from . import (
    classify,
    contract as contract_mod,
    gitops,
    github,
    isolation,
    log,
    metrics,
    paths,
    phases,
    profiles,
    prompts,
)
from .loop import RunConfig, run as run_loop


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auto-fix", description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="verbose logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="fix an issue and open a draft PR")
    p_run.add_argument("--issue", type=int, required=True)
    p_run.add_argument("--repo", default=None, help="path to the target repo (default: cwd)")
    p_run.add_argument("--slug", default=None, help="owner/name (default: from git remote)")
    p_run.add_argument("--ci", action="store_true")
    p_run.add_argument("--resume", action="store_true")
    p_run.add_argument("--no-pr", action="store_true", help="push and print, do not open a PR")
    p_run.add_argument("--dry-run", action="store_true", help="print the plan, execute nothing")
    p_run.add_argument("--in-place", action="store_true", help="work in --repo instead of a worktree")
    p_run.add_argument("--max-rounds", type=int, default=None)

    p_val = sub.add_parser("validate", help="check contract, profiles, and the CLI")
    p_val.add_argument("--repo", default=None)

    p_sum = sub.add_parser("summary", help="aggregate recorded metrics")
    p_sum.add_argument("--repo", default=None)
    p_sum.add_argument("--group-by", default="phase", help="comma-separated")
    p_sum.add_argument("--json", action="store_true")

    p_st = sub.add_parser("status", help="show run state for an issue")
    p_st.add_argument("--issue", type=int, default=None)
    p_st.add_argument("--repo", default=None)

    p_init = sub.add_parser("init", help="write a starter contract and workflow")
    p_init.add_argument("--repo", default=None)
    p_init.add_argument("--force", action="store_true")

    p_imp = sub.add_parser("import", help="merge downloaded CI metrics into the local store")
    p_imp.add_argument("path", help="directory or file containing *.jsonl")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    log.set_verbose(args.verbose)

    try:
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "validate":
            return _cmd_validate(args)
        if args.command == "summary":
            return _cmd_summary(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "init":
            return _cmd_init(args)
        if args.command == "import":
            return _cmd_import(args)
    except (contract_mod.ContractError, profiles.ProfileError, isolation.IsolationError) as exc:
        log.error(str(exc))
        return 2
    except github.GitHubError as exc:
        log.error(str(exc))
        return 2
    except KeyboardInterrupt:
        log.warn("interrupted")
        return 130
    return 1


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


def _cmd_run(args) -> int:
    repo_root = gitops.repo_root(Path(args.repo or Path.cwd()).resolve())
    contract = contract_mod.load_contract(repo_root)
    fixer, reviewer = profiles.resolve_all(
        contract, profiles_path=paths.profiles_path(), secrets_path=paths.secrets_path()
    )

    slug = args.slug or _slug_from_remote(repo_root)
    client = None
    if args.dry_run:
        log.warn("dry run: skipping GitHub client creation")
    elif args.no_pr:
        # --no-pr is the offline fallback (no `gh`, no token): there is no
        # GitHub to talk to, so do not build a client that would only 404.
        # The loop handles a None client by skipping issue fetch, push, and PR.
        log.warn("--no-pr: running offline; issue text and PR are unavailable")
    else:
        token = isolation.resolve_auth(
            "GITHUB_TOKEN", env_file_values=isolation.parse_secrets_file(paths.secrets_path())
        ) or os.environ.get("GH_TOKEN")
        if token:
            isolation.register_known_secret(token)
        try:
            client = github.make_client(slug, token=token)
        except github.GitHubError as exc:
            # Degrade rather than abort: the run still produces a diff and
            # metrics, just without GitHub.  This is edge case #14.
            log.warn(f"no GitHub access ({exc}); continuing without issue text or PR")

    ctx = metrics.RunContext(
        run_id=metrics.new_run_id(),
        repo=slug,
        issue=args.issue,
        cli_ver=phases.cli_version(shutil.which("claude") or "claude"),
        self_review=(fixer.model, fixer.base_url) == (reviewer.model, reviewer.base_url),
    )

    workdir, cleanup = _prepare_workdir(repo_root, contract, args, ctx)
    sink = _make_sink(repo_root, ci=args.ci)
    try:
        outcome = run_loop(
            RunConfig(
                issue=args.issue,
                repo_slug=slug,
                workdir=workdir,
                repo_root=repo_root,
                ci=args.ci,
                dry_run=args.dry_run,
                resume=args.resume,
                no_pr=args.no_pr,
                max_rounds_override=args.max_rounds,
            ),
            contract,
            fixer,
            reviewer,
            client,
            sink,
            ctx,
        )
    finally:
        sink.close()
        if cleanup is not None:
            cleanup()

    _report(outcome, ctx)
    _maybe_push_metrics(repo_root, ctx)
    return outcome.exit_code


def _prepare_workdir(repo_root: Path, contract, args, ctx) -> tuple[Path, callable]:
    """Default to an isolated worktree so the user's checkout is never touched."""
    if args.in_place or args.ci or args.dry_run:
        return repo_root, None
    if gitops.is_dirty(repo_root):
        log.warn("working copy has uncommitted changes; using an isolated worktree instead")

    branch = f"{contract.pr.branch_prefix}{args.issue}"
    dest = Path(tempfile.mkdtemp(prefix=f"autofix-{args.issue}-"))
    base = gitops.resolve_base_sha(repo_root, contract.base_branch)
    gitops.worktree_add(repo_root, dest, branch, base)

    def cleanup() -> None:
        gitops.worktree_remove(repo_root, dest)

    return dest, cleanup


def _report(outcome, ctx) -> None:
    reason = outcome.reason
    level = classify.for_reason(reason).level
    log.log(f"result: {reason} (exit {outcome.exit_code})", level="info" if level == "info" else "warn")
    if outcome.details.get("pr_url"):
        log.log(f"draft PR: {outcome.details['pr_url']}")
    if reason == "max_iterations_exceeded":
        log.warn(
            f"not converged after {outcome.state.round} rounds; "
            f"pushed {outcome.details.get('branch')} and commented — no PR opened"
        )


def _make_sink(repo_root: Path, *, ci: bool) -> metrics.MetricsSink:
    if ci:
        return metrics.MetricsSink(paths.ci_metrics_file(repo_root))
    slug_dir = paths.local_metrics_dir()
    slug_dir.mkdir(parents=True, exist_ok=True)
    return metrics.MetricsSink(slug_dir / "metrics.jsonl")


def _maybe_push_metrics(repo_root: Path, ctx) -> None:
    """CI only: publish this run's metrics to the marketplace repo's metrics branch."""
    if os.environ.get("METRICS_PUSH") != "1":
        return
    import subprocess

    source = paths.ci_metrics_file(repo_root)
    if not source.exists():
        return
    repo = os.environ.get("METRICS_REPO")
    token = os.environ.get("METRICS_TOKEN")
    if not repo or not token:
        log.warn("METRICS_PUSH set but METRICS_REPO/METRICS_TOKEN missing; skipping")
        return
    isolation.register_known_secret(token)

    dest = Path(tempfile.mkdtemp(prefix="autofix-metrics-"))
    relpath = f"metrics/{ctx.repo.replace('/', '__')}/{ctx.run_id}.jsonl"
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    env = isolation.build_phase_env(None, phase="metrics")
    env["GIT_TERMINAL_PROMPT"] = "0"

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", *args], cwd=dest, env=env, capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            raise RuntimeError(log.redact(proc.stderr.strip())[:300])
        return proc

    try:
        git("init", "-q", "-b", "metrics")
        # The metrics branch may not exist yet (first run anywhere): that is
        # the common case, not an error.  Only a real branch is checked out.
        has_branch = git("fetch", "-q", url, "metrics", check=False).returncode == 0
        if has_branch:
            git("checkout", "-q", "-B", "metrics", "FETCH_HEAD")

        target = dest / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source.read_text())
        git("add", "-A")
        git(
            "-c", "user.name=auto-fix",
            "-c", "user.email=auto-fix@users.noreply.github.com",
            "commit", "-q", "-m", f"metrics: {ctx.repo} #{ctx.issue} {ctx.run_id}",
        )

        # Each run writes a unique file, so a rejected push is a lost race, not
        # a conflict: rebase onto the new tip and try again.
        for attempt in range(3):
            push = git("push", "-q", url, "metrics", check=False)
            if push.returncode == 0:
                log.log("metrics pushed to the metrics branch")
                return
            if attempt == 2:
                log.warn(f"metrics push failed: {log.redact(push.stderr.strip())[:200]}")
                return
            if git("fetch", "-q", url, "metrics", check=False).returncode == 0:
                git("rebase", "-q", "FETCH_HEAD", check=False)
    except (RuntimeError, OSError) as exc:
        log.warn(f"metrics push step failed: {exc}")


# --------------------------------------------------------------------------
# other subcommands
# --------------------------------------------------------------------------


def _cmd_validate(args) -> int:
    repo_root = gitops.repo_root(Path(args.repo or Path.cwd()).resolve())
    contract = contract_mod.load_contract(repo_root, strict=False)
    log.log(f"contract: language={contract.language} base={contract.base_branch}")
    log.log(f"  tests: {contract.commands.test or '(none — convergence will be blocked)'}")
    log.log(f"  max_rounds: {contract.limits.max_rounds} (ceiling 3)")
    if contract.degraded:
        log.warn(f"degraded: {', '.join(contract.degraded)}")
    fixer, reviewer = profiles.resolve_all(
        contract, profiles_path=paths.profiles_path(), secrets_path=paths.secrets_path()
    )
    log.log(f"  fixer: {fixer.name} -> {fixer.model}")
    log.log(f"  reviewer: {reviewer.name} -> {reviewer.model}")
    claude_bin = shutil.which("claude")
    if not claude_bin:
        log.error("the `claude` CLI is not on PATH")
        return 2
    log.log(f"  claude: {phases.cli_version(claude_bin)}")
    log.log("validation OK")
    return 0


def _cmd_summary(args) -> int:
    files = metrics.discover_metric_files(repo=args.repo)
    if not files:
        print(f"No metrics found under {paths.local_metrics_dir()}")
        return 0
    records = metrics.read_records(files)
    group_by = tuple(g.strip() for g in args.group_by.split(",") if g.strip())
    rows = metrics.summarize(records, group_by=group_by)
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    else:
        print(metrics.render_table(rows, group_by))
    return 0


def _cmd_status(args) -> int:
    repo_root = gitops.repo_root(Path(args.repo or Path.cwd()).resolve())
    found = False
    for ci in (False, True):
        directory = paths.state_dir(repo_root, ci=ci)
        if not directory.exists():
            continue
        for path in sorted(directory.glob("issue-*.json")):
            if args.issue is not None and _issue_of(path) != args.issue:
                continue
            data = json.loads(path.read_text())
            found = True
            pr = data.get("pr_url") or "-"
            commented = " commented" if data.get("commented") else ""
            print(
                f"[{'ci' if ci else 'local'}] #{data.get('issue')}: "
                f"round={data.get('round')} phase={data.get('phase')} "
                f"branch={data.get('branch') or '-'} pr={pr}{commented}"
            )
    if not found:
        where = "this repository"
        print(f"no auto-fix runs recorded for {where}")
        print("start one with:  /auto-fix:fix-issue <issue-number>")
    return 0


def _issue_of(path: Path) -> int | None:
    try:
        return json.loads(path.read_text()).get("issue")
    except (json.JSONDecodeError, OSError):
        return None


def _cmd_init(args) -> int:
    repo_root = gitops.repo_root(Path(args.repo or Path.cwd()).resolve())
    from .detect import detect

    guess = detect(repo_root)
    contract_path = repo_root / contract_mod.CONTRACT_RELPATH
    if contract_path.exists() and not args.force:
        log.error(f"{contract_path} already exists (use --force to overwrite)")
        return 2

    contract_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = paths.template_path("autofix.toml").read_text()
    for key, value in guess.items():
        rendered = rendered.replace("{" + key + "}", value)
    contract_path.write_text(rendered)
    log.log(f"wrote {contract_path} (detected language: {guess.get('language')})")

    workflow = repo_root / ".github" / "workflows" / "auto-fix.yml"
    if not workflow.exists() or args.force:
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(paths.template_path("github-workflow.yml").read_text())
        log.log(f"wrote {workflow}")

    log.log("review the contract, then set the model profiles — see the README")
    return 0


def _cmd_import(args) -> int:
    source = Path(args.path)
    files = [source] if source.is_file() else sorted(source.rglob("*.jsonl"))
    if not files:
        log.error(f"no .jsonl files under {source}")
        return 2
    paths.local_metrics_dir().mkdir(parents=True, exist_ok=True)
    target = paths.local_metrics_dir() / "metrics.jsonl"
    with target.open("a", encoding="utf-8") as out:
        for path in files:
            out.write(path.read_text())
    log.log(f"imported {len(files)} file(s) into {target}")
    return 0


def _slug_from_remote(repo_root: Path) -> str:
    result = gitops._git(repo_root, ["remote", "get-url", "origin"], check=False)  # noqa: SLF001
    url = (result.stdout or "").strip()
    if not url:
        return "local/unknown"
    # git@github.com:owner/name.git  |  https://github.com/owner/name.git
    if url.startswith("git@"):
        url = url.split(":", 1)[-1]
    else:
        url = "/".join(url.split("/")[-2:])
    return url.removesuffix(".git").strip("/")


if __name__ == "__main__":
    sys.exit(main())
