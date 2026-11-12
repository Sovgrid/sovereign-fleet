"""Pull request providers for the `pull_request` landing mode.

A provider opens a pull request for a pushed branch and returns its URL. Built in: GitHub
(through the `gh` CLI and its login) and Gitea (REST API with a token file from fleet.toml).
`provider = "auto"` in a project picks one from the remote URL. Add your own by subclassing
PullRequestProvider and passing it to the engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Optional, Tuple

from fleet.core.config import Project, _expand, load_config
from fleet.core.redact import redact

logger = logging.getLogger(__name__)


class PullRequestProvider(ABC):
    name = "provider"

    @abstractmethod
    async def open(self, repo: Path, remote_url: str, branch: str, base: str, title: str, body: str) -> str:
        """Open a pull request of `branch` into `base`; return its URL or raise RuntimeError."""


class GitHubCli(PullRequestProvider):
    name = "github"

    async def _find_existing_pr(self, repo, branch, base) -> Optional[str]:
        """Look for an already-open PR of this branch into base; return its URL or None."""
        # A failed lookup is not an error: the create call below still runs.
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "list", "--state", "open", "--head", branch, "--base", base,
                "--json", "url", "--jq", ".[0].url",
                cwd=str(repo), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (TimeoutError, OSError) as e:
            logger.debug("gh pr list for %s failed: %s", branch, e)
            return None
        if proc.returncode == 0:
            url = (out or b"").decode(errors="replace").strip()
            if url:
                return url
        return None

    async def open(self, repo, remote_url, branch, base, title, body) -> str:
        # PR-REUSE: if the branch already has an open PR into base, reuse its URL instead of failing.
        existing = await self._find_existing_pr(repo, branch, base)
        if existing:
            return existing
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body,
            cwd=str(repo), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(redact((err or b"").decode(errors="replace").strip()[-300:]) or "gh pr create failed")
        lines = (out or b"").decode(errors="replace").strip().splitlines()
        return lines[-1] if lines else ""


def owner_repo(remote_url: str) -> Optional[Tuple[str, str]]:
    """('owner', 'repo') from an https or ssh git remote URL."""
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", remote_url)
    return (m.group(1), m.group(2)) if m else None


class GiteaApi(PullRequestProvider):
    name = "gitea"

    def __init__(self, base_url: str, token_file: str):
        self.base_url = base_url.rstrip("/")
        self.token_file = token_file

    async def _find_existing_pr(self, parts, branch, base, token) -> Optional[str]:
        """Look for an already-open PR of this branch into base; return its URL or None."""
        # Gitea's list filters by base (`base_branch`) but not by head, so the head is matched here;
        # trusting the server to filter would hand back another branch's pull request.
        query = urllib.parse.urlencode({"state": "open", "base_branch": base, "limit": 50})
        url = f"{self.base_url}/api/v1/repos/{parts[0]}/{parts[1]}/pulls?{query}"
        req = urllib.request.Request(url, headers={"Authorization": f"token {token}"})

        def call() -> Optional[str]:
            with urllib.request.urlopen(req, timeout=30) as resp:
                items = json.loads(resp.read() or b"[]")
            if not isinstance(items, list):
                return None
            for pr in items:
                if not isinstance(pr, dict):
                    continue
                head, into = pr.get("head") or {}, pr.get("base") or {}
                if head.get("ref") == branch and into.get("ref") == base and pr.get("html_url"):
                    return pr["html_url"]
            return None

        # A failed lookup is not an error: the POST below still runs.
        try:
            return await asyncio.get_running_loop().run_in_executor(None, call)
        except Exception as e:  # noqa: BLE001 - any lookup failure falls through to create
            logger.debug("Gitea pull list for %s failed: %s", branch, redact(str(e)))
            return None

    async def open(self, repo, remote_url, branch, base, title, body) -> str:
        parts = owner_repo(remote_url)
        if not parts:
            raise RuntimeError(redact(f"cannot read owner/repo from {remote_url}"))
        token_path = Path(_expand(self.token_file))
        if token_path.exists() and (token_path.stat().st_mode & 0o077) != 0:
            import logging
            logging.getLogger("fleet.plugins.landing").warning(
                "Token file %s has overly permissive permissions (%o); recommend chmod 0600",
                token_path, token_path.stat().st_mode & 0o777)
        token = token_path.read_text(encoding="utf-8").strip()
        # PR-REUSE: if the branch already has an open PR into base, reuse its URL instead of failing.
        existing = await self._find_existing_pr(parts, branch, base, token)
        if existing:
            return existing
        req = urllib.request.Request(
            f"{self.base_url}/api/v1/repos/{parts[0]}/{parts[1]}/pulls",
            data=json.dumps({"head": branch, "base": base, "title": title, "body": body}).encode(),
            headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
            method="POST")

        def call() -> Dict:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")

        try:
            data = await asyncio.get_running_loop().run_in_executor(None, call)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Gitea answered {e.code}: {e.read()[:200]!r}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise RuntimeError(f"Gitea not reachable: {e}") from e
        return data.get("html_url", "")


def provider_for(remote_url: str, project: Project,
                 providers: Optional[Dict[str, PullRequestProvider]] = None) -> Optional[PullRequestProvider]:
    """The configured provider, or one picked from the remote URL; None if nothing fits."""
    providers = providers or {}
    name = project.provider
    gitea = load_config().gitea
    if name == "auto":
        if "github.com" in remote_url:
            name = "github"
        elif gitea.url and _same_host(gitea.url, remote_url):
            name = "gitea"
        else:
            return providers.get("auto")
    if name in providers:
        return providers[name]
    if name == "github":
        return GitHubCli()
    if name == "gitea" and gitea.url and gitea.token_file:
        return GiteaApi(gitea.url, gitea.token_file)
    return None


def _same_host(base_url: str, remote_url: str) -> bool:
    host = re.sub(r"^\w+://", "", base_url).split("/")[0]
    return bool(host) and host in remote_url
