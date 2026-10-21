"""Pull request providers for the `pull_request` landing mode.

A provider opens a pull request for a pushed branch and returns its URL. Built in: GitHub
(through the `gh` CLI and its login) and Gitea (REST API with a token file from fleet.toml).
`provider = "auto"` in a project picks one from the remote URL. Add your own by subclassing
PullRequestProvider and passing it to the engine.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Optional, Tuple

from fleet.core.config import Project, _expand, load_config


class PullRequestProvider(ABC):
    name = "provider"

    @abstractmethod
    async def open(self, repo: Path, remote_url: str, branch: str, base: str, title: str, body: str) -> str:
        """Open a pull request of `branch` into `base`; return its URL or raise RuntimeError."""


class GitHubCli(PullRequestProvider):
    name = "github"

    async def open(self, repo, remote_url, branch, base, title, body) -> str:
        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body,
            cwd=str(repo), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError((err or b"").decode(errors="replace").strip()[-300:] or "gh pr create failed")
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

    async def open(self, repo, remote_url, branch, base, title, body) -> str:
        parts = owner_repo(remote_url)
        if not parts:
            raise RuntimeError(f"cannot read owner/repo from {remote_url}")
        token_path = Path(_expand(self.token_file))
        if token_path.exists() and (token_path.stat().st_mode & 0o077) != 0:
            import logging
            logging.getLogger("fleet.plugins.landing").warning(
                "Token file %s has overly permissive permissions (%o); recommend chmod 0600",
                token_path, token_path.stat().st_mode & 0o777)
        token = token_path.read_text(encoding="utf-8").strip()
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
