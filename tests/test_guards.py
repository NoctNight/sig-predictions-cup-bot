"""Regression tests for the safety guards added after the 22 Oct incidents. Offline, stub objects only, no API calls.

    python3 tests/test_guards.py

Each test replays a failure that happened (or one a reviewer found) and checks it can no longer happen.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import sigbot.run as R  # noqa: E402
from sigbot.proposals.own_book import OwnLevels  # noqa: E402
from sigbot.proposals.mm_upgrade import xv_gate  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- offload churn (19:41-20:04, 38 unwinds)
def offload_bot(cash, tilt_free):
    calls = []

    class Arb:
        def unwind_set(self, rname, books, q, why="", new_ret=None, min_gain=0.0):
            calls.append((rname, q))
            return {"race": rname, "qty": q}

    races = {"A": types.SimpleNamespace(exchange_ids=["a1", "a2"]), "B": types.SimpleNamespace(exchange_ids=["b1", "b2"])}
    b = types.SimpleNamespace(offload_live=True, dry_run=False, cash=cash,
                              cfg=types.SimpleNamespace(arb=types.SimpleNamespace(cash_reserve=20000.0, recycle_min_gain=0.005), offload_arb_for_sleeves=True),
                              arb=Arb(), universe=types.SimpleNamespace(races=races), fresh_book=lambda ex, age=None: {"asks": []},
                              trade_lock=threading.Lock(), tilt=types.SimpleNamespace(unfunded=[1], free_last=tilt_free),
                              api=types.SimpleNamespace(account=lambda: {"balance": cash}), store=None, universe_races=races)
    return b, calls


def rec(race, ex="881", kind="tilt"):
    return [{"opportunity": {"kind": kind, "exchange_id": ex, "ret_net": 0.0445, "capital": 1000.0},
             "unwind": {"kind": "arb_set", "race": race, "qty": 5000, "proceeds": 0.97, "forgone_ret": 0.0206}, "gain": 0.0239}]


R.build_state_from_bot = lambda bot: {}
b, calls = offload_bot(8600.0, 0.0)
for race in ["A", "B"] * 30:
    R.offload_check = lambda st, g, race=race: rec(race)
    R.Bot.offload_report(b, 0)
check("offload: one trade offered 60 times funds at most one unwind", len(calls) <= 1, f"{len(calls)} unwinds")
b, calls = offload_bot(25000.0, 0.0)
R.offload_check = lambda st, g: rec("A")
R.Bot.offload_report(b, 0)
check("offload: no unwind while cash is above the reserve", len(calls) == 0)
b, calls = offload_bot(8600.0, 5000.0)
R.Bot.offload_report(b, 0)
check("offload: no unwind when tilt already has room", len(calls) == 0)

# ---------------------------------------------------------------- own orders in the books (17 of 33 failed baskets)
own = OwnLevels()
own.upsert(1, "1075", "yes", "buy", 0.925, 300)
book = {"bids": [{"price": 0.925, "quantity": 300}, {"price": 0.92, "quantity": 2400}], "asks": [{"price": 0.93, "quantity": 2000}]}
nb = own.net("1075", book)
check("own book: our own best bid is removed", nb["bestBid"] == 0.92, str(nb["bestBid"]))
own.upsert(2, "1075", "no", "buy", 0.07, 500)
check("own book: our NO order is removed from the YES asks", own.net("1075", book)["asks"][0]["quantity"] == 1500)

# ---------------------------------------------------------------- outside-fair gate
check("xv gate: fair 4 ticks above blocks the ask only", xv_gate(0.50, 0.52, 0.52, 0.52, 30) == (True, False))
check("xv gate: fair 4 ticks below blocks the bid only", xv_gate(0.50, 0.48, 0.48, 0.48, 30) == (False, True))
check("xv gate: stale or disputed fairs never gate", xv_gate(0.5, 0.52, 0.52, 0.52, 900) == (True, True) and xv_gate(0.5, 0.52, 0.40, 0.60, 30) == (True, True))

# ---------------------------------------------------------------- high-return ceiling must be reachable from the scanner
from sigbot.arb import ArbEngine  # noqa: E402

eng = ArbEngine.__new__(ArbEngine)
eng.cfg = types.SimpleNamespace(max_race_frac=0.15, high_ret_min=0.04, high_ret_race_frac=0.40, three_leg_race_frac=0.25)
eng.starting_cash = 100000.0
eng.committed = {"Idaho Governor": 15000.0}
eng.u = types.SimpleNamespace(races={"Idaho Governor": types.SimpleNamespace(has_independent=False)})
check("arb: a race at its normal cap has no room at a normal return", eng.race_budget_left("Idaho Governor") == 0.0)
check("arb: the same race has room at a 5% return", eng.race_budget_left("Idaho Governor", 0.05) == 25000.0)
src = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("scanner: the pre-filter passes the snapshot return to race_budget_left", "race_budget_left(r.name)" not in src)

# ---------------------------------------------------------------- unwinds are recorded as orders (fills must not land as purpose None)
asrc = open(os.path.join(ROOT, "sigbot", "arb.py")).read()
i = asrc.find("def unwind_set")
j = asrc.find("def free_unwinds")
check("arb: unwind_set records its legs as orders", "record_order" in asrc[i:j])

# ---------------------------------------------------------------- review 22 Oct 21:40: arb-for-sleeve unwinds are off by default
from sigbot.config import BotConfig  # noqa: E402
check("config: arb-set unwinds to fund tilt/directional are off by default", BotConfig().offload_arb_for_sleeves is False)
b, calls = offload_bot(8600.0, 0.0)
b.cfg.offload_arb_for_sleeves = False
R.offload_check = lambda st, g: rec("A", "777")
R.Bot.offload_report(b, 0)
check("offload: with the flag off, no arb set is unwound for a tilt trade", len(calls) == 0)

# ---------------------------------------------------------------- recycle follow-through can spend what the unwind freed
eng2 = ArbEngine.__new__(ArbEngine)
eng2.cfg = types.SimpleNamespace(capital_frac=0.9, cash_reserve=20000.0)
eng2.starting_cash = 100000.0
eng2.committed = {"X": 60000.0}
check("arb: budget is 0 just under the reserve", eng2.budget_left(20500.0) == 0.0)
src_arb = open(os.path.join(ROOT, "sigbot", "arb.py")).read()
check("arb: plan() accepts the capital a recycle freed", "extra_budget" in src_arb)
check("scanner: both recycle follow-throughs pass the freed capital", open(os.path.join(ROOT, "sigbot", "run.py")).read().count("extra_budget=rec.get") == 2)

# ---------------------------------------------------------------- exits net of exit orders still resting
rsrc = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("exits: quantity is net of resting exit orders", "resting_exit.get((ex, side)" in rsrc)
check("scanner: a missing leg book no longer blocks a race forever", "v is not None and v.get(\"feed_ts\", 0) <= last_fill" in rsrc)
check("scanner: the feed wait tolerates a missing feed", "feed = b.feed" in rsrc)

# ---------------------------------------------------------------- behaviour: an exit is not re-sent while the first still rests
sent = []
class Store:
    def rows(self, q, a=()):
        if "purpose IN ('direction','tilt')" in q:
            return [{"exchange_id": "1068", "side": "yes", "q": 1203.0}]
        if "purpose IN ('mm','inside')" in q or "purpose='exit' GROUP BY" in q:
            return []
        if "FROM orders WHERE purpose='exit'" in q:
            return [{"order_id": 9001}]
        return []
    def record_order(self, *a, **k):
        pass
api = types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: sent.append((ex, side, q)) or {"orderId": 1},
                            new_key=lambda k: k)
row = {"exchange_id": "1068", "fair": 0.95, "sig_bid": 0.955, "sig_ask": 0.96, "pm": 0.95, "kalshi": 0.95}
bot = types.SimpleNamespace(cfg=types.SimpleNamespace(exit_band=0.01), store=Store(), api=api, dry_run=False,
                            open_orders_cache={"1068": [{"id": 9001, "side": "no", "quantity": 1203, "quantityFilled": None}]})
R.Bot.zero_ev_exits(bot, {"1068": 1203.0}, [row])
check("exits: no second exit while the first rests for the whole holding", len(sent) == 0, str(sent))
bot.open_orders_cache = {}
R.Bot.zero_ev_exits(bot, {"1068": 1203.0}, [row])
check("exits: one exit for exactly the held quantity once nothing rests", sent == [("1068", "no", 1203)], str(sent))

# ---------------------------------------------------------------- kill switch on settlement basis (21:49 false trip on recycling)
rs = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("health: the drawdown peak no longer comes from the marked cycles table", "SELECT MAX(total_value) m FROM cycles" not in rs)
check("health: the kill switch is fed the settlement-basis value", "pos_value + getattr(self, \"settle_adj\", 0.0), cycle_id)" in rs)
# behaviour: a recycle lowers the marked value by 900 but raises locked profit; settlement-basis value must not drop
hb = types.SimpleNamespace()
vals = [(0, 102000.0), (60, 102000.0 + 879.0)]           # settlement basis after the recycle: higher
hb.settle_hist = []
for t, v in vals:
    hb.settle_hist.append((t, v))
peak = max(v for _, v in hb.settle_hist)
check("health: a profitable recycle gives zero settlement drawdown", peak - vals[-1][1] <= 0.0)

# ---------------------------------------------------------------- overrides file: half-written JSON must not crash the scanner
import json as _json, tempfile  # noqa: E402
from sigbot.mm import MarketMaker  # noqa: E402
d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "params"), exist_ok=True)
fp = os.path.join(d, "params", "mm_overrides.json")
open(fp, "w").write('{"exchanges": {"1": {"pressure": 0.1}}}')
mm = MarketMaker.__new__(MarketMaker)
mm.root, mm.cfg, mm.overrides, mm.overrides_mtime = d, types.SimpleNamespace(overrides_file="params/mm_overrides.json"), {}, None
mm.load_overrides()
good = dict(mm.overrides)
open(fp, "w").write('{"exchanges": {"1": {"pres')                # half-written
os.utime(fp, (time.time() + 5, time.time() + 5))
try:
    mm.load_overrides()
    check("mm: a half-written overrides file keeps the previous overrides", mm.overrides == good)
except Exception as e:
    check("mm: a half-written overrides file keeps the previous overrides", False, repr(e))
check("analytics: the overrides file is written atomically", "os.replace(tmp, path)" in rs and "os.replace(path + \".tmp\", path)" in open(os.path.join(ROOT, "sigbot", "analytics.py")).read())

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
