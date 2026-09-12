"""GitHub client.

There is deliberately **no merge method here**, and no ``gh pr merge`` anywhere
in this plugin: a human merges.  A test greps the source to keep it that way.

Two backends: the ``gh`` CLI when available (local runs), else the REST API via
``urllib`` (CI, where ``GITHUB_TOKEN`` is present).
"""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import isolation, log
from .proc import run_proc

API = "https://api.github.com"


@dataclass
class Issue:
    number: int
    title: str
    body: str
    author: str
    labels: list[str] = field(default_factory=list)


@dataclass
class PullRef:
    number: int
    url: str


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    """Base + shared logic.  Concrete backends implement the primitives."""

    def __init__(self, repo: str, token: str | None = None):
        self.repo = repo
        self._token = token
        if token:
            isolation.register_known_secret(token)

    # -- primitives, overridden by backends --------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list | None:
        raise NotImplementedError

    def get_issue(self, number: int) -> Issue:
        data = self._request("GET", f"/repos/{self.repo}/issues/{number}")
        return _parse_issue(data)

    def comment_issue(self, number: int, body: str) -> None:
        self._request("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def add_labels(self, number: int, labels: list[str]) -> None:
        if not labels:
            return
        for label in labels:
            self._ensure_label(label)
        self._request("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": labels})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._request("DELETE", f"/repos/{self.repo}/issues/{number}/labels/{label}")
        except GitHubError:
            pass  # already absent

    def _ensure_label(self, label: str) -> None:
        # Create if missing; ignore 422 (already exists).
        try:
            self._request("POST", f"/repos/{self.repo}/labels", {"name": label, "color": "ededed"})
        except GitHubError:
            pass

    def find_open_pr_for_branch(self, branch: str) -> PullRef | None:
        owner = self.repo.split("/")[0]
        data = self._request(
            "GET", f"/repos/{self.repo}/pulls?state=open&head={owner}:{branch}"
        )
        if isinstance(data, list) and data:
            return PullRef(number=data[0]["number"], url=data[0]["html_url"])
        return None

    def create_draft_pr(self, *, head: str, base: str, title: str, body: str) -> PullRef:
        data = self._request(
            "POST",
            f"/repos/{self.repo}/pulls",
            {"head": head, "base": base, "title": title, "body": body, "draft": True},
        )
        return PullRef(number=data["number"], url=data["html_url"])

    def update_pr(self, number: int, *, body: str) -> None:
        self._request("PATCH", f"/repos/{self.repo}/pulls/{number}", {"body": body})

    def permission_for(self, login: str) -> str:
        try:
            data = self._request("GET", f"/repos/{self.repo}/collaborators/{login}/permission")
            return str(data.get("permission", "none"))
        except GitHubError:
            return "none"


class GHClient(GitHubClient):
    """Backend using the ``gh`` CLI (present in local sessions)."""

    def __init__(self, repo: str, token: str | None = None):
        super().__init__(repo, token)

    def _env(self) -> dict[str, str]:
        env = isolation.build_phase_env(None, phase="gh")
        if self._token:
            env["GH_TOKEN"] = self._token
        else:
            # gh reads its own keyring config for the logged-in user.
            env["HOME"] = str(Path.home())
        return env

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list | None:
        args = ["gh", "api", "--method", method, path.lstrip("/")]
        stdin_text = None
        if payload is not None:
            args += ["--input", "-"]
            stdin_text = json.dumps(payload)
        result = run_proc(args, env=self._env(), cwd=Path.cwd(), timeout_s=60, stdin_text=stdin_text)
        if result.exit_code != 0:
            raise GitHubError(
                f"gh api {method} {path} failed: {log.redact(result.stderr.strip())}"
            )
        out = result.stdout.strip()
        return json.loads(out) if out else None


class RestClient(GitHubClient):
    """Backend using the REST API directly (CI, with GITHUB_TOKEN)."""

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict | list | None:
        url = path if path.startswith("http") else API + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "auto-fix-plugin")
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise GitHubError(
                f"{method} {path} -> {exc.code}: {log.redact(detail[:300])}"
            ) from None
        except urllib.error.URLError as exc:
            raise GitHubError(f"{method} {path} failed: {exc.reason}") from None


def make_client(repo: str, *, token: str | None, prefer_gh: bool = True) -> GitHubClient:
    if prefer_gh and shutil.which("gh") and _gh_authenticated(token):
        return GHClient(repo, token)
    if token:
        return RestClient(repo, token)
    raise GitHubError(
        "no GitHub access: install and authenticate the `gh` CLI, or set GITHUB_TOKEN"
    )


def _gh_authenticated(token: str | None) -> bool:
    env = isolation.build_phase_env(None, phase="gh")
    if token:
        env["GH_TOKEN"] = token
    result = run_proc(["gh", "auth", "status"], env=env, cwd=Path.cwd(), timeout_s=20)
    return result.exit_code == 0


def _parse_issue(data: dict) -> Issue:
    user = data.get("user") or {}
    return Issue(
        number=int(data.get("number", 0)),
        title=str(data.get("title", "")),
        body=str(data.get("body") or ""),
        author=str(user.get("login", "")),
        labels=[l.get("name", "") for l in data.get("labels", [])],
    )
