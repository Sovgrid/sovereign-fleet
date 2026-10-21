"""Issue intake (migration phase 5): tasks from a project's Gitea or GitHub issues.

Per project in fleet.toml:

    [projects.app.intake]
    enabled = true
    provider = "auto"        # gitea / github / auto (from the project's remote, as landing does)
    label = "fleet"          # only issues with this label; "": every open issue
    authors = ["alice"]      # who may queue a task that runs unattended
    interval_min = 5

An issue is a prompt for an agent with write access to the repository. So:

- An issue of a trusted author with a valid ```fleet``` block is queued to run. The block (JSON)
  sets the task's contract: allowed_paths, gate ({"pytest": path} or {"npm": script}; the grid
  runner's gate_cmd in the same form, or as "pytest <path>" / "npm run <script>"), category,
  lane (or assigned_lane), acceptance_criteria.
- Every other issue (no block, or an author not in `authors`) is queued for a human approval in
  the dashboard. Without `authors` nothing runs unattended, which is what a public repo needs.
- A block that does not parse is answered once per issue revision and nothing is queued.

Each issue becomes at most one task, whatever happens to it later; its comments (ours included)
change nothing. A rejected block is looked at again only when the issue's title or text changed.
When the task has finished, its result goes to the issue: the pull request (whose body closes the
issue on merge), the landed commit (the issue is closed), or the failure with its gate excerpt.

Gitea is read through its API with [gitea] url and token_file, GitHub through `gh` and its login.
The state lives in <fleet.root>/intake_state.json; a poll holds a file lock, so the daemon and
`fleet intake sync` never queue the same issue twice.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from fleet.core.config import FleetConfig, Project, _expand, load_config
from fleet.plugins.base import InputPlugin, PluginKind, PluginMeta

logger = logging.getLogger("fleet.plugins.intake")

SOURCE = "issue_intake"
STATE_FILE = "intake_state.json"
MAX_NEW_PER_POLL = 10           # the rest waits for the next poll
SKIP_LABELS = frozenset({"wip", "hold", "fleet-ignore"})
# A Markdown heading that names a fleet task id: the tracking issue of that task, not a request.
_TRACKING_HEADING = re.compile(r"^#{1,6} .*`task-\d+-[0-9a-f]+`", re.MULTILINE)
_BLOCK = re.compile(r"```fleet[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)
_CHECKBOX = re.compile(r"^\s*[-*] \[[ xX]\] (.+)$", re.M)
_ENDED = ("completed", "failed", "cancelled", "skipped", "archived")
# A task whose work reached the default branch closes its issue. The grid runner calls its
# fast-forward and push "success" (a pull request is "pr_opened", a failed push "push_failed").
_LANDED = ("success", "pushed", "landed", "merged", "pr_merged")


# ── issues and trackers ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class Issue:
    provider: str                 # gitea | github
    repo: str                     # owner/name
    number: int
    title: str
    body: str
    author: str
    labels: Tuple[str, ...] = ()
    url: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.repo.lower()}#{self.number}"

    @property
    def revision(self) -> str:
        """Changes with the title or text, not with comments or labels."""
        return hashlib.sha256(f"{self.title}\0{self.body}".encode()).hexdigest()[:16]

    def meta(self) -> Dict[str, Any]:
        return {"provider": self.provider, "repo": self.repo, "number": self.number,
                "url": self.url, "author": self.author}


class TrackerError(Exception):
    """The issue tracker could not be reached or refused the request."""


class IssueTracker(ABC):
    name = "tracker"

    @abstractmethod
    def list_open(self, repo: str, label: str = "") -> List[Issue]:
        """Open issues (not pull requests), with `label` if given."""

    @abstractmethod
    def comment(self, repo: str, number: int, text: str) -> None: ...

    @abstractmethod
    def close(self, repo: str, number: int) -> None: ...


class GiteaTracker(IssueTracker):
    name = "gitea"
    PAGE = 50

    def __init__(self, url: str, token_file: str, timeout: float = 15.0):
        self.base = url.rstrip("/") + "/api/v1"
        self.token_file = token_file
        self.timeout = timeout

    def _call(self, method: str, path: str, payload: Optional[Dict] = None) -> Any:
        try:
            token = Path(_expand(self.token_file)).read_text(encoding="utf-8").strip()
        except OSError as e:
            raise TrackerError(f"Gitea token file {self.token_file}: {e.strerror}") from e
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"token {token}", "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": "sovereign-fleet-intake"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise TrackerError(f"Gitea answered {e.code} for {method} {path.split('?')[0]}") from e
        except (urllib.error.URLError, OSError) as e:
            raise TrackerError(f"Gitea not reachable: {getattr(e, 'reason', e)}") from e
        try:
            return json.loads(raw) if raw else None
        except ValueError as e:
            raise TrackerError("Gitea sent no JSON") from e

    def list_open(self, repo: str, label: str = "") -> List[Issue]:
        out: List[Issue] = []
        for page in range(1, 21):
            query = {"state": "open", "type": "issues", "limit": self.PAGE, "page": page}
            if label:
                query["labels"] = label
            items = self._call("GET", f"/repos/{repo}/issues?{urllib.parse.urlencode(query)}") or []
            for item in items:
                if item.get("pull_request"):
                    continue
                out.append(Issue("gitea", repo, int(item["number"]), item.get("title") or "",
                                 item.get("body") or "", (item.get("user") or {}).get("login", ""),
                                 tuple(l.get("name", "") for l in item.get("labels") or []),
                                 item.get("html_url", "")))
            if len(items) < self.PAGE:
                break
        return out

    def comment(self, repo: str, number: int, text: str) -> None:
        self._call("POST", f"/repos/{repo}/issues/{number}/comments", {"body": text})

    def close(self, repo: str, number: int) -> None:
        self._call("PATCH", f"/repos/{repo}/issues/{number}", {"state": "closed"})


class GitHubTracker(IssueTracker):
    """Through `gh` and the login it already has."""
    name = "github"

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout

    def _gh(self, *args: str, stdin: Optional[str] = None) -> str:
        try:
            res = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True,
                                 timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as e:
            raise TrackerError(f"gh: {e}") from e
        if res.returncode != 0:
            raise TrackerError(f"gh {args[0]} {args[1]}: {(res.stderr or res.stdout).strip()[-300:]}")
        return res.stdout

    def list_open(self, repo: str, label: str = "") -> List[Issue]:
        args = ["issue", "list", "--repo", repo, "--state", "open", "--limit", "200",
                "--json", "number,title,body,author,labels,url"]
        if label:
            args += ["--label", label]
        try:
            items = json.loads(self._gh(*args) or "[]")
        except ValueError as e:
            raise TrackerError("gh issue list sent no JSON") from e
        return [Issue("github", repo, int(i["number"]), i.get("title") or "", i.get("body") or "",
                      (i.get("author") or {}).get("login", ""),
                      tuple(l.get("name", "") for l in i.get("labels") or []), i.get("url", ""))
                for i in items]

    def comment(self, repo: str, number: int, text: str) -> None:
        self._gh("issue", "comment", str(number), "--repo", repo, "--body-file", "-", stdin=text)

    def close(self, repo: str, number: int) -> None:
        self._gh("issue", "close", str(number), "--repo", repo)


# ── the ```fleet``` block ─────────────────────────────────────────────────────

_STR_FIELDS = ("title", "summary", "category", "project", "lane", "assigned_lane",
               "complexity", "target_branch", "base_ref", "summary_de")
_LIST_FIELDS = ("allowed_paths", "acceptance_criteria", "claimed_files")


def parse_block(body: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(spec, None), (None, None) without a block, or (None, why it is rejected)."""
    m = _BLOCK.search(body or "")
    if not m:
        return None, None
    try:
        spec = json.loads(m.group(1))
    except ValueError as e:
        return None, f"the block is not valid JSON ({e})"
    if not isinstance(spec, dict):
        return None, "the block must be a JSON object"
    for name in _STR_FIELDS:
        if name in spec and not isinstance(spec[name], str):
            return None, f"'{name}' must be a string"
    for name in _LIST_FIELDS:
        if name in spec and not (isinstance(spec[name], list)
                                 and all(isinstance(x, str) and x.strip() for x in spec[name])):
            return None, f"'{name}' must be a list of non-empty strings"
    for name in ("gate", "gate_cmd"):
        gate = spec.get(name)
        if gate is None or (name == "gate_cmd" and isinstance(gate, str)):
            continue
        if not (isinstance(gate, dict) and len(gate) == 1 and next(iter(gate)) in ("pytest", "npm")
                and isinstance(next(iter(gate.values())), str)):
            return None, f"'{name}' must be {{\"pytest\": \"<path>\"}} or {{\"npm\": \"<script>\"}}"
    if isinstance(spec.get("gate_cmd"), str) and not spec.get("gate") and engine_gate(spec) is None:
        return None, "'gate_cmd' must be \"pytest <path>\" or \"npm run <script>\" (or use gate)"
    for path in spec.get("allowed_paths", []):
        if path.startswith("/") or ".." in Path(path).parts:
            return None, f"allowed_paths entry '{path}' leaves the repository"
    return spec, None


def engine_gate(spec: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """The engine's task gate from `gate`, or from a simple `gate_cmd` (pytest <path> / npm run x)."""
    for name in ("gate", "gate_cmd"):
        if isinstance(spec.get(name), dict) and spec[name]:
            return dict(spec[name])
    words = (spec.get("gate_cmd") or "").split() if isinstance(spec.get("gate_cmd"), str) else []
    if "pytest" in words and len(words) > words.index("pytest") + 1:
        rest = [w for w in words[words.index("pytest") + 1:] if not w.startswith("-")]
        if len(rest) == 1:
            return {"pytest": rest[0]}
    if words[:2] == ["npm", "run"] and len(words) == 3:
        return {"npm": words[2]}
    if words == ["npm", "test"]:
        return {"npm": "test"}
    return None


def task_text(issue: Issue) -> str:
    body = _BLOCK.sub("", issue.body or "").strip()
    return f"{issue.title}\n\n{body}\n\n(From issue {issue.repo}#{issue.number}: {issue.url})".replace("\n\n\n\n", "\n\n")


# ── comments ──────────────────────────────────────────────────────────────────

def _quote(text: str, limit: int = 1200) -> str:
    text = (text or "").strip().replace("```", "'''")
    if len(text) > limit:
        text = "...\n" + text[-limit:]
    return f"\n\n```\n{text}\n```" if text else ""


def queued_comment(task_id: str, unattended: bool, has_spec: bool, author: str) -> str:
    if unattended:
        return (f"Fleet: queued as `{task_id}`. It runs unattended: the author is trusted and the "
                "```fleet``` block sets its scope. The result will be posted here.")
    if has_spec:
        return (f"Fleet: queued as `{task_id}`, waiting for approval. `{author or 'unknown'}` is not "
                "a trusted author for this project, so a maintainer approves the task in the dashboard first.")
    return (f"Fleet: queued as `{task_id}`, waiting for approval. The issue has no ```fleet``` block, "
            "so the task has no allowed_paths and no gate; a maintainer approves it in the dashboard, "
            "or add a block.")


def result_comment(task: Any) -> Tuple[str, bool]:
    """(comment, close the issue) for a finished task of either backend."""
    r = task.result or {}
    landing = r.get("landing") or {}
    raw = r.get("status")                       # the grid runner's own result status
    lane = task.lane or r.get("lane")
    lane = f" on lane `{lane}`" if lane else ""
    if task.status == "cancelled":
        return f"Fleet: task `{task.id}` was cancelled. The issue stays open.", False
    if r.get("error") == "Rejected by user" or r.get("grid_status") == "rejected":
        return f"Fleet: task `{task.id}` was rejected in the dashboard. The issue stays open.", False
    if r.get("exit_code") == 0:
        lines = [f"Fleet: task `{task.id}` passed its gates{lane}."]
        pr = landing.get("pr_url") or r.get("pr_url")
        commit = landing.get("commit") or r.get("commit") or r.get("commit_hash") or ""
        if pr:
            lines.append(f"Pull request: {pr}")
        elif landing.get("status") in ("pushed", "landed") or raw in _LANDED:
            lines.append(f"Landed on the default branch{f' as `{commit[:10]}`' if commit else ''}.")
        elif landing.get("status") == "failed":
            lines.append(f"Landing failed: {landing.get('detail')}")
        elif r.get("branch"):
            lines.append(f"The work is on branch `{r['branch']}` for review.")
        close = landing.get("status") in ("pushed", "landed") or raw in _LANDED
        return "\n".join(lines), close
    gates = [g for g in r.get("gates") or [] if not g.get("passed")]
    excerpt = gates[-1].get("excerpt", "") if gates else ""
    tried = [t for t in r.get("tried_lanes") or [] if t]
    if len(tried) > 1:
        lane = f" on lanes {', '.join(f'`{t}`' for t in tried)}"
    text = f"Fleet: task `{task.id}` failed{lane}: {_failure_reason(r)}"
    if r.get("branch"):
        text += f"\nWhat the agent changed is on branch `{r['branch']}`."
    return text + _quote(excerpt), False


def _failure_reason(r: Dict[str, Any]) -> str:
    """Why a task failed: the engine's error, else the grid runner's autopsy or its last
    "Failed:" log line (its result is empty when no lane got through the gates)."""
    if r.get("error"):
        return str(r["error"])
    cause = ((r.get("autopsy") or {}).get("root_cause") or "").strip(" |")
    if cause:
        return cause
    for line in reversed(r.get("log_tail") or []):
        if "Failed:" in line:
            return line.split("Failed:", 1)[1].strip(" |")
    return r.get("status") or r.get("grid_status") or "no result"


def finished(task: Any) -> bool:
    if task.status in _ENDED:
        return True
    # a run that opened a pull request (or could not land) waits for a human with its result
    return task.status == "awaiting_approval" and bool(task.result) and \
        (task.result.get("exit_code") is not None or bool(task.result.get("landing")))


def fingerprint(task: Any) -> str:
    r = task.result or {}
    return f"{task.status}:{r.get('exit_code')}:{r.get('commit') or r.get('commit_hash') or ''}"


# ── state ─────────────────────────────────────────────────────────────────────

@contextmanager
def _locked(path: Path) -> Iterator[Dict[str, Any]]:
    """The state, read and written under an exclusive lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            state = read_state(path)
            yield state
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def read_state(path: Path) -> Dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("issues", {})
    state.setdefault("projects", {})
    return state


# ── the service ───────────────────────────────────────────────────────────────

class IntakeError(Exception):
    """A project's intake cannot run as configured."""


def _default_backend():
    from fleet.api.app import backend
    return backend()


class IntakeService:
    """Polls the projects whose intake is enabled, queues tasks, reports their results."""

    def __init__(self, config: Callable[[], FleetConfig] = load_config,
                 backend: Callable[[], Any] = _default_backend,
                 trackers: Optional[Dict[str, IssueTracker]] = None,
                 paused: Optional[Callable[[], List[str]]] = None,
                 clock: Callable[[], float] = time.time):
        self._config = config
        self._backend = backend
        self._trackers = trackers or {}
        self._paused = paused or (lambda: _paused_projects())
        self._clock = clock

    # configuration

    def projects(self) -> Dict[str, Project]:
        return {k: p for k, p in self._config().projects.items() if p.intake.enabled}

    def target(self, key: str, project: Project) -> Tuple[str, str]:
        """(provider, owner/name) of a project's issues."""
        from fleet.plugins.landing import _same_host, owner_repo
        cfg = self._config()
        url = ""
        if project.intake.provider == "auto" or not project.intake.repo:
            res = subprocess.run(["git", "-C", _expand(project.path), "remote", "get-url", project.remote],
                                 capture_output=True, text=True, timeout=10)
            url = res.stdout.strip() if res.returncode == 0 else ""
            if not url:
                raise IntakeError(f"project '{key}': no '{project.remote}' remote; set intake.repo and intake.provider")
        provider = project.intake.provider
        if provider == "auto":
            if "github.com" in url:
                provider = "github"
            elif cfg.gitea.url and _same_host(cfg.gitea.url, url):
                provider = "gitea"
            else:
                raise IntakeError(f"project '{key}': cannot tell the tracker from {url}; set intake.provider")
        parts = project.intake.repo.split("/") if project.intake.repo else owner_repo(url)
        if not parts:
            raise IntakeError(f"project '{key}': cannot read owner/name from {url}; set intake.repo")
        return provider, "/".join(parts)

    def tracker(self, provider: str) -> IssueTracker:
        if provider in self._trackers:
            return self._trackers[provider]
        if provider == "github":
            return GitHubTracker()
        gitea = self._config().gitea
        if not (gitea.url and gitea.token_file):
            raise IntakeError("intake from Gitea needs [gitea] url and token_file")
        return GiteaTracker(gitea.url, gitea.token_file)

    def state_path(self) -> Path:
        return self._config().root / STATE_FILE

    # the loop

    async def tick(self) -> None:
        """Poll every project that is due, then report finished tasks."""
        now = self._clock()
        state = read_state(self.state_path())
        paused = set(self._paused())
        for key, project in self.projects().items():
            last = (state["projects"].get(key) or {}).get("last_poll") or 0
            if key not in paused and now - last >= project.intake.interval_min * 60:
                try:
                    await asyncio.to_thread(self.poll, key)
                except IntakeError:
                    pass                      # recorded in the project's status and logged
        await asyncio.to_thread(self.report)

    def poll(self, key: str, dry_run: bool = False) -> List[Dict[str, Any]]:
        """Queue tasks for the project's new issues; with dry_run only say what would happen."""
        project = self._config().projects.get(key)
        if project is None:
            raise IntakeError(f"no project '{key}'")
        actions: List[Dict[str, Any]] = []
        failure: Optional[str] = None
        with _locked(self.state_path()) as state:
            status = state["projects"].setdefault(key, {})
            try:
                provider, repo = self.target(key, project)
                status.update(provider=provider, repo=repo)
                issues = self.tracker(provider).list_open(repo, project.intake.label)
            except (IntakeError, TrackerError, OSError, subprocess.SubprocessError) as e:
                failure = str(e)
                issues = []
            authors = {a.lower() for a in project.intake.authors}
            for issue in sorted(issues, key=lambda i: i.number):
                if sum(1 for a in actions if a["action"] == "queue") >= MAX_NEW_PER_POLL:
                    break
                action = self._ingest(key, project, issue, authors, state, dry_run)
                if action:
                    actions.append(action)
            if not dry_run:
                # also after a failure: the next poll waits its interval instead of hammering
                status.update(last_poll=self._clock(), last_error=failure)
        if failure:
            logger.warning("intake %s: %s", key, failure)
            raise IntakeError(failure)
        return actions

    def _ingest(self, key: str, project: Project, issue: Issue, authors: set,
                state: Dict[str, Any], dry_run: bool) -> Optional[Dict[str, Any]]:
        labels = {l.lower() for l in issue.labels}
        if project.intake.label and project.intake.label.lower() not in labels:
            return None
        if labels & SKIP_LABELS:
            return None
        if _TRACKING_HEADING.search(issue.body or ""):
            # a task's own tracking issue (the grid runner opens one per task): queuing it would
            # make the task again (2026-10-18: sovereign-ops#93 became a second task)
            return None
        record = state["issues"].get(issue.key) or {}
        if record.get("task_id") or record.get("revision") == issue.revision:
            return None                      # one task per issue; a rejected revision once
        spec, error = parse_block(issue.body)
        base = {"issue": f"{issue.repo}#{issue.number}", "author": issue.author}
        tracker = None if dry_run else self.tracker(issue.provider)
        if error:
            if not dry_run:
                state["issues"][issue.key] = {"project": key, "status": "rejected", "revision": issue.revision,
                                              "reason": error, "at": self._clock()}
                self._say(tracker, issue, f"Fleet: not queued, the ```fleet``` block was rejected: {error}\n\n"
                                          "Edit the block; the next poll looks at it again.")
            return {**base, "action": "reject", "reason": error}
        unattended = spec is not None and issue.author.lower() in authors
        if dry_run:
            return {**base, "action": "queue", "unattended": unattended, "has_spec": spec is not None}
        try:
            task = self._enqueue(key, project, issue, spec, unattended)
        except Exception as e:  # noqa: BLE001 - BackendError, a dispatcher's ValueError, ...
            reason = str(e)[:300] or type(e).__name__
            state["issues"][issue.key] = {"project": key, "status": "not_queued", "revision": issue.revision,
                                          "reason": reason, "at": self._clock()}
            self._say(tracker, issue, f"Fleet: not queued: {reason}\n\nEdit the issue; the next poll tries again.")
            return {**base, "action": "not_queued", "reason": reason}
        # recorded before the comment: a failing comment must never lead to a second task
        state["issues"][issue.key] = {"project": key, "task_id": task.id, "status": task.status,
                                      "unattended": unattended, "revision": issue.revision,
                                      "issue": issue.meta(), "at": self._clock()}
        self._say(tracker, issue, queued_comment(task.id, unattended, spec is not None, issue.author))
        logger.info("intake %s: %s#%s queued as %s (%s)", key, issue.repo, issue.number, task.id,
                    "unattended" if unattended else "awaiting approval")
        return {**base, "action": "queue", "task_id": task.id, "unattended": unattended}

    def _enqueue(self, key: str, project: Project, issue: Issue, spec: Optional[Dict[str, Any]],
                 unattended: bool) -> Any:
        backend = self._backend()
        spec = dict(spec or {})
        lane = spec.get("lane") or spec.get("assigned_lane") or None
        metadata: Dict[str, Any] = {"project": key, "issue": issue.meta(), "harness": "auto"}
        if spec:
            intent = {**spec, "project": spec.get("project") or key, "title": spec.get("title") or issue.title}
            intent.setdefault("acceptance_criteria", _CHECKBOX.findall(issue.body or "")[:8])
            if lane:
                intent["assigned_lane"] = lane
            intent.pop("lane", None)
            metadata["intent"] = intent
            # the package's own engine reads its contract from these keys
            for name in ("allowed_paths", "category", "claimed_files"):
                if spec.get(name):
                    metadata[name] = spec[name]
            gate = engine_gate(spec)
            intent.pop("gate", None)
            intent.pop("gate_cmd", None)
            if gate:
                metadata["gate"] = gate
                intent["gate_cmd"] = gate          # the grid runner's form of the same gate
        return backend.enqueue(task_text(issue), lane, [], metadata, not unattended, source=SOURCE)

    def _say(self, tracker: Optional[IssueTracker], issue: Issue, text: str) -> bool:
        if tracker is None:
            return False
        try:
            tracker.comment(issue.repo, issue.number, text)
            return True
        except TrackerError as e:
            logger.warning("intake: comment on %s#%s failed: %s", issue.repo, issue.number, e)
            return False

    def report(self) -> List[Dict[str, Any]]:
        """Post each finished task's result to its issue, once per outcome."""
        reported: List[Dict[str, Any]] = []
        backend = self._backend()
        with _locked(self.state_path()) as state:
            for key, record in state["issues"].items():
                if not record.get("task_id") or record.get("gone"):
                    continue
                task = backend.get_task(record["task_id"])
                if task is None:
                    record["gone"] = True
                    continue
                record["status"] = task.status
                if not finished(task) or record.get("reported") == fingerprint(task):
                    continue
                meta = record.get("issue") or {}
                text, close = result_comment(task)
                try:
                    tracker = self.tracker(meta.get("provider", ""))
                    tracker.comment(meta["repo"], meta["number"], text)
                    if close and not record.get("closed"):
                        tracker.close(meta["repo"], meta["number"])
                        record["closed"] = True
                except (TrackerError, IntakeError, KeyError) as e:
                    record["report_errors"] = record.get("report_errors", 0) + 1
                    logger.warning("intake: result of %s to %s: %s", task.id, key, e)
                    if record["report_errors"] < 5:
                        continue                  # tried again on the next tick
                record["reported"] = fingerprint(task)
                reported.append({"issue": key, "task_id": task.id, "closed": bool(record.get("closed"))})
        return reported

    # the dashboard's view

    def status(self) -> List[Dict[str, Any]]:
        state = read_state(self.state_path())
        paused = set(self._paused())
        out = []
        for key, project in self._config().projects.items():
            records = [r for r in state["issues"].values() if r.get("project") == key]
            polled = state["projects"].get(key) or {}
            out.append({
                "project": key, "enabled": project.intake.enabled, "paused": key in paused,
                "provider": polled.get("provider") or project.intake.provider,
                "repo": polled.get("repo") or project.intake.repo, "label": project.intake.label,
                "authors": list(project.intake.authors), "interval_min": project.intake.interval_min,
                "last_poll": polled.get("last_poll"), "last_error": polled.get("last_error"),
                "queued": sum(1 for r in records if r.get("task_id")),     # tasks so far, not waiting ones
                "awaiting_approval": sum(1 for r in records if r.get("status") == "awaiting_approval"),
                "open": sum(1 for r in records if r.get("task_id") and not r.get("reported") and not r.get("gone")),
                "rejected": sum(1 for r in records if r.get("status") in ("rejected", "not_queued")),
            })
        return out


def _paused_projects() -> List[str]:
    from fleet.core.settings import load_settings
    return list(load_settings().intake_paused)


class IssueIntakePlugin(InputPlugin):
    """The intake as an input plugin: `poll_or_listen` runs the service's loop."""

    def __init__(self, service: Optional[IntakeService] = None, tick_seconds: float = 30.0):
        self.service = service or IntakeService()
        self.tick_seconds = tick_seconds
        self._running = False

    @property
    def meta(self) -> PluginMeta:
        return PluginMeta(name="issue-intake", kind=PluginKind.INPUT, version="1.0",
                          description="Tasks from Gitea and GitHub issues, results back to the issue")

    async def poll_or_listen(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.service.tick()
            except Exception:  # noqa: BLE001 - the next tick may succeed
                logger.exception("issue intake tick failed")
            await asyncio.sleep(self.tick_seconds)

    async def teardown(self) -> None:
        self._running = False
