"""Ledger: what the fleet's work would cost at API list prices (pure).

Spec: reviews/specs/usage-ledger.md (U3, U4). Built by local_qwen (6fad67d, fea640d), condensed in
review.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fleet.core.catalog import entry
from fleet.core.usage import Usage

_CLASSES = ("local", "free", "subscription", "paid", "unknown")
_ENDED = ("completed", "failed")
_SPENT = ("cancelled", "awaiting_approval")  # these only count once an agent ran for them


def _harness_names() -> List[str]:
    from fleet.core.config import builtin_harnesses
    return [cls.name for cls in builtin_harnesses()]


def lane_classes(lanes: Mapping[str, Any], models: Optional[Mapping[str, Any]] = None) -> Dict[str, str]:
    """Every run label to its cost class. A lane gets the class Budgets shows for its provider: the
    provider's facts come from all its lanes (fleet.core.budget), never from one lane alone. A
    pair-routed run (`pair:<harness>/<model>`) gets its model's, on a built-in harness; a harness
    that logs in with its own account makes a paid model a subscription."""
    from fleet.core import budget
    from fleet.core.config import own_login_harnesses
    out: Dict[str, str] = {}
    for name, spec in lanes.items():
        cost, own_login, _ = budget.provider_facts(budget.lane_provider(name, spec), lanes)
        out[name] = budget.provider_kind(cost, own_login)
    own = own_login_harnesses()
    for harness in _harness_names():
        for name, endpoint in (models or {}).items():
            out[f"pair:{harness}/{name}"] = budget.provider_kind(endpoint.cost, harness in own)
    return out


def lane_prices(lanes: Mapping[str, Any], models: Optional[Mapping[str, Any]],
                catalog: dict) -> Dict[str, Dict[str, float]]:
    """Every run label whose own model the catalog knows, to that model's prices."""
    def own(provider: str, model: str) -> Optional[Dict[str, float]]:
        named = provider and not model.startswith(f"{provider}/")
        return reference_price(catalog, f"{provider}/{model}" if named else model)

    out: Dict[str, Dict[str, float]] = {}
    for name, spec in lanes.items():
        price = own(spec.provider, spec.steps[0].model) if spec.steps else None
        if price:
            out[name] = price
    for name, endpoint in (models or {}).items():
        price = own(endpoint.provider, endpoint.model_id)
        if price:
            out.update({f"pair:{harness}/{name}": price for harness in _harness_names()})
    return out


def _tokens(n: int) -> str:
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + " k"
    return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + " M"


def format_ledger(view: Dict[str, Any]) -> List[str]:
    """The ledger as five lines for the MCP tool and `fleet ledger`."""
    tokens, by_class = view.get("tokens") or {}, view.get("by_class") or {}
    reference, price = view.get("reference"), view.get("list_price_usd")
    if price and reference:
        line2 = f"At {reference} list prices: ${price.get('low', 0):.2f} to ${price.get('high', 0):.2f}."
    elif reference:
        line2 = f"Reference {reference} is not in the model catalog."
    else:
        line2 = ('No reference model: set ledger_reference in settings.json or '
                 '[ledger] reference = "provider/model" in fleet.toml.')
    classes = ("local", "subscription", "free", "paid", "unknown")  # the order Budgets shows
    unpriced = view.get("paid_unpriced_tokens") or 0
    return [
        f"This week: {_tokens(tokens.get('total', 0))} tokens, measured on {view.get('measured', 0)} "
        f"of {view.get('tasks', 0)} tasks ({view.get('runs_measured', 0)} of {view.get('runs', 0)} runs).",
        line2,
        f"Paid: ${view.get('paid_usd') or 0:.2f}."
        + (f" {_tokens(unpriced)} tokens have no price, so it was more." if unpriced else ""),
        f"Failed attempts: {_tokens(view.get('failed_attempt_tokens', 0))} tokens.",
        "By class: " + ", ".join(f"{c} {_tokens(by_class.get(c, 0))}" for c in classes) + ".",
    ]


def _price(v: Any) -> Optional[float]:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return float(v) if math.isfinite(v) else None


def reference_price(catalog: dict, reference: str) -> Optional[Dict[str, float]]:
    """USD per million tokens {input, output, cache_read, cache_write} of "provider/model" from
    the models.dev catalog; cache prices fall back to input; None when unknown or malformed."""
    if not isinstance(reference, str) or "/" not in reference:
        return None
    provider, model = reference.split("/", 1)
    e = entry(catalog or {}, provider, model)
    cost = (e or {}).get("cost")
    if not isinstance(cost, dict):
        return None
    p_in, p_out = _price(cost.get("input")), _price(cost.get("output"))
    if p_in is None or p_out is None:
        return None
    cache_read, cache_write = _price(cost.get("cache_read")), _price(cost.get("cache_write"))
    return {"input": p_in, "output": p_out,
            "cache_read": p_in if cache_read is None else cache_read,
            "cache_write": p_in if cache_write is None else cache_write}


def _runs(task: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Optional[Usage]]]:
    """(run, usage) for every well-formed run of a task, in order; usage None: not measured."""
    result = task.get("result")
    runs = result.get("usage_runs") if isinstance(result, dict) else None
    return [(r, Usage.from_dict(r.get("usage"))) for r in runs if isinstance(r, dict)] \
        if isinstance(runs, list) else []


def _ended_as(task: Dict[str, Any], ran: bool) -> Optional[str]:
    """How a task ended, for the week; None while it did not end or no agent ran for it. Archiving
    changes where a task is listed, not what it cost: a task archived here remembers how it ended,
    one archived by the grid runner tells by its exit code."""
    status = task.get("status")
    if status == "archived":
        meta, result = task.get("metadata"), task.get("result")
        status = meta.get("archived_from") if isinstance(meta, dict) else None
        code = result.get("exit_code") if isinstance(result, dict) else None
        if status is None and isinstance(code, int):
            status = "completed" if code == 0 else "failed"
    if status in _ENDED or (status in _SPENT and ran):
        return status
    return None


def _end(task: Dict[str, Any], runs: List[Tuple[Dict[str, Any], Optional[Usage]]]) -> Optional[float]:
    """The latest run's time, else created_at; None when neither is a number."""
    ats = [r.get("at") for r, _ in runs]
    ats = [a for a in ats if _price(a) is not None]
    return float(max(ats)) if ats else _price(task.get("created_at"))


def _cost(u: Usage, p: Dict[str, float], input_price: float) -> float:
    return (u.input * input_price + u.output * p["output"] + u.cache_read * p["cache_read"]
            + u.cache_write * p["cache_write"]) / 1e6


def week_ledger(tasks: list, lane_class: Mapping[str, str], price: Optional[Dict[str, float]],
                reference: Optional[str], now: float, days: int = 7,
                lane_price: Optional[Mapping[str, Dict[str, float]]] = None) -> Dict[str, Any]:
    """Token usage, list-price range and paid amount of the tasks that ended in the last *days*.

    A run is a failed attempt when its task failed or was cancelled, or when a later run of its
    task followed it. Paid is the cost the harness reported, else the tokens at the *lane_price* of
    the run's own model; paid tokens with neither are counted in `paid_unpriced_tokens`, so paid
    work never reads as free. `low` prices the input of a run that reported no cache reads at the cache-read
    price: local engines report none although a caching API would bill most of it that way."""
    since = now - days * 86400
    tokens = dict.fromkeys(("input", "output", "cache_read", "cache_write", "total"), 0)
    by_class = dict.fromkeys(_CLASSES, 0)
    counted = measured = runs_total = runs_measured = failed_tokens = unpriced = 0
    high = low = paid = 0.0
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        runs = _runs(task)
        ended = _ended_as(task, bool(runs))
        end = _end(task, runs)
        if ended is None or end is None or end < since:
            continue
        counted += 1
        runs_total += len(runs)
        measured += any(u is not None for _, u in runs)
        for i, (run, u) in enumerate(runs):
            if u is None:
                continue
            runs_measured += 1
            for key in ("input", "output", "cache_read", "cache_write"):
                tokens[key] += getattr(u, key)
            tokens["total"] += u.total
            if ended in ("failed", "cancelled") or i < len(runs) - 1:
                failed_tokens += u.total
            lane = run.get("lane")
            cls = lane_class.get(lane, "unknown") if isinstance(lane, str) else "unknown"
            cls = cls if cls in by_class else "unknown"
            by_class[cls] += u.total
            if cls == "paid":
                own_price = (lane_price or {}).get(lane) if isinstance(lane, str) else None
                if u.reported_cost is not None:
                    paid += u.reported_cost
                elif own_price:
                    paid += _cost(u, own_price, own_price["input"])
                else:
                    unpriced += u.total
            if price is not None:
                high += _cost(u, price, price["input"])
                low += _cost(u, price, price["cache_read"] if u.cache_read == 0 else price["input"])
    return {
        "days": days,
        "reference": reference,
        "tasks": counted,
        "measured": measured,
        "runs": runs_total,
        "runs_measured": runs_measured,
        "tokens": tokens,
        "failed_attempt_tokens": failed_tokens,
        "by_class": by_class,
        "list_price_usd": None if price is None else {"high": round(high, 2), "low": round(low, 2)},
        "paid_usd": round(paid, 2),
        "paid_unpriced_tokens": unpriced,
    }
