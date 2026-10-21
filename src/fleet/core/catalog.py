"""What is known about a provider's models: whether one is retired, what replaces it, what is new.

Sources, strongest first:

    the provider's answer   HTTP 410 (NVIDIA NIM says "reached its end of life on <date>"), a
                            model missing from `GET <base>/models` or from `opencode models`
    the provider's list     Mistral's `deprecation` and `deprecation_replacement_model`,
                            OpenRouter's `expiration_date`
    models.dev              the open catalog opencode itself reads: per provider and model its
                            `family`, `release_date`, `status: deprecated`, `cost`, `tool_call`

A successor keeps a lane's purpose: same provider, same family, not older, not deprecated, not
more expensive, tool calling if the old model had it, and offered by the provider today. Kimi K3
retiring on NIM does not make Kimi K2.6 its successor: older, another family, and deprecated.
The caller still probes a candidate before it uses it; a listed model can answer 404.

The catalog is cached in <fleet.root>/model_catalog.json for a day. Without network the last
copy is used; without any copy every catalog question answers "unknown".
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("fleet.core.catalog")

CATALOG_URL = "https://models.dev/api.json"
CATALOG_FILE = "model_catalog.json"
MAX_AGE = 24 * 3600
MAX_BYTES = 64 * 1024 * 1024
TIMEOUT = 30.0

# models.dev provider ids by API host; opencode's own models by the "opencode/" prefix
_HOSTS = {
    "integrate.api.nvidia.com": "nvidia",
    "api.mistral.ai": "mistral",
    "api.groq.com": "groq",
    "generativelanguage.googleapis.com": "google",
    "openrouter.ai": "openrouter",
    "api.cerebras.ai": "cerebras",
    "api.openai.com": "openai",
}
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def source(configured: str = CATALOG_URL) -> str:
    """The catalog's URL or file (routing.catalog); FLEET_MODEL_CATALOG overrides it, "off" or
    "" switches it off."""
    return os.environ.get("FLEET_MODEL_CATALOG", configured).strip()


def _fetch(url: str) -> Dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        return json.loads(Path(url).expanduser().read_text(encoding="utf-8"))
    req = urllib.request.Request(url, headers={"User-Agent": "sovereign-fleet-catalog"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"catalog larger than {MAX_BYTES} bytes")
    return json.loads(data)


def load(root: Path, configured: str = CATALOG_URL, refresh: bool = True) -> Dict[str, Any]:
    """The catalog: the cached copy while it is fresh, else a new one (stale copy on failure)."""
    url = source(configured)
    if not url or url == "off":
        return {}
    path = Path(root) / CATALOG_FILE
    cached: Dict[str, Any] = {}
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    fresh = cached.get("source") == url and time.time() - float(cached.get("fetched_at") or 0) < MAX_AGE
    if fresh or not refresh:
        return cached.get("providers") or {}
    try:
        providers = _fetch(url)
        if not isinstance(providers, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError, urllib.error.URLError) as e:
        logger.warning("model catalog %s unavailable (%s); using the last copy", url, e)
        return cached.get("providers") or {}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"source": url, "fetched_at": time.time(), "providers": providers}),
                   encoding="utf-8")
    tmp.replace(path)
    return providers


def provider_of(base_url: str) -> str:
    """models.dev's provider id for an API base URL ("opencode" for opencode's own models)."""
    if base_url == "opencode":
        return "opencode"
    host = (urlparse(base_url).hostname or "").lower()
    return _HOSTS.get(host, "")


def _key(provider: str, model: str) -> str:
    """The id models.dev uses: opencode's models without the "opencode/" prefix."""
    return model.split("/", 1)[1] if provider == "opencode" and model.startswith("opencode/") else model


def entry(catalog: Dict[str, Any], provider: str, model: str) -> Optional[Dict[str, Any]]:
    models = (catalog.get(provider) or {}).get("models") or {}
    found = models.get(_key(provider, model))
    return found if isinstance(found, dict) else None


def deprecated(catalog: Dict[str, Any], provider: str, model: str) -> Optional[bool]:
    """True or False by the catalog; None when the catalog does not know the model."""
    e = entry(catalog, provider, model)
    return None if e is None else e.get("status") == "deprecated"


def _cost(e: Dict[str, Any]) -> Optional[float]:
    try:
        return float((e.get("cost") or {}).get("input"))
    except (TypeError, ValueError):
        return None


def free(catalog: Dict[str, Any], provider: str, model: str) -> bool:
    e = entry(catalog, provider, model)
    return e is not None and _cost(e) == 0


def successors(catalog: Dict[str, Any], provider: str, model: str, offered: Iterable[str],
               replacement: str = "") -> List[str]:
    """Candidates to replace a retired `model`, best first; `offered` is the provider's list today.
    `replacement` is the successor the provider itself names (Mistral) and comes first."""
    offered = [m for m in offered if m != model]
    out: List[str] = [replacement] if replacement and replacement in offered else []
    old = entry(catalog, provider, model)
    if old is None or not old.get("family"):
        return out
    old_cost = _cost(old)

    def fits(candidate: str) -> bool:
        e = entry(catalog, provider, candidate)
        if e is None or e.get("family") != old["family"] or e.get("status") == "deprecated":
            return False
        if (e.get("release_date") or "") < (old.get("release_date") or ""):
            return False
        if old.get("tool_call") and not e.get("tool_call"):
            return False
        cost = _cost(e)
        return old_cost is None or (cost is not None and cost <= old_cost)

    ranked = sorted((m for m in offered if fits(m)),
                    key=lambda m: (entry(catalog, provider, m) or {}).get("release_date") or "", reverse=True)
    return out + [m for m in ranked if m not in out]


def new_free_models(catalog: Dict[str, Any], provider: str, offered: Iterable[str],
                    used: Iterable[str], limit: int = 5) -> List[Dict[str, Any]]:
    """Models the provider offers today that cost nothing, call tools, are not deprecated and no
    lane uses, newest first."""
    used = set(used)
    found = []
    for model in offered:
        e = entry(catalog, provider, model)
        if model in used or e is None or e.get("status") == "deprecated" or not e.get("tool_call"):
            continue
        if _cost(e) != 0:
            continue
        found.append({"model": model, "family": e.get("family") or "", "name": e.get("name") or model,
                      "release_date": e.get("release_date") or ""})
    found.sort(key=lambda f: f["release_date"], reverse=True)
    return found[:limit]


def list_models(base_url: str, key: str, timeout: float = 20.0) -> Optional[Dict[str, Dict[str, Any]]]:
    """The provider's OpenAI-compatible model list, {id: entry}; None if it cannot be read."""
    req = urllib.request.Request(base_url.rstrip("/") + "/models", headers={
        "Authorization": f"Bearer {key}", "User-Agent": "sovereign-fleet-lane-health"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(MAX_BYTES))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    return {str(m["id"]): m for m in items if isinstance(m, dict) and m.get("id")}


def provider_notice(item: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """What the provider's list says about a model's end: {"retires": date, "replacement": id}."""
    if not item:
        return {}
    out: Dict[str, str] = {}
    for name in ("deprecation", "deprecation_date", "expiration_date"):
        value = item.get(name)
        if isinstance(value, (int, float)) and value > 0:
            value = time.strftime("%Y-%m-%d", time.gmtime(value))
        if isinstance(value, str) and _DATE.search(value):
            out["retires"] = _DATE.search(value).group(0)
            break
    replacement = item.get("deprecation_replacement_model")
    if isinstance(replacement, str) and replacement:
        out["replacement"] = replacement
    return out


def end_of_life(body: str) -> str:
    """The date in a provider's 410 answer ("reached its end of life on 2026-10-15T08:00:00Z")."""
    try:
        detail = json.loads(body).get("detail") or ""
    except (ValueError, AttributeError):
        detail = body or ""
    m = _DATE.search(str(detail))
    return m.group(0) if m else ""
