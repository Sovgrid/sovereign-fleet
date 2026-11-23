"""Update the goose CLI from the app.

goose's own `goose update` panics in 1.60.x while it verifies the download ("no sigstore-crypto
CryptoProvider installed"), so the app could not offer an update. This installs the newest stable
release from GitHub: the release asset's sha256 must match the digest GitHub lists for that asset,
the SLSA provenance payload GitHub lists must match that digest and goose's release workflow at that
tag (digest and provenance both come from GitHub's API; no local Sigstore/Rekor signature check
is performed), and the new binary must report that version before it replaces the old one.
The old binary stays next to it as `goose.prev`. Refused if a task is running before or right
before swap.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

REPO = "aaif-goose/goose"
WORKFLOW = ".github/workflows/release.yml"
API = "https://api.github.com"
MAX_ARCHIVE_BYTES = 400 * 1024 * 1024
MAX_BINARY_BYTES = 600 * 1024 * 1024
_LOCK = threading.Lock()


class HarnessUpdateError(Exception):
    pass


def _semver(text: str) -> Optional[Tuple[int, int, int]]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _asset_name() -> str:
    arch = {"aarch64": "aarch64", "arm64": "aarch64", "x86_64": "x86_64", "amd64": "x86_64"}.get(platform.machine().lower())
    if not arch or platform.system() != "Linux":
        raise HarnessUpdateError(f"no goose release for {platform.system()} {platform.machine()}")
    return f"goose-{arch}-unknown-linux-gnu.tar.bz2"


def _binary() -> Optional[Path]:
    found = shutil.which("goose")
    return Path(found).resolve() if found else None


def _version_of(binary: Path) -> Optional[str]:
    try:
        out = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    v = _semver(out.stdout + out.stderr)
    return ".".join(map(str, v)) if v else None


def _client(client: Optional[httpx.Client], timeout: float):
    """The caller's client as is (tests, nested calls), else a fresh one closed after use."""
    return nullcontext(client) if client is not None else httpx.Client(timeout=timeout, follow_redirects=True)


def _get(client: httpx.Client, url: str) -> Any:
    r = client.get(url, headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        raise HarnessUpdateError(f"GitHub answered {r.status_code} for {url.replace(API, '')}")
    return r.json()


def _latest(client: httpx.Client) -> Dict[str, Any]:
    rel = _get(client, f"{API}/repos/{REPO}/releases/latest")
    name = _asset_name()
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == name), None)
    if asset is None:
        raise HarnessUpdateError(f"release {rel.get('tag_name')} has no {name}")
    digest = str(asset.get("digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise HarnessUpdateError("GitHub lists no sha256 digest for the release asset")
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith("https://"):
        raise HarnessUpdateError("release asset URL must use HTTPS")
    return {"tag": str(rel.get("tag_name") or ""), "published_at": str(rel.get("published_at") or ""),
            "url": url, "digest": digest[7:]}


def check_provenance(attestations: Dict[str, Any], digest_hex: str, tag: str) -> None:
    """Raise unless one SLSA provenance names this digest and goose's release workflow at `tag`."""
    for att in attestations.get("attestations") or []:
        try:
            stmt = json.loads(base64.b64decode(att["bundle"]["dsseEnvelope"]["payload"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not str(stmt.get("predicateType", "")).startswith("https://slsa.dev/provenance/"):
            continue
        wf = (((stmt.get("predicate") or {}).get("buildDefinition") or {}).get("externalParameters") or {}).get("workflow") or {}
        digests = {((s or {}).get("digest") or {}).get("sha256") for s in stmt.get("subject") or []}
        if (digest_hex in digests and wf.get("repository") == f"https://github.com/{REPO}"
                and wf.get("ref") == f"refs/tags/{tag}" and wf.get("path") == WORKFLOW):
            return
    raise HarnessUpdateError("no build attestation from goose's release workflow for this download")


def _extract_goose(archive: Path, dest_dir: Path) -> Path:
    with tarfile.open(archive, mode="r:bz2") as tar:
        member = next((m for m in tar.getmembers() if m.name.lstrip("./") == "goose"), None)
        if member is None or not member.isreg() or member.size > MAX_BINARY_BYTES:
            raise HarnessUpdateError("the archive holds no goose binary")
        src = tar.extractfile(member)  # a regular member: reads exactly member.size bytes, bounded above
        fd, tmp = tempfile.mkstemp(prefix=".goose-new-", dir=dest_dir)
        try:
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(tmp, 0o755)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return Path(tmp)


def status(busy: bool = False, client: Optional[httpx.Client] = None) -> Dict[str, Any]:
    binary = _binary()
    installed = _version_of(binary) if binary else None
    res: Dict[str, Any] = {"harness": "goose", "installed": installed, "newest": None, "newest_age_days": None,
                           "update_available": False, "busy": busy, "updating": _LOCK.locked(), "error": None}
    try:
        with _client(client, 20) as c:
            latest = _latest(c)
    except (HarnessUpdateError, httpx.HTTPError, ValueError) as e:
        res["error"] = str(e) if isinstance(e, HarnessUpdateError) else "GitHub not reachable"
        return res
    res["newest"] = latest["tag"].lstrip("v")
    try:
        published = datetime.fromisoformat(latest["published_at"].replace("Z", "+00:00")).timestamp()
        res["newest_age_days"] = round((time.time() - published) / 86400, 1)
    except ValueError:
        pass
    new, cur = _semver(res["newest"]), _semver(installed or "")
    res["update_available"] = bool(binary and new and (cur is None or new > cur))
    return res


def _check_busy(busy: bool, is_busy: Optional[Callable[[], bool]]) -> None:
    if busy or (is_busy is not None and is_busy()):
        raise HarnessUpdateError("a task is running; update goose when the queue is idle")


def update(busy: bool = False, client: Optional[httpx.Client] = None,
           *, is_busy: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """Install the newest stable goose; returns the new status with `updated_from`."""
    _check_busy(busy, is_busy)
    binary = _binary()
    if binary is None:
        raise HarnessUpdateError("goose is not installed on this host")
    if not _LOCK.acquire(blocking=False):
        raise HarnessUpdateError("an update is already running")
    updated_from: Optional[str] = None
    try:
        before = _version_of(binary)
        with _client(client, 60) as c:
            latest = _latest(c)
            want = latest["tag"].lstrip("v")
            if _semver(before or "") and _semver(want) <= _semver(before or ""):
                updated_from = None
            else:
                with tempfile.TemporaryDirectory(prefix="goose-update-") as tmpdir:
                    archive = Path(tmpdir) / "goose.tar.bz2"
                    h, size = hashlib.sha256(), 0
                    with c.stream("GET", latest["url"]) as r, open(archive, "wb") as f:
                        if r.status_code != 200:
                            raise HarnessUpdateError(f"download answered {r.status_code}")
                        for chunk in r.iter_bytes():
                            size += len(chunk)
                            if size > MAX_ARCHIVE_BYTES:
                                raise HarnessUpdateError("download larger than expected")
                            h.update(chunk)
                            f.write(chunk)
                    if h.hexdigest() != latest["digest"]:
                        raise HarnessUpdateError("download does not match the digest GitHub lists")
                    check_provenance(_get(c, f"{API}/repos/{REPO}/attestations/sha256:{latest['digest']}"),
                                     latest["digest"], latest["tag"])
                    try:
                        new_bin = _extract_goose(archive, binary.parent)
                    except OSError as e:
                        raise HarnessUpdateError(f"cannot install goose: {e}") from e
                    try:
                        if _version_of(new_bin) != want:
                            raise HarnessUpdateError(f"the new binary does not report version {want}")
                        _check_busy(busy, is_busy)
                        try:
                            shutil.copy2(binary, binary.with_name(binary.name + ".prev"))
                            os.replace(new_bin, binary)
                        except OSError as e:
                            raise HarnessUpdateError(f"cannot install goose: {e}") from e
                    finally:
                        if new_bin.exists():
                            new_bin.unlink()
                updated_from = before
    except httpx.HTTPError:
        raise HarnessUpdateError("GitHub not reachable") from None
    finally:
        _LOCK.release()
    return {**status(busy=False, client=client), "updated_from": updated_from}
