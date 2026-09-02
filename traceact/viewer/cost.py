# viewer/cost.py
#
# Cost estimates for model events, priced via the optional `rates` package
# (https://pypi.org/project/rates/). The viewer computes an estimate at
# display time from the token counts a model event carries — nothing is
# stamped into trace records, and `import traceact` stays dependency-free.
#
# rates is a soft dependency:
#   - Not installed: rates_available() is False, /api/health reports
#     cost_estimates: false, and the viewer renders no cost UI at all.
#     `pip install rates` turns it on.
#   - Installed: the bundled price snapshot is loaded once per process, on
#     the first estimate. The bundled fetch tier is used unconditionally —
#     the viewer never touches the network to price an event.
#
# Why the event must carry a provider:
# One model id maps to many providers with different prices (the same
# model id can be served by its owner, a cloud reseller, and several
# aggregators). An event that names only the model is ambiguous, so it gets
# no estimate rather than a guess — the caller knows which provider it
# called; the viewer doesn't.
#
# rates traces its own registry loads with traceact. In a process with no
# sinks configured, that trace would print to stdout (the console fallback).
# The viewer CLI, as the process entry point, routes package tracing to a
# file before this module ever loads the registry — see
# _configure_app_tracing() in viewer/cli.py. This module itself never
# touches traceact's global configuration: when the viewer runs embedded in
# a host app, that app's own configuration stands.

import functools
import importlib.util
import threading
from typing import Any, Dict, Optional, Tuple

# How many tokens one price unit covers. rates prices in USD (or another
# currency) per million tokens: input_mtok / output_mtok.
_TOKENS_PER_UNIT = 1_000_000

_load_lock = threading.Lock()
_registry: Optional[Any] = None
_load_error: Optional[str] = None
_load_attempted = False


def rates_available() -> bool:
    """True when the `rates` package is importable in this environment."""
    return importlib.util.find_spec("rates") is not None


def _load_registry() -> Tuple[Optional[Any], Optional[str]]:
    """
    Load the bundled rates registry once per process.

    Returns (registry, None) on success and (None, error_message) when
    rates isn't installed or its bundled snapshot can't be read. The
    outcome — success or failure — is cached: the bundled snapshot is a
    local file, so retrying can't change the answer within one process.
    """
    global _registry, _load_error, _load_attempted
    with _load_lock:
        if _load_attempted:
            return _registry, _load_error
        _load_attempted = True
        if not rates_available():
            _load_error = ("cost estimates are off — the rates package "
                           "isn't installed. Run: pip install rates")
            return None, _load_error
        try:
            from rates import ai
            _registry = ai.load(fetch="bundled")
        except Exception as exc:
            _load_error = f"could not load the rates price registry: {exc}"
            return None, _load_error
        return _registry, None


@functools.lru_cache(maxsize=256)
def _price_for(provider: str, model: str) -> Tuple[Optional[Any], Optional[str]]:
    """
    The rates Price for one provider+model pair, or (None, error_message).

    Matching is exact and case-insensitive (rates' own filter semantics).
    When the pair matches more than one registry entry — the same id listed
    under one provider with different prices — the estimate is refused
    rather than picking one, unless every entry agrees on the price.
    """
    registry, error = _load_registry()
    if registry is None:
        return None, error
    try:
        matches = registry.filter(provider=provider, model=model).models
    except Exception as exc:
        return None, f"price lookup failed: {exc}"
    prices = [m.price for m in matches if m.price is not None]
    if not prices:
        return None, (f"no price entry for model {model!r} from provider "
                      f"{provider!r} in the rates snapshot")
    first = prices[0]
    for other in prices[1:]:
        if other.units != first.units or other.currency != first.currency:
            return None, (f"model {model!r} from provider {provider!r} has "
                          "more than one price entry in the rates snapshot; "
                          "no single estimate applies")
    return first, None


def estimate(provider: str, model: str,
             tokens_in: int, tokens_out: int) -> Dict[str, Any]:
    """
    Estimated cost of one model call.

    Returns {"ok": True, "cost", "currency", "snapshot_date", "provider",
    "model"} on success, or {"ok": False, "error": message} when the pair
    has no usable price. Only input and output tokens are priced — cache
    and reasoning tokens have no traceact field convention yet, so a price
    covering them wouldn't have counts to apply to.
    """
    price, error = _price_for(provider.lower(), model.lower())
    if price is None:
        return {"ok": False, "error": error}
    units = price.units or {}
    if tokens_in > 0 and "input_mtok" not in units:
        return {"ok": False,
                "error": (f"the rates snapshot has no input-token price for "
                          f"{model!r} from {provider!r}")}
    if tokens_out > 0 and "output_mtok" not in units:
        return {"ok": False,
                "error": (f"the rates snapshot has no output-token price for "
                          f"{model!r} from {provider!r}")}
    cost = (tokens_in / _TOKENS_PER_UNIT * units.get("input_mtok", 0)
            + tokens_out / _TOKENS_PER_UNIT * units.get("output_mtok", 0))
    registry, _ = _load_registry()
    return {
        "ok": True,
        "cost": cost,
        "currency": price.currency,
        "snapshot_date": str(getattr(registry, "snapshot_date", "")),
        "provider": provider,
        "model": model,
    }
