"""Engine-level tests for evidence gate verification, receipt facts, and could_not rotation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fleet.core.config import RoutingSection
from fleet.core.toolgate import CallLog
from test_router import _engine, _run, lane


def _seed_logs(
    root: Path,
    tid: str,
    *,
    body_text: str,
    is_error: bool = False,
    egress_allowed: bool = True,
    host: str = "berg.example",
) -> None:
    call_log_path = root / "toolcalls" / f"{tid}.fetch.jsonl"
    bodies_path = call_log_path.with_suffix(".bodies.jsonl")
    shown_sha = hashlib.sha256(body_text.encode("utf-8")).hexdigest()

    log = CallLog(call_log_path)
    log.write(event="start")
    log.write(
        tool="fetch_url",
        decision="allowed",
        is_error=is_error,
        result_bytes=len(body_text),
        shown_sha256=shown_sha,
    )
    log.write(event="end")
    log.close()

    bodies_path.write_text(
        json.dumps({"shown_sha256": shown_sha, "text": body_text, "is_error": is_error, "cut": False}) + "\n",
        encoding="utf-8",
    )
    egress_path = root / "egress" / f"{tid}.fetch.jsonl"
    egress_path.parent.mkdir(parents=True, exist_ok=True)
    egress_path.write_text(
        json.dumps({"ts": 1794747200.0, "method": "CONNECT", "host": host, "port": 443, "allowed": egress_allowed})
        + "\n",
        encoding="utf-8",
    )


def _attach_json_harness(engine, answer_map: dict[str, str]):
    async def fake_exec(task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        return {"exit_code": 0, "stdout": answer_map[model], "stderr": ""}

    engine.harnesses["opencode"].execute_task = fake_exec


def test_engine_evidence_gate_passes_and_persists_receipt_facts(tmp_path: Path):
    lanes = {"a": lane(model="m1")}
    engine, _, _ = _engine(
        tmp_path,
        lanes,
        {"m1": None},
        routing=RoutingSection(max_lanes=2, repair=False),
    )
    ev_root = tmp_path / "evroot"
    _seed_logs(ev_root, "t1", body_text="Saisonkarte 1.003,00 EUR / Anlagen: 26")
    _attach_json_harness(engine, {"m1": json.dumps({"open_lifts": 26, "price": 1003.0})})

    res, task = _run(
        engine,
        metadata={
            "category": "errand",
            "repo_gate": False,
            "evidence_root": str(ev_root),
            "evidence": {
                "required_keys": ["open_lifts", "price"],
                "sources": ["berg.example"],
                "required_tools": ["fetch_url"],
            },
        },
    )
    assert task.status == "completed", res
    assert res["facts"]["verified_values_count"] == 2
    assert any(g["name"] == "evidence" and g["exit_code"] == 0 for g in res["gates"])

    receipt = json.loads((engine.task_store.spool_dir / "receipts" / "t1.json").read_text())
    assert receipt["facts"]["verified_values_count"] == 2
    assert any(g["name"] == "evidence" and g["exit_code"] == 0 for g in receipt["gates"])


def test_engine_could_not_with_external_fault_completes_without_router_record(tmp_path: Path):
    lanes = {"a": lane(model="m1"), "b": lane(model="m2")}
    engine, _, _ = _engine(
        tmp_path,
        lanes,
        {"m1": None, "m2": None},
        routing=RoutingSection(max_lanes=2, repair=True),
    )
    ev_root = tmp_path / "evroot"
    _seed_logs(ev_root, "t1", body_text="HTTP 503 Service Unavailable", is_error=True)
    _attach_json_harness(engine, {"m1": json.dumps({"error": "upstream returned HTTP 503"}), "m2": "{}"})

    recorded = []
    engine.router.record = lambda att: recorded.append(att.lane)

    res, task = _run(
        engine,
        metadata={
            "category": "errand",
            "repo_gate": False,
            "evidence_root": str(ev_root),
            "evidence": {
                "required_keys": ["open_lifts"],
                "sources": ["berg.example"],
                "required_tools": ["fetch_url"],
            },
        },
    )
    assert task.status == "completed", res
    assert res["lane"] == "a"
    assert res["could_not"] is True
    assert res["external_fault"] is True
    assert recorded == []


def test_engine_could_not_without_external_fault_rotates_once_to_second_lane(tmp_path: Path):
    lanes = {"a": lane(model="m1"), "b": lane(model="m2")}
    engine, _, _ = _engine(
        tmp_path,
        lanes,
        {"m1": None, "m2": None},
        routing=RoutingSection(max_lanes=2, repair=True),
    )
    ev_root = tmp_path / "evroot"
    _seed_logs(ev_root, "t1", body_text="Normal HTML page without expected number", is_error=False)
    _attach_json_harness(
        engine,
        {
            "m1": json.dumps({"error": "could not find figure on page"}),
            "m2": json.dumps({"error": "confirmed: figure absent from page"}),
        },
    )

    recorded = []
    orig_record = engine.router.record

    def capture_record(att):
        recorded.append((att.lane, att.passed))
        return orig_record(att)

    engine.router.record = capture_record

    res, task = _run(
        engine,
        metadata={
            "category": "errand",
            "repo_gate": False,
            "evidence_root": str(ev_root),
            "evidence": {
                "required_keys": ["open_lifts"],
                "sources": ["berg.example"],
                "required_tools": ["fetch_url"],
            },
        },
    )
    assert task.status == "completed", res
    assert [a["lane"] for a in res["attempts"]] == ["a", "b"]
    assert res["attempts"][0]["skipped"] is True and res["attempts"][0]["passed"] is False
    assert res["attempts"][1]["passed"] is True
    assert recorded == [("b", True)]



def test_g6_could_not_empty_worktree_lifecycle_keeps_receipt_skips_review_and_renders_dashboard(tmp_path: Path):
    """G6: could_not with empty git worktree goes cleanly through _keep_work, skips review loop, writes receipt outcome='could_not'."""
    import asyncio
    import subprocess
    from fleet.core.taskstore import FleetTask

    repo = tmp_path / "git-repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@test.local"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    engine, _, _ = _engine(
        repo,
        {"a": lane(model="m1"), "b": lane(model="m2")},
        {"m1": None, "m2": None},
        routing=RoutingSection(max_lanes=2, repair=True),
    )
    store = engine.task_store
    ev_root = tmp_path / "evroot"
    _seed_logs(ev_root, "t-couldnot", body_text="HTTP 503 Service Unavailable", is_error=True)
    _attach_json_harness(engine, {"m1": json.dumps({"error": "source returned 503"}), "m2": json.dumps({"error": "503"})})

    task = FleetTask(
        id="t-couldnot",
        text="Check status without touching files",
        metadata={
            "category": "feature",
            "review": True,
            "repo_gate": False,
            "evidence_root": str(ev_root),
            "evidence": {
                "required_keys": ["open_lifts"],
                "sources": ["berg.example"],
                "required_tools": ["fetch_url"],
            },
        },
    )
    store.enqueue(task)
    res = asyncio.run(engine.run_single_task(task))
    assert res["could_not"] is True
    assert res["external_fault"] is True
    assert res.get("changed_files") == []
    assert "keep_error" not in res
    assert "review_task_id" not in res

    stored = store.get_task("t-couldnot")
    assert stored is not None and stored.status == "completed"
    assert len(store.load_tasks()) == 1

    receipt = json.loads((store.spool_dir / "receipts" / "t-couldnot.json").read_text())
    assert receipt["could_not"] is True
    assert receipt["outcome"] == "could_not"
    assert receipt["status"] == "completed"


def test_g7_prior_could_not_kept_in_engine_run_state_not_task_metadata(tmp_path, monkeypatch):
    """G7: _prior_could_not lives in FleetEngine per-run memory, never persisted into task.metadata."""
    import asyncio
    import json
    import subprocess

    from fleet.core.config import LaneSpec, RoutingSection
    from fleet.core.engine import FleetEngine
    from fleet.core.taskstore import FleetTask, TaskStore
    from fleet.core.worktree import WorktreeManager
    from fleet.plugins.harness_base import OpenCodeHarness

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.org"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "a.txt").write_text("1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"escalate_after": 1, "escalate_to": []}), encoding="utf-8")
    monkeypatch.setenv("FLEET_SETTINGS_FILE", str(settings_file))
    monkeypatch.setenv("FLEET_SANDBOX", "0")

    root = tmp_path / "evidence-root"
    store = TaskStore(spool_dir=str(tmp_path / "spool"))
    lanes = {
        "l1": LaneSpec(cost="free", steps=[{"harness": "opencode", "model": "m1"}]),
        "l2": LaneSpec(cost="free", steps=[{"harness": "opencode", "model": "m2"}]),
    }
    engine = FleetEngine(
        repo_path=str(repo),
        task_store=store,
        worktree_mgr=WorktreeManager(repo_path=str(repo), worktrees_base_dir=str(tmp_path / "wts")),
        repo_gate=False,
        lanes=lanes,
        routing=RoutingSection(),
    )

    mode = {"run": 1, "calls": 0}
    oc = OpenCodeHarness()
    oc.is_available = lambda: True

    async def fake_exec(task, worktree_dir, timeout=600, model="", args=(), pass_env=(), env=None):
        mode["calls"] += 1
        egress_path = root / "egress" / f"{task['id']}.fetch.jsonl"
        egress_path.parent.mkdir(parents=True, exist_ok=True)
        egress_path.write_text(
            json.dumps({"ts": 1.0, "method": "CONNECT", "host": "example.com", "port": 443, "allowed": True, "status": 200}) + "\n",
            encoding="utf-8",
        )
        if mode["run"] == 2 and mode["calls"] >= 2:
            return {"exit_code": 0, "stdout": json.dumps({"status": "ok"}), "stderr": ""}
        return {"exit_code": 0, "stdout": json.dumps({"error": "source page returned empty table"}), "stderr": ""}

    oc.execute_task = fake_exec
    engine.register_harness(oc)

    task = FleetTask(
        id="t-g7",
        text="check source",
        metadata={
            "harness": "auto",
            "category": "bugfix",
            "evidence_root": str(root),
            "evidence": {"required_keys": ["status"], "sources": ["example.com"]},
        },
    )
    store.enqueue(task)
    res1 = asyncio.run(engine.run_single_task(task))
    assert mode["calls"] == 2
    assert res1["receipt"]["outcome"] == "could_not"
    assert "_prior_could_not" not in task.metadata
    assert "_prior_could_not" not in store.get_task("t-g7").metadata

    # Simulate task retry on a fresh run: first could_not must rotate to l2, not accept immediately.
    mode["run"] = 2
    mode["calls"] = 0
    store.update_task_status("t-g7", "queued")
    res2 = asyncio.run(engine.run_single_task(store.get_task("t-g7")))
    assert mode["calls"] == 2
    assert res2["receipt"]["status"] == "completed"
    assert not res2["receipt"].get("could_not")
    assert "_prior_could_not" not in store.get_task("t-g7").metadata
