"""Lane quirks: renamed opencode free models, local model servers that hold one model at a time.

opencode's free models get renamed upstream (`<family>-1.2-<variant>-free` becomes `...-1.3-...`).
`refresh_opencode_models()` reads `opencode models` off the event loop; `resolve_opencode_model()`
then maps a configured id that disappeared to the newest id of the same family that is still
free, and never to anything else. Without a model list, ids are used as configured.

A local lane with `probe_url` runs only while its server answers `GET <probe_url>/models`. When
`routing.switch_local` is on, `ensure_resident()` loads the lane's model with
`routing.switch_command` before the lane runs; one switch at a time, and never while another task
of this process runs on a local lane.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import time
import urllib.request
from typing import Dict, List, Optional, Sequence, Set, Tuple

from fleet.core.config import LaneSpec, RoutingSection

logger = logging.getLogger("fleet.core.quirks")

MODEL_LIST_TTL = 300
_models: Dict[str, object] = {"ts": 0.0, "ids": []}
# `<provider>/<family>-<version>[-<variant>]-free`: a rename changes only the version
_FREE_ID = re.compile(r"^(?P<provider>[^/]+)/(?P<family>[a-z][a-z0-9-]*?)-(?P<version>\d[\w.]*?)"
                      r"(?P<variant>-[a-z][\w.-]*)?-free$")


# ── opencode free models ──────────────────────────────────────────────────────

def _list_opencode_models(timeout: float) -> List[str]:
    binary = shutil.which("opencode")
    if not binary:
        return []
    try:
        res = subprocess.run([binary, "models"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("opencode models: %s", e)
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()] if res.returncode == 0 else []


async def refresh_opencode_models(force: bool = False, timeout: float = 10.0) -> List[str]:
    """The live opencode model list, cached for MODEL_LIST_TTL seconds; read in a thread."""
    if force or not _models["ids"] or time.time() - float(_models["ts"]) > MODEL_LIST_TTL:
        ids = await asyncio.to_thread(_list_opencode_models, timeout)
        if ids:
            _models.update(ts=time.time(), ids=ids)
    return list(_models["ids"])  # type: ignore[arg-type]


def resolve_opencode_model(model: str, known: Optional[Sequence[str]] = None) -> str:
    """`model`, or the newest free id of its family if `model` is gone from the list. Never
    blocks: it uses the list that refresh_opencode_models() cached."""
    ids = list(known if known is not None else _models["ids"])  # type: ignore[arg-type]
    if not model or not ids or model in ids:
        return model
    m = _FREE_ID.match(model)
    if not m:
        return model
    def same_model(other: str) -> bool:
        o = _FREE_ID.match(other)
        return bool(o) and (o["provider"], o["family"], o["variant"]) == (m["provider"], m["family"], m["variant"])

    same_family = [i for i in ids if same_model(i)]
    if not same_family:
        logger.warning("opencode model %s is gone and has no free successor", model)
        return model
    newest = max(same_family, key=_version_key)
    logger.info("opencode model %s renamed upstream; using %s", model, newest)
    return newest


def gone_opencode_model(spec: LaneSpec) -> Optional[str]:
    """The model of a lane that runs only through opencode, if the cached list shows it is gone
    and has no successor; such a lane cannot run. None when unknown or fine."""
    ids = _models["ids"]
    if not ids or any(step.harness != "opencode" for step in spec.steps):
        return None
    for step in spec.steps:
        if not step.model or resolve_opencode_model(step.model) in ids:  # type: ignore[operator]
            return None
    return spec.steps[0].model


def _version_key(model_id: str) -> Tuple:
    return tuple(int(n) for n in re.findall(r"\d+", _FREE_ID.match(model_id)["version"]))


# ── local model servers ───────────────────────────────────────────────────────

_switch_lock: Optional[asyncio.Lock] = None
_resident_cache: Dict[str, Tuple[float, bool]] = {}


def is_resident(spec: LaneSpec, timeout: float = 1.0) -> bool:
    """A lane without probe_url always counts as resident."""
    if not spec.probe_url:
        return True
    try:
        with urllib.request.urlopen(f"{spec.probe_url.rstrip('/')}/models", timeout=timeout) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


def non_resident_lanes(lanes: Dict[str, LaneSpec], ttl: float = 0.0) -> Set[str]:
    """Lanes whose `probe_url` server is not answering right now.

    Each unique `probe_url` is probed at most once per call; when `ttl > 0`, recent answers
    within `ttl` seconds are reused so dashboard polls do not probe on every request.
    """
    now = time.monotonic()
    checked: Dict[str, bool] = {}
    missing: Set[str] = set()
    for name, spec in lanes.items():
        if not spec.probe_url:
            continue
        url = spec.probe_url.rstrip("/")
        if url not in checked:
            cached = _resident_cache.get(url)
            if ttl > 0 and cached is not None and (now - cached[0]) < ttl:
                checked[url] = cached[1]
            else:
                ok = is_resident(spec)
                _resident_cache[url] = (now, ok)
                checked[url] = ok
        if not checked[url]:
            missing.add(name)
    return missing


async def ensure_resident(lane: str, spec: LaneSpec, routing: RoutingSection,
                          busy: Sequence[str] = ()) -> Optional[str]:
    """None when the lane's model server answers (loading it first if allowed), else why not.
    `busy`: other local lanes with tasks running now; their model must not be unloaded."""
    global _switch_lock
    if await asyncio.to_thread(is_resident, spec):
        return None
    if not (routing.switch_local and routing.switch_command and spec.switch_to):
        return f"lane {lane}: its model is not loaded ({spec.probe_url})"
    _switch_lock = _switch_lock or asyncio.Lock()
    async with _switch_lock:
        if await asyncio.to_thread(is_resident, spec):
            return None
        busy = [name for name in busy if name != lane]
        if busy:
            return f"lane {lane}: not loaded, and {', '.join(busy)} still uses the loaded model"
        argv = [a.replace("{model}", spec.switch_to) for a in routing.switch_command]
        logger.warning("loading %s for lane %s: %s", spec.switch_to, lane, " ".join(argv))
        proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
        except asyncio.TimeoutError:
            proc.kill()
            return f"lane {lane}: switching to {spec.switch_to} timed out"
        if proc.returncode != 0:
            return f"lane {lane}: switch to {spec.switch_to} failed: {out.decode(errors='replace')[-200:]}"
        for _ in range(60):  # the server needs a moment after the switch command returns
            if await asyncio.to_thread(is_resident, spec):
                return None
            await asyncio.sleep(5)
        return f"lane {lane}: {spec.switch_to} switched but {spec.probe_url} does not answer"
