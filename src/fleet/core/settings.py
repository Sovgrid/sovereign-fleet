"""Runtime settings, persisted to ~/.config/sovereign-fleet/settings.json (FLEET_SETTINGS_FILE).

The sandbox is deliberately not a setting: a web toggle that switches it off would turn a
stolen session into unconfined code execution. Only FLEET_SANDBOX=0 in the daemon's own
environment disables it.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("fleet.core.settings")

Autonomy = Literal["manual_gate", "safe_auto", "full_swarm"]


class FleetSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Only settings the code reads. (Older files also carry max_concurrent, gateguard_* and
    # pacific_midnight_offset_hours; they never changed anything and are ignored when loaded.)
    task_timeout_seconds: int = Field(600, ge=30, le=24 * 3600)   # one agent run under `fleet run`
    autonomy_level: Autonomy = "safe_auto"
    # Draw provider keys from the credential plugin of fleet.toml [plugins], if one is loaded.
    use_credential_plugin: bool = Field(False, validation_alias=AliasChoices("use_credential_plugin",
                                                                            "multi_key_rotation"))
    # Who coordinates the fleet (the dashboard's choice; default: [fleet] coordinator of
    # fleet.toml). With the grid backend the runner's own setting is used instead.
    coordinator: Optional[str] = Field(None, max_length=64)
    # Projects whose issue intake is paused from the dashboard (fleet.toml enables it).
    intake_paused: List[str] = Field(default_factory=list)
    # Cross-lane review loop: automatically queue a review report for landed features
    auto_review: Literal["off", "features", "all"] = "features"
    # Auto-fix severity threshold: critical or high findings trigger an automated fix task
    review_autofix: Literal["off", "critical", "high"] = "high"
    # Per-provider budget policy overrides: keys are provider names (from lane_provider),
    # values one of fleet.core.budget.POLICIES. Empty dict: every provider uses its default.
    # Stored as plain str so a broken JSON file never raises on load (we validate at read time).
    budget_policies: Dict[str, str] = Field(default_factory=dict)
    # Audit log of budget policy changes; kept to the last 200 entries.
    budget_log: List[Dict[str, Any]] = Field(default_factory=list)
    # Usage ledger reference model, "provider/model" (wins over fleet.toml [ledger] reference).
    # Here and not only in fleet.toml: an older client reading the strict fleet.toml rejects a
    # section it does not know, while this file's unknown keys are ignored (2026-10-29).
    # Stored as plain str like budget_policies; the ledger validates it when pricing.
    ledger_reference: Optional[str] = Field(None, max_length=200)


def settings_file() -> Path:
    default = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "sovereign-fleet" / "settings.json"
    return Path(os.environ.get("FLEET_SETTINGS_FILE", str(default)))


def load_settings() -> FleetSettings:
    path = settings_file()
    if not path.exists():
        return FleetSettings()
    try:
        return FleetSettings(**json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as e:
        # a broken file must not take the daemon down; say so and run on defaults
        logger.error("Ignoring unreadable settings %s: %s", path, e)
        return FleetSettings()


def save_settings(settings: FleetSettings) -> None:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(settings.model_dump_json(indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
