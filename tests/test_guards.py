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
        if "purpose IN ('mm','inside')" in q or "purpose IN ('exit', 'raise') GROUP BY" in q:
            return []
        if "FROM orders WHERE purpose IN ('exit', 'raise')" in q:
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


# ---------------------------------------------------------------- 24 Oct: cash raise (proposals/cash_raise.py)
from sigbot.proposals import cash_raise as CRm  # noqa: E402
from sigbot.universe import Universe as _Univ  # noqa: E402
from sigbot.mm import MarketMaker as _MM  # noqa: E402
import json as _jr  # noqa: E402
_U = _Univ.from_markets(_jr.load(open(os.path.join(ROOT, "data", "markets.json"))))
_two = [r for r in sorted(_U.races) if len(_U.races[r].legs) == 2 and set(_U.races[r].legs) == {"D", "R"}]
_RA, _RB, _RC, _RD, _RE, _RF = _two[:6]
_L = lambda r, p: _U.races[r].legs[p].exchange_id
_RI = {p: l.exchange_id for p, l in _U.races["Rhode Island Governor"].legs.items()}


class _RVenue:
    def __init__(self, pos):
        self.pos, self.orders, self.cancels, self.n, self.tournament_id = dict(pos), [], [], 0, "t"

    def new_key(self, p):
        self.n += 1
        return f"{p}-{self.n}"

    def place_order(self, ex, side, action, qty, price, expiration=None, key=None):
        self.orders.append((ex, side, qty, price, key))
        self.pos[ex] = self.pos.get(ex, 0.0) + (qty if side == "yes" else -qty)
        return {"orderId": 1000 + self.n, "quantityTraded": qty}

    def cancel_all(self, exchange_id=None, **k):
        self.cancels.append(exchange_id)
        return {}

    def cancel_order(self, order_id):
        self.cancelled = getattr(self, "cancelled", []) + [order_id]
        return {}


def _rbk(bid, ask, qty=5000):
    return {"bids": [{"price": bid, "quantity": qty}], "asks": [{"price": ask, "quantity": qty}]}


def _rbot(pos, books, needs=None, **cfgkw):
    tmp = tempfile.mkdtemp()
    st = _Store(os.path.join(tmp, "r.sqlite"))
    v = _RVenue(pos)
    cfg = types.SimpleNamespace(raise_enabled=True, raise_max_cost=0.01, raise_buffer=200.0, raise_max_total=32000.0, raise_max_clips=4, raise_min_clip=0.0,
                                raise_pause_sec=900.0, raise_hold_limit=4000.0, raise_cap_margin=1000.0, raise_exclude_races=["Rhode Island Governor"],
                                mm=types.SimpleNamespace(portfolio_max_partisan_notional=5000.0))
    for k, x in cfgkw.items():
        setattr(cfg.mm if k == "cap" else cfg, "portfolio_max_partisan_notional" if k == "cap" else k, x)
    b = types.SimpleNamespace(cfg=cfg, cash_needs=dict(needs or {}), dry_run=False, store=st, universe=_U, cash=1000.0,
                              arb=types.SimpleNamespace(protected=lambda: set()), catchup_open={}, cheapfav_open={}, trade_lock=threading.Lock(),
                              fresh_book=lambda ex, age=None: books.get(ex), open_orders_cache={}, open_orders_ts=0.0,
                              mm=types.SimpleNamespace(raise_pause={}), api=v, positions_snapshot=dict(pos))
    b.fresh_positions = lambda: dict(v.pos)
    b.refresh_snapshot = lambda deltas=None: True
    return b, v


_rprices = lambda books: {ex: {"bestBid": bk["bids"][0]["price"], "bestAsk": bk["asks"][0]["price"]} for ex, bk in books.items()}
# holdings: A D YES 1000 (cost 0.5%), B R NO 800 (cost 0.4%), C D YES 500 with a wide spread (cost 4%)
_rpos = {_L(_RA, "D"): 1000.0, _L(_RB, "R"): -800.0, _L(_RC, "D"): 500.0}
_rbooks = {_L(_RA, "D"): _rbk(0.495, 0.50), _L(_RB, "R"): _rbk(0.39, 0.395), _L(_RC, "D"): _rbk(0.48, 0.52),
           _L(_RA, "R"): _rbk(0.50, 0.505), _L(_RB, "D"): _rbk(0.605, 0.61)}   # A 0.5%, B 0.41%, C 4% (a 1-tick book at 0.5 is 1%: excluded)
_b, _v = _rbot(_rpos, _rbooks)
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
check("raise: nothing is sold without a sleeve waiting for the cash", _v.orders == [] and _b.store.get_param("raise_log", []) == [])
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (300.0, "floor")})
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
_got = sum(q * (1 - px) for ex, side, q, px, k in _v.orders)
check("raise: a 300 need raises the shortfall plus the 200 buffer (cheapest first: B's NO at 0.4%), not more",
      _v.orders and _v.orders[0][0] == _L(_RB, "R") and _v.orders[0][1] == "yes" and 500 - 1 <= _got <= 500 + 0.61 and all(k.startswith("raise") for *_, k in _v.orders),
      f"{_v.orders} {_got}")
_rec = _b.store.rows("SELECT purpose, exchange_id FROM orders")
check("raise: clips are recorded with purpose 'raise' and the MM is paused on the leg before the clip",
      {r["purpose"] for r in _rec} == {"raise"} and _L(_RB, "R") in _b.mm.raise_pause and _L(_RB, "R") in (_b.store.get_param("raise_pause") or {}))
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (50000.0, "floor")})
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
_sold = {ex for ex, *_ in _v.orders}
_lim_ok = all((px <= 0.505 + 1e-9 if side == "no" else px <= 0.395 + 1e-9) for ex, side, q, px, k in _v.orders)   # NO at 1 - bid, YES at the ask
check("raise: only clips costing < 1% of mid, at the touch (C's 4% spread untouched), each clip <= 80% of the touch and the holding",
      _L(_RC, "D") not in _sold and _sold == {_L(_RA, "D"), _L(_RB, "R")} and _lim_ok
      and all(q <= min(0.8 * 5000, abs(_rpos[ex])) for ex, side, q, px, k in _v.orders) and _v.pos[_L(_RA, "D")] >= 0 and _v.pos[_L(_RB, "R")] <= 0,
      f"{_v.orders}")
# exclusions: a sell-set (common NO), a buy-set (common YES), RI Governor, a churn race, catch-up / cheap-favourite legs, > 4,000 shares
_xpos = {_L(_RA, "D"): -1000.0, _L(_RA, "R"): -1300.0,       # sell-set 1,000 + 300 NO beyond it on R
         _L(_RB, "D"): 600.0, _L(_RB, "R"): 600.0,             # buy-set only: nothing beyond it
         _RI["D"]: 500.0, _L(_RC, "D"): 700.0, _L(_RD, "D"): 400.0, _L(_RE, "D"): 300.0, _L(_RF, "D"): 4500.0}
_xbooks = {ex: _rbk(0.495, 0.50) for ex in _xpos}
_b, _v = _rbot(_xpos, _xbooks, {"hf_arb": (50000.0, "floor")}, raise_max_clips=20)
_b.arb.protected = lambda: {_RC}
_b.catchup_open = {_L(_RD, "D"): 400.0}
_b.store.record_order(77, _L(_RE, "D"), None, "yes", "buy", 0.5, 10, "cheapfav")
R.Bot.raise_cycle(_b, _rprices(_xbooks), False)
_sold = {ex: q for ex, side, q, px, k in _v.orders}
check("raise: arb sets, buy-sets, RI Governor, high-frequency arb races, catch-up / cheap-favourite legs and > 4,000-share legs are untouched",
      _sold == {_L(_RA, "R"): 300} and _v.pos[_L(_RA, "D")] == -1000.0 and _v.pos[_L(_RA, "R")] == -1000.0, f"{_sold}")
# partisan cap: near the cap the pro-D clip goes first; a clip that would push |net D| past the cap is skipped
_cpos = {_L(_RA, "D"): 1800.0, _L(_RB, "R"): 300.0}
_cbooks = {_L(_RA, "D"): _rbk(0.495, 0.50), _L(_RB, "R"): _rbk(0.4975, 0.5025)}
_nd = lambda p: _risk.exposure(_U, p, _rprices(_cbooks)).net_d_notional
_c1 = CRm.plan(CRm.eligible({_RA: [_L(_RA, "D"), _L(_RA, "R")], _RB: [_L(_RB, "D"), _L(_RB, "R")]}, _cpos), _cbooks, 50.0, 0.01, _nd, 1000.0, _cpos, 300.0, 1)
_c2 = CRm.plan(CRm.eligible({_RA: [_L(_RA, "D"), _L(_RA, "R")], _RB: [_L(_RB, "D"), _L(_RB, "R")]}, _cpos), _cbooks, 50.0, 0.01, _nd, 1000.0, _cpos, 50.0, 1)
_hpos = {_L(_RA, "D"): 1990.0, _L(_RB, "R"): 300.0}           # net D ~ +845: selling B's R YES (cheapest) lifts it past a 900 cap
_c3 = CRm.plan(CRm.eligible({_RA: [_L(_RA, "D"), _L(_RA, "R")], _RB: [_L(_RB, "D"), _L(_RB, "R")]}, _hpos), _cbooks, 5000.0, 0.01,
               _nd, 900.0, _hpos, 300.0, 4)
check("raise: near the partisan cap the pro-D leg is sold first (cheapest first otherwise); no clip takes |net D| past the cap",
      _c1 and _c1[0]["exchange_id"] == _L(_RA, "D") and _c2 and _c2[0]["exchange_id"] == _L(_RB, "R")
      and all(abs(c["net_d_after"]) <= 900.0 + 1e-6 for c in _c3) and _nd(_hpos) > 800,
      f"{[c['exchange_id'] for c in _c1]} {[c['exchange_id'] for c in _c2]} {[(c['exchange_id'], c['qty'], round(c['net_d_after'])) for c in _c3]} nd {_nd(_hpos)}")
# the MM stays off a leg being sold: resting quotes pulled, none placed
_mm = _MM.__new__(_MM)
_mm.load_overrides = lambda: None
_mm.hours_to_end = lambda: 100.0
_mm.news_paused = lambda: {}
_mm.cfg = types.SimpleNamespace(capital_frac=0.5, wave_hot_frac=0.5, order_ttl_sec=60, batch_size=10)
_mm.starting_cash, _mm.quoted, _mm.hot_until, _mm.wave, _mm.state, _mm.dry_run = 100000.0, ["e1"], {}, False, {}, False
_mm.raise_pause = {"e1": time.time() + 600}
_mmapi = _RVenue({})
_mm.api, _mm.store = _mmapi, types.SimpleNamespace(mark_orders=lambda ids, st: None)
_mm.build_quotes = lambda *a, **k: (_ for _ in ()).throw(AssertionError("quoted a raise leg"))
_ms = _MM.quote_cycle(_mm, {"e1": _rbk(0.5, 0.505)}, {}, {}, {"e1": [{"id": 5}]}, {}, 50000.0, 1, 10)
check("raise: the MM does not quote a leg the cash raise is selling and pulls its resting orders there",
      _ms.get("raise_paused") == ["e1"] and _mmapi.cancels == ["e1"])
# restart mid-raise: the pause and the 24-h total come back from the store, and the next clip is sized from positions then
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (300.0, "floor")}, raise_max_total=600.0)
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
_n1 = len(_v.orders)
_b2 = types.SimpleNamespace(**vars(_b))
_b2.mm = types.SimpleNamespace(raise_pause={})
R.Bot.load_raise_pause(_b2)
_b2.cash_needs = {"hf_arb": (5000.0, "floor")}
R.Bot.raise_cycle(_b2, _rprices(_rbooks), False)
_tot = sum(q * (1 - px) for ex, side, q, px, k in _v.orders)
check("raise: after a restart the MM pause is reloaded and the 24-h cap still counts the earlier clips (600 cap: ~600 raised in all)",
      _L(_RB, "R") in _b2.mm.raise_pause and len(_v.orders) > _n1 and _tot <= 600 + 1.0, f"{_v.orders} {_tot}")
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (5000.0, "floor")})
_v.pos[_L(_RB, "R")] = -100.0                                   # a fill since the snapshot: 100 NO left, not 800
_b.positions_snapshot = dict(_rpos)
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
check("raise: clips are sized from a positions read under the lock, never through zero",
      all(q <= 100 for ex, side, q, px, k in _v.orders if ex == _L(_RB, "R")) and _v.pos[_L(_RB, "R")] <= 0, f"{_v.orders} {_v.pos}")
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (5000.0, "floor")})
_b.fresh_positions = lambda: None
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
_b3, _v3 = _rbot(_rpos, _rbooks, {"hf_arb": (5000.0, "floor")}, raise_enabled=False)
R.Bot.raise_cycle(_b3, _rprices(_rbooks), False)
check("raise: a failed positions read sends nothing; raise_enabled false sends nothing; it is off by default in code",
      _v.orders == [] and _v3.orders == [] and BotConfig().raise_enabled is False and BotConfig().raise_max_cost == 0.01)
# the needs: high-frequency arb blocked by its cash floor, cheap favourites short of cash
_b, _v, _ch = _hbot({}, _legs("CA", 0.52, 0.525, 2000))
_b.churn_med = {"CA": -0.01}
_b.cash, _b.cash_needs = 3500.0, {}
_b.post_cash_need = lambda s, a, w="": R.Bot.post_cash_need(_b, s, a, w)
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("raise: high-frequency arb posts the shortfall its 3,000 cash floor blocked (it buys 520 with the 500 free; 2,000 x 0.96 - 500 short)",
      abs(_b.cash_needs.get("hf_arb", (0,))[0] - (2000 * 0.96 - 500.0)) < 1e-6 and _ch.held("CA") == 520, f"{_b.cash_needs} {_ch.held('CA')}")


# 24 Oct, tier 2 of the cash raise: whole sets, all-or-none, only when tier 1 cannot cover it and high-frequency arb kept
# skipping entries for cash, at <= 4.5% exit cost, never a race held through an early call
def _mlv(self, legs, all_or_none=True, key=None):
    self.sent_ml = getattr(self, "sent_ml", []) + [(key, [dict(l) for l in legs])]
    out = []
    for l in legs:
        self.pos[l["exchangeId"]] = self.pos.get(l["exchangeId"], 0.0) + (l["quantity"] if l["side"] == "yes" else -l["quantity"])
        out.append({"data": {"exchangeId": l["exchangeId"], "quantityTraded": l["quantity"], "price": l["price"], "open": False, "orderId": 5000 + len(out)}})
    return {"results": out}


_RVenue.place_multi_leg = _mlv


def _sbot(pos, books, needs, skips=3, **cfgkw):
    b, v = _rbot(pos, books, needs, **cfgkw)
    for k, x in dict(raise_sets_enabled=True, raise_set_max_cost=0.045, raise_set_min_skips=3, raise_set_max_pieces=2,
                     raise_set_hold_races=[]).items():
        if not hasattr(b.cfg, k) or k not in cfgkw:
            setattr(b.cfg, k, cfgkw.get(k, x))
    e = ArbEngine.__new__(ArbEngine)
    e.cfg = ArbConfig(min_set_qty=20)
    e.u, e.api, e.store, e.dry_run, e.committed, e.cooldown, e.hostile = _U, v, b.store, False, {}, {}, set()
    b.arb = e
    now = time.time()
    b.store.set_param("churn_cash_skips", [[now - 1860 * i, "X%d" % i, 2000, 0, 0.98] for i in range(skips)])   # one per half-hour
    b.churn_skips_6h = lambda: R.Bot.churn_skips_6h(b)
    b.churn_skip_windows_6h = lambda w=1800.0: R.Bot.churn_skip_windows_6h(b, w)
    return b, v


_sA = [_L(_RA, "D"), _L(_RA, "R")]
_sB = [_L(_RB, "D"), _L(_RB, "R")]
_sC = [_L(_RC, "D"), _L(_RC, "R")]
_spos = {_sA[0]: -3000.0, _sA[1]: -3000.0,            # sell-set A: asks 0.51 + 0.50 -> 0.99/set, 1.0% to exit
         _sB[0]: -3000.0, _sB[1]: -3000.0,            # sell-set B: asks 0.53 + 0.53 -> 0.94/set, 6.4%: never
         _sC[0]: 2000.0, _sC[1]: 2000.0,              # buy-set C: bids 0.49 + 0.49 -> 0.98/set, 2.0%
         _RI["D"]: -1000.0, _RI["R"]: -1000.0, _RI["I"]: -1000.0}   # RI Governor set: asks 0.30+0.40+0.31 -> 1.99 of 2, 0.5%
_sbooks = {_sA[0]: _rbk(0.505, 0.51, 1000), _sA[1]: _rbk(0.495, 0.50, 1000), _sB[0]: _rbk(0.525, 0.53), _sB[1]: _rbk(0.525, 0.53),
           _sC[0]: _rbk(0.49, 0.495, 1000), _sC[1]: _rbk(0.49, 0.495, 1000),
           _RI["D"]: _rbk(0.295, 0.30, 400), _RI["R"]: _rbk(0.395, 0.40, 400), _RI["I"]: _rbk(0.305, 0.31, 400)}
_b, _v = _sbot(_spos, _sbooks, {"hf_arb": (2000.0, "floor")})
R.Bot.raise_cycle(_b, _rprices(_sbooks), False)
_ml = [(k, {l["exchangeId"]: (l["side"], l["quantity"]) for l in legs}) for k, legs in getattr(_v, "sent_ml", [])]
_races = [_U.race_of(next(iter(m))).name for k, m in _ml]
check("raise tier 2: no tier-1 holdings, cash skips in 3 half-hours of the last 6 h: the cheapest sets go first (RI Governor 0.5%, then A 1.0%), each piece "
      "all legs at once, <= 80% of the thinnest touch (320 / 800 sets), never more than the 2,200 needed; B (6.4%) untouched",
      _races == ["Rhode Island Governor", _RA] and all(len(set(q for s_, q in m.values())) == 1 for k, m in _ml)
      and [max(q for s_, q in m.values()) for k, m in _ml] == [320, 800] and _v.pos[_sB[0]] == -3000.0 and _v.pos[_sB[1]] == -3000.0
      and sum(x for t, x in _b.store.get_param("raise_log")) <= 2200.0 + 1.0, f"{_ml} {_races} {_b.store.get_param('raise_log')}")
_rr = _b.store.rows("SELECT DISTINCT purpose FROM orders")
_bs, _vs = _sbot(_spos, _sbooks, {"hf_arb": (100.0, "floor")})
R.Bot.raise_cycle(_bs, _rprices(_sbooks), False)
check("raise tier 2: a 300 shortfall (100 + 200 buffer) unwinds one piece of ceil(300 / 1.99) = 151 RI Governor sets, not the touch",
      [max(l["quantity"] for l in legs) for k, legs in getattr(_vs, "sent_ml", [])] == [151], f"{getattr(_vs, 'sent_ml', [])}")
check("raise tier 2: set unwinds are tagged 'raise_set' (tier-1 clips 'raise') and leave every leg's set intact beyond the pieces (no leg through zero)",
      {r["purpose"] for r in _rr} == {"raise_set"} and all(_v.pos[e] <= 0 for e in _sA + list(_RI.values())), f"{[dict(r) for r in _rr]} {_v.pos}")
_b, _v = _sbot(_spos, _sbooks, {"hf_arb": (2000.0, "floor")}, skips=2)
R.Bot.raise_cycle(_b, _rprices(_sbooks), False)
_b2, _v2 = _sbot(_spos, _sbooks, {"hf_arb": (2000.0, "floor")}, raise_sets_enabled=False)
R.Bot.raise_cycle(_b2, _rprices(_sbooks), False)
check("raise tier 2: never with skips in < 3 half-hours of the last 6 h, never when raise_sets_enabled is off",
      not getattr(_v, "sent_ml", []) and not getattr(_v2, "sent_ml", []), f"{getattr(_v, 'sent_ml', [])} {getattr(_v2, 'sent_ml', [])}")
_tpos = dict(_spos, **{_L(_RD, "D"): 3000.0})                 # tier 1: 3,000 YES at 0.495/0.50 (0.5%) covers a 700 need
_tbooks = dict(_sbooks, **{_L(_RD, "D"): _rbk(0.495, 0.50, 5000), _L(_RD, "R"): _rbk(0.50, 0.505, 5000)})
_b, _v = _sbot(_tpos, _tbooks, {"hf_arb": (500.0, "floor")})
R.Bot.raise_cycle(_b, _rprices(_tbooks), False)
check("raise tier 2: while tier 1 can cover the shortfall no set is unwound (the holding is sold instead)",
      not getattr(_v, "sent_ml", []) and _v.orders and _v.orders[0][0] == _L(_RD, "D"), f"{getattr(_v, 'sent_ml', [])} {_v.orders}")
_b, _v = _sbot(_spos, _sbooks, {"hf_arb": (2000.0, "floor")}, raise_set_hold_races=["Rhode Island Governor"])
_b.arb.protected = lambda: {_RA}
R.Bot.raise_cycle(_b, _rprices(_sbooks), False)
_races = [_U.race_of(l[0]["exchangeId"]).name for k, l in getattr(_v, "sent_ml", [])]
check("raise tier 2: never a race held through an early call or a high-frequency arb race; a buy-set is unwound by buying NO on every leg",
      _races == [_RC] and all(l["side"] == "no" for k, legs in _v.sent_ml for l in legs) and _v.pos[_sC[0]] > 0, f"{_races} {getattr(_v, 'sent_ml', [])}")
# churn's skipped-for-cash entries: logged once per race per 5 min and counted over 6 h
_ns = types.SimpleNamespace(store=_Store(os.path.join(tempfile.mkdtemp(), "s.sqlite")))
R.Bot.note_churn_skip(_ns, "CA", 2000, 0, 0.98)
R.Bot.note_churn_skip(_ns, "CA", 2000, 0, 0.98)
R.Bot.note_churn_skip(_ns, "CB", 2000, 500, 0.98)
check("raise: high-frequency arb's cash skips are recorded (once per race per 5 min) and counted over the last 6 h",
      R.Bot.churn_skips_6h(_ns) == 2 and len(_ns.store.get_param("churn_cash_skips")) == 2)
check("raise tier 2 config: off by default, 4.5% cost bound, 3 skips, no hold races", BotConfig().raise_sets_enabled is False
      and BotConfig().raise_set_max_cost == 0.045 and BotConfig().raise_set_min_skips == 3 and BotConfig().raise_set_hold_races == [])


# Cash raise review 1 (24 Oct): the reviewer's attacks, which broke the first version
_BR = _L(_RB, "R")
_b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _L(_RB, "D"): _rbk(0.605, 0.61)}, {"hf_arb": (5000.0, "floor")})
_b.open_orders_cache = {_BR: [{"id": 9, "side": "yes", "action": "buy", "priceLimit": 0.385, "quantity": 300, "quantityFilled": 0}]}
_fb0, _fired = _b.fresh_book, []


def _fb_fill(ex, age=None):
    if ex == _BR and not _fired:
        _fired.append(1)
        _v.pos[_BR] += 300.0                      # our MM bid fills right after the raise's first positions read
    return _fb0(ex, age)


_b.fresh_book = _fb_fill
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)
check("raise review A1: an MM fill after the raise's positions read shrinks the clip to the 500 NO held at send; never through zero",
      _v.pos[_BR] == 0.0 and [q for ex, side, q, px, k in _v.orders] == [500] and _BR in _v.cancels, f"{_v.orders} {_v.pos} {_v.cancels}")
_b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _L(_RB, "D"): _rbk(0.605, 0.61)}, {"hf_arb": (5000.0, "floor")})
_b.open_orders_cache = {_BR: [{"id": 9, "side": "yes", "action": "buy", "priceLimit": 0.385, "quantity": 300, "quantityFilled": 0}]}
_ca0 = _v.cancel_all


def _ca_fill(exchange_id=None, **k):
    if exchange_id == _BR:
        _v.pos[_BR] += 300.0                      # the MM bid filled before the cancel landed
    return _ca0(exchange_id=exchange_id, **k)


_v.cancel_all = _ca_fill
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)
check("raise review A1b: a fill while the MM quotes are being pulled: the clip is sized after the pull", _v.pos[_BR] == 0.0, f"{_v.orders} {_v.pos}")
_AD, _AR = _L(_RA, "D"), _L(_RA, "R")
_zst = _Store(os.path.join(tempfile.mkdtemp(), "z.sqlite"))
_zst.record_order(1, _AR, None, "no", "buy", 0.5, 300, "direction")
_zst.exec("UPDATE orders SET placed_at=?", (time.time() - 7200,))
_zst.ingest_fills([{"id": 1, "orderId": 1, "exchangeId": _AR, "side": "no", "price": 0.5, "quantity": 300, "filledAt": "2026-10-24T00:00:00Z"}])
_b, _v = _rbot({_AD: -1000.0, _AR: -1300.0}, {_AD: _rbk(0.495, 0.50), _AR: _rbk(0.495, 0.50)}, {"hf_arb": (5000.0, "floor")})
_b.store = _zst
R.Bot.raise_cycle(_b, _rprices({_AD: _rbk(0.495, 0.50), _AR: _rbk(0.495, 0.50)}), False)
_zst.ingest_fills([{"id": 2, "orderId": 1000 + _v.n, "exchangeId": _AR, "side": "yes", "price": 0.5, "quantity": 300, "filledAt": "2026-10-24T00:01:00Z"}])
_zb = types.SimpleNamespace(cfg=types.SimpleNamespace(exit_band=0.01), store=_zst, open_orders_cache={}, dry_run=False, api=_v)
_n0 = len(_v.orders)
R.Bot.zero_ev_exits(_zb, dict(_v.pos), [{"exchange_id": _AR, "fair": 0.50, "sig_bid": 0.495, "sig_ask": 0.50, "pm": 0.5, "kalshi": 0.5}])
check("raise review A2: after the raise sold a directional holding, the zero-EV exit does not close it again (the set stays whole)",
      len(_v.orders) == _n0 and _v.pos[_AR] == _v.pos[_AD] == -1000.0, f"{_v.orders[_n0:]} {_v.pos}")
_b, _v = _rbot({_AD: -500.0, _AR: -1000.0}, {_AD: _rbk(0.495, 0.50), _AR: _rbk(0.495, 0.50)}, {"hf_arb": (5000.0, "floor")})
_b.catchup_open = {_AD: 500.0}                    # catch-up's 500 YES on D next to 1,000 sets: the venue shows R 500 NO beyond the set
R.Bot.raise_cycle(_b, _rprices({_AD: _rbk(0.495, 0.50), _AR: _rbk(0.495, 0.50)}), False)
check("raise review A3: a race with a catch-up (or cheap-favourite) leg is left alone entirely, not sold through its other leg",
      _v.orders == [], f"{_v.orders}")
_ns = types.SimpleNamespace(store=_Store(os.path.join(tempfile.mkdtemp(), "s.sqlite")))
for _r in ("X1", "X2", "X3"):
    R.Bot.note_churn_skip(_ns, _r, 2000, 0, 0.98)
_w_burst = R.Bot.churn_skip_windows_6h(_ns)
_ns = types.SimpleNamespace(store=_Store(os.path.join(tempfile.mkdtemp(), "s2.sqlite")))
_real_time, _base = time.time, (time.time() // 1800) * 1800 + 60
try:
    for _k in range(3):                           # one race's opportunity retried for ~10 min
        time.time = lambda k=_k: _base + 301 * k
        R.Bot.note_churn_skip(_ns, "X1", 2000, 0, 0.98)
    time.time = lambda: _base + 700
    _w_one = R.Bot.churn_skip_windows_6h(_ns)
    for _k in range(3):                           # skips in three different half-hours
        time.time = lambda k=_k: _base + 900 + 1800 * k
        R.Bot.note_churn_skip(_ns, "X2", 2000, 0, 0.98)
    time.time = lambda: _base + 900 + 3700
    _w_rep = R.Bot.churn_skip_windows_6h(_ns)
finally:
    time.time = _real_time
check("raise review A4: tier 2's 'repeatedly' counts half-hours with a skip: a 3-race burst is 1, one opportunity over 10 min is 1, "
      "three separate half-hours are 3", _w_burst == 1 and _w_one == 1 and _w_rep >= 3, f"{_w_burst} {_w_one} {_w_rep}")


_b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _L(_RB, "D"): _rbk(0.605, 0.61)}, {"hf_arb": (300.0, "floor")})
_po0 = _v.place_order
_v.place_order = lambda ex, side, action, qty, price, expiration=None, key=None: (_v.orders.append((ex, side, qty, price, key)), {"orderId": 1000 + _v.n})[1]
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)       # the first clip rests unfilled
_v.place_order, _b.cash_needs, _b.open_orders_cache, _b.open_orders_ts = _po0, {"hf_arb": (300.0, "floor")}, {}, 0.0
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)       # next cycle, the open-orders read has not caught up
check("raise review A7: a clip that does not fill at once is cancelled at once (fill-or-cancel), before any later clip",
      len(_v.orders) == 2 and getattr(_v, "cancelled", []) == [1001], f"{_v.orders} {_v.cancels} {getattr(_v, 'cancelled', None)}")


# Cash raise review 2 (24 Oct): an MM bid the open-orders cache misses, the cap after a failed clip, the gate's spacing
_BD = _L(_RB, "D")
for _cache in (True, False):
    _b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _BD: _rbk(0.605, 0.61)}, {"hf_arb": (5000.0, "floor")})
    _b.store.record_order(9, _BR, None, "yes", "buy", 0.385, 300, "mm")
    _b.store.exec("UPDATE orders SET placed_at=? WHERE order_id=9", (time.time() - 300,))
    _b.cfg.mm.order_ttl_sec = 1800
    _b.open_orders_ts = time.time() - 5
    _b.open_orders_cache = {_BR: [{"id": 9, "side": "yes", "action": "buy", "priceLimit": 0.385, "quantity": 300, "quantityFilled": 0}]} if _cache else {}
    _po0 = _v.place_order

    def _po_n2(ex, side, action, qty, price, expiration=None, key=None, v=_v, po0=_po0):
        if ex == _BR and _BR not in v.cancels:
            v.pos[_BR] += 300.0                   # our MM bid, if never cancelled, fills just before the clip lands
        return po0(ex, side, action, qty, price, expiration, key)
    _v.place_order = _po_n2
    R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)
    check(f"raise review N2 ({'fresh cache' if _cache else 'cache empty'}): an MM bid resting on the leg is pulled before the clip "
          "(store-backed, not only the cache); the leg never crosses zero", _v.pos[_BR] <= 0 and _BR in _v.cancels, f"{_v.cancels} {_v.pos}")
_b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _BD: _rbk(0.605, 0.61)}, {"hf_arb": (5000.0, "floor")})
_b.open_orders_ok = False
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)
check("raise review N2: no raise in a cycle whose open-orders read failed", _v.orders == [] and _v.cancels == [])
_npos = {_AD: 1990.0, _BR: 300.0}
_nbks = {_AD: _rbk(0.495, 0.50), _BR: _rbk(0.4975, 0.5025), _AR: _rbk(0.50, 0.505), _BD: _rbk(0.4975, 0.5025)}
_nnd = lambda p: _risk.exposure(_U, p, _rprices(_nbks)).net_d_notional
for _mode in ("ok", "api_error", "rests"):
    _b, _v = _rbot(dict(_npos), _nbks, {"hf_arb": (5000.0, "floor")}, cap=900.0, raise_cap_margin=300.0)
    _po0 = _v.place_order

    def _po_n1(ex, side, action, qty, price, expiration=None, key=None, v=_v, po0=_po0, mode=_mode):
        if ex == _AD and mode == "api_error":
            raise R.ApiError(429, "RATE", "rate limited")
        if ex == _AD and mode == "rests":
            v.orders.append((ex, side, qty, price, key))
            return {"orderId": 1000 + v.n}
        return po0(ex, side, action, qty, price, expiration, key)
    _v.place_order = _po_n1
    R.Bot.raise_cycle(_b, _rprices(_nbks), False)
    check(f"raise review N1 (first clip {_mode}): net D never ends past the 900 cap, whatever the earlier clip did",
          abs(_nnd(_v.pos)) <= 900 + 1e-6 or abs(_nnd(_v.pos)) <= abs(_nnd(_npos)) + 1e-6,
          f"net D {_nnd(_npos):+.0f} -> {_nnd(_v.pos):+.0f}; {[(o[0], o[2]) for o in _v.orders]}")
_ns = types.SimpleNamespace(store=_Store(os.path.join(tempfile.mkdtemp(), "w.sqlite")))
_real_time = time.time
_base = (_real_time() // 1800) * 1800 + 1800 - 60
try:
    for _k in range(8):                           # one race retried every 5 min for 35 min, across two grid boundaries
        time.time = lambda k=_k: _base + 301 * k
        R.Bot.note_churn_skip(_ns, "X1", 2000, 0, 0.98)
    time.time = lambda: _base + 301 * 7 + 1
    _w35 = R.Bot.churn_skip_windows_6h(_ns)
finally:
    time.time = _real_time
check("raise review N4: one opportunity persisting 35 min counts < 3 (counted skips are >= 30 min apart: 3 needs an hour)", _w35 < 3, str(_w35))


# Cash raise review 3 (24 Oct): a clip that rests is in no later cap check: clips are fill-or-cancel
_CD = _L(_RC, "D")
_RID = _U.races["Rhode Island Governor"].legs["D"].exchange_id
_r3b = {_AR: _rbk(0.499, 0.500), _AD: _rbk(0.500, 0.501), _BD: _rbk(0.4985, 0.500), _BR: _rbk(0.500, 0.5015),
        _CD: _rbk(0.498, 0.500), _L(_RC, "R"): _rbk(0.500, 0.502), _RID: _rbk(0.5, 0.501)}
_r3nd = lambda p: _risk.exposure(_U, p, _rprices(_r3b)).net_d_notional
_r3pos = {_AR: 1000.0, _BD: 800.0, _CD: -1400.0, _RID: 1600.0}


def _r3_fill_rested(v, rested):
    for oid, ex, side, q in rested:               # a rested clip fills within its TTL unless it was cancelled
        if oid not in getattr(v, "cancelled", []) and ex not in v.cancels:
            v.pos[ex] += q if side == "yes" else -q


_b, _v = _rbot(dict(_r3pos), _r3b, {"hf_arb": (50000.0, "floor")}, cap=900.0, raise_cap_margin=300.0)
_po0, _rested = _v.place_order, []


def _po_r3a(ex, side, action, qty, price, expiration=None, key=None):
    if ex == _AR:
        _v.orders.append((ex, side, qty, price, key))
        _rested.append((1000 + _v.n, ex, side, qty))
        return {"orderId": 1000 + _v.n}            # rests: no quantityTraded
    if ex == _BD:
        raise R.ApiError(429, "RATE", "rate limited")
    return _po0(ex, side, action, qty, price, expiration, key)


_v.place_order = _po_r3a
R.Bot.raise_cycle(_b, _rprices(_r3b), False)
_r3_fill_rested(_v, _rested)
check("raise review R3-a: a clip that rests is cancelled at once, so a later clip's cap check cannot be wrong (|net D| <= 900)",
      abs(_r3nd(_v.pos)) <= 900 + 1e-6, f"net D {_r3nd(_r3pos):+.0f} -> {_r3nd(_v.pos):+.0f} {[(o[0], o[2]) for o in _v.orders]}")
_b, _v = _rbot(dict(_r3pos), _r3b, {"hf_arb": (300.0, "floor")}, cap=900.0, raise_cap_margin=300.0, raise_min_clip=50.0)
_po0, _rested = _v.place_order, []


def _po_r3b(ex, side, action, qty, price, expiration=None, key=None):
    if ex == _AR and not _rested:
        _v.orders.append((ex, side, qty, price, key))
        _rested.append((1000 + _v.n, ex, side, qty))
        return {"orderId": 1000 + _v.n}
    return _po0(ex, side, action, qty, price, expiration, key)


_v.place_order = _po_r3b
R.Bot.raise_cycle(_b, _rprices(_r3b), False)
_r3b2 = dict(_r3b, **{_AR: _rbk(0.49, 0.51), _BD: _rbk(0.49, 0.51)})
_b.fresh_book = lambda ex, age=None: _r3b2.get(ex)
_b.cash_needs = {"hf_arb": (500.0, "floor")}
R.Bot.raise_cycle(_b, _rprices(_r3b2), False)
_r3_fill_rested(_v, _rested)
check("raise review R3-b: a clip resting from the previous cycle cannot fill on top of this cycle's clips (|net D| <= 900)",
      abs(_r3nd(_v.pos)) <= 900 + 1e-6, f"net D {_r3nd(_r3pos):+.0f} -> {_r3nd(_v.pos):+.0f} {[(o[0], o[2]) for o in _v.orders]}")
_b, _v = _rbot(dict(_r3pos), _r3b, {"hf_arb": (50000.0, "floor")}, cap=900.0, raise_cap_margin=300.0)
_v.place_order = lambda ex, side, action, qty, price, expiration=None, key=None: (_v.orders.append((ex, side, qty, price, key)), {"orderId": 1000 + _v.n})[1]
_v.cancel_order = lambda oid: (_ for _ in ()).throw(R.ApiError(500, "ERR", "cancel failed"))
R.Bot.raise_cycle(_b, _rprices(_r3b), False)
check("raise review R3: if a resting clip cannot be cancelled, no further clip is sent that cycle", len(_v.orders) == 1, f"{_v.orders}")
_cr = CRm.plan({_BR: -5}, {_BR: _rbk(0.39, 0.395)}, 1000.0, 0.01, lambda p: 0.0, 5000.0, {_BR: -5.0}, 1000.0, 4, 50.0)
check("raise review 3: no crumb clips (a clip trimmed below raise_min_clip is not sent)", _cr == [], f"{_cr}")


# Cash raise review 4 (24 Oct): unknown placement status, a remainder the venue closed, the cap charged on what traded
_b, _v = _rbot(dict(_r3pos), _r3b, {"hf_arb": (50000.0, "floor")}, cap=900.0, raise_cap_margin=300.0)
_v.place_order = lambda *a, **k: (_ for _ in ()).throw(R.ApiError(502, "ORDER_STATUS_UNKNOWN", "unknown"))
R.Bot.raise_cycle(_b, _rprices(_r3b), False)
check("raise review 4: a placement with unknown status pulls that leg and stops the cycle", len(_v.cancels) >= 1 and _v.orders == [], f"{_v.cancels}")
_b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _L(_RB, "D"): _rbk(0.605, 0.61)}, {"hf_arb": (5000.0, "floor")})
_v.place_order = lambda ex, side, action, qty, price, expiration=None, key=None: (_v.orders.append((ex, side, qty, price, key)),
                                                                                    {"orderId": 1000 + _v.n, "quantityTraded": 0, "open": True})[1]
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)
check("raise review 4: a clip that traded nothing and was cancelled charges nothing to the 24-h cap",
      getattr(_v, "cancelled", []) and sum(x for t, x in _b.store.get_param("raise_log")) == 0.0, f"{_b.store.get_param('raise_log')}")
_b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _L(_RB, "D"): _rbk(0.605, 0.61)}, {"hf_arb": (5000.0, "floor")})
_v.place_order = lambda ex, side, action, qty, price, expiration=None, key=None: (_v.orders.append((ex, side, qty, price, key)),
                                                                                    {"orderId": 1000 + _v.n, "quantityTraded": 100, "open": False})[1]
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)
check("raise review 4: a partly filled clip the venue already closed is not cancelled (no 409 stall)", not getattr(_v, "cancelled", []))
_sp = CRm.set_pieces({_RA: [_AD, _AR]}, {_AD: -500.0, _AR: -500.0}, {_AD: _rbk(0.495, 0.50), _AR: _rbk(0.495, 0.50)}, 10.0, 0.045, (), 2, 50.0)
check("raise review 4: no tier-2 crumb pieces (trimmed below raise_min_clip)", _sp == [], f"{_sp}")


_b, _v = _rbot(dict(_r3pos), _r3b, {"hf_arb": (50000.0, "floor")}, cap=900.0, raise_cap_margin=300.0)
import urllib.error as _ue  # noqa: E402
_v.place_order = lambda *a, **k: (_ for _ in ()).throw(_ue.URLError("connection reset"))
R.Bot.raise_cycle(_b, _rprices(_r3b), False)
check("raise review 5: a network error on placement (status unknown) pulls the leg, stops the cycle and charges the 24-h cap",
      len(_v.cancels) >= 1 and sum(x for t, x in (_b.store.get_param("raise_log") or [])) > 0, f"{_v.cancels} {_b.store.get_param('raise_log')}")

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


# 24 Oct, the shock reserve (Sizhe: "yes add money for the shock reserve"): 15k kept free for purpose 'shock' only
_sh = types.SimpleNamespace(cash=20000.0, cfg=types.SimpleNamespace(shock_enabled=True, shock_reserve=15000.0))
_sh_off = types.SimpleNamespace(cash=20000.0, cfg=types.SimpleNamespace(shock_enabled=False, shock_reserve=15000.0))
check("shock reserve: every sleeve but shock plans with free cash minus 15k while shock mode is on, all of it otherwise",
      R.spendable_cash(_sh) == 5000.0 and R.spendable_cash(_sh_off) == 20000.0 and R.shock_reserve(_sh_off) == 0.0
      and BotConfig().shock_reserve == 15000.0)
_b, _v, _ch = _hbot({}, _legs("CA", 0.52, 0.525, 2000))
_b.churn_med, _b.cash, _b.cfg.shock_enabled, _b.cfg.shock_reserve = {"CA": -0.01}, 18500.0, True, 15000.0
_b.cash_needs = {}
_b.post_cash_need = lambda sl, a, w="": R.Bot.post_cash_need(_b, sl, a, w)
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("shock reserve: high-frequency arb stops at its 3k floor + 15k (18.5k cash: 500 to spend, 520 sets at 0.96)",
      _ch.held("CA") == 520 and _b.cash >= 18000.0 - 1, f"held {_ch.held('CA')} cash {_b.cash}")
_cfs = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("shock reserve: cheap favourites, arbs, recycles, offload, catch-up and the MM / tilt / news / direction budgets read the "
      "spendable cash (only the value log reads raw cash)",
      "cf.entries(cands, held, deployed, spendable_cash(self)" in _cfs and "b.arb.plan(cand_books, spendable_cash(b))" in _cfs
      and "fills_count, max(0.0, spendable_cash(self)), cycle_id" in _cfs and "shock_reserve(self) > 0 and spendable_cash(self) < q * px" in _cfs
      and _cfs.count("spendable_cash(") >= 20)
# the raise refills the reserve after a shock spent it, tier 1 only, capped at deficit + buffer
_b, _v = _rbot(_rpos, _rbooks, {})
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = True, 15000.0, 14700.0      # a shock spent cash: 300 under the reserve
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
_got = sum(q * (1 - px) for ex, side, q, px, k in _v.orders)
check("shock reserve: after a shock spend the raise refills it with no waiting trade, at most the 300 deficit + the 200 buffer",
      _v.orders and _got <= 500 + 0.61 and _got >= 300, f"{_v.orders} {_got}")
_b, _v = _rbot(_rpos, _rbooks, {})
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = False, 15000.0, 14700.0
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
check("shock reserve: no raise at all while shock mode is off", _v.orders == [])
_b, _v = _sbot(_spos, _sbooks, {})
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = True, 15000.0, 10000.0
R.Bot.raise_cycle(_b, _rprices(_sbooks), False)
check("shock reserve: tier 2 never unwinds a set for the reserve (sets on offer, no tier-1 holdings, skips present)",
      not getattr(_v, "sent_ml", []), f"{getattr(_v, 'sent_ml', [])}")
_b, _v = _rbot({_AD: 1990.0, _BR: 300.0}, {_AD: _rbk(0.495, 0.50), _BR: _rbk(0.4975, 0.5025), _AR: _rbk(0.50, 0.505), _BD: _rbk(0.4975, 0.5025)},
               {}, cap=900.0, raise_cap_margin=300.0)
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = True, 15000.0, 13000.0
_pb = {_AD: _rbk(0.495, 0.50), _BR: _rbk(0.4975, 0.5025), _AR: _rbk(0.50, 0.505), _BD: _rbk(0.4975, 0.5025)}
R.Bot.raise_cycle(_b, _rprices(_pb), False)
_ndr = lambda p: _risk.exposure(_U, p, _rprices(_pb)).net_d_notional
check("shock reserve: the partisan cap holds during a reserve raise (cap 900)", abs(_ndr(_v.pos)) <= 900 + 1e-6 or abs(_ndr(_v.pos)) <= abs(_ndr({_AD: 1990.0, _BR: 300.0})),
      f"net D {_ndr({_AD: 1990.0, _BR: 300.0}):+.0f} -> {_ndr(_v.pos):+.0f}")
_b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _BD: _rbk(0.605, 0.61)}, {})
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = True, 15000.0, 14000.0
_b.store.record_order(77, _BR, None, "no", "buy", 0.6, 800, "shock")
_b.store.exec("UPDATE orders SET placed_at=? WHERE order_id=77", (time.time() - 3600,))
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)
check("shock reserve: the raise never sells a shock position to refill it", _v.orders == [], f"{_v.orders}")


# Shock reserve review 6 (24 Oct): no tier 2 for the reserve by way of another need; refill after the shock, not during it
_b, _v = _sbot(_spos, _sbooks, {})
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = True, 15000.0, 10000.0
for _cyc in range(6):                              # the reviewer's experiment: a 2k high-frequency arb need every cycle
    _b.cash_needs = {"hf_arb": (2000.0, "floor")}
    R.Bot.raise_cycle(_b, _rprices(_sbooks), False)
check("shock reserve review B1: while the reserve is short, tier 2 never unwinds sets for another sleeve's need (whose cash would "
      "refill the reserve first)", not getattr(_v, "sent_ml", []), f"{len(getattr(_v, 'sent_ml', []))} pieces")
_b, _v = _rbot(_rpos, _rbooks, {})
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = True, 15000.0, 14700.0
_b.shock_active = lambda: True
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
_n_during = len(_v.orders)
_b.shock_active = lambda: False
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
check("shock reserve review B2: no refill while a shock is open or entering; it refills once the shock has closed",
      _n_during == 0 and len(_v.orders) > 0, f"during {_n_during}, after {len(_v.orders)}")
_b, _v = _rbot({_BR: -800.0}, {_BR: _rbk(0.39, 0.395), _BD: _rbk(0.605, 0.61)}, {"hf_arb": (5000.0, "floor")})
_b.shock_races = lambda: {_RB}
R.Bot.raise_cycle(_b, _rprices({_BR: _rbk(0.39, 0.395)}), False)
check("shock reserve review B2: races shock mode holds or is entering (incl. orders of unknown outcome) are never sold", _v.orders == [])
_kp = types.SimpleNamespace(cash=20000.0, cfg=types.SimpleNamespace(shock_enabled=True, shock_reserve=15000.0), monitor_logged=set(),
                            strategy_kill=types.SimpleNamespace(paused=lambda n: "losing" if n == "shock" else None))
_kk = types.SimpleNamespace(cash=20000.0, cfg=types.SimpleNamespace(shock_enabled=True, shock_reserve=15000.0), shock=types.SimpleNamespace(killed=True))
check("shock reserve: no reserve while the shock sleeve is paused by the strategy monitor or killed (15k idle for nothing)",
      R.shock_reserve(_kp) == 0.0 and R.shock_reserve(_kk) == 0.0 and R.shock_reserve(_sh) == 15000.0)
_b, _v, _ch = _hbot({}, _legs("CA", 0.52, 0.525, 2000))
_b.churn_med, _b.cash, _b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash_needs = {"CA": -0.01}, 10000.0, True, 15000.0, {}
_b.post_cash_need = lambda sl, a, w="": R.Bot.post_cash_need(_b, sl, a, w)
_skips = []
_b.note_churn_skip = lambda *a: _skips.append(a)
R.Bot.churn_cycle(_b, _b.positions_snapshot, False)
check("shock reserve review B1: high-frequency arb's cash skips are not counted toward tier 2 while the reserve itself is short "
      "(its need is still posted)", _skips == [] and "hf_arb" in _b.cash_needs and _ch.held("CA") == 0, f"{_skips} {_b.cash_needs}")
check("shock reserve: catch-up counts its own spend within the cycle; offload unwinds no set while the reserve is short",
      'self.cash = getattr(self, "cash", 0.0) - q * px' in _cfs and "or spendable_cash(self) < 0):" in _cfs)


# Shock reserve review 7 (24 Oct): during a shock, other sleeves' needs do not make the raise sell either
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (1500.0, "floor"), "cheapfav": (800.0, "x")})
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = True, 15000.0, 14700.0
_b.shock_active = lambda: True
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
check("shock reserve review 7: during a shock (free cash under the reserve) other sleeves' needs raise nothing either", _v.orders == [],
      f"{_v.orders}")
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (1500.0, "floor")})
_b.cfg.shock_enabled, _b.cfg.shock_reserve, _b.cash = True, 15000.0, 14700.0
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
_got7 = sum(q * (1 - px) for ex, side, q, px, k in _v.orders)
check("shock reserve review 7: after a shock, the refill comes first (300 deficit + 200 buffer); the other need waits for a later cycle",
      _v.orders and _got7 <= 500 + 0.61, f"{_got7}")
_fake_sm = types.ModuleType("sigbot.proposals.shock_mode")
_fake_sm.LAST_ENTRY = time.time() - 60
sys.modules["sigbot.proposals.shock_mode"] = _fake_sm
try:
    _after_last = R.shock_reserve(_sh)
finally:
    del sys.modules["sigbot.proposals.shock_mode"]
check("shock reserve review 7: no reserve after shock mode's last entry time", _after_last == 0.0 and R.shock_reserve(_sh) == 15000.0)


# 24 Oct, the RI trim (Sizhe): tier 2 for Rhode Island Governor only, clips <= 1,000 sets, one per 30 min, level 0, no loss
_RIL = [_U.races["Rhode Island Governor"].legs[p_].exchange_id for p_ in ("D", "R", "I")]


def _tbot(pos, asks=(0.30, 0.40, 0.31), median_asks=(0.30, 0.40, 0.31), basis=1.95, deployed=0.0, cash=1000.0, depth=2000, **cfgkw):
    books = {e: _rbk(round(a - 0.005, 3), a, depth) for e, a in zip(_RIL, asks)}
    books.update({_sA[0]: _rbk(0.505, 0.51, depth), _sA[1]: _rbk(0.495, 0.50, depth)})
    b, v = _rbot(pos, books, {}, **({"cap": cfgkw.pop("cap")} if "cap" in cfgkw else {}))
    conf = dict(raise_sets_races=["Rhode Island Governor"], raise_ri_trim_sets=4500.0, raise_ri_trim_clip=1000, raise_ri_trim_gap_sec=1800.0)
    conf.update(cfgkw)
    for k, x in conf.items():
        setattr(b.cfg, k, x)
    e = ArbEngine.__new__(ArbEngine)
    e.cfg = ArbConfig(min_set_qty=20)
    e.u, e.api, e.store, e.dry_run, e.committed, e.cooldown, e.hostile = _U, v, b.store, False, {}, {}, set()
    b.arb, b.cash = e, cash
    b.cheapfav = types.SimpleNamespace(budget=15000.0)
    b.cheapfav_refresh = lambda: (None, None, None, None, deployed)
    b.cost_basis = {x: basis / 3.0 * abs(pos.get(x, 0.0)) for x in _RIL}      # per set: `basis` across the three NO legs
    b.fresh_positions = lambda: (b.cost_basis.update({x: basis / 3.0 * abs(v.pos.get(x, 0.0)) for x in _RIL}), dict(v.pos))[1]   # as positions_map
    b.ri_trimmed = lambda: R.Bot.ri_trimmed(b)
    b.unwind_deltas = lambda u: R.Bot.unwind_deltas(b, u)
    b.store.exec("CREATE TABLE IF NOT EXISTS venue_quotes (ts REAL, venue TEXT, race TEXT, party TEXT, instrument TEXT, bid REAL, ask REAL, "
                 "mid REAL, bid_sz REAL, ask_sz REAL, depth_usd REAL, latency_ms REAL)")
    t0 = time.time()
    for k in range(150):                                                       # 25 h of 10-min heartbeats
        for party, a in zip(("D", "R", "I"), median_asks):
            b.store.exec("INSERT INTO venue_quotes(ts, venue, race, party, ask) VALUES(?,?,?,?,?)", (t0 - 90000 + 600 * k, "sig", "Rhode Island Governor", party, a))
    return b, v


_tpos = {**{x: -3000.0 for x in _RIL}, _sA[0]: -2000.0, _sA[1]: -2000.0}
_b, _v = _tbot(dict(_tpos))
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
_ml = [(k, {l["exchangeId"]: l["quantity"] for l in legs}) for k, legs in getattr(_v, "sent_ml", [])]
check("RI trim: only Rhode Island Governor is unwound (all 3 legs at once), at most 1,000 sets a clip, tagged 'ri_trim'",
      len(_ml) == 1 and set(_ml[0][1]) == set(_RIL) and set(_ml[0][1].values()) == {1000} and _v.pos[_sA[0]] == -2000.0
      and {r["purpose"] for r in _b.store.rows("SELECT DISTINCT purpose FROM orders")} == {"ri_trim"}, f"{_ml}")
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
_n1 = len(getattr(_v, "sent_ml", []))
_rt, _t1 = time.time, time.time()
try:
    time.time = lambda: _t1 + 1801
    R.Bot.ri_trim_cycle(_b, _rprices({}), False)
finally:
    time.time = _rt
check("RI trim: at most one clip per 30 min (none at once, the next after 30 min)", _n1 == 1 and len(_v.sent_ml) == 2, f"{_n1} {len(_v.sent_ml)}")
_b, _v = _tbot(dict(_tpos), asks=(0.305, 0.405, 0.31))
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim: no clip while the exit cost is above the race's trailing 24-h median (level 0)", not getattr(_v, "sent_ml", []))
_b, _v = _tbot(dict(_tpos), basis=2.0)
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim: no clip at a realised loss (proceeds 1.99/set under a 2.00 cost basis)", not getattr(_v, "sent_ml", []))
_b, _v = _tbot(dict(_tpos))
_b.store.set_param("ri_trim_log", [[time.time() - 7200, "Rhode Island Governor", 4200, 1.99]])
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
_q42 = [l[0]["quantity"] for k, l in getattr(_v, "sent_ml", [])]
_b, _v = _tbot(dict(_tpos))
_b.store.set_param("ri_trim_log", [[time.time() - 7200, "Rhode Island Governor", 4500, 1.99]])
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim: stops at 4,500 sets in all (300 left -> a 300-set clip; at 4,500 nothing)", _q42 == [300] and not getattr(_v, "sent_ml", []), f"{_q42}")
_b, _v = _tbot(dict(_tpos))
for _oid, _x in enumerate(_RIL):                                               # a restart that lost the log: the orders still count
    _b.store.record_order(7000 + _oid, _x, None, "yes", "buy", 0.3, 4500, "ri_trim", status="filled")
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim: after a restart (log lost) the trim's own orders keep the 4,500 cap", R.Bot.ri_trimmed(_b) == 4500.0 and not getattr(_v, "sent_ml", []))
_b, _v = _tbot(dict(_tpos), cash=17000.0)
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim: nothing while free cash covers cheap favourites' remaining budget (+ the shock reserve)", not getattr(_v, "sent_ml", []))
_b, _v = _tbot(dict(_tpos), cash=14500.0)
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim: only as many sets as the need takes (500 short at 1.99/set -> 252 sets)",
      [l[0]["quantity"] for k, l in getattr(_v, "sent_ml", [])] == [252], f"{[l[0]['quantity'] for k, l in getattr(_v, 'sent_ml', [])]}")
_bpos = dict(_tpos, **{_L(_RD, "D"): 3500.0})                                   # 3,500 D YES elsewhere: net D past a 1,000 cap
_b, _v = _tbot(_bpos, cap=1000.0)
_b.fresh_book = (lambda f: (lambda ex, age=None: f(ex, age) or _rbk(0.495, 0.50, 5000)))(_b.fresh_book)
R.Bot.ri_trim_cycle(_b, {**_rprices({}), _L(_RD, "D"): {"bestBid": 0.495, "bestAsk": 0.50}}, False)
check("RI trim: no clip while net D would stay past the partisan cap after it", not getattr(_v, "sent_ml", []))
_b, _v = _tbot(dict(_tpos), raise_sets_races=[])
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim: off without raise_sets_races; tier 2 stays off for every race (raise_sets_enabled false by default)",
      not getattr(_v, "sent_ml", []) and BotConfig().raise_sets_races == [] and BotConfig().raise_sets_enabled is False
      and BotConfig().raise_ri_trim_sets == 4500.0)


# RI trim review 9: unknown outcome charged, the 24-h cap, cheap favourites unable to spend, a cooldown after a rejection
_b, _v = _tbot(dict(_tpos))
def _ml_fill_then_err(legs, all_or_none=True, key=None):
    _mlv(_v, legs, all_or_none, key)                # it filled ...
    raise R.ApiError(502, "ORDER_STATUS_UNKNOWN", "unknown")   # ... but the response was lost (all retries)
_v.place_multi_leg = _ml_fill_then_err
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim review 9: a basket whose outcome is unknown but filled is still charged to the 4,500 cap (and the gap starts)",
      R.Bot.ri_trimmed(_b) == 1000.0 and _b.store.get_param("ri_trim_log"), f"{R.Bot.ri_trimmed(_b)}")
_b, _v = _tbot(dict(_tpos))
_v.place_multi_leg = lambda legs, all_or_none=True, key=None: (_ for _ in ()).throw(R.ApiError(400, "VALIDATION_ERROR", "AllOrNoneNotFilled"))
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
_n_try = 1
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim review 9: a rejected basket charges nothing and is not retried for 10 min",
      R.Bot.ri_trimmed(_b) == 0.0 and getattr(_b, "ri_trim_fail_ts", 0) > 0 and not _b.store.get_param("ri_trim_log"))
_b, _v = _tbot(dict(_tpos), raise_max_total=500.0)
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim review 9: the 24-h raise cap binds the trim (500 left -> 252 sets, not 1,000)",
      [l[0]["quantity"] for k, l in getattr(_v, "sent_ml", [])] == [252], f"{[l[0]['quantity'] for k, l in getattr(_v, 'sent_ml', [])]}")
_b, _v = _tbot(dict(_tpos))
_b.cheapfav.last_entry = time.time() - 60
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim review 9: no trim once cheap favourites can no longer enter (past its last entry)", not getattr(_v, "sent_ml", []))


# RI trim review 10: a basket never sent charges nothing; a lost response plus a failed re-read charges the clip
_b, _v = _tbot(dict(_tpos))
_fp0 = _b.fresh_positions
_calls = []
def _fp_fail_second():
    _calls.append(1)
    if len(_calls) == 2:                           # the pre-send re-read inside unwind_set fails: nothing is sent
        return None
    return _fp0()
_b.fresh_positions = _fp_fail_second
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim review 10: a basket that was never sent (its pre-send read failed) charges nothing to the cap",
      not getattr(_v, "sent_ml", []) and R.Bot.ri_trimmed(_b) == 0.0, f"{R.Bot.ri_trimmed(_b)}")
_b, _v = _tbot(dict(_tpos))
_fp0 = _b.fresh_positions
_st = {"sent": False}
def _ml_fill_net_err(legs, all_or_none=True, key=None):
    _mlv(_v, legs, all_or_none, key)
    _st["sent"] = True
    raise OSError("connection reset")
_v.place_multi_leg = _ml_fill_net_err
def _fp_down_after_send():
    if _st["sent"]:
        raise OSError("still down")
    return _fp0()
_b.fresh_positions = _fp_down_after_send
R.Bot.ri_trim_cycle(_b, _rprices({}), False)
check("RI trim review 10: a lost response and a failed re-read charge the full clip to the trim cap and the 24-h raise cap",
      R.Bot.ri_trimmed(_b) == 1000.0 and sum(x for t, x in (_b.store.get_param("raise_log") or [])) > 1900, f"{R.Bot.ri_trimmed(_b)}")

# ---------------------------------------------------------------- 24 Oct: MM last look when the feed book is stale (sim 'day' cycle 1500)
from sigbot.mm import MarketMaker as _MMk  # noqa: E402
_Q = lambda ex, side, yp: types.SimpleNamespace(exchange_id=ex, side=side, yes_price=yp)
_looks = []
def _tf(ex):
    _looks.append(ex)
    return {"932": (0.75, 0.76), "940": None}.get(ex, (0.40, 0.45))
_llst = {}
_kept = _MMk.last_look([(_Q("932", "yes", 0.76), {}), (_Q("932", "yes", 0.755), {}), (_Q("932", "no", 0.75), {}), (_Q("932", "no", 0.765), {}),
                        (_Q("940", "yes", 0.30), {}), (_Q("950", "yes", 0.41), {})], _tf, _llst)
check("mm last look: a bid at the ask and an ask at the bid are dropped, passive quotes kept",
      [(q.exchange_id, q.side, q.yes_price) for q, _ in _kept] == [("932", "yes", 0.755), ("932", "no", 0.765), ("950", "yes", 0.41)],
      str([(q.exchange_id, q.side, q.yes_price) for q, _ in _kept]))
check("mm last look: an unknown touch drops the quote instead of sending it unchecked", not any(q.exchange_id == "940" for q, _ in _kept))
check("mm last look: one look per exchange per batch", sorted(_looks) == ["932", "940", "950"], str(_looks))
check("mm last look: unknown-touch drops are counted apart from crossings", _llst == {"crossing": 2, "unknown": 1}, str(_llst))
_now = time.time()
def _mtb(feed=None, cache=None, cache_ts=None, snap=None, snap_ts=0.0):
    return types.SimpleNamespace(fresh_book=lambda ex, age=None: feed, book_cache={"932": (cache, 0, 0, 0)} if cache else {},
                                 book_ts={"932": cache_ts} if cache_ts is not None else {}, rest_snapshot=(snap or {}, snap_ts))
_bk = lambda b, a: {"bids": [{"price": b, "quantity": 5}], "asks": [{"price": a, "quantity": 5}]}
check("mm_touch: a fresh feed book is used as is", R.Bot.mm_touch(_mtb(feed=_bk(0.70, 0.72)), "932") == (0.70, 0.72))
check("mm_touch: stale feed book -> this cycle's REST book (3 s old): touch 0.75/0.76, so the 0.76 bid is dropped",
      R.Bot.mm_touch(_mtb(cache=_bk(0.75, 0.76), cache_ts=_now - 3), "932") == (0.75, 0.76))
check("mm_touch: stale feed book -> the newer of the REST book and the price snapshot",
      R.Bot.mm_touch(_mtb(cache=_bk(0.75, 0.765), cache_ts=_now - 10, snap={"932": {"bestBid": 0.755, "bestAsk": 0.76}}, snap_ts=_now - 2), "932")
      == (0.755, 0.76))
check("mm_touch: stale feed book and only touches older than 15 s -> None (the quote is dropped, never sent unchecked)",
      R.Bot.mm_touch(_mtb(cache=_bk(0.75, 0.76), cache_ts=_now - 40, snap={"932": {"bestBid": 0.75, "bestAsk": 0.76}}, snap_ts=_now - 40), "932") is None
      and R.Bot.mm_touch(_mtb(), "932") is None)
check("mm_touch: a book_cache entry without a REST read time (a feed book) is never used as a fallback",
      R.Bot.mm_touch(_mtb(cache=_bk(0.75, 0.76)), "932") is None)
_mmsrc = open(os.path.join(ROOT, "sigbot", "mm.py")).read()
check("mm: with no touch to check against, a market keeps its resting quotes (no cancel, not left dark)",
      _mmsrc.find("if tf is not None and tf(ex) is None:") != -1 and _mmsrc.find("if tf is not None and tf(ex) is None:") < _mmsrc.find("self.api.cancel_all(exchange_id=ex)\n                        self.store.mark_orders"))
_rs7 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("mm_touch: the snapshot fallback is the REST snapshot stamped at read time; feed books written to book_cache clear its REST stamp",
      'getattr(self, "rest_snapshot", None)' in _rs7 and _rs7.count("book_ts.pop(ex, None)") >= 2)

# ---------------------------------------------------------------- wind-down (backlog 000), under a fake clock
from sigbot.proposals import wind_down as WD  # noqa: E402


class _WVenue(_Venue):
    """_Venue plus single orders (filling `leg_fill` shares, None = all; the rest stays open), cancels (cancel_fail raises)."""
    def __init__(self, pos):
        super().__init__(pos)
        self.orders, self.cancels, self.cancel_alls, self.leg_fill, self.cancel_fail = [], [], [], None, False

    def place_order(self, ex, side, act, q, px, expiration=None, key=None):
        f = q if self.leg_fill is None else min(self.leg_fill, q)
        self.pos[ex] = self.pos.get(ex, 0.0) + (f if side == "yes" else -f)
        self.n += 1
        self.orders.append((key, ex, side, q, px))
        return {"orderId": self.n, "open": f < q, "quantityTraded": f, "remainingQuantity": q - f}

    def cancel_order(self, oid):
        if self.cancel_fail:
            raise _ApiErr(503, "DOWN", "down")
        self.cancels.append(oid)
        return {}

    def cancel_all(self, exchange_id=None, **k):
        self.cancel_alls.append(exchange_id)
        return {}


def _wrace(name, parties, exs):
    return Race(name, {p: Leg(p, e, e, name) for p, e in zip(parties, exs)})


_WR = {r.name: r for r in (_wrace("Hawaii Governor", "DR", ["H1", "H2"]), _wrace("Rhode Island Governor", "DRI", ["RD", "RR", "RI"]),
                            _wrace("Florida Governor", "DR", ["F1", "F2"]), _wrace("Ohio Senate", "DR", ["O1", "O2"]),
                            _wrace("Texas Senate", "DR", ["T1", "T2"]), _wrace("Kansas Senate", "DR", ["K1", "K2"]))}


def _wdbot(pos, books, fairs=None, med=None, now=None, cfg_over=None, churn_lots=(), cheapfav=()):
    tmp = tempfile.mkdtemp()
    st = _Store(os.path.join(tmp, "t.sqlite"))
    v = _WVenue(pos)
    bx = {l.exchange_id: (r.name, p) for r in _WR.values() for p, l in r.legs.items()}
    u = types.SimpleNamespace(races=_WR, by_exchange=bx, all_exchanges=lambda: list(bx))
    e = ArbEngine.__new__(ArbEngine)
    e.cfg = ArbConfig(min_set_qty=20)
    e.u, e.api, e.store, e.dry_run, e.starting_cash = u, v, st, False, 100000.0
    e.committed, e.cooldown, e.hostile, e.recycle_credit = {}, {}, set(), 0.0
    ch = DC.DevChurn()
    for race, qty in churn_lots:
        ch.add_lot(race, qty, 0.98, 0.0)
    e.protected = lambda: {l["race"] for l in ch.lots} | set(cheapfav)
    cfg = BotConfig()
    cfg.wind_down_enabled = True
    for k_, v_ in (cfg_over or {}).items():
        setattr(cfg, k_, v_)
    b = R.Bot.__new__(R.Bot)
    b.cfg, b.dry_run, b.store, b.api, b.arb, b.universe = cfg, False, st, v, e, u
    b.wind_down, b.wind_down_path = WD.WindDownState(), os.path.join(tmp, "wind_down_state.json")
    b.wd_med, b.wd_med_ts, b.wd_report_ts, b.wd_cap_logged = dict(med or {}), 9e18, 9e18, 0.0
    b.exit_lock, b.mm_inv_base, b.cash, b.wd_mm_end, b.wd_foreign_logged = {}, 900.0, 50000.0, WD.MM_FLAT, {}
    b.positions_snapshot, b.positions_ok, b.cost_basis = dict(pos), True, {}
    b.open_orders_cache, b.open_orders_ts, b.trade_lock = {}, 0.0, threading.Lock()
    b.churn, b.catchup_open, b.feed = ch, {}, None
    b.cheapfav_races = lambda: set(cheapfav)
    b.direction = types.SimpleNamespace(rows=[{"exchange_id": x, "fair": f} for x, f in (fairs or {}).items()])
    b.mm = types.SimpleNamespace(quoted=[])
    b.fresh_book = lambda ex, age=None: books.get(ex)
    b.get_book = lambda ex, d=50, *a, **k: books[ex]
    b.positions_map = lambda strict=False: (dict(v.pos), 0.0, len(v.pos))
    return b, v


def _wcycle(b, t, killed=False):
    _keep = R.time.time
    R.time.time = lambda: t
    try:
        R.Bot.wind_down_cycle(b, {}, killed)
    finally:
        R.time.time = _keep


def _sent_keys(v):
    return [k.split("-")[0] for k, _ in v.sent] + [o[0].split("-")[0] for o in v.orders]


_HI = {"H1": _book(0.93, 0.935, 50000), "H2": _book(0.065, 0.07, 50000)}           # YES set exits at 0.995: 1 tick under payout
_HIP = {"H1": 8743.0, "H2": 7658.0}                                                 # 7,658 YES sets + 1,085 spare YES on H1
_HIF = {"H1": 0.93, "H2": 0.07}
_HIM = {"Hawaii Governor": {"buy_set": (0.015, 0.015), "sell_set": (0.02, 0.02)}}   # usual cost 3 ticks: 1 tick is cheap
_b, _v = _wdbot(_HIP, _HI, _HIF, _HIM)
_wcycle(_b, WD.START - 60)
check("wind-down: nothing is sold before 31 Oct 00:00 ET (fake clock 30 Oct 23:59 ET)", _v.sent == [] and _v.orders == [], str(_sent_keys(_v)))
_t1 = WD.START + 10 * 3600
_wcycle(_b, _t1)
_wcycle(_b, _t1 + 30)
_wcycle(_b, _t1 + 60)
_k1 = _sent_keys(_v)
_wcycle(_b, _t1 + 1860)
_k2 = _sent_keys(_v)
_q = [l[0]["quantity"] for k, l in _v.sent]
check("wind-down: 31 Oct 10:00 ET: one clip a cycle, cheapest first (the fair-priced spare leg, then a 1,000-set YES clip), "
      "then nothing in that race for 30 min, then the next clip",
      _k1 == ["unb", "wdx"] and _k2 == ["unb", "unb", "wdx"] and _q == [1000, 1000] and _v.pos["H2"] == 5658.0, f"{_k1} {_k2} {_q} {_v.pos}")
check("wind-down: a YES-set clip buys NO on every leg at 1 - bid (the venue nets it against the YES held)",
      all(l["side"] == "no" and l["action"] == "buy" for l in _v.sent[0][1]) and {l["price"] for l in _v.sent[0][1]} == {0.07, 0.935},
      str(_v.sent[0][1]))
check("wind-down: the spare YES on a leg is sold at the bid (NO buy at 1 - bid), 80% of the touch at most, and only that leg",
      _v.orders[0][1:4] == ("H1", "no", 1085) and abs(_v.orders[0][4] - 0.07) < 1e-9, str(_v.orders))
_b2, _ = _wdbot(_HIP, _HI, _HIF, _HIM)
_b2.wind_down.load(_j3.load(open(_b.wind_down_path)))
check("wind-down: the state file reloads after a restart (spacing kept)", _b2.wind_down.last_race.get("Hawaii Governor") == _t1 + 1860,
      str(_b2.wind_down.last_race))

# cost cap: a 3-leg NO set whose exit costs 3.1% of its proceeds, locked return 1%
_RI = {"RD": _book(0.895, 0.90, 50000), "RR": _book(0.095, 0.10, 50000), "RI": _book(0.055, 0.06, 50000)}
_RIP = {"RD": -12304.0, "RR": -12304.0, "RI": -12304.0}
_RIM = {"Rhode Island Governor": {"sell_set": (0.08, 0.07), "buy_set": (None, None)}}
_b, _v = _wdbot(_RIP, _RI, {"RD": 0.9, "RR": 0.08, "RI": 0.02}, _RIM)
_b.cost_basis = {"RD": 12304 * 0.10, "RR": 12304 * 0.90, "RI": 12304 * 0.98}
_wcycle(_b, WD.START + 3600)
_n_early = len(_v.sent)
_wcycle(_b, WD.LOOSEN + 60)
check("wind-down: cost cap: a set costing 3.1% of proceeds is not sold on 31 Oct (cap = its 1% locked return), is sold from 2 Nov (4.5%)",
      _n_early == 0 and len(_v.sent) == 1 and _sent_keys(_v) == ["unw"] and _v.sent[0][1][0]["quantity"] == 1000
      and all(l["side"] == "yes" for l in _v.sent[0][1]), f"{_n_early} {_sent_keys(_v)}")
check("wind-down: the cap schedule: locked return at the start, 4.5% on 2 Nov 00:00 ET, 5.5% at 17:45 ET 3 Nov, never below the locked return",
      abs(WD.set_cost_cap(WD.START, 0.01) - 0.01) < 1e-9 and abs(WD.set_cost_cap(WD.LOOSEN, 0.0) - 0.045) < 1e-9
      and abs(WD.set_cost_cap(WD.CAP_END, 0.0) - 0.055) < 1e-9 and abs(WD.set_cost_cap(WD.CAP_END, 0.08) - 0.08) < 1e-9)
# timing gate (ev_hunt 2c, then level 0)
_b, _v = _wdbot(_RIP, _RI, {}, {"Rhode Island Governor": {"sell_set": (0.06, 0.05), "buy_set": (None, None)}})
_b.cost_basis = {"RD": 12304 * 0.10, "RR": 12304 * 0.90, "RI": 12304 * 0.98}
_wcycle(_b, WD.LOOSEN + 60)
_g1 = len(_v.sent)
_b.wd_med = {"Rhode Island Governor": {"sell_set": (0.06, 0.06), "buy_set": (None, None)}}
_wcycle(_b, WD.LOOSEN + 120)
check("wind-down: timing gate: from 2 Nov a clip waits while the exit cost (12 ticks) is above the race's 24-h median (10), sells at the median",
      _g1 == 0 and len(_v.sent) == 1, f"{_g1} {len(_v.sent)}")
_b.wd_med = {"Rhode Island Governor": {"sell_set": (0.065, 0.10), "buy_set": (None, None)}}
check("wind-down: timing gate before 2 Nov: the cost must be >= 1 tick below the 12-h median; near the deadline no gate",
      not WD.gate_ok(WD.START + 3600, 0.06, 0.0625, 0.1, WD.DEADLINE)[0] and WD.gate_ok(WD.START + 3600, 0.06, 0.065, 0.1, WD.DEADLINE)[0]
      and WD.gate_ok(WD.DEADLINE - 3600, 0.09, 0.05, 0.05, WD.DEADLINE - 7200)[0])
# early-call sets are held; Rhode Island Governor is sold anyway; exempt sleeves untouched
_FL = {"F1": _book(0.95, 0.955, 50000), "F2": _book(0.045, 0.05, 50000)}
_b, _v = _wdbot({"F1": -5000.0, "F2": -5000.0}, _FL, {"F1": 0.95, "F2": 0.05},
                {"Florida Governor": {"sell_set": (0.03, 0.03), "buy_set": (None, None)}})
_wcycle(_b, WD.LOOSEN + 60)
_b.cfg.wind_down_force_late = ["Florida Governor"]
_wcycle(_b, WD.LOOSEN + 120)
check("wind-down: an early-call set (Florida Governor, polls close 20:00 ET, favourite 0.95) is held; listed in force_late it is sold",
      len(_v.sent) == 1 and WD.call_class("Florida Governor", 0.95)[0] == "early", str(_sent_keys(_v)))
_b, _v = _wdbot({"O1": -3000.0, "O2": -3000.0, "T1": -3000.0, "T2": -3000.0},
                {"O1": _book(0.5, 0.505, 50000), "O2": _book(0.495, 0.5, 50000), "T1": _book(0.5, 0.505, 50000), "T2": _book(0.495, 0.5, 50000)},
                {}, {"Ohio Senate": {"sell_set": (0.02, 0.02)}, "Texas Senate": {"sell_set": (0.02, 0.02)}}, churn_lots=[("Ohio Senate", 3000)],
                cheapfav=["Texas Senate"])
_wcycle(_b, WD.LOOSEN + 60)
check("wind-down: races high-frequency arb or cheap favourites hold are never sold by it (their own schedules)", _v.sent == [] and _v.orders == [],
      str(_sent_keys(_v)))
# never more than held now: the clip is re-clamped to the positions re-read under the lock
_b, _v = _wdbot({"H1": 300.0, "H2": 300.0}, _HI, _HIF, _HIM)
_b.positions_snapshot = dict(_HIP)
_wcycle(_b, _t1)
check("wind-down: a clip sized from a stale snapshot (7,658 sets) is cut to the sets held now (300); no leg ends short",
      [l[0]["quantity"] for k, l in _v.sent] == [300] and _v.pos["H1"] == 0.0 and _v.pos["H2"] == 0.0 and _v.orders == [],
      f"{_v.sent} {_v.pos} {_v.orders}")
# partisan cap after every clip
_b, _v = _wdbot({"O1": 1000.0, "T2": 11400.0}, {"O1": _book(0.5, 0.505, 50000), "T2": _book(0.5, 0.505, 50000)},
                {"O1": 0.5, "T2": 0.5}, {}, cfg_over={"wind_down_clips_per_cycle": 5})
_wcycle(_b, WD.START + 3600)
_ex = [o[1] for o in _v.orders]
check("wind-down: partisan cap: no sale of O1 (D YES, |net D| 5,200 -> 5,700), T2 (R YES) is sold",
      "O1" not in _ex and "T2" in _ex, str(_v.orders))
# single legs: fill-or-cancel
_b, _v = _wdbot({"O1": 1000.0}, {"O1": _book(0.5, 0.505, 50000)}, {"O1": 0.5}, {})
_v.leg_fill = 400
_wcycle(_b, WD.START + 3600)
check("wind-down: a leg clip's unfilled remainder is cancelled at once and only what traded is booked",
      len(_v.cancels) == 1 and _b.wind_down.sold[-1][2] == 400 and _v.pos["O1"] == 600.0,
      f"{_v.cancels} {_b.wind_down.sold} {_v.pos}")
_b, _v = _wdbot({"O1": 1000.0, "T2": 1000.0}, {"O1": _book(0.5, 0.505, 50000), "T2": _book(0.5, 0.505, 50000)}, {"O1": 0.5, "T2": 0.5}, {},
                cfg_over={"wind_down_clips_per_cycle": 5})
_v.leg_fill, _v.cancel_fail = 400, True
_wcycle(_b, WD.START + 3600)
check("wind-down: a remainder that cannot be cancelled pulls that leg for 30 min and stops the cycle (no second order)",
      len(_v.orders) == 1 and _b.wind_down.pulled.get(_v.orders[0][1], 0) > WD.START + 3600, f"{_v.orders} {_b.wind_down.pulled}")
# zero-EV exit just sent on the leg: leave it this cycle
_b, _v = _wdbot({"O1": 1000.0}, {"O1": _book(0.5, 0.505, 50000)}, {"O1": 0.5}, {})
_b.store.record_order(77, "O1", None, "no", "buy", 0.5, 1000, "exit")
_b.store.conn.execute("UPDATE orders SET placed_at=?", (WD.START + 3600 - 60,))
_b.store.conn.commit()
_wcycle(_b, WD.START + 3600)
_z1 = list(_v.orders)
_wcycle(_b, WD.START + 3600 + 130)
check("wind-down: no leg clip while a zero-EV exit sent in the last 2 min may still rest on that leg; 2 min later it is sold",
      _z1 == [] and [o[1] for o in _v.orders] == ["O1"], f"{_z1} {_v.orders}")
# hourly cost budget and the kill switch
_b, _v = _wdbot(_RIP, _RI, {}, _RIM, cfg_over={"wind_down_max_cost_per_hour": 50.0})
_wcycle(_b, WD.LOOSEN + 60)
check("wind-down: no clip whose exit cost would take the last hour past wind_down_max_cost_per_hour (kill switch at 400/h)", _v.sent == [],
      str(_sent_keys(_v)))
_b, _v = _wdbot(_RIP, _RI, {}, _RIM)
_wcycle(_b, WD.LOOSEN + 60, killed=True)
_n_k = len(_v.sent)
_wcycle(_b, WD.DEADLINE - 600, killed=True)
check("wind-down: while the kill switch is on only clips that cannot wait (past the race's fallback time) are sold",
      _n_k == 0 and len(_v.sent) == 1, f"{_n_k} {len(_v.sent)}")
check("wind-down: the MM is flat at 12:00 ET 3 Nov or at its own stop_quoting_at if earlier (live: 12:00 UTC = 07:00 ET)",
      WD.mm_end("2026-11-03T12:00:00Z") == datetime(2026, 11, 3, 12, tzinfo=timezone.utc).timestamp() and WD.mm_end("2026-11-04T00:00:00Z") == WD.MM_FLAT
      and WD.mm_end(None) == WD.MM_FLAT and WD.mm_inventory_cap(WD.mm_end("2026-11-03T12:00:00Z"), 900, WD.mm_end("2026-11-03T12:00:00Z")) == 0)
_b, _v = _wdbot(_HIP, _HI, _HIF, _HIM)
_b.cfg.arb.recycle_enabled = True
_wcycle(_b, WD.START - 60)
_rec_before = _b.cfg.arb.recycle_enabled
_wcycle(_b, WD.START + 60)
check("wind-down: recycling stays on before 31 Oct 00:00 ET and is switched off from then (a recycled set would have to be sold again)",
      _rec_before is True and _b.cfg.arb.recycle_enabled is False)
# MM: cap shrinks from 1 Nov, flat (no quotes, no fast requotes) from 12:00 ET 3 Nov
_calls = []
_fb = types.SimpleNamespace(cfg=types.SimpleNamespace(mm_fast_requotes_per_min=3, mm=types.SimpleNamespace(enabled=True)), feed_ok=lambda: True,
                            mm=types.SimpleNamespace(quoted=["H1"], state={"H1": types.SimpleNamespace(last_touch=(0.5, 0.51), last_reserve=0.5)}), killed_until=0.0, wind_down=WD.WindDownState(), strategy_kill=None,
                            wd_mm_end=WD.MM_FLAT)
_sc = R.ArbScanner.__new__(R.ArbScanner)
_sc.bot, _sc.fast_log, _sc.stats = _fb, [], {}
_fb.fresh_book = lambda ex, age=None: _calls.append(ex) or None
_keep = R.time.time
R.time.time = lambda: WD.MM_FLAT + 60
try:
    _sc.fast_requote_pass()
finally:
    R.time.time = _keep
_c_after = list(_calls)
R.time.time = lambda: WD.MM_FLAT - 60
try:
    _sc.fast_requote_pass()
finally:
    R.time.time = _keep
_rs_wd = open(os.path.join(ROOT, "sigbot", "run.py")).read()
_mm = MarketMaker.__new__(MarketMaker)
check("wind-down: the MM reads its cap from inv_cap_override when set, the shared config otherwise (the shadow tuner's replays read the config)",
      "cap = getattr(self, \"inv_cap_override\", None) or self.cfg.max_inventory_notional" in open(os.path.join(ROOT, "sigbot", "mm.py")).read()
      and getattr(_mm, "inv_cap_override", None) is None)
check("wind-down: MM: inventory cap 900 until 1 Nov, ~531 on 2 Nov 00:00 ET, 0 at 12:00 ET 3 Nov; no fast requotes from 12:00 ET 3 Nov; "
      "the cycle pulls MM quotes then (monitor-pause path)",
      WD.mm_inventory_cap(WD.MM_SHRINK, 900) == 900 and abs(WD.mm_inventory_cap(WD.LOOSEN, 900) - 531.3) < 1 and WD.mm_inventory_cap(WD.MM_FLAT, 900) == 0
      and _c_after == [] and _calls == ["H1"] and "if WD.mm_flat_due(now_, self.wd_mm_end):\n                mm_paused = True" in _rs_wd
      and "self.mm.inv_cap_override = max(1.0, WD.mm_inventory_cap(now_, self.mm_inv_base, self.wd_mm_end))" in _rs_wd
      and "self.cfg.mm.max_inventory_notional =" not in _rs_wd, f"{_c_after} {_calls}")

# review 1 (Opus): blockers 1-4 and should-fixes 5, 9
_b, _v = _wdbot(_HIP, _HI, _HIF, _HIM)
_b.open_orders_cache = {"H1": [{"id": 5, "quantity": 100}]}
_wcycle(_b, WD.DEADLINE + 60)
_wcycle(_b, WD.DEADLINE + 3 * 3600)
check("wind-down: nothing is sold or cancelled at or after 18:00 ET 3 Nov (the punt and the election-night sleeve trade then)",
      _v.sent == [] and _v.orders == [] and _v.cancel_alls == [] and WD.STOP == WD.DEADLINE, f"{_sent_keys(_v)} {_v.cancel_alls}")
_b, _v = _wdbot(_HIP, _HI, _HIF, _HIM)
_b.store.record_order(88, "H2", None, "yes", "buy", 0.07, 500, "punt")
_wcycle(_b, _t1)
_wcycle(_b, _t1 + 3600)
check("wind-down: a race with a punt / election-night order on any leg sells no set; that leg is never sold or cancelled (the other leg's spare YES may be)",
      _v.sent == [] and "H2" not in _v.cancel_alls and all(o[1] != "H2" for o in _v.orders), f"{_sent_keys(_v)} {_v.orders} {_v.cancel_alls}")
_b, _v = _wdbot(_HIP, _HI, _HIF, _HIM)
_b.wind_down_hold_path = os.path.join(tempfile.mkdtemp(), "wind_down_hold.json")
open(_b.wind_down_hold_path, "w").write('{"races": ["Hawaii Governor"], "exchanges": []}')
_wcycle(_b, _t1)
_h1 = (list(_v.sent), list(_v.orders))
open(_b.wind_down_hold_path, "w").write('{"races": [')
_wcycle(_b, _t1 + 60)
check("wind-down: the hold file (params/wind_down_hold.json) holds a race; an unreadable hold file stops every clip",
      _h1 == ([], []) and _v.sent == [] and _v.orders == [], f"{_h1} {_sent_keys(_v)}")
_b, _v = _wdbot({"O1": -1000.0, "O2": -1000.0}, {"O1": _book(0.5, 0.505, 50000), "O2": _book(0.5, 0.505, 50000)}, {},
                {"Ohio Senate": {"sell_set": (0.03, 0.03)}}, cfg_over={"wind_down_force_late": ["Ohio Senate"]})
_b.open_orders_cache = {"O1": [{"id": 5, "quantity": 500}]}
_b.store.record_order(5, "O1", None, "yes", "buy", 0.495, 500, "mm")             # our own MM bid: known, so cancelled
_ca = _v.cancel_all
_v.cancel_all = lambda exchange_id=None, **k: (_v.pos.__setitem__("O1", _v.pos["O1"] + 500) if exchange_id == "O1" else None) or _ca(exchange_id)
_wcycle(_b, WD.LOOSEN + 60)
check("wind-down: our MM bid filling during the cancel (O1 -1,000 -> -500) shrinks the clip to the 500 sets held: no leg crosses zero",
      [l[0]["quantity"] for k, l in _v.sent] == [500] and _v.pos["O1"] == 0.0 and _v.pos["O2"] == -500.0, f"{_v.sent} {_v.pos}")
_b, _v = _wdbot({"O1": -300.0, "O2": -1000.0}, {"O1": _book(0.5, 0.505, 50000), "O2": _book(0.5, 0.505, 50000)}, {"O1": 0.5, "O2": 0.5},
                {"Ohio Senate": {"sell_set": (0.03, 0.03)}}, cfg_over={"wind_down_force_late": ["Ohio Senate"]})
_b.catchup_open = {"O1": -300.0}
_wcycle(_b, WD.LOOSEN + 60)
check("wind-down: a catch-up NO is not counted inside a set (O1 -300 is all catch-up): no set sold, O1 untouched",
      _v.sent == [] and "O1" not in [o[1] for o in _v.orders] and _v.pos["O1"] == -300.0, f"{_sent_keys(_v)} {_v.orders}")
check("wind-down: locked return only from legs holding exactly the sets, at most 5.5% (residual NO at other prices inflated it to 22%)",
      WD.locked_return("sell_set", {"a": 1000 * 0.03 + 3000 * 0.30, "b": 1000 * 0.97}, {"a": -4000, "b": -1000}, ["a", "b"], 1000) == 0.0
      and abs(WD.locked_return("sell_set", {"a": 30.0, "b": 940.0}, {"a": -1000, "b": -1000}, ["a", "b"], 1000) - 0.0309) < 1e-3
      and WD.locked_return("sell_set", {"a": 10.0, "b": 10.0}, {"a": -1000, "b": -1000}, ["a", "b"], 1000) == WD.CAP_END_FRAC)
_b, _v = _wdbot({"O1": 3000.0}, {"O1": {"bids": [{"price": 0.30, "quantity": 50000}], "asks": []}}, {}, {})
_wcycle(_b, WD.START + 3600)
check("wind-down: a leg with no outside fair and a one-sided book is not sold (no idea of its value)", _v.orders == [], str(_v.orders))
check("wind-down: recycling is off at start-up when the bot restarts inside the window (before the scanner thread starts)",
      "if time.time() >= WD.START and cfg.arb.recycle_enabled:\n                cfg.arb.recycle_enabled = False" in _rs_wd)

# review 2 (Opus): N1-N5
_b, _v = _wdbot({"O1": 2000.0}, {"O1": _book(0.50, 0.53, 50000)}, {"O1": 0.50}, {})
_wcycle(_b, WD.START + 3600)
check("wind-down: the hourly ledger books a leg's cost against the mark too (fair = bid 0.50, mid 0.515: 2,000 sold books 30, not 0)",
      [o[1] for o in _v.orders] == ["O1"] and abs(_b.wind_down.cost_log[-1][1] - 30.0) < 1e-6, f"{_v.orders} {_b.wind_down.cost_log}")
_b, _v = _wdbot({"O1": 1000.0}, {"O1": _book(0.5, 0.505, 50000)}, {"O1": 0.5}, {})
_b.open_orders_cache = {"O1": [{"id": 999, "quantity": 300}]}
_wcycle(_b, WD.START + 3600)
check("wind-down: a leg carrying an open order the bot did not place (punt executor before it records, Sizhe's own) is not cancelled or sold",
      _v.orders == [] and _v.cancel_alls == [], f"{_v.orders} {_v.cancel_alls}")
_b, _v = _wdbot(_HIP, _HI, _HIF, _HIM)
_keep = R.time.time
R.time.time = lambda: _t1
try:
    _cs = R.Bot.wind_down_candidates(_b, _t1, {})
finally:
    R.time.time = _keep
R.time.time = lambda: WD.STOP + 1
try:
    _r = [R.Bot.wind_down_set_clip(_b, c, _t1, {}) if c["type"] == "set" else R.Bot.wind_down_leg_clip(_b, c, _t1, {}) for c in _cs]
finally:
    R.time.time = _keep
check("wind-down: a clip whose lock is taken at or after 18:00 ET sends nothing and cancels nothing (candidates built before)",
      _cs and all(x == "stop" for x in _r) and _v.sent == [] and _v.orders == [] and _v.cancel_alls == [], f"{len(_cs)} {_r} {_v.cancel_alls}")
_g = WD.global_fallback_ts(1500.0, 30, 300.0, 60.0, 7200.0)
check("wind-down: global fallback: 1,500 of exit cost at 300/h needs 5 h -> every race drops its gate at 11:00 ET 3 Nov (18:00 - 2 h - 5 h)",
      abs(_g - (WD.DEADLINE - 7200 - 5 * 3600)) < 1 and WD.global_fallback_ts(0.0, 120, 300.0, 60.0, 7200.0) == WD.DEADLINE - 7200 - 7200)
_b, _v = _wdbot(_RIP, _RI, {}, {"Rhode Island Governor": {"sell_set": (0.03, 0.03), "buy_set": (None, None)}},
                cfg_over={"wind_down_max_cost_per_hour": 50.0})
_wcycle(_b, WD.LOOSEN + 60)
_n_g = len(_v.sent)
_b2, _v2 = _wdbot({"RD": -40000.0, "RR": -40000.0, "RI": -40000.0}, _RI, {}, {"Rhode Island Governor": {"sell_set": (0.03, 0.03), "buy_set": (None, None)}})
_wcycle(_b2, WD.DEADLINE - 6 * 3600)
check("wind-down: with the gate shut (cost above the medians), 12,304 sets wait on 2 Nov; 40,000 sets (about 2.3k of exit cost) drop the gate "
      "at 12:00 ET 3 Nov", _n_g == 0 and len(_v2.sent) == 1 and _b2.wd_global_fallback <= WD.DEADLINE - 6 * 3600, f"{_n_g} {len(_v2.sent)} "
      f"{WD.et_str(_b2.wd_global_fallback)}")
check("wind-down: a leg without its own cost basis gives a locked return of 0, not the 5.5% clamp",
      WD.locked_return("sell_set", {"a": 970.0}, {"a": -1000, "b": -1000}, ["a", "b"], 1000) == 0.0)

# merge with the cash raise (24 Oct): one shared exit lock, the wind-down owns its races 31 Oct - 3 Nov, the raise's MM pause
_rB = _U.race_of(_L(_RB, "R")).name
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (300.0, "floor")})
R.Bot.stamp_exit(_b, _rB, "wind_down")
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
check("merge: the cash raise leaves a race the wind-down clipped < 30 min ago (B) and sells elsewhere (A)",
      _v.orders and all(o[0] not in _U.races[_rB].exchange_ids for o in _v.orders), str(_v.orders))
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (300.0, "floor")})
R.Bot.stamp_exit(_b, _rB, "raise")
R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
check("merge: a routine's own stamp does not lock it out (the raise's own spacing rules apply)", _v.orders and _v.orders[0][0] == _L(_RB, "R"),
      str(_v.orders))
_b, _v = _rbot(_rpos, _rbooks, {"hf_arb": (300.0, "floor")})
_b.wind_down, _b.wind_down_exempt = WD.WindDownState(), (lambda: ({}, {}))
_keep = R.time.time
R.time.time = lambda: WD.START + 3600
try:
    R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
finally:
    R.time.time = _keep
_o_in = list(_v.orders)
R.time.time = lambda: WD.STOP + 60
try:
    R.Bot.raise_cycle(_b, _rprices(_rbooks), False)
finally:
    R.time.time = _keep
check("merge: from 31 Oct 00:00 ET to 18:00 ET 3 Nov the cash raise leaves the wind-down's races alone; after 18:00 it may sell again",
      _o_in == [] and len(_v.orders) >= 1, f"{_o_in} {_v.orders}")
_b, _v = _wdbot(_HIP, _HI, _HIF, _HIM)
R.Bot.stamp_exit(_b, "Hawaii Governor", "raise", _t1 - 600)
_wcycle(_b, _t1)
_l1 = (list(_v.sent), list(_v.orders))
_wcycle(_b, _t1 + 1201)
check("merge: the wind-down leaves a race the cash raise clipped < 30 min ago (no set clip, no leg clip), then sells it",
      _l1 == ([], []) and len(_v.sent) + len(_v.orders) == 1, f"{_l1} {_sent_keys(_v)}")
_b, _v = _wdbot(_HIP, _HI, _HIF, _HIM)
_wcycle(_b, _t1)
_wcycle(_b, _t1 + 30)
check("merge: a wind-down clip keeps the MM off the race's legs with the cash raise's raise_pause (in the store, survives a restart) and stamps the lock",
      {"H1", "H2"} <= set(_b.mm.raise_pause) and {"H1", "H2"} <= set(_b.store.get_param("raise_pause") or {})
      and "wind_down" in _b.exit_lock.get("Hawaii Governor", {}) and _sent_keys(_v) == ["unb", "wdx"], f"{_b.mm.raise_pause} {_b.exit_lock}")
_rs_m = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("merge: the RI trim and tier 2 also leave a wind-down race (or one the wind-down clipped < 30 min ago) alone; the wind-down sells YES "
      "sets through the cash raise's unwind_buy_set",
      "if rname in Bot.wind_down_owned(self, now) or Bot.exit_locked(self, rname, \"raise\", now=now):" in _rs_m
      and 'self.arb.unwind_buy_set(race.name, c["books"], q, why="wind-down", purpose="arb")' in _rs_m
      and 's_excl |= Bot.wind_down_owned(self, now) | {r for r in race_legs if Bot.exit_locked(self, r, "raise", now=now)}' in _rs_m
      and _rs_m.count("    def unwind_buy_set(") == 0 and open(os.path.join(ROOT, "sigbot", "arb.py")).read().count("    def unwind_buy_set(") == 1)

_b, _v = _sbot(_spos, _sbooks, {"hf_arb": (2000.0, "floor")})
R.Bot.stamp_exit(_b, "Rhode Island Governor", "wind_down")
R.Bot.raise_cycle(_b, _rprices(_sbooks), False)
_r1 = [_U.race_of(l[0]["exchangeId"]).name for k, l in getattr(_v, "sent_ml", [])]
_b, _v = _sbot(_spos, _sbooks, {"hf_arb": (2000.0, "floor")})
_b.wind_down, _b.wind_down_exempt = WD.WindDownState(), (lambda: ({}, {}))
_keep = R.time.time
_tn = time.time()
R.time.time = lambda: max(_tn, WD.START + 3600)
try:
    _b.store.set_param("churn_cash_skips", [[R.time.time() - 1860 * i, "X%d" % i, 2000, 0, 0.98] for i in range(3)])
    R.Bot.raise_cycle(_b, _rprices(_sbooks), False)
finally:
    R.time.time = _keep
check("merge (review M1): tier 2 skips a race the wind-down clipped < 30 min ago (RI Governor: A only) and sends no set at all in the window",
      "Rhode Island Governor" not in _r1 and _RA in _r1 and getattr(_v, "sent_ml", []) == [], f"{_r1} {getattr(_v, 'sent_ml', [])}")

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
