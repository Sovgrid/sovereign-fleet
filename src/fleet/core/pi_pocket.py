"""Pi Pocket host controls and status for Sovereign Fleet Settings (admin-only).

The daemon runs unprivileged and never touches `/opt/pi-pocket` directly; service power
actions and launching `pi-pocket-upgrade.service` rely on a 4-action polkit rule installed
separately by root (`contrib/pi-pocket/50-sovereign-fleet-pi-pocket.rules`).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fleet.core.config import load_config
from fleet.core.pi_pocket_upgrade import parse_semver, read_package_version
from fleet.core.settings import FleetSettings, save_settings

_SAFE_UNIT_RE = re.compile(r"^[A-Za-z0-9@._-]{1,128}$")
_VALID_POWER = ("start", "stop", "restart")


class PiPocketError(RuntimeError):
    """Raised when Pi Pocket service control or status configuration is invalid or refused."""


def repo_dir() -> Path:
    return Path(os.environ.get("FLEET_PI_POCKET_REPO", "/opt/pi-pocket"))


def status_file() -> Path:
    return Path(os.environ.get("FLEET_PI_POCKET_STATUS_FILE", "/var/lib/pi-pocket-upgrade/status.json"))


def control_file() -> Path:
    override = os.environ.get("FLEET_PI_POCKET_CONTROL_FILE", "").strip()
    if override:
        return Path(override)
    return load_config().root / "pi-pocket-control.json"


def service_name() -> str:
    unit = os.environ.get("FLEET_PI_POCKET_SERVICE", "pi-pocket.service").strip()
    if not _SAFE_UNIT_RE.match(unit):
        raise PiPocketError(f"invalid service unit name: {unit!r}")
    return unit


def upgrade_service_name() -> str:
    unit = os.environ.get("FLEET_PI_POCKET_UPGRADE_SERVICE", "pi-pocket-upgrade.service").strip()
    if not _SAFE_UNIT_RE.match(unit):
        raise PiPocketError(f"invalid upgrade unit name: {unit!r}")
    return unit


def delay_days() -> int:
    try:
        return max(1, int(os.environ.get("FLEET_PI_POCKET_DELAY_DAYS", "3")))
    except ValueError:
        return 3


def _read_status_json(path: Path) -> Dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _systemctl_state(unit: str) -> str:
    try:
        res = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except OSError:
        return "unavailable"
    text = (res.stdout or res.stderr or "").strip()
    return text or ("active" if res.returncode == 0 else "inactive")


def _run_systemctl(args: list[str], *, timeout: int) -> None:
    try:
        res = subprocess.run(
            ["systemctl", "--no-ask-password", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PiPocketError(f"systemctl failed: {exc}") from exc
    if res.returncode != 0:
        detail = (res.stderr or res.stdout or f"systemctl exited {res.returncode}").strip()
        raise PiPocketError(detail[:300])


def write_control_file(
    auto_upgrade: bool,
    *,
    upgrade_requested_at: Optional[float] = None,
) -> Path:
    """Write the daemon's upgrade preference/trigger JSON atomically (`0644`) for the root job."""
    dst = control_file()
    if dst.parent.is_symlink() or dst.is_symlink():
        raise PiPocketError(f"refusing to write control file through symlink: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "auto_upgrade": bool(auto_upgrade),
        "updated_at": round(time.time(), 3),
    }
    if upgrade_requested_at is not None:
        payload["upgrade_requested_at"] = round(float(upgrade_requested_at), 3)

    fd, tmp_name = tempfile.mkstemp(dir=dst.parent, prefix=".pi-pocket-ctrl.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, dst)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return dst


def get_status(settings: FleetSettings) -> Dict[str, Any]:
    """Return Pi Pocket runtime, version, and last-upgrade state without triggering external I/O."""
    repo = repo_dir()
    sfile = status_file()
    svc = service_name()
    up_svc = upgrade_service_name()

    disk_ver = read_package_version(repo)
    raw_status = _read_status_json(sfile)
    installed = disk_ver is not None or bool(raw_status)
    svc_state = _systemctl_state(svc) if installed else "not-installed"
    up_state = _systemctl_state(up_svc) if installed else "inactive"

    current_ver = disk_ver or raw_status.get("current_version")
    eligible_ver = raw_status.get("eligible_version")
    newest_ver = raw_status.get("newest_version")
    cur_t = parse_semver(str(current_ver or ""))
    elig_t = parse_semver(str(eligible_ver or ""))
    new_t = parse_semver(str(newest_ver or ""))
    if cur_t and elig_t:
        upgrade_available = elig_t > cur_t
    elif eligible_ver and current_ver:
        upgrade_available = False
    else:
        upgrade_available = bool(raw_status.get("upgrade_available"))
    newer_available = bool(cur_t and new_t and new_t > cur_t)

    return {
        "installed": installed,
        "url": load_config().fleet.pi_pocket_url or None,
        "service_name": svc,
        "active": svc_state == "active",
        "service_state": svc_state,
        "upgrade_running": up_state in ("activating", "active"),
        "current_version": current_ver,
        "newest_version": newest_ver,
        "newest_age_days": raw_status.get("newest_age_days"),
        "eligible_version": eligible_ver,
        "upgrade_available": upgrade_available,
        "newer_available": newer_available,   # "Upgrade now" installs it without the wait
        "auto_upgrade": bool(settings.pi_pocket_auto_upgrade),
        "delay_days": int(raw_status.get("delay_days") or delay_days()),
        "checked_at": raw_status.get("checked_at"),
        "last_result": raw_status.get("last_result"),
    }


def set_power(action: str, settings: FleetSettings) -> Dict[str, Any]:
    """Start, stop, or restart `pi-pocket.service` via polkit-authorized systemctl."""
    if action not in _VALID_POWER:
        raise PiPocketError(f"unsupported power action: {action!r}")
    svc = service_name()
    st = get_status(settings)
    if not st["installed"]:
        raise PiPocketError("Pi Pocket is not installed on this host")
    _run_systemctl([action, svc], timeout=30)
    return get_status(settings)


def set_settings(auto_upgrade: bool, settings: FleetSettings) -> tuple[FleetSettings, Dict[str, Any]]:
    """Persist `pi_pocket_auto_upgrade` in `FleetSettings` and the root-readable control file."""
    updated = settings.model_copy(update={"pi_pocket_auto_upgrade": bool(auto_upgrade)})
    save_settings(updated)
    write_control_file(auto_upgrade=bool(auto_upgrade))
    return updated, get_status(updated)


def trigger_upgrade(settings: FleetSettings) -> Dict[str, Any]:
    """Request an immediate `pi-pocket-upgrade.service` run without waiting 3s/network in HTTP."""
    st = get_status(settings)
    if not st["installed"]:
        raise PiPocketError("Pi Pocket is not installed on this host")
    write_control_file(
        auto_upgrade=bool(settings.pi_pocket_auto_upgrade),
        upgrade_requested_at=time.time(),
    )
    _run_systemctl(["--no-block", "start", upgrade_service_name()], timeout=15)
    out = get_status(settings)
    out["triggered"] = True
    return out
