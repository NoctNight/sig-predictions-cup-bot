"""Behaviour tests for sigbot/strategy_monitor.py. Offline: synthetic fills, arb trades, quotes, log lines and ledgers in
temporary directories; never touches data/ or params/.

    python3 tests/test_strategy_monitor.py

For each sleeve: a losing record -> kill, a flat one -> ok, a winning one -> ok; plus no kill below the minimum sample,
kills persisted in the kill file, a human re-enables by deleting the entry (the test then restarts from that moment),
and the rule's false-alarm rate on a short simulation.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sigbot.strategy_monitor as SM  # noqa: E402
from sigbot.store import Store  # noqa: E402

FAILS = []
TICK = 0.005
T0 = datetime(2026, 10, 24, 0, 0, tzinfo=timezone.utc).timestamp()
NOISE = [-1.5, -0.5, 0.0, 0.5, 1.5]                   # symmetric, mean 0, sd ~1.1


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def logline(t, msg):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + ",%03d INFO sigbot: %s\n" % (int((t % 1) * 1000), msg)


class Env:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="smon_")
        self.params = os.path.join(self.dir, "params")
        os.makedirs(self.params)
        self.log = os.path.join(self.dir, "sigbot.log")
        open(self.log, "w").close()
        self.store = Store(os.path.join(self.dir, "t.sqlite"))
        self.store.exec("CREATE TABLE IF NOT EXISTS venue_quotes (ts REAL, venue TEXT, race TEXT, party TEXT, instrument TEXT, "
                        "bid REAL, ask REAL, mid REAL, bid_sz REAL, ask_sz REAL, depth_usd REAL, latency_ms REAL)")
        self.store.exec("CREATE TABLE IF NOT EXISTS venue_fair (ts REAL, race TEXT, party TEXT, pm REAL, kalshi REAL, fair REAL, "
                        "pm_w REAL, k_w REAL, sig_bid REAL, sig_ask REAL, sig_mid REAL, exchange_id TEXT)")
        self.fid = 0

    def fill(self, ex, qty, price, t, purpose):
        self.fid += 1
        self.store.exec("INSERT INTO fills(id,order_id,exchange_id,side,price,quantity,filled_at,purpose,ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.fid, self.fid, str(ex), "yes" if qty > 0 else "no", price, qty, iso(t), purpose, t))

    def write_log(self, lines):
        with open(self.log, "a") as f:
            f.writelines(lines)

    def evaluate(self, now, write=True):
        return SM.evaluate(self.store, now, params_dir=self.params, log_path=self.log, write=write)

    def kills(self):
        p = os.path.join(self.params, SM.KILL_FILE)
        return json.load(open(p)) if os.path.exists(p) else {}

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# ------------------------------------------------------------------------------------------------------------- builders
def mm_env(mean, n, env=None, start=T0):
    env = env or Env()
    for i in range(n):
        t = start + i * 120
        ex = str(1000 + env.fid)                        # a different market per fill: no merging
        env.fill(ex, 100, 0.50, t, "mm")
        m = mean + NOISE[i % len(NOISE)]
        env.store.exec("INSERT INTO quotes(ts,exchange_id,mid) VALUES(?,?,?)", (t + 1805, ex, 0.50 + m * TICK))
    return env


def catchup_env(mean, n):
    env = Env()
    for i in range(n):
        t = T0 + i * 1800
        d = mean + NOISE[i % len(NOISE)]
        env.fill("77", 300, 0.40, t, "catchup")
        env.fill("77", -300, 0.40 + d * TICK, t + 600, "catchup_exit")
    return env


def hf_env(mean, n_exits, open_lot_ticks=None, lot_age=0.0):
    env = Env()
    lines = []
    for i in range(n_exits):
        t = T0 + i * 3600
        d = mean + NOISE[i % len(NOISE)]
        pnl = 1000 * d * TICK
        lines.append(logline(t, f"CHURN enter Race{i} x1000 at cost 1.0000/set (edge 0.0100, overround +0.030 vs 24-h median +0.010)"))
        if i % 5 == 4:   # the stack's formats: a partial exit, and ledger sets another path closed booked as an exit
            lines.append(logline(t + 1800, f"CHURN Race{i}: 1000 sets no longer held (closed by another path), booked as an exit at "
                                            f"{1 + d * TICK:.4f}/set (recorded unwind): {pnl:+.1f}, realised so far 0.0"))
        else:
            lines.append(logline(t + 1800, f"CHURN exit Race{i} x1000 of 1200 at {1 + d * TICK:.4f}/set (round trip +2 ticks): {pnl:+.1f}, "
                                            "realised so far 0.0"))
    env.write_log(lines)
    lots = []
    if open_lot_ticks is not None:
        now = T0 + max(n_exits * 3600, 7 * 3600)
        lots.append({"race": "Open Race", "qty": 500, "cost": 1.0, "ts": now - lot_age})
        for party, ask in (("D", 0.50), ("R", 0.50 - open_lot_ticks * TICK)):   # proceeds = 2 - sum(asks) = 1 + ticks
            env.store.exec("INSERT INTO venue_quotes(ts,venue,race,party,instrument,ask) VALUES(?,?,?,?,?,?)",
                           (now - 60, "sig", "Open Race", party, party, ask))
    json.dump({"lots": lots, "realised": 0.0}, open(os.path.join(env.params, "churn_state.json"), "w"))
    return env


def recycle_env(mean_pct, n, extras=True):
    """Each recycle (40 min apart, so no two form one burst): unwind 1000 sets of A (forgone 0.02/set, proceeds 1.90) and a follow-through basket in B 2 s later
    whose edge makes the gain mean_pct (% of the 1,900 freed) plus noise."""
    env = Env()
    lines = []
    for i in range(n):
        t = T0 + i * 2400
        g = (mean_pct + 0.3 * NOISE[i % len(NOISE)]) / 100.0
        edge = (g * 1900 + 20) / 1000
        env.store.record_arb("A", "unwind_set", [], 1000, -0.02, 1.90, True, {})
        env.store.exec("UPDATE arb_trades SET ts=? WHERE id=(SELECT MAX(id) FROM arb_trades)", (t,))
        env.store.record_arb("B", "sell_set", [], 1000, edge, 1.90, True, {})
        env.store.exec("UPDATE arb_trades SET ts=? WHERE id=(SELECT MAX(id) FROM arb_trades)", (t + 2,))
        lines.append(logline(t - 1, "RECYCLE A: unwind 1000 sets at proceeds 1.9000/set (forgone ret 1.05%) to fund B sell_set at 3% (gain 1%)"))
    if extras:   # a churn exit, an offload and a free unwind must not count as recycles; a churn entry is not a follow-through
        for k, (msg, kind, edge) in enumerate((("UNWIND (churn: 24 h maximum hold) A: 700 sets at proceeds 1.0000/set, gives up 1%", "unwind_set", -0.5),
                                                ("UNWIND (fund tilt 12) A: 800 sets at proceeds 1.9000/set, gives up 1%", "unwind_set", -0.5),
                                                ("FREE UNWIND A: 900 sets at proceeds 2.0000/set >= payout 1", "unwind_set", 0.0))):
            t = T0 + n * 2400 + k * 900
            env.store.record_arb("A", kind, [], 700 + 100 * k, edge, 1.0, True, {})
            env.store.exec("UPDATE arb_trades SET ts=? WHERE id=(SELECT MAX(id) FROM arb_trades)", (t,))
            lines.append(logline(t - 1, msg))
        t = T0 + 1                                     # a churn entry right after the first recycle: must not be credited
        env.store.record_arb("C", "sell_set", [], 600, 5.0, 1.0, True, {})     # requested 600, booked 450 (partial fill)
        env.store.exec("UPDATE arb_trades SET ts=? WHERE id=(SELECT MAX(id) FROM arb_trades)", (t,))
        lines.append(logline(t + 3, "CHURN enter C x450 of 600 (450 traded: partial fill or netted against YES held on a leg) at cost "
                                    "1.0000/set (edge 0.0100, overround +0.030 vs 24-h median +0.010)"))
    env.write_log(lines)
    return env


def cheapfav_env(mean, legs, mark_ticks=None):
    env = Env()
    now = T0 + 6 * 3600
    for i in range(legs):
        ex = str(500 + i)
        env.fill(ex, 200, 0.80, T0 + i * 60, "cheapfav")
        d = mean + NOISE[i % len(NOISE)]
        m = d if mark_ticks is None else mark_ticks
        env.store.exec("INSERT INTO venue_fair(ts,race,party,fair,sig_mid,exchange_id) VALUES(?,?,?,?,?,?)",
                       (now - 120, f"R{i}", "D", 0.80 + d * TICK, 0.80 + m * TICK, ex))
    return env, now


# ---------------------------------------------------------------------------------------------------------------- tests
def three_way(name, build, now_fn, strat):
    out = {}
    for tag, mean in (("losing", -1.0), ("flat", 0.0), ("winning", 1.0)):
        env = build(mean)
        r = env.evaluate(now_fn(env))
        out[tag] = (r[strat]["verdict"], r[strat], env.kills())
        env.close()
    check(f"{name}: losing -> kill (written to the kill file)", out["losing"][0] == "kill" and strat in out["losing"][2],
          str(out["losing"][1]))
    check(f"{name}: flat -> ok", out["flat"][0] == "ok" and strat not in out["flat"][2], str(out["flat"][1]))
    check(f"{name}: winning -> ok", out["winning"][0] == "ok" and strat not in out["winning"][2], str(out["winning"][1]))
    check(f"{name}: a losing {name} kills no other sleeve", set(out["losing"][2]) == {strat}, str(out["losing"][2]))


three_way("mm", lambda m: mm_env(m, 60), lambda e: T0 + 60 * 120 + 3600, "mm")
three_way("catchup", lambda m: catchup_env(m, 30), lambda e: T0 + 30 * 1800 + 3600, "catchup")
three_way("hf_arb", lambda m: hf_env(m, 30), lambda e: T0 + 31 * 3600, "hf_arb")
three_way("recycle", lambda m: recycle_env(m, 45), lambda e: T0 + 47 * 2400, "recycle")
three_way("cheapfav", lambda m: cheapfav_env(m * 4, 12)[0], lambda e: T0 + 6 * 3600, "cheapfav")

# metric details
e = mm_env(-2.0, 1)
r = e.evaluate(T0 + 3600, write=False)
check("mm: markout = (mid 30 min later - our price) in ticks, signed for a YES buy", r["mm"]["n"] == 1 and abs(r["mm"]["mean"] - (-3.5)) < 1e-6,
      str(r["mm"]))
e.close()
e = Env()
for k in range(3):                                     # a 3-fill sweep on one market and side within 60 s = one observation
    e.fill("9", 100, 0.50, T0 + 10 * k, "mm")
e.store.exec("INSERT INTO quotes(ts,exchange_id,mid) VALUES(?,?,?)", (T0 + 1830, "9", 0.51))
r = e.evaluate(T0 + 7200, write=False)
check("mm: fills of one sweep on one market are merged into one observation", r["mm"]["n"] == 1 and abs(r["mm"]["mean"] - 2.0) < 1e-6,
      str(r["mm"]))
e.close()
e = Env()
for k, dt in enumerate((0, 1200, 2400)):              # 0 and 20 min share a window; 40 min starts a new one
    e.fill("9", 100 if k != 1 else -100, 0.50, T0 + dt, "mm")
    e.store.exec("INSERT INTO quotes(ts,exchange_id,mid) VALUES(?,?,?)", (T0 + dt + 1805, "9", 0.50))
r = e.evaluate(T0 + 9000, write=False)
check("mm: fills on one market within 30 min of the first are one observation (shared later mids), later ones a new one",
      r["mm"]["n"] == 2, str(r["mm"]))
e.close()
_ac = [(-1.0 if (k // 10) % 2 else 1.0) + 0.1 for k in range(200)]   # long runs: strongly autocorrelated
_sh = [(-1.0 if k % 2 else 1.0) + 0.1 for k in range(200)]         # same values, alternating
check("rule: an autocorrelated series counts as less evidence than the same values alternating (Newey-West variance)",
      SM.evidence(_ac, 0.1)["lrsd"] > 1.5 * SM.evidence(_sh, 0.1)["lrsd"] and SM.evidence(_sh, 0.1)["lrsd"] >= SM.evidence(_sh, 0.1)["sd"] - 1e-9)
e = Env()
e.fill("9", 100, 0.50, T0, "mm")
r = e.evaluate(T0 + 1000, write=False)
check("mm: a fill younger than 30 min is not scored yet", r["mm"]["n"] == 0)
e.close()

e = catchup_env(-2.0, 1)
e.fill("78", -400, 0.60, T0 + 5000, "catchup")        # still open: reported, not scored
e.store.exec("INSERT INTO venue_fair(ts,race,party,fair,sig_mid,exchange_id) VALUES(?,?,?,?,?,?)", (T0 + 5100, "X", "D", 0.6, 0.61, "78"))
r = e.evaluate(T0 + 5200, write=False)
check("catchup: closed trade scored in ticks per share; open position only reported at the SIG mid",
      r["catchup"]["n"] == 1 and abs(r["catchup"]["mean"] - (-3.5)) < 1e-6 and r["catchup"]["open_positions"] == 1
      and abs(r["catchup"]["open_pnl"] - (-4.0)) < 1e-6, str(r["catchup"]))
e.close()

e = hf_env(0.0, 0, open_lot_ticks=-3.0, lot_age=7 * 3600)
r = e.evaluate(T0 + 7 * 3600, write=False)
check("hf_arb: an open lot over 6 h old counts at what selling it back at the current asks fetches", r["hf_arb"]["n"] == 1 and
      abs(r["hf_arb"]["mean"] - (-3.0)) < 1e-6 and r["hf_arb"]["open_lots"] == 1, str(r["hf_arb"]))
e.close()
e = hf_env(0.0, 0, open_lot_ticks=-3.0, lot_age=3600)
r = e.evaluate(T0 + 7 * 3600, write=False)
check("hf_arb: a lot under 6 h old is reported but not tested (it would only show the spread as a loss)",
      r["hf_arb"]["n"] == 0 and r["hf_arb"]["young_lots"] == 1, str(r["hf_arb"]))
e.close()

e = recycle_env(0.5, 1)
r = e.evaluate(T0 + 6 * 3600, write=False)
check("recycle: churn exits, offloads and free unwinds are not recycles; a churn entry is not credited as follow-through",
      r["recycle"]["n"] == 1 and abs(r["recycle"]["mean"] - (0.5 - 0.45)) < 1e-6
      and r["recycle"]["unwinds_by_kind"] == {"recycle": 1, "churn": 1, "offload": 1, "free": 1, "unknown": 0}, str(r["recycle"]))
e.close()
e = Env()
for k in range(3):                                     # three unwinds of one race within 30 min: one decision
    e.store.record_arb("A", "unwind_set", [], 1000, -0.02, 1.90, True, {})
    e.store.exec("UPDATE arb_trades SET ts=? WHERE id=(SELECT MAX(id) FROM arb_trades)", (T0 + 300 * k,))
e.write_log([logline(T0 + 300 * k - 1, "RECYCLE A: unwind 1000 sets at proceeds 1.9000/set (forgone ret 1.05%) to fund B sell_set at 3% (gain 1%)")
             for k in range(3)])
r = e.evaluate(T0 + 3 * 3600, write=False)
check("recycle: a burst of unwinds of one race within 30 min is one observation", r["recycle"]["n"] == 1
      and r["recycle"]["recycle_unwinds"] == 3, str(r["recycle"]))
e.close()
e = Env()
e.store.record_arb("A", "unwind_set", [], 1000, -0.02, 1.90, True, {})
e.store.exec("UPDATE arb_trades SET ts=?", (T0,))
r = e.evaluate(T0 + 3600, write=False)
check("recycle: an unwind with no log line to say what it was is left out (never counted as a losing recycle)",
      r["recycle"]["n"] == 0 and r["recycle"]["unwinds_by_kind"]["unknown"] == 1, str(r["recycle"]))
e.write_log([logline(T0 - 1, "RECYCLE A: unwind 1000 sets at proceeds 1.9000/set (forgone ret 1.05%) to fund B sell_set at 3% (gain 1%)")])
r = e.evaluate(T0 + 3600, write=False)
check("recycle: an unwind with no follow-through costs its forgone profit (-20 on 1,900 freed)",
      r["recycle"]["n"] == 1 and abs(r["recycle"]["mean"] - (-100 * 20 / 1900)) < 1e-3, str(r["recycle"]))
r = e.evaluate(T0 + 60, write=False)
check("recycle: not scored while its 10-min follow-through window is open", r["recycle"]["n"] == 0)
e.close()

env, now = cheapfav_env(-6.0, 12, mark_ticks=+1.0)
r = env.evaluate(now)
check("cheapfav: edge clearly negative but mark P&L positive -> warn, not kill", r["cheapfav"]["verdict"] == "warn" and not env.kills(),
      str(r["cheapfav"]))
env.close()
env, now = cheapfav_env(-6.0, 6)
r = env.evaluate(now)
check("cheapfav: fewer than 8 legs -> no kill", r["cheapfav"]["verdict"] != "kill" and not env.kills(), str(r["cheapfav"]))
env.close()

# minimum sample
_m = SM.N_MIN - 1
_mr = SM.SPECS["recycle"]["n_min"] - 1
for name, build, now in (("mm", lambda: mm_env(-3.0, _m), T0 + _m * 120 + 3600), ("catchup", lambda: catchup_env(-3.0, _m), T0 + _m * 1800 + 3600),
                         ("hf_arb", lambda: hf_env(-3.0, _m), T0 + (_m + 1) * 3600),
                         ("recycle", lambda: recycle_env(-1.0, _mr, extras=False), T0 + (_mr + 1) * 2400)):
    env = build()
    r = env.evaluate(now)
    _k = _mr if name == "recycle" else _m
    check(f"{name}: {_k} clearly losing trades -> no kill (minimum {_k + 1})", r[name]["verdict"] == "ok" and not env.kills()
          and "not tested yet" in r[name]["reason"], str(r[name]))
    env.close()

# persistence, re-enable, KillFile
env = mm_env(-1.0, 60)
now = T0 + 60 * 120 + 3600
r1 = env.evaluate(now)
kf = SM.KillFile(os.path.join(env.params, SM.KILL_FILE))
check("kill file: entry has time, reason, n, mean and evidence; no temp file left", r1["mm"].get("new_kill") and
      set(env.kills()["mm"]) >= {"ts", "utc", "reason", "n", "mean", "stat"} and not any(f.endswith(".tmp") for f in os.listdir(env.params)))
check("KillFile: mm paused, the others not", kf.paused("mm") is not None and kf.paused("catchup") is None and kf.paused("hf_arb") is None)
mm_env(3.0, 200, env, start=now)                       # the sleeve then looks great: the kill still stands
r2 = env.evaluate(now + 200 * 120 + 3600)
check("persistence: a kill is never lifted by the bot, even when later results recover", r2["mm"]["verdict"] == "kill"
      and "only a human re-enables" in r2["mm"]["reason"] and "mm" in env.kills())
SM._log_cache.clear(); SM._markout_cache.clear()      # a restart: nothing in memory
check("persistence: survives a restart (read back from the file)", SM.KillFile(os.path.join(env.params, SM.KILL_FILE)).paused("mm") is not None
      and env.evaluate(now + 200 * 120 + 3700)["mm"]["verdict"] == "kill")
k = env.kills(); k.pop("mm"); json.dump(k, open(os.path.join(env.params, SM.KILL_FILE), "w"))   # Sizhe deletes the entry
t_re = now + 200 * 120 + 4000
r3 = env.evaluate(t_re)
st = json.load(open(os.path.join(env.params, SM.STATE_FILE)))
check("re-enable: deleting the entry re-enables the sleeve and restarts its test (old evidence cannot re-kill it)",
      r3["mm"]["verdict"] == "ok" and r3["mm"]["n"] == 0 and "mm" not in env.kills() and kf.paused("mm") is None
      and st["reenabled"][-1]["strategy"] == "mm", str(r3["mm"]))
mm_env(-1.0, 60, env, start=t_re + 10)
r4 = env.evaluate(t_re + 10 + 60 * 120 + 3600)
check("re-enable: new losses after the re-enable are tested afresh and kill again", r4["mm"]["verdict"] == "kill" and "mm" in env.kills(),
      str(r4["mm"]))
check("KillFile: the re-kill is seen by the sleeves", kf.paused("mm") is not None)
with open(os.path.join(env.params, SM.KILL_FILE), "w") as f:
    f.write("{ not json")                               # a half-edited file
os.utime(os.path.join(env.params, SM.KILL_FILE), (time.time() + 5, time.time() + 5))
check("KillFile: an unreadable file keeps the last good content (never re-enables by accident)", kf.paused("mm") is not None)
os.remove(os.path.join(env.params, SM.KILL_FILE))
check("KillFile: no file -> nothing paused", kf.paused("mm") is None)
env.close()

# review 24 Oct: hand-written entries, an unreadable file, manual pauses removed
env = mm_env(-1.0, 60)
kp = os.path.join(env.params, SM.KILL_FILE)
json.dump({"catchup": "manual pause", "hf_arb": True, "recycle": False, "cheapfav": None}, open(kp, "w"))
kf = SM.KillFile(kp)
r = env.evaluate(T0 + 60 * 120 + 3600)
check("kill file: non-dict entries pause (string, true) without crashing the monitor; false / null do not pause",
      kf.paused("catchup") == "manual pause" and kf.paused("hf_arb") == "True" and kf.paused("recycle") is None
      and kf.paused("cheapfav") is None and r["catchup"]["verdict"] == "kill" and r["recycle"]["verdict"] == "ok", str(r["catchup"]))
check("kill file: the monitor's new kill is added and hand-written entries are kept exactly as written",
      json.load(open(kp)).get("catchup") == "manual pause" and json.load(open(kp)).get("recycle") is False and "mm" in json.load(open(kp)))
k = json.load(open(kp)); k.pop("catchup"); json.dump(k, open(kp, "w"))
for i in range(40):                                    # 40 losing trades, all before the entry was removed
    env.fill("77", 300, 0.40, T0 + i * 120, "catchup")
    env.fill("77", -300, 0.39, T0 + i * 120 + 60, "catchup_exit")
r = env.evaluate(T0 + 60 * 120 + 7200)
check("kill file: removing a MANUAL pause also restarts that sleeve's test (old losses cannot re-pause it at once)",
      r["catchup"]["verdict"] == "ok" and r["catchup"]["n"] == 0 and "catchup" not in json.load(open(kp)), str(r["catchup"]))
with open(kp, "w") as f:
    f.write("{ half-edited")
st_before = json.load(open(os.path.join(env.params, SM.STATE_FILE)))["paused_seen"]
mm_env(-3.0, 80, env, start=T0 + 60 * 120 + 7300)
r = env.evaluate(T0 + 60 * 120 + 7300 + 80 * 120 + 3600)
st_after = json.load(open(os.path.join(env.params, SM.STATE_FILE)))["paused_seen"]
check("kill file unreadable: never rewritten, nothing treated as re-enabled, results still reported",
      open(kp).read() == "{ half-edited" and set(st_before) <= set(st_after) and "unreadable" in r["mm"].get("kill_file", ""), str(st_after))
env.close()

e = Env()
e.write_log([logline(T0, "CHURN enter R x1000 at cost 1.0000/set (edge 0.0100, overround +0.030 vs 24-h median +0.010)"),
             logline(T0 + 600, "CHURN enter R x500 of 800 (500 traded: partial fill or netted against YES held on a leg) at cost 1.0100/set (e)"),
             logline(T0 + 3600, "CHURN exit R x700 of 1500 at 1.0200/set (round trip +2 ticks): +14.0, realised so far 14.0"),
             logline(T0 + 3700, "CHURN R: 3 sets no longer held (closed by another path), booked as an exit at 0.9950/set (current exit value): -0.0, realised so far 14.0"),
             logline(T0 + 7200, "CHURN exit R x797 of 797 at 1.0150/set (24 h maximum hold): +3.0, realised so far 17.0")])
json.dump({"lots": [], "realised": 17.0}, open(os.path.join(e.params, "churn_state.json"), "w"))
r = e.evaluate(T0 + 8000, write=False)
exact = (700 * 0.02 + 300 * (1.015 - 1.0) * 0 + 0) if False else None
pnl = 700 * (1.02 - 1.0) + 3 * (0.995 - 1.0) + 297 * (1.015 - 1.0) + 500 * (1.015 - 1.01)
check("hf_arb: partial exits and a small reconcile form ONE round trip, quantity-weighted, P&L exact from the entry costs",
      r["hf_arb"]["n"] == 1 and abs(r["hf_arb"]["mean"] - pnl / 1500 / TICK) < 1e-3 and r["hf_arb"]["round_trips"] == 1,
      f"{r['hf_arb']} expected {pnl / 1500 / TICK:.3f}")
e.close()

e = Env()   # 25 Oct, BST -> GMT: the repeated hour gives a later line an earlier local stamp; file order must win
e.write_log([logline(T0 + 3000, "CHURN enter Q x1000 at cost 1.0000/set (edge 0.0100, overround +0.030 vs 24-h median +0.010)"),
             logline(T0 + 600, "CHURN exit Q x1000 of 1000 at 1.0100/set (round trip +2 ticks): +10.0, realised so far 10.0")])
json.dump({"lots": [], "realised": 10.0}, open(os.path.join(e.params, "churn_state.json"), "w"))
r = e.evaluate(T0 + 8000, write=False)
check("hf_arb: events are taken in log-file order (clock change), so an exit stamped before its entry still closes the round trip",
      r["hf_arb"]["n"] == 1 and abs(r["hf_arb"]["mean"] - 2.0) < 1e-6, str(r["hf_arb"]))
e.close()

e = recycle_env(-0.6, 40, extras=False)
r = e.evaluate(T0 + 42 * 2400, write=False)
check("recycle: stricter rule (bursts stay dependent): switched off only at evidence 500 and n >= 40",
      SM.verdict({"n": 39, "mean": -1.0, "L": 1e9}, n_min=SM.SPECS["recycle"]["n_min"])[0] == "ok"
      and r["recycle"]["verdict"] in ("kill", "warn", "ok") and (r["recycle"]["verdict"] != "kill" or r["recycle"]["stat"] >= 500)
      and "500" in SM.verdict({"n": 50, "mean": -1.0, "L": 600}, alpha_kill=SM.SPECS["recycle"]["alpha_kill"])[1], str(r["recycle"]))
e.close()
e = Env()
e.store.record_arb("A", "unwind_set", [], 1000, -0.02, 1.90, True, {})
e.store.exec("UPDATE arb_trades SET ts=?", (T0,))
e.write_log([logline(T0 - 1, "UNWIND (offload) A: 1000 sets at proceeds 1.9000/set, gives up 1.05%")])
r = e.evaluate(T0 + 7200, write=False)
check("recycle: an unwind logged for any other reason is left out, not assumed to be a recycle",
      r["recycle"]["n"] == 0 and r["recycle"]["unwinds_by_kind"]["unknown"] == 1, str(r["recycle"]))
e.close()

env = mm_env(-1.0, 60)
env.evaluate(T0 + 60 * 120 + 3600, write=False)
check("write=False (read-only report) writes no file", os.listdir(env.params) == [])
env.close()

env = Env()
r = env.evaluate(T0)
check("empty database: every sleeve ok, no kill", all(v["verdict"] == "ok" for v in r.values()) and set(r) == set(SM.SPECS) and not env.kills())
env.close()

# the rule itself
s = SM.simulate(0.0, 1.8, 400, 300, seed=11)
check("rule: zero-edge sleeve killed in <= 2% of 300 simulated runs of 400 trades", s["p_kill"] <= 0.02, str(s))
s = SM.simulate(0.0, 1.8, 400, 300, "t3", 0.3, seed=13)
check("rule: ... also with heavy tails and autocorrelation 0.3", s["p_kill"] <= 0.03, str(s))
s = SM.simulate(-0.5, 1.8, 400, 100, seed=12)
check("rule: a -0.5 tick sleeve (sd 1.8) is killed in >= 80% of runs within 400 trades", s["p_kill"] >= 0.8, str(s))
check("rule: evidence grows with a consistent loss and is 1 with no data", SM.evidence([], 1.0)["L"] == 1.0 and
      SM.evidence([-1.0] * 40, 1.0)["L"] > SM.evidence([-1.0] * 20, 1.0)["L"] > 1.0)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
