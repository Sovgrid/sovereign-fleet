"""The roster: the lanes of fleet.toml as the dashboard and the CLI show them.

fleet.toml is the one list of lanes (with retired models replaced by their successors and, with
routing.discover, the discovered free models, as the lane health check keeps them). The package
ships no lanes of its own: models, prices and free tiers change faster than any release, and a
built-in catalog is stale the day it ships.

The roster adds what can be known without running a task, cheapest first:

    switched off here      [DISABLED]       the dashboard's toggle (roster.json); the router skips it
    health check           [DEAD]           dead or retired at the last check (reason, since)
    provider limit         [QUOTA BLOCKED]  resting after a limit, or reported by grid_status
    CLI                    [NO CLI]         the first step's CLI is not installed
    local model            [STANDBY]        the lane's model server does not answer probe_url
    key                    [NO KEY]         key_env set, no key from plugin, key_file or env
    otherwise              [ONLINE]

Nothing here reads a key's value into the result; only whether one is there.
"""

import json
import logging
import os
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fleet.core.config import (FleetConfig, LaneSpec, _expand, active_credential_plugin, harness_binary, lane_key,
                               load_config)

logger = logging.getLogger("fleet.core.roster")

_NIM_HOSTS = ("integrate.api.nvidia.com",)


def grid_unavailable_lanes() -> Dict[str, Dict[str, Any]]:
    """Quota blocks reported by an external grid status API, if one is configured.

    grid_status.url (fleet.toml): a JSON endpoint with config.roster.<lane>.unavailable_reason;
    grid_status.token_file: optional file holding its bearer token.
    """
    res: Dict[str, Dict[str, Any]] = {}
    status_cfg = load_config().grid_status
    url = status_cfg.url.strip()
    if not url:
        return res
    headers = {}
    token_file = _expand(status_cfg.token_file.strip())
    try:
        if token_file:
            headers["Authorization"] = f"Bearer {Path(token_file).read_text().strip()}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=0.8) as response:
            data = json.loads(response.read().decode())
        for k, v in (data.get("config", {}).get("roster", {}) or {}).items():
            if isinstance(v, dict) and v.get("unavailable_reason"):
                res[k] = {"reason": v["unavailable_reason"]}
    except (OSError, ValueError) as e:
        logger.info("Grid status %s unavailable: %s", url, e)
    return res


def category(spec: LaneSpec) -> str:
    """The dashboard's group: local_sovereign, nvidia_nim, subscription or free_tier."""
    if spec.cost == "local" or spec.probe_url:
        return "local_sovereign"
    joined = " ".join([spec.provider.lower()] + [f"{s.model} {' '.join(s.args)}" for s in spec.steps])
    if spec.provider.lower() in ("nvidia", "nvidia_nim", "nim") or any(h in joined for h in _NIM_HOSTS):
        return "nvidia_nim"
    if spec.cost == "paid":
        return "subscription"
    return "free_tier"


def _endpoint(spec: LaneSpec) -> str:
    if spec.probe_url:
        return spec.probe_url
    for step in spec.steps:
        for i, arg in enumerate(step.args):
            if arg in ("--openai-api-base", "--api-base", "--base-url") and i + 1 < len(step.args):
                return step.args[i + 1]
    return ""


def cli_installed(cmd: str) -> bool:
    """In PATH, or in the user dirs a systemd user service does not have on its PATH."""
    if shutil.which(cmd):
        return True
    home = Path.home()
    dirs = [home / ".local" / "bin", home / "bin", Path("/usr/local/bin"), Path("/opt/bin")]
    dirs += list((home / ".nvm" / "versions" / "node").glob("*/bin"))
    return any((d / cmd).is_file() and os.access(d / cmd, os.X_OK) for d in dirs)


def _resident(spec: LaneSpec) -> bool:
    from fleet.core import quirks
    return quirks.is_resident(spec, timeout=1.0)


class RosterManager:
    """The lanes of fleet.toml with their state; the dashboard's toggles live in roster.json."""

    def __init__(self, config_path: Optional[str] = None, cfg: Optional[FleetConfig] = None):
        config_home = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
        self.config_path = Path(config_path) if config_path else Path(config_home) / "sovereign-fleet" / "roster.json"
        self._cfg = cfg
        self.disabled: Set[str] = self._load_disabled()

    def cfg(self) -> FleetConfig:
        return self._cfg or load_config()

    def _load_disabled(self) -> Set[str]:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        if not isinstance(data, dict):
            return set()
        # also reads the per-lane dicts older versions wrote
        return {lane for lane, v in data.items() if isinstance(v, dict) and v.get("enabled") is False}

    def save(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({lane: {"enabled": False} for lane in sorted(self.disabled)}, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.config_path)

    def toggle_lane(self, lane_id: str, enabled: bool) -> bool:
        from fleet.core import lane_health
        if lane_id not in lane_health.effective_lanes(self.cfg()):
            return False
        (self.disabled.discard if enabled else self.disabled.add)(lane_id)
        self.save()
        return True

    def lanes(self) -> List[Dict[str, Any]]:
        """Every lane the fleet runs, with its state, best first."""
        from fleet.core import lane_health
        from fleet.core.quota import lane_block_info
        cfg = self.cfg()
        lanes = lane_health.effective_lanes(cfg)
        records = lane_health.lane_records(cfg)
        external = grid_unavailable_lanes()
        plugin = active_credential_plugin()
        out = []
        for name, spec in lanes.items():
            rec = records.get(name) or {}
            badge, reason = "[ONLINE]", None
            binary = harness_binary(spec.steps[0].harness)
            if name in self.disabled or not spec.enabled:
                badge, reason = "[DISABLED]", "switched off" if name in self.disabled else "enabled = false in fleet.toml"
            elif rec.get("status") in lane_health.UNAVAILABLE:
                badge, reason = "[DEAD]", rec.get("reason")
            elif lane_block_info(name)[0] or name in external:
                until, why = lane_block_info(name)
                badge = "[QUOTA BLOCKED]"
                reason = (f"{why} until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}" if until
                          else str(external[name].get("reason") or "reported by grid_status"))
            elif not cli_installed(binary):
                badge, reason = "[NO CLI]", f"{binary} is not installed"
            elif spec.probe_url and not _resident(spec):
                badge, reason = "[STANDBY]", f"no model served at {spec.probe_url}"
            elif spec.key_env and not (lane_key(spec, plugin, cfg.secrets_dir) or os.environ.get(spec.key_env)):
                badge, reason = "[NO KEY]", f"no key: key_file {spec.key_file or '-'} or ${spec.key_env}"
            out.append({
                "id": name, "name": name, "provider": spec.provider or _provider(spec),
                "category": category(spec), "cost_tier": spec.cost,
                "cost_desc": {"local": "runs on this machine", "free": "free tier", "paid": "paid"}[spec.cost],
                "models": [s.model for s in spec.steps if s.model],
                "harnesses": [s.harness for s in spec.steps],
                "default_harness": spec.steps[0].harness, "endpoint": _endpoint(spec),
                "private": spec.private, "max_parallel": spec.max_parallel,
                "enabled": badge != "[DISABLED]", "available": badge == "[ONLINE]",
                "status_badge": badge, "unavailable_reason": reason, "health": rec or None,
                "context_window": "", "arch": "", "throughput": "", "pass_rate": None, "priority_weight": 0,
            })
        order = {"[ONLINE]": 0, "[STANDBY]": 1, "[QUOTA BLOCKED]": 2, "[NO KEY]": 3, "[NO CLI]": 4,
                 "[DEAD]": 5, "[DISABLED]": 6}
        return sorted(out, key=lambda lane: (order.get(lane["status_badge"], 9), lane["id"]))

    def summary(self) -> Dict[str, int]:
        lanes = self.lanes()
        count = lambda badge: sum(1 for lane in lanes if lane["status_badge"] == badge)  # noqa: E731
        return {"total_count": len(lanes), "online_count": count("[ONLINE]"),
                "quota_blocked_count": count("[QUOTA BLOCKED]"), "no_key_count": count("[NO KEY]"),
                "disabled_count": count("[DISABLED]"), "dead_count": count("[DEAD]")}


def _provider(spec: LaneSpec) -> str:
    step = spec.steps[0]
    if spec.probe_url:
        return "local"
    prefix = step.model.split("/", 1)[0] if "/" in step.model else ""
    return prefix or step.harness
