#!/usr/bin/env python3
"""Root-run upgrade worker for Pi Pocket (stdlib-only, runs under `python3 -I`).

Selects only stable `vX.Y.Z` tags at least `delay_days` (default 3) old, backs up the Pi
Pocket state directory, installs npm dependencies with `--ignore-scripts`, aborts and rolls
back if any new package install script appears or if the post-upgrade health check (`/` -> 200,
`/api/status` without cookie -> 401) fails, and writes a status JSON file readable by the
Fleet daemon.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("pi_pocket_upgrade")

STABLE_TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
INSTALL_HOOKS = ("preinstall", "install", "postinstall", "prepare")
SAFE_GIT = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "advice.detachedHead=false",
)

CommandRunner = Callable[..., Tuple[int, str, str]]
HealthChecker = Callable[[str], Tuple[bool, str]]


def parse_semver(value: str) -> Optional[Tuple[int, int, int]]:
    """Parse strict X.Y.Z or vX.Y.Z without pre-release or build metadata."""
    m = SEMVER_RE.match((value or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def read_package_version(repo_dir: Path) -> Optional[str]:
    """Read `version` from `package.json` inside `repo_dir`, or return None."""
    pkg_file = repo_dir / "package.json"
    if not pkg_file.is_file():
        return None
    try:
        data = json.loads(pkg_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ver = data.get("version")
    return str(ver).strip() if isinstance(ver, str) and ver.strip() else None


def select_target_tag(
    tags: Sequence[Tuple[str, int, str]],
    *,
    current_version: Optional[str],
    now: float,
    delay_days: int = 3,
) -> Dict[str, Any]:
    """Choose the newest stable tag at least `delay_days` old and newer than `current_version`."""
    min_age_s = max(0, int(delay_days)) * 86400   # 0: "Upgrade now", the operator's own choice
    stable: List[Tuple[Tuple[int, int, int], int, str, str]] = []
    for tag, created_ts, commit in tags:
        m = STABLE_TAG_RE.match((tag or "").strip())
        if not m or created_ts <= 0 or created_ts > now or not commit:
            continue
        ver_tuple = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        stable.append((ver_tuple, int(created_ts), tag.strip(), commit.strip()))

    if not stable:
        return {
            "newest_version": None,
            "newest_tag": None,
            "newest_age_days": None,
            "eligible_version": None,
            "eligible_tag": None,
            "target": None,
        }

    stable.sort(key=lambda item: item[0], reverse=True)
    top_tuple, top_ts, top_tag, _ = stable[0]
    newest_ver = ".".join(str(x) for x in top_tuple)
    newest_age_days = round(max(0.0, (now - top_ts) / 86400.0), 2)

    eligible_item: Optional[Tuple[Tuple[int, int, int], int, str, str]] = None
    for item in stable:
        if now - item[1] >= min_age_s:
            eligible_item = item
            break

    cur_tuple = parse_semver(current_version or "")
    target: Optional[Dict[str, Any]] = None
    eligible_ver: Optional[str] = None
    eligible_tag: Optional[str] = None
    if eligible_item is not None:
        elig_tuple, elig_ts, eligible_tag, elig_commit = eligible_item
        eligible_ver = ".".join(str(x) for x in elig_tuple)
        if cur_tuple is None or elig_tuple > cur_tuple:
            target = {
                "tag": eligible_tag,
                "version": eligible_ver,
                "commit": elig_commit,
                "created_at": elig_ts,
            }

    return {
        "newest_version": newest_ver,
        "newest_tag": top_tag,
        "newest_age_days": newest_age_days,
        "eligible_version": eligible_ver,
        "eligible_tag": eligible_tag,
        "target": target,
    }


def scan_install_scripts(repo_dir: Path) -> Dict[str, Dict[str, str]]:
    """Collect packages with `hasInstallScript` in lockfile or install lifecycle hooks on disk."""
    found: Dict[str, Dict[str, str]] = {}
    lock_file = repo_dir / "package-lock.json"
    if lock_file.is_file():
        try:
            lock = json.loads(lock_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            lock = {}
        packages = lock.get("packages")
        if isinstance(packages, Mapping):
            for key, meta in packages.items():
                if key and isinstance(meta, Mapping) and meta.get("hasInstallScript") is True:
                    found.setdefault(str(key), {})["hasInstallScript"] = "true"

    nm_root = repo_dir / "node_modules"
    if nm_root.is_dir() and not nm_root.is_symlink():
        for root, dirs, files in os.walk(nm_root, followlinks=False):
            dirs[:] = [
                d for d in sorted(dirs)
                if d != ".git" and not (Path(root) / d).is_symlink()
            ]
            if "package.json" not in files:
                continue
            pkg_json = Path(root) / "package.json"
            if pkg_json.is_symlink():
                continue
            try:
                meta = json.loads(pkg_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            scripts = meta.get("scripts")
            if not isinstance(scripts, Mapping):
                continue
            hooks = {
                k: str(v) for k, v in scripts.items()
                if k in INSTALL_HOOKS and isinstance(v, str) and v.strip()
            }
            if hooks:
                rel = str(Path(root).relative_to(repo_dir))
                found.setdefault(rel, {}).update(hooks)
    return found


def detect_new_install_scripts(
    before: Mapping[str, Mapping[str, str]],
    after: Mapping[str, Mapping[str, str]],
) -> List[str]:
    """Return package keys that newly gained or changed install scripts between snapshots."""
    flagged: List[str] = []
    for pkg, hooks in sorted(after.items()):
        prev = before.get(pkg)
        if prev is None or dict(prev) != dict(hooks):
            flagged.append(pkg)
    return flagged


def read_control_file(
    path: Optional[Path],
    *,
    now: float,
    manual_window_s: int = 300,
) -> Tuple[bool, bool]:
    """Read `(auto_upgrade, manual_requested)` safely without following symlinks."""
    if path is None:
        return True, False
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True, False
    except OSError as exc:
        logger.warning("Ignoring control file %s: %s", path, exc)
        return True, False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > 4096:
            logger.warning("Ignoring non-regular or oversized control file %s", path)
            return True, False
        raw = os.read(fd, 4096)
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("Ignoring malformed JSON in control file %s", path)
        return True, False
    if not isinstance(data, dict):
        return True, False
    auto = data.get("auto_upgrade")
    auto_enabled = auto if isinstance(auto, bool) else True
    req_at = data.get("upgrade_requested_at")
    manual = (
        isinstance(req_at, (int, float))
        and not isinstance(req_at, bool)
        and 0.0 <= (now - float(req_at)) <= float(manual_window_s)
    )
    return auto_enabled, manual


def backup_data_dir(
    data_dir: Path,
    backup_dir: Path,
    version: str,
    *,
    now: float,
    keep: int = 5,
) -> Optional[Path]:
    """Create a root-only `.tar.gz` backup of `data_dir` and prune old archives."""
    if not data_dir.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    safe_ver = re.sub(r"[^A-Za-z0-9_.-]+", "_", version or "unknown").strip("._") or "unknown"
    dst = backup_dir / f"pi-pocket-data-{safe_ver}-{int(now)}.tar.gz"
    fd, tmp_name = tempfile.mkstemp(dir=backup_dir, prefix=".backup-", suffix=".tar.gz")
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        os.chmod(tmp_path, 0o600)
        with tarfile.open(tmp_path, "w:gz") as tar:
            tar.add(data_dir, arcname=data_dir.name)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, dst)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    archives = sorted(
        backup_dir.glob("pi-pocket-data-*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for stale in archives[max(1, keep):]:
        stale.unlink(missing_ok=True)
    return dst


class _StatusOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_status(url: str, timeout_s: float) -> Tuple[Optional[int], str]:
    opener = urllib.request.build_opener(_StatusOnlyRedirect())
    req = urllib.request.Request(url, method="GET")
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            return int(resp.status), ""
    except urllib.error.HTTPError as err:
        return int(err.code), ""
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return None, str(exc)


def check_health(
    base_url: str,
    *,
    retries: int = 20,
    interval_s: float = 0.5,
    timeout_s: float = 3.0,
) -> Tuple[bool, str]:
    """Verify `/` returns 200 and unauthenticated `/api/status` returns 401."""
    root = base_url.rstrip("/")
    last_reason = "health check did not run"
    for attempt in range(max(1, retries)):
        code_root, err_root = _http_status(f"{root}/", timeout_s)
        code_api, err_api = _http_status(f"{root}/api/status", timeout_s)
        if code_root == 200 and code_api == 401:
            return True, "ok (/ 200, /api/status 401)"
        last_reason = (
            f"unhealthy (/ -> {code_root or err_root}, "
            f"/api/status -> {code_api or err_api})"
        )
        if attempt + 1 < retries and interval_s > 0:
            time.sleep(interval_s)
    return False, last_reason


def write_status_atomic(status_file: Path, payload: Mapping[str, Any]) -> None:
    """Write `payload` to `status_file` (`0644`) atomically without following symlinks."""
    status_file.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(status_file.parent, 0o755)
    fd, tmp_name = tempfile.mkstemp(dir=status_file.parent, prefix=".status-", suffix=".json")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, status_file)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


FIRST_SEEN_FILE = "tags-first-seen.json"


def apply_first_seen(
    tags: Sequence[Tuple[str, int, str]], seen_file: Path, now: float
) -> List[Tuple[str, int, str]]:
    """Age each tag from when this host first saw it, never from the date upstream wrote into it.

    A tag's own date is set by whoever creates it, so a backdated tag would pass the delay at once.
    The first run (no record yet) trusts the dates of the tags already there; every tag that shows
    up later counts from the moment it was first seen. A tag moved to another commit is new again.
    A record that cannot be read counts every tag as new (fail closed).
    """
    exists = seen_file.exists() or seen_file.is_symlink()
    known: Dict[str, Any] = {}
    if exists and seen_file.is_file() and not seen_file.is_symlink():
        try:
            data = json.loads(seen_file.read_text(encoding="utf-8"))
            known = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            known = {}
    ts_now = int(now)
    out: List[Tuple[str, int, str]] = []
    changed = False
    for tag, created_ts, commit in tags:
        key = f"{tag}@{commit}"
        seen = known.get(key)
        if not isinstance(seen, int) or isinstance(seen, bool):
            seen = min(int(created_ts), ts_now) if not exists else ts_now
            known[key] = seen
            changed = True
        out.append((tag, max(int(created_ts), seen), commit))
    if changed or not exists:
        write_status_atomic(seen_file, known)
    return out


def _default_run_cmd(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: int = 300,
) -> Tuple[int, str, str]:
    res = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return res.returncode, res.stdout or "", res.stderr or ""


def _read_previous_status(status_file: Path) -> Dict[str, Any]:
    if not status_file.is_file() or status_file.is_symlink():
        return {}
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _git_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _npm_env(node_bin: Optional[Path]) -> Dict[str, str]:
    env = dict(os.environ)
    base_path = env.get("PATH", "/usr/bin:/bin")
    env["PATH"] = f"{node_bin}:{base_path}" if node_bin else base_path
    env["npm_config_update_notifier"] = "false"
    return env


def _parse_tags(raw: str) -> List[Tuple[str, int, str]]:
    out: List[Tuple[str, int, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        tag, ts_str, commit = parts
        try:
            ts = int(ts_str.strip())
        except ValueError:
            continue
        out.append((tag.strip(), ts, commit.strip()))
    return out


def _fix_repo_permissions(repo_dir: Path) -> None:
    """Keep `/opt/pi-pocket` root-owned and un-writable by `pi-pocket` when running as root."""
    if os.geteuid() != 0:
        return
    for root, dirs, files in os.walk(repo_dir, followlinks=False):
        rpath = Path(root)
        os.lchown(rpath, 0, 0)
        os.chmod(rpath, 0o755)
        for name in dirs:
            p = rpath / name
            if p.is_symlink():
                os.lchown(p, 0, 0)
        for name in files:
            p = rpath / name
            os.lchown(p, 0, 0)
            if not p.is_symlink():
                mode = p.stat().st_mode
                new_mode = (mode | 0o444) & ~0o022
                if mode & 0o111:
                    new_mode |= 0o111
                os.chmod(p, new_mode)


def _service_active(run_cmd: CommandRunner, service_name: str) -> bool:
    rc, out, _ = run_cmd(["systemctl", "is-active", service_name], timeout=15)
    return rc == 0 and out.strip() == "active"


def _apply_service_state(run_cmd: CommandRunner, service_name: str, should_run: bool) -> Tuple[int, str, str]:
    verb = "restart" if should_run else "stop"
    return run_cmd(["systemctl", verb, service_name], timeout=30)


def _build_status_payload(
    *,
    now: float,
    delay_days: int,
    auto_upgrade: bool,
    current_version: Optional[str],
    tag_info: Mapping[str, Any],
    last_result: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    eligible = tag_info.get("eligible_version")
    target = tag_info.get("target")
    return {
        "checked_at": round(now, 3),
        "delay_days": max(1, int(delay_days)),
        "auto_upgrade": bool(auto_upgrade),
        "current_version": current_version,
        "newest_version": tag_info.get("newest_version"),
        "newest_tag": tag_info.get("newest_tag"),
        "newest_age_days": tag_info.get("newest_age_days"),
        "eligible_version": eligible,
        "eligible_tag": tag_info.get("eligible_tag"),
        "upgrade_available": target is not None,
        "last_result": dict(last_result) if last_result is not None else None,
    }


def run_upgrade(
    *,
    repo_dir: Path,
    data_dir: Path,
    backup_dir: Path,
    status_file: Path,
    control_file: Optional[Path] = None,
    node_bin: Optional[Path] = None,
    service_name: str = "pi-pocket.service",
    health_base: str = "http://127.0.0.1:8797",
    delay_days: int = 3,
    check_only: bool = False,
    fetch_remote: bool = True,
    now_fn: Callable[[], float] = time.time,
    run_cmd: Optional[CommandRunner] = None,
    health_fn: Optional[HealthChecker] = None,
) -> Tuple[int, Dict[str, Any]]:
    """Run check-only refresh or controlled Pi Pocket upgrade with rollback."""
    exec_cmd = run_cmd or _default_run_cmd
    probe_health = health_fn or check_health
    npm_cmd = str(node_bin / "npm") if node_bin else "npm"

    status_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = status_file.parent / ".upgrade.lock"
    with open(lock_path, "w", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

        now = float(now_fn())
        prev_status = _read_previous_status(status_file)
        prev_last = prev_status.get("last_result") if isinstance(prev_status.get("last_result"), dict) else None
        auto_enabled, manual_requested = read_control_file(control_file, now=now)

        if fetch_remote:
            rc, _, err = exec_cmd(
                ["git", *SAFE_GIT, "-C", str(repo_dir), "fetch", "--tags", "--quiet", "origin"],
                env=_git_env(),
                timeout=120,
            )
            if rc != 0:
                current_version = read_package_version(repo_dir)
                result = {
                    "at": round(now, 3),
                    "from_version": current_version,
                    "to_version": current_version,
                    "outcome": "fetch_failed",
                    "rolled_back": False,
                    "new_install_scripts": [],
                    "detail": (err or "git fetch failed").strip()[:300],
                }
                payload = {
                    "checked_at": round(now, 3),
                    "delay_days": max(1, int(delay_days)),
                    "auto_upgrade": bool(auto_enabled),
                    "current_version": current_version,
                    "newest_version": prev_status.get("newest_version"),
                    "newest_tag": prev_status.get("newest_tag"),
                    "newest_age_days": prev_status.get("newest_age_days"),
                    "eligible_version": prev_status.get("eligible_version"),
                    "eligible_tag": prev_status.get("eligible_tag"),
                    "upgrade_available": bool(prev_status.get("upgrade_available", False)),
                    "last_result": result,
                }
                write_status_atomic(status_file, payload)
                return 1, payload

        rc, raw_tags, err = exec_cmd(
            [
                "git", *SAFE_GIT, "-C", str(repo_dir),
                "for-each-ref",
                "--format=%(refname:short)\t%(creatordate:unix)\t%(objectname)",
                "refs/tags",
            ],
            env=_git_env(),
            timeout=30,
        )
        if rc != 0:
            raise RuntimeError(f"git for-each-ref failed: {err.strip()}")

        tags = apply_first_seen(_parse_tags(raw_tags), status_file.parent / FIRST_SEEN_FILE, now)
        current_version = read_package_version(repo_dir)
        tag_info = select_target_tag(
            tags,
            current_version=current_version,
            now=now,
            delay_days=delay_days,
        )

        if check_only:
            payload = _build_status_payload(
                now=now,
                delay_days=delay_days,
                auto_upgrade=auto_enabled,
                current_version=current_version,
                tag_info=tag_info,
                last_result=prev_last,
            )
            write_status_atomic(status_file, payload)
            return 0, payload

        if not auto_enabled and not manual_requested:
            payload = _build_status_payload(
                now=now,
                delay_days=delay_days,
                auto_upgrade=auto_enabled,
                current_version=current_version,
                tag_info=tag_info,
                last_result=prev_last,
            )
            write_status_atomic(status_file, payload)
            return 0, payload

        if manual_requested:
            # The operator pressed "Upgrade now": the newest stable tag, without the wait. Backup,
            # the install-script guard and the rollback still apply.
            tag_info = select_target_tag(tags, current_version=current_version, now=now, delay_days=0)

        target = tag_info.get("target")
        if target is None:
            keep_real = prev_last if (prev_last and prev_last.get("outcome") in {
                "ok", "rolled_back", "blocked_install_scripts", "upgrade_failed", "fetch_failed",
            }) else None
            last_res = keep_real or {
                "at": round(now, 3),
                "from_version": current_version,
                "to_version": current_version,
                "outcome": "up_to_date",
                "rolled_back": False,
                "new_install_scripts": [],
                "detail": "Already on latest eligible stable release",
            }
            payload = _build_status_payload(
                now=now,
                delay_days=delay_days,
                auto_upgrade=auto_enabled,
                current_version=current_version,
                tag_info=tag_info,
                last_result=last_res,
            )
            write_status_atomic(status_file, payload)
            return 0, payload

        rc, head_out, err = exec_cmd(
            ["git", *SAFE_GIT, "-C", str(repo_dir), "rev-parse", "--verify", "HEAD"],
            env=_git_env(),
            timeout=15,
        )
        if rc != 0 or not head_out.strip():
            raise RuntimeError(f"cannot read current HEAD: {err.strip()}")
        prev_commit = head_out.strip()
        was_active = _service_active(exec_cmd, service_name)
        before_scripts = scan_install_scripts(repo_dir)
        backup_path = backup_data_dir(data_dir, backup_dir, current_version or "unknown", now=now)

        def _rollback(reason: str, outcome: str, new_scripts: Optional[List[str]] = None) -> Dict[str, Any]:
            exec_cmd(
                ["git", *SAFE_GIT, "-C", str(repo_dir), "checkout", "--force", prev_commit],
                env=_git_env(),
                timeout=60,
            )
            exec_cmd(
                [npm_cmd, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=repo_dir,
                env=_npm_env(node_bin),
                timeout=300,
            )
            _fix_repo_permissions(repo_dir)
            _apply_service_state(exec_cmd, service_name, was_active)
            rb_ok = True
            rb_note = ""
            if was_active:
                rb_ok, rb_note = probe_health(health_base)
            restored_ver = read_package_version(repo_dir) or current_version
            result = {
                "at": round(now, 3),
                "from_version": current_version,
                "to_version": target["version"],
                "target_tag": target["tag"],
                "outcome": outcome,
                "rolled_back": True,
                "rollback_healthy": rb_ok,
                "new_install_scripts": new_scripts or [],
                "backup_path": str(backup_path) if backup_path else "",
                "detail": reason if rb_ok else f"{reason}; rollback health: {rb_note}",
            }
            payload = _build_status_payload(
                now=now,
                delay_days=delay_days,
                auto_upgrade=auto_enabled,
                current_version=restored_ver,
                tag_info=tag_info,
                last_result=result,
            )
            write_status_atomic(status_file, payload)
            return payload

        rc, _, err = exec_cmd(
            ["git", *SAFE_GIT, "-C", str(repo_dir), "checkout", "--force", target["commit"]],
            env=_git_env(),
            timeout=60,
        )
        if rc != 0:
            payload = _rollback(f"git checkout failed: {err.strip()[:200]}", "rolled_back")
            return 1, payload

        rc, out, err = exec_cmd(
            [npm_cmd, "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
            cwd=repo_dir,
            env=_npm_env(node_bin),
            timeout=300,
        )
        if rc != 0:
            msg = (err or out or "npm ci failed").strip()[:240]
            payload = _rollback(f"npm ci failed: {msg}", "rolled_back")
            return 1, payload

        after_scripts = scan_install_scripts(repo_dir)
        new_scripts = detect_new_install_scripts(before_scripts, after_scripts)
        if new_scripts:
            payload = _rollback(
                f"blocked new install script(s) in: {', '.join(new_scripts)}",
                "blocked_install_scripts",
                new_scripts=new_scripts,
            )
            return 1, payload

        _fix_repo_permissions(repo_dir)

        rc, out, err = exec_cmd(["systemctl", "restart" if was_active else "start", service_name], timeout=30)
        if rc != 0:
            msg = (err or out or "systemctl start/restart failed").strip()[:240]
            payload = _rollback(f"service start failed: {msg}", "rolled_back")
            return 1, payload

        healthy, health_msg = probe_health(health_base)
        if not healthy:
            payload = _rollback(health_msg, "rolled_back")
            return 1, payload

        if not was_active:
            exec_cmd(["systemctl", "stop", service_name], timeout=30)

        new_version = read_package_version(repo_dir) or target["version"]
        post_tag_info = select_target_tag(
            tags,
            current_version=new_version,
            now=now,
            delay_days=delay_days,
        )
        ok_result = {
            "at": round(now, 3),
            "from_version": current_version,
            "to_version": new_version,
            "target_tag": target["tag"],
            "outcome": "ok",
            "rolled_back": False,
            "new_install_scripts": [],
            "backup_path": str(backup_path) if backup_path else "",
            "detail": f"Upgraded {current_version or '?'} -> {new_version} ({health_msg})",
        }
        payload = _build_status_payload(
            now=now,
            delay_days=delay_days,
            auto_upgrade=auto_enabled,
            current_version=new_version,
            tag_info=post_tag_info,
            last_result=ok_result,
        )
        write_status_atomic(status_file, payload)
        return 0, payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    check_only = "--check-only" in (list(argv) if argv is not None else sys.argv[1:])
    node_env = os.environ.get("PI_POCKET_NODE_BIN", "/opt/node-v24.22.3/bin").strip()
    ctrl_env = os.environ.get("PI_POCKET_CONTROL_FILE", "").strip()
    try:
        delay = int(os.environ.get("PI_POCKET_DELAY_DAYS", "3"))
    except ValueError:
        delay = 3

    code, payload = run_upgrade(
        repo_dir=Path(os.environ.get("PI_POCKET_REPO", "/opt/pi-pocket")),
        data_dir=Path(os.environ.get("PI_POCKET_DATA", "/var/lib/pi-pocket")),
        backup_dir=Path(os.environ.get("PI_POCKET_BACKUP_DIR", "/var/backups/pi-pocket")),
        status_file=Path(os.environ.get("PI_POCKET_STATUS_FILE", "/var/lib/pi-pocket-upgrade/status.json")),
        control_file=Path(ctrl_env) if ctrl_env else None,
        node_bin=Path(node_env) if node_env else None,
        service_name=os.environ.get("PI_POCKET_SERVICE", "pi-pocket.service"),
        health_base=os.environ.get("PI_POCKET_HEALTH_BASE", "http://127.0.0.1:8797"),
        delay_days=delay,
        check_only=check_only,
    )
    print(json.dumps({
        "current_version": payload.get("current_version"),
        "newest_version": payload.get("newest_version"),
        "eligible_version": payload.get("eligible_version"),
        "upgrade_available": payload.get("upgrade_available"),
        "last_outcome": (payload.get("last_result") or {}).get("outcome"),
    }))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
