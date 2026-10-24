"""Old bot-wide kill switch vs per-strategy stops, side by side on the sim (strategy_stops.py acceptance (a) and (b)).

    python3 tests/sim_kill_compare.py churn_widen day                  # scenarios as they are
    python3 tests/sim_kill_compare.py recycle:tests/kill_bugs/pump.py  # with an injected bug (a --patch file)
    python3 tests/sim_kill_compare.py --bugs                           # every injected bug in tests/kill_bugs/

Each item runs twice in fresh sandboxes: strategy_stops_enabled false (today's switch: drawdown > kill_drawdown vs the
trailing-hour peak or MM markout, 30-min pause of everything) and true. A read-only observer (StrategyStops with
write=False on a scratch params dir) values every strategy's ledger after each cycle in both runs. Printed per run: when
the first trigger fired and what it paused, account P&L at the end, and the loss after the moment TODAY's switch first
fired (the same reference time for both runs), in total and for the strategy that lost most. A bug file may define
`caught(sim) -> (bool, why)` and `culprit` (a stop key): the new run must catch it.
Exit status 1 if a new run is slower than the old one to pause something, loses more after the old trigger (beyond
`TOL`), or misses a bug.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sim_pipeline as SP  # noqa: E402

REF: dict = {}       # scenario -> today's first trigger time (from the old run), for the decomposition
TOL = 25.0           # P&L noise allowed between the two runs (they diverge once either pauses anything)


def observer_values(sim, obs):
    b, v = sim.bot, sim.v
    touch = {ex: (SP.SV.P(v.bid_t[ex]) if v.bid_t.get(ex) is not None else None,
                  SP.SV.P(v.ask_t[ex]) if v.ask_t.get(ex) is not None else None) for ex in v.exs}
    L, untagged, _ = obs.ledgers()
    ch, cs = b.churn, b.catchup
    churn = None
    if ch is not None and getattr(ch, "last_open_value", None) is not None:
        churn = {"realised": ch.realised, "open": ch.last_open_value, "lots": list(ch.lots)}
    vals = obs.values(L, touch, churn)
    vals["untagged"] = sys.modules["sigbot.strategy_stops"].touch_value(untagged, touch)
    return vals


def run_one(name, patch, stops_on, mods, sb):
    SP.FORCE_CFG.clear()
    SP.FORCE_CFG["strategy_stops_enabled"] = stops_on
    S = next(s for s in SP.SCENARIOS if s.name == name)
    sc = S()
    sim = SP.Sim(sc, sb, mods, False)
    b = sim.bot
    STOPS = sys.modules["sigbot.strategy_stops"]
    obs_dir = tempfile.mkdtemp(prefix="stops-obs-")
    obs = STOPS.StrategyStops(b.cfg, b.store, obs_dir, [r.exchange_ids for r in b.universe.races.values()], write=False)
    kill_path = os.path.join(sb, "params", "strategy_kill.json")
    rec = {"t": [], "pnl": [], "vals": [], "trigger": None, "log": [], "mids": []}
    pat = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--log=")), None)
    if pat:
        import logging as _lg

        class _H(_lg.Handler):
            def emit(self, r):
                m = r.getMessage()
                if any(x in m for x in pat.split("|")):
                    rec["log"].append(f"{SP.datetime.fromtimestamp(SP.CLOCK.t, SP.timezone.utc).strftime('%H:%M:%S')} {m[:220]}")
        h = _H()
        _lg.getLogger("sigbot").addHandler(h)
        rec["handler"] = h
    orig = b.cycle

    def cycle():
        orig()
        t = SP.CLOCK.t
        rec["t"].append(t)
        rec["pnl"].append(sim.v.value() - sim.start_value)
        rec["vals"].append(observer_values(sim, obs))
        rec["mids"].append({ex: sim.v.mid(ex) for ex in sim.v.exs})
        if rec["trigger"] is None:
            if not stops_on and b.killed_until > t:
                rec["trigger"] = (t, ["ALL (30 min)"])
            elif stops_on:
                k = json.load(open(kill_path)) if os.path.exists(kill_path) else {}
                ours = sorted(n for n, e in k.items() if isinstance(e, dict) and e.get("by") == "strategy_stops")
                held = sorted(n for n, u in (getattr(getattr(b, "stops", None), "hold", None) or {}).items() if u > t)
                if ours or held:                # a first breach holds entries from that cycle (strategy_paused reads it)
                    rec["trigger"] = (t, ours + [f"{n} (held)" for n in held if n not in ours])
        if not stops_on and b.killed_until > t:
            rec.setdefault("old_windows", []).append(t)
    b.cycle = cycle
    chk = sim.run()
    if rec.get("handler") is not None:
        import logging as _lg
        _lg.getLogger("sigbot").removeHandler(rec["handler"])
    rec["violations"] = [x["kind"] for x in chk.viol]
    rec["sim"] = sim
    if stops_on and os.path.exists(kill_path):
        rec["kills"] = json.load(open(kill_path))
    try:
        b.store.conn.close()
    except Exception:
        pass
    return rec


def decompose(sim, ref):
    """Account P&L after `ref`: mark change of what was held at ref + each order prefix's fills after ref marked at the
    end (mid); the two parts sum to the account's change."""
    v = sim.v
    mid_end = {ex: v.mid(ex) for ex in v.exs}
    pos_ref, out = {}, {}
    for m in v.fill_meta.values():
        q = m["qty"] if m["side"] == "yes" else -m["qty"]
        if m["t"] < ref:
            pos_ref[m["ex"]] = pos_ref.get(m["ex"], 0.0) + q
        else:
            px_end = mid_end[m["ex"]] if m["side"] == "yes" else 1 - mid_end[m["ex"]]
            out[m["prefix"]] = out.get(m["prefix"], 0.0) + (px_end - m["price"]) * m["qty"]
    mid_ref = getattr(sim, "mid_at_ref", None)
    held = sum(q * (mid_end[ex] - mid_ref[ex]) for ex, q in pos_ref.items()) if mid_ref else None
    s = ", ".join(f"{k} {x:+.1f}" for k, x in sorted(out.items(), key=lambda kv: kv[1]) if abs(x) >= 0.5)
    return (f"held-at-ref marks {held:+.1f}; " if held is not None else "") + "fills after ref by prefix: " + s


def at(rec, t):
    """Index of the first record at or after t."""
    for i, x in enumerate(rec["t"]):
        if x >= t:
            return i
    return len(rec["t"]) - 1


def main(argv):
    items = [a for a in argv if not a.startswith("-")]
    if "--bugs" in argv:
        for f in sorted(g for g in glob.glob(os.path.join(HERE, "kill_bugs", "*.py")) if not os.path.basename(g).startswith("_")):
            src = open(f).read()
            scen = next((ln.split("=", 1)[1].strip().strip("\"'") for ln in src.splitlines() if ln.startswith("SCENARIO")), None)
            items.append(f"{scen}:{f}")
    SP.install_network_guard()
    bad = []
    for it in items:
        name, _, patch = it.partition(":")
        out = {}
        bugmod = {}
        for mode in (False, True):
            sb = SP.make_sandbox()
            mods = SP.import_sandbox(sb)
            venue_saved = {k: v for k, v in vars(SP.SV.Venue).items() if callable(v)}   # a bug may patch the shared venue
            bugmod = {"mods": mods, "__name__": "sim_patch", "__file__": os.path.abspath(patch) if patch else None}
            if patch:
                exec(compile(open(patch).read(), patch, "exec"), bugmod)
            import logging
            lg = logging.getLogger("sigbot")
            lg.setLevel(logging.INFO)
            lg.propagate = False
            if SP.CAP not in lg.handlers:
                lg.addHandler(SP.CAP)
            SP.install_clock([m for n, m in sys.modules.items() if n == "sigbot" or n.startswith("sigbot.")])
            try:
                if not mode:
                    REF.pop(name, None)
                elif out[False]["trigger"]:
                    REF[name] = out[False]["trigger"][0]
                out[mode] = run_one(name, patch, mode, mods, sb)
                if mode and "caught" in bugmod:
                    out[mode]["caught"] = bugmod["caught"](out[mode]["sim"])
            except Exception:
                print(f"HARNESS ERROR {it} stops={mode}:\n{traceback.format_exc()}")
                return 2
            finally:
                for k, v in venue_saved.items():
                    setattr(SP.SV.Venue, k, v)
                for k in [k for k in sys.modules if k == "sigbot" or k.startswith("sigbot.")]:
                    del sys.modules[k]
                sys.path.remove(sb)
        old, new = out[False], out[True]
        label = os.path.basename(patch)[:-3] if patch else ""
        print(f"=== {name}{' + bug ' + label if label else ''}")
        # reference time: today's first trigger; when today's switch never fires, the new run's first pause (what the new
        # design's own actions cost from then on must not exceed the old run over the same period)
        ref = old["trigger"][0] if old["trigger"] else (new["trigger"][0] if new["trigger"] else None)
        for tag, r in (("old", old), ("new", new)):
            trig = r["trigger"]
            line = f"  {tag}: end P&L {r['pnl'][-1]:+8.1f}"
            line += (f" | first trigger {SP.datetime.fromtimestamp(trig[0], SP.timezone.utc).strftime('%H:%M:%S')} {trig[1]}"
                     if trig else " | no trigger")
            if ref is not None:
                i = at(r, ref)
                line += f" | after today's trigger: {r['pnl'][-1] - r['pnl'][i]:+8.1f}"
                d = {k: r["vals"][-1].get(k, 0.0) - r["vals"][i].get(k, 0.0) for k in r["vals"][-1]}
                worst = min(d, key=d.get) if d else None
                if worst:
                    line += f" (worst {worst} {d[worst]:+.1f})"
                if "-v" in sys.argv:
                    line += "\n        by strategy after: " + ", ".join(f"{k} {x:+.1f}" for k, x in sorted(d.items()) if abs(x) >= 0.5)
                    line += "\n        by strategy whole run: " + ", ".join(
                        f"{k} {r['vals'][-1].get(k, 0.0) - r['vals'][0].get(k, 0.0):+.1f}" for k in sorted(r["vals"][-1])
                        if abs(r["vals"][-1].get(k, 0.0) - r["vals"][0].get(k, 0.0)) >= 0.5)
            if ref is not None and "-v" in sys.argv:
                r["sim"].mid_at_ref = r["mids"][at(r, ref)]
                line += "\n        account after: " + decompose(r["sim"], ref)
            for ln in r["log"]:
                line += "\n        | " + ln
            if r["violations"]:
                line += f" | violations {sorted(set(r['violations']))}"
            print(line)
        if new.get("kills"):
            for k, e in sorted(new["kills"].items()):
                if isinstance(e, dict) and e.get("by") == "strategy_stops":
                    print(f"      paused {k} at {e.get('utc')} [{e.get('rule')}]: {str(e.get('reason'))[:150]}")
                else:
                    print(f"      paused {k} by the strategy monitor at {(e or {}).get('utc') if isinstance(e, dict) else '?'}: "
                          f"{str((e or {}).get('reason') if isinstance(e, dict) else e)[:120]}")
        if "caught" in new:
            ok, why = new["caught"]
            print(f"  bug caught: {'YES' if ok else 'NO'} - {why}")
            if not ok:
                bad.append(f"{it}: bug not caught")
        if ref is not None:
            if old["trigger"] and (new["trigger"] is None or new["trigger"][0] > ref):
                bad.append(f"{it}: new design paused nothing by today's trigger time")
            i_o, i_n = at(old, ref), at(new, ref)
            lo, ln = old["pnl"][-1] - old["pnl"][i_o], new["pnl"][-1] - new["pnl"][i_n]
            if bugmod.get("NOT_A_BUG"):
                print(f"  note: not a bug (acceptance e): after today's trigger {ln - lo:+.1f} vs today's switch, which "
                      f"pauses every strategy on any 400 drop")
            elif ln < lo - TOL and old["trigger"]:
                bad.append(f"{it}: lost {ln - lo:+.1f} more after today's trigger than today's switch")
            elif ln < lo - TOL:
                print(f"  note: today's switch never fires here; the pause cost {ln - lo:+.1f} vs not pausing")
    print("\nRESULT:", "OK" if not bad else "PROBLEMS")
    for x in bad:
        print("  -", x)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
