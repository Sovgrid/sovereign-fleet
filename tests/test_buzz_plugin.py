"""Contract tests for the Sovereign Fleet Buzz plugin."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "integrations" / "buzz-plugin"


def test_buzz_plugin_manifest_contract():
    manifest = json.loads((PLUGIN_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) <= {"id", "name", "apiVersion", "description", "updates", "discovery", "host"}
    assert re.fullmatch(r"[a-z][a-z0-9.-]{0,79}", manifest["id"])
    assert manifest["apiVersion"] == 1
    assert 1 <= len(manifest["name"].encode("utf-8")) <= 80

    commands = manifest["host"]["commands"]
    assert 1 <= len(commands) <= 16
    for cmd in commands:
        assert re.fullmatch(r"[A-Za-z0-9._-]{1,80}", cmd["program"])
        assert len(cmd["args"]) <= 16
        assert all(len(a.encode("utf-8")) <= 1024 for a in cmd["args"])
        if "maxInputBytes" in cmd:
            assert 0 < cmd["maxInputBytes"] <= 65536
        if "maxOutputBytes" in cmd:
            assert 0 < cmd["maxOutputBytes"] <= 1048576


def test_buzz_plugin_js_smoke():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    script = """
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
const mod = await import(pathToFileURL(process.argv[1]).href);
assert.deepEqual(mod.inject, ["agents", "host"]);
const tools = new Map();
const seen = [];
mod.apply({
  agents: { addTool: (t) => tools.set(t.name, t) },
  host: {
    async runCommand(id, input) {
      seen.push({ id, input });
      if (id === "submit") return JSON.stringify({ id: "task-1", status: "awaiting_approval", lane: null });
      if (id === "show") return JSON.stringify({ id: "task-1", status: "awaiting_approval", text: "Hello", created_at: 1794575000 });
      if (id === "list") return JSON.stringify([{ id: "task-1", status: "awaiting_approval", text: "Hello", created_at: 1794575000 }]);
      if (id === "lanes") return JSON.stringify({ lanes: { local_qwen: { status: "ok" } } });
      return null;
    },
  },
});
assert.deepEqual([...tools.keys()], ["fleet_submit", "fleet_task", "fleet_tasks", "fleet_lanes"]);
assert.ok(!tools.has("fleet_approve") && !tools.has("fleet_reject"));
assert.match(await tools.get("fleet_submit").call({}, {}, { text: "Hello", project: "demo" }), /awaiting_approval/);
assert.match(await tools.get("fleet_task").call({}, {}, { task_id: "task-1" }), /task-1/);
assert.match(await tools.get("fleet_tasks").call({}, {}, {}), /1 open/);
assert.match(await tools.get("fleet_lanes").call({}, {}, {}), /local_qwen: OK/);
assert.equal(seen[0].id, "submit");
assert.equal(JSON.parse(seen[0].input).source, "buzz_plugin");
assert.equal(seen[1].id, "show");
assert.equal(seen[1].input, "task-1");
"""
    res = subprocess.run([node, "--input-type=module", "-e", script, str(PLUGIN_DIR / "plugin.js")],
                         capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
