"""Paused strategies still exit, nothing else re-buys what they sell, pauses survive a restart (strategy_stops.py (c)).

    python3 tests/sim_pipeline.py catchup cheapfav churn churn_restart calm --patch=tests/stops_pause_check.py
    STOPS_PAUSE=all python3 tests/sim_pipeline.py ... --patch=tests/stops_pause_check.py      # KILL-ENTRIES instead

A --patch for sim_pipeline.py. Mid-run, each sleeve is paused through params/strategy_kill.json the way the strategy
stops write it, as soon as it holds something: high-frequency arb (churn lots), catch-up, cheap favourites, and the MM
at cycle 20 (STOPS_PAUSE=all: every entry at once, at the first of those moments). Pause times are kept when the
scenario builds a new Bot over the same files (churn_restart), so a pause must survive the restart. Per scenario:
  entries after a pause (+60 s for orders in flight): must be 0
  exits after a pause: reported (a sleeve holding something must still close it)
  re-buys: shares bought, in the direction a paused sleeve was holding, on a leg it exited after the pause, by any
           other order within 30 min of that exit: must be 0
Prints PAUSECHECK lines; PAUSECHECK FAIL on a broken rule.
"""
import atexit
import collections
import json
import logging
import os
import time

R = mods["run"]  # noqa: F821  (sim_pipeline --patch provides mods)
MODE = os.environ.get("STOPS_PAUSE", "each")
ENTRY = {"mm": ("bat",), "catchup": ("cu",), "cheapfav": ("cf",)}
EXIT = {"catchup": ("cux",), "cheapfav": ("cfx",), "mm": ("exit",), "hf_arb": ("unw",)}
ST = {"paused": {}, "ev": [], "bot": None, "venue": None, "n": 0}


class _H(logging.Handler):
    def emit(self, rec):
        m = rec.getMessage()
        if m.startswith("CHURN enter") or m.startswith("CHURN exit") or "sets no longer held" in m:
            ST["ev"].append((time.time(), m[:120]))


logging.getLogger("sigbot").addHandler(_H())


def write(name):
    kp = os.path.join(R.HERE, "params", "strategy_kill.json")
    try:
        d = json.load(open(kp))
    except Exception:
        d = {}
    now = time.time()
    d[name] = {"ts": now, "utc": "sim", "reason": "sim pause (stops_pause_check)", "by": "strategy_stops", "rule": "sim"}
    tmp = kp + ".tmp"
    json.dump(d, open(tmp, "w"))
    os.replace(tmp, kp)
    ST["paused"][name] = now
    print(f"PAUSE {name} at {time.strftime('%H:%M:%S', time.gmtime(now))}", flush=True)


_cyc = R.Bot.cycle


def cycle(self):
    v = getattr(self.api, "v", None)
    if v is not ST["venue"]:                    # a new scenario (a restart keeps the scenario's venue and its pauses)
        report()
        ST.update(paused={}, ev=[], n=0, venue=v)
    if ST["bot"] is not self and ST["paused"]:
        print(f"PAUSECHECK restart: a new Bot over the same files with {sorted(ST['paused'])} paused", flush=True)
    ST["bot"] = self
    _cyc(self)
    ST["n"] += 1
    want = []
    if self.churn is not None and self.churn.lots:
        want.append("hf_arb")
    if getattr(self, "catchup_open", None):
        want.append("catchup")
    if getattr(self, "cheapfav_open", None):
        want.append("cheapfav")
    if ST["n"] >= 20:
        want.append("mm")
    if MODE == "all":
        if want and "all" not in ST["paused"]:
            write("all")
            for k in ("hf_arb", "catchup", "cheapfav", "mm"):
                ST["paused"][k] = ST["paused"]["all"]
    else:
        for k in want:
            if k not in ST["paused"]:
                write(k)


R.Bot.cycle = cycle


def report():
    v = ST["venue"]
    if v is None or not ST["paused"]:
        return
    bad = []
    out = []
    for k, tp in sorted(ST["paused"].items()):
        if k == "all":
            continue
        if k == "hf_arb":
            ent = [m for t, m in ST["ev"] if m.startswith("CHURN enter") and t > tp + 1]
        else:
            ent = [c for c in v.calls if c.get("kind") == "order" and c.get("prefix") in ENTRY.get(k, ()) and c["t"] > tp + 60]
        ex_fills = [m for m in v.fill_meta.values() if m["prefix"] in EXIT.get(k, ()) and m["t"] > tp]
        if k == "hf_arb":
            n_exit = sum(1 for t, m in ST["ev"] if t > tp and (m.startswith("CHURN exit") or "no longer held" in m))
        else:
            n_exit = len({(m["ex"], m["t"]) for m in ex_fills})
        rebuy = collections.Counter()
        for f in ex_fills:
            held_side = "no" if f["side"] == "yes" else "yes"       # an exit buying YES closed held NO, and vice versa
            for g in v.fill_meta.values():
                if g["ex"] == f["ex"] and g["side"] == held_side and f["t"] < g["t"] <= f["t"] + 1800 and \
                        g["prefix"] not in EXIT.get(k, ()) and g["prefix"] not in ("seed",):
                    rebuy[g["prefix"]] += g["qty"]
        out.append(f"{k}: entries after pause {len(ent)}, exits after {n_exit}, re-bought by others {dict(rebuy) or 0}")
        if ent:
            bad.append(f"{k} entered after its pause: {ent[:2]}")
        if rebuy:
            bad.append(f"{k}: what it sold was bought back by {dict(rebuy)}")
    print("PAUSECHECK", "; ".join(out), flush=True)
    for b in bad:
        print("PAUSECHECK FAIL", b, flush=True)


atexit.register(report)
