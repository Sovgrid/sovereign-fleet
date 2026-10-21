"""Quota ladder and Pacific Midnight reset tracker for Sovereign Fleet."""

import datetime
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from fleet.core.config import load_config

logger = logging.getLogger("fleet.core.quota")


@dataclass
class LaneQuota:
    lane: str
    daily_limit: int
    used_today: int = 0
    consecutive_rate_limits: int = 0
    blocked_until: float = 0.0
    free_tier: bool = True


@dataclass
class KeySlot:
    key_id: str
    token: str
    active: bool = True
    blocked_until: float = 0.0
    failure_count: int = 0


class SingleKeyManager:
    """One API key per provider and account. No rotation to second accounts: Groq's and Google's
    terms forbid using extra accounts to get past rate limits; a limit means a lane change."""

    def __init__(self, secrets_dir: Optional[str] = None):
        if secrets_dir:
            self.secrets_dir = Path(secrets_dir)
        else:
            self.secrets_dir = load_config().secrets_dir
        self.keys: Dict[str, KeySlot] = {}
        self._auto_discover_keys()

    def _auto_discover_keys(self) -> None:
        """Discovers each provider's single key from secrets_dir or the environment."""
        providers = {
            "groq": ("groq_primary", "groq_api.key", "GROQ_API_KEY"),
            "gemini": ("gemini_primary", "gemini_studio.key", "GEMINI_API_KEY"),
            "openrouter": ("openrouter_primary", "openrouter_api.key", "OPENROUTER_API_KEY"),
        }

        for provider, (slot_id, filename, env_var) in providers.items():
            token = ""
            # 1. Try environment variable
            if env_var in os.environ and os.environ[env_var].strip():
                token = os.environ[env_var].strip()
            # 2. Try secret file
            elif self.secrets_dir.exists():
                fpath = self.secrets_dir / filename
                if fpath.exists():
                    try:
                        token = fpath.read_text(encoding="utf-8").strip()
                    except OSError as e:
                        logger.warning(f"Could not read {fpath}: {e}")
            if token:
                self.keys[provider] = KeySlot(key_id=slot_id, token=token)

    def register_key(self, provider: str, slot_id: str, token: str) -> None:
        """Registers a single key for a provider. Any subsequent key registration replaces the key;
        never queues second accounts for rotation."""
        self.keys[provider] = KeySlot(key_id=slot_id, token=token)

    def get_active_key(self, provider: str) -> Optional[str]:
        slot = self.keys.get(provider)
        if not slot:
            return None
        return slot.token

    def record_rate_limit(self, provider: str, penalty_seconds: int = 300) -> Tuple[bool, Optional[str]]:
        """Records a rate limit on the provider's key. Returns (False, token) because rotation
        to second accounts is prohibited by provider terms."""
        slot = self.keys.get(provider)
        if not slot:
            return False, None
        slot.failure_count += 1
        slot.blocked_until = time.time() + penalty_seconds
        logger.warning(
            f"Key {slot.key_id} for '{provider}' rate-limited. Blocked for {penalty_seconds}s (no secondary rotation)."
        )
        return False, slot.token

    def rotate_on_rate_limit(self, provider: str, penalty_seconds: int = 300) -> Tuple[bool, Optional[str]]:
        """Deprecated alias for record_rate_limit. Always returns False for rotation."""
        return self.record_rate_limit(provider, penalty_seconds)

    def record_success(self, provider: str) -> None:
        slot = self.keys.get(provider)
        if slot:
            slot.failure_count = 0



# ── Canonical lane quota & throttling functions (ported from voice/dispatcher.py) ──

def get_quota_file(spool_dir: Optional[Path] = None) -> Path:
    base = spool_dir or load_config().root / "spool"
    return base / "quota_state.json"


def get_quota_state(spool_dir: Optional[Path] = None) -> Dict[str, Any]:
    qfile = get_quota_file(spool_dir)
    if qfile.exists():
        try:
            return json.loads(qfile.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return {}


def next_pacific_midnight(now: Optional[float] = None) -> int:
    """Google AI Studio daily free-tier quotas reset at midnight Pacific time."""
    pacific = ZoneInfo("America/Los_Angeles")
    local = datetime.datetime.fromtimestamp(now if now is not None else time.time(), pacific)
    nxt = local.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    return int(nxt.timestamp())


def block_lane_until(lane: str, until: int, reason: str, spool_dir: Optional[Path] = None) -> None:
    """Take a lane out of routing until a quota resets (daily limit hit)."""
    state = get_quota_state(spool_dir)
    lane_state = state.setdefault(lane, {"events": []})
    lane_state.update(blocked_until=int(until), block_reason=reason[:120])
    qfile = get_quota_file(spool_dir)
    try:
        qfile.parent.mkdir(parents=True, exist_ok=True)
        qfile.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning(f"Could not save quota state to {qfile}: {e}")
    logger.warning(f"lane {lane} blocked until {datetime.datetime.fromtimestamp(until):%Y-%m-%d %H:%M}: {reason}")


def lane_blocked_until(lane: str, spool_dir: Optional[Path] = None) -> int:
    """Returns timestamp until which lane is blocked, or 0 if unblocked."""
    until = int((get_quota_state(spool_dir).get(lane) or {}).get("blocked_until") or 0)
    return until if until > time.time() else 0


def lane_block_info(lane: str, spool_dir: Optional[Path] = None) -> Tuple[int, str]:
    """Returns (until_timestamp, reason) if lane is currently blocked, else (0, '')."""
    st = get_quota_state(spool_dir).get(lane) or {}
    until = int(st.get("blocked_until") or 0)
    if until > time.time():
        return until, str(st.get("block_reason") or "")
    return 0, ""


def record_lane_quota_event(lane: str, event_type: str, details: str = "", spool_dir: Optional[Path] = None) -> None:
    """Records a rate_limit or quota event in the sliding 1-hour window."""
    state = get_quota_state(spool_dir)
    now = int(time.time())
    lane_state = state.get(lane, {"events": []})
    lane_state["events"] = [e for e in lane_state.get("events", []) if now - e.get("timestamp", 0) < 3600][-19:]
    lane_state["events"].append({"timestamp": now, "type": event_type, "details": details[:100]})
    state[lane] = lane_state
    qfile = get_quota_file(spool_dir)
    try:
        qfile.parent.mkdir(parents=True, exist_ok=True)
        qfile.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning(f"Could not save quota state to {qfile}: {e}")


def is_lane_quota_throttled(lane: str, window_seconds: int = 900, spool_dir: Optional[Path] = None) -> bool:
    """Checks whether lane recently hit rate limits in the throttle window."""
    state = get_quota_state(spool_dir)
    lane_state = state.get(lane, {})
    now = int(time.time())
    for ev in lane_state.get("events", []):
        if ev.get("type") in ("rate_limit", "quota_exceeded") and (now - ev.get("timestamp", 0)) < window_seconds:
            return True
    return False


class QuotaLadder:
    """Manages rate-limits, free-tier ladders, strictly single-account keys, and Pacific Midnight resets."""

    def __init__(self, secrets_dir: Optional[str] = None, spool_dir: Optional[Path] = None,
                 key_manager: Optional[Any] = None):
        self.lanes: Dict[str, LaneQuota] = {}
        self.spool_dir = spool_dir
        self.key_manager = key_manager or SingleKeyManager(secrets_dir=secrets_dir)

    def set_key_manager(self, manager: Any) -> None:
        """Pluggable key manager hook, e.g. one that reads keys from a vault."""
        self.key_manager = manager

    def register_lane(self, lane: str, daily_limit: int, free_tier: bool = True) -> None:
        self.lanes[lane] = LaneQuota(lane=lane, daily_limit=daily_limit, free_tier=free_tier)

    @staticmethod
    def get_pacific_midnight() -> float:
        return float(next_pacific_midnight())

    def record_usage(self, lane: str, count: int = 1) -> None:
        if lane in self.lanes:
            self.lanes[lane].used_today += count

    def record_rate_limit(self, lane: str, penalty_seconds: int = 60, reason: str = "rate limit") -> None:
        if lane in self.lanes:
            q = self.lanes[lane]
            q.consecutive_rate_limits += 1
            delay = min(penalty_seconds * (2 ** (q.consecutive_rate_limits - 1)), 600)
            until = time.time() + delay
            q.blocked_until = until
            block_lane_until(lane, int(until), reason, spool_dir=self.spool_dir)
            record_lane_quota_event(lane, "rate_limit", reason, spool_dir=self.spool_dir)
            logger.warning(f"Lane {lane} rate-limited. Blocked for {delay}s")

    def record_success(self, lane: str) -> None:
        if lane in self.lanes:
            self.lanes[lane].consecutive_rate_limits = 0

    def get_provider_key(self, provider: str) -> Optional[str]:
        return self.key_manager.get_active_key(provider)

    def rotate_provider_key(self, provider: str, penalty_seconds: int = 300) -> Tuple[bool, Optional[str]]:
        if hasattr(self.key_manager, "rotate_on_rate_limit"):
            return self.key_manager.rotate_on_rate_limit(provider, penalty_seconds=penalty_seconds)
        return self.key_manager.record_rate_limit(provider, penalty_seconds=penalty_seconds)

    def select_best_lane(self, preferred: Optional[str] = None) -> Optional[str]:
        """Selects the best available lane: preferred if available, then free tiers, then commercial."""
        now = time.time()

        # Check preferred
        if preferred and preferred in self.lanes:
            p_lane = self.lanes[preferred]
            if p_lane.blocked_until <= now and not lane_blocked_until(preferred, self.spool_dir) and p_lane.used_today < p_lane.daily_limit:
                return preferred

        # Candidate free-tier lanes not blocked
        available_free = [
            l for l, q in self.lanes.items()
            if q.free_tier and q.blocked_until <= now and not lane_blocked_until(l, self.spool_dir) and q.used_today < q.daily_limit
        ]
        if available_free:
            available_free.sort(key=lambda l: self.lanes[l].used_today)
            return available_free[0]

        # Fallback commercial / paid lanes
        available_paid = [
            l for l, q in self.lanes.items()
            if not q.free_tier and q.blocked_until <= now and not lane_blocked_until(l, self.spool_dir) and q.used_today < q.daily_limit
        ]
        if available_paid:
            return available_paid[0]

        return None

