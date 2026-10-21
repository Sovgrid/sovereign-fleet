"""Lane health check: which lanes of fleet.toml work today (`fleet lanes check`, daily in the daemon).

One probe per lane, chosen by what the lane needs to run:

    key_env set       an API lane: one tiny chat completion straight to the provider, with the
                      key exactly as the engine resolves it (credential plugin while the setting
                      allows it, key_file, the daemon's own environment)
    probe_url set     a local model server: GET <probe_url>/models
    opencode only     the lane's model (or its renamed successor) is in `opencode models`
    anything else     a CLI that logs in (claude-code, goose, pi): not probed; the router learns
                      from its runs

The verdict of an API probe comes from the HTTP status, as in the grid runner:

    200                                ok
    401, 402, 404, 410, 403 with JSON  dead: the key, the plan or the model is gone
    429 with a quota of 0              dead: the free tier was never granted
    anything else, no answer           transient: nothing changes

A dead lane stays unavailable to the router until a later check passes. Transient errors and
lanes that are not probed keep their last status. A local model that is not loaded is not
dead: the router checks residency before every run and may load it (routing.switch_local).

Retired is dead for good: the provider answered 410, or the model left the provider's list and
the model catalog (fleet.core.catalog) marks it deprecated or it stayed gone for RETIRE_AFTER.
A retired lane takes a successor on its own: the newest model of the same family that the
provider offers, that is not older, not deprecated and not more expensive, and that passes the
same probe. The lane keeps its name and settings; fleet.toml is not rewritten. Its router
history starts again with the successor. Without a successor the lane stays out of routing.

With routing.discover, free tool-calling models that the providers of your lanes offer and no
lane uses become lanes of their own (auto_*) after a probe; without it they are only listed.

The state lives in <fleet.root>/lane_health.json. Keys are never printed, logged or stored.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from fleet.core import catalog, quirks
from fleet.core.config import FleetConfig, LaneSpec, LaneStep, active_credential_plugin, lane_key, load_config

logger = logging.getLogger("fleet.core.lane_health")

OK, DEAD, RETIRED, TRANSIENT, SKIPPED = "ok", "dead", "retired", "transient", "skipped"
UNAVAILABLE = (DEAD, RETIRED)
# NVIDIA NIM queues free requests: a healthy model answered after 55 and 86 s (2026-10-17), so a
# 20 s limit reported working models as failed. A timeout never changes a lane's status anyway.
PROBE_TIMEOUT = 120.0
STATE_FILE = "lane_health.json"
# a model gone from the provider's list that the catalog does not call deprecated
RETIRE_AFTER = 7 * 86400
SUCCESSOR_TRIES = 3
DISCOVER_PER_PROVIDER = 5

# OpenAI-compatible chat endpoints by the provider prefix of a model id: aider's (litellm) and
# opencode's names. A lane elsewhere names its endpoint with --openai-api-base / --api-base.
_ENDPOINTS = {
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "google": "https://generativelanguage.googleapis.com/v1beta/openai",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "nvidia_nim": "https://integrate.api.nvidia.com/v1",
    "openai": "https://api.openai.com/v1",
}
_BASE_FLAGS = ("--openai-api-base", "--api-base", "--base-url", "--openai-base-url")
_CHAT = "/chat/completions"
# Google: "limit: 0" in the message, or "limit": 0 in the (possibly escaped) JSON details
_ZERO_QUOTA = re.compile(r'limit\\?"?\s*:\s*0\b|quota of 0\b', re.IGNORECASE)


@dataclass(frozen=True)
class Probe:
    verdict: str                  # ok | dead | transient | skipped
    reason: Optional[str] = None
    retired: bool = False         # the provider says the model is gone for good
    gone: bool = False            # the model is not in the provider's list any more
    notice: Dict[str, str] = field(default_factory=dict)   # an announced end, while it still works


# ── verdicts ──────────────────────────────────────────────────────────────────

def verdict(status: int, body: str) -> Probe:
    """The verdict on a provider's answer to the tiny request."""
    if status == 200:
        return Probe(OK)
    if status == 401:
        return Probe(DEAD, "key rejected (HTTP 401)")
    if status == 402:
        return Probe(DEAD, "payment required: trial or credit used up (HTTP 402)")
    if status == 403 and (body or "").lstrip().startswith("{"):
        return Probe(DEAD, "access denied by the provider (HTTP 403)")
    if status == 404:
        return Probe(DEAD, "model not available (HTTP 404)")
    if status == 410:
        eol = catalog.end_of_life(body)
        return Probe(DEAD, f"retired by the provider{f' on {eol}' if eol else ''} (HTTP 410)", retired=True)
    if status == 429 and _ZERO_QUOTA.search(body or ""):
        return Probe(DEAD, "free-tier quota is 0 (HTTP 429)")
    return Probe(TRANSIENT, f"HTTP {status}" if status else "no answer")


# ── probes ────────────────────────────────────────────────────────────────────

def api_target(spec: LaneSpec) -> Optional[Tuple[str, str]]:
    """(chat completions URL, model id) for the first step whose endpoint is known."""
    for step in spec.steps:
        if not step.model:
            continue
        prefix, _, rest = step.model.partition("/")
        base = _flag(step, _BASE_FLAGS)
        if base:
            # an explicit base: litellm's "openai/" prefix only picks the client
            model = rest if prefix == "openai" and rest else step.model
            return base.rstrip("/") + _CHAT, model
        endpoint = _ENDPOINTS.get(prefix) if rest else None
        endpoint = endpoint or (_ENDPOINTS.get(spec.provider.lower()) if spec.provider else None)
        if endpoint:
            return endpoint + _CHAT, rest or step.model
    return None


def _flag(step: LaneStep, names: Sequence[str]) -> str:
    for i, arg in enumerate(step.args):
        if arg in names and i + 1 < len(step.args):
            return step.args[i + 1]
        for name in names:
            if arg.startswith(name + "="):
                return arg.split("=", 1)[1]
    return ""


def probe_api(url: str, model: str, key: str, timeout: float = PROBE_TIMEOUT) -> Probe:
    body = json.dumps({"model": model, "max_tokens": 8,
                       "messages": [{"role": "user", "content": "Reply with: ok"}]}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "User-Agent": "sovereign-fleet-lane-health"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return verdict(resp.status, "")
    except urllib.error.HTTPError as e:
        try:
            text = e.read(2000).decode("utf-8", "replace")
        except OSError:
            text = ""
        return verdict(e.code, text)
    except (urllib.error.URLError, OSError, ValueError) as e:
        # the reason names the host, never the request headers
        return Probe(TRANSIENT, f"no answer: {getattr(e, 'reason', e)}"[:160])


def probe_local(spec: LaneSpec) -> Probe:
    if quirks.is_resident(spec, timeout=5.0):
        return Probe(OK)
    return Probe(TRANSIENT, f"model not loaded ({spec.probe_url})")


class Context:
    """What one check knows beyond the lane itself: the catalog, the provider lists (each read
    once), the credential plugin. Shared by the probes, which run in threads."""

    def __init__(self, cfg: FleetConfig, credential_plugin: Any = None,
                 models_catalog: Optional[Dict[str, Any]] = None, opencode_models: Optional[List[str]] = None):
        self.cfg = cfg
        self.plugin = credential_plugin
        self.catalog = models_catalog or {}
        self.opencode_models = opencode_models
        self._lists: Dict[str, Optional[Dict[str, Dict[str, Any]]]] = {}
        self._lock = Lock()

    def key(self, spec: LaneSpec) -> Optional[str]:
        return lane_key(spec, self.plugin, self.cfg.secrets_dir) or os.environ.get(spec.key_env) or None

    def offered(self, base: str, key: str) -> Optional[Dict[str, Dict[str, Any]]]:
        """The provider's model list for `base`, read once per check."""
        with self._lock:
            if base not in self._lists:
                self._lists[base] = catalog.list_models(base, key)
            return self._lists[base]


def _catalog_notice(ctx: Context, provider: str, model: str) -> Dict[str, str]:
    return {"deprecated": "models.dev"} if catalog.deprecated(ctx.catalog, provider, model) else {}


def probe_api_lane(spec: LaneSpec, ctx: Context) -> Probe:
    target = api_target(spec)
    if target is None:
        return Probe(SKIPPED, "no known endpoint for its model; add --openai-api-base to a step")
    key = ctx.key(spec)
    if not key:
        where = " or ".join(filter(None, [
            f"credential plugin '{spec.provider}'" if ctx.plugin is not None and spec.provider else "",
            f"key_file {spec.key_file}" if spec.key_file else "", f"${spec.key_env}"]))
        return Probe(DEAD, f"no key ({where})")
    url, model = target
    probe = probe_api(url, model, key)
    if probe.verdict == TRANSIENT:
        return probe
    base = url[: -len(_CHAT)]
    listed = ctx.offered(base, key)
    provider = catalog.provider_of(base)
    if probe.verdict == OK:
        notice = {**_catalog_notice(ctx, provider, model), **catalog.provider_notice((listed or {}).get(model))}
        return Probe(OK, notice=notice)
    if probe.retired or listed is None or not probe.reason or "404" not in probe.reason:
        return probe
    if model in listed:
        return Probe(DEAD, "listed by the provider, but not available for this key (HTTP 404)")
    return Probe(DEAD, "no longer in the provider's model list (HTTP 404)", gone=True,
                 retired=bool(catalog.deprecated(ctx.catalog, provider, model)))


def probe_opencode(spec: LaneSpec, models: Optional[List[str]], models_catalog: Optional[Dict] = None) -> Probe:
    if not models:
        return Probe(TRANSIENT, "`opencode models` gave no list")
    for step in spec.steps:
        if step.model and quirks.resolve_opencode_model(step.model, models) in models:
            notice = {"deprecated": "models.dev"} if catalog.deprecated(
                models_catalog or {}, "opencode", quirks.resolve_opencode_model(step.model, models)) else {}
            return Probe(OK, notice=notice)
    model = spec.steps[0].model
    return Probe(DEAD, f"{model} is no longer offered by opencode", gone=True,
                 retired=bool(catalog.deprecated(models_catalog or {}, "opencode", model)))


def _opencode_only(spec: LaneSpec) -> bool:
    return all(step.harness == "opencode" for step in spec.steps)


def probe_lane(name: str, spec: LaneSpec, credential_plugin: Any = None,
               secrets_dir: Optional[Path] = None, opencode_models: Optional[List[str]] = None,
               ctx: Optional[Context] = None) -> Probe:
    if ctx is None:
        cfg = load_config()
        ctx = Context(cfg, credential_plugin, {}, opencode_models)
    if spec.key_env:
        return probe_api_lane(spec, ctx)
    if spec.probe_url:
        return probe_local(spec)
    if _opencode_only(spec):
        return probe_opencode(spec, ctx.opencode_models, ctx.catalog)
    return Probe(SKIPPED, "logs in through its CLI; not probed")


# ── state ─────────────────────────────────────────────────────────────────────

def state_path(cfg: Optional[FleetConfig] = None) -> Path:
    return (cfg or load_config()).root / STATE_FILE


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """One writer at a time: the daily run, the dashboard button and the CLI may overlap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def load_state(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"checked_at": 0, "lanes": {}, "discovered": {}}
    if not isinstance(data, dict) or not isinstance(data.get("lanes"), dict):
        return {"checked_at": 0, "lanes": {}, "discovered": {}}
    if not isinstance(data.get("discovered"), dict):
        data["discovered"] = {}
    return data


def _save_state(path: Path, state: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# ── successors and discovered lanes: the lanes the fleet runs ─────────────────

def _replace_model(model: str, old: str, new: str) -> str:
    if model == old:
        return new
    if model.endswith("/" + old):
        return model[: -len(old)] + new
    return model


def _applies(spec: LaneSpec, successor: Optional[Dict[str, Any]]) -> bool:
    return bool(successor) and any(_replace_model(s.model, successor["from"], successor["to"]) != s.model
                                   for s in spec.steps if s.model)


def _with_successor(spec: LaneSpec, successor: Dict[str, Any]) -> LaneSpec:
    steps = [s.model_copy(update={"model": _replace_model(s.model, successor["from"], successor["to"])})
             for s in spec.steps]
    return spec.model_copy(update={"steps": steps})


def _discovered_spec(cfg: FleetConfig, found: Dict[str, Any]) -> Optional[LaneSpec]:
    if found.get("harness") == "opencode":
        return LaneSpec(cost="free", steps=[LaneStep(harness="opencode", model=found["model"])])
    template = cfg.lanes.get(found.get("template") or "")
    if template is None:
        return None
    return LaneSpec(cost="free", provider=template.provider, key_env=template.key_env,
                    key_file=template.key_file, pass_env=list(template.pass_env),
                    steps=[LaneStep(harness="aider", model=f"openai/{found['model']}",
                                    args=["--openai-api-base", found["base"]])])


def effective_lanes(cfg: Optional[FleetConfig] = None, state: Optional[Dict[str, Any]] = None) -> Dict[str, LaneSpec]:
    """The lanes as the fleet runs them: fleet.toml's, with a retired model replaced by its
    successor, plus the discovered lanes when routing.discover is on."""
    cfg = cfg or load_config()
    state = state if state is not None else load_state(state_path(cfg))
    stored = state.get("lanes") or {}
    lanes: Dict[str, LaneSpec] = {}
    for name, spec in cfg.lanes.items():
        successor = (stored.get(name) or {}).get("successor")
        lanes[name] = _with_successor(spec, successor) if _applies(spec, successor) else spec
    if cfg.routing.discover:
        for name, found in (state.get("discovered") or {}).items():
            if name not in lanes and found.get("verified"):
                spec = _discovered_spec(cfg, found)
                if spec is not None:
                    lanes[name] = spec
    return lanes


def fresh_since(cfg: Optional[FleetConfig] = None) -> Dict[str, float]:
    """Per lane that switched to a successor: when. The router ignores its older history."""
    cfg = cfg or load_config()
    stored = load_state(state_path(cfg))["lanes"]
    return {name: float(rec["successor"]["since"]) for name, rec in stored.items()
            if name in cfg.lanes and (rec.get("successor") or {}).get("since")
            and _applies(cfg.lanes[name], rec["successor"])}


def lane_records(cfg: Optional[FleetConfig] = None) -> Dict[str, Dict[str, Any]]:
    """Per lane the fleet runs: status (ok | dead | retired | untested), reason, since, checked_at;
    `successor` when a retired model was replaced, `notice` when its end is announced."""
    cfg = cfg or load_config()
    state = load_state(state_path(cfg))
    stored = state["lanes"]
    return {name: stored.get(name) or {"status": "untested", "reason": None, "since": None, "checked_at": None}
            for name in effective_lanes(cfg, state)}


def dead_lanes(cfg: Optional[FleetConfig] = None) -> Set[str]:
    """Lanes that cannot run: dead or retired."""
    return {name for name, rec in lane_records(cfg).items() if rec.get("status") in UNAVAILABLE}


def summary(records: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    by = {s: sorted(n for n, r in records.items() if r.get("status") == s) for s in (OK, DEAD, RETIRED, "untested")}
    text = f"{len(by[OK])} ok, {len(by[DEAD])} dead, {len(by['untested'])} untested"
    if by[RETIRED]:
        text += f", {len(by[RETIRED])} retired"
    down = "; ".join(f"{label}: {', '.join(by[s])}" for label, s in (("dead", DEAD), ("retired", RETIRED)) if by[s])
    replaced = sorted(n for n, r in records.items() if r.get("successor") and r.get("status") == OK)
    return {"ok": len(by[OK]), "dead": len(by[DEAD]), "retired": len(by[RETIRED]), "untested": len(by["untested"]),
            "dead_lanes": by[DEAD], "retired_lanes": by[RETIRED], "replaced_lanes": replaced,
            "text": text + (f" ({down})" if down else "")}


def _next(old: Dict[str, Any], probe: Probe, now: int) -> Dict[str, Any]:
    status = old.get("status") or "untested"
    rec = {"status": status, "reason": old.get("reason"), "since": old.get("since"),
           "checked_at": now, "last_probe": probe.verdict, "last_detail": probe.reason}
    if old.get("successor"):
        rec["successor"] = old["successor"]
    if probe.verdict == OK:
        rec.update(status=OK, reason=None, since=old.get("since") if status == OK else now)
        if probe.notice:
            rec["notice"] = probe.notice
    elif probe.verdict == DEAD:
        down = status in UNAVAILABLE
        since = old.get("since") if down else now
        retired = probe.retired or status == RETIRED or (probe.gone and down and now - (since or now) >= RETIRE_AFTER)
        rec.update(status=RETIRED if retired else DEAD, reason=probe.reason, since=since)
    return rec


# ── successors ────────────────────────────────────────────────────────────────

def _find_successor(name: str, spec: LaneSpec, ctx: Context, now: int) -> Optional[Dict[str, Any]]:
    """The first same-family candidate that passes the lane's own probe."""
    if spec.key_env:
        target = api_target(spec)
        key = ctx.key(spec)
        if target is None or not key:
            return None
        url, model = target
        base = url[: -len(_CHAT)]
        listed = ctx.offered(base, key) or {}
        provider = catalog.provider_of(base)
        hint = catalog.provider_notice(listed.get(model)).get("replacement", "")
        for candidate in catalog.successors(ctx.catalog, provider, model, listed, hint)[:SUCCESSOR_TRIES]:
            if probe_api(url, candidate, key).verdict == OK:
                return {"from": model, "to": candidate, "since": now,
                        "source": "named by the provider" if candidate == hint else f"models.dev family of {model}"}
        return None
    if _opencode_only(spec) and ctx.opencode_models:
        model = spec.steps[0].model
        candidates = catalog.successors(ctx.catalog, "opencode", model, ctx.opencode_models)
        if candidates:
            return {"from": model, "to": candidates[0], "since": now, "source": f"models.dev family of {model}"}
    return None


def _adopt(name: str, cfg_spec: LaneSpec, rec: Dict[str, Any], ctx: Context, now: int) -> Dict[str, Any]:
    """A retired lane with a working successor runs again; the successor counts from the
    configured model, so a chain of retirements still maps fleet.toml's model to the newest."""
    current = _with_successor(cfg_spec, rec["successor"]) if _applies(cfg_spec, rec.get("successor")) else cfg_spec
    found = _find_successor(name, current, ctx, now)
    if not found:
        rec["reason"] = f"{rec.get('reason')}; no successor".replace("; no successor; no successor", "; no successor")
        return rec
    first = (rec.get("successor") or {}).get("from") if _applies(cfg_spec, rec.get("successor")) else None
    successor = {**found, "from": first or found["from"], "replaced": found["from"],
                 "replaced_because": rec.get("reason")}
    logger.warning("lane %s: %s retired (%s); it runs %s now", name, found["from"], rec.get("reason"), found["to"])
    return {**rec, "status": OK, "reason": None, "since": now, "successor": successor,
            "last_detail": f"{found['from']} retired; successor {found['to']} passed its probe"}


# ── discovery ─────────────────────────────────────────────────────────────────

def _slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", model.split("/")[-1].lower()).strip("_")


def _discover(cfg: FleetConfig, lanes: Dict[str, LaneSpec], ctx: Context, old: Dict[str, Any],
              now: int) -> Dict[str, Any]:
    """Free tool-calling models the lanes' providers offer and no lane uses, probed."""
    if not ctx.catalog:
        return {}
    used: Set[str] = set()
    bases: Dict[str, Tuple[str, str]] = {}           # base -> (template lane, key)
    for name, spec in lanes.items():
        for step in spec.steps:
            if step.model:
                used.add(step.model)
                used.add(step.model.split("/", 1)[-1])
        target = api_target(spec) if spec.key_env and name in cfg.lanes else None
        key = ctx.key(spec) if target else None
        if target and key:
            used.add(target[1])
            bases.setdefault(target[0][: -len(_CHAT)], (name, key))
    found: Dict[str, Any] = {}

    def keep(lane: str, entry: Dict[str, Any], verified: bool) -> None:
        prev = old.get(lane) or {}
        found[lane] = {**entry, "verified": verified, "since": prev.get("since") or now, "checked_at": now}

    if ctx.opencode_models and any(_opencode_only(s) for s in cfg.lanes.values()):
        for item in catalog.new_free_models(ctx.catalog, "opencode", ctx.opencode_models, used,
                                            DISCOVER_PER_PROVIDER):
            keep(f"auto_oc_{_slug(item['model'])}", {**item, "harness": "opencode", "provider": "opencode"}, True)
    pending = []
    for base, (template, key) in bases.items():
        provider = catalog.provider_of(base)
        listed = ctx.offered(base, key)
        if not provider or not listed:
            continue
        for item in catalog.new_free_models(ctx.catalog, provider, listed, used, DISCOVER_PER_PROVIDER):
            pending.append((f"auto_{provider}_{_slug(item['model'])}", item, provider, base, template, key))

    def probe_one(entry):
        lane, item, provider, base, template, key = entry
        prev = old.get(lane) or {}
        if prev.get("verified") and now - (prev.get("checked_at") or 0) < 86400:
            return entry, Probe(OK)
        return entry, probe_api(base + _CHAT, item["model"], key)

    with ThreadPoolExecutor(max_workers=8) as pool:      # slow queues answer in parallel
        for (lane, item, provider, base, template, _key), probe in pool.map(probe_one, pending):
            keep(lane, {**item, "harness": "aider", "provider": provider, "base": base, "template": template,
                        "last_probe": probe.verdict, "last_detail": probe.reason}, probe.verdict == OK)
    return found


# ── the check ─────────────────────────────────────────────────────────────────

def check(cfg: Optional[FleetConfig] = None, apply: bool = False, lanes: Optional[Sequence[str]] = None,
          credential_plugin: Any = "active", models_catalog: Optional[Dict[str, Any]] = None,
          found_models: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Probe the enabled lanes (or `lanes`) in parallel, find successors for retired models and
    new free models; with `apply`, store the outcome. Returns per probed lane its probe verdict
    and the resulting record; `found_models`, if given, receives the new free models."""
    cfg = cfg or load_config()
    plugin = active_credential_plugin() if credential_plugin == "active" else credential_plugin
    path = state_path(cfg)
    before = load_state(path)
    running = effective_lanes(cfg, before)
    names = [n for n, s in running.items() if s.enabled and (lanes is None or n in lanes)]
    needs_models = any(not s.key_env and not s.probe_url and _opencode_only(s) for s in cfg.lanes.values())
    models = quirks._list_opencode_models(30.0) if needs_models else None
    cat = models_catalog if models_catalog is not None else catalog.load(cfg.root, cfg.routing.catalog)
    ctx = Context(cfg, plugin, cat, models)

    def one(name: str) -> Tuple[str, Probe]:
        try:
            return name, probe_lane(name, running[name], ctx=ctx)
        except Exception as e:  # noqa: BLE001 - one broken probe must not end the check
            logger.exception("lane %s: probe failed", name)
            return name, Probe(TRANSIENT, f"probe error: {type(e).__name__}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        probes = dict(pool.map(one, names))

    # Successor and discovery probes happen outside the lock; it guards only the write.
    now = int(time.time())
    stored = {n: dict(r) for n, r in before["lanes"].items() if n in running}   # drop removed lanes
    for name, rec in stored.items():                        # fleet.toml changed the model: start over
        if name in cfg.lanes and rec.get("successor") and not _applies(cfg.lanes[name], rec["successor"]):
            rec.pop("successor")
    out = {}
    for name, probe in probes.items():
        rec = _next(stored.get(name) or {}, probe, now)
        if rec["status"] == RETIRED and name in cfg.lanes:
            rec = _adopt(name, cfg.lanes[name], rec, ctx, now)
        stored[name] = rec
        out[name] = {"probe": probe.verdict, "detail": probe.reason, **rec}
    found = before.get("discovered") or {}
    if lanes is None:
        found = _discover(cfg, effective_lanes(cfg, {"lanes": stored, "discovered": {}}), ctx, found, now)
        for name, item in found.items():                    # a verified new lane starts out ok
            if item["verified"] and cfg.routing.discover and name not in stored:
                stored[name] = {"status": OK, "reason": None, "since": now, "checked_at": now,
                                "last_probe": OK, "last_detail": "discovered", "discovered": True}
    stored = {n: r for n, r in stored.items() if n in cfg.lanes or (cfg.routing.discover and n in found)}
    if found_models is not None:
        found_models.update(found)
    if apply:
        with _locked(path):
            _save_state(path, {"checked_at": now, "lanes": stored, "discovered": found})
    for name, rec in out.items():
        if rec["status"] in UNAVAILABLE and rec["probe"] == DEAD:
            logger.warning("lane %s is %s: %s", name, rec["status"], rec["reason"])
    return out


def discovered(cfg: Optional[FleetConfig] = None) -> Dict[str, Any]:
    """The free models the last check found that no configured lane uses."""
    cfg = cfg or load_config()
    return load_state(state_path(cfg)).get("discovered") or {}
