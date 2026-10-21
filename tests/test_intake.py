"""Issue intake: trust, one task per issue, rejected blocks, results back to the issue, trackers."""

import http.server
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.api import app as app_module
from fleet.api.app import app
from fleet.core.backends import BackendError, LocalBackend
from fleet.core.config import load_config
from fleet.plugins import intake
from fleet.plugins.intake import (
    GiteaTracker,
    GitHubTracker,
    IntakeError,
    IntakeService,
    Issue,
    IssueTracker,
    TrackerError,
    engine_gate,
    parse_block,
)

from conftest import require_local_tcp

AUTH = {"Authorization": "Bearer test-token"}
OWNER = "alice"
BLOCK = '```fleet\n{"category": "bugfix", "allowed_paths": ["calc.py", "tests/**"], "gate_cmd": "pytest tests/test_calc.py"}\n```'


class FakeTracker(IssueTracker):
    """Issues in memory; comments and closes are recorded (and change nothing, as in reality
    they only bump updated_at, which the intake ignores)."""
    name = "fake"

    def __init__(self, provider="gitea"):
        self.provider = provider
        self.issues = {}
        self.comments = []
        self.closed = []
        self.fail_comments = False

    def add(self, number, body="", author=OWNER, labels=("fleet",), title=None, repo="me/app"):
        self.issues[number] = Issue(self.provider, repo, number, title or f"issue {number}", body, author,
                                    tuple(labels), f"http://tracker/{repo}/issues/{number}")
        return self.issues[number]

    def list_open(self, repo, label=""):
        return [i for i in self.issues.values() if i.repo == repo and i.number not in self.closed]

    def comment(self, repo, number, text):
        if self.fail_comments:
            raise TrackerError("tracker down")
        self.comments.append((number, text))

    def close(self, repo, number):
        self.closed.append(number)

    def said(self, number):
        return [t for n, t in self.comments if n == number]


def write_config(intake_toml='authors = ["alice"]', landing="branch", extra=""):
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)
    repo = path.parent / "repo"
    repo.mkdir(exist_ok=True)
    path.write_text(f"""
[projects.app]
path = "{repo}"
landing = "{landing}"
[projects.app.intake]
enabled = true
provider = "gitea"
repo = "me/app"
{intake_toml}
{extra}
""", encoding="utf-8")


@pytest.fixture
def setup(isolate_fleet):
    write_config()
    tracker = FakeTracker()
    store = isolate_fleet
    service = IntakeService(backend=lambda: LocalBackend(store), trackers={"gitea": tracker}, paused=lambda: [])
    return service, tracker, store


# ── trust ─────────────────────────────────────────────────────────────────────

def test_a_trusted_author_with_a_block_queues_an_unattended_task(setup):
    service, tracker, store = setup
    tracker.add(1, f"Fix add.\n\n- [ ] add(2, 3) is 5\n\n{BLOCK}")
    [action] = service.poll("app")
    task = store.get_task(action["task_id"])
    assert task.status == "queued" and action["unattended"]
    meta = task.metadata
    assert meta["source"] == "issue_intake" and meta["project"] == "app"
    assert meta["allowed_paths"] == ["calc.py", "tests/**"] and meta["category"] == "bugfix"
    assert meta["gate"] == {"pytest": "tests/test_calc.py"}
    assert meta["issue"] == {"provider": "gitea", "repo": "me/app", "number": 1,
                             "url": "http://tracker/me/app/issues/1", "author": OWNER}
    assert meta["intent"]["acceptance_criteria"] == ["add(2, 3) is 5"]
    assert "```fleet" not in task.text and task.text.startswith("issue 1\n\nFix add.")
    assert "runs unattended" in tracker.said(1)[0] and task.id in tracker.said(1)[0]


@pytest.mark.parametrize("body,author,authors,why", [
    ("Please fix add.", OWNER, '["alice"]', "no ```fleet``` block"),
    (BLOCK, "mallory", '["alice"]', "not a trusted author"),
    (BLOCK, OWNER, "[]", "not a trusted author"),           # no authors: nothing unattended
])
def test_everything_else_waits_for_an_approval(setup, body, author, authors, why):
    service, tracker, store = setup
    write_config(f"authors = {authors}")
    tracker.add(1, body, author=author)
    [action] = service.poll("app")
    assert store.get_task(action["task_id"]).status == "awaiting_approval"
    assert why in tracker.said(1)[0]


def test_authors_compare_case_insensitively(setup):
    service, tracker, store = setup
    tracker.add(1, BLOCK, author="Alice")
    assert service.poll("app")[0]["unattended"]


def test_a_tasks_own_tracking_issue_is_not_a_new_request(setup):
    service, tracker, store = setup
    tracker.add(1, "[local_qwen] Fix\n\n### Sovereign Voice-to-Agent Task: `task-1792316714-8d24`\n\nSummary")
    tracker.add(2, "task-1792316714-8d24 failed on the gate, please fix the test")      # a request about a task
    actions = service.poll("app")
    assert [a["issue"] for a in actions] == ["me/app#2"] and tracker.said(1) == []


# ── one task per issue ────────────────────────────────────────────────────────

def test_our_own_comments_never_queue_the_issue_again(setup):
    """The loop agy's version had: each comment bumped updated_at, each poll queued again."""
    service, tracker, store = setup
    tracker.add(1, BLOCK)
    tracker.add(2, "no block")
    for _ in range(4):
        service.poll("app")
    assert len(store.load_tasks()) == 2
    assert len(tracker.said(1)) == 1 and len(tracker.said(2)) == 1


def test_an_edited_issue_keeps_its_one_task(setup):
    service, tracker, store = setup
    tracker.add(1, "first version")
    service.poll("app")
    tracker.add(1, f"second version\n{BLOCK}")
    assert service.poll("app") == []
    assert len(store.load_tasks()) == 1


def test_a_rejected_block_is_answered_once_per_revision(setup):
    service, tracker, store = setup
    tracker.add(1, '```fleet\n{"allowed_paths": "calc.py"\n```')
    [action] = service.poll("app")
    assert action["action"] == "reject" and "not valid JSON" in action["reason"]
    service.poll("app")
    assert len(tracker.said(1)) == 1 and store.load_tasks() == []
    tracker.add(1, BLOCK)                                   # fixed by the author
    [action] = service.poll("app")
    assert action["task_id"] and len(tracker.said(1)) == 2


def test_a_backend_refusal_is_said_once_and_retried_after_an_edit(setup):
    service, tracker, _ = setup

    class Refusing:
        def enqueue(self, *a, **k):
            raise BackendError("rejected by the dispatcher: unknown lane x")

    service._backend = lambda: Refusing()
    tracker.add(1, BLOCK)
    assert service.poll("app")[0]["action"] == "not_queued"
    service.poll("app")
    assert tracker.said(1) == ["Fleet: not queued: rejected by the dispatcher: unknown lane x\n\n"
                               "Edit the issue; the next poll tries again."]


def test_a_failing_comment_never_leads_to_a_second_task(setup):
    service, tracker, store = setup
    tracker.fail_comments = True
    tracker.add(1, BLOCK)
    service.poll("app")
    service.poll("app")
    assert len(store.load_tasks()) == 1


def test_two_pollers_at_once_queue_each_issue_once(setup):
    service, tracker, store = setup
    for n in range(1, 6):
        tracker.add(n, BLOCK)
    threads = [threading.Thread(target=service.poll, args=("app",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.load_tasks()) == 5


def test_labels_filter_and_skip(setup):
    service, tracker, store = setup
    tracker.add(1, BLOCK, labels=())
    tracker.add(2, BLOCK, labels=("Fleet",))
    tracker.add(3, BLOCK, labels=("fleet", "hold"))
    assert [a["issue"] for a in service.poll("app")] == ["me/app#2"]


def test_a_poll_queues_at_most_ten_new_tasks(setup):
    service, tracker, store = setup
    for n in range(1, 14):
        tracker.add(n, "x")
    assert len(service.poll("app")) == intake.MAX_NEW_PER_POLL
    assert len(service.poll("app")) == 3


def test_a_dry_run_changes_nothing(setup):
    service, tracker, store = setup
    tracker.add(1, BLOCK)
    tracker.add(2, '```fleet\n[1]\n```')
    actions = service.poll("app", dry_run=True)
    assert [a["action"] for a in actions] == ["queue", "reject"]
    assert store.load_tasks() == [] and tracker.comments == []
    assert len(service.poll("app")) == 2


# ── results back to the issue ─────────────────────────────────────────────────

def _finish(store, task_id, status, result):
    store.update_task_status(task_id, "in_progress")
    store.update_task_status(task_id, status, result)


def test_a_pull_request_is_linked_and_the_issue_stays_open(setup):
    service, tracker, store = setup
    tracker.add(1, BLOCK)
    tid = service.poll("app")[0]["task_id"]
    assert service.report() == []                            # still queued
    _finish(store, tid, "awaiting_approval", {"exit_code": 0, "branch": f"fleet/{tid}",
                                               "landing": {"status": "pr_opened", "pr_url": "http://pr/9"}})
    service.report()
    service.report()
    [msg] = tracker.said(1)[1:]
    assert "passed its gates" in msg and "Pull request: http://pr/9" in msg
    assert tracker.closed == []


def test_landed_work_closes_the_issue(setup):
    service, tracker, store = setup
    tracker.add(1, BLOCK)
    tid = service.poll("app")[0]["task_id"]
    _finish(store, tid, "completed", {"exit_code": 0, "landing": {"status": "pushed", "commit": "abcdef1234567"}})
    service.report()
    assert "Landed on the default branch as `abcdef1234`." in tracker.said(1)[-1]
    assert tracker.closed == [1]


def test_a_failure_shows_the_gate_excerpt_and_a_retry_reports_again(setup):
    service, tracker, store = setup
    tracker.add(1, BLOCK)
    tid = service.poll("app")[0]["task_id"]
    _finish(store, tid, "failed", {"exit_code": 5, "error": "Task gate exit 1: assert -1 == 5",
                                   "gates": [{"name": "task", "passed": False, "excerpt": "E assert -1 == 5"}]})
    service.report()
    assert "failed: Task gate exit 1" in tracker.said(1)[-1] and "```\nE assert -1 == 5\n```" in tracker.said(1)[-1]
    store.transition(tid, "queued", allowed_from=("failed",))
    _finish(store, tid, "completed", {"exit_code": 0, "commit": "1234567890ab", "branch": f"fleet/{tid}"})
    service.report()
    assert "passed its gates" in tracker.said(1)[-1] and len(tracker.said(1)) == 3


def test_a_report_that_cannot_be_posted_is_tried_again(setup):
    service, tracker, store = setup
    tracker.add(1, BLOCK)
    tid = service.poll("app")[0]["task_id"]
    _finish(store, tid, "cancelled", {"exit_code": 130})
    tracker.fail_comments = True
    assert service.report() == []
    tracker.fail_comments = False
    assert len(service.report()) == 1 and "was cancelled" in tracker.said(1)[-1]


# ── grid backend ──────────────────────────────────────────────────────────────

def test_the_grid_gets_a_coordinator_intent_and_the_intake_source(setup):
    service, tracker, _ = setup
    calls = []

    class Grid:
        def enqueue(self, text, lane, depends_on, metadata, needs_approval, source="sovereign_fleet"):
            calls.append((lane, metadata, needs_approval, source))

            class T:
                id, status = "grid-1", "pending"
            return T()

    service._backend = lambda: Grid()
    tracker.add(1, '```fleet\n{"assigned_lane": "local_qwen", "gate_cmd": "uv run pytest -q tests/x.py"}\n```')
    # the grid runner's own block format: gate_cmd as an object
    tracker.add(2, '```fleet\n{"assigned_lane": "local_qwen", "gate_cmd": {"pytest": "tests"}}\n```')
    service.poll("app")
    lane, meta, needs_approval, source = calls[0]
    assert (lane, needs_approval, source) == ("local_qwen", False, "issue_intake")
    assert meta["intent"]["project"] == "app" and meta["intent"]["title"] == "issue 1"
    # the runner validates gate_cmd as {"pytest": path} | {"npm": script}: never a string
    assert meta["intent"]["gate_cmd"] == {"pytest": "tests/x.py"} and meta["gate"] == {"pytest": "tests/x.py"}
    assert calls[1][1]["intent"]["gate_cmd"] == {"pytest": "tests"} and calls[1][1]["gate"] == {"pytest": "tests"}


# ── the block ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("block,error", [
    ('{"allowed_paths": ["../etc/passwd"]}', "leaves the repository"),
    ('{"allowed_paths": ["/etc"]}', "leaves the repository"),
    ('{"gate": {"shell": "rm -rf /"}}', "'gate' must be"),
    ('{"category": 3}', "'category' must be a string"),
    ('{"gate_cmd": "make test"}', "'gate_cmd' must be"),
    ('{"gate_cmd": {"sh": "make"}}', "'gate_cmd' must be"),
    ('[]', "must be a JSON object"),
])
def test_bad_blocks_are_rejected(block, error):
    spec, why = parse_block(f"```fleet\n{block}\n```")
    assert spec is None and error in why


def test_gates_from_the_block():
    assert engine_gate({"gate": {"npm": "check"}}) == {"npm": "check"}
    assert engine_gate({"gate_cmd": "python3 -m pytest -q tests/a.py"}) == {"pytest": "tests/a.py"}
    assert engine_gate({"gate_cmd": "npm run check"}) == {"npm": "check"}
    assert engine_gate({"gate_cmd": "make test"}) is None
    assert engine_gate({"gate_cmd": "pytest tests/a.py tests/b.py"}) is None
    assert engine_gate({"gate_cmd": {"pytest": "tests"}}) == {"pytest": "tests"}


# ── trackers ──────────────────────────────────────────────────────────────────

@pytest.fixture
def gitea(tmp_path):
    """A Gitea API with two pages of issues (one a pull request)."""
    seen = []
    token = tmp_path / "gitea_token"
    token.write_text("gitea-secret-token\n")

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, status, data):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            if "page=1" in self.path:
                items = [{"number": n, "title": f"t{n}", "body": "b", "user": {"login": "alice"},
                          "labels": [{"name": "fleet"}], "html_url": f"http://g/me/app/issues/{n}"}
                         for n in range(1, 51)]
                items[3]["pull_request"] = {"merged": False}
                return self._send(200, items)
            return self._send(200, [{"number": 51, "title": "t51", "body": None, "user": {"login": "bob"},
                                     "labels": [], "html_url": "http://g/me/app/issues/51"}])

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(("POST", self.path, body))
            self._send(201 if "comments" in self.path else 403, {})

        def do_PATCH(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(("PATCH", self.path, body))
            self._send(201, {})

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        require_local_tcp(server.server_address[1])
    except BaseException:
        server.shutdown()
        raise
    yield f"http://127.0.0.1:{server.server_address[1]}", str(token), seen
    server.shutdown()


def test_gitea_tracker_pages_skips_pull_requests_comments_and_closes(gitea):
    url, token, seen = gitea
    tracker = GiteaTracker(url, token)
    issues = tracker.list_open("me/app", "fleet")
    assert len(issues) == 50 and 4 not in [i.number for i in issues]
    assert issues[-1].author == "bob" and issues[-1].body == ""
    first = seen[0][1]
    assert "type=issues" in first and "labels=fleet" in first and "state=open" in first
    assert seen[0][2] == "token gitea-secret-token"
    tracker.comment("me/app", 7, "hello")
    tracker.close("me/app", 7)
    assert seen[-2] == ("POST", "/api/v1/repos/me/app/issues/7/comments", {"body": "hello"})
    assert seen[-1] == ("PATCH", "/api/v1/repos/me/app/issues/7", {"state": "closed"})


def test_gitea_errors_never_carry_the_token(gitea, tmp_path):
    url, token, _ = gitea
    with pytest.raises(TrackerError) as e:
        GiteaTracker(url, token)._call("POST", "/repos/me/app/labels", {})
    assert "403" in str(e.value) and "gitea-secret-token" not in str(e.value)
    with pytest.raises(TrackerError):
        GiteaTracker("http://127.0.0.1:9", token).list_open("me/app")
    with pytest.raises(TrackerError, match="token file"):
        GiteaTracker(url, str(tmp_path / "missing")).list_open("me/app")


def test_github_tracker_uses_gh(monkeypatch):
    calls = []

    def fake_run(argv, input=None, capture_output=True, text=True, timeout=None):
        calls.append((argv, input))
        out = json.dumps([{"number": 5, "title": "t", "body": "b", "author": {"login": "Alice"},
                           "labels": [{"name": "fleet"}], "url": "https://github.com/o/r/issues/5"}])
        return subprocess.CompletedProcess(argv, 0, out if argv[1:3] == ["issue", "list"] else "", "")

    monkeypatch.setattr(intake.subprocess, "run", fake_run)
    tracker = GitHubTracker()
    [issue] = tracker.list_open("o/r", "fleet")
    assert (issue.provider, issue.number, issue.author, issue.labels) == ("github", 5, "Alice", ("fleet",))
    assert calls[0][0][:6] == ["gh", "issue", "list", "--repo", "o/r", "--state"]
    assert calls[0][0][-2:] == ["--label", "fleet"]
    tracker.comment("o/r", 5, "a long `comment`")
    assert calls[1] == (["gh", "issue", "comment", "5", "--repo", "o/r", "--body-file", "-"], "a long `comment`")
    monkeypatch.setattr(intake.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "HTTP 404: Not Found"))
    with pytest.raises(TrackerError, match="404"):
        tracker.close("o/r", 5)


def test_the_tracker_follows_the_projects_remote(isolate_fleet, tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    path = Path(os.environ["FLEET_CONFIG"])
    path.parent.mkdir(parents=True, exist_ok=True)

    def target(remote, gitea_url="http://gitea.local:3000"):
        subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], capture_output=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], check=True)
        path.write_text(f'[gitea]\nurl = "{gitea_url}"\ntoken_file = "/x"\n'
                        f'[projects.p]\npath = "{repo}"\n[projects.p.intake]\nenabled = true\n')
        service = IntakeService()
        return service.target("p", load_config().projects["p"])

    assert target("git@github.com:Org/App.git") == ("github", "Org/App")
    assert target("http://gitea.local:3000/me/app.git") == ("gitea", "me/app")
    with pytest.raises(IntakeError, match="cannot tell the tracker"):
        target("https://example.org/me/app.git")


# ── the loop and the API ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tick_polls_when_due_and_not_paused(setup):
    service, tracker, store = setup
    now = [1_000_000.0]
    paused = []
    service._clock = lambda: now[0]
    service._paused = lambda: paused
    tracker.add(1, BLOCK)
    await service.tick()
    tracker.add(2, BLOCK)
    now[0] += 60
    await service.tick()                                   # interval 5 min: not due
    assert len(store.load_tasks()) == 1
    paused.append("app")
    now[0] += 600
    await service.tick()
    assert len(store.load_tasks()) == 1
    paused.clear()
    await service.tick()
    assert len(store.load_tasks()) == 2


@pytest.mark.asyncio
async def test_a_tracker_failure_is_shown_and_does_not_stop_the_loop(setup):
    service, tracker, _ = setup

    def down(repo, label=""):
        raise TrackerError("Gitea not reachable: refused")

    tracker.list_open = down
    await service.tick()
    [status] = service.status()
    assert status["last_error"] == "Gitea not reachable: refused" and status["last_poll"]


@pytest.mark.asyncio
async def test_intake_api(setup, monkeypatch):
    _, tracker, store = setup
    monkeypatch.setattr(app_module, "intake_service",
                        lambda: IntakeService(backend=app_module.backend, trackers={"gitea": tracker},
                                              paused=lambda: list(app_module._current_settings.intake_paused)))
    tracker.add(1, BLOCK)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        [project] = (await client.get("/api/fleet/intake")).json()["projects"]
        assert project["enabled"] and not project["paused"] and project["queued"] == 0
        dry = (await client.post("/api/fleet/intake/app/sync", json={"dry_run": True})).json()
        assert dry["actions"][0]["action"] == "queue" and store.load_tasks() == []
        res = (await client.post("/api/fleet/intake/app/sync", json={})).json()
        assert res["projects"][0]["queued"] == 1 and len(store.load_tasks()) == 1
        paused = (await client.post("/api/fleet/intake/app/pause", json={"paused": True})).json()
        assert paused["projects"][0]["paused"]
        # saving the settings form (which knows nothing of the intake) keeps the pause
        await client.post("/api/fleet/settings", json={"task_timeout_seconds": 900})
        assert (await client.get("/api/fleet/settings")).json()["intake_paused"] == ["app"]
        assert (await client.post("/api/fleet/intake/nope/sync", json={})).status_code == 404


def test_a_grid_failure_names_its_cause_and_the_lanes_tried():
    class T:
        id, status, lane = "grid-9", "failed", "c"
        result = {"grid_status": "failed", "tried_lanes": ["a", "b", "c"],
                  "autopsy": {"root_cause": "Gate exit 4: ImportError while loading conftest. | "},
                  "log_tail": ["[16:13] Failed: Gate exit 4: something"]}
    text, close = intake.result_comment(T())
    assert text == ("Fleet: task `grid-9` failed on lanes `a`, `b`, `c`: "
                    "Gate exit 4: ImportError while loading conftest.") and not close
    T.result = {"grid_status": "failed", "log_tail": ["x", "[16:13] Failed: Gate exit 1: tests | "]}
    assert intake.result_comment(T())[0].endswith("failed on lane `c`: Gate exit 1: tests")


def test_a_grid_task_that_landed_closes_its_issue():
    class T:
        id, status, lane = "grid-7", "completed", "local_qwen"
        result = {"status": "success", "lane": "local_qwen", "commit_hash": "0d307f6",
                  "grid_status": "completed", "exit_code": 0}
    text, close = intake.result_comment(T())
    assert close and "Landed on the default branch as `0d307f6`" in text
    T.result = {"status": "pr_opened", "pr_url": "http://x/pr/1", "exit_code": 0, "grid_status": "awaiting_approval"}
    text, close = intake.result_comment(T())
    assert not close and "Pull request: http://x/pr/1" in text
