#!/usr/bin/env python3
"""End-to-end tests: drive the orchestrator through real repos with a fake CLI.

Run with:  python3 tests/e2e.py

These exercise the loop's control flow — convergence, the 3-round cap, the
safety gates, and metrics — without spending any tokens and without touching
GitHub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
AUTOFIX = PLUGIN / "scripts" / "autofix.py"
FAKE_CLAUDE = PLUGIN / "tests" / "fake_claude.py"

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}  {detail}")


def make_repo(tmp: Path, *, test_command: str = "python3 -c 'import calc; assert calc.add(2,2)==4'") -> Path:
    """A tiny python project whose test fails until calc.py is fixed."""
    repo = tmp / "repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    (repo / ".claude" / "autofix.toml").write_text(
        f"""
version = 1
language = "python"
base_branch = "main"

[commands]
test = {json.dumps(test_command)}
timeout_s = 60

[limits]
max_rounds = 3
lock_tests = true

[models]
fixer = "fixer"
reviewer = "reviewer"

[review]
blocking_severity = "major"
"""
    )
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=repo,
        check=True,
    )
    return repo


def make_home(tmp: Path, *, same_model: bool = False, price_entry: bool = True) -> Path:
    """A fake AUTOFIX_HOME with two profiles and a price table."""
    home = tmp / "autofix-home"
    home.mkdir()
    fixer_model = "fake-fixer-1"
    reviewer_model = fixer_model if same_model else "fake-reviewer-1"
    (home / "profiles.toml").write_text(
        f"""
[profiles.fixer]
model = "{fixer_model}"
base_url = "http://fixer.invalid"
auth_env_var = "FAKE_FIXER_TOKEN"

[profiles.reviewer]
model = "{reviewer_model}"
base_url = "http://reviewer.invalid"
auth_env_var = "FAKE_REVIEWER_TOKEN"
"""
    )
    (home / "env").write_text("FAKE_FIXER_TOKEN=fixer-secret-value\nFAKE_REVIEWER_TOKEN=reviewer-secret-value\n")
    os.chmod(home / "env", 0o600)
    shutil.copy(PLUGIN / "price_table.toml", home / "price_table.toml")
    if price_entry:
        with (home / "price_table.toml").open("a") as fh:
            fh.write('\n[models."fake-fixer-1"]\ninput = 1.0\noutput = 2.0\ncache_read = 0.1\ncache_write = 1.0\n')
    return home


def run_autofix(repo: Path, home: Path, script: list[dict], *, extra: list[str] | None = None) -> tuple[int, str]:
    """Invoke the orchestrator with the fake CLI shimmed onto PATH.

    The shim hard-codes the scenario paths rather than reading them from the
    environment, because the plugin's env isolation deliberately strips unknown
    variables before spawning a model process — as it should.
    """
    bindir = repo.parent / "bin"
    bindir.mkdir(exist_ok=True)
    script_path = repo.parent / "script.json"
    script_path.write_text(json.dumps(script))
    counter_path = repo.parent / "counter.txt"
    if counter_path.exists():
        counter_path.unlink()

    shim = bindir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        f'export FAKE_CLAUDE_SCRIPT="{script_path}"\n'
        f'export FAKE_CLAUDE_COUNTER="{counter_path}"\n'
        f'exec python3 "{FAKE_CLAUDE}" "$@"\n'
    )
    shim.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["AUTOFIX_HOME"] = str(home)
    env["FAKE_FIXER_TOKEN"] = "fixer-secret-value"
    env["FAKE_REVIEWER_TOKEN"] = "reviewer-secret-value"

    result = subprocess.run(
        [sys.executable, str(AUTOFIX), "run", "--issue", "1", "--in-place", "--no-pr", *(extra or [])],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


def fixer_step(**kw) -> dict:
    return {"phase": "fixer", **kw}


def reviewer_step(verdict: str, issues: list | None = None, **kw) -> dict:
    structured = {
        "verdict": verdict,
        "base_sha": kw.pop("base_sha", "PLACEHOLDER"),
        "diff_hash": kw.pop("diff_hash", "PLACEHOLDER"),
        "summary": "reviewed",
        "issues": issues or [],
        "blocking_count": sum(1 for i in (issues or []) if i.get("severity") in ("blocker", "major")),
        "scope_assessment": {
            "matches_issue": True,
            "unrelated_changes": [],
            "behavior_changes_undocumented": False,
        },
        "test_assessment": {
            "tests_present": True,
            "tests_weakened": False,
            "missing_coverage": [],
            "notes": "",
        },
        "confidence": 0.9,
        "requires_human": False,
        "partial_context": False,
        **kw,
    }
    return {"phase": "reviewer", "structured": structured}


def anchor_steps(repo: Path, script: list[dict]) -> list[dict]:
    """Fill in real base_sha/diff_hash so anchoring checks pass.

    The orchestrator requires the reviewer to echo the artifact it was given.
    Rather than hard-code hashes, we compute them the same way the plugin does.
    """
    sys.path.insert(0, str(PLUGIN / "scripts"))
    from autofix import changes  # noqa: E402

    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    # Walk the script, simulating the tree after each edit, to derive hashes.
    simulated = repo.parent / "sim"
    if simulated.exists():
        shutil.rmtree(simulated)
    shutil.copytree(repo, simulated)

    patched: list[dict] = []
    for step in script:
        patched.append(step)
        if step.get("phase") == "reviewer" and step.get("structured"):
            diff = _simulated_diff(simulated, base)
            step["structured"]["base_sha"] = base
            step["structured"]["diff_hash"] = changes.diff_hash(base, diff)
        for edit in step.get("edits", []):
            target = simulated / edit["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit["content"])
        # The real loop commits the reviewed artifact at the end of each round.
        # Without the same commit here, round 2's diff would be taken against
        # the original base instead of round 1's commit, and the anchors would
        # never match.  A reviewer step marks the end of a round.
        if step.get("phase") == "reviewer" and step.get("structured"):
            _simulated_commit(simulated)
    return patched


def _simulated_commit(simdir: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=simdir, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "round",
        ],
        cwd=simdir,
        capture_output=True,
    )


def _simulated_diff(simdir: Path, base: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=simdir, capture_output=True)
    result = subprocess.run(
        ["git", "diff", "--cached", "--binary", "--no-color"],
        cwd=simdir,
        capture_output=True,
        text=True,
    )
    return result.stdout


# --------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------


def scenario_converges(tmp: Path) -> None:
    print("\n[scenario] converges on round 1 → draft PR path")
    repo = make_repo(tmp)
    home = make_home(tmp)
    script = anchor_steps(
        repo,
        [
            fixer_step(edits=[{"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}]),
            reviewer_step("approve"),
        ],
    )
    code, out = run_autofix(repo, home, script)
    check("converge: exit 0", code == 0, f"exit={code}\n{out}")
    check("converge: result reported", "converged" in out, out)
    check("converge: fixer reported", "result: converged" in out, out)


def scenario_not_converges(tmp: Path) -> None:
    print("\n[scenario] never converges → stops at exactly 3 rounds, no PR")
    repo = make_repo(tmp)
    home = make_home(tmp)
    issue = {
        "id": "C1",
        "severity": "major",
        "category": "correctness",
        "file": "calc.py",
        "line": 1,
        "title": "problem",
        "detail": "still wrong",
        "suggestion": "fix it",
    }
    steps = []
    for n in range(3):
        steps.append(fixer_step(edits=[{"path": "calc.py", "content": f"def add(a, b):\n    return a + b  # {n}\n"}]))
        steps.append(reviewer_step("request_changes", issues=[{**issue, "title": f"problem {n}"}]))
    code, out = run_autofix(repo, home, anchor_steps(repo, steps))
    check("nonconverge: non-zero exit", code != 0, f"exit={code}")
    check("nonconverge: max_iterations", "max_iterations_exceeded" in out, out)
    check("nonconverge: exactly 3 fixer rounds", _rounds(out) == 3, out)


def _rounds(out: str) -> int:
    """Count fixer invocations the fake CLI actually made."""
    import re

    match = re.search(r"after (\d+) rounds", out)
    return int(match.group(1)) if match else (3 if "max_iterations_exceeded" in out else 0)


def scenario_empty_diff(tmp: Path) -> None:
    print("\n[scenario] fixer changes nothing → no_changes, no PR")
    repo = make_repo(tmp)
    home = make_home(tmp)
    script = anchor_steps(repo, [fixer_step(), reviewer_step("request_changes")])
    code, out = run_autofix(repo, home, script)
    check("empty: non-zero exit", code != 0, f"exit={code}")
    check("empty: no_changes", "no_changes" in out, out)


def scenario_contract_tampered(tmp: Path) -> None:
    print("\n[scenario] fixer edits the contract → tampered gate fires")
    repo = make_repo(tmp)
    home = make_home(tmp)
    script = anchor_steps(
        repo,
        [
            fixer_step(
                edits=[
                    {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                    {"path": ".claude/autofix.toml", "content": "version = 1\n# neutered\n"},
                ]
            ),
        ],
    )
    code, out = run_autofix(repo, home, script)
    check("tamper: non-zero exit", code != 0, f"exit={code}")
    check("tamper: contract_tampered", "contract_tampered" in out, out)


def scenario_protected_path(tmp: Path) -> None:
    print("\n[scenario] fixer edits a protected path → gate fires")
    repo = make_repo(tmp)
    home = make_home(tmp)
    # Make .github protected by adding it to the contract.
    contract = (repo / ".claude" / "autofix.toml").read_text()
    (repo / ".claude" / "autofix.toml").write_text(
        contract.replace("lock_tests = true", 'lock_tests = true\n\n[paths]\nprotected = [".github/**"]')
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "paths"],
        cwd=repo,
        check=True,
    )
    script = anchor_steps(
        repo,
        [
            fixer_step(
                edits=[
                    {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                    {"path": ".github/workflows/ci.yml", "content": "on: push\n"},
                ]
            ),
        ],
    )
    code, out = run_autofix(repo, home, script)
    check("protected: non-zero exit", code != 0, f"exit={code}")
    check("protected: gate fired", "protected_path_violation" in out, out)


def scenario_tests_weakened(tmp: Path) -> None:
    print("\n[scenario] fixer deletes an assertion → tests_weakened gate fires")
    repo = make_repo(tmp, test_command="python3 -m pytest -q tests/")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "test"],
        cwd=repo,
        check=True,
    )
    script = anchor_steps(
        repo,
        [
            fixer_step(
                edits=[
                    {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"},
                    {"path": "tests/test_calc.py", "content": "def test_add():\n    pass\n"},
                ]
            ),
        ],
    )
    code, out = run_autofix(repo, make_home(tmp), script)
    check("weakened: non-zero exit", code != 0, f"exit={code}")
    check("weakened: gate fired", "tests_weakened" in out, out)


def scenario_self_review_forced(tmp: Path) -> None:
    print("\n[scenario] same model both sides without allow_self_review → refuses")
    repo = make_repo(tmp)
    home = make_home(tmp, same_model=True)
    code, out = run_autofix(repo, home, [fixer_step(), reviewer_step("approve")])
    check("self-review: config error exit", code == 2, f"exit={code}\n{out}")
    check("self-review: explains", "same model" in out, out)


def scenario_metrics_written(tmp: Path) -> None:
    print("\n[scenario] metrics are recorded per phase")
    repo = make_repo(tmp)
    home = make_home(tmp)
    script = anchor_steps(
        repo,
        [
            fixer_step(edits=[{"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}]),
            reviewer_step("approve"),
        ],
    )
    code, out = run_autofix(repo, home, script)
    metrics_file = home / "metrics" / "metrics.jsonl"
    check("metrics: file exists", metrics_file.exists(), str(metrics_file))
    if metrics_file.exists():
        records = [json.loads(l) for l in metrics_file.read_text().splitlines() if l.strip()]
        phases = {r["phase"] for r in records}
        check("metrics: fixer+reviewer+tests recorded", {"fixer", "reviewer"} <= phases, str(phases))
        fixer = next(r for r in records if r["phase"] == "fixer")
        check("metrics: token counters present", fixer["raw"]["input_tokens"] == 1000, str(fixer.get("raw")))
        check("metrics: prompt_ver present", bool(fixer.get("prompt_ver")), str(fixer))
        check("metrics: duration_ms present", fixer.get("duration_ms") == 1234, str(fixer))
        # summary must render without a price entry for the fake model
        result = subprocess.run(
            [sys.executable, str(AUTOFIX), "summary"],
            cwd=repo,
            env={**os.environ, "AUTOFIX_HOME": str(home)},
            capture_output=True,
            text=True,
        )
        check("metrics: summary renders", result.returncode == 0, result.stderr)


def scenario_lock_held(tmp: Path) -> None:
    print("\n[scenario] a second concurrent run refuses cleanly, no traceback")
    repo = make_repo(tmp)
    home = make_home(tmp)

    sys.path.insert(0, str(PLUGIN / "scripts"))
    from autofix import paths as paths_mod

    # Simulate a live holder.  The pid must be one that exists, or the lock is
    # taken over as stale and the run proceeds.
    lock_dir = paths_mod.state_dir(repo, ci=False)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / "issue-1.lock"
    lock.write_text(f"{os.getpid()} {time.time()}\n")

    try:
        code, out = run_autofix(repo, home, [fixer_step(), reviewer_step("approve")])
        check("lock: non-zero exit", code != 0, f"exit={code}")
        check("lock: reports contention", "lock_held" in out, out)
        check("lock: no traceback", "Traceback" not in out, out)
    finally:
        lock.unlink(missing_ok=True)


def scenario_dry_run(tmp: Path) -> None:
    print("\n[scenario] dry run prints the plan and leaks no secrets")
    repo = make_repo(tmp)
    home = make_home(tmp)
    code, out = run_autofix(repo, home, [], extra=["--dry-run"])
    check("dry-run: exit 0", code == 0, f"exit={code}\n{out}")
    check("dry-run: mentions fixer", "fixer:" in out, out)
    check("dry-run: no secret value", "fixer-secret-value" not in out, out)
    check("dry-run: no reviewer token in fixer env", "reviewer-secret-value" not in out, out)

    # The checks above use long secrets, which `log.redact` would scrub even if
    # they were printed.  A short credential is the real test: redaction
    # deliberately ignores short values, so the dry run must not print the
    # value at all rather than rely on redaction to catch it.
    (home / "env").write_text("FAKE_FIXER_TOKEN=sh0rt\nFAKE_REVIEWER_TOKEN=sh0rt2\n")
    code, out = run_autofix(repo, home, [], extra=["--dry-run"])
    check("dry-run: no short credential printed", "sh0rt" not in out, out)
    check("dry-run: says a credential is set", "<set" in out, out)


def scenario_isolation_unit(tmp: Path) -> None:
    print("\n[unit] env isolation")
    sys.path.insert(0, str(PLUGIN / "scripts"))
    from autofix import isolation
    from autofix.isolation import Profile

    fixer = Profile(name="fixer", model="m1", base_url="http://a", auth_value="FIXER-SECRET-VALUE")
    reviewer = Profile(name="reviewer", model="m2", base_url="http://b", auth_value="REVIEWER-SECRET-VALUE")
    isolation.register_known_secret("FIXER-SECRET-VALUE")
    isolation.register_known_secret("REVIEWER-SECRET-VALUE")

    env = isolation.build_phase_env(fixer, phase="fixer")
    check("isolation: fixer env has its own token", env.get("ANTHROPIC_AUTH_TOKEN") == "FIXER-SECRET-VALUE")
    check(
        "isolation: fixer env has NO reviewer token",
        "REVIEWER-SECRET-VALUE" not in env.values(),
        str(env),
    )
    check("isolation: fixer env has no GH_TOKEN", "GH_TOKEN" not in env)
    check("isolation: fixer env has no GITHUB_TOKEN", "GITHUB_TOKEN" not in env)
    check("isolation: os.environ not copied wholesale", "ANTHROPIC_API_KEY" not in env)

    # The assertion must fire when a foreign secret is smuggled in.
    bad = isolation.build_phase_env(fixer, phase="fixer", extra={"LEAK": "REVIEWER-SECRET-VALUE"})
    raised = False
    try:
        isolation.assert_no_foreign_secrets(bad, allowed={"FIXER-SECRET-VALUE"}, phase="fixer")
    except isolation.IsolationError:
        raised = True
    check("isolation: assertion catches a smuggled secret", raised)

    # A credential-free phase must carry none.
    tests_env = isolation.build_phase_env(None, phase="tests")
    check(
        "isolation: tests env has no tokens",
        not any("SECRET-VALUE" in v for v in tests_env.values()),
        str(tests_env),
    )


def scenario_comment_unit(tmp: Path) -> None:
    print("\n[unit] every failure mode posts exactly one actionable comment")
    sys.path.insert(0, str(PLUGIN / "scripts"))
    from autofix import classify, loop, prompts
    from autofix.state import RunState, StateStore

    # Every non-success reason must have a template that actually exists, or a
    # run would fail silently with no explanation for the human.
    missing = []
    for reason, outcome in classify.FAILURES.items():
        if classify.is_success(reason):
            continue
        if not (PLUGIN / "prompts" / "comments" / f"{outcome.comment}.md").exists():
            missing.append(f"{reason} -> {outcome.comment}.md")
    check("comments: every failure reason has a template", not missing, str(missing))

    # Every template needs a Chinese sibling, and the two must use the same
    # placeholders.  A translation that drops a placeholder renders a comment
    # with a blank where the offender should be — worse than English.
    import re as _re

    no_zh, placeholder_drift = [], []
    for path in sorted((PLUGIN / "prompts" / "comments").glob("*.md")):
        if path.name.endswith(".zh.md"):
            continue
        zh = path.with_name(path.stem + ".zh.md")
        if not zh.exists():
            no_zh.append(path.name)
            continue
        en_ph = set(_re.findall(r"\{[a-z_]+\}", path.read_text()))
        zh_ph = set(_re.findall(r"\{[a-z_]+\}", zh.read_text()))
        if en_ph != zh_ph:
            placeholder_drift.append(f"{path.name}: en={sorted(en_ph)} zh={sorted(zh_ph)}")
    check("comments: every template has a Chinese version", not no_zh, str(no_zh))
    check("comments: translations keep the placeholders", not placeholder_drift, str(placeholder_drift))

    class Stub:
        def __init__(self):
            self.calls = []

        def comment_issue(self, number, body):
            self.calls.append((number, body))

    class Cfg:
        issue = 7

    stub = Stub()
    store = StateStore(tmp / "state", 7)
    state = RunState(round=3, branch="autofix/issue-7")
    state.open_issues = [
        {"severity": "major", "title": "boom", "file": "a.py", "line": 3, "detail": "d"}
    ]
    out = loop.RunOutcome(
        "protected_path_violation", state, {"protected": [".github/workflows/ci.yml"]}
    )
    prompts.load("comments/protected_paths.md")  # must not raise

    loop._comment_terminal(Cfg(), stub, store, out)
    check("comments: one comment posted", len(stub.calls) == 1, str(len(stub.calls)))
    body = stub.calls[0][1] if stub.calls else ""
    check("comments: names the offender", ".github/workflows/ci.yml" in body, body[:200])
    check("comments: no raw placeholder left", "{" not in body, body[:200])

    # A resumed run must not post the same comment twice.
    loop._comment_terminal(Cfg(), stub, store, out)
    check("comments: not posted twice", len(stub.calls) == 1, str(len(stub.calls)))

    # A converged run reports through its PR, not a comment.
    loop._comment_terminal(
        Cfg(), stub, store, loop.RunOutcome("converged", RunState(round=1), {})
    )
    check("comments: success posts nothing", len(stub.calls) == 1, str(len(stub.calls)))

    # -- language ----------------------------------------------------------
    check(
        "lang: Chinese issue detected",
        prompts.detect_language("修复登录崩溃", "用户点击登录时应用崩溃了") == "zh",
        "expected zh",
    )
    check(
        "lang: English issue detected",
        prompts.detect_language("Fix login crash", "The app crashes on login") == "en",
        "expected en",
    )
    # A Chinese issue about code is mostly Latin tokens; the prose must win.
    check(
        "lang: Chinese issue full of code is still zh",
        prompts.detect_language(
            "修复 `get_user()` 的空指针",
            "调用 `get_user()` 时抛出 AttributeError。\n```python\nuser = get_user(id)\nprint(user.name)\n```\n请修复这个问题。",
        )
        == "zh",
        "expected zh",
    )
    check(
        "lang: empty text defaults to en", prompts.detect_language("", "") == "en"
    )

    # A Chinese issue must produce a Chinese comment, via the same code path.
    stub2 = Stub()
    store2 = StateStore(tmp / "state2", 7)
    zh_state = RunState(round=3, branch="autofix/issue-7", lang="zh")
    zh_state.open_issues = [
        {"severity": "major", "title": "问题", "file": "a.py", "line": 3, "detail": "细节"}
    ]
    zh_out = loop.RunOutcome(
        "protected_path_violation", zh_state, {"protected": [".github/workflows/ci.yml"]}
    )
    loop._comment_terminal(Cfg(), stub2, store2, zh_out)
    zh_body = stub2.calls[0][1] if stub2.calls else ""
    check("lang: Chinese comment is Chinese", "受保护路径" in zh_body, zh_body[:200])
    check("lang: Chinese comment has no raw placeholder", "{" not in zh_body, zh_body[:200])

    # An untranslated language falls back to English rather than failing.
    fallback = prompts.load_localized("comments/protected_paths.md", "fr")
    check("lang: unknown language falls back to English", "protected paths" in fallback)


def scenario_no_merge_unit(tmp: Path) -> None:
    print("\n[unit] no merge capability anywhere in the plugin")
    import re

    # Match the call shapes that would actually merge a PR.  Comments and
    # docstrings are stripped first, because they legitimately *document* that
    # merging is absent.
    patterns = [
        re.compile(r"""["']merge["']"""),
        re.compile(r"gh\s+pr\s+merge"),
        re.compile(r"/pulls/[^/\"']*/merge"),
        re.compile(r"mergePullRequest"),
        re.compile(r"\.merge\("),
    ]
    offenders = []
    for path in (PLUGIN / "scripts").rglob("*.py"):
        text = _strip_comments_and_docstrings(path.read_text())
        for pattern in patterns:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(PLUGIN)}: {pattern.pattern}")
    check("no-merge: no merge calls in plugin code", not offenders, str(offenders))


def scenario_classify_unit(tmp: Path) -> None:
    print("\n[unit] a healthy run is never misread as an auth/rate failure")
    sys.path.insert(0, str(PLUGIN / "scripts"))
    from autofix import phases
    from autofix.proc import ProcResult

    def ok(stdout: str = "", stderr: str = "", exit_code: int = 0):
        return ProcResult(
            argv=[], exit_code=exit_code, stdout=stdout, stderr=stderr, wall_ms=0, timed_out=False
        )

    # The exact bug this guards: the CLI echoes the reviewed artifact, and a
    # commit sha containing "429" was classified as rate limiting, aborting a
    # converged run.  Hashes, line numbers and quoted source must never match.
    echo = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "structured_output": {
                "base_sha": "569cf49429860ad914c9d3eb5928fb630bac9dbf",
                "diff_hash": "sha256:274d4aadce8783a445d888b82996fe6b110416fd",
                "issues": [{"line": 429, "detail": "HTTP 429 handling is missing"}],
            },
            "usage": {"input_tokens": 429},
        }
    )
    healthy = ok(echo)
    healthy_result = {"type": "result", "is_error": False, "structured_output": {}}
    got = phases._classify(healthy, healthy_result)
    check("classify: sha containing 429 is not rate limiting", got == "ok", got)

    # A genuine failure is still diagnosed from what it printed.
    got = phases._classify(ok(stderr="HTTP 429 Too Many Requests", exit_code=1), None)
    check("classify: real 429 still detected", got == "rate_limited", got)
    got = phases._classify(ok(stderr="error: 401 unauthorized", exit_code=1), None)
    check("classify: real 401 still detected", got == "auth_error", got)
    got = phases._classify(ok(stderr="boom", exit_code=1), None)
    check("classify: bare nonzero exit is cli_error", got == "cli_error", got)


def _strip_comments_and_docstrings(source: str) -> str:
    import ast
    import re

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            doc = ast.get_docstring(node, clean=False)
            if doc and node.body and isinstance(node.body[0], ast.Expr):
                target = node.body[0]
                for i in range(target.lineno - 1, target.end_lineno or target.lineno):
                    lines[i] = ""
    # Drop # comments.
    return "\n".join(re.sub(r"#.*$", "", line) for line in lines)


def main() -> int:
    print("auto-fix end-to-end tests")
    scenario_isolation_unit(Path(tempfile.mkdtemp()))
    scenario_comment_unit(Path(tempfile.mkdtemp()))
    scenario_no_merge_unit(Path(tempfile.mkdtemp()))
    scenario_classify_unit(Path(tempfile.mkdtemp()))
    for scenario in (
        scenario_converges,
        scenario_not_converges,
        scenario_empty_diff,
        scenario_contract_tampered,
        scenario_protected_path,
        scenario_tests_weakened,
        scenario_self_review_forced,
        scenario_metrics_written,
        scenario_lock_held,
        scenario_dry_run,
    ):
        tmp = Path(tempfile.mkdtemp(prefix="autofix-e2e-"))
        try:
            scenario(tmp)
        except Exception as exc:  # noqa: BLE001
            check(scenario.__name__, False, f"raised {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("failed: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
