"""The round loop: deterministic, capped at 3, anchored to real artifacts.

The cap lives here in code, not in a prompt, because a model cannot be trusted
to count its own rounds.  Likewise convergence is computed by the orchestrator
from the reviewer's structured issue list — never read from ``verdict`` alone,
since a reviewer that says "approve" while listing majors is a real failure mode.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import changes, classify, gitops, log, metrics, paths, phases, prompts
from . import tests as tests_mod
from .contract import Contract
from .github import GitHubClient, GitHubError
from .isolation import IsolationError, Profile
from .metrics import MetricsSink, RunContext
from .state import IssueLock, LockError, RunState, StateStore

SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}


@dataclass
class RunConfig:
    issue: int
    repo_slug: str
    workdir: Path
    repo_root: Path
    ci: bool = False
    dry_run: bool = False
    resume: bool = False
    no_pr: bool = False
    max_rounds_override: int | None = None


@dataclass
class RunOutcome:
    reason: str
    state: RunState
    details: dict = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return classify.for_reason(self.reason).exit_code


def _fingerprint_issues(issues: list) -> str:
    ids = sorted(str(i.get("title", "")) + "|" + str(i.get("file", "")) for i in issues)
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()[:16]


def _blocking(issues: list, contract: Contract) -> list:
    threshold = SEVERITY_RANK[contract.review.blocking_severity]
    return [i for i in issues if SEVERITY_RANK.get(str(i.get("severity", "major")), 1) <= threshold]


def run(
    cfg: RunConfig,
    contract: Contract,
    fixer: Profile,
    reviewer: Profile,
    client: GitHubClient | None,
    sink: MetricsSink,
    ctx: RunContext,
) -> RunOutcome:
    state_dir = paths.state_dir(cfg.repo_root, ci=cfg.ci)
    store = StateStore(state_dir, cfg.issue)
    state = store.load() if cfg.resume else None
    if state is None:
        state = RunState(
            run_id=ctx.run_id,
            issue=cfg.issue,
            repo=cfg.repo_slug,
            contract_hash=contract.source_hash,
            branch=f"{contract.pr.branch_prefix}{cfg.issue}",
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    if cfg.dry_run:
        return _dry_run(contract, fixer, reviewer, state)

    try:
        with IssueLock(state_dir, cfg.issue):
            outcome = _run_locked(cfg, contract, fixer, reviewer, client, sink, ctx, store, state)
    except LockError as exc:
        # A concurrent run is expected, not a defect: report it as a clean
        # terminal outcome rather than letting the traceback escape.
        outcome = RunOutcome("lock_held", state, {"detail": str(exc)})
    except IsolationError as exc:
        outcome = RunOutcome("internal_error", state, {"detail": str(exc)})
    except gitops.GitError as exc:
        outcome = RunOutcome("push_rejected", state, {"detail": str(exc)})
    except GitHubError as exc:
        outcome = RunOutcome("internal_error", state, {"detail": str(exc)})

    # Mark the run finished and persist it, so `fix-status` shows a terminal
    # phase rather than the last phase that happened to be running.
    outcome.state.phase = "done"
    store.save(outcome.state)

    _emit_terminal(sink, ctx, outcome.state, outcome.reason)
    _comment_terminal(cfg, client, store, outcome)
    return outcome


def _comment_terminal(cfg, client, store: StateStore, outcome: RunOutcome) -> None:
    """Post exactly one actionable issue comment, whatever the outcome.

    Every terminal reason has a written comment in ``prompts/comments/``; this
    is the single place they are posted, so a new failure mode cannot silently
    stop reporting.  The state flag means a resumed run does not double-post.
    """
    state = outcome.state
    if client is None or state.commented:
        return
    if classify.is_success(outcome.reason):
        return  # a converged run reports through its PR instead

    template = classify.for_reason(outcome.reason).comment
    try:
        body = prompts.render(
            f"comments/{template}.md",
            lang=state.lang,
            issue=str(cfg.issue),
            rounds=str(state.round),
            branch=state.branch,
            pr_url=state.pr_url or "",
            detail=str(outcome.details.get("detail", "")),
            unresolved=_render_issues(state.open_issues),
            violations="\n".join(f"- `{h}`" for h in outcome.details.get("protected", []) or []),
            files="\n".join(f"- `{f}`" for f in outcome.details.get("weakened", []) or []),
        )
        client.comment_issue(cfg.issue, body)
        state.commented = True
        store.save(state)
    except (GitHubError, OSError) as exc:
        # Never let a failed comment mask the real outcome.
        log.warn(f"could not comment on issue #{cfg.issue}: {exc}")


def _run_locked(cfg, contract, fixer, reviewer, client, sink, ctx, store, state) -> RunOutcome:
    max_rounds = contract.limits.max_rounds
    if cfg.max_rounds_override is not None:
        max_rounds = max(1, min(cfg.max_rounds_override, 3))

    # -- preflight --------------------------------------------------------
    if not state.base_sha:
        state.base_sha = gitops.resolve_base_sha(cfg.workdir, contract.base_branch)
        gitops.create_branch(cfg.workdir, state.branch, state.base_sha)

    if client is not None and state.pr_number is None:
        existing = client.find_open_pr_for_branch(state.branch)
        if existing:
            state.pr_number, state.pr_url = existing.number, existing.url

    if not state.issue_body and client is not None:
        issue = client.get_issue(cfg.issue)
        state.issue_title = issue.title
        state.issue_body = issue.body
        state.issue_sha256 = hashlib.sha256((issue.title + "\n" + issue.body).encode()).hexdigest()
        # Answer in the language the issue was written in, so a Chinese issue
        # gets Chinese comments.  Detected once, from the run-start snapshot.
        state.lang = prompts.detect_language(issue.title, issue.body)

        # A drive-by account must not be able to bill the owner's model quota
        # just by applying a label.  Checked before any token is spent, and
        # only in CI — locally the person running it already has repo access.
        if cfg.ci and issue.author:
            permission = client.permission_for(issue.author)
            if permission in ("none", ""):
                return RunOutcome(
                    "unauthorized_labeler",
                    state,
                    {"detail": f"{issue.author} has no write access to {cfg.repo_slug}"},
                )

    setup_fail = tests_mod.run_setup(contract, workdir=cfg.workdir)
    if setup_fail is not None:
        return RunOutcome(
            "config_error", state, {"detail": f"setup failed: {setup_fail.stdout_tail[-1000:]}"}
        )

    # -- rounds -----------------------------------------------------------
    start_round = max(1, state.round) if cfg.resume else 1
    for round_no in range(start_round, max_rounds + 1):
        state.round = round_no
        state.phase = "fixer"
        store.save(state)

        fix = phases.invoke_fixer(
            _fixer_prompt(contract, state, round_no),
            profile=fixer,
            workdir=cfg.workdir,
            timeout_s=contract.commands.timeout_s,
        )
        sink.emit(
            metrics.phase_record(
                ctx,
                phase="fixer",
                round_no=round_no,
                profile=fixer,
                proc=fix.proc,
                result=fix.result,
                classification=fix.classification,
                prompt_file="prompts/fixer.md",
                prompt_ver=prompts.prompt_ver("fixer", fixer.model),
            )
        )
        if not fix.ok:
            return RunOutcome(fix.classification, state)

        cs = changes.snapshot_changeset(cfg.workdir, state.base_sha, contract)
        gate = changes.enforce_policy(cfg.workdir, cs, contract, contract_hash=contract.source_hash)
        if gate:
            return RunOutcome(
                gate, state, {"protected": cs.protected_hits, "weakened": cs.weakened_tests}
            )
        if cs.is_empty:
            return _review_empty_diff(cfg, contract, reviewer, sink, ctx, store, state)

        state.phase = "tests"
        store.save(state)
        test_run = tests_mod.run_tests(contract, workdir=cfg.workdir)
        sink.emit(
            metrics.base_record(ctx, phase="tests", round_no=round_no, profile=None)
            | {
                "exit_code": 0 if (test_run is None or test_run.passed) else test_run.exit_code,
                "wall_ms": test_run.wall_ms if test_run else 0,
                "classification": "ok" if (test_run is None or test_run.passed) else "failed",
                "retried": bool(test_run.retried) if test_run else False,
                "extras": {
                    "fingerprint": test_run.fingerprint if test_run else "",
                    "has_tests": contract.has_tests,
                },
            }
        )

        state.phase = "reviewer"
        store.save(state)
        diff_text, truncated = changes.assemble_review_payload(cs, contract)
        rev_prompt = _reviewer_prompt(
            contract,
            state,
            cs,
            tests_mod.test_summary(test_run, has_tests=contract.has_tests),
            truncated,
            diff_text=diff_text,
        )
        rev = phases.invoke_reviewer(
            rev_prompt, profile=reviewer, workdir=cfg.workdir, timeout_s=contract.commands.timeout_s
        )
        sink.emit(
            metrics.phase_record(
                ctx,
                phase="reviewer",
                round_no=round_no,
                profile=reviewer,
                proc=rev.proc,
                result=rev.result,
                classification=rev.classification,
                prompt_file="prompts/reviewer.md",
                prompt_ver=prompts.prompt_ver("reviewer", reviewer.model),
                extras={"truncated": truncated},
            )
        )
        if not rev.ok:
            return RunOutcome(rev.classification, state)

        verdict = rev.structured or {}
        if not _anchored(verdict, state, cs):
            # The reviewer did not see this artifact.  Re-review exactly once.
            rev = phases.invoke_reviewer(
                rev_prompt, profile=reviewer, workdir=cfg.workdir, timeout_s=contract.commands.timeout_s
            )
            verdict = rev.structured or {}
            if not rev.ok or not _anchored(verdict, state, cs):
                return RunOutcome("verdict_mismatch", state)

        # The reviewed artifact becomes an immutable git object.
        state.head_sha = gitops.commit_round(cfg.workdir, round_no, cfg.issue)
        issues = verdict.get("issues") or []
        state.open_issues = issues
        state.history.append(
            {
                "round": round_no,
                "verdict": verdict.get("verdict"),
                "blocking": len(_blocking(issues, contract)),
            }
        )
        store.save(state)

        fp = _fingerprint_issues(issues)
        if fp in state.seen_fingerprints:
            return RunOutcome("stalled", state)
        state.seen_fingerprints.append(fp)
        store.save(state)

        tests_ok = test_run is None or test_run.passed
        anchored = not truncated and not verdict.get("partial_context")
        converged = (
            verdict.get("verdict") == "approve"
            and not _blocking(issues, contract)
            and not verdict.get("requires_human")
            and (tests_ok or not contract.review.require_tests_pass)
            and anchored
        )

        if converged:
            # Review-only verification is not verification: keep the work and
            # open the draft PR, but hand off rather than declaring success.
            reason = "review_only" if contract.degraded_blocks_convergence() else "converged"
            return _publish(cfg, contract, client, state, reason=reason, verdict=verdict)

        if not tests_ok and contract.review.require_tests_pass:
            # Failing tests are not a reason to stop; give the fixer the output.
            state.open_issues = issues + [
                {
                    "severity": "blocker",
                    "category": "tests",
                    "title": "Configured tests are failing",
                    "file": "",
                    "line": 0,
                    "detail": tests_mod.test_summary(test_run, has_tests=True)[-2000:],
                    "suggestion": "Make the configured test command pass.",
                }
            ]
            store.save(state)

    # -- non-convergence: keep the work, open no PR -----------------------
    return _handoff_no_pr(cfg, contract, client, state)


def _anchored(verdict: dict, state: RunState, cs: changes.ChangeSet) -> bool:
    return verdict.get("base_sha") == state.base_sha and verdict.get("diff_hash") == cs.diff_hash


def _review_empty_diff(cfg, contract, reviewer, sink, ctx, store, state) -> RunOutcome:
    state.phase = "reviewer"
    store.save(state)
    cs = changes.snapshot_changeset(cfg.workdir, state.base_sha, contract)
    rev = phases.invoke_reviewer(
        _reviewer_prompt(contract, state, cs, "No changes were made.", truncated=False, diff_text=""),
        profile=reviewer,
        workdir=cfg.workdir,
        timeout_s=contract.commands.timeout_s,
    )
    verdict = (rev.structured or {}).get("verdict")
    return RunOutcome("already_fixed" if verdict == "already_fixed" else "no_changes", state)


def _fixer_prompt(contract: Contract, state: RunState, round_no: int) -> str:
    prior = ""
    if round_no > 1 and state.open_issues:
        prior = "\n\nA previous review raised these issues. Address them:\n" + json.dumps(
            state.open_issues[:20], indent=2, ensure_ascii=False
        )
    template = "fixer.md" if round_no == 1 else "fixer_revise.md"
    return prompts.render(
        template,
        lang=state.lang,
        issue=prompts.fence_untrusted(f"{state.issue_title}\n\n{state.issue_body}", "ISSUE"),
        round=str(round_no),
        test_command="; ".join(contract.commands.test) or "(none configured)",
        prior_issues=prior,
        protected=", ".join(contract.protected_globs()) or "(none)",
        reply_language=prompts.language_name(state.lang),
    )


def _reviewer_prompt(
    contract: Contract,
    state: RunState,
    cs: changes.ChangeSet,
    test_summary_text: str,
    truncated: bool,
    *,
    diff_text: str | None = None,
) -> str:
    diff = cs.diff_text if diff_text is None else diff_text
    return prompts.render(
        "reviewer.md",
        lang=state.lang,
        issue=prompts.fence_untrusted(f"{state.issue_title}\n\n{state.issue_body}", "ISSUE"),
        diff=prompts.fence_untrusted(diff, "DIFF"),
        base_sha=state.base_sha,
        diff_hash=cs.diff_hash,
        tests=test_summary_text,
        truncated="yes" if truncated else "no",
        blocking_severity=contract.review.blocking_severity,
        extra_rules=contract.review.blocking_severity,
        reply_language=prompts.language_name(state.lang),
    )


def _publish(cfg, contract, client, state, *, reason: str, verdict: dict) -> RunOutcome:
    if client is not None and not cfg.no_pr:
        try:
            gitops.push_branch(cfg.workdir, state.branch, token=_token(client))
        except gitops.GitError as exc:
            return RunOutcome("push_rejected", state, {"detail": str(exc)})

        body = prompts.render(
            "pr_body.md",
            lang=state.lang,
            issue=str(cfg.issue),
            repo=cfg.repo_slug,
            summary=verdict.get("summary", ""),
            rounds=str(state.round),
            branch=state.branch,
            reviewed_sha=state.head_sha or "",
        )
        try:
            if state.pr_number:
                client.update_pr(state.pr_number, body=body)
            else:
                ref = client.create_draft_pr(
                    head=state.branch,
                    base=contract.base_branch,
                    title=f"auto-fix: {state.issue_title or f'issue #{cfg.issue}'}",
                    body=body,
                )
                state.pr_number, state.pr_url = ref.number, ref.url
        except GitHubError as exc:
            return RunOutcome("internal_error", state, {"detail": str(exc)})

    state.phase = "done"
    return RunOutcome(reason, state, {"pr_url": state.pr_url})


def _handoff_no_pr(cfg, contract, client, state) -> RunOutcome:
    """Non-convergence: push the branch, but open NO PR.

    The issue comment is posted centrally by ``_comment_terminal`` from the
    returned outcome, so it stays in one place for every failure mode.
    """
    if client is not None:
        try:
            gitops.push_branch(cfg.workdir, state.branch, token=_token(client))
        except gitops.GitError as exc:
            return RunOutcome("push_rejected", state, {"detail": str(exc)})

    state.phase = "done"
    return RunOutcome("max_iterations_exceeded", state, {"branch": state.branch})


def _render_issues(issues: list) -> str:
    if not issues:
        return "_(none reported)_"
    lines = []
    for issue in issues[:20]:
        lines.append(
            f"- **[{issue.get('severity', '?')}] {issue.get('title', '')}** "
            f"(`{issue.get('file', '')}`:{issue.get('line', 0)}) — {issue.get('detail', '')}"
        )
    return "\n".join(lines)


def _emit_terminal(sink: MetricsSink, ctx: RunContext, state: RunState, reason: str) -> None:
    sink.emit(
        metrics.base_record(ctx, phase="run", round_no=state.round, profile=None)
        | {
            "classification": reason,
            "exit_code": classify.for_reason(reason).exit_code,
            "extras": {
                "pr_number": state.pr_number,
                "pr_url": state.pr_url,
                "rounds_used": state.round,
            },
        }
    )


def _token(client) -> str:
    return getattr(client, "_token", "") or ""


def _dry_run(contract: Contract, fixer: Profile, reviewer: Profile, state: RunState) -> RunOutcome:
    from . import isolation

    credential_keys = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "GH_TOKEN", "GITHUB_TOKEN")
    for phase, profile in (("fixer", fixer), ("reviewer", reviewer)):
        env = isolation.build_phase_env(profile, phase=phase)
        log.log(f"[dry-run] {phase}: model={profile.model} base_url={profile.base_url}")
        for key in sorted(env):
            if not key.startswith(("ANTHROPIC_", "GH_", "GITHUB_")):
                continue
            if key in credential_keys:
                # Show that a credential is present, never its value: a dry run
                # exists to be safe to run and safe to paste into a report.
                value = env[key] or ""
                log.log(f"[dry-run]   {key}=<set, {len(value)} chars>" if value else f"[dry-run]   {key}=<unset>")
            else:
                log.log(f"[dry-run]   {key}={env[key]}")
    log.log("[dry-run] each phase receives only its own credentials; none are shared")
    log.log(f"[dry-run] contract: language={contract.language} tests={contract.commands.test}")
    log.log(f"[dry-run] max_rounds={contract.limits.max_rounds} base_branch={contract.base_branch}")
    # A dry run did what it was asked to do, so it is a success: exiting
    # non-zero would fail a CI step or a `set -e` script for no reason.
    return RunOutcome("dry_run", state, {"dry_run": True})
