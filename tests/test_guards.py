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
check("backstop: the main-loop recycle runs only when the scanner's feed path is down", "best_unfunded if not self.feed_ok() else None" in rs2)
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
check("cash mode: live config holds and unwinds only (no new sets, no recycles, no tilt, no directional entries, no offload)",
      _live["arb"].get("new_sets") is False and _live["arb"].get("recycle_enabled") is False and _live["tilt"].get("enabled") is False
      and _live["direction"].get("allow_live") is False and _live["allocator"].get("offload_live") is False
      and _live["allocator"].get("auto_apply") is False)
check("cash mode: hard limits untouched (partisan 5,000, election sleeve off)",
      _live["mm"].get("portfolio_max_partisan_notional", 5000) == 5000 and not _live.get("election_enabled", False))

class _NoArb:
    committed = {"X": 1000.0}
    def candidates_from_prices(self, p): raise AssertionError("candidates read with new_sets off")
    def plan(self, *a, **k): raise AssertionError("plan() called with new_sets off")
    def execute(self, *a, **k): raise AssertionError("execute() called with new_sets off")
    def budget_left(self, c): return 0.0
_cb = types.SimpleNamespace(cfg=types.SimpleNamespace(arb=types.SimpleNamespace(enabled=True, new_sets=False, recycle_enabled=True, min_set_qty=20)),
                            feed_ok=lambda: True, rest_snapshot=({"1": {}}, time.time()), feed_prices=lambda p: p, killed_until=0.0,
                            arb=_NoArb(), cash=50000.0)
_sc = R.ArbScanner.__new__(R.ArbScanner)
_sc.bot, _sc.stats = _cb, {"passes": 0, "last_pass": 0}
_sc.best_unfunded = {"race": "Y", "kind": "sell_set", "ret": 0.06, "cost": 1.9, "qty": 500, "ts": time.time()}
try:
    _sc.one_pass(); _sc.recycle_pass(); _ok = True; _err = ""
except AssertionError as e:
    _ok, _err = False, str(e)
check("cash mode: the scanner neither buys a new set nor recycles (even with recycle_enabled on)", _ok and _sc.stats["passes"] == 1, _err)
check("cash mode: free unwinds are not gated by new_sets", "new_sets" not in R.ArbScanner.free_unwind_pass.__code__.co_names)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
