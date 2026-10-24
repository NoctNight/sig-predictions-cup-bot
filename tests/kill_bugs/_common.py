"""Shared helpers for the injected bugs (exec'd by sim_kill_compare.py / sim_pipeline.py --patch with `mods`)."""
import json
import os
from datetime import datetime, timedelta, timezone


def ttl(sec=60):
    return (datetime.now(timezone.utc) + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%SZ")


def stop_pauses(sim) -> dict:
    """{key: entry} the strategy stops wrote into the sandbox kill file."""
    p = os.path.join(sim.sb, "params", "strategy_kill.json")
    if not os.path.exists(p):
        return {}
    return {k: e for k, e in json.load(open(p)).items() if isinstance(e, dict) and e.get("by") == "strategy_stops"}


def calls_after(sim, prefix, t):
    return [c for c in sim.v.calls if c.get("kind") == "order" and c.get("prefix") == prefix and c["t"] > t]


def round_trip(api, ex, bid, ask, qty, key, record=None):
    """Cross the spread both ways on ex (buy YES at the ask, buy NO at 1 - bid): loses the spread on qty shares."""
    out = []
    for side, px in (("yes", round(ask, 3)), ("no", round(1 - bid, 3))):
        try:
            r = api.place_order(ex, side, "buy", qty, px, expiration=ttl(60), key=api.new_key(key))
            out.append((r.get("orderId"), side, px))
            if record is not None:
                record(r.get("orderId"), ex, side, px, qty)
        except Exception:
            pass
    return out
