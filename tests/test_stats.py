"""Track record counting rules (core/stats.py), shared by the app and the measurement script."""

from fleet.core.stats import track_record
from fleet.core.taskstore import FleetTask

NOW = 1_800_000_000


def _t(i, status, *, lane="local_qwen", dur=120, gate=True, error=None, ago=3600):
    meta = {"gate": {"pytest": "tests/x.py"}} if gate else {}
    result = {"lane": lane, "receipt": {"duration_seconds": dur, "lane": lane}}
    if error:
        result["error"] = error
    return FleetTask(id=f"t{i}", text="x", status=status, created_at=NOW - ago, metadata=meta, result=result)


def test_counts_only_tasks_that_ran_and_splits_by_test_and_lane():
    tasks = [
        _t(1, "completed"), _t(2, "completed", dur=900), _t(3, "failed"),
        _t(4, "completed", gate=False), _t(5, "completed", lane="gemini_studio_flash"),
        _t(6, "failed", error="Rejected by user"), _t(7, "cancelled"), _t(8, "awaiting_approval"),
        _t(9, "completed", ago=40 * 86400),                       # outside the window
    ]
    r = track_record(tasks, since=NOW - 14 * 86400, limit_s=600)
    assert r["all"]["ran"] == 5 and r["all"]["passed"] == 4
    assert r["gated"] == {**r["gated"], "ran": 4, "passed": 3, "passed_in_limit": 2}
    assert r["gated_local"]["ran"] == 3 and r["gated_local"]["passed_in_limit"] == 1
    lanes = {x["lane"]: x for x in r["by_lane"]}
    assert lanes["local_qwen"]["ran"] == 4 and lanes["local_qwen"]["local"]
    assert lanes["gemini_studio_flash"]["ran"] == 1 and not lanes["gemini_studio_flash"]["local"]


def test_empty_window_has_no_rates():
    r = track_record([], since=NOW)
    assert r["all"] == {"ran": 0, "passed": 0, "passed_in_limit": 0, "pass_rate": None, "in_limit_rate": None, "median_min": None, "p90_min": None,
                        "not_started": 0, "causes": {"infrastructure": 0, "agent": 0}}


def test_the_api_serves_it_with_read_scope(monkeypatch):
    import asyncio
    import time
    from httpx import ASGITransport, AsyncClient
    import fleet.api.app as app_module
    from fleet.api import scopes

    class _Backend:
        def list_tasks(self):
            now = time.time()
            return [FleetTask(id="a", text="x", status="completed", created_at=now,
                              metadata={"gate": {"pytest": "t.py"}},
                              result={"lane": "local_qwen", "receipt": {"duration_seconds": 60}}),
                    FleetTask(id="b", text="x", status="failed", created_at=now - 100 * 86400,   # the 90 days before
                              metadata={"gate": {"pytest": "t.py"}},
                              result={"lane": "local_qwen", "receipt": {"duration_seconds": 60, "finished_at": now - 100 * 86400}})]

    monkeypatch.setattr(app_module, "backend", lambda: _Backend())
    assert scopes.required_scope("GET", "/api/fleet/stats") == "read"

    async def get():
        async with AsyncClient(transport=ASGITransport(app=app_module.app), base_url="http://test",
                               headers={"Authorization": "Bearer test-token"}) as c:
            return await c.get("/api/fleet/stats?days=500")

    r = asyncio.run(get())
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 90 and body["gated"]["passed_in_limit"] == 1 and body["by_lane"][0]["lane"] == "local_qwen"
    assert body["gated"]["ran"] == 1                                    # the old task is not in this window
    assert body["previous"]["gated"]["ran"] == 1 and body["previous"]["gated"]["passed"] == 0   # it is in the one before


def test_excludes_synthetic_and_rejected_uses_grid_runtime_failed_attempt_lane_and_finish_ts():
    from fleet.core.backends import GridBackend

    g = GridBackend.__new__(GridBackend)
    derived = g._to_task({
        "id": "task-grid-1", "status": "completed", "created_at": NOW - 20 * 86400, "started_at": NOW - 300,
        "source": "cli", "intent": {"project": "p", "assigned_lane": "local_qwen", "gate_cmd": {"pytest": "t.py"}},
        "result": {"status": "success", "lane": "local_qwen", "completed_at": NOW - 120},
    })
    assert derived.result["runtime_s"] == 180.0

    tasks = [
        derived,  # created 20d ago, finished NOW-120 -> counts in 14d window AND <=600s!
        FleetTask(id="bench-1", text="x", status="completed", created_at=NOW - 60,
                  metadata={"source": "fleet_bench", "project": "fleetbench"},
                  result={"lane": "local_qwen"}),
        FleetTask(id="grid-rej", text="x", status="failed", created_at=NOW - 60,
                  metadata={"gate": {"pytest": "t.py"}}, result={"grid_status": "rejected"}),
        FleetTask(id="lq-fail-att", text="x", status="failed", created_at=NOW - 60,
                  metadata={"gate": {"pytest": "t.py"}},
                  result={"attempts": [{"lane": "antigravity_sonnet", "skipped": True},
                                       {"lane": "local_qwen", "passed": False, "duration_seconds": 400}]}),
    ]
    r = track_record(tasks, since=NOW - 14 * 86400, limit_s=600)
    assert r["all"]["ran"] == 2 and r["all"]["passed"] == 1
    assert r["gated"]["passed_in_limit"] == 1 and r["gated_local"]["ran"] == 2
    assert r["all"]["median_min"] == 3.0
    lanes = {x["lane"]: x for x in r["by_lane"]}
    assert "unknown" not in lanes and lanes["local_qwen"]["ran"] == 2 and lanes["local_qwen"]["passed"] == 1


def test_archived_tasks_keep_their_outcome_and_obsolete_ones_skip():
    tasks = [
        FleetTask(id="loc-done-arch", text="x", status="archived", created_at=NOW - 60,
                  metadata={"archived_from": "completed", "gate": {"pytest": "t.py"}},
                  result={"lane": "local_qwen", "receipt": {"duration_seconds": 120}}),
        FleetTask(id="loc-fail-arch", text="x", status="archived", created_at=NOW - 60,
                  metadata={"archived_from": "failed", "gate": {"pytest": "t.py"}},
                  result={"lane": "local_qwen", "error": "Gate exit 1", "receipt": {"duration_seconds": 120}}),
        FleetTask(id="grid-fail-arch", text="x", status="archived", created_at=NOW - 60,
                  metadata={"gate": {"pytest": "t.py"}},
                  result={"grid_status": "archived", "status": "merge_failed", "exit_code": 1, "lane": "local_qwen", "runtime_s": 90}),
        FleetTask(id="grid-refused-arch", text="x", status="archived", created_at=NOW - 60,
                  metadata={"gate": {"pytest": "t.py"}},
                  result={"grid_status": "archived", "status": "nothing_to_land", "exit_code": 1, "lane": "local_qwen"}),
        FleetTask(id="grid-unknown-arch", text="x", status="archived", created_at=NOW - 60,
                  metadata={"gate": {"pytest": "t.py"}}, result={"grid_status": "archived", "lane": "local_qwen"}),
        FleetTask(id="loc-obs-space", text="x", status="failed", created_at=NOW - 60,
                  metadata={"gate": {"pytest": "t.py"}},
                  result={"lane": "local_qwen", "error": "obsolete ngitprobe release check"}),
    ]
    r = track_record(tasks, since=NOW - 86400, limit_s=600)
    assert r["all"]["ran"] == 4 and r["all"]["passed"] == 1   # no outcome known: not counted
    assert r["gated"]["causes"] == {"infrastructure": 0, "agent": 3}
