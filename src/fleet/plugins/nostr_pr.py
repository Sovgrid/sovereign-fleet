"""Pull requests on Nostr (NIP-34) for a project whose remote is a nostr:// URL (RFC step F3).

ngit's own `git push` to a nostr:// remote needs a signer in ngit's key store and cannot sign the
HTTP auth a Buzz git server asks for, so the fleet does the two halves itself, with its own key:

1. The git data goes to the repository's git server (the first `git_servers` entry of its
   announcement, read with `ngit repo --json`): fetch the default branch, rebase onto it, push the
   task branch as `pr/<branch>`. A server that answers 401 with `WWW-Authenticate: Nostr` (Buzz)
   gets a NIP-98 token (kind 27235 over the repository URL, signed with nak); Buzz binds it to
   the URL and +-60 s, not to the method, so one token serves a fetch or a push. The token
   reaches git through GIT_CONFIG_* in the child's environment, never its argv.
2. The pull request is a kind 1618 event (NIP-34) published to the repository's relays: the
   repository coordinate, its root commit, the maintainer, subject, tip commit, clone URL,
   branch name, target branch (`b`) and merge base, as ngit writes them. A pull request of the
   same branch that the fleet already opened gets a kind 1619 update instead of a second one.

On a Buzz relay the repository must be bound to a channel the fleet is a member of; the relay's
push policy then decides (protected branches stay the maintainers'). Nothing here merges:
a maintainer merges with `ngit pr merge`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from fleet.core.config import _expand
from fleet.core.redact import redact

# (argv, cwd, env additions) -> (returncode, stdout, stderr)
Runner = Callable[[Sequence[str], Optional[Path], Dict[str, str]], Awaitable[Tuple[int, str, str]]]
# url -> True when the git server asks for NIP-98 ("WWW-Authenticate: Nostr")
Challenge = Callable[[str], Awaitable[bool]]


class NostrPrError(RuntimeError):
    """The pull request could not be pushed or published; the message says which step."""


async def run(argv: Sequence[str], cwd: Optional[Path], env: Dict[str, str]) -> Tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd) if cwd else None, env={**os.environ, **env},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
    except TimeoutError:
        proc.kill()
        return 124, "", f"{Path(argv[0]).name} timed out"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def asks_for_nostr_auth(url: str) -> bool:
    """True when the git server answers the push advertisement with a Nostr (NIP-98) challenge."""
    def probe() -> bool:
        req = urllib.request.Request(url.rstrip("/") + "/info/refs?service=git-receive-pack")
        try:
            with urllib.request.urlopen(req, timeout=20):
                return False
        except urllib.error.HTTPError as e:
            return e.code == 401 and "nostr" in (e.headers.get("WWW-Authenticate") or "").lower()
        except (urllib.error.URLError, OSError, ValueError):
            return False
    return await asyncio.get_running_loop().run_in_executor(None, probe)


@dataclass(frozen=True)
class NostrRepo:
    url: str                    # the project's nostr:// remote
    identifier: str
    owner: str                  # 64-hex pubkey of the lead maintainer
    maintainers: Tuple[str, ...]  # 64-hex
    relays: Tuple[str, ...]
    clone: str                  # the git server the fleet pushes to

    @property
    def coordinate(self) -> str:
        return f"30617:{self.owner}:{self.identifier}"


def _hex(npub_or_hex: str) -> str:
    from fleet.core.nostr_identity import npub_hex
    value = str(npub_or_hex or "")
    return value if re.fullmatch(r"[0-9a-f]{64}", value) else npub_hex(value)


class NostrPullRequests:
    """Opens or updates the fleet's NIP-34 pull request of a branch."""

    def __init__(self, key_file: Path, pubkey: str, npub: str, workdir: Path, ngit: str = "ngit",
                 nak: str = "nak", runner: Runner = run, challenge: Challenge = asks_for_nostr_auth):
        self.key_file = Path(key_file)
        self.pubkey, self.npub = pubkey, npub
        self.workdir = Path(workdir)
        self.ngit, self.nak = _expand(ngit), _expand(nak)
        self.run, self.challenge = runner, challenge

    # helpers

    def _secret_env(self) -> Dict[str, str]:
        from fleet.core.nostr_identity import _secret
        return {"NOSTR_SECRET_KEY": _secret(self.key_file)}

    async def _ngit(self, url: str, args: List[str]) -> object:
        # ngit runs only inside a git repository; a fresh one per call, because its incremental
        # sync of a reused event cache misses events other clients published (see NgitTracker).
        self.workdir.mkdir(parents=True, exist_ok=True)
        cwd = Path(tempfile.mkdtemp(dir=self.workdir, prefix="ngit-"))
        try:
            await self.run(["git", "init", "-q", str(cwd)], None, {})
            code, out, err = await self.run([self.ngit, *args, "--repo", url, "--nsec-file",
                                             str(self.key_file), "--json"], cwd, {})
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
        try:
            data = json.loads(out) if out.strip() else None
        except ValueError:
            data = None
        if code != 0 or (isinstance(data, dict) and data.get("command_status") == "error"):
            why = data.get("error") if isinstance(data, dict) else None
            raise NostrPrError(f"ngit {' '.join(args[:2])}: {(why or err or out).strip()[-300:]}")
        return data

    async def _git_env(self, clone: str) -> Dict[str, str]:
        """GIT_CONFIG_* with a fresh NIP-98 header when the server asks for one, else {}."""
        if not await self.challenge(clone):
            return {}
        code, out, err = await self.run([self.nak, "event", "-k", "27235", "-t", f"u={clone}",
                                         "-t", "method=GET"], None, self._secret_env())
        if code != 0 or not out.strip().startswith("{"):
            raise NostrPrError(f"cannot sign the git server's NIP-98 token: {err.strip()[-200:]}")
        token = base64.b64encode(out.strip().splitlines()[-1].encode()).decode()
        return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Nostr {token}"}

    async def _git(self, worktree: Path, clone: str, *args: str) -> Tuple[int, str, str]:
        from fleet.core.worktree import _SAFE_GIT
        code, out, err = await self.run(["git", *_SAFE_GIT, "-C", str(worktree), *args], None,
                                        await self._git_env(clone))
        return code, out, redact(err)

    # the steps

    async def resolve(self, url: str) -> NostrRepo:
        info = await self._ngit(url, ["repo"])
        if not isinstance(info, dict):
            raise NostrPrError("ngit repo: no JSON object")
        servers = [s for s in info.get("git_servers") or [] if isinstance(s, str)]
        clone = next((s for s in servers if s.startswith("https://")), servers[0] if servers else "")
        relays = tuple(r for r in info.get("relays") or [] if isinstance(r, str))
        lead = info.get("lead_maintainer") or (info.get("maintainers") or [""])[0]
        if not (clone and relays and lead and info.get("identifier")):
            raise NostrPrError("the repository announcement lacks a git server, a relay or a maintainer")
        try:
            owner = _hex(lead)
            maintainers = tuple(_hex(m) for m in info.get("maintainers") or [lead])
        except ValueError as e:
            raise NostrPrError(f"unreadable maintainer key in the announcement: {e}") from None
        return NostrRepo(url, str(info["identifier"]), owner, maintainers, relays, clone)

    async def fetch_base(self, repo: NostrRepo, worktree: Path, base: str) -> None:
        code, _, err = await self._git(worktree, repo.clone, "fetch", "-q", repo.clone, base)
        if code != 0:
            raise NostrPrError(f"fetch of {base} from the git server failed: {err.strip()[-200:]}")

    async def push(self, repo: NostrRepo, worktree: Path, branch: str) -> None:
        # The fleet's own pr/ branch: a rerun replaces it, as the update event below says.
        code, _, err = await self._git(worktree, repo.clone, "push", "--force", repo.clone,
                                       f"HEAD:refs/heads/pr/{branch}")
        if code != 0:
            raise NostrPrError(f"push of pr/{branch} failed: {err.strip()[-200:]}")

    async def open_pull_request(self, repo: NostrRepo, worktree: Path, branch: str, base: str,
                                title: str, body: str) -> str:
        """Publish the pull request (or its update); return a nostr: link to it."""
        from fleet.core.worktree import _SAFE_GIT
        async def out(*args: str) -> str:
            code, text, _ = await self.run(["git", *_SAFE_GIT, "-C", str(worktree), *args], None, {})
            return text.strip() if code == 0 else ""
        tip = await out("rev-parse", "HEAD")
        root = (await out("rev-list", "--max-parents=0", "HEAD")).splitlines()
        merge_base = await out("merge-base", "HEAD", "FETCH_HEAD")
        if not (tip and root):
            raise NostrPrError("cannot read the branch's commits")
        common = ["-t", f"a={repo.coordinate}", "-t", f"r={root[-1]}",
                  *[x for m in repo.maintainers for x in ("-t", f"p={m}")],
                  "-t", f"c={tip}", "-t", f"clone={repo.clone}"]
        if merge_base:
            common += ["-t", f"merge-base={merge_base}"]
        existing = await self.existing_pull_request(repo, branch)
        if existing:
            argv = [self.nak, "event", "-k", "1619", "-c", "", *common,
                    "-t", f"E={existing}", "-t", f"P={self.pubkey}"]
            root_id = existing
        else:
            argv = [self.nak, "event", "-k", "1618", "-c", body, *common,
                    "-t", f"subject={title}", "-t", f"branch-name={branch[:60]}", "-t", f"b={base}",
                    "-t", f"alt=git pull request: {title}"]
            root_id = ""
        code, text, err = await self.run([*argv, "--auth", *repo.relays], None, self._secret_env())
        published = next((line for line in text.splitlines() if line.startswith("{")), "")
        if code != 0 or "success" not in (err + text).lower() or not published:
            raise NostrPrError(f"publishing the pull request event failed: {redact((err or text).strip()[-200:])}")
        root_id = root_id or json.loads(published)["id"]
        code, nevent, _ = await self.run([self.nak, "encode", "nevent", "--relay", repo.relays[0], root_id], None, {})
        return f"nostr:{nevent.strip()}" if code == 0 and nevent.strip() else f"nostr:{root_id}"

    async def existing_pull_request(self, repo: NostrRepo, branch: str) -> str:
        """Event id (hex) of the fleet's open pull request of `branch`, or ''."""
        from fleet.core.nostr_identity import nevent_id
        try:
            listed = await self._ngit(repo.url, ["pr", "list", "--status", "open,draft"])
        except NostrPrError:
            return ""                        # no lookup, no reuse: a new pull request is opened
        for item in listed if isinstance(listed, list) else []:
            if not isinstance(item, dict) or item.get("author") != self.npub:
                continue
            m = re.fullmatch(r"pr/(.+)\([0-9a-f]{8}\)", str(item.get("branch") or ""))
            if m and m.group(1) == branch[:60]:
                try:
                    return nevent_id(str(item.get("id", "")))
                except ValueError:
                    continue
        return ""
