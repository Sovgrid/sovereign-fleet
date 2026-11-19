"""Task templates as files (core/templates.py), the API route and the MCP prompts."""

import os
from types import SimpleNamespace

import pytest

from fleet.core import templates as T

GOOD = "---\nname: Fix a bug\ndescription: d\nblank.where: Where | src/x\n---\nFix {where}.\n"


def _cfg(tmp_path, project_path=None):
    projects = {"p": SimpleNamespace(path=str(project_path), aliases=[])} if project_path else {}
    return SimpleNamespace(root=str(tmp_path / "root"), projects=projects)


def test_builtins_are_the_five_starts():
    ids = {t["id"] for t in T.load_templates(SimpleNamespace(root=None, projects={}))}
    assert ids == {"fix-bug", "refactor", "add-tests", "update-docs", "bump-deps"}


def test_parse_and_fill():
    t = T.parse_template("fix-bug", GOOD)
    assert t["name"] == "Fix a bug" and t["blanks"] == [{"id": "where", "label": "Where", "placeholder": "src/x"}]
    assert T.fill(t, {"where": "src/y"}) == "Fix src/y." and T.fill(t, {}) == "Fix src/x."


@pytest.mark.parametrize("raw,why", [
    ("name: x\n---\nbody", "no front matter"),
    ("---\nname: x\nbody", "not closed"),
    ("---\ndescription: d\n---\nbody", "no name"),
    ("---\nname: x\n---\n", "body is empty"),
    ("---\nname: x\ncolor: red\n---\nb", "unknown key"),
    ("---\nname: x\n---\nuses {nope}", "without a blank"),
    ("---\nname: x\nblank.Bad: L | p\n---\nb", "blank id"),
])
def test_bad_files_are_refused_with_a_reason(raw, why):
    with pytest.raises(T.TemplateError, match=why):
        T.parse_template("x", raw)


def test_project_overrides_global_overrides_builtin(tmp_path):
    g = tmp_path / "root" / "templates"; g.mkdir(parents=True)
    (g / "fix-bug.md").write_text(GOOD.replace("Fix a bug", "Global fix"))
    proj = tmp_path / "proj"; (proj / ".fleet" / "templates").mkdir(parents=True)
    (proj / ".fleet" / "templates" / "fix-bug.md").write_text(GOOD.replace("Fix a bug", "Project fix"))
    by = {t["id"]: t for t in T.load_templates(_cfg(tmp_path, proj), "p")}
    assert by["fix-bug"]["name"] == "Project fix" and by["fix-bug"]["scope"] == "project"
    assert {t["id"]: t for t in T.load_templates(_cfg(tmp_path))}["fix-bug"]["scope"] == "global"


def test_unsafe_files_are_skipped(tmp_path):
    g = tmp_path / "root" / "templates"; g.mkdir(parents=True)
    outside = tmp_path / "outside.md"; outside.write_text(GOOD)
    os.symlink(outside, g / "linked.md")
    (g / "big.md").write_text(GOOD + "x" * (T.MAX_BYTES + 1))
    (g / "Bad_Name.md").write_text(GOOD)
    (g / "broken.md").write_text("no front matter")
    (g / "ok.md").write_text(GOOD)
    ids = {t["id"] for t in T.load_templates(_cfg(tmp_path)) if t["scope"] == "global"}
    assert ids == {"ok"}


def test_unknown_project_is_refused(tmp_path):
    with pytest.raises(T.TemplateError):
        T.load_templates(_cfg(tmp_path), "nope")


def test_route_is_read_scope():
    from fleet.api import scopes
    assert scopes.required_scope("GET", "/api/fleet/templates") == "read"


def test_mcp_prompts_list_and_get(monkeypatch):
    from fleet.api import mcp_server as M
    tpl = {**T.parse_template("fix-bug", GOOD), "scope": "built-in"}
    monkeypatch.setattr(M, "_templates", lambda project=None: [tpl])
    for mode in ("full", "front_door"):
        init = M.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, mode=mode)
        assert "prompts" in init["result"]["capabilities"]
        lst = M.handle_request({"jsonrpc": "2.0", "id": 2, "method": "prompts/list"}, mode=mode)
        assert lst["result"]["prompts"][0]["name"] == "fix-bug"
        assert lst["result"]["prompts"][0]["arguments"][0]["name"] == "where"
        got = M.handle_request({"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                                "params": {"name": "fix-bug", "arguments": {"where": "src/z"}}}, mode=mode)
        assert got["result"]["messages"][0]["content"]["text"] == "Fix src/z."
    miss = M.handle_request({"jsonrpc": "2.0", "id": 4, "method": "prompts/get", "params": {"name": "nope"}}, mode="full")
    assert miss["error"]["code"] == -32602
