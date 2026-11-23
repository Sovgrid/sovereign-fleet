"""goose update from the app: every check must hold before the binary is replaced."""

import base64
import hashlib
import io
import json
import tarfile

import httpx
import pytest

from fleet.core import harness_update as H

TAG = "v1.0.1"


def _script(version: str) -> bytes:
    return f"#!/bin/sh\necho ' {version}'\n".encode()


def _archive(content: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        info = tarfile.TarInfo("goose")
        info.size, info.mode = len(content), 0o755
        tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _attestation(digest: str, repo=f"https://github.com/{H.REPO}", ref=f"refs/tags/{TAG}", path=H.WORKFLOW):
    stmt = {"predicateType": "https://slsa.dev/provenance/v1",
            "subject": [{"name": "goose.tar.bz2", "digest": {"sha256": digest}}],
            "predicate": {"buildDefinition": {"externalParameters": {"workflow": {"repository": repo, "ref": ref, "path": path}}}}}
    return {"attestations": [{"bundle": {"dsseEnvelope": {"payload": base64.b64encode(json.dumps(stmt).encode()).decode()}}}]}


def _client(archive: bytes, *, listed_digest=None, attestation=None):
    digest = hashlib.sha256(archive).hexdigest()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": TAG, "published_at": "2026-11-20T06:18:55Z", "assets": [
                {"name": H._asset_name(), "browser_download_url": "https://github.com/dl/goose.tar.bz2",
                 "digest": "sha256:" + (listed_digest or digest)}]})
        if "/attestations/" in req.url.path:
            return httpx.Response(200, json=attestation if attestation is not None else _attestation(digest))
        if req.url.path == "/dl/goose.tar.bz2":
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def goose(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    b = tmp_path / "bin" / "goose"
    b.write_bytes(_script("1.0.0"))
    b.chmod(0o755)
    monkeypatch.setattr(H.shutil, "which", lambda name: str(b) if name == "goose" else None)
    return b


def test_update_installs_the_checked_release_and_keeps_the_old_one(goose):
    res = H.update(busy=False, client=_client(_archive(_script("1.0.1"))))
    assert res["updated_from"] == "1.0.0" and res["installed"] == "1.0.1"
    assert H._version_of(goose) == "1.0.1"
    assert H._version_of(goose.with_name("goose.prev")) == "1.0.0"


def test_status_offers_the_newer_release(goose):
    st = H.status(client=_client(_archive(_script("1.0.1"))))
    assert st["installed"] == "1.0.0" and st["newest"] == "1.0.1" and st["update_available"]


@pytest.mark.parametrize("bad", [
    {"listed_digest": "0" * 64},                                              # download differs from GitHub's digest
    {"attestation": {"attestations": []}},                                   # no provenance at all
    {"attestation": "other-repo"},
    {"attestation": "other-tag"},
    {"attestation": "other-workflow"},
])
def test_any_failed_check_leaves_goose_alone(goose, bad):
    archive = _archive(_script("1.0.1"))
    digest = hashlib.sha256(archive).hexdigest()
    named = {"other-repo": _attestation(digest, repo="https://github.com/evil/goose"),
             "other-tag": _attestation(digest, ref="refs/tags/v9.9.9"),
             "other-workflow": _attestation(digest, path=".github/workflows/nightly.yml")}
    att = bad.get("attestation")
    att = named[att] if isinstance(att, str) else att
    with pytest.raises(H.HarnessUpdateError):
        H.update(busy=False, client=_client(archive, listed_digest=bad.get("listed_digest"), attestation=att))
    assert H._version_of(goose) == "1.0.0" and not goose.with_name("goose.prev").exists()


def test_a_binary_that_reports_another_version_is_refused(goose):
    with pytest.raises(H.HarnessUpdateError, match="does not report version"):
        H.update(busy=False, client=_client(_archive(_script("0.9.0"))))
    assert H._version_of(goose) == "1.0.0"
    assert [p.name for p in goose.parent.iterdir()] == ["goose"]   # no leftover temp binary


def test_never_while_a_task_runs(goose):
    with pytest.raises(H.HarnessUpdateError, match="task is running"):
        H.update(busy=True, client=_client(_archive(_script("1.0.1"))))


def test_the_update_routes_are_admin_only():
    from fleet.api import scopes
    assert scopes.required_scope("GET", "/api/fleet/harness/goose") == "admin"
    assert scopes.required_scope("POST", "/api/fleet/harness/goose/update") == "admin"


def test_update_refuses_if_a_task_starts_during_download_and_returns_updating_false(goose, monkeypatch):
    calls = {"n": 0}

    def becomes_busy():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(H.HarnessUpdateError, match="task is running"):
        H.update(busy=False, is_busy=becomes_busy, client=_client(_archive(_script("1.0.1"))))
    assert H._version_of(goose) == "1.0.0" and not goose.with_name("goose.prev").exists()
    assert [p.name for p in goose.parent.iterdir()] == ["goose"]

    res = H.update(busy=False, is_busy=lambda: False, client=_client(_archive(_script("1.0.1"))))
    assert res["updating"] is False


def test_update_refuses_non_https_download_and_wraps_oserror(goose, monkeypatch):
    def http_dl(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json={"tag_name": TAG, "published_at": "2026-11-20T06:18:55Z", "assets": [
                {"name": H._asset_name(), "browser_download_url": "http://insecure.example/goose.tar.bz2",
                 "digest": "sha256:" + ("a" * 64)}]})
        return httpx.Response(404)

    with pytest.raises(H.HarnessUpdateError, match="HTTPS"):
        H.update(busy=False, client=httpx.Client(transport=httpx.MockTransport(http_dl)))

    def fail_replace(*a, **kw):
        raise PermissionError("read-only bin")

    monkeypatch.setattr(H.os, "replace", fail_replace)
    with pytest.raises(H.HarnessUpdateError, match="cannot install"):
        H.update(busy=False, client=_client(_archive(_script("1.0.1"))))
    assert H._version_of(goose) == "1.0.0"
