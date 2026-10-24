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
        protected = staticmethod(lambda: set())

        def unwind_set(self, rname, books, q, why="", new_ret=None, min_gain=0.0, **kw):
            calls.append((rname, q))
            return {"race": rname, "qty": q}

    races = {"A": types.SimpleNamespace(exchange_ids=["a1", "a2"]), "B": types.SimpleNamespace(exchange_ids=["b1", "b2"])}
    b = types.SimpleNamespace(offload_live=True, dry_run=False, cash=cash,
                              cfg=types.SimpleNamespace(arb=types.SimpleNamespace(cash_reserve=20000.0, recycle_min_gain=0.005), offload_arb_for_sleeves=True),
                              arb=Arb(), universe=types.SimpleNamespace(races=races), fresh_book=lambda ex, age=None: {"asks": []},
                              trade_lock=threading.Lock(), tilt=types.SimpleNamespace(unfunded=[1], free_last=tilt_free),
                              api=types.SimpleNamespace(account=lambda: {"balance": cash}), store=None, universe_races=races,
                              refresh_snapshot=lambda deltas=None: None, unwind_deltas=lambda u: {}, fresh_positions=lambda: {})
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
    protected = staticmethod(lambda: set())
    no_buy = staticmethod(lambda race: False)
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
check("churn: live config: on or off (off since 24 Oct 01:00 until the Fable fixes are reviewed), pot <= 12k, 3k floor, stop between -600 and 0",
      _live.get("churn_enabled") in (True, False) and 0 < _live.get("churn_capital", 0) <= 12000.0
      and _live.get("churn_cash_floor") == 3000.0 and -600.0 <= _live.get("churn_kill", 1) < 0)

_rs6 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("unwinds: the main-loop free unwind reads the latest snapshot under the lock, not the cycle-start positions (sim churn double unwind)",
      "fu = self.arb.free_unwinds(held_books, self.positions_snapshot, cycle_id, refresh=self.fresh_positions)" in _rs6 and "free_unwinds(held_books, positions, cycle_id)" not in _rs6)

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


# ---------------------------------------------------------------- 24 Oct: high-frequency arb (churn) fixes after the Fable stress check
from sigbot.store import Store as _Store  # noqa: E402
from sigbot.api import ApiError as _ApiErr  # noqa: E402


class _Venue:
    """Signed positions (NO legs negative) and an all-or-none multi-leg endpoint filling `fill` shares a leg (None = all)."""
    def __init__(self, pos):
        self.pos, self.sent, self.fill, self.tournament_id, self.n = dict(pos), [], None, "t", 0

    def new_key(self, p):
        self.n += 1
        return f"{p}-{self.n}"

    def place_multi_leg(self, legs, all_or_none=True, key=None):
        self.sent.append((key, [dict(l) for l in legs]))
        out = []
        for l in legs:
            q = l["quantity"] if self.fill is None else min(self.fill, l["quantity"])
            self.pos[l["exchangeId"]] = self.pos.get(l["exchangeId"], 0.0) + (-q if l["side"] == "no" else q)
            out.append({"data": {"exchangeId": l["exchangeId"], "quantityTraded": q, "price": l["price"], "open": False, "orderId": self.n}})
        return {"results": out}


def _hrace(name, exs):                     # (_race is rebound to a stub further up)
    return Race(name, {p: Leg(p, e, e, name) for p, e in zip("DR", exs)})


_HR = {n: _hrace(n, [n + "1", n + "2"]) for n in ("CA", "CB", "Rhode Island Governor", "BIG")}


def _hengine():                            # _engine() with real Race objects
    _keep = globals()["_race"]
    globals()["_race"] = _hrace
    try:
        return _engine()
    finally:
        globals()["_race"] = _keep


def _hbot(pos, books, lots=(), races=(), kill=-600.0):
    tmp = tempfile.mkdtemp()
    st = _Store(os.path.join(tmp, "t.sqlite"))
    v = _Venue(pos)
    e = ArbEngine.__new__(ArbEngine)
    e.cfg = ArbConfig(min_set_qty=20, min_edge_sell=0.01, min_ret_sell=0.005)
    e.u, e.api, e.store, e.dry_run, e.starting_cash = types.SimpleNamespace(races=_HR), v, st, False, 100000.0
    e.committed, e.cooldown, e.hostile, e.recycle_credit = {}, {}, set(), 0.0
    e.x_table, e._x_live, e._x_live_mtime, e._x_hist = {}, {}, 0.0, {}
    ch = DC.DevChurn(capital=12000, cash_floor=3000, kill=kill)
    for race, qty, cost, ts in lots:
        ch.add_lot(race, qty, cost, ts)
    ch.races |= set(races)
    b = types.SimpleNamespace(churn=ch, dry_run=False, store=st, churn_med_ts=9e18, churn_med={}, universe=types.SimpleNamespace(races=_HR),
                              fresh_book=lambda ex, age=None: books.get(ex), get_book=lambda ex, d=50, *a, **k: books[ex],
                              positions_snapshot=dict(pos), arb=e, trade_lock=threading.Lock(), cash=50000.0, basket_fills=R.Bot.basket_fills,
                              cfg=types.SimpleNamespace(arb=e.cfg, churn_skip_holding=4000.0), churn_state_path=os.path.join(tmp, "churn_state.json"),
                              open_orders_cache={}, open_orders_ts=0.0)
    e.protected = lambda: {l["race"] for l in ch.lots}
    b.positions_map = lambda strict=False: (dict(v.pos), 0.0, len(v.pos))
    b.refresh_snapshot = lambda deltas=None: R.Bot.refresh_snapshot(b, deltas)
    b.fresh_positions = lambda: R.Bot.fresh_positions(b)
    return b, v, ch


def _legs(race, bid, ask, qty=50000):
    return {ex: _book(bid, ask, qty) for ex in _HR[race].exchange_ids}


def _sold(v):
    return [l[0]["quantity"] for k, l in v.sent if k.startswith("unw")]


_t = time.time()
# fix 1: the recycler (and every unwind-to-fund path) leaves high-frequency arb's races alone and never buys into them
_e = _hengine()
_hpos = {"h1": -34479.0, "h2": -34479.0}
_r_ok = _e.recycle(dict(best), {"HELD": held_books}, _hpos, 1000.0)
_e.protected = lambda: {"HELD"}
_r_prot = _e.recycle(dict(best), {"HELD": held_books}, _hpos, 1000.0)
_u_prot = _e.unwind_set("HELD", held_books, 100, why="offload")
_u_own = _e.unwind_set("HELD", held_books, 100, why="churn: x", allow_protected=True)
check("hf arb: the recycler and offload unwinds never sell a race whose sets high-frequency arb holds (the sleeve itself still can)",
      _r_ok is not None and _r_prot is None and _u_prot is None and _u_own is not None, f"{_r_ok} {_r_prot} {_u_prot} {_u_own}")
_e.protected = lambda: set()
_e.no_buy = lambda r: r == "TGT"
check("hf arb: recycling never buys into a race high-frequency arb holds or excludes (recycle and its follow-through plan)",
      _e.recycle(dict(best), {"HELD": held_books}, _hpos, 1000.0) is None and _e.plan({"TGT": tgt_books}, 1e6, extra_budget=50000.0, recycle=True) == []
      and _e.plan({"TGT": tgt_books}, 1e6) != [])
_e.no_buy = lambda r: False
_e.protected = lambda: {"TGT"}
check("hf arb: no arb plan adds sets to a race high-frequency arb holds (its ledger would not know them)", _e.plan({"TGT": tgt_books}, 1e6) == [])
_nb = types.SimpleNamespace(churn=DC.DevChurn(), universe=types.SimpleNamespace(races=_HR), cfg=types.SimpleNamespace(churn_skip_holding=4000.0),
                            positions_snapshot={"BIG1": -6000.0, "BIG2": -10.0, "CB1": -300.0, "CB2": -300.0})
_nb.churn.add_lot("CA", 500, 0.98, _t)
check("hf arb: races recycling may not buy: Rhode Island Governor, races it holds, races with > 4,000 shares on a leg; others are fine",
      R.Bot.churn_no_buy(_nb, "Rhode Island Governor") and R.Bot.churn_no_buy(_nb, "CA") and R.Bot.churn_no_buy(_nb, "BIG")
      and not R.Bot.churn_no_buy(_nb, "CB") and not R.Bot.churn_no_buy(types.SimpleNamespace(churn=None), "CA"))
_hb = R.Bot.__new__(R.Bot)
_hb.churn = None
_hb.arb = ArbEngine.__new__(ArbEngine)
check("hf arb: the hooks are wired only when the sleeve runs (defaults leave the arb engine as before)",
      _hb.arb.protected() == set() and _hb.arb.no_buy("Rhode Island Governor") is False
      and "self.arb.protected = lambda: {l[\"race\"] for l in self.churn.lots}" in _rs6 and "self.arb.no_buy = self.churn_no_buy" in _rs6)

# fix 1: sets another path closed are booked as an exit (recorded unwind proceeds, else the exit value now) and the gap starts
_b, _v, _ch = _hbot({"CA1": 0.0, "CA2": 0.0}, _legs("CA", 0.47, 0.48), lots=[("CA", 2000, 0.985, _t - 100)])
_b.store.record_arb("CA", "unwind_set", [], 2000, 0.0, 1.0, True, {})
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: sets a free unwind closed are booked at its recorded proceeds (+30), the 30-min re-entry gap starts, nothing is sold again",
      _ch.held("CA") == 0 and abs(_ch.realised - 30.0) < 1e-6 and not _ch.eligible(_HR["CA"], time.time() + 600) and _sold(_v) == [],
      f"held {_ch.held('CA')} realised {_ch.realised} sold {_sold(_v)}")
_b, _v, _ch = _hbot({"CA1": -500.0, "CA2": -500.0}, _legs("CA", 0.51, 0.52), lots=[("CA", 2000, 0.985, _t - 100)], kill=-40.0)
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: without a recorded unwind the dropped sets are booked at the exit value now (1500 x -0.025); the stop sees it and sells the rest",
      abs(_ch.last_reconcile_pnl - 1500 * (0.96 - 0.985)) < 1e-6 and _ch.killed and _sold(_v) == [500] and abs(_ch.realised + 50.0) < 1e-6,
      f"booked {_ch.last_reconcile_pnl} realised {_ch.realised} killed {_ch.killed} sold {_sold(_v)}")

# fix 2: scanner paths refresh the shared snapshot inside the trade lock, right after the unwind
class _SpyLock:
    held = False

    def __enter__(self):
        self.held = True

    def __exit__(self, *a):
        self.held = False


def _spybot(**kw):
    lk, seen = _SpyLock(), []
    fb = types.SimpleNamespace(feed_ok=lambda: True, killed_until=0.0, last_free_unwind=0.0, universe=types.SimpleNamespace(races=_HR),
                               fresh_book=lambda ex, age=None: {}, trade_lock=lk, positions_snapshot={"CA1": -100.0, "CA2": -100.0}, cash=0.0,
                               scan_api=types.SimpleNamespace(account=lambda: {"balance": 1.0}), **kw)
    fb.refresh_snapshot = lambda d=None: R.Bot.refresh_snapshot(fb, d)
    fb.unwind_deltas = lambda u: R.Bot.unwind_deltas(fb, u)
    fb.positions_map = lambda strict=False: (seen.append(lk.held), ({"CA1": 0.0, "CA2": 0.0}, 0.0, 0))[1]
    fb.fresh_positions = lambda: R.Bot.fresh_positions(fb)
    sc = R.ArbScanner.__new__(R.ArbScanner)
    sc.bot, sc.stats, sc.best_unfunded = fb, {}, None
    return sc, fb, seen


_sc, _fb, _seen = _spybot(arb=types.SimpleNamespace(committed={"CA": 1.0}, free_unwinds=lambda h, p, c, refresh=None: [{"race": "CA", "qty": 100, "proceeds": 1.0}]),
                          cfg=types.SimpleNamespace(free_unwind_scan_sec=0.0))
_sc.free_unwind_pass()
check("hf arb: the scanner's free unwind refreshes the shared snapshot inside the trade lock (sim churn_interleave: sets sold twice)",
      _seen == [True] and _fb.positions_snapshot == {"CA1": 0.0, "CA2": 0.0}, str(_seen))


def _raise(strict=False):
    raise _ApiErr(503, "DOWN", "down")


_fb.positions_map, _fb.positions_snapshot, _fb.last_free_unwind = _raise, {"CA1": -100.0, "CA2": -100.0}, 0.0
_sc.free_unwind_pass()
check("hf arb: if the positions read fails, the unwound sets are taken out of the snapshot locally", _fb.positions_snapshot == {"CA1": 0.0, "CA2": 0.0},
      str(_fb.positions_snapshot))


def _no_acct():
    raise _ApiErr(503, "DOWN", "down")


_sc, _fb, _seen = _spybot(arb=types.SimpleNamespace(committed={"CB": 1.0}, recycle_credit=0.0, cooldown={}, protected=lambda: set(),
                                                    recycle=lambda *a, **k: {"race": "CB", "qty": 100, "proceeds": 0.98}),
                          cfg=types.SimpleNamespace(arb=types.SimpleNamespace(recycle_enabled=True, new_sets=False, min_set_qty=20)))
_fb.scan_api = types.SimpleNamespace(account=_no_acct)
_sc.best_unfunded = {"race": "CA", "kind": "sell_set", "ret": 0.05, "cost": 0.95, "qty": 500, "ts": time.time()}
_sc.recycle_pass()
check("hf arb: a recycle unwind refreshes the snapshot inside the same trade lock", _seen and _seen[0] is True, str(_seen))
check("hf arb: the main cycle publishes its positions to the shared snapshot under the trade lock (an in-lock scanner refresh is never overwritten)",
      "            self.positions_snapshot = positions\n" in _rs6 and "        self.positions_snapshot = positions      # read by" not in _rs6)

# fix 3: the sell-all from 3 Nov 22:30 UTC: every two-leg set in a race it traded (not RI Governor / > 4,000-share races), halving
_old_sa = DC.SELL_ALL_AT
DC.SELL_ALL_AT = time.time() - 10
try:
    _books = dict(_legs("CA", 0.49, 0.50, 1100), **_legs("CB", 0.49, 0.50))
    _pos = {"CA1": -2500.0, "CA2": -2500.0, "CB1": -300.0, "CB2": -300.0, "Rhode Island Governor1": -1000.0, "Rhode Island Governor2": -1000.0,
            "BIG1": -6000.0, "BIG2": -6000.0}
    _b, _v, _ch = _hbot(_pos, _books, lots=[("CA", 2000, 0.98, _t - 100)], races=["CB", "Rhode Island Governor", "BIG"])
    _n = []
    for _i in range(5):
        R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
        _b.positions_snapshot = dict(_v.pos)
        _n.append(len(_sold(_v)))
    _p = _v.pos
    check("hf arb: sell-all sells every set in the races it traded, ledger or not, halving on thin asks, retried every cycle",
          _p["CA1"] == 0 and _p["CA2"] == 0 and _p["CB1"] == 0 and _ch.lots == [] and _sold(_v)[:2] == [1000, 300] and _n[-1] == _n[-2],
          f"pos {_p} sold {_sold(_v)} per cycle {_n} lots {_ch.lots}")
    check("hf arb: sell-all never touches Rhode Island Governor or a race holding > 4,000 shares on a leg",
          _p["Rhode Island Governor1"] == -1000.0 and _p["BIG1"] == -6000.0)
    check("hf arb: no entries once the sell-all is due", not _ch.eligible(_HR["CB"], time.time()))
    _nb2 = types.SimpleNamespace(churn=_ch, universe=types.SimpleNamespace(races=_HR), cfg=types.SimpleNamespace(churn_skip_holding=4000.0),
                                 positions_snapshot={})
    check("hf arb: from the sell-all on, recycling never buys back into a race it traded (no sell / re-buy loop)",
          R.Bot.churn_no_buy(_nb2, "CB") and not R.Bot.churn_no_buy(types.SimpleNamespace(churn=DC.DevChurn(), universe=_nb2.universe,
                                                                                            cfg=_nb2.cfg, positions_snapshot={}), "CB"))
finally:
    DC.SELL_ALL_AT = _old_sa

# fix 4: a 9-tick loss per set (6 until param_audit.md, 24 Oct), or the stop, sells the race at the touch (not held to
# 24 h); no re-entry there for 24 h. Cost 0.985 per set: loss = sum of the two asks - 1.015
_b, _v, _ch = _hbot({"CA1": -2000.0, "CA2": -2000.0}, _legs("CA", 0.525, 0.53), lots=[("CA", 2000, 0.985, _t - 100)])
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: a race 9+ ticks per set under water is sold at the touch, and is not bought again for 24 h",
      _sold(_v) == [2000] and _ch.held("CA") == 0 and "CA" in _ch.last_loss and not _ch.eligible(_HR["CA"], time.time() + 23 * 3600)
      and _ch.eligible(_HR["CA"], time.time() + 25 * 3600), str(_sold(_v)))
# loss exit widened to 9 ticks: a set 7 or 8 ticks under water is held (no sale, no 24-h lockout); 9 and 10 ticks are sold
_lx = {}
for _tk, (_a1, _a2) in {7: (0.525, 0.525), 8: (0.525, 0.53), 9: (0.53, 0.53), 10: (0.53, 0.535)}.items():
    _bk = dict(zip(_HR["CA"].exchange_ids, (_book(_a1 - 0.005, _a1, 50000), _book(_a2 - 0.005, _a2, 50000))))
    _b, _v, _ch = _hbot({"CA1": -2000.0, "CA2": -2000.0}, _bk, lots=[("CA", 2000, 0.985, _t - 100)])
    R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
    _lx[_tk] = (_sold(_v), _ch.held("CA"), "CA" in _ch.last_loss)
check("hf arb: 7 and 8 ticks per set under water are held; 9 and 10 ticks are sold at the touch with the 24-h lockout",
      _lx[7] == ([], 2000, False) and _lx[8] == ([], 2000, False) and _lx[9] == ([2000], 0, True) and _lx[10] == ([2000], 0, True),
      str(_lx))
check("hf arb: the loss exit is 9 ticks per set", abs(DC.LOSS_EXIT - 0.045) < 1e-12 and "9 ticks" in DC.LOSS_WHY, f"{DC.LOSS_EXIT} {DC.LOSS_WHY}")
_b, _v, _ch = _hbot({"CA1": -2000.0, "CA2": -2000.0}, _legs("CA", 0.51, 0.5175), lots=[("CA", 2000, 0.97, _t - 100)], kill=-5.0)
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: when the stop trips, every churn set is sold at the touch in the same cycle", _ch.killed and _sold(_v) == [2000], str(_sold(_v)))
check("hf arb: exit reasons: stop and 9-tick loss before take-profit / 24 h (6 and 8 ticks under water are held)",
      DC.DevChurn(kill=-1).exit_reason({"cost": 0.97, "ts": _t}, 0.969, _t) is None
      and DC.DevChurn().exit_reason({"cost": 0.97, "ts": _t}, 0.925, _t) == DC.LOSS_WHY
      and DC.DevChurn().exit_reason({"cost": 0.97, "ts": _t}, 0.9255, _t) is None
      and DC.DevChurn().exit_reason({"cost": 0.97, "ts": _t}, 0.930, _t) is None
      and DC.DevChurn().exit_reason({"cost": 0.97, "ts": _t}, 0.940, _t) is None)
_k = DC.DevChurn(); _k.killed = True
check("hf arb: a tripped stop gives the stop exit", _k.exit_reason({"cost": 0.97, "ts": _t}, 0.99, _t) == DC.STOP_WHY)

# fix 5: churn sells its ledger's sets only, never MM inventory that also sits on both legs
_b, _v, _ch = _hbot({"CA1": -5000.0, "CA2": -5000.0}, _legs("CA", 0.48, 0.49), lots=[("CA", 2000, 0.97, _t - 100)])
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: with 5,000 NO on both legs (MM inventory too) and 2,000 in the ledger, the +2-tick exit sells 2,000",
      _sold(_v) == [2000] and _v.pos["CA1"] == -3000.0 and _b.positions_snapshot["CA1"] == -3000.0 and abs(_ch.realised - 100.0) < 1e-6,
      f"sold {_sold(_v)} pos {_v.pos} realised {_ch.realised}")

# fix 6: a partly filled entry books only what traded (ledger, snapshot, cash)
_b, _v, _ch = _hbot({}, _legs("CA", 0.52, 0.525, 2000))
_b.churn_med = {"CA": -0.01}
_v.fill = 1200
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: a basket that fills 1,200 of 2,000 sets books 1,200 in the ledger, the snapshot and cash",
      _ch.held("CA") == 1200 and _b.positions_snapshot.get("CA1") == -1200.0 and abs(_b.cash - (50000.0 - 1200 * 0.96)) < 1e-6,
      f"held {_ch.held('CA')} snap {_b.positions_snapshot} cash {_b.cash}")
_b, _v, _ch = _hbot({"CA1": 14.0}, _legs("CA", 0.52, 0.525, 2000))
_b.churn_med = {"CA": -0.01}
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: an entry that nets 14 NO against YES the MM held on a leg books the 1,986 sets the venue shows (cash: all 2,000 traded)",
      _ch.held("CA") == 1986 and abs(_b.cash - (50000.0 - 2000 * 0.96)) < 1e-6, f"held {_ch.held('CA')} cash {_b.cash}")
_st = DC.DevChurn(); _st.load(_ch.to_json())
check("hf arb: traded races, loss gaps and booked unwind ids survive a restart", _st.races == {"CA"} and _st.seen_unwind == _ch.seen_unwind)


# Fable review 2 (24 Oct): every exit is clamped to what the venue holds when it is sent (re-read under the trade lock)
_b, _v, _ch = _hbot({"CA1": -2000.0, "CA2": -2000.0}, _legs("CA", 0.48, 0.49), lots=[("CA", 2000, 0.97, _t - 100)])
_v.pos["CA1"] = -1700.0              # an MM bid fill for 300 YES on leg 1 after the cycle's positions read (snapshot still -2,000)
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: an MM fill after the cycle's read shrinks the exit to the 1,700 sets held now; no leg goes through zero",
      _sold(_v) == [1700] and _v.pos["CA1"] == 0.0 and _v.pos["CA2"] == -300.0 and _ch.held("CA") == 0, f"sold {_sold(_v)} pos {_v.pos}")
_b, _v, _ch = _hbot({"CA1": -2000.0, "CA2": -2000.0}, _legs("CA", 0.48, 0.49), lots=[("CA", 2000, 0.97, _t - 100)])
_b.positions_map = _raise
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: if the re-read before an exit fails, nothing is sent (retried next cycle)", _sold(_v) == [] and _ch.held("CA") == 2000, str(_sold(_v)))
_b, _v, _ch = _hbot({"CA1": -2000.0, "CA2": -2000.0}, _legs("CA", 0.48, 0.49), lots=[("CA", 2000, 0.97, _t - 100)])
_b.positions_snapshot, _b.positions_ok = {}, False
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("hf arb: after a failed positions read (empty snapshot) the ledger is not reconciled away", _ch.held("CA") == 2000 and _ch.realised == 0.0)
_b, _v, _ch = _hbot({"CA1": -1700.0, "CA2": -2000.0}, _legs("CA", 0.47, 0.48))
_b.store.record_arb("CA", "sell_set", [], 2000, 0.02, 0.98, True, {})
_fu = _b.arb.free_unwinds({"CA": _legs("CA", 0.47, 0.48)}, {"CA1": -2000.0, "CA2": -2000.0}, None, refresh=lambda: dict(_v.pos))
check("hf arb: a free unwind is re-sized to the positions read right before it is sent (1,700, not the stale 2,000)",
      [u["qty"] for u in _fu] == [1700] and _v.pos["CA1"] == 0.0, f"{_fu} {_v.pos}")
_b, _v, _ch = _hbot({"CA1": -2000.0, "CA2": -2000.0}, _legs("CA", 0.47, 0.48))
_b.store.record_arb("CA", "sell_set", [], 2000, 0.02, 0.98, True, {})
check("hf arb: a free unwind whose re-read fails sends nothing",
      _b.arb.free_unwinds({"CA": _legs("CA", 0.47, 0.48)}, {"CA1": -2000.0, "CA2": -2000.0}, None, refresh=lambda: None) == [] and _v.sent == [])
_p0 = R.Bot.positions_map
_pm = types.SimpleNamespace(api=types.SimpleNamespace(positions=lambda slug: _raise()), slug="s")
try:
    _strict_raises = False
    R.Bot.positions_map(_pm, strict=True)
except _ApiErr:
    _strict_raises = True
check("positions: a failed read raises in strict mode (exits) and flags positions_ok; the lenient read still returns {}",
      _strict_raises and _pm.positions_ok is False and R.Bot.positions_map(_pm)[0] == {})


# Fable review 3 (24 Oct): recycle and offload unwinds are re-sized to a positions read right before they are sent
_e = _hengine()
_r_stale = _e.recycle(dict(best), {"HELD": held_books}, _hpos, 1000.0)
_e = _hengine()
_r_fresh = _e.recycle(dict(best), {"HELD": held_books}, _hpos, 1000.0, refresh=lambda: {"h1": -5000.0, "h2": -34479.0})
_e = _hengine()
_r_fail = _e.recycle(dict(best), {"HELD": held_books}, _hpos, 1000.0, refresh=lambda: None)
check("recycle: an MM fill since the snapshot (leg h1 down to 5,000 NO) shrinks the unwind to the sets held now; a failed re-read sends nothing",
      _r_stale is not None and _r_stale["qty"] > 5000 and _r_fresh is not None and _r_fresh["qty"] <= 5000 and _r_fail is None,
      f"stale {_r_stale and _r_stale['qty']} fresh {_r_fresh and _r_fresh['qty']} fail {_r_fail}")
_b, _v, _ch = _hbot({"CB1": -1700.0, "CB2": -2000.0}, _legs("CB", 0.47, 0.48))
_res = _b.arb.unwind_set("CB", _legs("CB", 0.47, 0.48), 2000, why="fund tilt", refresh=lambda: dict(_v.pos))
check("offload unwind_set: re-sized to the 1,700 sets held now; no leg goes through zero",
      _res is not None and _res["qty"] == 1700 and _sold(_v) == [1700] and _v.pos["CB1"] == 0.0 and _v.pos["CB2"] == -300.0, f"{_res} {_v.pos}")
_b, _v, _ch = _hbot({"CB1": -2000.0, "CB2": -2000.0}, _legs("CB", 0.47, 0.48))
check("offload unwind_set: a failed re-read sends nothing",
      _b.arb.unwind_set("CB", _legs("CB", 0.47, 0.48), 2000, why="fund tilt", refresh=lambda: None) is None and _v.sent == [])
check("recycle and offload call sites pass the re-read hook",
      _rs6.count("refresh=b.fresh_positions") >= 2 and "cycle_id, refresh=self.fresh_positions)" in _rs6 and _rs6.count("refresh=self.fresh_positions") >= 3)

# ---------------------------------------------------------------- 24 Oct: cheap safe favourites (proposals/cheap_favourites.py)
from sigbot.proposals import cheap_favourites as CF  # noqa: E402
_same = CF.same_terms_exchanges(os.path.join(ROOT, "data", "research_contract_rules", "tc_mapping.json"))
check("cheapfav: contract map: Rhode Island Senate legs are same-terms; GA Senate (runoff, late) is not; a missing file gives None",
      _same is not None and {"1076", "1077"} <= _same and not any(
          v.get("race") == "Georgia Senate" and str(k) in _same for k, v in __import__("json").load(open(os.path.join(ROOT, "data", "research_contract_rules", "tc_mapping.json"))).items()
          if isinstance(v, dict)) and CF.same_terms_exchanges("/nonexistent/tc.json") is None)


def _fr(race, party, ex, fair, bid, ask, pm=None, k=None):
    pm = fair if pm is None else pm
    k = fair if k is None else k
    return {"race": race, "party": party, "exchange_id": ex, "fair": fair, "sig_bid": bid, "sig_ask": ask, "pm": pm, "kalshi": k}


_RI = [_fr("Rhode Island Senate", "D", "d1", 0.984, 0.865, 0.875), _fr("Rhode Island Senate", "R", "r1", 0.016, 0.135, 0.14)]
_cf = CF.CheapFavourites(same_terms={"d1", "r1", "d2", "r2", "d3", "r3", "d4", "r4", "d5", "r5", "d6", "r6", "i6", "d7", "r7"})
_c = _cf.candidates(_RI, {})
check("cheapfav: one route per race outcome: RI Senate D via R-NO at 0.865 (edge 11.9c) beats D-YES at 0.875",
      len(_c) == 1 and _c[0]["exchange_id"] == "r1" and _c[0]["side"] == "no" and abs(_c[0]["price"] - 0.865) < 1e-9 and _c[0]["win_party"] == "D", str(_c))
_c = _cf.candidates(_RI, {"r1": 300.0})
check("cheapfav: never a route that trades against a holding on that leg (held R YES -> buy D YES instead of R NO)",
      len(_c) == 1 and _c[0]["exchange_id"] == "d1" and _c[0]["side"] == "yes", str(_c))
check("cheapfav: no candidates in a race where we hold > 4,000 shares on a leg", _cf.candidates(_RI, {"d1": -4100.0}) == [])
check("cheapfav: legs not in the same-terms map are never traded", CF.CheapFavourites(same_terms={"zz"}).candidates(_RI, {}) == []
      and CF.CheapFavourites(same_terms=None).candidates(_RI, {}) == [])
_slow = [_fr("Arizona Governor", "D", "d2", 0.95, 0.88, 0.89), _fr("Arizona Governor", "R", "r2", 0.05, 0.11, 0.12)]
_late = [_fr("Alaska Senate", "D", "d3", 0.95, 0.88, 0.89), _fr("Alaska Senate", "R", "r3", 0.05, 0.11, 0.12)]
_punt = [_fr("Minnesota Senate", "D", "d4", 0.95, 0.88, 0.89), _fr("Minnesota Senate", "R", "r4", 0.05, 0.11, 0.12)]
check("cheapfav: slow-count states (AZ, AK) and the punt's candidate races (MN Senate) are skipped",
      _cf.candidates(_slow + _late + _punt, {}) == [])
_lowfav = [_fr("Ohio Senate", "D", "d5", 0.70, 0.60, 0.61), _fr("Ohio Senate", "R", "r5", 0.30, 0.39, 0.40)]
check("cheapfav: favourites only (p >= 0.80): a 0.70 leg 9c cheap is not bought", _cf.candidates(_lowfav, {}) == [])
_ind = [_fr("Nebraska Senate", "D", "d6", 0.02, 0.01, 0.02), _fr("Nebraska Senate", "R", "r6", 0.95, 0.90, 0.91),
        _fr("Nebraska Senate", "I", "i6", 0.03, 0.02, 0.03)]
check("cheapfav: no race with an Independent leg", _cf.candidates(_ind, {}) == [])
_dis = [_fr("Rhode Island Senate", "D", "d1", 0.95, 0.88, 0.89, pm=0.97, k=0.92), _fr("Rhode Island Senate", "R", "r1", 0.05, 0.11, 0.12, pm=0.03, k=0.08)]
check("cheapfav: no entry while Polymarket and Kalshi disagree by > 4 pts", _cf.candidates(_dis, {}) == [])
_one = [_fr("Kentucky Senate", "D", "d7", 0.026, 0.050, 0.055, k=None), _fr("Kentucky Senate", "R", "r7", 0.974, 0.94, 0.95, k=None)]
_one[0]["kalshi"] = _one[1]["kalshi"] = None
_c = _cf.candidates(_one, {})
check("cheapfav: single-venue legs need 3c: KY Senate R via D-NO at 0.950 (2.4c) skipped, at 0.945 (2.9c) still skipped",
      _c == [] and _cf.candidates([dict(_one[0], sig_bid=0.04), dict(_one[1], sig_ask=0.96)], {}) == [])
_c4 = _cf.candidates([dict(_one[0], sig_bid=0.06), dict(_one[1], sig_ask=0.96)], {})
check("cheapfav: ... and at 0.940 (3.4c) bought", len(_c4) == 1 and _c4[0]["side"] == "no", str(_c4))

_cands = [{"race": "B Senate", "win_party": "D", "exchange_id": "b", "side": "yes", "price": 0.875, "p_win": 0.984, "edge": 0.109, "weight": 6.8},
          {"race": "A Senate", "win_party": "R", "exchange_id": "a", "side": "no", "price": 0.935, "p_win": 0.987, "edge": 0.052, "weight": 4.0},
          {"race": "C Senate", "win_party": "R", "exchange_id": "c", "side": "yes", "price": 0.910, "p_win": 0.949, "edge": 0.039, "weight": 0.8}]
_cf2 = CF.CheapFavourites(budget=3000, leg_cap=1500, order_notional=500)
_t = _cf2.targets(_cands)
check("cheapfav: targets spend the budget by weight, each leg capped at 1,500", abs(sum(_t.values()) - 3000) < 1 and max(_t.values()) <= 1500 + 1e-6
      and _t[("B Senate", "D")] == 1500, str(_t))
_touch = {"a": (0.935, 5000), "b": (0.875, 5000), "c": (0.910, 5000)}
_P = {"other": 10.0}                       # a non-empty positions read (empty = the read failed)
_o = _cf2.entries(_cands, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 6300.0)
check("cheapfav: book over the partisan cap on the D side (+6,300): D favourites blocked, R favourites bought (orders <= 500)",
      [x["exchange_id"] for x in _o] == ["a", "c"] and all(x["notional"] <= 500 + 1e-6 for x in _o), str(_o))
_o = _cf2.entries(_cands[:1], {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 3600.0)
_o2 = _cf2.entries(_cands[:1], {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 3400.0)
check("cheapfav: 1,000 of partisan headroom left to the other sleeves: a 500 D order is refused at +3,600 (4,100) and allowed at +3,400",
      _o == [] and len(_o2) == 1, f"{_o} {_o2}")
_o = _cf2.entries(_cands[:1], {("X Senate", "D"): 1800.0}, 1800.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 0.0)
_o2 = _cf2.entries(_cands[1:2], {("X Senate", "R"): 1800.0}, 1800.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 6300.0)
check("cheapfav: the sleeve's own net D stays within +-2,000 whatever the book (own +1,800: no 500 D; own -1,800: no 500 R even "
      "while the book is long D)", _o == [] and _o2 == [], f"{_o} {_o2}")
_D3 = [dict(_cands[0], race=r, exchange_id=e) for r, e in (("B Senate", "b"), ("E Senate", "e"), ("F Senate", "f"))]
_t3 = {"b": (0.875, 5000), "e": (0.875, 5000), "f": (0.875, 5000)}
_o = _cf2.entries(_D3, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _t3, _P, 3400.0, pend=(0.0, 1500.0), own=(0.0, 0.0, 1500.0))
check("cheapfav: unfilled R orders never make room for D adds (book +3,400 with 1,500 R pending that expired: one 500 D, not three)",
      len(_o) == 1, str(_o))
_o = _cf2.entries(_D3, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _t3, _P, 3000.0, pend=(1200.0, 0.0))
check("cheapfav: another sleeve's unfilled D orders count against D adds (book +3,000, 1,200 D in flight: no D add)", _o == [], str(_o))
_o = _cf2.entries(_cands, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 0.0, blocked={"D"})
check("cheapfav: never adds to a direction the bot blocked this cycle (D blocked at net D 0: only R favourites)",
      [x["win_party"] for x in _o] == ["R", "R"], str(_o))
_o = _cf2.entries(_cands, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 6300.0)
check("cheapfav: book long D over the cap: R favourites (narrowing the book) are bought within the own-net band",
      [x["win_party"] for x in _o] == ["R", "R"] and sum(x["notional"] for x in _o) > 0, str(_o))
_o = _cf2.entries(_cands, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 0.0)
check("cheapfav: with room: one order per leg, highest weight first, at the touch", [x["exchange_id"] for x in _o] == ["b", "a", "c"]
      and _o[0]["qty"] == int(500 / 0.875), str(_o))
check("cheapfav: no entries after 3 Nov 17:30 ET", _cf2.entries(_cands, {}, 0.0, 20000.0, CF.LAST_ENTRY + 1, _touch, _P, 0.0) == [])
check("cheapfav: budget used up -> no entries; cash floor respected",
      _cf2.entries(_cands, {}, 3000.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 0.0) == []
      and sum(x["notional"] for x in _cf2.entries(_cands, {}, 0.0, 1600.0, CF.LAST_ENTRY - 3600, _touch, _P, 0.0)) <= 600 + 1e-6)
check("cheapfav: a leg's target reached -> no more orders on it",
      "b" not in [x["exchange_id"] for x in _cf2.entries(_cands, {("B Senate", "D"): 1500.0}, 1500.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 0.0)])
_o = _cf2.entries(_cands, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, {"b": (0.875, 5000)}, {"b": 3800.0}, 0.0)
check("cheapfav: never takes a leg past 4,000 shares (held 3,800 -> at most 200 more)", len(_o) == 1 and _o[0]["qty"] == 200, str(_o))
_o = _cf2.entries(_cands, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, {"b": (0.97, 5000), "a": (0.935, 100)}, _P, 0.0)
check("cheapfav: fresh book without the edge -> skipped; small touch -> at most 80% of it", [(x["exchange_id"], x["qty"]) for x in _o] == [("a", 80)], str(_o))

_now = CF.LAST_ENTRY - 3600
check("cheapfav: no exit while the edge holds; exit when SIG's bid is >= 2c above the fair on a held YES",
      _cf.exit_reason("d1", 500.0, _fr("Rhode Island Senate", "D", "d1", 0.95, 0.94, 0.945), "Rhode Island Senate", _now) is None
      and _cf.exit_reason("d1", 500.0, _fr("Rhode Island Senate", "D", "d1", 0.95, 0.97, 0.975), "Rhode Island Senate", _now) is not None)
check("cheapfav: held NO exits when SIG's ask is >= 2c under the fair",
      _cf.exit_reason("r1", -500.0, _fr("Rhode Island Senate", "R", "r1", 0.05, 0.025, 0.03), "Rhode Island Senate", _now) is not None)
check("cheapfav: no exit after the state's polls close (RI 20:00 ET), or while the venues disagree",
      _cf.exit_reason("d1", 500.0, _fr("Rhode Island Senate", "D", "d1", 0.95, 0.97, 0.975), "Rhode Island Senate",
                      CF.poll_close_ts("RI") + 1) is None
      and _cf.exit_reason("d1", 500.0, _fr("Rhode Island Senate", "D", "d1", 0.95, 0.97, 0.975, pm=0.99, k=0.92), "Rhode Island Senate", _now) is None)
_op, _cost, _held, _dep = CF.ledger([{"exchange_id": "r1", "quantity": -1000.0, "price": 0.135},
                                     {"exchange_id": "r1", "quantity": 400.0, "price": 0.11},
                                     {"exchange_id": "d1", "quantity": 500.0, "price": 0.875}], {"r1": ("RI", "R"), "d1": ("RI", "D")})
check("cheapfav: ledger from fills: NO 1,000 @0.865 then 400 closed -> 600 held costing 519; D outcome holds both legs",
      _op == {"r1": -600.0, "d1": 500.0} and abs(_cost["r1"] - 519.0) < 1e-6 and abs(_held[("RI", "D")] - (519.0 + 437.5)) < 1e-6
      and abs(_dep - 956.5) < 1e-6, f"{_op} {_cost} {_held} {_dep}")


class _FStore:
    def __init__(self, fills, recent=()): self.fills, self.orders, self.recent = fills, [], list(recent)
    def rows(self, q, a=()):
        if "FROM fills" in q: return self.fills
        if "FROM orders WHERE purpose='cheapfav' AND placed_at" in q: return self.recent
        if "FROM orders WHERE purpose='cheapfav_exit' AND placed_at" in q: return getattr(self, "recent_exits", [])
        if "FROM orders WHERE purpose IN ('catchup'" in q: return getattr(self, "others", [])
        return []
    def record_order(self, oid, ex, mid, side, act, px, q, purpose, **k): self.orders.append((ex, side, px, q, purpose))


def _cfbot(fills, rows, positions, net_d, killed=False, polled=True, now_ok=True, dry=False, recent=(), extra_bx=None):
    sent = []
    uni = types.SimpleNamespace(by_exchange=dict({"d1": ("Rhode Island Senate", "D"), "r1": ("Rhode Island Senate", "R")}, **(extra_bx or {})), races={})
    bks = {"d1": {"bids": [{"price": 0.865, "quantity": 5000}], "asks": [{"price": 0.875, "quantity": 5000}]},
           "r1": {"bids": [{"price": 0.135, "quantity": 5000}], "asks": [{"price": 0.14, "quantity": 5000}]}}
    cf = CF.CheapFavourites(budget=3000, leg_cap=1500, same_terms={"d1", "r1"})
    if not now_ok:
        cf.last_entry = 0.0
    b = types.SimpleNamespace(cheapfav=cf, dry_run=dry, store=_FStore(fills, recent), direction=types.SimpleNamespace(rows=rows), universe=uni,
                              open_orders_cache={}, cash=20000.0, cfg=types.SimpleNamespace(mm=types.SimpleNamespace(portfolio_max_partisan_notional=5000.0)),
                              fresh_book=lambda ex, age=None: bks.get(ex),
                              api=types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: sent.append((ex, side, q, px, key)) or {"orderId": len(sent)},
                                                        new_key=lambda p: p))
    b.cheapfav_refresh = lambda: R.Bot.cheapfav_refresh(b)
    b._cheapfav_trade = lambda *x: R.Bot._cheapfav_trade(b, *x)
    _orig = R.risk.exposure
    R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=net_d)
    try:
        R.Bot.cheapfav_cycle(b, {"polled": polled}, positions, {}, killed)
    finally:
        R.risk.exposure = _orig
    return b, sent


_b, _sent = _cfbot([], _RI, _P, 0.0)
check("cheapfav bot: one entry (RI Senate D via R NO @0.865 = 1 - 0.135), purpose cheapfav, <= 500 notional",
      len(_sent) == 1 and _sent[0][:2] == ("r1", "no") and abs(_sent[0][3] - 0.865) < 1e-9 and _sent[0][2] * _sent[0][3] <= 500 + 1e-6
      and _b.store.orders[0][4] == "cheapfav", str(_sent))
check("cheapfav bot: nothing when the kill switch is on, without a fresh poll, after the last entry, or in dry run",
      _cfbot([], _RI, _P, 0.0, killed=True)[1] == [] and _cfbot([], _RI, _P, 0.0, polled=False)[1] == []
      and _cfbot([], _RI, _P, 0.0, now_ok=False)[1] == [] and _cfbot([], _RI, _P, 0.0, dry=True)[1] == [])
check("cheapfav bot: D favourite blocked while the book is over the partisan cap", _cfbot([], _RI, _P, 6300.0)[1] == [])
_rev = [_fr("Rhode Island Senate", "D", "d1", 0.84, 0.865, 0.875), _fr("Rhode Island Senate", "R", "r1", 0.16, 0.135, 0.14)]
_b, _sent = _cfbot([{"exchange_id": "r1", "quantity": -600.0, "price": 0.135}], _rev, {"r1": -600.0}, 0.0)
check("cheapfav bot: reversed edge (SIG R ask 0.14 <= fair 0.16 - 2c) -> close the 600 held NO by buying YES @0.14, purpose cheapfav_exit",
      ("r1", "yes", 600, 0.14, "cfx") in _sent and any(o[4] == "cheapfav_exit" for o in _b.store.orders), str(_sent))
_b, _sent = _cfbot([{"exchange_id": "r1", "quantity": -600.0, "price": 0.135}], _RI, {"r1": -600.0}, 0.0)
check("cheapfav bot: restart-safe: the held position is rebuilt from fills, not exited while the edge holds, and the MM does not see it",
      not any(x[4] == "cfx" for x in _sent) and _b.cheapfav_open == {"r1": -600.0}
      and R.Bot.mm_positions(types.SimpleNamespace(catchup_open={}, cheapfav_open={"r1": -600.0}), {"r1": -650.0}) == {"r1": -50.0}, str(_sent))
check("cheapfav: off by default", __import__("sigbot.config", fromlist=["BotConfig"]).BotConfig().cheapfav_enabled is False)
check("cheapfav: live config.json runs it at the approved 15k (24 Oct): edge 2c, 2,000 per leg, 500 orders, floor 1,000",
      _live.get("cheapfav_enabled") is True and _live.get("cheapfav_budget") == 15000.0 and _live.get("cheapfav_min_edge") == 0.02
      and _live.get("cheapfav_leg_cap") == 2000.0 and _live.get("cheapfav_order_notional") == 500.0 and _live.get("cheapfav_cash_floor") == 1000.0)
# review 24 Oct (Fable): holes found in the first draft
check("cheapfav bot: positions read failed (empty map): no entries, the 4,000-share / no-crossing / partisan guards would be blind",
      _cfbot([], _RI, {}, 0.0)[1] == [] and _cf2.entries(_cands, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, {}, 0.0) == [])
_b, _sent = _cfbot([], _RI, _P, 0.0, recent=[{"order_id": 9, "exchange_id": "r1", "side": "no", "price": 0.865, "quantity": 1734}])
check("cheapfav bot: an entry order not yet in the fills table counts as held: the leg is not bought twice while the ingest lags",
      _sent == [], str(_sent))
_b, _sent = _cfbot([{"order_id": 9, "exchange_id": "r1", "quantity": -578.0, "price": 0.135}], _RI, _P, 0.0,
                   recent=[{"order_id": 9, "exchange_id": "r1", "side": "no", "price": 0.865, "quantity": 578}])
check("cheapfav bot: ... and a filled order is counted once (fills), not twice (fills + pending): the leg keeps buying to its target",
      len(_sent) == 1 and _sent[0][0] == "r1", str(_sent))
_c = _cf.candidates([dict(_one[0], sig_bid=0.06), dict(_one[1], sig_ask=0.96)], {})
check("cheapfav: single-venue leg: the fresh-book recheck keeps the 3c bar (2.5c at the touch -> no order)",
      len(_c) == 1 and _c[0]["need"] > 0.029 and _cf.entries(_c, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, {_c[0]["exchange_id"]: (0.949, 5000)}, _P, 0.0) == []
      and len(_cf.entries(_c, {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, {_c[0]["exchange_id"]: (0.94, 5000)}, _P, 0.0)) == 1, str(_c))
check("cheapfav: the > 4,000-share race skip also sees legs without a directional row",
      _cf.candidates(_RI[:1], {"r1": -4100.0}, {"Rhode Island Senate": ["d1", "r1"]}) == [] and len(_cf.candidates(_RI[:1], {"r1": -4100.0})) == 1)
check("cheapfav: the headroom follows the cap (cap 3,000: no D add at +2,850)",
      _cf2.entries(_cands[:1], {}, 0.0, 20000.0, CF.LAST_ENTRY - 3600, _touch, _P, 2850.0, cap=3000.0) == [])
_b, _sent = _cfbot([{"exchange_id": "r1", "quantity": -600.0, "price": 0.135}], _rev, {"r1": -200.0}, 0.0)
_b0, _ = _cfbot([], _RI, _P, 0.0)
_b0.churn = types.SimpleNamespace(held=lambda race: 2000 if race == "Rhode Island Senate" else 0)
_sent0 = []
_b0.api = types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: _sent0.append((ex, side, q, px)) or {"orderId": 1},
                                new_key=lambda p: p)
_b0.store = _FStore([])
_b0.cheapfav_refresh = lambda: R.Bot.cheapfav_refresh(_b0)
_b0._cheapfav_trade = lambda *x: R.Bot._cheapfav_trade(_b0, *x)
_orig_e = R.risk.exposure
R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=0.0)
try:
    R.Bot.cheapfav_cycle(_b0, {"polled": True}, _P, {}, False)
finally:
    R.risk.exposure = _orig_e
check("cheapfav bot: no entries in a race where the high-frequency arb holds sets (the two ledgers stay apart)", _sent0 == [], str(_sent0))
def _cfbot2(**attrs):
    b, _ = _cfbot([], _RI, _P, 0.0, dry=True)          # built, then run live with extra attributes
    b.dry_run = False
    for k, v in attrs.items():
        setattr(b, k, v)
    sent = []
    b.api = types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: sent.append((ex, side, q, px)) or {"orderId": 1},
                                  new_key=lambda p: p)
    b.store = _FStore([])
    _o = R.risk.exposure
    R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=0.0)
    try:
        R.Bot.cheapfav_cycle(b, {"polled": True}, _P, {}, False)
    finally:
        R.risk.exposure = _o
    return sent
check("cheapfav bot: honours the bot's own partisan block (mm.blocked = {'D'}: no D favourite; precheck inside_on 24 Oct)",
      _cfbot2(mm=types.SimpleNamespace(blocked={"D"})) == [] and len(_cfbot2(mm=types.SimpleNamespace(blocked=set()))) == 1)
import time as _tm
def _cfbot3(net_d, others=(), fresh=lambda: {"other": 10.0}):
    b, _ = _cfbot([], _RI, _P, 0.0, dry=True)
    b.dry_run, b.fresh_positions = False, fresh
    sent = []
    b.api = types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: sent.append((ex, side, q, px)) or {"orderId": 1},
                                  new_key=lambda p: p)
    b.store = _FStore([])
    b.store.others = list(others)
    _o = R.risk.exposure
    R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=net_d)
    try:
        R.Bot.cheapfav_cycle(b, {"polled": True}, _P, {}, False)
    finally:
        R.risk.exposure = _o
    return sent
check("cheapfav bot: a catch-up D order sent earlier this cycle and not yet filled counts against D adds (book 3,000 + 1,200 in flight)",
      _cfbot3(3000.0, [{"order_id": 77, "exchange_id": "d1", "side": "yes", "quantity": 1300, "price": 0.92, "placed_at": _tm.time() - 20}]) == []
      and len(_cfbot3(3000.0)) == 1)
check("cheapfav bot: entries use a positions read made under the lock; none if that read fails", _cfbot3(0.0, fresh=lambda: None) == [])
import time as _tm
check("cheapfav bot: an inside-quoter D order 4 min old (300 s TTL) still counts against D adds (book 3,000 + 1,200)",
      _cfbot3(3000.0, [{"order_id": 78, "exchange_id": "d1", "side": "yes", "quantity": 1300, "price": 0.92, "placed_at": _tm.time() - 240}]) == [])
check("cheapfav bot: the catch-up sweep has priority: no entry in a race where it holds a position or an entry context",
      _cfbot2(catchup_open={"d1": 300.0}) == [] and _cfbot2(catchup=types.SimpleNamespace(ctx={"r1": {}})) == [])
check("cheapfav bot: an exit closes the sleeve's own ledger (600 NO) even when the venue's net is smaller (other sleeves trade the leg)",
      ("r1", "yes", 600, 0.14, "cfx") in _sent, str(_sent))
_b, _sent = _cfbot([{"exchange_id": "r1", "quantity": -600.0, "price": 0.135}], _rev, {"r1": 1426.0}, 0.0)
check("cheapfav bot: ... and when another sleeve is net the other way on that leg (sim cheapfav 24 Oct: a clamp to the venue blocked it)",
      ("r1", "yes", 600, 0.14, "cfx") in _sent, str(_sent))
_b, _ = _cfbot([], _rev, _P, 0.0)
_xs = []
_b.store = _FStore([{"exchange_id": "r1", "quantity": -600.0, "price": 0.135}])
_b.store.recent_exits = [{"exchange_id": "r1"}]      # an exit sent 2 min ago; its fill not ingested yet
_b.api = types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: _xs.append((ex, side, q, px, key)) or {"orderId": 1},
                               new_key=lambda p: p)
_orig_e = R.risk.exposure
R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=0.0)
try:
    R.Bot.cheapfav_cycle(_b, {"polled": True}, {"r1": -600.0}, {}, False)
finally:
    R.risk.exposure = _orig_e
check("cheapfav bot: no second exit on a leg within 5 min of the last one (a failed fill ingest cannot flip the position)",
      not any(x[4] == "cfx" for x in _xs), str(_xs))
# review 24 Oct (Fable, rebased patch)
_c = _cf.candidates(_RI, {"d1": -300.0}, {"Rhode Island Senate": ["d1", "r1"]})
check("cheapfav: never a NO next to a NO on another leg (D NO held -> no R NO route; D YES blocked by the no-crossing rule)",
      _c == [], str(_c))
_b, _sent = _cfbot([], _RI, _P, 0.0, recent=[{"order_id": 9, "exchange_id": "zz", "side": "yes", "price": 0.9, "quantity": 5000}],
                   extra_bx={"zz": ("Ohio Senate", "D")})
_b2, _sent2 = _cfbot([], _RI, _P, 0.0)
check("cheapfav bot: unfilled cheap-favourite orders count on the partisan axis (4,500 D pending at net D 0 -> no D add; none pending -> buys)",
      _sent == [] and len(_sent2) == 1, f"{_sent} {_sent2}")
_b = _cfbot([{"exchange_id": "r1", "quantity": -600.0, "price": 0.135}], _RI, _P, 0.0)[0]
_b.universe = types.SimpleNamespace(by_exchange={"d1": ("Rhode Island Senate", "D"), "r1": ("Rhode Island Senate", "R")}, races={})
check("cheapfav bot: races it holds are protected from the set paths (cheapfav_races)", R.Bot.cheapfav_races(_b) == {"Rhode Island Senate"})
_rs4 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("cheapfav wiring: arb.protected includes cheap-favourite races; churn sell-all and churn entries skip them; entries trade under the lock",
      "self.arb.protected = lambda: set(_prot()) | self.cheapfav_races()" in _rs4 and _rs4.count("in cheapfav_races_of(self):") >= 2
      and "with lock:                                   # the scanner thread trades sets between cycles" in _rs4
      and _rs4.count("in cf_races:") >= 1 and _rs4.count("or rname in cf_races:") >= 1)
_fu_seen = []
_fub = types.SimpleNamespace(arb=types.SimpleNamespace(committed={"Rhode Island Senate": 1.0, "Ohio Senate": 1.0},
                                                       free_unwinds=lambda held, pos, cid, **k: _fu_seen.append(sorted(held)) or []),
                             fresh_positions=lambda: {},
                             feed_ok=lambda: True, killed_until=0.0, last_free_unwind=0.0, cfg=types.SimpleNamespace(free_unwind_scan_sec=0.0),
                             universe=types.SimpleNamespace(races={"Rhode Island Senate": types.SimpleNamespace(exchange_ids=["d1", "r1"]),
                                                                   "Ohio Senate": types.SimpleNamespace(exchange_ids=["d5", "r5"])}),
                             fresh_book=lambda ex, age=None: {"bids": [], "asks": []}, trade_lock=threading.Lock(), positions_snapshot={},
                             cheapfav_races=lambda: {"Rhode Island Senate"})
R.ArbScanner.free_unwind_pass(types.SimpleNamespace(bot=_fub))
check("cheapfav: the scanner's free unwinds never touch a race the cheap-favourites sleeve holds", _fu_seen == [["Ohio Senate"]], str(_fu_seen))
_b, _sent = _cfbot([], _RI, _P, 0.0)
_b.cheapfav_skip = {}
_bad = []
_b.api = types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: _bad.append(ex) or {}, new_key=lambda p: p)
_b.store = _FStore([])
_orig_e = R.risk.exposure
R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=0.0)
try:
    R.Bot.cheapfav_cycle(_b, {"polled": True}, _P, {}, False)
    _n1 = len(_bad)
    R.Bot.cheapfav_cycle(_b, {"polled": True}, _P, {}, False)
finally:
    R.risk.exposure = _orig_e
check("cheapfav bot: an entry whose response has no orderId blocks that leg for 1 h (its fill could not be attributed)",
      _n1 == 1 and len(_bad) == 1 and _b.cheapfav_skip.get("r1", 0) > 0, f"{_bad} {_b.cheapfav_skip}")

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
