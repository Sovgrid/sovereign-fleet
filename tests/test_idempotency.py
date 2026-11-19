"""Idempotent dispatch: a retried request finds the task its first attempt queued."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

import fleet.api.app as app_module
from fleet.api.app import app
from fleet.core import idempotency
from fleet.core.idempotency import IdempotencyError, IdempotencyStore, fingerprint

AUTH = {"Authorization": "Bearer test-token"}


def test_new_then_replay_then_mismatch(tmp_path):
    store = IdempotencyStore(tmp_path / "keys.json")
    assert store.begin("k", "fp1").kind == "new"
    store.commit("k", "task-1")
    d = store.begin("k", "fp1")
    assert (d.kind, d.task_id) == ("replay", "task-1")
    with pytest.raises(IdempotencyError) as e:
        store.begin("k", "fp2")
    assert e.value.status == 422 and e.value.task_id == "task-1"


def test_a_key_in_flight_is_409_and_released_keys_are_free(tmp_path):
    store = IdempotencyStore(tmp_path / "keys.json")
    store.begin("k", "fp")
    with pytest.raises(IdempotencyError) as e:
        store.begin("k", "fp")
    assert e.value.status == 409 and "still being processed" in str(e.value)
    store.release("k")
    assert store.begin("k", "fp").kind == "new"


def test_a_reservation_of_a_dead_daemon_reports_interrupted(tmp_path):
    """Write-ahead: the task may or may not exist, so the caller looks instead of a guess."""
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"version": 1, "keys": {
        "k": {"fp": "fp", "task_id": None, "owner": "o1s1", "at": 9e9}}}))
    with pytest.raises(IdempotencyError) as e:
        IdempotencyStore(path).begin("k", "fp", now=9e9)
    assert e.value.status == 409 and "interrupted" in str(e.value)


def test_keys_expire_after_the_window(tmp_path):
    store = IdempotencyStore(tmp_path / "keys.json", window=100)
    store.begin("k", "fp", now=1000)
    store.commit("k", "task-1")
    assert store.begin("k", "other", now=1101).kind == "new"


def test_a_bound_task_that_no_longer_counts_frees_the_key(tmp_path):
    store = IdempotencyStore(tmp_path / "keys.json")
    store.begin("auto:x", "fp")
    store.commit("auto:x", "task-1")
    assert store.begin("auto:x", "fp", still_valid=lambda tid, k: False).kind == "new"


@pytest.mark.parametrize("bad", ["", "a b", "x" * 129, "ä", "k/1"])
def test_bad_keys_are_refused(bad):
    with pytest.raises(IdempotencyError) as e:
        idempotency.validate_key(bad)
    assert e.value.status == 400


def test_fingerprint_is_order_independent():
    assert fingerprint({"a": 1, "b": [1, 2]}) == fingerprint({"b": [1, 2], "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


def _count(tasks, text):
    return sum(1 for t in tasks if t["text"] == text)


@pytest.mark.asyncio
async def test_api_replays_the_first_task_for_the_same_key():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        body = {"text": "idem once", "idempotency_key": "req-1"}
        first = await client.post("/api/fleet/tasks", json=body)
        again = await client.post("/api/fleet/tasks", json=body)
        assert first.status_code == again.status_code == 200
        assert again.json()["id"] == first.json()["id"]
        assert again.headers.get("idempotent-replayed") == "true"
        assert "idempotent-replayed" not in first.headers
        # the header form is the same key
        hdr = await client.post("/api/fleet/tasks", json={"text": "idem once"},
                                headers={"Idempotency-Key": '"req-1"'})
        assert hdr.json()["id"] == first.json()["id"]
        assert _count((await client.get("/api/fleet/tasks")).json(), "idem once") == 1


@pytest.mark.asyncio
async def test_api_refuses_a_reused_key_with_another_request():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        await client.post("/api/fleet/tasks", json={"text": "idem a", "idempotency_key": "req-2"})
        other = await client.post("/api/fleet/tasks", json={"text": "idem b", "idempotency_key": "req-2"})
        assert other.status_code == 422 and "different request" in other.json()["detail"]
        clash = await client.post("/api/fleet/tasks", json={"text": "idem a", "idempotency_key": "req-2"},
                                  headers={"Idempotency-Key": "req-3"})
        assert clash.status_code == 400
        bad = await client.post("/api/fleet/tasks", json={"text": "idem a", "idempotency_key": "a b"})
        assert bad.status_code == 400
        assert _count((await client.get("/api/fleet/tasks")).json(), "idem b") == 0


@pytest.mark.asyncio
async def test_api_without_a_key_queues_every_time():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        for _ in range(2):
            await client.post("/api/fleet/tasks", json={"text": "idem none"})
        assert _count((await client.get("/api/fleet/tasks")).json(), "idem none") == 2


@pytest.mark.asyncio
async def test_an_auto_key_dedups_only_while_the_task_is_open(monkeypatch):
    from fleet.core.settings import FleetSettings
    monkeypatch.setattr(app_module, "_current_settings", FleetSettings(autonomy_level="manual_gate"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        body = {"text": "idem auto", "idempotency_key": "auto:abc", "metadata": {"require_approval": True}}
        first = (await client.post("/api/fleet/tasks", json=body)).json()
        assert first["status"] == "awaiting_approval"
        assert (await client.post("/api/fleet/tasks", json=body)).json()["id"] == first["id"]
        await client.post(f"/api/fleet/tasks/{first['id']}/reject")
        fresh = (await client.post("/api/fleet/tasks", json=body)).json()
        assert fresh["id"] != first["id"]          # asking again after a rejection is a new order


@pytest.mark.asyncio
async def test_a_failed_enqueue_frees_the_key(monkeypatch):
    from fleet.core.backends import BackendError
    real = app_module.backend

    class Refusing:
        def enqueue(self, *a, **k):
            raise BackendError("lane inactive")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        monkeypatch.setattr(app_module, "backend", lambda: Refusing())
        bad = await client.post("/api/fleet/tasks", json={"text": "idem dep", "idempotency_key": "req-4"})
        assert bad.status_code == 400
        monkeypatch.setattr(app_module, "backend", real)
        ok = await client.post("/api/fleet/tasks", json={"text": "idem dep", "idempotency_key": "req-4"})
        assert ok.status_code == 200 and "idempotent-replayed" not in ok.headers
