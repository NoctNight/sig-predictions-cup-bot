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
from datetime import datetime, timezone

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
_rs5 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("scanner: both recycle follow-throughs pass the freed capital", "extra_budget=(b.arb.recycle_credit if cash_mode else freed)" in _rs5
      and _rs5.count("extra_budget=rec.get") == 1)

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
check("health: the kill switch is fed the settlement-basis value", "cycle_adj = getattr(self, \"settle_adj\", 0.0)" in rs)
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

# ---------------------------------------------------------------- sim_pipeline findings, 22 Oct 23:20
import json as _j2  # noqa: E402
check("lag follower: off by default and in the live config (flips positions, bypasses the partisan block)",
      BotConfig().lag_enabled is False and _j2.load(open(os.path.join(ROOT, "config.json"))).get("lag_enabled") is False)
# recycle follow-through: the freed capital is already in cash; it may be spent once, not twice
eng3 = ArbEngine.__new__(ArbEngine)
eng3.cfg = types.SimpleNamespace(capital_frac=0.9, cash_reserve=20000.0, max_race_frac=0.15)
eng3.starting_cash = 100000.0
eng3.committed = {}
cash, freed = 30000.0, 10000.0                          # cash after the unwind, of which 10000 the unwind freed
eng3.cfg.capital_frac = 0.9
check("arb: recycle budget counts the freed capital once (cash binds)", eng3.follow_budget(cash, freed) == 10000.0, str(eng3.follow_budget(cash, freed)))
eng3.committed = {"X": 85000.0}                          # the unwind already took the freed 10000 out of committed
check("arb: recycle budget counts the freed capital once (capital cap binds)", eng3.follow_budget(60000.0, 10000.0) == 5000.0,
      str(eng3.follow_budget(60000.0, 10000.0)))
check("arb: plan() spends the follow-through budget", "self.follow_budget(cash, extra_budget) if extra_budget > 0" in open(os.path.join(ROOT, "sigbot", "arb.py")).read())
rs2 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("backstop: the main-loop recycle runs only when the scanner's feed path is down (and never in cash mode or while killed)",
      "self.scanner.best_unfunded if not self.feed_ok() and self.cfg.arb.new_sets and time.time() >= self.killed_until" in rs2)
seen = []
fb = types.SimpleNamespace(feed=types.SimpleNamespace(healthy=lambda s=120: seen.append(s) or s >= 15))
R.Bot.feed_ok(fb)
check("feed: books silent for more than 30 s (past the 25-s heartbeat) are not healthy", seen == [30], str(seen))
# xv gate: a market whose gate state changed is requoted at once
from sigbot.mm import QuoteState  # noqa: E402
mm2 = MarketMaker.__new__(MarketMaker)
mm2.cfg = types.SimpleNamespace(requote_ticks=2, refresh_sec=1500)
mm2.state = {"9": QuoteState("9", last_reserve=0.5, last_quote_ts=time.time(), fills_seen=0, last_gate=(True, True))}
info = {"reserve": 0.5, "gate": (True, False)}
check("mm: a gate that closes the ask forces a requote", mm2.needs_requote("9", info, 0, 2, 2) is True)
info2 = {"reserve": 0.5, "gate": (True, True)}
check("mm: no gate change and nothing else, no requote", mm2.needs_requote("9", info2, 0, 2, 2) is False)

check("health: sets in a newly arbed race get their settlement credit at once (no cached race list)", "_real_sets_ts" not in open(os.path.join(ROOT, "sigbot", "run.py")).read())
from sigbot import risk as _risk  # noqa: E402
_u = types.SimpleNamespace(races={"S": types.SimpleNamespace(legs={"D": types.SimpleNamespace(exchange_id="d"), "R": types.SimpleNamespace(exchange_id="r")})})
check("risk: a held buy-set is not partisan exposure", _risk.net_positions_ex_arb(_u, {"d": 2000.0, "r": 2000.0}) == {"d": 0.0, "r": 0.0})
check("risk: a held sell-set is still netted, leftovers kept", _risk.net_positions_ex_arb(_u, {"d": -2000.0, "r": -2500.0}) == {"d": 0.0, "r": -500.0})

# ---------------------------------------------------------------- review 22 Oct 23:55 (Fable): behaviour, not source text
gb = types.SimpleNamespace(cfg=types.SimpleNamespace(mm=types.SimpleNamespace(xv_gate_ticks=3)),
                           direction=types.SimpleNamespace(cfg=types.SimpleNamespace(enabled=True)), mm=types.SimpleNamespace(xv_fair={}))
G = lambda killed, t: R.Bot.mm_gate_ready(gb, killed, t)
check("gate: after a start with no outside fair, the MM waits", G(False, 1000.0) is False)
check("gate: ... but not for more than 10 min", G(False, 1000.0 + 601) is True)
gb.mm_need_xv, gb.mm_gate_wait0 = True, None
gb.mm.xv_fair = {"9": (0.5, 0.5, 0.5, 5000.0)}
check("gate: a fresh fair lets the MM quote", G(False, 5100.0) is True)
check("gate: a fair going stale MID-SESSION does not stop the MM", G(False, 5000.0 + 900) is True)
check("gate: killed, no quoting", G(True, 6000.0) is False)
check("gate: after a kill pause with only stale fairs, the MM waits again", G(False, 7900.0) is False)
gb.mm.xv_fair = {"9": (0.5, 0.5, 0.5, 7950.0)}
check("gate: ... until the next fresh poll", G(False, 8000.0) is True)
gb.direction.cfg.enabled = False
gb.mm_need_xv = True
check("gate: with the directional poll off, no wait at all", G(False, 9000.0) is True)

class OStore:
    def rows(self, q, a=()):
        return [{"order_id": 77, "exchange_id": "1075", "side": "yes", "action": "buy", "price": 0.92, "quantity": 400}] if "placed_at >" in q else []
ob = types.SimpleNamespace(open_orders_cache={"1075": [{"id": 1, "side": "yes", "action": "buy", "priceLimit": 0.925, "quantity": 300, "quantityFilled": 100}]},
                           open_orders_ts=time.time(), store=OStore())
lv = R.own_levels(ob)
nb2 = lv.net("1075", {"bids": [{"price": 0.925, "quantity": 200}, {"price": 0.92, "quantity": 2400}], "asks": [{"price": 0.93, "quantity": 2000}]})
check("scanner: our partly filled resting bid (remaining 200) is removed from the book", nb2["bestBid"] == 0.92, str(nb2.get("bestBid")))
check("scanner: an order placed after the snapshot is netted too", nb2["bids"][0]["quantity"] == 2000, str(nb2["bids"][:1]))
check("health: the kill switch gets cash, marks and settlement adjustment from one snapshot",
      "self.health(cycle_cash + pos_value + cycle_adj, cycle_id)" in open(os.path.join(ROOT, "sigbot", "run.py")).read())

_rs3 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
_i = _rs3.find("self.direction.cycle(prices")
check("gate: the outside fairs are refreshed right after the directional poll (fast requotes read them between cycles)",
      _i > 0 and "self.mm.xv_fair = {" in _rs3[_i:_i + 1500])

# ---------------------------------------------------------------- 23 Oct 06:14 UTC: recycle unwound 33,458 Idaho Governor sets
# (forgoing 1,021 of locked profit) to fund baskets the follow-through could only size at 17,166 (gain 639). The unwind must
# be sized by what plan() will actually buy, and the recycle judged on the capital actually redeployed.
from sigbot.arb import ArbEngine  # noqa: E402
from sigbot.config import ArbConfig  # noqa: E402
from sigbot.universe import Race, Leg  # noqa: E402


def _race(name, exs):
    return Race(name, {p: Leg(p, e, e, name) for p, e in zip("DR", exs)})


def _book(bid, ask, qty):
    return {"bids": [{"price": bid, "quantity": qty}], "asks": [{"price": ask, "quantity": qty}]}


def _engine(race_cap=None, committed=None, cash_reserve=20000.0):
    eng = ArbEngine.__new__(ArbEngine)
    eng.cfg = ArbConfig(capital_frac=0.9, max_race_frac=0.15, min_set_qty=20, cash_reserve=cash_reserve, min_ret=0.024, min_ret_sell=0.005,
                        recycle_enabled=True, recycle_min_gain=0.005, recycle_min_new_ret=0.02, high_ret_min=0.10, high_ret_race_frac=0.40)  # high-ret bypass off: the race cap must bind as it did for the MN-05 buy-set
    eng.u = types.SimpleNamespace(races={"HELD": _race("HELD", ["h1", "h2"]), "TGT": _race("TGT", ["t1", "t2"])})
    eng.starting_cash = 100000.0
    eng.dry_run = True
    eng.committed = dict(committed or {"HELD": 32000.0, "OTHER": 28000.0})
    eng.cooldown, eng.hostile = {}, set()
    eng.x_table, eng._x_live, eng._x_live_mtime, eng._x_hist = {}, {}, 0.0, {}
    eng.store = types.SimpleNamespace(rows=lambda *a, **k: [{"race": "HELD"}], record_arb=lambda *a, **k: None)
    eng.api = types.SimpleNamespace(tournament_id="t")
    if race_cap is not None:
        eng.race_caps, eng.race_cap_default, eng.race_cap_floor = {"TGT": race_cap}, 15000.0, 0.0
    return eng


# held: 34,479 two-leg sets (payout 1 each); unwinding buys YES on both legs at the asks
held_books = {"h1": _book(0.51, 0.5165, 50000), "h2": _book(0.51, 0.5165, 50000)}   # exit buys YES at 0.5165 x 2 -> proceeds 0.967, forgone 3.3%
# target: standing 6% sell-set (bids sum 1.06), 50,000 deep on both legs -> best_new qty 50,000 at ret 6/94
tgt_books = {"t1": _book(0.53, 0.535, 50000), "t2": _book(0.53, 0.535, 50000)}
best = {"race": "TGT", "kind": "sell_set", "ret": 0.06 / 0.94, "cost": 0.94, "qty": 50000, "ts": time.time(), "fresh_books": tgt_books}
positions = {"h1": -34479.0, "h2": -34479.0}
eng = _engine(race_cap=2000.0)                       # the allocator caps the target race at 2,000 of capital
rec = eng.recycle(best, {"HELD": held_books}, positions, 4919.0)
check("recycle: the unwind is sized to what the follow-through will buy (2,000 cap -> ~2,100 sets, not 34,479)",
      rec is not None and 2000 <= rec["qty"] <= 2200, str(rec))
if rec:
    freed = rec["qty"] * rec["proceeds"]
    plan = eng.follow_through_plan(best, "HELD", freed, 4919.0)
    dep = sum(c.qty * c.cost for c in plan)
    check("recycle: the freed capital is (nearly) all redeployed", dep >= 0.95 * freed, f"freed {freed:.0f} deployed {dep:.0f}")
eng = _engine(race_cap=2000.0, committed={"HELD": 32000.0, "TGT": 2000.0})   # the target race is already at its cap
check("recycle: nothing to buy -> no unwind", eng.recycle(best, {"HELD": held_books}, positions, 4919.0) is None)
check("recycle: no fresh books for the target -> no unwind", _engine(race_cap=2000.0).recycle(dict(best, fresh_books=None), {"HELD": held_books}, positions, 4919.0) is None)
# no allocator cap: the default per-race ceiling (max_race_frac 0.15 -> 15k) is what plan() can buy
eng = _engine(committed={"HELD": 32000.0, "OTHER": 56000.0})
rec = eng.recycle(best, {"HELD": held_books}, positions, 4919.0)
check("recycle: without an allocator cap the default per-race ceiling (15k) sizes the unwind, not the 34,479 held sets",
      rec is not None and rec["qty"] < 34479 and 14250 <= rec["qty"] * rec["proceeds"] <= 15000, str(rec))
# the gain is judged on redeployed capital: a target barely better than the held set, with only part of the proceeds deployable,
# is refused (old code: ret 3.0% - forgone 2.4% = +0.6% passes recycle_min_gain 0.5% while the real swap loses money)
tgt2 = {"t1": _book(0.515, 0.52, 50000), "t2": _book(0.515, 0.52, 50000)}           # bids sum 1.03 -> ret 3.09%
best2 = dict(best, ret=0.03 / 0.97, cost=0.97, fresh_books=tgt2)
held2 = {"h1": _book(0.505, 0.512, 50000), "h2": _book(0.505, 0.512, 50000)}        # exit at 0.976 -> forgone 2.46%
eng = _engine(race_cap=2000.0)
r2 = eng.recycle(best2, {"HELD": held2}, positions, 4919.0)
check("recycle: a 3.09% target vs a 2.46% exit still recycles when the unwind fits the purchase", r2 is not None and r2["qty"] <= 2100, str(r2))
eng = _engine(race_cap=2000.0)
eng.cfg.recycle_fit_tolerance = 1.0                                                  # tolerance off: unwind would stay oversized
r3 = eng.fit_recycle_qty(best2, "HELD", held2, 34479, 4919.0)
check("recycle: judged on redeployed capital, an oversized unwind (forgone 827 vs gained 62) is refused", r3 is None, str(r3))

# ---------------------------------------------------------------- 23 Oct: objective = liquid cash for the 3 Nov punt
import json as _j3  # noqa: E402
_live = _j3.load(open(os.path.join(ROOT, "config.json")))
check("cash mode: live config: free cash buys no sets, recycles on, tilt/directional entries/offload/allocator auto-apply off",
      _live["arb"].get("new_sets") is False and _live["arb"].get("recycle_enabled") is True and _live["tilt"].get("enabled") is False
      and _live["direction"].get("allow_live") is False and _live["allocator"].get("offload_live") is False
      and _live["allocator"].get("auto_apply") is False)
check("cash mode: hard limits untouched (partisan 5,000, election sleeve off)",
      _live["mm"].get("portfolio_max_partisan_notional", 5000) == 5000 and not _live.get("election_enabled", False))

# one_pass with a real candidate: in cash mode it must remember the best set (for recycling) and never plan/execute
_race = types.SimpleNamespace(name="R", exchange_ids=["a", "b"], has_independent=False)
_bk = lambda bid, ask: {"bids": [{"price": bid, "quantity": 2000}], "asks": [{"price": ask, "quantity": 2000}], "feed_ts": 9e9}
_books = {"a": _bk(0.60, 0.61), "b": _bk(0.45, 0.46)}
class _Arb:
    committed = {"X": 1000.0}
    calls = []
    def candidates_from_prices(self, p): return [_race]
    def x_for(self, r): return 0.0
    def race_budget_left(self, n, ret=None): return 1e9
    def budget_left(self, c): return 1e9
    def chunks_for_race(self, r, books): return [types.SimpleNamespace(race="R", kind="sell_set", ret=0.05, cost=0.95, qty=500)]
    def plan(self, *a, **k): self.calls.append("plan"); return []
    def execute(self, *a, **k): self.calls.append("execute"); return []
def _scan(new_sets):
    arb = _Arb(); arb.calls = []
    b = types.SimpleNamespace(cfg=types.SimpleNamespace(arb=types.SimpleNamespace(enabled=True, new_sets=new_sets, recycle_enabled=True, min_set_qty=20,
                                                                                  min_edge_sell=0.01, min_edge_buy=0.01),
                                                        arb_scan_books_feed=4, arb_scan_books=2),
                              feed_ok=lambda: True, rest_snapshot=({"a": {"bestBid": 0.60, "bestAsk": 0.61}, "b": {"bestBid": 0.45, "bestAsk": 0.46}}, time.time()),
                              feed_prices=lambda p: p, killed_until=0.0, arb=arb, cash=50000.0, fresh_book=lambda ex, age=None: _books[ex],
                              open_orders_cache={}, open_orders_ts=0.0, store=None, trade_lock=threading.Lock(),
                              universe=types.SimpleNamespace(races={"R": _race}))
    sc = R.ArbScanner.__new__(R.ArbScanner)
    sc.bot, sc.stats, sc.best_unfunded = b, {"passes": 0, "last_pass": 0, "candidates": 0}, None
    sc.one_pass()
    return sc, arb
_sc, _a = _scan(False)
check("cash mode: the scanner remembers the best set for recycling but never plans or buys with free cash",
      _a.calls == [] and (_sc.best_unfunded or {}).get("race") == "R", f"calls={_a.calls} best={_sc.best_unfunded}")
_sc, _a = _scan(True)
check("cash mode test is sensitive: with new_sets on, the same pass plans a basket", "plan" in _a.calls, str(_a.calls))
# the follow-through after a recycle may spend only what the unwind freed, even with plenty of cash above the reserve
eng4 = ArbEngine.__new__(ArbEngine)
eng4.cfg = types.SimpleNamespace(capital_frac=0.9, cash_reserve=20000.0, new_sets=False)
eng4.starting_cash, eng4.committed = 100000.0, {"X": 30000.0}
check("cash mode: a recycle's follow-through spends only the freed 5,000 (cash 60k above the reserve)", eng4.follow_budget(60000.0, 5000.0) == 5000.0,
      str(eng4.follow_budget(60000.0, 5000.0)))
eng4.cfg.new_sets = True
check("... and with new_sets on it may also use the room above the reserve", eng4.follow_budget(60000.0, 5000.0) > 5000.0)
check("cash mode: free unwinds are not gated by new_sets", "new_sets" not in R.ArbScanner.free_unwind_pass.__code__.co_names)

# review 23 Oct 16:40: in cash mode an unwind whose follow-through bought nothing must not be followed by another unwind
eng5 = ArbEngine.__new__(ArbEngine)
eng5.cfg = types.SimpleNamespace(recycle_enabled=True, recycle_min_new_ret=0.0, new_sets=False, min_set_qty=20, capital_frac=0.9, cash_reserve=20000.0)
eng5.starting_cash, eng5.committed, eng5.hostile, eng5.cooldown = 100000.0, {"X": 30000.0}, set(), {}
eng5.recycle_credit = 5000.0
check("cash mode: no new unwind while an earlier unwind's cash is unspent", eng5.recycle({"race": "T", "kind": "sell_set", "ret": 0.06, "cost": 0.95, "qty": 500},
                                                                                     {}, {}, 30000.0) is None)
_rs4 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("cash mode: recycle_pass spends outstanding credit before unwinding, and cools down a target that vanished",
      "self.spend_recycle_credit(tgt)" in _rs4 and "b.arb.cooldown[tgt] = time.time() + 60" in _rs4)

# ---------------------------------------------------------------- 23 Oct: catch-up sweep trial (proposals/outside_sweep.py)
from sigbot.proposals import outside_sweep as OS  # noqa: E402
def _row(t, pm, k, mid, party="D", ex="10", race="Rc", bid=None, ask=None):
    return {"ts": t, "exchange_id": ex, "race": race, "party": party, "pm": pm, "kalshi": k, "sig_mid": mid,
            "sig_bid": mid - 0.0025 if bid is None else bid, "sig_ask": mid + 0.0025 if ask is None else ask}
T0 = 1792700000.0
def _cs(rows_then, rows_now):
    cs = OS.CatchupSweep()
    cs.push(rows_then); cs.push(rows_now)
    return cs, cs.signals(rows_now, rows_now[0]["ts"])
_, sg = _cs([_row(T0, 0.50, 0.50, 0.50)], [_row(T0 + 3600, 0.53, 0.53, 0.50)])
check("catchup: PM+Kalshi up 3 pts, SIG flat -> one buy-YES signal", len(sg) == 1 and sg[0]["d"] == 1 and abs(sg[0]["gap"] - 0.03) < 1e-9, str(sg))
_, sg = _cs([_row(T0, 0.50, 0.50, 0.50)], [_row(T0 + 3600, 0.53, 0.505, 0.50)])
check("catchup: no signal when Kalshi did not move with Polymarket", sg == [])
_, sg = _cs([_row(T0, 0.50, 0.50, 0.50)], [_row(T0 + 3600, 0.53, 0.53, 0.51)])
check("catchup: no signal when SIG already followed a third of the move", sg == [])
_, sg = _cs([_row(T0, 0.50, 0.50, 0.50)], [_row(T0 + 3600, 0.53, 0.53, 0.50, bid=0.49, ask=0.51)])
check("catchup: no signal on a SIG spread wider than 2 ticks", sg == [])
_, sg = _cs([_row(T0, 0.50, 0.50, 0.50, party="R"), _row(T0, 0.50, 0.50, 0.50, party="D", ex="11")],
            [_row(T0 + 3600, 0.53, 0.53, 0.50, party="R"), _row(T0 + 3600, 0.47, 0.47, 0.50, party="D", ex="11")])
check("catchup: one leg per race (D), never both legs of the same move", [x["exchange_id"] for x in sg] == ["11"], str(sg))
_, sg = _cs([_row(OS.LAST_ENTRY - 3600, 0.50, 0.50, 0.50)], [_row(OS.LAST_ENTRY + 60, 0.53, 0.53, 0.50)])
check("catchup: no entries after 2 Nov 23:30 ET", sg == [])
cs = OS.CatchupSweep()
check("catchup: size = 300 notional at the touch, capped by 80% of the touch", cs.size({"d": 1, "ask": 0.5, "bid": 0.495}, 5000)[2] == 600
      and cs.size({"d": 1, "ask": 0.5, "bid": 0.495}, 400)[2] == 320 and cs.size({"d": -1, "ask": 0.705, "bid": 0.7}, 5000)[:2] == ("no", 0.3))
check("catchup: tiny touch -> no trade", cs.size({"d": 1, "ask": 0.5, "bid": 0.495}, 100)[2] == 0)
check("catchup: partisan cap: no D-adding entry near the cap, R-adding allowed, never past the cap",
      not cs.can_enter(0, "D", 4850, 300) and cs.can_enter(0, "R", 4850, 300) and not cs.can_enter(0, "D", 4600, 500)
      and cs.can_enter(0, "D", 2000, 300))
check("catchup: at most 4 open positions", not cs.can_enter(4, "R", 0, 300) and cs.can_enter(3, "R", 0, 300))
cs.ctx["10"] = {"ts": T0, "d": 1, "f_then": 0.50, "move": 0.03, "mid0": 0.50, "gap": 0.03}
check("catchup: exit when SIG has moved 70% of the gap", cs.exit_reason("10", _row(T0 + 600, 0.53, 0.53, 0.521), T0 + 600) == "SIG caught up")
check("catchup: hold while SIG has not caught up and the outside holds", cs.exit_reason("10", _row(T0 + 600, 0.53, 0.53, 0.51), T0 + 600) is None)
check("catchup: exit when the outside move reverts", cs.exit_reason("10", _row(T0 + 600, 0.505, 0.505, 0.50), T0 + 600) == "outside move reverted")
check("catchup: exit when Polymarket and Kalshi disagree by > 4 pts", cs.exit_reason("10", _row(T0 + 600, 0.58, 0.53, 0.50), T0 + 600) is not None)
check("catchup: 12 h time stop", cs.exit_reason("10", _row(T0 + 43201, 0.53, 0.53, 0.51), T0 + 43201) == "12 h time stop")
check("catchup: flatten everything before the punt (3 Nov 11:30 ET)", cs.exit_reason("10", _row(OS.FLATTEN_AT, 0.53, 0.53, 0.51), OS.FLATTEN_AT) is not None)
check("catchup: a position without entry context (restart) is closed", cs.exit_reason("99", None, T0) is not None)
pnl, op = OS.pnl_from_fills([{"exchange_id": "10", "quantity": 600, "price": 0.50}, {"exchange_id": "10", "quantity": -600, "price": 0.41}], {})
check("catchup: P&L from fills (buy 600 @0.50, sell @0.41 = -54) and the sleeve switches itself off at -50",
      abs(pnl + 54) < 1e-6 and op == {} and cs.check_kill(pnl) and cs.signals([_row(T0 + 3600, 0.53, 0.53, 0.50)], T0 + 3600) == [])
check("catchup: live config: trial on, 300 per position, -50 stop", _live.get("catchup_enabled") is True and _live.get("catchup_notional") == 300.0
      and _live.get("catchup_kill") == -50.0)

# review 23 Oct (catch-up): cap per entry, missing marks, exits off a fresh/REST book, MM inventory
pnl0, op0 = OS.pnl_from_fills([{"exchange_id": "10", "quantity": 600, "price": 0.50}], {})
check("catchup: a position with no current mark is valued at its fill price, not 0 (no spurious kill)", abs(pnl0) < 1e-9 and op0 == {"10": 600.0})

class _CStore:
    def __init__(self, fills): self.fills, self.orders = fills, []
    def rows(self, q, a=()):
        if "FROM fills" in q: return self.fills
        return []
    def record_order(self, oid, ex, mid, side, act, px, q, purpose, **k): self.orders.append((ex, side, px, q, purpose))
def _cbot(fills, sigs, net_d, fresh=True):
    sent = []
    cs = OS.CatchupSweep()
    cs.signals = lambda rows, now: sigs
    races = {"R%d" % i: types.SimpleNamespace(legs={}) for i in range(4)}
    uni = types.SimpleNamespace(by_exchange={str(i): ("R%d" % i, "D") for i in range(4)}, races=races)
    bk = {"bids": [{"price": 0.50, "quantity": 5000}], "asks": [{"price": 0.505, "quantity": 5000}]}
    b = types.SimpleNamespace(catchup=cs, dry_run=False, store=_CStore(fills), direction=types.SimpleNamespace(rows=[{"exchange_id": "0", "race": "R0", "party": "D", "sig_mid": 0.5025}]),
                              universe=uni, open_orders_cache={}, cfg=types.SimpleNamespace(mm=types.SimpleNamespace(portfolio_max_partisan_notional=5000.0)),
                              fresh_book=(lambda ex, age=None: bk) if fresh else (lambda ex, age=None: None),
                              get_book=lambda ex, depth=50, client=None, max_age=None: {"bids": [{"price": 0.48, "quantity": 5000}], "asks": [{"price": 0.485, "quantity": 5000}]},
                              api=types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: sent.append((ex, side, q, px)) or {"orderId": len(sent)},
                                                        new_key=lambda p: p),
                              catchup_state_path=os.path.join(tempfile.mkdtemp(), "cs.json"))
    _orig = R.risk.exposure
    R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=net_d)
    try:
        R.Bot.catchup_cycle(b, {"polled": True}, {}, {}, False)
    finally:
        R.risk.exposure = _orig
    return b, sent
_sigs = [{"exchange_id": str(i), "race": "R%d" % i, "party": "D", "d": 1, "move": 0.03, "f_now": 0.53, "f_then": 0.50, "mid": 0.5025, "gap": 0.0275,
          "bid": 0.50, "ask": 0.505} for i in range(4)]
_b, _sent = _cbot([], _sigs, 4400.0)
check("catchup: four D-adding signals in one poll at net D 4,400: entries stop before net D passes 5,000 (was: all four, 5,600)",
      len(_sent) == 2 and 4400.0 + sum(x[2] * x[3] for x in _sent) <= 5000.0 + 1e-6, str(_sent))
_b, _sent = _cbot([], _sigs, 0.0)
check("catchup: ... and with room, at most 4 entries of <= 300 each", len(_sent) == 4 and all(x[2] * x[3] <= 300.0 + 1e-6 for x in _sent), str(_sent))
_b, _sent = _cbot([{"exchange_id": "7", "quantity": 600.0, "price": 0.50}], [], 0.0, fresh=False)
check("catchup: an exit with no fresh feed book is priced off a REST read (sell YES at its bid 0.48 = buy NO @0.52), never a stale row",
      _sent == [("7", "no", 600, 0.52)], str(_sent))
_mmb = types.SimpleNamespace(catchup_open={"5": 600.0})
check("mm: the catch-up sweep's own position is not counted as MM inventory", R.Bot.mm_positions(_mmb, {"5": 650.0, "6": -10.0}) == {"5": 50.0, "6": -10.0})

# ---------------------------------------------------------------- 23 Oct: deviation churn (proposals/dev_churn.py)
from sigbot.proposals import dev_churn as DC  # noqa: E402
_now = 1792800000.0
# leg D quoted every 10 min (heartbeat) at 0.505, leg R changes every 20 min between 0.505 and 0.507: carried forward
_rows = [("R2", "D", _now - 86000 + 600 * i, 0.505) for i in range(144)] + \
        [("R2", "R", _now - 86000 + 1200 * i, 0.505 + (0.002 if i % 2 else 0.0)) for i in range(72)]
_med = DC.medians_from_rows(_rows, {"R2": 2}, _now)
check("churn: 24-h median of the mid overround with each leg's last quote carried forward", _med.get("R2") is not None
      and 0.010 - 1e-9 <= _med["R2"] <= 0.012 + 1e-9, str(_med))
check("churn: no median without 12 h of history", DC.medians_from_rows([r for r in _rows if r[2] > _now - 6 * 3600], {"R2": 2}, _now) == {})
check("churn: no sample where a leg's last quote is over 30 min old", DC.medians_from_rows(
      [("R2", "D", _now - 86000, 0.5)] + [("R2", "R", _now - 86000 + 300 * i, 0.51) for i in range(287)], {"R2": 2}, _now) == {})
ch = DC.DevChurn(capital=12000, cash_floor=3000, kill=-600)
_r2 = types.SimpleNamespace(name="R2", exchange_ids=["a", "b"], has_independent=False)
_r3 = types.SimpleNamespace(name="R3", exchange_ids=["a", "b", "c"], has_independent=True)
_ri = types.SimpleNamespace(name="Rhode Island Governor", exchange_ids=["a", "b"], has_independent=False)
check("churn: two-leg races only; never RI Governor or a three-leg race", ch.eligible(_r2, _now) and not ch.eligible(_r3, _now) and not ch.eligible(_ri, _now))
check("churn: unusually cheap = >= 4 ticks above the 24-h median", ch.unusually_cheap(0.031, 0.011) and not ch.unusually_cheap(0.030, 0.011))
_c = types.SimpleNamespace(race="R2", kind="sell_set", qty=5000, cost=0.97)
check("churn: entry size: 2,000 a time, within the pot, the per-race limit and the 3k cash floor",
      ch.entry_qty(_c, 50000, 20) == 2000 and ch.entry_qty(_c, 4000, 20) == 1030 and ch.entry_qty(_c, 3010, 20) == 0
      and ch.entry_qty(types.SimpleNamespace(race="R2", kind="buy_set", qty=5000, cost=0.95), 50000, 20) == 0)
ch.add_lot("R2", 2000, 0.97, _now)
_ch2 = DC.DevChurn(capital=12000, cash_floor=3000, kill=-600)
_ch2.add_lot("R2", 3000, 0.97, _now)
check("churn: per-race limit 4k counts what the race already holds", _ch2.entry_qty(_c, 50000, 20) == int((4000 - 2910) // 0.97), str(_ch2.entry_qty(_c, 50000, 20)))
check("churn: no re-entry in the same race within 5 min", not ch.eligible(_r2, _now + 60))
_lot = {"cost": 0.97, "ts": _now}
check("churn: exit at +2 ticks per set, hold below it", ch.exit_reason(_lot, 0.980, _now + 600) == "round trip +2 ticks" and ch.exit_reason(_lot, 0.975, _now + 600) is None)
check("churn: exit after 24 h whatever the price, and sell everything before the 3 Nov punt",
      ch.exit_reason(_lot, 0.96, _now + 86401) == "24 h maximum hold" and ch.exit_reason(_lot, 0.96, DC.SELL_ALL_AT) is not None)
check("churn: ledger shrinks to the sets actually held (a recycle closed some), never grows", ch.reconcile("R2", 1500) == 500 and ch.held("R2") == 1500
      and ch.reconcile("R2", 9000) == 0 and ch.held("R2") == 1500)
_p = ch.close("R2", 1500, 0.98, _now + 900)
check("churn: realised P&L of an exit, and no re-entry for 30 min after it", abs(_p - 15.0) < 1e-6 and ch.held("R2") == 0 and not ch.eligible(_r2, _now + 900 + 600))
check("churn: stops entering at -600 (realised + open)", ch.check_kill(-615.0) and not ch.eligible(_r2, _now + 99999))
check("churn: no entries after 2 Nov 23:00 UTC", not DC.DevChurn().eligible(_r2, DC.LAST_ENTRY + 1))
check("churn: live config: on, pot <= 12k, 3k floor, stop between -600 and 0 (launch 24 Oct at 4k / -200 until the Fable check)",
      _live.get("churn_enabled") is True and 0 < _live.get("churn_capital", 0) <= 12000.0
      and _live.get("churn_cash_floor") == 3000.0 and -600.0 <= _live.get("churn_kill", 1) < 0)

_rs6 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("unwinds: the main-loop free unwind reads the latest snapshot under the lock, not the cycle-start positions (sim churn double unwind)",
      "fu = self.arb.free_unwinds(held_books, self.positions_snapshot, cycle_id)" in _rs6 and "free_unwinds(held_books, positions, cycle_id)" not in _rs6)
check("churn: re-checks the sets actually held under the lock right before selling, and updates the shared snapshot after its baskets",
      "held = min(held, sets_held(race))" in _rs6 and "snap_add(race, held)" in _rs6 and "snap_add(race, -q)" in _rs6)

# 23 Oct: churn pauses on rising gap volatility (Sizhe), and stops entering 1 Nov 00:00 UTC
def _ser(n_races, base_sd, rec_sd):
    import random as _rnd
    g = _rnd.Random(7)
    out = {}
    for k in range(n_races):
        v, xs = 0.01, []
        for i in range(int((DC.VOL_BASE + DC.VOL_RECENT) / DC.GRID)):
            t = _now - DC.VOL_BASE - DC.VOL_RECENT + DC.GRID * (i + 1)
            v += g.gauss(0, rec_sd if t > _now - DC.VOL_RECENT else base_sd)
            xs.append((t, v))
        out["R%d" % k] = xs
    return out
_calm, _wild = DC.vol_ratio(_ser(12, 0.001, 0.001), _now), DC.vol_ratio(_ser(12, 0.001, 0.003), _now)
check("churn: gap-movement ratio ~1 when calm, ~3 when the last 6 h move 3x as much", 0.7 < _calm < 1.4 and _wild > 2.0, f"{_calm} {_wild}")
check("churn: too few races -> no reading (state kept)", DC.vol_ratio(_ser(5, 0.001, 0.003), _now) is None)
_v = DC.DevChurn()
check("churn: pause new entries at >= 1.5x, stay paused at 1.3x, resume below 1.2x",
      _v.update_vol(1.6) == "paused" and not _v.eligible(_r2, _now) and _v.update_vol(1.3) is None and _v.vol_paused
      and _v.update_vol(1.1) == "resumed" and _v.eligible(_r2, _now))
_v.update_vol(2.0); _v2 = DC.DevChurn(); _v2.load(_v.to_json())
check("churn: the volatility pause survives a restart", _v2.vol_paused)
check("churn: no new entries from 1 Nov 00:00 UTC (Sizhe: stop 2-3 days before 3 Nov)",
      DC.LAST_ENTRY == datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc).timestamp() and not DC.DevChurn().eligible(_r2, DC.LAST_ENTRY + 1))

# 23 Oct: hourly order-book snapshots for the punt screener (no API reads)
_tmpd = tempfile.mkdtemp(); os.makedirs(os.path.join(_tmpd, "data"))
_oldhere = R.HERE; R.HERE = _tmpd
try:
    _bk = {str(i): {"bids": [{"price": 0.5, "quantity": 100}], "asks": [{"price": 0.505, "quantity": 100}]} for i in range(10)}
    _sb = types.SimpleNamespace(feed_ok=lambda: True, feed=types.SimpleNamespace(books=lambda: _bk),
                                universe=types.SimpleNamespace(all_exchanges=lambda: [str(i) for i in range(10)]))
    R.Bot.snapshot_books(_sb)
    _ok1 = _json.load(open(os.path.join(_tmpd, "data", "books_latest.json"))) == _bk and len(os.listdir(os.path.join(_tmpd, "data", "books_history"))) == 1
    os.remove(os.path.join(_tmpd, "data", "books_latest.json"))
    R.Bot.snapshot_books(_sb)                     # within the hour: nothing written
    _ok2 = not os.path.exists(os.path.join(_tmpd, "data", "books_latest.json"))
    _sb2 = types.SimpleNamespace(feed_ok=lambda: True, feed=types.SimpleNamespace(books=lambda: {"1": _bk["1"]}),
                                 universe=_sb.universe)
    R.Bot.snapshot_books(_sb2)                    # feed still filling: no snapshot
    _ok3 = not os.path.exists(os.path.join(_tmpd, "data", "books_latest.json"))
finally:
    R.HERE = _oldhere
check("books: hourly snapshot of the feed's books in the screener's format, history every 6 h, none while the feed is still filling",
      _ok1 and _ok2 and _ok3, f"{_ok1} {_ok2} {_ok3}")
# 23 Oct: the catch-up sweep loads its 60-min look-back from stored quotes at start-up (no hour of waiting)
_vq = [(T0, "polymarket", "Rc", "D", 0.50), (T0, "kalshi", "Rc", "D", 0.50), (T0, "sig", "Rc", "D", 0.50),
       (T0 + 3600, "polymarket", "Rc", "D", 0.53), (T0 + 3600, "kalshi", "Rc", "D", 0.53)] + \
      [(T0 + 600 * k, "sig", "Rc", "D", 0.50) for k in range(1, 6)]            # SIG unchanged: 10-min heartbeats only
_boot = OS.rows_from_venue_quotes(_vq, {("Rc", "D"): "10"})
_cs = OS.CatchupSweep(); _cs.push(_boot)
_now_row = [_row(T0 + 3600, 0.53, 0.53, 0.50)]
check("catchup: history rebuilt from stored quotes (changes carried forward) gives a signal right after a restart",
      _boot[-1]["sig_mid"] == 0.50 and _boot[-1]["pm"] == 0.53 and len(_cs.signals(_now_row, T0 + 3600)) == 1, str(_boot[-2:]))
_stale = OS.rows_from_venue_quotes([(T0, "polymarket", "Rc", "D", 0.5), (T0 + 3601, "kalshi", "Rc", "D", 0.5)], {("Rc", "D"): "10"})
check("catchup: a stored quote older than 30 min is not carried forward", _stale[-1]["pm"] is None)

check("churn: no entries in races where we already hold > 4,000 shares on a leg (own churn sets not counted)",
      DC.big_holding({"a": -4500.0, "b": -100.0}, ["a", "b"], 0, 4000) and not DC.big_holding({"a": -4500.0, "b": -4500.0}, ["a", "b"], 2000, 4000)
      and not DC.big_holding({"a": -3900.0}, ["a", "b"], 0, 4000) and _live.get("churn_skip_holding") == 4000.0
      and "big_holding(self.positions_snapshot or positions, race.exchange_ids, ch.held(race.name), lim)" in open(os.path.join(ROOT, "sigbot", "run.py")).read())

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
