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

# the sleeves have dated deadlines (high-frequency arb stops entering 1 Nov 00:00 UTC, flatten and sell-all times on 3 Nov), so
# these tests run as of 30 Oct 12:00 UTC whatever the real date: on the real clock the gate refused every deploy from 1 Nov
_clock_real = time.time
_clock_shift = 1793361600.0 - _clock_real()
time.time = lambda: _clock_real() + _clock_shift

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
# behaviour: a zero-EV exit never closes what catch-up / cheap favourites / shock hold on the same leg (24 Oct: the venue
# position nets every sleeve, so without the cap a direction exit sells the cheapfav lot and crosses the account)
check("exits: the venue position is capped by the sleeves' own holdings",
      all(a in rsrc for a in ('"catchup_open"', '"cheapfav_open"', '"shock_open"')) and "for a in (\"catchup_open\", \"cheapfav_open\", \"shock_open\")" in rsrc)
for attr in ("catchup_open", "cheapfav_open", "shock_open"):
    sent.clear()
    bot.open_orders_cache = {}
    for a in ("catchup_open", "cheapfav_open", "shock_open"):
        setattr(bot, a, {})
    setattr(bot, attr, {"1068": 800.0})           # that sleeve holds 800 YES of the venue's 1203 YES
    R.Bot.zero_ev_exits(bot, {"1068": 1203.0}, [row])
    check(f"exits: {attr} keeps its 800, the exit closes only the other 403", sent == [("1068", "no", 403)], str(sent))
    sent.clear()
    setattr(bot, attr, {"1068": 1203.0})          # the sleeve holds all of it: no exit at all
    R.Bot.zero_ev_exits(bot, {"1068": 1203.0}, [row])
    check(f"exits: nothing sent when {attr} holds the whole venue position", sent == [], str(sent))
for a in ("catchup_open", "cheapfav_open", "shock_open"):
    setattr(bot, a, {})
# a failed positions read ({} with positions_ok False) sends nothing, even with a sleeve holding the other side
sent.clear()
bot.cheapfav_open = {"1068": -400.0}
bot.positions_ok = False
R.Bot.zero_ev_exits(bot, {}, [row])
check("exits: nothing sent on a failed positions read", sent == [], str(sent))
bot.positions_ok = True
R.Bot.zero_ev_exits(bot, {}, [row])
check("exits: an empty account with a sleeve on the other side makes no room to close", sent == [], str(sent))
# the shock's ledger read after the venue's (an add in between) must not make room either: venue -500, shock -750
sent.clear()
bot.cheapfav_open = {}
bot.shock_open = {"1068": -750.0}
R.Bot.zero_ev_exits(bot, {"1068": -500.0}, [row])
check("exits: a sleeve ledger ahead of the positions read sends nothing", sent == [], str(sent))
# never more than the venue position alone would allow (the cap before the sleeves were netted)
import random as _rnd
_g = _rnd.Random(7)
_worse = []
for _ in range(300):
    sent.clear()
    vp = float(_g.choice([-1500, -800, -300, 0, 200, 600, 1203, 2000]))
    for a in ("catchup_open", "cheapfav_open", "shock_open"):
        setattr(bot, a, {"1068": float(_g.choice([-900, -400, 0, 0, 300, 700]))})
    R.Bot.zero_ev_exits(bot, {"1068": vp}, [row])
    sl = sum(max(0.0, getattr(bot, a)["1068"]) for a in ("catchup_open", "cheapfav_open", "shock_open"))
    want = max(0, int(min(1203.0, vp - sl)))
    got = sum(q for _, _, q in sent)
    if got > max(0, int(min(1203.0, vp))) or got != want:
        _worse.append((vp, [getattr(bot, a)["1068"] for a in ("catchup_open", "cheapfav_open", "shock_open")], got))
check("exits: the sleeve cap never sends more than the venue position alone allows", not _worse, str(_worse[:3]))
# the same on the held-NO side (closing buys YES at the ask)
class StoreNo(Store):
    def rows(self, q, a=()):
        if "purpose IN ('direction','tilt')" in q:
            return [{"exchange_id": "1068", "side": "no", "q": 1203.0}]
        return Store.rows(self, q, a)
bot.store = StoreNo()
_worse = []
for _ in range(300):
    sent.clear()
    vp = float(_g.choice([-2000, -1203, -600, -200, 0, 300, 800, 1500]))
    for a in ("catchup_open", "cheapfav_open", "shock_open"):
        setattr(bot, a, {"1068": float(_g.choice([-700, -300, 0, 0, 400, 900]))})
    R.Bot.zero_ev_exits(bot, {"1068": vp}, [row])
    sl = sum(max(0.0, -getattr(bot, a)["1068"]) for a in ("catchup_open", "cheapfav_open", "shock_open"))
    want = max(0, int(min(1203.0, -vp - sl)))
    got = sum(q for _, s_, q in sent if s_ == "yes")
    if got > max(0, int(min(1203.0, -vp))) or got != want or any(s_ != "yes" for _, s_, _ in sent):
        _worse.append((vp, [getattr(bot, a)["1068"] for a in ("catchup_open", "cheapfav_open", "shock_open")], sent[:]))
check("exits: held NO too: the sleeve cap never sends more than the venue position alone allows", not _worse, str(_worse[:3]))
bot.store = Store()
for a in ("catchup_open", "cheapfav_open", "shock_open"):
    setattr(bot, a, {})

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
_rsh = open(os.path.join(ROOT, "sigbot", "run.py")).read()
_cyc = _rsh[_rsh.index("    def cycle(self):"):]
check("health: the kill switch gets cash, marks, settlement adjustment and the shock sleeve's P&L from one snapshot",
      "self.health(cycle_cash + pos_value + cycle_adj - shock_pnl, cycle_id)" in _cyc
      and _cyc.index("with self.trade_lock:") < _cyc.index("shock_pnl = self.shock_ledger(getattr(self, \"venue_mark\", None))[0]") < _cyc.index("open_orders = self.open_orders_map()"))

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
check("catchup: a clip is at the touch, capped by 80% of what shows (first clip of a 2,500 trade at 50c: 5,000 shares wanted)",
      cs.size({"d": 1, "ask": 0.5, "bid": 0.495}, 5000)[2] == 4000 and cs.size({"d": 1, "ask": 0.5, "bid": 0.495}, 400)[2] == 320
      and cs.size({"d": -1, "ask": 0.705, "bid": 0.7}, 5000)[:2] == ("no", 0.3) and cs.size({"d": 1, "ask": 0.5, "bid": 0.495}, 5000, 700)[2] == 700)
check("catchup: tiny touch -> no trade", cs.size({"d": 1, "ask": 0.5, "bid": 0.495}, 100)[2] == 0)
check("catchup: partisan cap: no D-adding entry near the cap, R-adding allowed, never past the cap",
      not cs.can_enter(0, "D", 4850, 300) and cs.can_enter(0, "R", 4850, 300) and not cs.can_enter(0, "D", 4600, 500)
      and cs.can_enter(0, "D", 2000, 300))
check("catchup: at most 6 open positions", not cs.can_enter(6, "R", 0, 300) and cs.can_enter(5, "R", 0, 300))
cs.ctx["10"] = {"ts": T0, "d": 1, "f_then": 0.50, "move": 0.03, "mid0": 0.50, "gap": 0.03}
check("catchup: NO exit when SIG has moved 70% of the gap (31 Oct: the convergence exit cut the winners; was 'SIG caught up')",
      cs.exit_reason("10", _row(T0 + 600, 0.53, 0.53, 0.521), T0 + 600) is None and cs.exit_reason("10", _row(T0 + 600, 0.53, 0.53, 0.53), T0 + 600) is None)
check("catchup: hold while SIG has not caught up and the outside holds", cs.exit_reason("10", _row(T0 + 600, 0.53, 0.53, 0.51), T0 + 600) is None)
check("catchup: exit when the outside move reverts", cs.exit_reason("10", _row(T0 + 600, 0.505, 0.505, 0.50), T0 + 600) == "outside move reverted")
check("catchup: exit when Polymarket and Kalshi disagree by > 4 pts", cs.exit_reason("10", _row(T0 + 600, 0.58, 0.53, 0.50), T0 + 600) is not None)
check("catchup: a position whose context has no kind (written by the old code) keeps its 12 h time stop",
      cs.exit_reason("10", _row(T0 + 43201, 0.53, 0.53, 0.51), T0 + 43201) == "12 h time stop (legacy)" and cs.exit_reason("10", _row(T0 + 43100, 0.53, 0.53, 0.51), T0 + 43100) is None)
check("catchup: flatten everything before the punt (3 Nov 11:30 ET)", cs.exit_reason("10", _row(OS.FLATTEN_AT, 0.53, 0.53, 0.51), OS.FLATTEN_AT) is not None)
check("catchup: a position without entry context (restart) is closed", cs.exit_reason("99", None, T0) is not None)
pnl, op = OS.pnl_from_fills([{"exchange_id": "10", "quantity": 600, "price": 0.50}, {"exchange_id": "10", "quantity": -600, "price": 0.41}], {})
check("catchup: P&L from fills (buy 600 @0.50, sell @0.41 = -54) and the sleeve switches itself off at -50",
      abs(pnl + 54) < 1e-6 and op == {} and cs.check_kill(pnl) and cs.signals([_row(T0 + 3600, 0.53, 0.53, 0.50)], T0 + 3600) == [])
check("catchup: live config: on, 2,500 base a trade tilted to at most 5,000, 15k open at most, -400 stop (sized with the trades: the old -50 is under the spread of one 5k trade)", _live.get("catchup_enabled") is True
      and _live.get("catchup_notional") == 2500.0 and _live.get("catchup_notional_max") == 5000.0 and _live.get("catchup_max_open") == 15000.0
      and _live.get("catchup_kill") == -400.0)

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
    b = types.SimpleNamespace(catchup=cs, dry_run=False, cash=1e9, store=_CStore(fills), direction=types.SimpleNamespace(rows=[{"exchange_id": "0", "race": "R0", "party": "D", "sig_mid": 0.5025}]),
                              universe=uni, open_orders_cache={}, cfg=types.SimpleNamespace(mm=types.SimpleNamespace(portfolio_max_partisan_notional=5000.0)),
                              fresh_book=(lambda ex, age=None: bk) if fresh else (lambda ex, age=None: None),
                              get_book=lambda ex, depth=50, client=None, max_age=None: {"bids": [{"price": 0.48, "quantity": 5000}], "asks": [{"price": 0.485, "quantity": 5000}]},
                              api=types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: sent.append((ex, side, q, px)) or {"orderId": len(sent)},
                                                        new_key=lambda p: p),
                              catchup_state_path=os.path.join(tempfile.mkdtemp(), "cs.json"))
    b.catchup_save = lambda: R.Bot.catchup_save(b)
    b._catchup_trade = lambda *a_: R.Bot._catchup_trade(b, *a_)
    b.catchup_pending = lambda now: R.Bot.catchup_pending(b, now)
    _orig = R.risk.exposure
    R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=net_d)
    try:
        R.Bot.catchup_cycle(b, {"polled": True}, {}, {}, False)
    finally:
        R.risk.exposure = _orig
    return b, sent
_sigs = [{"exchange_id": str(i), "race": "R%d" % i, "party": "D", "d": 1, "move": 0.03, "f_now": 0.53, "f_then": 0.50, "mid": 0.5025, "gap": 0.0275,
          "bid": 0.50, "ask": 0.505, "kind": "slow", "pts": 0, "right": False, "move30": None} for i in range(4)]
_b, _sent = _cbot([], _sigs, 4400.0)
check("catchup: four D-adding signals in one poll at net D 4,400: entries stop at net D 4,800 (clip cut to the room), never past 5,000 (was: all four, 5,600)",
      len(_sent) == 1 and 4400.0 + sum(x[2] * x[3] for x in _sent) <= 4800.0 + 1e-6 and _sent[0][2] * _sent[0][3] > 350, str(_sent))
_b, _sent = _cbot([], _sigs, 0.0)
check("catchup: four D-adding signals at net D 0: clips stop at net D 4,800 (the third is cut to the room, the fourth gets none), each <= 80% of its touch",
      len(_sent) == 3 and sum(x[2] * x[3] for x in _sent) <= 4800.0 + 1e-6 and sum(x[2] * x[3] for x in _sent) > 4700.0, str(_sent))
_b, _sent = _cbot([], _sigs, -4000.0)
check("catchup: ... with net D -4,000 all four D-adding entries fit, each <= 2,500 (base size), up to 80% of the touch (4,000 shares)",
      len(_sent) == 4 and all(x[2] * x[3] <= 2500.0 + 1e-6 and x[2] == 4000 for x in _sent), str(_sent))
_b, _sent = _cbot([{"exchange_id": "7", "quantity": 600.0, "price": 0.50}], [], 0.0, fresh=False)
check("catchup: an exit with no fresh feed book is priced off a REST read (sell YES at its bid 0.48 = buy NO @0.52), never a stale row",
      _sent == [("7", "no", 600, 0.52)], str(_sent))
_mmb = types.SimpleNamespace(catchup_open={"5": 600.0})
check("mm: the catch-up sweep's own position is not counted as MM inventory", R.Bot.mm_positions(_mmb, {"5": 650.0, "6": -10.0}) == {"5": 50.0, "6": -10.0})

# ---------------------------------------------------------------- 31 Oct: catch-up follows the dir_alpha research (exits, size, cap, wind-down)
# fast / slow tag
def _hist(rows_by_age, now_row):
    c = OS.CatchupSweep(); [c.push([r]) for r in rows_by_age]; c.push([now_row])
    return c, c.signals([now_row], now_row["ts"])
_fast_then = [_row(T0, 0.50, 0.50, 0.50), _row(T0 + 1800, 0.51, 0.51, 0.50)]
_cf, sg = _hist(_fast_then, _row(T0 + 3600, 0.53, 0.53, 0.50))
check("catchup tag: both venues up 2c in the last 30 min, SIG flat, leg at 50c -> fast", len(sg) == 1 and sg[0]["kind"] == "fast", str(sg))
_cf, sg = _hist([_row(T0, 0.50, 0.50, 0.50), _row(T0 + 1800, 0.525, 0.525, 0.50)], _row(T0 + 3600, 0.53, 0.53, 0.50))
check("catchup tag: the move came earlier (only 0.5c in the last 30 min) -> slow", len(sg) == 1 and sg[0]["kind"] == "slow", str(sg))
_cf, sg = _hist([_row(T0, 0.50, 0.50, 0.50), _row(T0 + 1800, 0.515, 0.515, 0.50)], _row(T0 + 3600, 0.53, 0.53, 0.508))
check("catchup tag: SIG copied half of the 30-min move (0.8c of 1.5c) -> slow, not fast", len(sg) == 1 and sg[0]["kind"] == "slow", str(sg))
_cf, sg = _hist([_row(T0, 0.82, 0.82, 0.82), _row(T0 + 1800, 0.83, 0.83, 0.82)], _row(T0 + 3600, 0.85, 0.85, 0.82))
check("catchup tag: leg priced 82c is outside the fast 25-75 range -> slow, and slow with our side at 82c+ is skipped", sg == [], str(sg))
_cf, sg = _hist([_row(T0, 0.70, 0.70, 0.70), _row(T0 + 1800, 0.71, 0.71, 0.70)], _row(T0 + 3600, 0.73, 0.73, 0.70, bid=0.6975, ask=0.7025))
check("catchup tag: a fast signal with our side at 70.25c is NOT skipped (the 70c filter is slow only)", len(sg) == 1 and sg[0]["kind"] == "fast", str(sg))
_cf, sg = _hist([_row(T0, 0.67, 0.67, 0.67)], _row(T0 + 3600, 0.70, 0.70, 0.67, bid=0.6675, ask=0.6725))
check("catchup filter: slow with our side at 67.25c is kept, ...", len(sg) == 1 and sg[0]["kind"] == "slow", str(sg))
_cf, sg = _hist([_row(T0, 0.70, 0.70, 0.70)], _row(T0 + 3600, 0.73, 0.73, 0.70, bid=0.6975, ask=0.7025))
check("catchup filter: ... slow with our side at 70.25c (the ask) is skipped; selling a leg at 30c costs 70.25c on the NO side, also skipped",
      sg == [] and _hist([_row(T0, 0.30, 0.30, 0.30)], _row(T0 + 3600, 0.27, 0.27, 0.30, bid=0.2975, ask=0.3025))[1] == [])
# conviction points and the line
_cp = OS.CatchupSweep()
_cp.inputs = {"Rc": {"n_polls": 0, "rating_margin": 1.0}, "Rp": {"n_polls": 3, "rating_margin": 6.0}}
check("catchup points: no polls (1) + toss-up (1) + gap 3c in the upper two thirds (1) = 3; a polled lean race with a 1c gap = 0",
      _cp.points("Rc", "slow", 0.0, 0.03, None) == 3 and _cp.points("Rp", "slow", 0.0, 0.01, None) == 0 and _cp.points("Rc", "slow", 0.0, 0.01, None) == 2)
check("catchup points: quote unchanged 2 h+ is a point (7,199 s is not), fast counts the 30-min move (2.28c) instead of the gap, an unknown race has none of the model points",
      _cp.points("Rp", "slow", 7200.0, 0.01, None) == 1 and _cp.points("Rp", "slow", 7199.0, 0.01, None) == 0
      and _cp.points("Rp", "fast", 0.0, 0.05, 0.0227) == 0 and _cp.points("Rp", "fast", 0.0, 0.0, 0.0229) == 1 and _cp.points("Nowhere", "slow", 0.0, 0.0, None) == 0)
_cu = OS.CatchupSweep(); _cu.push([_row(T0, 0.5, 0.5, 0.50)]); _cu.push([_row(T0 + 5000, 0.5, 0.5, 0.50)]); _cu.push([_row(T0 + 8000, 0.5, 0.5, 0.505)])
check("catchup points: the unchanged clock restarts when SIG's mid moves (a short history reads short, never long)",
      _cu.unchanged_for("10", T0 + 8000) == 0.0 and _cu.unchanged_for("10", T0 + 8000 + 7300) == 7300.0 and OS.CatchupSweep().unchanged_for("10", T0) == 0.0)
_cf, sg = _hist([_row(T0, 0.50, 0.50, 0.50), _row(T0 + 1800, 0.51, 0.51, 0.50)], _row(T0 + 3600, 0.53, 0.53, 0.50))
check("catchup line: the leg at 50c, outside 53c: the market maker's line is 52.76c, 2.76c on the right side of a buy -> right", sg[0]["right"] is True)
_cf, sg = _hist([_row(T0, 0.50, 0.50, 0.50)], _row(T0 + 3600, 0.53, 0.53, 0.5095))
check("catchup line: SIG already at 50.95c (1.8c under the line) -> not right-side; ... and a sell with the leg richer than the line is right-side",
      sg[0]["right"] is False and _hist([_row(T0, 0.50, 0.50, 0.50)], _row(T0 + 3600, 0.47, 0.47, 0.50))[1][0]["right"] is True)
# size
_s0 = {"kind": "slow", "pts": 0, "right": False, "move30": None}
_cz = OS.CatchupSweep()
check("catchup size: base 2,500; 2 points x1.4 = 3,500; 3+ points x2 = 5,000; right of the line x1.4 (both 4,900); 3+ and right capped at 5,000 (never 7,000)",
      abs(_cz.target_notional(_s0) - 2500) < 1e-6 and abs(_cz.target_notional(dict(_s0, pts=1)) - 2500) < 1e-6
      and abs(_cz.target_notional(dict(_s0, pts=2)) - 3500) < 1e-6 and abs(_cz.target_notional(dict(_s0, pts=3)) - 5000) < 1e-6
      and abs(_cz.target_notional(dict(_s0, right=True)) - 3500) < 1e-6 and abs(_cz.target_notional(dict(_s0, pts=2, right=True)) - 4900) < 1e-6
      and abs(_cz.target_notional(dict(_s0, pts=4, right=True)) - 5000) < 1e-6)
check("catchup size: a fast signal under 2.3c (smallest third) gets 0.6 of the base, one at 2.3c+ gets the full tilt",
      abs(_cz.target_notional(dict(_s0, kind="fast", move30=0.02)) - 1500) < 1e-6 and abs(_cz.target_notional(dict(_s0, kind="fast", move30=-0.0231)) - 2500) < 1e-6)
check("catchup size: shares for the trade come from the dollars at the touch (5,000 at 50c, 16,667 at 30c capped at 15,000 shares)",
      _cz.target_shares(dict(_s0, pts=3), 0.50) == 10000 and _cz.target_shares(dict(_s0, pts=3), 0.30) == 15000
      and OS.CatchupSweep(notional=300.0).target_shares(_s0, 0.50) == 600)
# exits
def _xcs(kind, px_yes=0.50, d=1):
    c = OS.CatchupSweep(); c.ctx["10"] = {"ts": T0, "d": d, "f_then": 0.50 if d > 0 else 0.50, "move": 0.03 * d, "mid0": 0.50, "gap": 0.03, "kind": kind}
    return c
def _xrow(t, mid, bid=None, ask=None, pm=0.53, k=0.53):
    return _row(t, pm, k, mid, bid=bid, ask=ask)
_fx = _xcs("fast")
check("catchup exit fast: sold at 8 h (28,800 s), held at 7 h 59 min", _fx.exit_reason("10", _xrow(T0 + 28800, 0.51), T0 + 28800) == "8 h time stop (fast)"
      and _fx.exit_reason("10", _xrow(T0 + 28740, 0.51), T0 + 28740) is None)
check("catchup exit fast: no take-profit however far it is up (+8c at 2 h), no stop however far it is down (-8c at 2 h)",
      _fx.exit_reason("10", _xrow(T0 + 7200, 0.58, bid=0.58, ask=0.585), T0 + 7200, 0.50, 3000.0) is None
      and _fx.exit_reason("10", _xrow(T0 + 7200, 0.42, bid=0.42, ask=0.425), T0 + 7200, 0.50, 3000.0) is None)
_sx = _xcs("slow")
check("catchup exit slow: held at 12 h and 19 h 59 min (was sold at 12 h), sold at 20 h",
      _sx.exit_reason("10", _xrow(T0 + 43201, 0.51), T0 + 43201) is None and _sx.exit_reason("10", _xrow(T0 + 71940, 0.51), T0 + 71940) is None
      and _sx.exit_reason("10", _xrow(T0 + 72000, 0.51), T0 + 72000) == "20 h time stop (slow)")
check("catchup exit slow: take-profit at +3c on the bid (bought YES at 50c, bid 53c), not at +2.9c; none without a known entry price",
      (_sx.exit_reason("10", _xrow(T0 + 3600, 0.535, bid=0.53, ask=0.54), T0 + 3600, 0.50, 3000.0) or "").startswith("slow take-profit")
      and _sx.exit_reason("10", _xrow(T0 + 3600, 0.5325, bid=0.529, ask=0.535), T0 + 3600, 0.50, 3000.0) is None
      and _sx.exit_reason("10", _xrow(T0 + 3600, 0.535, bid=0.53, ask=0.54), T0 + 3600) is None)
_sn = _xcs("slow", d=-1); _sn.ctx["10"]["f_then"] = 0.53; _sn.ctx["10"]["move"] = -0.03
check("catchup exit slow: a NO position (short YES at an average YES price of 0.60) takes profit when the ask is 3c lower (0.57), not at 0.575",
      (_sn.exit_reason("10", _xrow(T0 + 3600, 0.565, bid=0.56, ask=0.57, pm=0.50, k=0.50), T0 + 3600, 0.60, -3000.0) or "").startswith("slow take-profit")
      and _sn.exit_reason("10", _xrow(T0 + 3600, 0.57, bid=0.565, ask=0.575, pm=0.50, k=0.50), T0 + 3600, 0.60, -3000.0) is None)
check("catchup exit slow: no hard stop on a loser (-6c at 6 h while the outside holds): held; the outside reverting still sells it",
      _sx.exit_reason("10", _xrow(T0 + 21600, 0.44, bid=0.44, ask=0.445), T0 + 21600, 0.50, 3000.0) is None
      and _sx.exit_reason("10", _xrow(T0 + 21600, 0.44, bid=0.44, ask=0.445, pm=0.505, k=0.505), T0 + 21600, 0.50, 3000.0) == "outside move reverted")
check("catchup exit: Polymarket / Kalshi disagreement and the 3 Nov flatten still sell fast and slow",
      _fx.exit_reason("10", _xrow(T0 + 600, 0.50, pm=0.58, k=0.53), T0 + 600) is not None and _sx.exit_reason("10", _xrow(OS.FLATTEN_AT, 0.50), OS.FLATTEN_AT) is not None)
check("catchup exit: average YES price from fills: adds weight it, a part-sale keeps it, a flip restarts it at the flipping price",
      OS.avg_yes_from_fills([{"exchange_id": "1", "quantity": 1000, "price": 0.50}, {"exchange_id": "1", "quantity": 1000, "price": 0.52}]) == {"1": 0.51}
      and OS.avg_yes_from_fills([{"exchange_id": "1", "quantity": 1000, "price": 0.50}, {"exchange_id": "1", "quantity": -400, "price": 0.55}]) == {"1": 0.50}
      and abs(OS.avg_yes_from_fills([{"exchange_id": "1", "quantity": 1000, "price": 0.50}, {"exchange_id": "1", "quantity": -1500, "price": 0.55}])["1"] - 0.55) < 1e-9
      and OS.avg_yes_from_fills([{"exchange_id": "1", "quantity": 1000, "price": 0.50}, {"exchange_id": "1", "quantity": -1000, "price": 0.55}]) == {})
# the partisan room: only adds are limited
check("catchup cap: room to add D is cap - 200 - net D (0 at net D +4,965), room to add R is cap - 200 + net D (9,765 there); other legs unlimited",
      OS.CatchupSweep.room("D", 4965.0) == 0.0 and abs(OS.CatchupSweep.room("R", 4965.0) - 9765.0) < 1e-6
      and abs(OS.CatchupSweep.room("D", 3000.0) - 1800.0) < 1e-6 and OS.CatchupSweep.room(None, 4965.0) == float("inf") and OS.CatchupSweep.room("D", 5300.0) == 0.0)

# cycle level: repeat clips, partisan room for adds only, open-cost limit, cash reserve, exits at the cap, wind-down
class _Cyc:
    """A catch-up bot that lives across cycles: fills the test adds are what the store returns, the clock is set per cycle."""
    def __init__(self, sig=None, net_d=-4000.0, rows=None, touch=(0.50, 0.505, 5000), cash=None, **cskw):
        self.sent, self.fills, self.net_d, self.touch = [], [], net_d, touch
        self.cs = OS.CatchupSweep(**cskw)
        self.sig = dict(sig) if sig else None
        races = {"R%d" % i: types.SimpleNamespace(legs={}) for i in range(4)}
        uni = types.SimpleNamespace(by_exchange={str(i): ("R%d" % i, "D") for i in range(4)}, races=races)
        self.rows = rows if rows is not None else [{"exchange_id": "0", "race": "R0", "party": "D", "sig_mid": 0.5025, "pm": 0.53, "kalshi": 0.53,
                                                   "sig_bid": 0.50, "sig_ask": 0.505}]
        store = _CStore(self.fills)
        self.bot = types.SimpleNamespace(catchup=self.cs, dry_run=False, store=store, direction=types.SimpleNamespace(rows=self.rows),
                                         universe=uni, open_orders_cache={}, cfg=types.SimpleNamespace(mm=types.SimpleNamespace(portfolio_max_partisan_notional=5000.0)),
                                         fresh_book=lambda ex, age=None: {"bids": [{"price": self.touch[0], "quantity": self.touch[2]}], "asks": [{"price": self.touch[1], "quantity": self.touch[2]}]},
                                         get_book=lambda ex, depth=50, client=None, max_age=None: {"bids": [{"price": 0.48, "quantity": 5000}], "asks": [{"price": 0.485, "quantity": 5000}]},
                                         api=types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: self.sent.append((ex, side, q, px, key)) or {"orderId": len(self.sent)},
                                                                   new_key=lambda p: p),
                                         catchup_state_path=os.path.join(tempfile.mkdtemp(), "cs.json"), cash=1e9)
        self.bot.catchup_save = lambda: R.Bot.catchup_save(self.bot)
        self.bot._catchup_trade = lambda *a_: R.Bot._catchup_trade(self.bot, *a_)
        self.bot.catchup_pending = lambda now: R.Bot.catchup_pending(self.bot, now)
        if cash is not None:
            self.bot.cash, self.bot.cfg.shock_enabled, self.bot.cfg.shock_reserve = cash, True, 15000.0
        if sig:
            self.cs.signals = lambda rows, now: [self.sig] if self.sig else []
    def run(self, t, **attrs):
        for k_, v_ in attrs.items():
            setattr(self.bot, k_, v_)
        _orig, _ot = R.risk.exposure, time.time
        R.risk.exposure = lambda u, p, pr: types.SimpleNamespace(net_d_notional=self.net_d)
        time.time = lambda: t
        n0 = len(self.sent)
        try:
            R.Bot.catchup_cycle(self.bot, {"polled": True}, {}, {}, False)
        finally:
            R.risk.exposure, time.time = _orig, _ot
        return self.sent[n0:]
    def fill(self, ex, q, px):
        self.fills.append({"exchange_id": ex, "quantity": q, "price": px})
        self.net_d += q * px                         # a D leg bought adds D, sold (NO bought) adds R
_sg = {"exchange_id": "0", "race": "R0", "party": "D", "d": 1, "move": 0.03, "f_now": 0.53, "f_then": 0.50, "mid": 0.5025, "gap": 0.0275,
       "bid": 0.50, "ask": 0.505, "kind": "slow", "pts": 3, "right": True, "move30": None}
_c = _Cyc(_sg)                                                  # target 5,000 at 50.5c = 9,900 shares, touch 5,000 -> clips of 4,000
_o1 = _c.run(T0)
_c.fill("0", 4000, 0.505); _o2 = _c.run(T0 + 30)
_c.fill("0", 4000, 0.505); _o3 = _c.run(T0 + 60)
_c.fill("0", 1900, 0.505); _o4 = _c.run(T0 + 90)
check("catchup clips: 3,000-share-plus trade built in repeat clips of 80% of the touch (4,000 + 4,000 + 1,900 = the 9,900 target at 50.5c), then no more",
      [x[2] for x in _o1 + _o2 + _o3] == [4000, 4000, 1900] and _o4 == [] and all(x[1] == "yes" and x[3] == 0.505 for x in _o1 + _o2 + _o3), str((_o1, _o2, _o3, _o4)))
check("catchup clips: the trade's context carries kind, points, target and the clip count; the whole trade costs <= 5,000",
      _c.cs.ctx["0"]["kind"] == "slow" and _c.cs.ctx["0"]["target"] == 9900 and _c.cs.ctx["0"]["clips"] == 3 and sum(x[2] * x[3] for x in _o1 + _o2 + _o3) <= 5000.0 + 1e-6)
_c = _Cyc(_sg); _c.run(T0); _c.fill("0", 4000, 0.505); _c.touch = (0.515, 0.52, 5000)
check("catchup clips: the touch ran 1.5c away from the first clip (0.52 against 0.505): no second clip", _c.run(T0 + 30) == [])
_c.touch = (0.505, 0.51, 5000)
check("catchup clips: ... and it clips again at 0.51 (within 1c of the first)", [x[2] for x in _c.run(T0 + 40)] == [4000])
_c = _Cyc(_sg); _c.run(T0); _c.fill("0", 4000, 0.505)
check("catchup clips: no clip after the build window (slow 60 min): none at 3,601 s, a fast trade's window is 20 min",
      _c.run(T0 + 3601) == [] and (lambda c: (c.run(T0), c.fill("0", 4000, 0.505), c.run(T0 + 1201))[2])(_Cyc(dict(_sg, kind="fast"))) == [])
_c = _Cyc(_sg); _c.run(T0)
check("catchup clips: the last clip is still settling (nothing filled yet, 10 s): no second clip; after 90 s it counts what it holds (0) and tries the remaining target again",
      _c.run(T0 + 10) == [] and [x[2] for x in _c.run(T0 + 95)] == [4000])
_c = _Cyc(_sg); _c.run(T0); _c.fill("0", 4000, 0.505)
_c.rows[0].update(pm=0.505, kalshi=0.505)                          # the outside reverted
_oo = _c.run(T0 + 30)
check("catchup clips: when the outside reverts the trade is sold at the REST/feed bid, never clipped again, even in the next cycles",
      len(_oo) == 1 and _oo[0][1] == "no" and _oo[0][2] == 4000 and _c.cs.ctx["0"].get("exiting") is True and [x for x in _c.run(T0 + 200) if x[4] == "cu"] == [], str(_oo))
_c = _Cyc(_sg, net_d=4965.0)
check("catchup cap: at net D +4,965 a D-adding signal (buy YES on a D leg) places nothing - room 0, no order - and sets no context", _c.run(T0) == [] and _c.cs.ctx == {})
_sgr = dict(_sg, d=-1, bid=0.495, ask=0.50)
_c = _Cyc(_sgr, net_d=4965.0, touch=(0.495, 0.50, 5000))
_o = _c.run(T0)
check("catchup cap: ... but an R-adding signal (buy NO) trades at net D +4,965: 4,000 shares clip at 50.5c, room 9,765", len(_o) == 1 and _o[0][1] == "no" and _o[0][2] == 4000, str(_o))
_c = _Cyc(_sg, net_d=3000.0)
_o = _c.run(T0)
check("catchup cap: at net D +3,000 the D-adding clip is cut to the room under 4,800 (1,800 / 0.505 = 3,564 shares), a second clip later gets what is left (0)",
      len(_o) == 1 and _o[0][2] == int(1800 / 0.505) and (_c.fill("0", _o[0][2], 0.505), _c.run(T0 + 30))[1] == [], str(_o))
# exits are never blocked by the cap
_c = _Cyc(None, net_d=4965.0); _c.fills.append({"exchange_id": "1", "quantity": -3000.0, "price": 0.50})      # holds NO on a D leg: its exit buys YES = adds D
_c.cs.ctx["1"] = {"ts": T0, "d": -1, "f_then": 0.53, "move": -0.03, "mid0": 0.50, "gap": 0.03, "kind": "fast"}
_c.rows.append({"exchange_id": "1", "race": "R1", "party": "D", "sig_mid": 0.50, "pm": 0.50, "kalshi": 0.50, "sig_bid": 0.4975, "sig_ask": 0.5025})
_o = _c.run(T0 + 28800)
check("catchup cap: at net D +4,965 the 8 h fast exit of a NO on a D leg (the exit buys YES, which adds D) is sent in full - the cap blocks adds, not exits",
      len(_o) == 1 and _o[0][0] == "1" and _o[0][1] == "yes" and _o[0][2] == 3000 and _o[0][4].startswith("cux"), str(_o))
_c = _Cyc(None, net_d=-4965.0); _c.fills.append({"exchange_id": "1", "quantity": 3000.0, "price": 0.50})
_c.cs.ctx["1"] = {"ts": T0, "d": 1, "f_then": 0.50, "move": 0.03, "mid0": 0.50, "gap": 0.03, "kind": "slow"}
_c.rows.append({"exchange_id": "1", "race": "R1", "party": "D", "sig_mid": 0.535, "pm": 0.55, "kalshi": 0.55, "sig_bid": 0.53, "sig_ask": 0.54})
_c.touch = (0.53, 0.535, 5000)
_o = _c.run(T0 + 3600)
check("catchup cap: ... and at net D -4,965 the slow take-profit of a YES on a D leg (the exit sells D, buying NO) is sent", len(_o) == 1 and _o[0][1] == "no" and _o[0][2] == 3000, str(_o))
# open-cost limit and cash
_c = _Cyc(_sg, max_open_notional=1000.0)
_o = _c.run(T0)
_c.fill("0", _o[0][2], 0.505)
check("catchup limit: the sleeve's open cost is capped (1,000 here, 15,000 live): the first clip of 4,000 shares is cut to 1,980, and once that fills the sleeve is full: no second clip",
      _o and _o[0][2] == int(1000 / 0.505) and _c.run(T0 + 30) == [], str(_o))
_c = _Cyc(_sg, cash=17000.0)
_o = _c.run(T0)
check("catchup cash: with the 15k shock reserve and 17k cash only 2,000 is the sleeve's: the clip is cut to 3,960 shares (was: skipped); with 15.5k cash 500 -> 990 shares; under min_shares nothing",
      _o and _o[0][2] == int(2000 / 0.505) and len(_Cyc(_sg, cash=15500.0).run(T0)) == 1 and _Cyc(_sg, cash=15500.0).run(T0)[0][2] == int(500 / 0.505) and _Cyc(_sg, cash=15040.0).run(T0) == [], str(_o))
_c = _Cyc(_sg, max_positions=1)
_c.fills.append({"exchange_id": "1", "quantity": 500.0, "price": 0.50})
_c.cs.ctx["1"] = {"ts": T0, "d": 1, "f_then": 0.50, "move": 0.03, "mid0": 0.50, "gap": 0.03, "kind": "slow"}
check("catchup limit: max positions still blocks a NEW trade (1 open, max 1) but not the clips of the trade already open", _c.run(T0 + 60) == [])
# review 31 Oct (Opus): unread account / fills, timeouts, fresh-book take-profit, MM ledger, cash with the reserve off, points off the line
_c = _Cyc(_sg, net_d=-4000.0)
check("catchup review: a failed positions read (positions_ok False: net D would read 0 and the room look full) places no clip and no entry",
      _c.run(T0, positions_ok=False) == [] and _c.cs.ctx == {} and [x[2] for x in _c.run(T0 + 5, positions_ok=True)] == [4000])
_c = _Cyc(None, net_d=0.0); _c.fills.append({"exchange_id": "1", "quantity": 3000.0, "price": 0.50})
_c.cs.ctx["1"] = {"ts": T0, "d": 1, "f_then": 0.50, "move": 0.03, "mid0": 0.50, "gap": 0.03, "kind": "fast"}
_c.rows.append({"exchange_id": "1", "race": "R1", "party": "D", "sig_mid": 0.50, "pm": 0.50, "kalshi": 0.50, "sig_bid": 0.4975, "sig_ask": 0.5025})
check("catchup review: positions_ok False still sells (exits are not gated by the account read), but a failed FILLS read sends nothing (a stale ledger would repeat or flip the exit)",
      len(_c.run(T0 + 28800, positions_ok=False)) == 1 and len(_c.run(T0 + 28900, positions_ok=True, fills_read_ok=False)) == 0 and len(_c.run(T0 + 29000, fills_read_ok=True)) == 1)
_c = _Cyc(_sg, net_d=-4000.0); _c.run(T0, fills_read_ok=False)
check("catchup review: ... and no clip or entry while the fills read is down", _c.sent == [] and _c.cs.ctx == {})
def _boom(*a, **k): raise TimeoutError("timed out")
_c = _Cyc(_sg, net_d=-4000.0); _ok_po = _c.bot.api.place_order; _c.bot.api.place_order = _boom
_c.run(T0)                                                    # raises inside place_order: caught, counted as placed
_c.bot.api.place_order = _ok_po
check("catchup review: a TIMEOUT on a first clip is caught, counts as placed (context + clip clock written), and the next poll does not send a second clip within 90 s",
      "0" in _c.cs.ctx and _c.cs.ctx["0"]["ordered"] == 4000 and _c.run(T0 + 5) == [], str(_c.cs.ctx))
_c = _Cyc(None, net_d=0.0); _c.fills.append({"exchange_id": "1", "quantity": 3000.0, "price": 0.50})
_c.cs.ctx["1"] = {"ts": T0, "d": 1, "f_then": 0.50, "move": 0.03, "mid0": 0.50, "gap": 0.03, "kind": "fast"}
_c.rows.append({"exchange_id": "1", "race": "R1", "party": "D", "sig_mid": 0.50, "pm": 0.50, "kalshi": 0.50, "sig_bid": 0.4975, "sig_ask": 0.5025})
_ok_po = _c.bot.api.place_order; _c.bot.api.place_order = _boom
_c.run(T0 + 28800); _c.bot.api.place_order = _ok_po
check("catchup review: a TIMEOUT on an exit is not re-sent blind for 90 s (it could flip the position), then it is", _c.run(T0 + 28830) == [] and len(_c.run(T0 + 28800 + 95)) == 1)
_c = _Cyc(None, net_d=0.0); _c.fills.append({"exchange_id": "1", "quantity": 3000.0, "price": 0.50})
_c.cs.ctx["1"] = {"ts": T0, "d": 1, "f_then": 0.50, "move": 0.03, "mid0": 0.50, "gap": 0.03, "kind": "slow"}
_c.rows.append({"exchange_id": "1", "race": "R1", "party": "D", "sig_mid": 0.535, "pm": 0.55, "kalshi": 0.55, "sig_bid": 0.53, "sig_ask": 0.54})
_c.touch = (0.51, 0.515, 5000)                                # the direction row is stale: the live bid is 51c, +1c only
check("catchup review: the take-profit is confirmed on the fresh book the exit is priced off: row says +3c, book says +1c -> held; book at +3c -> sold",
      _c.run(T0 + 3600) == [] and (setattr(_c, "touch", (0.53, 0.535, 5000)), len(_c.run(T0 + 3700)))[1] == 1)
_c = _Cyc(_sg, net_d=-4000.0, cash=17000.0)
_c.bot.cfg.shock_enabled = False                              # the reserve is off: the cash check still applies (cash 17,000 here; below with 900 only)
_c.bot.cash = 900.0
_o = _c.run(T0)
check("catchup review: with the shock reserve off the clip is still cut to the cash on hand (900 / 0.505 = 1,782 shares)", _o and _o[0][2] == int(900 / 0.505), str(_o))
_c = _Cyc(_sg, net_d=-4000.0); _c.bot.universe.by_exchange.pop("0")
_o = _c.run(T0)
check("catchup review: a leg unknown to the universe (risk.adds None -> room unlimited) does not crash the cycle (inf / px)", len(_o) == 1, str(_o))
_mb = types.SimpleNamespace(catchup=OS.CatchupSweep(), dry_run=False, catchup_open={}, store=_CStore([{"exchange_id": "9", "quantity": 700.0, "price": 0.5}]))
R.Bot.catchup_ledger_refresh(_mb)
check("catchup review: catchup_open is rebuilt from the fills right after they are ingested (before the MM runs), so a fresh clip is not MM inventory", _mb.catchup_open == {"9": 700.0})
_rsrc = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("catchup review: ... and that refresh sits right after ingest_fills in the cycle, before the MM", "fills_count = self.ingest_fills()\n        self.catchup_ledger_refresh()" in _rsrc)
_cf, sg = _hist([_row(T0, 0.33, 0.33, 0.33)], _row(T0 + 3600, 0.30, 0.30, 0.33))
_cf2, sg2 = _hist([_row(T0, 0.50, 0.50, 0.50)], _row(T0 + 3600, 0.53, 0.53, 0.50))
check("catchup review: the slow upper-two-thirds point is the distance beyond the MM line: a sell with outside 30c / SIG 33c has a 3.0c raw gap but is only 1.4c beyond the line -> no point; at 50c the gap and the line distance (2.76c) both count",
      len(sg) == 1 and sg[0]["pts"] == 0 and abs(sg[0]["gap"] - 0.03) < 1e-9 and len(sg2) == 1 and sg2[0]["pts"] == 1, str((sg, sg2)))

# ---- 31 Oct, second review: REAL risk.exposure on a real universe, books carrying our own quotes, the punt's timed exit, pending orders
from sigbot.universe import Universe as _UnivC  # noqa: E402
_UC = _UnivC.from_markets(__import__("json").load(open(os.path.join(ROOT, "data", "markets.json"))))
_PUNT = "1076"                                                  # Rhode Island Senate D: stands in for the punt's short (C1 holds 10,196 NO on 842)
class _CStore2(_CStore):
    def __init__(self, fills, pending=()):
        super().__init__(fills); self.pending = list(pending)
    def rows(self, q, a=()):
        if "FROM orders" in q and "'cheapfav','direction'" in q: return self.pending
        if "FROM fills" in q and "'cheapfav','direction'" in q: return []
        return super().rows(q, a)
class _Real:
    """Catch-up cycles on a real universe and the real risk.exposure: positions and marks are real dicts, fills move them."""
    def __init__(self, sigs, positions, punt=(_PUNT,), pending=(), book=None, touch=(0.50, 0.505, 5000), rows=(), open_orders=None):
        self.sent, self.fills, self.positions = [], [], dict(positions)
        self.prices = {e: {"bestBid": 0.495, "bestAsk": 0.505} for e in _UC.all_exchanges()}
        self.prices[_PUNT] = {"bestBid": 0.73, "bestAsk": 0.75}
        self.cs = OS.CatchupSweep()
        self.sigs = list(sigs)
        self.cs.signals = lambda rows_, now: list(self.sigs)
        self.touch, self.book = touch, book
        self.rows = list(rows)
        for s in self.sigs:
            self.rows.append({"exchange_id": s["exchange_id"], "race": s["race"], "party": "D", "sig_mid": 0.5025, "pm": 0.53, "kalshi": 0.53, "sig_bid": 0.50, "sig_ask": 0.505})
        b = types.SimpleNamespace(catchup=self.cs, dry_run=False, cash=1e9, store=_CStore2(self.fills, pending), direction=types.SimpleNamespace(rows=self.rows),
                                  universe=_UC, open_orders_cache=open_orders or {}, cfg=types.SimpleNamespace(mm=types.SimpleNamespace(portfolio_max_partisan_notional=5000.0)),
                                  fresh_book=lambda ex, age=None: self.book if self.book is not None else {"bids": [{"price": self.touch[0], "quantity": self.touch[2]}], "asks": [{"price": self.touch[1], "quantity": self.touch[2]}]},
                                  get_book=lambda ex, depth=50, client=None, max_age=None: {"bids": [{"price": 0.48, "quantity": 5000}], "asks": [{"price": 0.485, "quantity": 5000}]},
                                  api=types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: self.sent.append((ex, side, q, px, key)) or {"orderId": len(self.sent)},
                                                            new_key=lambda p: p),
                                  catchup_state_path=os.path.join(tempfile.mkdtemp(), "cs.json"), fresh_positions=lambda: dict(self.positions),
                                  punt_legs=lambda: set(punt))
        b.catchup_save = lambda: R.Bot.catchup_save(b)
        b._catchup_trade = lambda *a_: R.Bot._catchup_trade(b, *a_)
        b.catchup_pending = lambda now: R.Bot.catchup_pending(b, now)
        self.bot = b
    def run(self, t, stale_positions=None, **attrs):
        for k_, v_ in attrs.items(): setattr(self.bot, k_, v_)
        _ot = time.time; time.time = lambda: t
        n0 = len(self.sent)
        try:
            R.Bot.catchup_cycle(self.bot, {"polled": True}, stale_positions if stale_positions is not None else dict(self.positions), self.prices, False)
        finally:
            time.time = _ot
        return self.sent[n0:]
    def fill(self, ex, q, px):
        self.fills.append({"exchange_id": ex, "quantity": q, "price": px})
        self.positions[ex] = self.positions.get(ex, 0.0) + q
    def net_d(self, drop=()):
        return R.risk.exposure(_UC, {e: q for e, q in self.positions.items() if e not in drop}, self.prices).net_d_notional
def _rsig(ex, race, **kw):
    return dict(_sg, exchange_id=ex, race=race, **kw)
_s1, _s2 = _rsig("1042", "Delaware Senate"), _rsig("1072", "Oklahoma Senate")
def _drive(c, n=8):
    """Run cycles 30 s apart, filling every order at its price, until nothing more is sent."""
    for i in range(n):
        o = c.run(T0 + 30 * i)
        for ex, side, q, px, k in o:
            c.fill(ex, q if side == "yes" else -q, px)
        if not o and i > 1: break
    return c
_cp1 = _drive(_Real([_s1, _s2], {_PUNT: -10196.0}))
_ncu = lambda c: sum(max(0.0, c.positions.get(e, 0.0)) * 0.5025 for e in ("1042", "1072"))
check("catchup punt: with the punt's short (about -2,650 net D) in the book, catch-up adds D only to 4,800 judged without it, not to 4,800 + 2,650 = 7,450: two 5k trades stop at 4,800 of their own",
      abs((_cp1.net_d() - _cp1.net_d(drop=(_PUNT,))) + 2650) < 60 and 4000 < _cp1.net_d(drop=(_PUNT,)) <= 4800 + 1e-6, str((_cp1.net_d(), _cp1.net_d(drop=(_PUNT,)))))
check("catchup punt: ... and the punt's timed exit still fits the cap afterwards: net D without the punt legs is within 5,000 (exit_cap_ok: |after| <= cap)",
      abs(_cp1.net_d(drop=(_PUNT,))) <= 5000.0, str(_cp1.net_d(drop=(_PUNT,))))
_cp0 = _drive(_Real([_s1, _s2], {_PUNT: -10196.0}, punt=()))
check("catchup punt (control): without the punt adjustment the same cycles take catch-up past the cap once the punt exits (>5,000), which is the bug this test must see",
      _cp0.net_d(drop=(_PUNT,)) > 5000.0, str(_cp0.net_d(drop=(_PUNT,))))
# an R-adding trade with a punt LONG D: the matching min
_cpl = _drive(_Real([dict(_s1, d=-1, bid=0.495, ask=0.50)], {_PUNT: 20000.0}, touch=(0.495, 0.50, 5000)))
_ncr = -min(0.0, _cpl.positions.get("1042", 0.0)) * 0.4975
check("catchup punt: with a punt LONG D (+5,000 net D) an R-adding trade is judged without it too: R adds stop at 4,800 below the book without the punt, not 9,800",
      _ncr <= 4800 + 1e-6 and _cpl.net_d(drop=(_PUNT,)) >= -5000.0, str((_ncr, _cpl.net_d(drop=(_PUNT,)))))
# a stale cycle-start read, other sleeves' unfilled orders and our own unfilled clips on other legs
_cr = _Real([_s1], {"1072": 6000.0})                           # the venue now: D long 6,000 @ ~50c = 3,000 net D; the cycle-start read was empty
_o = _cr.run(T0, stale_positions={})
check("catchup room: the clip is cut by a positions read made now (net D 3,000 -> room 1,800 -> 3,564 shares), not by the stale cycle-start read (empty -> 4,000)",
      len(_o) == 1 and _o[0][2] == int(1800 / 0.505), str(_o))
_pend = [{"order_id": "d1", "exchange_id": "1072", "side": "yes", "quantity": 3000, "price": 0.5, "placed_at": T0 - 30}]
_cr = _Real([_s1], {"1072": 6000.0}, pending=_pend)
_o = _cr.run(T0)
check("catchup room: a direction-sleeve D order placed 30 s ago and not filled (1,500) counts: room 4,800 - 3,000 - 1,500 = 300 -> 594 shares",
      len(_o) == 1 and _o[0][2] == int(300 / 0.505), str(_o))
_cr = _Real([_s1], {"1072": 5000.0})
_cr.cs.ctx["1072"] = {"ts": T0 - 20, "d": 1, "f_then": 0.5, "move": 0.03, "mid0": 0.5, "gap": 0.03, "kind": "slow", "px0": 0.5, "target": 9900, "ordered": 4000, "clips": 1, "last_clip": T0 - 10, "build_until": T0 + 3000}
_o = _cr.run(T0)
check("catchup room: our own unfilled clip on another leg (4,000 ordered, 0 held, 2,000) counts as pending D: net D 2,500 + 2,000 -> room 300 -> 594 shares",
      [x for x in _o if x[0] == "1042"] and [x for x in _o if x[0] == "1042"][0][2] == int(300 / 0.505), str(_o))
check("catchup room: a failed fresh positions read means no clip or entry at all", _Real([_s1], {}).run(T0, fresh_positions=lambda: None) == [])
# own quotes in the book
_bk = {"bids": [{"price": 0.50, "quantity": 5000}], "asks": [{"price": 0.505, "quantity": 5000}, {"price": 0.51, "quantity": 1000}]}
_oo = {"1042": [{"id": "m1", "side": "yes", "action": "sell", "priceLimit": 0.505, "quantity": 5000}]}
_cr = _Real([_s1], {}, book=_bk, open_orders=_oo)
_o = _cr.run(T0)
check("catchup own quotes: our MM's 5,000 ask at 50.5c is not liquidity: the clip sizes off the next level (0.51 x 1,000 -> 800 shares at 51c), not 4,000 at 50.5c that would rest unfilled",
      len(_o) == 1 and _o[0][2:4] == (800, 0.51), str(_o))
_cr = _Real([_s1], {}, book=_bk, open_orders={})
check("catchup own quotes (control): with no own order the same book gives 4,000 at 50.5c", _cr.run(T0)[0][2:4] == (4000, 0.505))
_bkx = {"bids": [{"price": 0.53, "quantity": 5000}, {"price": 0.51, "quantity": 5000}], "asks": [{"price": 0.535, "quantity": 5000}]}
_oox = {"1042": [{"id": "m2", "side": "yes", "action": "buy", "priceLimit": 0.53, "quantity": 5000}]}
def _tp(oo):
    c = _Real([], {"1042": 3000.0}, book=_bkx, open_orders=oo)
    c.fills.append({"exchange_id": "1042", "quantity": 3000.0, "price": 0.50})
    c.cs.ctx["1042"] = {"ts": T0, "d": 1, "f_then": 0.50, "move": 0.03, "mid0": 0.50, "gap": 0.03, "kind": "slow"}
    c.rows.append({"exchange_id": "1042", "race": "Delaware Senate", "party": "D", "sig_mid": 0.5325, "pm": 0.55, "kalshi": 0.55, "sig_bid": 0.53, "sig_ask": 0.535})
    return c, c.run(T0 + 3600)
_c1, _o1 = _tp({}); _c2, _o2 = _tp(_oox)
check("catchup own quotes: the take-profit is confirmed on the book without our own bid: our MM's 53c bid is not a buyer, the real bid is 51c (+1c) -> held; with the 53c bid real it sells",
      len(_o1) == 1 and _o1[0][1] == "no" and _o2 == [], str((_o1, _o2)))
# the cut-off
_cr = _Real([], {"1042": 4000.0})
_cr.fills.append({"exchange_id": "1042", "quantity": 4000.0, "price": 0.50})
_cr.cs.ctx["1042"] = {"ts": OS.LAST_ENTRY - 600, "d": 1, "f_then": 0.5, "move": 0.03, "mid0": 0.5, "gap": 0.03, "kind": "slow", "px0": 0.5, "target": 9900, "ordered": 4000,
                      "clips": 1, "last_clip": OS.LAST_ENTRY - 400, "build_until": OS.LAST_ENTRY + 3000}
_cr.rows.append({"exchange_id": "1042", "race": "Delaware Senate", "party": "D", "sig_mid": 0.5025, "pm": 0.53, "kalshi": 0.53, "sig_bid": 0.50, "sig_ask": 0.505})
check("catchup last entry: a build still inside its window clips before LAST_ENTRY (2 Nov 23:30 ET) and not after it (the shock reserve is free then)",
      len(_cr.run(OS.LAST_ENTRY - 300)) == 1 and _cr.run(OS.LAST_ENTRY + 60) == [])
# an exit that timed out with no context
def _boom2(*a, **k): raise TimeoutError("timed out")
_cr = _Real([], {"1042": 3000.0})
_cr.fills.append({"exchange_id": "1042", "quantity": 3000.0, "price": 0.50})
_ok2 = _cr.bot.api.place_order; _cr.bot.api.place_order = _boom2
_cr.run(T0); _cr.bot.api.place_order = _ok2
check("catchup exit: an exit with no entry context that timed out is not re-sent every cycle: held back 90 s (side dict), then re-sent", _cr.run(T0 + 30) == [] and len(_cr.run(T0 + 100)) == 1)

# wind-down: legs of an open position and of a trade being built are exempt; the exits run under the wind-down
_wb2 = types.SimpleNamespace(churn=None, store=None, wind_down_hold_path="/nonexistent/hold.json", cheapfav_races=lambda: set(), shock_races=lambda: set(),
                             catchup_open={"4": 3000.0, "5": 0.0}, cheapfav_open={}, shock_open={}, catchup=types.SimpleNamespace(ctx={"6": {"ts": T0}}))
_er, _el = R.Bot.wind_down_exempt(_wb2)
check("wind-down: catch-up legs with an open position, and a trade with a context (being built in clips, between two fills), are exempt; a flat leg is not",
      _el.get("4") == "catch-up sweep" and _el.get("6") == "catch-up sweep" and "5" not in _el, str(_el))
check("wind-down: catch-up's own lots are not inventory to sell (wind_down_positions removes them)", R.Bot.wind_down_positions(_wb2, {"4": 3500.0}) == {"4": 500.0, "5": 0.0})
_ws = open(os.path.join(ROOT, "sigbot", "run.py")).read()
_wi = _ws.index("def catchup_cycle")
_wj = _ws.index("def cheapfav_races")
check("wind-down: catchup_cycle runs its exit loop whatever the wind-down does (no wind_down / WD.active reference inside it); entries only check kill, pause and the cap",
      "wind_down" not in _ws[_wi:_wj] and "WD." not in _ws[_wi:_wj])

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
_old_le = DC.LAST_ENTRY
DC.LAST_ENTRY = time.time() + 30 * 86400      # 31 Oct: the real clock + 25 h is past the 1 Nov entry deadline, which would fail the lockout check for the wrong reason
try:
    _lock_ok = not _ch.eligible(_HR["CA"], time.time() + 23 * 3600) and _ch.eligible(_HR["CA"], time.time() + 25 * 3600)
finally:
    DC.LAST_ENTRY = _old_le
check("hf arb: a race 9+ ticks per set under water is sold at the touch, and is not bought again for 24 h",
      _sold(_v) == [2000] and _ch.held("CA") == 0 and "CA" in _ch.last_loss and _lock_ok, str(_sold(_v)))
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
      "self.arb.protected = lambda: set(_prot()) | self.cheapfav_races()" in _rs4 and _rs4.count("in cheapfav_races_of(self) or ") >= 2
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
      and "fills_count, max(0.0, spendable_cash(self)), cycle_id" in _cfs and "int(max(0.0, spendable_cash(self)) / max(px, 0.01)))" in _cfs
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
# ---------------------------------------------------------------- shock mode (Sizhe 24 Oct: exempt from the partisan cap)
import json as _json  # noqa: E402
import tempfile as _tempfile  # noqa: E402
from sigbot.proposals import shock_mode as SHK  # noqa: E402
from sigbot.proposals.outside_sweep import CatchupSweep as _CS  # noqa: E402

_T0 = datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc).timestamp()


def _srow(ts, pm, k, mid, ex="d1", race="MT-01", party="D", spread=0.005):
    return {"ts": ts, "exchange_id": ex, "race": race, "party": party, "pm": pm, "kalshi": k, "sig_mid": mid,
            "sig_bid": round(mid - spread / 2, 4), "sig_ask": round(mid + spread / 2, 4)}


def _shock_hist(sk, ex="d1", race="MT-01", party="D", f=0.40, mid=0.39, t_end=_T0):
    """45 min of flat history (one row per 2 min) ending 15 min before t_end."""
    sk.push([_srow(t, f, f, mid, ex, race, party) for t in range(int(t_end - 50 * 60), int(t_end - 14 * 60), 120)])


sk = SHK.ShockMode()
_shock_hist(sk)
_sg = sk.signals([_srow(_T0, 0.48, 0.47, 0.39)], _T0, set())
check("shock: Polymarket +8, Kalshi +7, SIG flat -> one buy-YES signal on that leg", len(_sg) == 1 and _sg[0]["d"] == 1
      and abs(_sg[0]["move"] - 0.075) < 1e-9, str(_sg))
check("shock: no signal on a 4-pt move (catch-up territory)", sk.signals([_srow(_T0, 0.44, 0.44, 0.39)], _T0, set()) == [])
check("shock: no signal when only Polymarket jumped (SIG ignores single-venue moves)",
      sk.signals([_srow(_T0, 0.50, 0.415, 0.39)], _T0, set()) == [])
check("shock: no signal when Polymarket and Kalshi disagree by > 4 pts", sk.signals([_srow(_T0, 0.53, 0.47, 0.39)], _T0, set()) == [])
check("shock: no signal when SIG already copied a third of the move", sk.signals([_srow(_T0, 0.48, 0.48, 0.42)], _T0, set()) == [])
sk2 = SHK.ShockMode()
_shock_hist(sk2, race="Alaska Governor")
check("shock: Alaska is excluded (SIG never followed its moves)",
      sk2.signals([_srow(_T0, 0.48, 0.48, 0.39, race="Alaska Governor")], _T0, set()) == [])
check("shock: no signal without 45-15 min of reference history", SHK.ShockMode().signals([_srow(_T0, 0.48, 0.48, 0.39)], _T0, set()) == [])
check("shock: no entries after 2 Nov 23:30 ET", sk.signals([_srow(SHK.LAST_ENTRY + 60, 0.48, 0.48, 0.39)], SHK.LAST_ENTRY + 60, set()) == [])
check("shock: one race at a time (max_races 1)", sk.signals([_srow(_T0, 0.48, 0.47, 0.39)], _T0, {"FL-22"}) == [])
sk.last_shock["MT-01"] = _T0 - 3600
check("shock: one shock per race per 6 h", sk.signals([_srow(_T0, 0.48, 0.47, 0.39)], _T0, set()) == [])
sk.last_shock.clear()
check("shock: walk = floor((M - 2.5 pts) / 2) ticks, max 4",
      [SHK.walk_ticks(m) for m in (0.05, 0.065, 0.075, 0.08, 0.20)] == [2, 4, 4, 4, 4] and SHK.walk_ticks(0.035) == 1,
      str([SHK.walk_ticks(m) for m in (0.035, 0.05, 0.065, 0.075, 0.08, 0.20)]))
_asks = [{"price": 0.395, "quantity": 2000}, {"price": 0.40, "quantity": 5000}, {"price": 0.405, "quantity": 12000},
         {"price": 0.41, "quantity": 80000}]
_side, _px, _q = sk.plan(1, 0.05, _asks, 20000)
check("shock: a 5-pt move walks 2 ticks: buys the 19k shares up to 0.405 at a 0.405 limit", (_side, _px, _q) == ("yes", 0.405, 19000),
      str((_side, _px, _q)))
_side, _px, _q = sk.plan(1, 0.08, _asks, 20000)
check("shock: per-shock budget caps the order (8-pt move: 4-tick walk to 0.415; 20k / 0.415 = 48,192 shares)",
      _q == int(20000 / 0.415) and _px == 0.415, str((_px, _q)))
_side, _px, _q = sk.plan(-1, 0.05, [{"price": 0.60, "quantity": 3000}, {"price": 0.595, "quantity": 3000}, {"price": 0.59, "quantity": 9000}], 20000)
check("shock: a down move buys NO off the bids at 1 - (bid - walk)", (_side, _px, _q) == ("no", 0.41, 15000), str((_side, _px, _q)))
check("shock: per-shock and total caps", sk.budget_left("A", {"A": 15000.0}) == 5000.0 and sk.budget_left("B", {"A": 15000.0}) == 5000.0
      and sk.budget_left("B", {"A": 20000.0}) == 0.0)
sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 7000.0}
check("shock: exit once SIG copied 70% of the move", sk.exit_reason("d1", _srow(_T0 + 600, 0.475, 0.475, 0.443), _T0 + 600) == "SIG caught up")
check("shock: hold while SIG lags and the outside holds", sk.exit_reason("d1", _srow(_T0 + 600, 0.475, 0.475, 0.41), _T0 + 600) is None)
check("shock: hoax guard: the outside gives back half the move within 30 min -> exit",
      sk.exit_reason("d1", _srow(_T0 + 900, 0.43, 0.43, 0.39), _T0 + 900) == "outside gave back half the move")
check("shock: exit when the venues split by > 8 pts", sk.exit_reason("d1", _srow(_T0 + 600, 0.52, 0.43, 0.41), _T0 + 600) is not None)
check("shock: 60-min time stop", sk.exit_reason("d1", _srow(_T0 + 3600, 0.475, 0.475, 0.41), _T0 + 3600) == "60-min time stop")
check("shock: flatten before the punt (3 Nov 11:30 ET) whatever the state",
      sk.exit_reason("d1", _srow(SHK.FLATTEN_AT, 0.475, 0.475, 0.41), SHK.FLATTEN_AT) == "flatten before the 3 Nov punt"
      and sk.exit_reason("zz", None, SHK.FLATTEN_AT) == "flatten before the 3 Nov punt")
check("shock: flat by 12:00 ET 3 Nov (FLATTEN_AT is 11:30 ET, EST)", SHK.FLATTEN_AT == datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc).timestamp())
check("shock: a position without entry context (restart) is closed", sk.exit_reason("zz", None, _T0) is not None)
check("shock: pyramid add while the outside keeps moving and SIG lags", sk.add_move("d1", _srow(_T0 + 300, 0.50, 0.50, 0.40), _T0 + 300) is not None)
check("shock: no add once SIG copied half", sk.add_move("d1", _srow(_T0 + 300, 0.50, 0.50, 0.45), _T0 + 300) is None)
check("shock: no add after 30 min", sk.add_move("d1", _srow(_T0 + 1900, 0.50, 0.50, 0.40), _T0 + 1900) is None)
_st = SHK.ShockMode()
_st.load(_json.loads(_json.dumps(sk.to_json())))
check("shock: restart: entry context and per-race clock survive the state file; the time stop still counts from the entry",
      _st.ctx == sk.ctx and _st.exit_reason("d1", _srow(_T0 + 3600, 0.475, 0.475, 0.41), _T0 + 3600) == "60-min time stop")
_k = SHK.ShockMode(kill_pnl=-1500.0)
check("shock: the stop: P&L -1,500 stops the sleeve (entries off, positions closed)", not _k.check_kill(-1499.0) and _k.check_kill(-1500.0)
      and _k.signals([_srow(_T0, 0.48, 0.48, 0.39)], _T0, set()) == [] and _k.exit_reason("zz", None, _T0) is not None)


class _ShockStore:
    def exec(self, sql, args=()):                     # shock_claim: tag purpose-less fills on an exchange since a time
        if sql.startswith("UPDATE fills SET purpose=?"):
            for f in self.fills:
                if f.get("purpose", "x") is None and f["exchange_id"] == args[1]:
                    f["purpose"] = args[0]

    def __init__(self, fills):
        self.fills, self.orders, self.params, self.marked = fills, [], {}, []
        self.lock = threading.RLock()

    def rows(self, sql, params=()):
        if "FROM fills" in sql:
            return [dict(f) for f in self.fills]
        if "MAX(placed_at)" in sql:
            ts = [o["t"] for o in self.orders if o["ex"] == params[0]]
            return [{"t": max(ts) if ts else None}]
        if "FROM orders" in sql:                      # orders still open, sent after params[0], of the queried purpose
            pur = "shock_exit" if "purpose='shock_exit'" in sql else "shock"
            return [{"exchange_id": o["ex"]} for o in self.orders if o["purpose"] == pur and o["t"] > params[0]]
        return []

    def record_order(self, oid, ex, mid, side, act, px, q, purpose, response=None, status=None):
        self.orders.append({"ex": ex, "side": side, "px": px, "q": q, "purpose": purpose, "t": R.time.time()})

    def get_param(self, k, default=None):
        return self.params.get(k, default)

    def set_param(self, k, v):
        self.params[k] = v

    def mark_orders(self, ids, st):
        self.marked += list(ids)


def _shock_bot(fills=(), sk=None, books=None, venue=None):
    sent, cancels = [], []
    book = books or {"bids": [{"price": 0.385, "quantity": 2000}, {"price": 0.38, "quantity": 5000}],
                     "asks": [{"price": 0.39, "quantity": 2000}, {"price": 0.395, "quantity": 5000}, {"price": 0.40, "quantity": 9000}]}
    rbook = {"bids": [{"price": 0.61, "quantity": 2000}, {"price": 0.605, "quantity": 5000}],     # the R leg, mid 0.6125
             "asks": [{"price": 0.615, "quantity": 2000}, {"price": 0.62, "quantity": 5000}]}
    bk_of = lambda ex: rbook if (ex == "r1" and not books) else book
    races = {"MT-01": types.SimpleNamespace(name="MT-01", exchange_ids=["d1", "r1"])}
    b = types.SimpleNamespace(
        shock=sk or SHK.ShockMode(), dry_run=False, store=_ShockStore(list(fills)), trade_lock=threading.Lock(),
        killed_until=0.0, feed_ok=lambda: True, strategy_kill=None, monitor_logged={}, open_orders_cache={},
        universe=types.SimpleNamespace(by_exchange={"d1": ("MT-01", "D"), "r1": ("MT-01", "R")}, races=races),
        fresh_book=lambda ex, age=None: bk_of(ex), get_book=lambda ex, n=20, client=None, max_age=None: bk_of(ex), shock_open={}, shock_last_poll=0.0,
        shock_state_path=os.path.join(_tempfile.mkdtemp(), "shock_state.json"), cfg=types.SimpleNamespace(shock_poll_sec=10.0),
        api=types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: sent.append((ex, side, q, px, key)) or {"orderId": len(sent)},
                                  cancel_all=lambda exchange_id=None: cancels.append(exchange_id), new_key=lambda p: p))
    b.shock_ledger = types.MethodType(R.Bot.shock_ledger, b)
    b.shock_clear_race = types.MethodType(R.Bot.shock_clear_race, b)
    b.shock_publish_races = types.MethodType(R.Bot.shock_publish_races, b)
    b.shock_claim = types.MethodType(R.Bot.shock_claim, b)
    b.shock_races_cache, b.shock_hold, b.shock_vpos, b.shock_vfail = frozenset(), {}, (0.0, {}), 0
    b.shock_exit_sent, b.shock_ingest_ts, b.shock_cleared, b.shock_unknown = {}, 0.0, {}, []
    b.cash = 1e6
    vp = {}
    for f in fills:                                   # the venue's own positions: the fills by default, or `venue`
        vp[f["exchange_id"]] = vp.get(f["exchange_id"], 0.0) + float(f["quantity"])
    b.venue_pos = dict(venue) if venue is not None else vp
    b.positions_map = lambda: (dict(b.venue_pos), 0.0, len(b.venue_pos))
    b.shock_venue_positions = lambda: dict(b.venue_pos)
    b.shock_ingest = lambda: True
    return b, sent, cancels


_real_time = R.time


class _FakeTime:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _tick(b, rows, t):
    R.time = _FakeTime(t)
    try:
        R.Bot.shock_tick(b, rows=rows)
    finally:
        R.time = _real_time


_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_shock_hist(_sk, ex="r1", party="R", f=0.60, mid=0.61, t_end=_T0)
_b, _sent, _cx = _shock_bot(sk=_sk)
_orig_exposure = R.risk.exposure
R.risk.exposure = lambda *a, **k: types.SimpleNamespace(net_d_notional=9000.0, long_d=9000.0, short_d=0.0)   # far past the 5,000 cap
try:
    _tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875), _srow(_T0, 0.52, 0.53, 0.6125, ex="r1", party="R")], _T0)
finally:
    R.risk.exposure = _orig_exposure
check("shock bot: enters both legs (D YES, R NO) with net D at 9,000 (> the 5,000 cap): the cap exemption",
      sorted((s[0], s[1]) for s in _sent) == [("d1", "yes"), ("r1", "no")] and all(o["purpose"] == "shock" for o in _b.store.orders),
      str(_sent))
check("shock bot: cancels our orders on both legs first and keeps the MM out until the time stop",
      sorted(_cx) == ["d1", "r1"] and set(_b.store.params.get("news_pause", {})) == {"d1", "r1"}
      and min(_b.store.params["news_pause"].values()) >= _T0 + SHK.EXIT_AFTER, str((_cx, _b.store.params)))
check("shock bot: entry keys use the shk prefix (exits shx)", all(s[4] == "shk" for s in _sent))
check("shock bot: state saved (entry context for a restart)", set(_json.load(open(_b.shock_state_path))["ctx"]) == {"d1", "r1"})
check("shock: the exemption is shock-only: catch-up still refuses a D-adding entry when net D (incl. shock positions) is past the cap",
      not _CS().can_enter(0, "D", 9000.0, 300.0) and _CS().can_enter(0, "R", 9000.0, 300.0))
_src = open(os.path.join(ROOT, "sigbot", "run.py")).read()
_body = _src[_src.index("    def shock_tick("):_src.index("    def shock_clear_race(")]
check("shock bot: shock_tick never consults the partisan cap (exempt), every other sleeve's check is unchanged",
      "portfolio_max_partisan_notional" not in _body and "blocked_directions" not in _body
      and _src.count("risk.blocked_directions(expo, self.cfg.mm.portfolio_max_partisan_notional)") == 1)

# restart mid-position: the fills show a shock position, the state file is gone -> closed at once
_fills = [{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}]
_b, _sent, _ = _shock_bot(fills=_fills)
_tick(_b, [], _T0)
check("shock bot: restart with an open position and no entry context -> exit order for the full position (NO at 1 - bid + 2 ticks)",
      _sent == [("d1", "no", 15000, 0.625, "shx")] and _b.store.orders[0]["purpose"] == "shock_exit", str(_sent))
# restart mid-position with context restored: hold, then the 60-min stop fires
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=_fills, sk=_sk)
_tick(_b, [_srow(_T0 + 600, 0.47, 0.47, 0.41)], _T0 + 600)          # the move eased to 7 pts: no add, no exit
_n_hold = len(_sent)
_tick(_b, [_srow(_T0 + 3601, 0.475, 0.475, 0.41)], _T0 + 3601)
check("shock bot: restart with context restored: holds, then the 60-min stop closes it", _n_hold == 0 and len(_sent) == 1 and _sent[0][1] == "no",
      str(_sent))
# flatten with a fake clock at 3 Nov 11:30 ET even with fresh context and a lagging SIG
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": SHK.FLATTEN_AT - 120, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=_fills, sk=_sk)
_tick(_b, [_srow(SHK.FLATTEN_AT - 60, 0.475, 0.475, 0.41)], SHK.FLATTEN_AT - 60)
_pre = len(_sent)
_tick(_b, [_srow(SHK.FLATTEN_AT, 0.475, 0.475, 0.41)], SHK.FLATTEN_AT)
check("shock bot: fake clock: holds at 11:29 ET 3 Nov, flattens at 11:30 ET", _pre == 0 and len(_sent) == 1, str(_sent))
# the stop: losses past -1,500 -> no entries, positions closed
_sk = SHK.ShockMode(kill_pnl=-1500.0)
_shock_hist(_sk, t_end=_T0)
_sk.ctx["d1"] = {"ts": _T0 - 300, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.50}], sk=_sk)   # marked at 0.3875: -1,687
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
check("shock bot: the stop fires on marked P&L, closes the position, sends no entry",
      _sk.killed and [s[1] for s in _sent] == ["no"] and all(o["purpose"] == "shock_exit" for o in _b.store.orders), str(_sent))
# caps across ticks: an add never takes the race past the per-shock cap
_sk = SHK.ShockMode(per_shock=20000.0, total=20000.0)
_shock_hist(_sk, t_end=_T0)
_sk.ctx["d1"] = {"ts": _T0 - 120, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 18800.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 47000.0, "price": 0.40}], sk=_sk)   # 18.8k sent so far
_tick(_b, [_srow(_T0, 0.50, 0.50, 0.3875)], _T0)
_add = [s for s in _sent if s[4] == "shk"]
check("shock bot: a pyramid add is capped by the notional already sent for the race (per-shock 20k)",
      len(_add) == 1 and _add[0][2] * _add[0][3] <= 1200 + 1, str(_sent))
_sk.ctx["d1"]["spent"] = 20000.0
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 50000.0, "price": 0.40}], sk=_sk)
_tick(_b, [_srow(_T0, 0.50, 0.50, 0.3875)], _T0)
check("shock bot: no add once the race is at its cap", not [s for s in _sent if s[4] == "shk"], str(_sent))
_b.shock = None
check("shock bot: switched off (shock_enabled false -> no ShockMode): a tick does nothing", R.Bot.shock_tick(_b, rows=[]) is None)
# R3 maker exit: rests at the 70% price; a stop cancels it and the next tick takes
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
check("shock R3: the maker exit for a long YES rests at the 70% price (0.39 + 0.7 x 7.5 pts = 0.4425 -> sell YES at 0.445 = buy NO 0.555)",
      _sk.maker_exit("d1", 15000.0) == ("no", 0.555), str(_sk.maker_exit("d1", 15000.0)))
check("shock R3: with the maker resting, the taker convergence exit waits for 85%",
      _sk.exit_reason("d1", _srow(_T0 + 600, 0.475, 0.475, 0.445), _T0 + 600, SHK.CONVERGE_WITH_MAKER) is None
      and _sk.exit_reason("d1", _srow(_T0 + 600, 0.475, 0.475, 0.455), _T0 + 600, SHK.CONVERGE_WITH_MAKER) == "SIG caught up")
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_b.cfg.shock_exit_maker = True
_cancelled = []
_b.api.cancel_order = lambda oid: _cancelled.append(oid) or {}
_tick(_b, [_srow(_T0 + 3300, 0.47, 0.47, 0.41)], _T0 + 3300)      # (the live 5-s loop refreshes it before its TTL)
check("shock R3 bot: places one resting maker exit (shm) for the whole position", [(s[1], s[2], s[3], s[4]) for s in _sent] == [("no", 15000, 0.555, "shm")],
      str(_sent))
_tick(_b, [_srow(_T0 + 3601, 0.47, 0.47, 0.41)], _T0 + 3601)
_n_after_stop = len(_sent)
_tick(_b, [_srow(_T0 + 3611, 0.47, 0.47, 0.41)], _T0 + 3611)
check("shock R3 bot: at the 60-min stop the maker order is cancelled first, then the next tick takes the bid (shx)",
      _cancelled == [1] and _n_after_stop == 1 and len(_sent) == 2 and _sent[1][4] == "shx", str((_cancelled, _sent)))
# review 24 Oct (Fable): a stale fills ledger must never re-send exits past the venue position
_sk = SHK.ShockMode()
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk, venue={})
for _i in range(6):
    _tick(_b, [], _T0 + 30 * _i)                       # 3 min, ledger never updates, venue shows the position closed
check("shock bot: stale ledger (15k YES) but the venue shows it closed -> no exit is ever sent", _sent == [], str(_sent))
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=SHK.ShockMode(), venue={"d1": 6000.0})
_tick(_b, [], _T0)
check("shock bot: exits are clamped to what the venue shows held (6k of a 15k ledger)", [s[2] for s in _sent] == [6000], str(_sent))
_b.venue_pos = {"d1": 6000.0}
_tick(_b, [], _T0 + 10)
_n10 = len(_sent)
_tick(_b, [], _T0 + 21)
check("shock bot: a taker exit is re-sent after its 15-s TTL (+5 s), not after 90 s", _n10 == 1 and len(_sent) == 2, str(_sent))
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": SHK.FLATTEN_AT - 100, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_tick(_b, [], SHK.FLATTEN_AT)
check("shock bot: at the 3 Nov 11:30 ET flatten the exit walks 10 ticks (NO at 1 - 0.385 + 0.05 = 0.665)", _sent and _sent[0][3] == 0.665, str(_sent))
# a maker order that filled while its cancel failed: hold, then the clamp sees the venue flat
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_b.cfg.shock_exit_maker = True
def _cancel_fails(oid):
    raise R.ApiError(409, "conflict", "order already filled")   # a 4xx: the order is gone (filled), not "retry"
_b.api.cancel_order = _cancel_fails
_tick(_b, [_srow(_T0 + 3300, 0.47, 0.47, 0.41)], _T0 + 3300)     # maker exit rests
_b.venue_pos = {}                                                 # ... and fills (venue flat; ledger still stale)
_tick(_b, [_srow(_T0 + 3601, 0.47, 0.47, 0.41)], _T0 + 3601)     # time stop: cancel fails
_tick(_b, [_srow(_T0 + 3650, 0.47, 0.47, 0.41)], _T0 + 3650)     # inside the hold
_tick(_b, [_srow(_T0 + 3720, 0.47, 0.47, 0.41)], _T0 + 3720)     # after the hold: the venue clamp says flat
check("shock R3 bot: a maker that filled while its cancel failed is never followed by a taker over-close",
      [s[4] for s in _sent] == ["shm"], str(_sent))
_b2, _, _ = _shock_bot(sk=SHK.ShockMode())
_b2.shock.ctx["x1"] = {"race": "MT-01"}
_b2.shock_publish_races()
_b2.shock.ctx.clear()
check("shock: other threads read a frozen race set published under the lock", R.Bot.shock_races(_b2) == {"MT-01"})
_sk = SHK.ShockMode()
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": -63.0, "price": 0.60}], sk=_sk, venue={"d1": 400.0})
_tick(_b, [], _T0)
_b.shock_ledger()
check("shock bot: a residual the venue netted against other inventory (ledger -63 NO, venue +400 YES) is dropped, never 'closed'",
      _sent == [] and _sk.netted == {"d1": -63.0} and _b.shock_open == {}, str((_sent, _sk.netted, _b.shock_open)))
# review 3 (Fable)
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=SHK.ShockMode())
_b.shock_venue_positions = lambda: None
for _i in range(4):
    _tick(_b, [], _T0 + 300 * _i)
check("shock bot: a failed positions read is 'unknown', never 'flat': no exit, nothing netted, the position stays ours",
      _sent == [] and _b.shock.netted == {} and _b.shock_open == {"d1": 15000.0}, str((_sent, _b.shock.netted, _b.shock_open)))
_b.shock_venue_positions = lambda: {"d1": 15000.0}
_tick(_b, [], _T0 + 1300)
check("shock bot: once the read recovers, the exit goes out for the full position", [s[2] for s in _sent] == [15000], str(_sent))
# netting re-checks after a fresh ingest: an un-ingested exit fill is seen as our fill, not netted
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=SHK.ShockMode(), venue={})
def _ingest():
    _b.store.fills.append({"exchange_id": "d1", "quantity": -15000.0, "price": 0.45})   # our exit's fill arrives
    return True
_b.shock_ingest = _ingest
_tick(_b, [], _T0)
check("shock bot: before netting, a fresh ingest finds our own exit fill: nothing netted", _b.shock.netted == {} and _sent == [],
      str((_b.shock.netted, _sent)))
_sk = SHK.ShockMode()
_sk.netted["d1"] = 15000.0
_b, _, _ = _shock_bot(fills=[], sk=_sk)
_b.shock_ledger()
check("shock: a netted entry is cleared once the leg's ledger is flat (no permanent offset for the next shock)", _sk.netted == {})
# R3 maker refreshed near its TTL is cancelled first (never two live makers)
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_b.cfg.shock_exit_maker = True
_cx2 = []
_b.api.cancel_order = lambda oid: _cx2.append(oid) or {}
_tick(_b, [_srow(_T0 + 60, 0.47, 0.47, 0.41)], _T0 + 60)
_tick(_b, [_srow(_T0 + 610, 0.47, 0.47, 0.41)], _T0 + 610)
_tick(_b, [_srow(_T0 + 620, 0.47, 0.47, 0.41)], _T0 + 620)
check("shock R3 bot: the maker refresh near its TTL cancels the old order before placing a new one", _cx2 == [1]
      and [s[4] for s in _sent] == ["shm", "shm"], str((_cx2, _sent)))
# kill switch basis: the venue's own marks
_b, _, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 40000.0, "price": 0.41}], sk=SHK.ShockMode())
check("shock: the kill switch's shock P&L uses the venue's per-share mark when given (fill 0.41, venue mark 0.41 -> 0)",
      abs(R.Bot.shock_ledger(_b, {"d1": 0.41})[0]) < 1e-6 and R.Bot.shock_ledger(_b)[0] < -100)
# review 4 (Fable)
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk, books={"bids": [{"price": 0.44, "quantity": 5000}], "asks": [{"price": 0.445, "quantity": 9000}]})
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)                  # the feed says 0.3875; the fresh book says SIG moved to 0.4425
check("shock bot: the fresh book shows SIG already moved (feed topic stale): no entry", [s for s in _sent if s[4] == "shk"] == [], str(_sent))
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_b.cfg.shock_exit_maker = True
_tries = []
def _cancel_503(oid):
    _tries.append(oid)
    raise R.ApiError(503, "unavailable", "try later")
_b.api.cancel_order = _cancel_503
_tick(_b, [_srow(_T0 + 3400, 0.47, 0.47, 0.41)], _T0 + 3400)      # maker rests (placed 200 s before the stop)
for _i in range(1, 15):                                           # 60-min stop: the cancel keeps failing for 2+ min
    _tick(_b, [_srow(_T0 + 3600 + 10 * _i, 0.47, 0.47, 0.41)], _T0 + 3600 + 10 * _i)
check("shock R3 bot: a cancel that fails with 5xx is retried every tick and no taker exit goes out meanwhile",
      len(_tries) == 14 and [s[4] for s in _sent] == ["shm"] and _sk.ctx["d1"].get("maker"), str((_tries, _sent)))
_b.api.cancel_order = lambda oid: {}
_tick(_b, [_srow(_T0 + 3800, 0.47, 0.47, 0.41)], _T0 + 3800)
_tick(_b, [_srow(_T0 + 3810, 0.47, 0.47, 0.41)], _T0 + 3810)
check("shock R3 bot: once the cancel succeeds, the taker exit follows", [s[4] for s in _sent] == ["shm", "shx"], str(_sent))
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
def _timeout(*a, **k):
    raise TimeoutError("read timed out")
_b.api.place_order = _timeout
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
check("shock bot: an entry whose outcome is unknown (timeout) counts against the budget and blocks the race",
      _sk.last_shock.get("MT-01") == _T0 and _sk.ctx.get("d1", {}).get("unknown") is True, str((_sk.last_shock, _sk.ctx)))
# review 5 (Fable)
_sk = SHK.ShockMode(per_shock=20000.0, total=20000.0)
_shock_hist(_sk, t_end=_T0)
_sk.ctx["d1"] = {"ts": _T0 - 120, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_n_try = []
def _to(*a, **k):
    _n_try.append(a)
    raise TimeoutError("read timed out")
_b.api.place_order = _to
for _i in range(6):
    _tick(_b, [_srow(_T0 + 70 * _i, 0.50, 0.50, 0.3875)], _T0 + 70 * _i)
_tried = sum(x[3] * x[4] for x in _n_try)
check("shock bot: adds whose outcome is unknown count in the race's spent: the attempts never exceed the 20k cap",
      len(_n_try) >= 1 and 6000.0 + _tried <= 20000.0 + 1 and _sk.ctx["d1"]["spent"] <= 20000.0 + 1, str((_tried, _sk.ctx["d1"])))
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_b.cfg.shock_exit_maker = True
_b.api.cancel_order = lambda oid: (_ for _ in ()).throw(R.ApiError(429, "rate_limited", "slow down"))
_tick(_b, [_srow(_T0 + 3400, 0.47, 0.47, 0.41)], _T0 + 3400)
for _i in range(1, 30):
    _tick(_b, [_srow(_T0 + 3600 + 5 * _i, 0.47, 0.47, 0.41)], _T0 + 3600 + 5 * _i)
check("shock R3 bot: a 429 on the maker cancel is retried (not 'gone'): no taker exit next to a live maker",
      [s[4] for s in _sent] == ["shm"] and _sk.ctx["d1"].get("maker"), str(_sent))
_b.open_orders_read, _b.open_orders_cache = (_T0 + 3700, True), {"d1": []}   # a successful read begun after it: gone
_tick(_b, [_srow(_T0 + 3760, 0.47, 0.47, 0.41)], _T0 + 3760)
_tick(_b, [_srow(_T0 + 3880, 0.47, 0.47, 0.41)], _T0 + 3880)
check("shock R3 bot: a maker the open-orders read no longer lists is gone: after the hold, the clamped taker exit",
      [s[4] for s in _sent] == ["shm", "shx"], str(_sent))
_side, _px, _q = SHK.ShockMode().plan(1, 0.08, [{"price": 0.20, "quantity": 500000}], 20000)
check("shock: a cheap leg is sized as if it cost 0.40 (20k on a 0.20 leg -> 50k shares, not 100k)", _q == 50000, str((_px, _q)))
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": SHK.FLATTEN_AT - 100, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_b.shock_venue_positions = lambda: None
for _i in range(8):
    _tick(_b, [], SHK.FLATTEN_AT + 5 * _i)
check("shock bot: past the flatten with positions unreadable for 30 s, the exit falls back to the ledger size",
      _sent and _sent[0][2] == 15000 and _sent[0][4] == "shx", str(_sent))
# review 6 (Opus)
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_b.cfg.shock_exit_maker = True
_b.api.cancel_order = lambda oid: (_ for _ in ()).throw(R.ApiError(503, "unavailable", "later"))
_tick(_b, [_srow(_T0 + 3400, 0.47, 0.47, 0.41)], _T0 + 3400)
_b.open_orders_read, _b.open_orders_cache = (_T0 + 3500, False), {}           # a FAILED read returns {}
_tick(_b, [_srow(_T0 + 3610, 0.47, 0.47, 0.41)], _T0 + 3610)
_b.open_orders_read, _b.open_orders_cache = (_T0 + 3390, True), {}            # a read that BEGAN before the maker was placed
_tick(_b, [_srow(_T0 + 3620, 0.47, 0.47, 0.41)], _T0 + 3620)
check("shock R3 bot: a failed or earlier open-orders read never makes a live maker look gone (no taker beside it)",
      [s[4] for s in _sent] == ["shm"] and _sk.ctx["d1"].get("maker"), str(_sent))
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
_n5 = []
_b.api.place_order = lambda *a, **k: _n5.append(a) or (_ for _ in ()).throw(R.ApiError(504, "gateway_timeout", "upstream"))
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
_tick(_b, [_srow(_T0 + 70, 0.48, 0.47, 0.3875)], _T0 + 70)
check("shock bot: a 5xx on an entry is an unknown outcome: budget counted, race blocked, never re-sent",
      len(_n5) == 1 and _sk.ctx.get("d1", {}).get("unknown") is True and _sk.last_shock.get("MT-01") == _T0, str((_n5, _sk.ctx)))
_sk = SHK.ShockMode()
_sk.ctx["d1"] = {"ts": _T0, "race": "MT-01", "d": 1, "f_ref": 0.40, "s_ref": 0.39, "move": 0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "d1", "quantity": 15000.0, "price": 0.40}], sk=_sk)
_b.cfg.shock_exit_maker = True
_mk = []
_b.api.place_order = lambda *a, **k: _mk.append(a) or (_ for _ in ()).throw(R.ApiError(500, "internal", "oops"))
for _i in range(5):
    _tick(_b, [_srow(_T0 + 300 + 60 * _i, 0.47, 0.47, 0.41)], _T0 + 300 + 60 * _i)
check("shock R3 bot: a maker placement answered 5xx may be live: no second maker within its 600-s TTL", len(_mk) == 1, str(_mk))
_side, _px, _q = SHK.ShockMode().plan(1, 0.08, [{"price": 0.20, "quantity": 500000}], 20000)
_b, _sent, _ = _shock_bot(sk=SHK.ShockMode())
check("shock: spend is counted on the 0.40 floor too (50k shares at 0.20 count as 20k against the cap)",
      _q * max(_px, SHK.SHARE_PRICE_FLOOR) <= 20000 + 1 and "q * max(px, SHK.SHARE_PRICE_FLOOR)" in open(os.path.join(ROOT, "sigbot", "run.py")).read())
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_shock_hist(_sk, ex="r1", party="R", f=0.60, mid=0.61, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk, venue={"d1": -14793.0, "r1": -14793.0})      # a held sell-set: NO on both legs
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875), _srow(_T0, 0.52, 0.53, 0.6125, ex="r1", party="R")], _T0)
check("shock bot: no YES entry on a leg where an arb set holds NO (it would net and the exit would break the set); the NO leg is fine",
      [(s[0], s[1]) for s in _sent if s[4] == "shk"] == [("r1", "no")], str(_sent))
# review 7 (Opus): a partial exit fill not yet in the ledger must not size the next exit into an arb set's shares
_sk = SHK.ShockMode()
_sk.ctx["r1"] = {"ts": _T0 - 3700, "race": "MT-01", "d": -1, "f_ref": 0.60, "s_ref": 0.61, "move": -0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "r1", "quantity": -10000.0, "price": 0.40}], sk=_sk, venue={"r1": -15000.0})
_tick(_b, [], _T0)                                                   # first exit: 10k (shock 10k NO + arb 5k NO on the leg)
_b.venue_pos = {"r1": -9000.0}                                       # 6k of it filled; the main cycle has not pulled fills
def _pull():
    _b.store.fills.append({"exchange_id": "r1", "quantity": 6000.0, "price": 0.39})
    return True
_b.shock_ingest = _pull
_tick(_b, [], _T0 + 25)
check("shock bot: after a partial exit fill, the next exit is re-sized from fresh fills (4k), never into the arb's 5k NO",
      [s[2] for s in _sent] == [10000, 4000], str(_sent))
_fu_seen2 = []
_fub2 = types.SimpleNamespace(arb=types.SimpleNamespace(committed={"Rhode Island Senate": 1.0, "Ohio Senate": 1.0},
                                                        free_unwinds=lambda held, pos, cid: _fu_seen2.append(sorted(held)) or []),
                              feed_ok=lambda: True, killed_until=0.0, last_free_unwind=0.0, cfg=types.SimpleNamespace(free_unwind_scan_sec=0.0),
                              universe=types.SimpleNamespace(races={"Rhode Island Senate": types.SimpleNamespace(exchange_ids=["d1", "r1"]),
                                                                    "Ohio Senate": types.SimpleNamespace(exchange_ids=["d5", "r5"])}),
                              fresh_book=lambda ex, age=None: {"bids": [], "asks": []}, trade_lock=threading.Lock(), positions_snapshot={},
                              shock_races=lambda: {"Rhode Island Senate"})
try:
    R.ArbScanner.free_unwind_pass(types.SimpleNamespace(bot=_fub2))
except Exception:
    pass
check("shock: the scanner's free unwinds never touch a race shock mode holds (a shock leg next to a NO is not a set)",
      _fu_seen2 == [] or _fu_seen2 == [["Ohio Senate"]], str(_fu_seen2))
_rs5 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("shock: every set path skips shock races (arb.protected wrapper, scanner and cycle free unwinds, churn sell-all and entries)",
      "self.arb.protected = lambda: set(_prot2()) | self.shock_races()" in _rs5 and _rs5.count("shock_races_of(") >= 5)
# review 8 (Opus): a pull made for the other leg must not cover this leg's still-live exit
_sk = SHK.ShockMode()
_sk.ctx["r1"] = {"ts": _T0 - 3700, "race": "MT-01", "d": -1, "f_ref": 0.60, "s_ref": 0.61, "move": -0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "r1", "quantity": -10000.0, "price": 0.40}], sk=_sk, venue={"r1": -15000.0})
_tick(_b, [], _T0)                                                   # R exit 10k at T0
_b.shock_ingest_ts = _T0 + 10                                        # a pull made for the D leg at T0+10
_b.venue_pos = {"r1": -5000.0}                                       # R's exit filled in full by T0+12 (not ingested yet)
def _pull8():
    _b.store.fills.append({"exchange_id": "r1", "quantity": 10000.0, "price": 0.39})
    return True
_b.shock_ingest = _pull8
_tick(_b, [], _T0 + 21)
check("shock bot: an exit is re-sized from a fresh pull even when the other leg's pull came after it (no close of the arb's 5k)",
      [s[2] for s in _sent] == [10000], str(_sent))
_b, _sent, _ = _shock_bot(fills=[], sk=SHK.ShockMode())
_b.store.fills.append({"exchange_id": "d1", "quantity": 9000.0, "price": 0.41, "purpose": None})
_b.shock_unknown = [("d1", _T0 - 30, "shock")]
_b.store.rows = (lambda orig: (lambda sql, params=(): [f for f in _b.store.fills if f.get("purpose") in ("shock", "shock_exit")]
                                if "FROM fills" in sql else orig(sql, params)))(_b.store.rows)
_tick(_b, [], _T0)
check("shock bot: fills of an unknown-outcome order (no purpose) are claimed for shock, so its ledger and exits see them",
      _b.store.fills[-1]["purpose"] == "shock" and _b.shock_open.get("d1") == 9000.0, str((_b.store.fills, _b.shock_open)))
# the day replay (live config): an outside longshot rising to SIG's standing (overpriced) level is not a shock
_sk = SHK.ShockMode()
_shock_hist(_sk, f=0.193, mid=0.2525, t_end=_T0)
check("shock: no signal when the outside move only closes SIG's standing gap (outside 0.193 -> 0.253, SIG 0.2525)",
      _sk.signals([_srow(_T0, 0.253, 0.253, 0.2525)], _T0, set()) == [])
_sk = SHK.ShockMode()
_shock_hist(_sk, f=0.193, mid=0.2125, t_end=_T0)
_sg = _sk.signals([_srow(_T0, 0.253, 0.253, 0.2125)], _T0, set())
check("shock: a partial standing gap: signal, walk sized by the smaller of move (6 pts) and gap (4 pts)",
      len(_sg) == 1 and abs(_sg[0]["gap"] - 0.0405) < 1e-9 and SHK.walk_ticks(min(abs(_sg[0]["move"]), _sg[0]["gap"])) == 1, str(_sg))
# review 10 (Opus): an outside that fades after an add must not ratchet the stored move down
_sk = SHK.ShockMode()
_shock_hist(_sk, f=0.40, mid=0.39, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk, books={"bids": [{"price": 0.40, "quantity": 2000}], "asks": [{"price": 0.405, "quantity": 9000}]})
_tick(_b, [_srow(_T0, 0.46, 0.46, 0.39)], _T0)
_mv0 = abs(_sk.ctx.get("d1", {}).get("move", 0.0))
for _i, _f in enumerate((0.455, 0.45, 0.445, 0.44, 0.435)):
    _b.store.fills = [{"exchange_id": "d1", "quantity": 4000.0 * (_i + 1), "price": 0.405}]
    _tick(_b, [_srow(_T0 + 70 * (_i + 1), _f, _f, 0.4025)], _T0 + 70 * (_i + 1))
check("shock bot: the stored move never falls as the outside fades (no extra adds into a reversal; hoax exit intact)",
      abs(_sk.ctx["d1"]["move"]) >= _mv0 - 1e-9 and len([s for s in _sent if s[4] == "shk"]) <= 1, str((_mv0, _sk.ctx["d1"], _sent)))
check("shock bot: the hoax exit still fires once the outside gives back half of the original move",
      _sk.exit_reason("d1", _srow(_T0 + 600, 0.425, 0.425, 0.4025), _T0 + 600) == "outside gave back half the move")
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
_b.cash = 1000.0
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
_spent = sum(s[2] * s[3] for s in _sent if s[4] == "shk")
check("shock bot: entries never spend more than the free cash (1k cash -> <= 950 notional, not the 40k cap)",
      0 < _spent <= 950.0 + 1, str(_sent))
_b2, _, _ = _shock_bot(sk=SHK.ShockMode())
_b2.shock_races_cache = frozenset({"MT-01"})
check("shock: shock_active() tells the cash raise a shock is open", R.Bot.shock_active(_b2) is True
      and R.Bot.shock_active(_shock_bot(sk=SHK.ShockMode())[0]) is False)
# strategy monitor gate (rebase onto 52526c7): a paused shock sleeve sends no entries or adds, but still exits
class _KFshock:
    def paused(self, name):
        return "paused by test" if name == "shock" else None
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
_b.strategy_kill, _b.monitor_logged = _KFshock(), set()
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
check("shock bot: strategy monitor pause blocks new shock entries", [s for s in _sent if s[4] == "shk"] == [], str(_sent))
_sk = SHK.ShockMode()
_sk.ctx["r1"] = {"ts": _T0 - 3700, "race": "MT-01", "d": -1, "f_ref": 0.60, "s_ref": 0.61, "move": -0.075, "spent": 6000.0}
_b, _sent, _ = _shock_bot(fills=[{"exchange_id": "r1", "quantity": -10000.0, "price": 0.40}], sk=_sk, venue={"r1": -10000.0})
_b.strategy_kill, _b.monitor_logged = _KFshock(), set()
_tick(_b, [], _T0)
check("shock bot: ... and never gates exits (the 60-min stop still closes a paused sleeve's position)",
      [s[2] for s in _sent if s[4] in ("shx", "shm")] == [10000], str(_sent))
# cheap favourites x shock (rebase onto 161c8ab): the two sleeves never share a race; cash shock spends is gone at once
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
_b.cheapfav_races = lambda: {"MT-01"}
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
check("shock bot: no shock entry in a race where cheap favourites hold a position", [s for s in _sent if s[4] == "shk"] == [], str(_sent))
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
_b.cash = 30000.0
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
_spent = sum(s[2] * s[3] for s in _sent if s[4] == "shk")
check("shock bot: self.cash drops by what shock entries can spend, so the next tick and the cycle's sleeves don't re-count it",
      _spent > 0 and abs(_b.cash - (30000.0 - _spent)) < 1e-6, str((_b.cash, _spent)))
check("cheapfav bot: no cheap-favourite entry in a race shock mode holds (shock_races)",
      _cfbot2(shock_races=lambda: {"Rhode Island Senate"}) == [] and len(_cfbot2()) >= 1)
# cash raise x shock (rebase onto 06836fe): shock spends the reserve (all free cash); every other sleeve plans without it
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
_b.cash = 15000.0
_b.cfg = types.SimpleNamespace(shock_enabled=True, shock_reserve=15000.0)
_b.shock = _sk
_rv0, _sp0 = R.shock_reserve(_b), R.spendable_cash(_b)
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
_spent = sum(s[2] * s[3] for s in _sent if s[4] == "shk")
check("shock bot: with free cash = the 15k reserve, other sleeves see 0 spendable, shock still enters and spends <= 14,950",
      _rv0 == 15000.0 and abs(_sp0) < 1e-9 and 0 < _spent <= 14950.0 + 1, str((_rv0, _sp0, _sent)))
_sk.killed = True
check("shock: a stopped sleeve frees the reserve (shock_reserve 0)", R.shock_reserve(_b) == 0.0)
# wind-down x shock (rebase onto 734e9aa): neither trades the other's race
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
_b.exit_lock = {"MT-01": {"wind_down": _T0 - 600}}
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
check("shock bot: no shock entry in a race the wind-down (or the cash raise) clipped in the last 30 min",
      [s for s in _sent if s[4] == "shk"] == [], str(_sent))
_sk = SHK.ShockMode()
_shock_hist(_sk, t_end=_T0)
_b, _sent, _ = _shock_bot(sk=_sk)
_b.exit_lock = {"MT-01": {"wind_down": _T0 - 1900}}
_tick(_b, [_srow(_T0, 0.48, 0.47, 0.3875)], _T0)
check("shock bot: ... and enters once that lock has expired", len([s for s in _sent if s[4] == "shk"]) >= 1, str(_sent))
_wb = types.SimpleNamespace(churn=None, store=None, wind_down_hold_path="/nonexistent/hold.json", cheapfav_races=lambda: set(),
                            shock_races=lambda: {"MT-01"}, catchup_open={}, cheapfav_open={}, shock_open={"d1": 5000.0})
_er, _el = R.Bot.wind_down_exempt(_wb)
check("wind-down: a race with an open shock is exempt (never sold or cancelled), and shock's own shares are not inventory to sell",
      _er.get("MT-01") == "shock mode" and R.Bot.wind_down_positions(_wb, {"d1": 7000.0, "r1": 0.0}) == {"d1": 2000.0, "r1": 0.0},
      str((_er, R.Bot.wind_down_positions(_wb, {"d1": 7000.0}))))
_rs8 = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("cash raise: shock races and the open-shock refill hold are re-read after the raise takes trade_lock (review 14)",
      "sk_now = shock_races_of(self)\n            busy_races |= sk_now\n            excl_races |= sk_now" in _rs8)
check("shock: config default is off", R.BotConfig().shock_enabled is False if hasattr(R, "BotConfig") else True)


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
      and 'self.arb.unwind_buy_set(race.name, c["books"], q, why="wind-down", purpose="wind_down")' in _rs_m
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

# 24 Oct (3f51a2b precheck): the wind_down sim jumps the clock by hours or days. The jump is modelled as a restart: the MM
# waits for a fresh outside fair before quoting (mm_gate_ready's restart path), instead of quoting one cycle with a day-old
# fair (stale fairs never gate, by design) and getting a fill on a side the fresh fair blocks (mm_filled_on_gated_side).
_gb = types.SimpleNamespace(cfg=types.SimpleNamespace(mm=types.SimpleNamespace(xv_gate_ticks=3.0)), direction=types.SimpleNamespace(),
                            mm=types.SimpleNamespace(xv_fair={"907": (0.13, 0.13, 0.13, 1000.0)}), mm_need_xv=True)
_g1 = R.Bot.mm_gate_ready(_gb, False, now=1000.0 + 27 * 3600)            # a day-old fair, restart flag set: wait
_gb.mm.xv_fair = {"907": (0.13, 0.13, 0.13, 1000.0 + 27 * 3600 - 30)}    # a fresh poll 30 s ago: quote
_g2 = R.Bot.mm_gate_ready(_gb, False, now=1000.0 + 27 * 3600)
_sp = open(os.path.join(ROOT, "tests", "sim_pipeline.py")).read()
check("sim wind_down: a clock jump re-arms the MM's restart wait (no quote on a day-old outside fair); mm_gate_ready waits on a stale fair "
      "while the flag is set and quotes once a fresh fair is in",
      _g1 is False and _g2 is True and _gb.mm_need_xv is False
      and "                self.preload(sim)\n" in _sp and "sim.bot.mm_need_xv = True" in _sp, f"{_g1} {_g2}")

# ---------------------------------------------------------------- punt executor (BACKLOG 0000 / 0c; research_notes/punt_plan.md)
import json as _pj
import tempfile as _ptf
from sigbot.proposals import punt_executor as PE  # noqa: E402


class _PuntFake:
    """Fake venue for the executor: a fake clock, one book per leg, orders fill at once against the book up to their limit
    (YES/NO netting pays 1 a pair), every order and cancel recorded; `fail` = statuses to raise on the next places."""
    dry = False

    def __init__(self, t, books, cash=100000.0):
        self.t, self.books, self.c = t, _pj.loads(_pj.dumps(books)), cash
        self.pos, self.sent, self.cancels, self.fail, self.out, self.stamp, self.lead = {}, [], [], [], {}, 0, None
        self.abort = False

    def now(self): return self.t
    def positions(self): return dict(self.pos)
    def book(self, ex): return self.books.get(str(ex))
    def cash(self): return self.c
    def outside(self, ex, side):
        v = self.out.get(str(ex), (None, None))
        return tuple(None if x is None else (x if side == "yes" else 1 - x) for x in v)
    def outside_stamp(self, ex): return self.stamp
    def leader(self): return self.lead
    def known(self, ex): return str(ex) in self.books
    def abort_requested(self): return self.abort
    def fills_cost(self, ids): return None
    def clear_others(self, ex, keep): pass

    # venue semantics (reviews R2): an identical payload under a used key REPLAYS the stored response; a changed payload under
    # a used key is 409 CONFLICT; "lost" = placed, response lost; `down` = every place is a 503 that places nothing;
    # `terminal` = TERMINAL_ENGINE_SCOPE; `rest` = half of each order fills at once, the rest rests until its expiry + `lag`
    stored, recorded, live, overlap, down, terminal, rest, lag, cancel_fail = None, None, None, 0, False, False, False, 5.0, []
    fq_on, fq_lag, noop = False, 0.0, False
    def cancel_ours(self, ids):
        self.cancels.extend(ids)
        if self.cancel_fail:
            raise R.ApiError(self.cancel_fail.pop(0), "X", "fake cancel failure")
        self.live = [o for o in (self.live or []) if o not in ids]
    def fills_qty(self, ids):
        if not self.fq_on:
            return None
        return sum(o["fq"] for o in self.sent if o["oid"] in ids and o["t"] <= self.t - self.fq_lag)   # the store lags the venue
    def recover_order(self, ex, side, qty, price, exp, purpose):
        for o in self.sent:
            if (o["ex"], o["side"], o["qty"], o["price"], int(o["exp"])) == (str(ex), side, qty, price, int(exp)):
                self.recorded = dict(self.recorded or {}, **{o["oid"]: purpose})
                return o["oid"]
        return None

    def place(self, ex, side, qty, price, key, expires_at, purpose="punt"):
        self.calls = getattr(self, "calls", []) + [(key, ex, side, qty, price, expires_at)]
        if self.terminal:
            raise R.ApiError(409, "TERMINAL_ENGINE_SCOPE", "exchange settled")
        if self.noop:
            return None                              # 200 with orderId null: the venue's no-op
        if self.down:
            raise R.ApiError(503, "X", "fake down")
        payload = (str(ex), side, qty, price, int(expires_at))
        self.stored = self.stored or {}
        if key in self.stored:
            if self.stored[key][0] == payload:
                return self.stored[key][1]            # the real venue replays the exact stored response
            raise R.ApiError(409, "CONFLICT", "key used with another payload")
        lost = False
        if self.fail:
            st = self.fail.pop(0)
            if st != "lost":
                raise R.ApiError(*(st if isinstance(st, tuple) else (st, "X")), "fake %s" % (st,))
            lost = True                              # placed on the venue, response lost
        oid = self._fill(ex, side, qty, price, key, purpose, expires_at)
        self.stored[key] = (payload, oid)
        if lost:
            raise R.ApiError(502, "X", "fake lost response")
        return oid

    def _fill(self, ex, side, qty, price, key, purpose, expires_at):
        lx = getattr(self, "_live_exp", {})
        self.live = [o for o in (self.live or []) if lx.get(o, 0) > self.t]
        if self.live:
            self.overlap += 1                        # review B4: two live orders at once
        got = cost = 0.0
        for px, q in PE.asks_for(self.books[str(ex)], side):
            if px > price + 1e-9 or got >= qty:
                break
            n = min(q, qty - got)
            got, cost = got + n, cost + n * px
        if self.rest and got > 1:                    # half fills at once, the rest rests (and fills nothing)
            cost, got = cost / 2, float(int(got) // 2)
        held = self.pos.get(str(ex), 0.0)
        pairs = min(got, max(0.0, -held if side == "yes" else held))
        self.c += pairs - cost
        self.pos[str(ex)] = held + (got if side == "yes" else -got)
        oid = "o%d" % (len(self.sent) + 1)
        if self.rest:
            self._live_exp = dict(getattr(self, "_live_exp", {}), **{oid: expires_at + self.lag})
            self.live = (self.live or []) + [oid]
        self.sent.append(dict(ex=str(ex), side=side, qty=qty, price=price, key=key, got=got, cost=cost, purpose=purpose, t=self.t,
                              oid=oid, exp=expires_at, fq=got))
        return oid


def _pbook(asks=(), bids=()):
    return {"asks": [{"price": p, "quantity": q} for p, q in asks], "bids": [{"price": p, "quantity": q} for p, q in bids]}


_T0 = PE.ts_of("2026-11-03T17:00:00-05:00")


def _pplan(**over):
    p = {"plan_id": "T", "budget": 20000, "cap_exempt": True, "steps": [
        {"id": "s1", "kind": "buy", "exchange_id": "1007", "side": "yes", "max_price": 0.35, "max_cash": 20000, "chunk_cash": 5000,
         "start": "2026-11-03T17:30:00-05:00", "stop": "2026-11-03T19:45:00-05:00", "min_gap_sec": 20, "abort_if_outside_below": 0.2},
        {"id": "s1_sell", "kind": "sell", "of_step": "s1", "min_price": 0.85, "chunk_shares": 20000, "outside_min": 0.95,
         "outside_polls": 3, "single_venue_min": 0.97, "start": "2026-11-03T19:45:00-05:00", "stop": "2026-11-04T04:00:00-05:00",
         "min_gap_sec": 20},
        {"id": "s2", "kind": "target_buy", "after_step": "s1_sell", "min_sold_frac": 0.7, "target_min": 300000,
         "target_k_leader": 1.12, "target_fallback": 320000, "max_cash": 400000, "chunk_cash": 10000,
         "candidates": [{"exchange_id": "2001", "side": "yes", "max_price": 0.30, "min_price": 0.08},
                        {"exchange_id": "2002", "side": "yes", "max_price": 0.30, "min_price": 0.08}],
         "start": "2026-11-03T22:00:00-05:00", "stop": "2026-11-04T04:00:00-05:00", "min_gap_sec": 20}]}
    p.update(over)
    return p


def _pmk(plan, fake, sha=None, state=None):
    d = _ptf.mkdtemp(prefix="punt_t_")
    path = os.path.join(d, "plan.json")
    open(path, "w").write(_pj.dumps(plan))
    st = state or os.path.join(d, "state.json")
    return PE.PuntExecutor(path, st, PE.sha256_file(path) if sha is None else sha, fake, max_budget=150000.0, cash_floor=100.0), path, st


def _prun(ex, fake, until, step=10.0):
    while fake.t < until:
        ex.tick()
        fake.t += step


_books = {"1007": _pbook(asks=[(0.30, 20000), (0.32, 30000), (0.34, 40000), (0.40, 1e6)], bids=[(0.29, 1e5)]),
          "2001": _pbook(asks=[(0.35, 1e6)]), "2002": _pbook(asks=[(0.10, 2e6), (0.12, 2e6)])}

# refusals: hash mismatch, an invalid plan, a sell that starts before its buy stops
_f = _PuntFake(_T0, _books)
_e, _, _ = _pmk(_pplan(), _f, sha="0" * 64)
_prun(_e, _f, _T0 + 3 * 3600)
check("punt: a plan whose bytes do not hash to the approved SHA-256 is refused and sends nothing", bool(_e.refused) and not _f.sent, str(_e.refused))
_bad = _pplan()
_bad["steps"][0]["max_price"] = 1.2
_bad["steps"][1]["start"] = "2026-11-03T19:00:00-05:00"
_bad["steps"][2]["candidates"][0]["exchange_id"] = "9999"
_f = _PuntFake(_T0, _books)
_e, _, _ = _pmk(_bad, _f)
_r = _e.refused or ""
check("punt: validation refuses a max price above 0.99, a sell starting before its buy stops and an unknown exchange",
      "max_price 1.2" in _r and "starts before s1 stops" in _r and "unknown exchange '9999'" in _r and not _f.sent, _r)
_f = _PuntFake(_T0, _books)
_e, _, _ = _pmk(_pplan(budget=500000), _f)
check("punt: a budget above punt_max_budget is refused", bool(_e.refused) and "budget" in _e.refused, str(_e.refused))

# chamber plan C1 (31 Oct): a position sold by the clock on a side priced under 50c, with no outside gate
def _cplan(**sell):
    x = {"id": "x", "kind": "sell", "of_step": "b", "min_price": 0.20, "chunk_shares": 3000, "timed_exit": True,
         "start": "2026-11-03T12:00:00-05:00", "stop": "2026-11-03T15:00:00-05:00", "min_gap_sec": 60}
    x.update(sell)
    x = {k: v for k, v in x.items() if v is not None}
    return {"plan_id": "C", "budget": 2600, "cap_exempt": False, "steps": [
        {"id": "b", "kind": "buy", "exchange_id": "842", "side": "no", "max_price": 0.26, "max_cash": 2600, "chunk_cash": 900,
         "start": "2026-11-01T10:00:00-05:00", "stop": "2026-11-01T16:00:00-05:00", "min_gap_sec": 60, "abort_if_outside_below": 0.24}, x]}


_cb = {"842": _pbook(asks=[(0.75, 25000), (0.76, 1e5)], bids=[(0.745, 2000), (0.74, 10000), (0.73, 77000)])}
_cT = PE.ts_of("2026-11-01T10:00:00-05:00")
_f = _PuntFake(_cT, _cb)
_e, _, _ = _pmk(_cplan(timed_exit=None, outside_min=0.01), _f)
check("punt: an outside_min under 0.5 is still refused without timed_exit", bool(_e.refused) and "outside_min" in _e.refused, str(_e.refused))
_f = _PuntFake(_cT, _cb)
_e, _, _ = _pmk(_cplan(outside_min=0.6), _f)
check("punt: timed_exit with an outside_min is refused (one exit rule only)", bool(_e.refused) and "timed_exit" in _e.refused, str(_e.refused))
_f = _PuntFake(_cT, _cb)
_e, _, _ = _pmk(_cplan(timed_exit="yes"), _f)
check("punt: timed_exit must be exactly true", bool(_e.refused) and "timed_exit" in _e.refused, str(_e.refused))
_f = _PuntFake(_cT, _cb, cash=18300.0)
_f.pos["842"] = -856.0                       # 856 NO already held, not the plan's
_f.out["842"] = (0.72, 0.69)
_e, _, _ = _pmk(_cplan(), _f)
check("punt: the C1 plan with a timed exit is accepted", not _e.refused, str(_e.refused))
_prun(_e, _f, _cT + 1800, step=30.0)
_cn = -_f.pos["842"] - 856
_cbuys = list(_f.sent)
check("punt: C1 buys NO only, never above 26c, about 10,000 shares for the 2,600 budget, and stays under the 5,000 uncapped limit",
      9500 <= _cn <= 10300 and all(o["side"] == "no" and o["price"] <= 0.26 for o in _cbuys) and sum(o["qty"] * o["price"] for o in _cbuys) <= 2600.01,
      f"{_cn} {[(o['side'], o['qty'], o['price']) for o in _cbuys][:5]}")
_cx0 = PE.ts_of("2026-11-03T12:00:00-05:00")
_f.t = _cx0 - 600
_prun(_e, _f, _cx0 - 1, step=30.0)
check("punt: a timed exit sends nothing before its start", len(_f.sent) == len(_cbuys), str(_f.sent[len(_cbuys):][:2]))
_f.out["842"] = (None, None)                 # the outside feed down: the clock alone must still sell
_prun(_e, _f, _cx0 + 3600, step=30.0)
_cs = _f.sent[len(_cbuys):]
check("punt: a timed exit sells with the outside feed down, all of what the plan bought, in clips, and leaves the 856 NO it never bought",
      _f.pos["842"] == -856.0 and _cs and all(o["side"] == "yes" and o["qty"] <= 3000 for o in _cs), f"{_f.pos} {[(o['side'], o['qty'], o['price']) for o in _cs][:6]}")
check("punt: every timed-exit order is floored at min_price (YES limit 1 - 0.20)", all(abs(o["price"] - 0.80) < 1e-9 for o in _cs), str([o["price"] for o in _cs]))
_f = _PuntFake(_cT, _cb, cash=1000.0)         # the adapter's cash() is already net of shock's reserve
_e, _, _ = _pmk(_cplan(), _f)
_prun(_e, _f, _cT + 1800, step=30.0)
check("punt: spending stops at the cash floor, and a step short of cash says so once in its log",
      _f.c >= 100.0 - 1e-6 and sum("waiting for cash" in x[1] for x in _e.state["log"]) == 1, f"{_f.c} {_e.state['log'][-3:]}")


def _cbought(cash=18300.0):
    f = _PuntFake(_cT, _cb, cash=cash)
    f.pos["842"] = -856.0
    f.out["842"] = (0.72, 0.69)
    e, _, _ = _pmk(_cplan(), f)
    _prun(e, f, _cT + 1800, step=30.0)
    f.t = _cx0 - 60
    return e, f, len(f.sent)


_e, _f, _n = _cbought()
_f.fq_on, _f.fq_lag = True, 300.0            # fills reach the store minutes after the position falls
_prun(_e, _f, _cx0 + 3600, step=30.0)
check("punt: with fills lagging 5 minutes a timed exit never sells the 856 NO it did not buy", _f.pos["842"] == -856.0, str(_f.pos))
_e, _f, _n = _cbought()
_f.exit_cap_ok = lambda ex, side, qty, pos: False
_prun(_e, _f, _cx0 + 1800, step=30.0)
_cheld = len(_f.sent) - _n
_f.exit_cap_ok = lambda ex, side, qty, pos: True
_prun(_e, _f, _cx0 + 7200, step=30.0)
check("punt: a timed exit waits while the sale would break the partisan cap, then sells once there is room, inside its window",
      _cheld == 0 and _f.pos["842"] == -856.0 and _e.st("x")["status"] != "aborted", f"{_cheld} {_f.pos} {_e.st('x')}")
_e, _f, _n = _cbought()
_f.exit_cap_ok = lambda ex, side, qty, pos: qty <= 660
_prun(_e, _f, _cx0 + 900, step=30.0)
_cs = _f.sent[_n:]
check("punt: a clip too big for the cap is cut to what fits (never above it), not skipped", _cs and all(300 <= o["qty"] <= 660 for o in _cs), str([o["qty"] for o in _cs]))
_e, _f, _n = _cbought()
_f.exit_cap_ok = lambda ex, side, qty, pos: False
_prun(_e, _f, PE.ts_of("2026-11-03T15:00:00-05:00") + 120, step=30.0)
check("punt: an exit that cannot sell by its stop records what it left unsold", _e.st("x")["status"] == "done" and "unsold" in str(_e.st("x").get("why")), str(_e.st("x")))
_e, _f, _n = _cbought()
_f.books.update(_pj.loads(_pj.dumps(_books)))
_estate = _e.state_path
_cplan2 = _pmk(_pplan(), _f)[1]
_ebad = PE.PuntExecutor(_cplan2, _estate, "0" * 64, _f, max_budget=150000.0, cash_floor=100.0)
_ft0, _f.t = _f.t, PE.ts_of("2026-11-03T15:00:01-05:00")
_ebad.tick()
_f.t = _ft0
check("punt: a plan whose hash is wrong stays refused past a timed exit's stop (the wait never clears another refusal)", bool(_ebad.refused) and "hash" in _ebad.refused, str(_ebad.refused))
_e2 = PE.PuntExecutor(_cplan2, _estate, PE.sha256_file(_cplan2), _f, max_budget=150000.0, cash_floor=100.0)
_nb = len(_f.sent)
_e2.tick()
check("punt: a new plan is refused while a timed exit is open, and the old state is kept", bool(_e2.refused) and "timed exit" in _e2.refused and len(_f.sent) == _nb
      and PE.json.load(open(_estate)).get("plan_id") == "C", str(_e2.refused))
_f.t = PE.ts_of("2026-11-03T15:00:01-05:00")
_e2.tick()
check("punt: a plan refused for a timed exit starts by itself once that exit's stop has passed (a stale flag never blocks the punt)",
      not _e2.refused and _e2.state.get("plan_id") == "T", f"{_e2.refused} {_e2.state.get('plan_id')}")
_e3 = PE.PuntExecutor(_cplan2, _estate, PE.sha256_file(_cplan2), _f, max_budget=150000.0, cash_floor=100.0)
check("punt: after the stop a new plan loads at once, even if the old plan never ticked past it (bot down at 15:00)", not _e3.refused, str(_e3.refused))
_e, _f, _n = _cbought()
_f.exit_cap_ok = lambda ex, side, qty, pos: True
_e.st("b")["filled"] = float(_e.st("b")["filled"])
_f.t = _cx0
_prun(_e, _f, _cx0 + 3600, step=30.0)
_f.pos["842"] -= 50.0                        # 50 NO still to go from a part fill: under the minimum clip, but the cap is fine
_e.st("x")["sold"] = max(0.0, float(_e.st("x").get("sold") or 0.0) - 50.0); _e.st("x")["status"] = "active"; _e.st("x")["orders"] = []
_prun(_e, _f, _cx0 + 7200, step=30.0)
check("punt: a last remainder under 100 shares is still sold when the cap allows it", _f.pos["842"] == -856.0, str(_f.pos))
from sigbot.universe import Universe as _CU, Race as _CR, Leg as _CL  # noqa: E402
_cu = _CU()
_cu.races["U.S. Senate"] = _CR("U.S. Senate", {"D": _CL("D", "m1", "842", "t"), "R": _CL("R", "m2", "843", "t")})
_cu.races["Big"] = _CR("Big", {"D": _CL("D", "m3", "901", "t"), "R": _CL("R", "m4", "902", "t")})
_cu.by_exchange = {"842": ("U.S. Senate", "D"), "843": ("U.S. Senate", "R"), "901": ("Big", "D"), "902": ("Big", "R")}
_cpx = {"842": {"bestBid": 0.74, "bestAsk": 0.75, "last": 0.75}, "843": {"bestBid": 0.25, "bestAsk": 0.26, "last": 0.25},
        "901": {"bestBid": 0.49, "bestAsk": 0.51, "last": 0.5}, "902": {"bestBid": 0.49, "bestAsk": 0.51, "last": 0.5}}
_cbot = types.SimpleNamespace(universe=_cu, cfg=types.SimpleNamespace(mm=types.SimpleNamespace(portfolio_max_partisan_notional=5000.0)),
                              mm=types.SimpleNamespace(prices=_cpx))
_cbot.wind_down_partisan_ok = lambda b, a, p: R.Bot.wind_down_partisan_ok(_cbot, b, a, p)
_ca = R._PuntAdapter(_cbot)
_cpos = {"842": -10000.0, "901": 9000.0}     # net D: short 2,500 or so on the Senate, long 4,500 on Big: about +2,000
_cnd = lambda p: R.risk.exposure(_cu, p, _cpx).net_d_notional
_cafter = _cnd(dict(_cpos, **{"842": -6000.0}))
check("punt: real exit_cap_ok on real exposure: buying YES adds net D (sign), a small clip passes, a clip over 5,000 fails",
      _cafter > _cnd(_cpos) and _ca.exit_cap_ok("842", "yes", 1000, _cpos) and not _ca.exit_cap_ok("842", "yes", 10000, dict(_cpos, **{"901": 18000.0, "842": -10000.0})),
      f"{_cnd(_cpos)} {_cafter}")
_cbot.mm.prices = {}
check("punt: exit_cap_ok says no with no marks yet (after a restart)", not _ca.exit_cap_ok("842", "yes", 10, _cpos))
_cbot.mm.prices = _cpx
_e, _f, _n = _cbought()
_f.terminal = False
_rej = {"n": 0}
_place0 = _f.place


def _reject(ex, side, qty, price, key, expires_at, purpose="punt"):
    if _rej["n"] < 5:
        _rej["n"] += 1
        raise R.ApiError(400, "BAD", "fake reject")
    return _place0(ex, side, qty, price, key, expires_at, purpose)


_f.place = _reject
_prun(_e, _f, _cx0 + 7200, step=30.0)
check("punt: five rejected orders in a row never abort a timed exit: it backs off and still gets flat", _rej["n"] == 5 and _f.pos["842"] == -856.0 and _e.st("x")["status"] != "aborted",
      f"{_rej} {_f.pos} {_e.st('x').get('status')}")
_f = _PuntFake(_T0, _books, cash=15000.0)
_f.out["1007"] = (0.60, 0.60)
_p = _pplan()
_e, _, _ = _pmk(_p, _f)
_prun(_e, _f, PE.ts_of("2026-11-03T20:00:00-05:00"), step=10.0)
check("punt: an ordinary sell step keeps its outside gate (outside 60c, no sell)", not [o for o in _f.sent if "s1_sell" in o["key"]],
      str([o for o in _f.sent if "s1_sell" in o["key"]][:1]))

# the start / stop window on a fake clock; never above the max price; budget, max_cash and the cash floor
_f = _PuntFake(_T0, _books, cash=15000.0)
_e, _, _st = _pmk(_pplan(), _f)
_prun(_e, _f, PE.ts_of("2026-11-03T17:30:00-05:00") - 1)
check("punt: nothing is sent before the step's start (fake clock 17:00-17:29:59 ET)", not _f.sent, str(_f.sent[:1]))
_prun(_e, _f, PE.ts_of("2026-11-03T19:45:00-05:00") + 600)
_s1 = [o for o in _f.sent if o["key"].startswith("punt-T-s1-")]
check("punt: buys start at the window and every limit is a capped tick <= max_price 0.35",
      _s1 and min(o["t"] for o in _s1) >= PE.ts_of("2026-11-03T17:30:00-05:00") and all(o["price"] <= 0.35 for o in _s1), str(_s1[:2]))
check("punt: no buy after the stop time", all(o["t"] <= PE.ts_of("2026-11-03T19:45:00-05:00") for o in _s1))
check("punt: spend stays within free cash minus the 100 floor (15k account)", sum(o["cost"] for o in _s1) <= 15000 - 100 + 1e-6,
      "%.0f" % sum(o["cost"] for o in _s1))
check("punt: orders at least min_gap_sec and one TTL apart", all(b["t"] - a["t"] >= PE.ORDER_TTL for a, b in zip(_s1, _s1[1:])))
check("punt: buy orders are recorded with purpose punt", all(o["purpose"] == "punt" for o in _s1))
_f = _PuntFake(_T0, _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_prun(_e, _f, PE.ts_of("2026-11-03T19:50:00-05:00"))
_cost = sum(o["cost"] for o in _f.sent)
check("punt: total buy cost never exceeds the plan budget / max_cash (20k) with 1M of free cash", _cost <= 20000 + 1e-6 and _cost > 18000, "%.0f" % _cost)
_f = _PuntFake(_T0, {"1007": _pbook(asks=[(0.40, 1e6)])} | {k: v for k, v in _books.items() if k != "1007"})
_e, _, _ = _pmk(_pplan(), _f)
_prun(_e, _f, PE.ts_of("2026-11-03T19:50:00-05:00"))
check("punt: a touch above max_price is never chased (no order at all)", not _f.sent, str(_f.sent[:1]))

# idempotency: a 502 keeps the key; a restart reuses the state and never repeats a key; 3 rejections abort the step
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _pp, _st = _pmk(_pplan(), _f)
_f.fail = [502]
_e.tick(); _f.t += 10; _e.tick()
check("punt: after a 502 the same idempotency key AND the identical payload (qty, price, expiry) are resent next tick",
      [o["key"].rsplit("-", 1)[0] for o in _f.sent] == ["punt-T-s1-0"] and len(_f.calls) == 2 and _f.calls[0] == _f.calls[1], str(_f.calls))
for _ in range(12):
    _f.t += 10; _e.tick()
_e2 = PE.PuntExecutor(_pp, _st, PE.sha256_file(_pp), _f, max_budget=150000.0, cash_floor=100.0)   # restart
for _ in range(12):
    _f.t += 10; _e2.tick()
_keys = [o["key"] for o in _f.sent]
check("punt: a restart resumes from the state file with new keys only (no key sent twice)", len(_keys) == len(set(_keys)) and len(_keys) >= 3, str(_keys))
check("punt: the previous chunk is cancelled before the next one is sent (review B4)",
      all(("o%d" % k) in _f.cancels for k in range(1, len(_f.sent))), f"{_f.cancels} {len(_f.sent)}")
# review B4 + R2-4: with resting remainders and a sweep lag the previous chunk is cancelled before each new one, never two live
# orders, and the cancel does not cost a tick: chunks go out TTL+2 apart (not twice that)
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_f.rest = True
_e, _, _ = _pmk(_pplan(), _f)
_prun(_e, _f, PE.ts_of("2026-11-03T17:30:00-05:00") + 400)
_gaps = [b["t"] - a["t"] for a, b in zip(_f.sent, _f.sent[1:])]
check("punt: resting remainders + a 5 s sweep lag: never two live orders at once (review B4)", len(_f.sent) >= 5 and _f.overlap == 0,
      f"{len(_f.sent)} overlap {_f.overlap}")
check("punt: cancel + fresh positions + send in one tick: chunks are TTL+2 apart (<= 40 s), not ~64 s (review R2-4)",
      _gaps and max(_gaps) <= PE.ORDER_TTL + 12 and min(_gaps) >= PE.ORDER_TTL, str(_gaps))
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_f.rest = True
_e, _, _ = _pmk(_pplan(), _f)
_f.cancel_fail = [503, 503, 503, 503, 503, 503, 503, 503]
_prun(_e, _f, PE.ts_of("2026-11-03T17:30:00-05:00") + 70)
check("punt: while the cancel of the previous chunk keeps failing (5xx) nothing new is sent", len(_f.sent) == 1 and _f.overlap == 0, f"{len(_f.sent)} {_f.overlap}")
# review B1: the response is lost but the order was placed; the resend under the same key gets 409: counted, key advances
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.fail = ["lost"]
_e.tick(); _f.t += 10; _e.tick()
_v = _e.state["steps"]["s1"]
check("punt: a lost response + the resend of the identical payload under the used key (the venue REPLAYS it): the real order id is booked "
      "once, the key advances, no second order, no abort (review N2)",
      _v["status"] == "active" and _v["n"] == 1 and _v["orders"][0]["id"] == "o1" and _v["errors"] == 0 and not _v.get("pending")
      and len(_f.sent) == 1 and len(_f.calls) == 2, str(_v))
for _ in range(15):
    _f.t += 10; _e.tick()
check("punt: after the replay the next chunk goes out under the next key", any(o["key"].startswith("punt-T-s1-1-") for o in _f.sent), str([o["key"] for o in _f.sent]))
# review R2-6: a changed payload under a used key is a 409 CONFLICT: booked as placed (a placeholder), never a second order
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_e.tick()
_pe = _e.state["steps"]["s1"]["orders"][0]
_f.stored[_pe["key"]] = (("1007", "yes", 1, 0.3, 0), "oX")          # the key is already used by another payload
_e.state["steps"]["s1"]["n"] = 0; _e.state["steps"]["s1"]["orders"] = []; _e.state["steps"]["s1"]["last"] = 0.0
_f.t += 40; _e.tick()
_v = _e.state["steps"]["s1"]
check("punt: 409 CONFLICT (key used by another payload) is booked as a placeholder, no abort, no error counted",
      _v["status"] == "active" and _v["orders"] and _v["orders"][0]["id"] is None and _v["errors"] == 0, str(_v))
# review R2-2: a placeholder (id None) is looked up once expired and recorded with its purpose, so its fills are attributed
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.fail = ["lost"]
_f.down = False
_e.tick()                                            # placed, response lost
_f.down = True
for _ in range(3):                                   # venue down: the pending payload expires unresent
    _f.t += 10; _e.tick()
_v = _e.state["steps"]["s1"]
_ph = [o for o in _v["orders"] if o["id"] is None]
check("punt: a pending payload that expires unresent is booked as a placeholder (id None)", len(_ph) == 1 and not _v.get("pending"), str(_v))
_f.down = False
check("punt: no lookup before the placeholder's expiry + 10 s", not _f.recorded)
for _ in range(3):
    _f.t += 10; _e.tick()
check("punt: the placeholder is found by exchange, side, qty, limit and expiry, recorded with purpose punt and given its id (review R2-2)",
      _f.recorded == {"o1": "punt"} and _v["orders"][0]["id"] == "o1", f"{_f.recorded} {_v['orders']}")
_f = _PuntFake(PE.ts_of("2026-11-03T19:46:00-05:00"), _books, cash=1e6)         # a sell placeholder is recorded as punt_exit
_e, _, _ = _pmk(_pplan(), _f)
_e.state["steps"]["s1"] = dict(_e.st("s1"), status="done", filled=60000.0)
_f.pos["1007"] = 60000.0
_f.books["1007"] = _pbook(asks=[(0.92, 1e6)], bids=[(0.90, 1e6)])
_f.out = {"1007": (0.97, 0.96)}
_f.fail = ["lost"]
while not getattr(_f, "calls", None):
    _f.stamp += 1; _f.t += 10; _e.tick()
_f.down = True
for _ in range(4):
    _f.t += 10; _e.tick()
_f.down = False
for _ in range(3):
    _f.t += 10; _e.tick()
check("punt: a placeholder SELL is recorded with purpose punt_exit", _f.recorded == {"o1": "punt_exit"}, str(_f.recorded))
# review R2-6: TERMINAL_ENGINE_SCOPE (the market settled) aborts the step; it is not booked as placed
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.terminal = True
_prun(_e, _f, _f.t + 300)
_v = _e.state["steps"]["s1"]
check("punt: a TERMINAL_ENGINE_SCOPE 409 aborts the step at once: one call, no placeholder, no resend",
      _v["status"] == "aborted" and not _v["orders"] and len(_f.calls) == 1 and "terminal" in (_v.get("why") or ""), str(_v))
# a crash after the payload was saved: the restart resends exactly that payload
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _pp, _st = _pmk(_pplan(), _f)
_e.st("s1")["status"] = "active"
_e.st("s1")["base"] = 0.0
_e.st("s1")["pending"] = {"key": "punt-T-s1-0-x", "ex": "1007", "side": "yes", "qty": 100, "price": 0.3, "exp": _f.t + 30, "purpose": "punt", "what": "BUY"}
_e.save()
_e2 = PE.PuntExecutor(_pp, _st, PE.sha256_file(_pp), _f, max_budget=150000.0, cash_floor=100.0)
_f.t += 5; _e2.tick()
check("punt: a restart resends the saved pending payload unchanged", getattr(_f, "calls", [])[:1] == [("punt-T-s1-0-x", "1007", "yes", 100, 0.3, _f.t - 5 + 30)],
      str(getattr(_f, "calls", [])))
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.fail = [400, 403, 422]
for _ in range(5):
    _e.tick(); _f.t += 40
check("punt: three rejected orders in a row abort that step and nothing more is sent",
      _e.state["steps"]["s1"]["status"] == "aborted" and not _f.sent, str(_e.state["steps"]["s1"]))

# outside called against stage 1: abort the buy; the sale waits for 3 distinct both-venue polls >= 0.95 and is capped
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.out = {"1007": (0.15, 0.12)}
_e.tick()
check("punt: a buy step aborts when the outside price falls below abort_if_outside_below", _e.state["steps"]["s1"]["status"] == "aborted" and not _f.sent)
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_prun(_e, _f, PE.ts_of("2026-11-03T19:46:00-05:00"))
_held = _f.pos["1007"]
_f.books["1007"] = _pbook(asks=[(0.92, 1e6)], bids=[(0.90, 1e6)])
_f.out = {"1007": (0.97, 0.96)}
for _ in range(6):                                       # same poll stamp: one poll however many ticks
    _e.tick(); _f.t += 10
check("punt: one outside poll seen on many ticks counts once (no sale before 3 polls)", not [o for o in _f.sent if "s1_sell" in o["key"]])
for _ in range(2):
    _f.stamp += 1; _e.tick(); _f.t += 10
_sold0 = [o for o in _f.sent if "s1_sell" in o["key"]]
check("punt: the sale needs 3 polls (2 so far: none)", not _sold0)
_f.stamp += 1; _e.tick(); _f.t += 10
_sl = [o for o in _f.sent if "s1_sell" in o["key"]]
check("punt: the 3rd poll sells: buy NO at <= 0.15 (= sell YES at >= 0.85), purpose punt_exit",
      len(_sl) == 1 and _sl[0]["side"] == "no" and abs(_sl[0]["price"] - 0.15) < 1e-9 and _sl[0]["purpose"] == "punt_exit", str(_sl))
_f.pos["1007"] = 5000.0                                   # someone else's sale moved the position: never sell more than held
for _ in range(20):
    _f.stamp += 1; _e.tick(); _f.t += 40
check("punt: never sells more than the venue position (no flip to short)", _f.pos["1007"] >= -1e-9, str(_f.pos["1007"]))

# stage 2: target from the leaderboard; first candidate that reaches it (2002 at 0.10), not the first listed (2001 at 0.35)
_f = _PuntFake(PE.ts_of("2026-11-03T22:00:00-05:00"), _books, cash=40000.0)
_e, _, _ = _pmk(_pplan(), _f)
_e.state["steps"]["s1"] = dict(_e.st("s1"), status="done", filled=100000.0)
_e.state["steps"]["s1_sell"] = dict(_e.st("s1_sell"), status="done", bought=100000.0, sold=100000.0)
_f.lead = 250000.0
_prun(_e, _f, PE.ts_of("2026-11-03T23:30:00-05:00"))
_s2 = [o for o in _f.sent if "-s2-" in o["key"]]
check("punt: stage 2 target = max(target_min, 1.12 x leader) and it buys the candidate that reaches it",
      _e.state["steps"]["s2"]["target"] == 300000 and _s2 and all(o["ex"] == "2002" and o["price"] <= 0.30 for o in _s2), str(_e.state["steps"]["s2"].get("chosen")))
_f = _PuntFake(PE.ts_of("2026-11-03T22:00:00-05:00"), _books, cash=20000.0)
_e, _, _ = _pmk(_pplan(), _f)
_e.state["steps"]["s1"] = dict(_e.st("s1"), status="done", filled=1.0)
_e.state["steps"]["s1_sell"] = dict(_e.st("s1_sell"), status="done", bought=1.0, sold=1.0)
_prun(_e, _f, PE.ts_of("2026-11-04T04:10:00-05:00"))
check("punt: no candidate reaches the target (20k cash): stage 2 keeps the cash and the step closes at its stop",
      not _f.sent and _e.state["steps"]["s2"]["status"] == "done", str(_e.state["steps"]["s2"]))

# review B2: a settled stage-1 market is never read as a sale; S6: a sale that sold nothing never opens stage 2
_f = _PuntFake(PE.ts_of("2026-11-03T19:46:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_e.state["steps"]["s1"] = dict(_e.st("s1"), status="done", filled=60000.0)
_f.pos["1007"] = 60000.0
_f.settled = lambda: {"1007"}
_f.pos.pop("1007")
_prun(_e, _f, PE.ts_of("2026-11-04T04:10:00-05:00"))
check("punt: a settled (lost) stage-1 leg aborts the sale and stage 2 sends nothing",
      _e.state["steps"]["s1_sell"]["status"] == "aborted" and not _f.sent, str({k: v.get("status") for k, v in _e.state["steps"].items()}))
_f = _PuntFake(PE.ts_of("2026-11-03T19:46:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_e.state["steps"]["s1"] = dict(_e.st("s1"), status="done", filled=60000.0)
_f.pos["1007"] = 60000.0
_prun(_e, _f, PE.ts_of("2026-11-03T19:50:00-05:00"))          # the sale step starts: base 60,000, never called
_f.pos["1007"] = 0.0                                          # the leg vanishes from /positions without our sells (review N3)
_prun(_e, _f, PE.ts_of("2026-11-04T04:10:00-05:00"))
check("punt: a sale window that closes with nothing sold never opens stage 2", not _f.sent, str(_f.sent[:1]))
check("punt: a stage-1 leg that vanishes from /positions without a settled flag or fills of ours is not a sale (sold 0, stage 2 shut)",
      _e.state["steps"]["s1_sell"].get("sold", 0) == 0 and not _e.state["steps"]["s2"].get("chosen"), str(_e.state["steps"]["s1_sell"]))

# review R2-1: once stage 2 has chosen its leg, a sell step that aborts later (market settled) does not freeze it
_f = _PuntFake(PE.ts_of("2026-11-03T22:00:00-05:00"), _books, cash=40000.0)
_e, _, _ = _pmk(_pplan(), _f)
_e.state["steps"]["s1"] = dict(_e.st("s1"), status="done", filled=100000.0)
_e.state["steps"]["s1_sell"] = dict(_e.st("s1_sell"), status="done", bought=100000.0, sold=100000.0)
_f.lead = 250000.0
for _ in range(6):
    _e.tick(); _f.t += 10
_n0 = len(_f.sent)
_e.state["steps"]["s1_sell"]["status"] = "aborted"
_prun(_e, _f, PE.ts_of("2026-11-03T23:30:00-05:00"))
check("punt: stage 2 keeps buying after its sell step aborts once a candidate is chosen (review R2-1)", _n0 >= 1 and len(_f.sent) > _n0, f"{_n0} {len(_f.sent)}")

# review R2-3: the sale is what OUR punt_exit orders filled. Nothing filled (bid 0.50 < the 0.85 floor) while the whole leg drops
# out of /positions (someone else's sale, a settlement without a flag): not a sale, stage 2 stays shut
def _sale_setup(bid):
    f = _PuntFake(PE.ts_of("2026-11-03T19:46:00-05:00"), _books, cash=1e6)
    f.fq_on = True
    e, _, _ = _pmk(_pplan(), f)
    e.state["steps"]["s1"] = dict(e.st("s1"), status="done", filled=60000.0)
    f.pos["1007"] = 60000.0
    f.books["1007"] = _pbook(asks=[(0.92, 1e6)], bids=[(bid, 1e6)])
    f.out = {"1007": (0.97, 0.96)}
    return f, e
_f, _e = _sale_setup(0.50)
for _ in range(8):
    _f.stamp += 1; _e.tick(); _f.t += 40
_f.pos["1007"] = 0.0
for _ in range(10):
    _f.stamp += 1; _e.tick(); _f.t += 40
_v = _e.state["steps"]["s1_sell"]
check("punt: sell orders that fill nothing while the leg vanishes from positions: sold 0 (not the quantity sent), stage 2 shut",
      _v.get("sold", 0) == 0 and sum(o["qty"] for o in _v["orders"]) > 0 and not _e.state["steps"]["s2"].get("chosen") and not _e.state["steps"]["s2"]["orders"],
      str(_v))
_f, _e = _sale_setup(0.90)
for _ in range(8):
    _f.stamp += 1; _e.tick(); _f.t += 40
_v = _e.state["steps"]["s1_sell"]
check("punt: the sold quantity is the fills of the punt_exit orders (20,000 a chunk) and stage 2 opens only past 70% of it",
      abs(_v["sold"] - sum(o["fq"] for o in _f.sent if o["purpose"] == "punt_exit")) < 1 and _v["sold"] >= 40000, str(_v))

# review S7: a dry run never uses (or writes) params/punt_state.json
class _DryFake(_PuntFake):
    dry = True
_f = _DryFake(_T0, _books)
_d = _ptf.mkdtemp(prefix="punt_t_")
_pp = os.path.join(_d, "plan.json"); open(_pp, "w").write(_pj.dumps(_pplan()))
_e = PE.PuntExecutor(_pp, os.path.join(_d, "punt_state.json"), PE.sha256_file(_pp), _f, max_budget=150000.0)
check("punt: an executor in dry mode refuses the live state file name", bool(_e.refused) and "state file" in _e.refused, str(_e.refused))
_live_state = os.path.join(ROOT, "params", "punt_state.json")
_before = (os.path.exists(_live_state), os.path.getmtime(_live_state) if os.path.exists(_live_state) else None)
_ppl = os.path.join(_d, "p2.json"); open(_ppl, "w").write(_pj.dumps(_pplan()))
_bk = os.path.join(_d, "books.json"); open(_bk, "w").write(_pj.dumps(_books))
import io as _pio, contextlib as _pcl
with _pcl.redirect_stdout(_pio.StringIO()):
    _rc = PE.main(["--plan", _ppl, "--books", _bk, "--at", "2026-11-03T17:31:00-05:00", "--minutes", "10"])
_after = (os.path.exists(_live_state), os.path.getmtime(_live_state) if os.path.exists(_live_state) else None)
check("punt: the CLI dry run completes and never creates or touches params/punt_state.json", _rc == 0 and _before == _after, f"{_rc} {_before} {_after}")

# B3 / R2-5: the election sleeve skips every leg of a punt plan race, logging once per leg
_el_logs = []
import logging  # noqa: E402
class _H(logging.Handler):
    def emit(self, r): _el_logs.append(r.getMessage())
_h = _H(); logging.getLogger("sigbot").addHandler(_h)
_erace = types.SimpleNamespace(name="Minnesota Senate", exchange_ids=["1007", "1008"])
_ebot = types.SimpleNamespace(punt=types.SimpleNamespace(legs=lambda: {"1007"}), universe=types.SimpleNamespace(
    race_of=lambda x: _erace if x in ("1007", "1008") else None))
_eo = [{"exchangeId": "1008", "side": "yes"}, {"exchangeId": "1009", "side": "yes"}]
_kept = [R.Bot.election_filter(_ebot, _eo) for _ in range(5)]
logging.getLogger("sigbot").removeHandler(_h)
check("election: orders on any leg of a punt plan race are dropped (other races pass), logged once per leg, not once per poll",
      all(k == [_eo[1]] for k in _kept) and len([m for m in _el_logs if "ELECTION skips" in m]) == 1, str(_el_logs))
_ebot.punt = None
check("election: with no punt executor every order passes", R.Bot.election_filter(_ebot, _eo) == _eo)

# S2b: past stop_quoting_at the MM's account-wide cancel-all is exchange by exchange when punt legs rest orders
import sigbot.mm as _pmm  # noqa: E402
_cc = []
_mmb = types.SimpleNamespace(load_overrides=lambda: None, hours_to_end=lambda: -1.0, dry_run=False,
                             api=types.SimpleNamespace(cancel_all=lambda exchange_id=None: _cc.append(exchange_id)),
                             protected_fn=lambda: {"1007"})
_pmm.MarketMaker.quote_cycle(_mmb, {}, {}, {}, {"1007": [{"id": 1}], "1500": [{"id": 2}], "1600": []}, {}, 0.0, 1, 10)
check("MM past stop_quoting_at: the punt leg's resting order is not cancelled (per-exchange cancels of the others only)", _cc == ["1500"], str(_cc))
_cc.clear()
_mmb.protected_fn = lambda: set()
_pmm.MarketMaker.quote_cycle(_mmb, {}, {}, {}, {"1007": [{"id": 1}], "1500": [{"id": 2}]}, {}, 0.0, 1, 10)
check("MM past stop_quoting_at with no punt leg: one account-wide cancel-all as before", _cc == [None], str(_cc))

# S4 (outside freshness) and S10 (single attempt) on the live adapter
_ad = R._PuntAdapter(types.SimpleNamespace(direction=types.SimpleNamespace(rows=[
    {"exchange_id": "1007", "pm": 0.97, "kalshi": 0.96, "ts": time.time() - 30},
    {"exchange_id": "1008", "pm": 0.97, "kalshi": 0.96, "ts": time.time() - 900}]), dry_run=False))
check("punt: a fresh outside row is used, one older than 5 minutes is no evidence (review S4)",
      _ad.outside("1007", "yes") == (0.97, 0.96) and _ad.outside("1008", "yes") == (None, None))
_pl = []
_lk = threading.Lock()
class _PApi:
    def place_order(self, ex, side, act, q, px, expiration=None, key=None, retries=None):
        _pl.append(retries); return {"orderId": 7}
_ad = R._PuntAdapter(types.SimpleNamespace(dry_run=False, trade_lock=_lk, api=_PApi(), store=types.SimpleNamespace(record_order=lambda *a, **k: None),
                                           book=None))
_ad.place("1007", "yes", 10, 0.3, "k", time.time() + 30, "punt")
check("punt: the live adapter places with retries=0 (one attempt; no client sleeps while the trade lock is held) (review S10)", _pl == [0], str(_pl))

# review R3-1: the fill store lags the venue by 90 s; the sale is measured again until stage 2 has chosen, and a finished sale
# waits for the fills to catch up, so stage 2 opens on the full proceeds (100,000 sold) instead of freezing a lagging 60-80k
def _lag_plan():
    p = _pplan()
    p["steps"][2]["start"] = "2026-11-03T19:46:00-05:00"
    return p
_f = _PuntFake(PE.ts_of("2026-11-03T19:46:00-05:00"), _books, cash=200000.0)
_f.fq_on, _f.fq_lag = True, 90.0
_e, _, _ = _pmk(_lag_plan(), _f)
_e.state["steps"]["s1"] = dict(_e.st("s1"), status="done", filled=100000.0)
_f.pos["1007"] = 100000.0
_f.books["1007"] = _pbook(asks=[(0.92, 1e6)], bids=[(0.90, 1e6)])
_f.out = {"1007": (0.97, 0.96)}
_f.lead = 250000.0
for _ in range(60):
    _f.stamp += 1; _e.tick(); _f.t += 10
_sv, _s2 = _e.state["steps"]["s1_sell"], _e.state["steps"]["s2"]
check("punt: fills lagging 90 s: the finished sale ends measured at the full 100,000 (not frozen at 60-80k) and stage 2 opens on all of it",
      _sv["status"] == "done" and abs(_sv["sold"] - 100000) < 1 and _s2.get("chosen") and abs(_s2["chosen"]["cash"] - (100000 * 0.85 + 20000)) < 1,
      f"{_sv.get('sold')} {_sv['status']} {_s2.get('chosen')}")

# review R3-2: the stage-1 leg settles / the sell step aborts BEFORE stage 2 chooses: the measured sale stays, stage 2 opens if
# min_sold_frac was met, and stays shut when nothing was sold
def _sale_then(after, nchunks):
    f = _PuntFake(PE.ts_of("2026-11-03T19:46:00-05:00"), _books, cash=200000.0)
    f.fq_on = True
    p = _lag_plan(); p["steps"][2]["start"] = "2026-11-03T21:00:00-05:00"
    e, _, _ = _pmk(p, f)
    e.state["steps"]["s1"] = dict(e.st("s1"), status="done", filled=100000.0)
    f.pos["1007"] = 100000.0
    f.books["1007"] = _pbook(asks=[(0.92, 1e6)], bids=[(0.90, 1e6)])
    f.out = {"1007": (0.97, 0.96)}
    f.lead = 250000.0
    while len([o for o in f.sent if "s1_sell" in o["key"]]) < nchunks:
        f.stamp += 1; e.tick(); f.t += 10
    after(f)
    _prun(e, f, PE.ts_of("2026-11-03T21:30:00-05:00"))
    return f, e
def _settle(f):
    f.settled = lambda: {"1007"}
    f.pos.pop("1007", None)
_f, _e = _sale_then(_settle, 4)
_sv = _e.state["steps"]["s1_sell"]
check("punt: the leg settles after 80,000 of 100,000 were sold, before stage 2 chose: the sale stays measured and stage 2 opens (review R3-2)",
      _sv["status"] == "done" and abs(_sv["sold"] - 80000) < 1 and _e.state["steps"]["s2"].get("chosen"), f"{_sv.get('status')} {_sv.get('sold')} {_e.state['steps']['s2'].get('chosen')}")
_f, _e = _sale_then(lambda f: setattr(f, "fail", [400, 403, 422]), 4)
_sv = _e.state["steps"]["s1_sell"]
check("punt: three rejected sells after 80,000 sold abort the sell step, but stage 2 still opens on the measured sale (review R3-2)",
      _sv["status"] == "aborted" and abs(_sv["sold"] - 80000) < 1 and _e.state["steps"]["s2"].get("chosen"), f"{_sv.get('status')} {_sv.get('sold')}")
_f, _e = _sale_then(_settle, 0)
check("punt: the leg settles before anything was sold: stage 2 stays shut", _e.state["steps"]["s1_sell"]["status"] == "aborted"
      and not _e.state["steps"]["s2"].get("chosen"), str(_e.state["steps"]["s1_sell"].get("status")))

# review R3-4: answers meaning "nothing placed" never become placeholders
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.fail = [429] * 4
_t0 = _f.t
_prun(_e, _f, _t0 + 120)
_v = _e.state["steps"]["s1"]
check("punt: a burst of 429s (nothing placed): no placeholder, and the first real order goes out within 55 s of the first refusal (was ~130 s) (review R3-4)",
      not [o for o in _v["orders"] if o["id"] is None] and _f.sent and _f.sent[0]["t"] - _t0 <= 55, f"{[o['id'] for o in _v['orders']]} {_f.sent[:1]}")
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.noop = True
_prun(_e, _f, _f.t + 100)
_f.noop = False
_prun(_e, _f, _f.t + 100)
_v = _e.state["steps"]["s1"]
check("punt: a 200 with orderId null is a no-op: no placeholder, the next key follows, and trading resumes",
      not [o for o in _v["orders"] if o["id"] is None] and _f.sent and _v["errors"] == 0, str(_v["orders"][:2]))
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.fail = [502, 503, 503, 503]
_prun(_e, _f, _f.t + 45)
check("punt: a 502 (maybe placed) followed by 503s still becomes a placeholder when the payload expires",
      [o["id"] for o in _e.state["steps"]["s1"]["orders"]][:1] == [None] or bool(_f.sent), str(_e.state["steps"]["s1"]["orders"][:1]))

# review R3b-2: a 503 is "the engine could not confirm the order" (ambiguous: placeholder at expiry); only TX_CONFLICT places nothing
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.fail = [(503, "TX_CONFLICT")] * 3
_prun(_e, _f, _f.t + 35)
check("punt: 503 TX_CONFLICT (not processed) is nothing placed: no placeholder when the payload expires",
      not [o for o in _e.state["steps"]["s1"]["orders"] if o["id"] is None], str(_e.state["steps"]["s1"]["orders"]))
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.fail = [(503, "SERVICE_UNAVAILABLE")] * 3
_prun(_e, _f, _f.t + 35)
check("punt: a plain 503 SERVICE_UNAVAILABLE (engine could not confirm) is ambiguous: a placeholder when the payload expires",
      [o["id"] for o in _e.state["steps"]["s1"]["orders"]][:1] == [None], str(_e.state["steps"]["s1"]["orders"]))

# review R3-5: recovery also runs on early-return ticks (here: after an abort, every step closed)
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_f.fail = ["lost"]
_e.tick()
_f.down = True
_prun(_e, _f, _f.t + 40)
_f.down = False
_f.abort = True
_prun(_e, _f, _f.t + 40)
check("punt: a placeholder is looked up and recorded even after the executor aborted (review R3-5)", _f.recorded == {"o1": "punt"}, str(_f.recorded))

# review R3-3: the live adapter's lookup against rows in the documented Order schema, whatever order they come in
class _Pager:
    def __init__(self, rows): self.rows = rows
    def get(self, path, **p):
        i = int(p.get("cursor") or 0); n = int(p.get("limit") or 50)
        part = self.rows[i:i + n]
        more = i + n < len(self.rows)
        return {"data": part, "pagination": {"hasMore": more, "nextCursor": str(i + n) if more else None}}
def _iso(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
def _lookup(newest_first, qty_back=None):
    import sigbot.api as _api
    now = 1_800_000_000.0
    exp = now + 30
    tgt = {"id": 4242, "exchangeId": "1007", "side": "yes", "action": "buy", "quantity": qty_back or 1000, "priceLimit": 0.3, "open": False,
           "createdAt": _iso(now), "expirationDate": _iso(exp), "terminalReasonCode": "Expired", "quantityFilled": 500}
    old = [dict(tgt, id=i, priceLimit=0.31, createdAt=_iso(now - 3600 - i)) for i in range(700)]       # MM requotes, older
    rows = [tgt] + old if newest_first else old[::-1] + [tgt]
    c = _api.SuperMarketClient(api_key="x", tournament_id="t")
    c.get = _Pager(rows).get
    rec = []
    st = types.SimpleNamespace(record_order=lambda *a, **k: rec.append((a[0], a[7])))
    ad = R._PuntAdapter(types.SimpleNamespace(api=c, store=st, dry_run=False))
    return ad.recover_order("1007", "yes", 1000, 0.3, exp, "punt"), rec
check("punt: the live adapter finds a placeholder newest-first (stops paging at older orders)", _lookup(True) == (4242, [(4242, "punt")]), str(_lookup(True)))
check("punt: ... and oldest-first (700 older orders, 4 pages), recorded with its purpose", _lookup(False) == (4242, [(4242, "punt")]), str(_lookup(False)))
check("punt: ... and when the venue shrank the order to the cash (quantity 600 of 1000)", _lookup(True, 600)[0] == 4242)

# review R3-6: the cancel helper tries every id and raises the transient error at the end
_cn = []
def _cancel(i):
    _cn.append(i)
    if i == 1:
        raise R.ApiError(503, "X", "down")
_ad = R._PuntAdapter(types.SimpleNamespace(api=types.SimpleNamespace(cancel_order=_cancel), dry_run=False))
try:
    _ad.cancel_ours([1, 2, 3]); _raised = False
except R.ApiError:
    _raised = True
check("punt: cancel_ours continues past a transient failure and reports it at the end", _cn == [1, 2, 3] and _raised, str(_cn))

# abort file: our resting orders cancelled, nothing more
_f = _PuntFake(PE.ts_of("2026-11-03T17:30:00-05:00"), _books, cash=1e6)
_e, _, _ = _pmk(_pplan(), _f)
_e.tick(); _f.t += 5
_f.abort = True
_e.tick()
_n = len(_f.sent)
_f.abort = False
_prun(_e, _f, PE.ts_of("2026-11-03T19:00:00-05:00"))
check("punt: the abort file cancels our resting orders and stops the executor for good", "o1" in _f.cancels and len(_f.sent) == _n == 1,
      f"{_f.cancels} {len(_f.sent)}")

# 0c: zero-EV exits never sell punt / election legs or the shares of a held set
_prow = {"exchange_id": "1068", "fair": 0.95, "sig_bid": 0.955, "sig_ask": 0.96, "pm": 0.95, "kalshi": 0.95}
_psent = []
class _PStore:
    def rows(self, q, a=()):
        if "purpose IN ('direction','tilt')" in q:
            return [{"exchange_id": "1068", "side": "yes", "q": 1203.0}]
        return []
    def record_order(self, *a, **k): pass
_prace = types.SimpleNamespace(name="Massachusetts Senate", exchange_ids=["1068", "1069"])
_puni = types.SimpleNamespace(races={"Massachusetts Senate": _prace})
_pbot = types.SimpleNamespace(cfg=types.SimpleNamespace(exit_band=0.01), store=_PStore(), dry_run=False, open_orders_cache={}, universe=_puni,
                              api=types.SimpleNamespace(place_order=lambda ex, side, act, q, px, expiration=None, key=None: _psent.append((ex, side, q)) or {"orderId": 1},
                                                        new_key=lambda k: k))
R.Bot.zero_ev_exits(_pbot, {"1068": 1203.0, "1069": 1000.0}, [_prow])          # 1,000 buy-sets (YES on both legs) + 203 direction
check("0c: a zero-EV exit leaves a held set's shares alone (1,203 YES of which 1,000 are buy-set legs: closes 203)",
      _psent == [("1068", "no", 203)], str(_psent))
_psent.clear()
_pbot.punt_races = lambda: {"Massachusetts Senate"}
R.Bot.zero_ev_exits(_pbot, {"1068": 1203.0, "1069": 0.0}, [_prow])
check("0c: a zero-EV exit never trades a punt / election race", _psent == [], str(_psent))

_rsp = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("punt: every set / exit / quote path excludes punt races (free unwinds x2, churn x2, cheap favourites, raise, wind-down, "
      "arb.protected, MM, inside, zero-EV)",
      _rsp.count("| punt_races_of(b)") == 1 and _rsp.count("| shock_races_of(self) | punt_races_of(self)") == 1
      and _rsp.count("or rname in punt_races_of(self)") == 1 and _rsp.count("or race.name in punt_races_of(self)") == 1
      and 'c["race"] not in punt_races_of(self)' in _rsp and "pr_now = punt_races_of(self)" in _rsp
      and '"punt / election night" for r in punt_races_of(self)' in _rsp and "set(_prot3()) | punt_races_of(self)" in _rsp
      and _rsp.count("Bot.punt_legs(self)") >= 4 and "ex in punt_ex" in _rsp)
import sigbot.config as _pcfg  # noqa: E402
_live = _pj.load(open(os.path.join(ROOT, "config.json")))
check("punt: off by default in config.py; on in config.json only with a plan whose bytes match the approved hash",
      _pcfg.BotConfig.__dataclass_fields__["punt_enabled"].default is False
      and (_live.get("punt_enabled") is False or PE.sha256_file(os.path.join(ROOT, "params", "punt_plan.json")) == _live.get("punt_plan_sha256")))
check("punt: shock mode and the catch-up sweep never enter a punt race", 'g["race"] not in punt_races_of(self)' in _rsp and 's["race"] not in punt_races_of(self)' in _rsp)
check("punt: the live cash read already leaves out shock's reserve, so config keeps no second 15,000 floor",
      "- shock_reserve(self.b)" in _rsp and float(_live.get("punt_cash_floor", _pcfg.BotConfig.__dataclass_fields__["punt_cash_floor"].default)) < 1000.0)
from sigbot import strategy_stops as _pss  # noqa: E402
check("punt: strategy stops attribute punt / punt_exit fills to 'punt' and give it no P&L stop (a punt loss pauses nothing)",
      _pss.PURPOSE_LEDGER.get("punt") == "punt" and _pss.PURPOSE_LEDGER.get("punt_exit") == "punt" and "punt" not in _pss.DEFAULT_LIMITS)


# ---- gap-trade pilot (proposals/gap_pilot.py, 27 Oct): tiny, 3c, any same-terms leg, sold before the punt
from sigbot.proposals import gap_pilot as GP  # noqa: E402
_gnow = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc).timestamp()
def _gr(race, party, ex, fair, bid, ask, pm=None, k=None):
    return {"race": race, "party": party, "exchange_id": ex, "fair": fair, "sig_bid": bid, "sig_ask": ask,
            "pm": fair if pm is None else pm, "kalshi": fair if k is None else k}
_gsame = {"g1", "g2", "h1", "h2", "p1", "p2", "i1", "i2", "i3", "n1", "n2", "o1", "o2", "s1", "s2"}
_gp = GP.GapPilot(same_terms=_gsame, last_entry=_gnow + 1e6, hard_exit_ts=_gnow + 1e7)
_gpos = {"x": 0.0}
_gc = lambda rows, pos=_gpos, **kw: _gp.candidates(rows, pos, **kw)
# universe
check("gap: a slow-count state is a candidate (not only fast-count favourites), p_win under 0.80 too",
      [c["exchange_id"] for c in _gc([_gr("Arizona Governor", "D", "g1", 0.55, 0.50, 0.52), _gr("Arizona Governor", "R", "g2", 0.45, 0.47, 0.50)])] == ["g1"])
check("gap: the U.S. House chamber market is excluded even with a 14c gap",
      _gc([_gr("U.S. House", "D", "h1", 0.80, 0.64, 0.66), _gr("U.S. House", "R", "h2", 0.20, 0.34, 0.36)]) == [])
check("gap: the punt's races are excluded", _gc([_gr("Vermont Governor", "D", "p1", 0.80, 0.70, 0.72), _gr("Vermont Governor", "R", "p2", 0.20, 0.28, 0.30)]) == [])
check("gap: a leg outside the same-terms map is never traded; no map means no candidates",
      _gc([_gr("Ohio Governor", "D", "zz", 0.60, 0.50, 0.52), _gr("Ohio Governor", "R", "zz2", 0.40, 0.48, 0.50)]) == []
      and GP.GapPilot(same_terms=None).candidates([_gr("Ohio Governor", "D", "g1", 0.6, 0.5, 0.52)], {}) == [])
check("gap: a race with an Independent leg is skipped",
      _gc([_gr("Maine Senate", "D", "i1", 0.60, 0.50, 0.52), _gr("Maine Senate", "R", "i2", 0.30, 0.28, 0.30), _gr("Maine Senate", "I", "i3", 0.1, 0.1, 0.1)]) == [])
check("gap: a leg held over 4,000 shares or held by the cheap-favourites sleeve blocks its race",
      _gc([_gr("Ohio Governor", "D", "n1", 0.60, 0.50, 0.52), _gr("Ohio Governor", "R", "n2", 0.40, 0.48, 0.50)], {"n1": 4100.0}) == []
      and _gc([_gr("Ohio Governor", "D", "n1", 0.60, 0.50, 0.52), _gr("Ohio Governor", "R", "n2", 0.40, 0.48, 0.50)], _gpos, avoid_ex={"n2"}) == [])
# edge threshold
_g1 = lambda ask, fair=0.60, **kw: _gc([_gr("Ohio Governor", "D", "n1", fair, ask - 0.02, ask, **kw), _gr("Ohio Governor", "R", "n2", 1 - fair, 1 - ask - 0.02, 1 - ask)])
check("gap: edge 2.9c is rejected, 3.0c accepted (two venues)", _g1(0.571) == [] and len(_g1(0.570)) == 1)
_sv = [dict(_gr("Ohio Governor", "D", "n1", 0.60, 0.55, 0.57), kalshi=None), _gr("Ohio Governor", "R", "n2", 0.40, 0.41, 0.43)]
check("gap: single-venue fair needs +1c (3c edge rejected, 4c accepted)",
      [c for c in _gc(_sv) if c["exchange_id"] == "n1"] == []
      and len([c for c in _gc([dict(_sv[0], sig_ask=0.56, sig_bid=0.54), _sv[1]]) if c["exchange_id"] == "n1"]) == 1)
check("gap: PM and Kalshi more than 4 pts apart are skipped", [c for c in _g1(0.50, pm=0.66, k=0.58) if c["exchange_id"] == "n1"] == [])
_lg = _gc([_gr("Ohio Governor", "D", "n1", 0.10, 0.16, 0.18), _gr("Ohio Governor", "R", "n2", 0.90, 0.87, 0.89)])
check("gap: an overpriced longshot is bought on the NO side (NO at 1 - bid) and the ranking is by edge",
      [(c["side"], round(c["edge"], 3)) for c in _lg if c["exchange_id"] == "n1"] == [("no", 0.06)]
      and [c["edge"] for c in _lg] == sorted([c["edge"] for c in _lg], reverse=True))
check("gap: never against a holding on the leg, never a NO next to a NO in the race",
      _gc([_gr("Ohio Governor", "D", "n1", 0.10, 0.16, 0.18), _gr("Ohio Governor", "R", "n2", 0.90, 0.80, 0.82)], {"n1": 100.0}) == []
      or all(c["side"] != "no" for c in _gc([_gr("Ohio Governor", "D", "n1", 0.10, 0.16, 0.18), _gr("Ohio Governor", "R", "n2", 0.90, 0.80, 0.82)], {"n1": 100.0})))
check("gap: no NO when the race's other leg already holds a NO",
      all(c["exchange_id"] != "n1" or c["side"] != "no" for c in _gc([_gr("Ohio Governor", "D", "n1", 0.10, 0.16, 0.18), _gr("Ohio Governor", "R", "n2", 0.90, 0.80, 0.82)], {"n2": -50.0})))
check("gap: one route per race outcome (D-YES and R-NO are the same bet): only the better of the two is a candidate",
      (lambda c: len(c) == 1 and c[0]["exchange_id"] in ("n1", "n2"))(
          _gc([_gr("Ohio Governor", "D", "n1", 0.60, 0.53, 0.55), _gr("Ohio Governor", "R", "n2", 0.40, 0.44, 0.47)])))
check("gap: both outcomes of a race can be candidates when each is cheap (D-YES and R-YES) only if each side has its own edge",
      all(len([x for x in _gc([_gr("Ohio Governor", "D", "n1", 0.60, 0.53, 0.55), _gr("Ohio Governor", "R", "n2", 0.40, 0.44, 0.47)]) if x["win_party"] == w]) <= 1 for w in "DR"))
# the walk
_wc = {"race": "Ohio Governor", "exchange_id": "n1", "side": "yes", "party": "D", "win_party": "D", "price": 0.50, "p_win": 0.55, "edge": 0.05, "need": 0.03, "fair": 0.55}
_wp = GP.GapPilot(same_terms=_gsame, order_notional=500, leg_cap=500, budget=1500, last_entry=_gnow + 1e6)
_wo = _wp.entries([_wc], {"n1": [(0.50, 100), (0.51, 100), (0.53, 1000)]}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0)
check("gap: the walk takes levels while each marginal share keeps >= 3c (0.50, 0.51 yes; 0.53 no) at <= 80% of a level",
      len(_wo) == 1 and _wo[0]["qty"] == 160 and abs(_wo[0]["price"] - 0.51) < 1e-9 and abs(_wo[0]["notional"] - 160 * 0.51) < 1e-6 and abs(_wo[0]["avg_price"] - (80 * 0.50 + 80 * 0.51) / 160) < 1e-9, str(_wo))
check("gap: no order when the fresh book no longer offers the edge",
      _wp.entries([_wc], {"n1": [(0.53, 1000)]}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0) == [])
# budget, leg cap, partisan
_bp = GP.GapPilot(same_terms=_gsame, order_notional=150, leg_cap=300, budget=1500, last_entry=_gnow + 1e6, net_max=500)
_big = [(0.50, 100000)]
_e1 = _bp.entries([_wc], {"n1": _big}, {"n1": 250.0}, 250.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0)
check("gap: the leg cap (300) limits the order; the budget (1,500) limits the total",
      len(_e1) == 1 and _e1[0]["notional"] <= 50.0 + 1e-6 and _bp.entries([_wc], {"n1": _big}, {}, 1500.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0) == [])
check("gap: cash short of the order limits it, no cash means no order",
      _bp.entries([_wc], {"n1": _big}, {}, 0.0, 0.0, _gnow, {"x": 0.0}, 0.0, 0.0) == []
      and _bp.entries([_wc], {"n1": _big}, {}, 0.0, 60.0, _gnow, {"x": 0.0}, 0.0, 0.0)[0]["notional"] <= 60.0 + 1e-6)
_wr = dict(_wc, win_party="R", party="R")
check("gap: partisan: the pilot's own net D stays within +-500 (a D add past it is dropped, an R add that pulls back is sent)",
      _bp.entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 480.0) == []
      and len(_bp.entries([_wr], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 480.0)) == 1)
check("gap: partisan: the book keeps 1,000 headroom to the 5,000 cap in the direction of each add (D at +3,900 blocked, R fine)",
      _bp.entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 3900.0, 0.0) == []
      and len(_bp.entries([_wr], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 3900.0, 0.0)) == 1
      and _bp.entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0, pend=(3950.0, 0.0)) == [])
check("gap: pairing: with both directions open, orders alternate within the own-net band (D 150 + R 150 leaves own net ~0)",
      (lambda o: len(o) == 2 and abs(sum(x["notional"] * (1 if x["win_party"] == "D" else -1) for x in o)) < 1.0)(
          _bp.entries([_wc, dict(_wr, exchange_id="n2", race="Texas Governor")], {"n1": _big, "n2": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0)))
check("gap: a direction the bot blocked is never added to", _bp.entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0, blocked={"D"}) == [])
check("gap: no entries after the last entry time (29 Oct 16:00 ET) or when the positions read failed",
      GP.GapPilot(same_terms=_gsame).entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, GP.LAST_ENTRY + 60, {"x": 0.0}, 0.0, 0.0) == []
      and _bp.entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {}, 0.0, 0.0) == [])
check("gap: a leg is never taken past 4,000 shares", (lambda o: not o or o[0]["qty"] <= 100)(
      _bp.entries([_wc], {"n1": [(0.50, 100000)]}, {}, 0.0, 5000.0, _gnow, {"n1": 3900.0}, 0.0, 0.0)))
# exits
_xp = GP.GapPilot(same_terms=_gsame, hard_exit_ts=_gnow + 10 * 86400)
_xb = {"bids": [{"price": 0.52, "quantity": 500}], "asks": [{"price": 0.54, "quantity": 500}]}
_xrow = {"fair": 0.58}
_x = lambda q=100.0, row=_xrow, book=_xb, cost=0.50, ts=_gnow - 3600, now=_gnow: _xp.exit_reason("n1", q, row, book, cost, ts, now)
check("gap: held 1 h with the gap still open: no exit", _x() is None)
check("gap: time stop at 72 h", _x(ts=_gnow - 73 * 3600) is not None and _x(ts=_gnow - 71 * 3600) is None)
check("gap: hard exit before the wind-down", GP.GapPilot(same_terms=_gsame, hard_exit_ts=_gnow - 1).exit_reason("n1", 100.0, _xrow, _xb, 0.50, _gnow - 60, _gnow) is not None)
check("gap: stop when the exit price is 4c under cost", _x(book={"bids": [{"price": 0.45, "quantity": 500}], "asks": [{"price": 0.47, "quantity": 5}]}) is not None
      and _x(book={"bids": [{"price": 0.47, "quantity": 500}], "asks": [{"price": 0.49, "quantity": 5}]}) is None)
check("gap: gap closed or reversed (SIG within 0.5c of the fair, or above it) is sold; a NO holding reads the NO side",
      _x(book={"bids": [{"price": 0.575, "quantity": 500}], "asks": [{"price": 0.59, "quantity": 5}]}) is not None
      and _x(book={"bids": [{"price": 0.62, "quantity": 500}], "asks": [{"price": 0.64, "quantity": 5}]}) is not None
      and _x(q=-100.0, row={"fair": 0.42}, cost=0.50, book={"bids": [{"price": 0.40, "quantity": 5}], "asks": [{"price": 0.425, "quantity": 500}]}) is not None
      and _x(q=-100.0, row={"fair": 0.42}, cost=0.45, book={"bids": [{"price": 0.50, "quantity": 5}], "asks": [{"price": 0.52, "quantity": 500}]}) is None)
check("gap: the time stop and the stop need no outside fair", _xp.exit_reason("n1", 100.0, None, _xb, 0.50, _gnow - 80 * 3600, _gnow) is not None)
# wiring
_grs = open(os.path.join(ROOT, "sigbot", "run.py")).read()
_gm = _grs[_grs.index("    def _gap_trade("):_grs.index("    # ------------------------------------------------------------------ shock mode (proposals/shock_mode.py)")]
check("gap: wiring: off by default in config.py, the launch config.json carries the pilot's keys, needs the cheap-favourites sleeve",
      _pcfg.BotConfig.__dataclass_fields__["gap_enabled"].default is False and _live.get("gap_budget") == 150.0 and _live.get("gap_min_edge") == 0.03
      and 'getattr(cfg, "gap_enabled", False) and self.cheapfav is not None' in _grs)
check("gap: wiring: orders carry the cheapfav purposes (ledger, race protections, wind-down exemption); cheapfav skips the pilot's legs",
      '"cheapfav", response=resp' in _gm and '"cheapfav_exit", response=resp' in _gm and 'c["exchange_id"] not in gap_legs' in _grs)
check("gap: funding: no cash raise for the pilot (no cash need posted, no leg sold to fund it); the shock reserve stays out of reach because the cash it spends is spendable_cash",
      "post_cash_need" not in _gm and "cash_needs" not in _gm and "raise_cycle" not in _gm and "CR." not in _gm and "spendable_cash(self)" in _gm)
check("gap: wiring: strategy kill file pauses entries (gap) and exits (exit:gap); fresh positions, trade lock and partisan cap are used",
      'strategy_paused(self, "gap")' in _gm and 'strategy_paused(self, "exit:gap")' in _gm and "fresh_positions" in _gm
      and "portfolio_max_partisan_notional" in _gm and "with lock:" in _grs[_grs.index("    def gap_cycle("):_grs.index("    def _gap_trade(")])

# ---- review fixes (Opus, 28 Oct)
check("gap: review: a cheapfav pause pauses the pilot (entries and exits) so it can never trip the strategy stops' bypass rule",
      R.PAUSE_ALIASES.get("gap") == ("cheapfav",) and R.PAUSE_ALIASES.get("exit:gap") == ("exit:cheapfav",))
check("gap: review: the U.S. Senate chamber market is excluded like the House",
      _gc([_gr("U.S. Senate", "D", "s1", 0.80, 0.64, 0.66), _gr("U.S. Senate", "R", "s2", 0.20, 0.34, 0.36)]) == [])
check("gap: review: a spread of 4c or more (the stop's size) is never entered; 3c is",
      _gc([_gr("Ohio Governor", "D", "n1", 0.60, 0.52, 0.56), _gr("Ohio Governor", "R", "n2", 0.40, 0.44, 0.48)]) == []
      and len(_gc([_gr("Ohio Governor", "D", "n1", 0.60, 0.53, 0.56), _gr("Ohio Governor", "R", "n2", 0.40, 0.44, 0.47)])) == 1)
check("gap: review: a leg the pilot exited lately is on cooldown (its race is skipped)",
      _gc([_gr("Ohio Governor", "D", "n1", 0.60, 0.53, 0.55), _gr("Ohio Governor", "R", "n2", 0.40, 0.44, 0.47)], _gpos, cooldown_ex={"n1"}) == [])
check("gap: review: the stop reads the mid: a stray low bid in a thin book does not stop a leg out, a real 4c fall does",
      _x(book={"bids": [{"price": 0.20, "quantity": 5}], "asks": [{"price": 0.54, "quantity": 500}]}, cost=0.50, row=None) is None
      and _x(book={"bids": [{"price": 0.45, "quantity": 500}], "asks": [{"price": 0.47, "quantity": 5}]}, cost=0.50, row=None) is not None)
_wl = _bp.entries([_wc], {"n1": [(0.50, 100), (0.51, 100)]}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0)
check("gap: review: the order's limit is the worst level actually walked, and cash/caps count qty x limit",
      len(_wl) == 1 and abs(_wl[0]["price"] - 0.51) < 1e-9 and abs(_wl[0]["notional"] - _wl[0]["qty"] * 0.51) < 1e-6
      and _bp.entries([_wc], {"n1": [(None, 100), (0.50, 100)]}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0) == [])
check("gap: review: own pending is one-sided (pending R never makes room for D; pending D counts against D)",
      _bp.entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0, own_pend=(0.0, 450.0)) != []
      and _bp.entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 400.0, own_pend=(0.0, 450.0)) == []
      and _bp.entries([_wc], {"n1": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0, own_pend=(480.0, 0.0)) == [])
check("gap: review: wiring: label written before the order, legs kept 6 h, exits wait 300 s and resting orders, 24 h cooldown, three stops end entries, wind-down and exit locks filter races, cheapfav skips the pilot's races",
      _gm.index('self.store.set_param("gap_legs", legs)                  # the label first') < _gm.index('self.api.place_order(o["exchange_id"]')
      and "6 * 3600" in _gm and "exited_lately" in _gm and "open_orders_cache" in _gm and 'gap_exited' in _gm and 'gap_stops' in _gm and ">= 3" in _gm
      and "Bot.wind_down_owned(self, now)" in _gm and 'Bot.exit_locked(self, c["race"], "gap", now=now)' in _gm
      and 'c["race"] not in gap_races' in _grs and "status='open'" in _gm and "pend_cf" in _gm)

check("gap: review 2: one order per race per cycle (no NO+NO / YES+YES pair before the fills show)",
      len(_bp.entries([_wc, dict(_wc, exchange_id="n2", win_party="R", side="no")], {"n1": _big, "n2": _big}, {}, 0.0, 5000.0, _gnow, {"x": 0.0}, 0.0, 0.0)) == 1)
check("gap: review 2: wiring: unknown outcome / no orderId counts the notional (gap_blind) and sets the cooldown; exits wait 900 s; own quotes netted from the books; stops counted per leg; the sleeve's own reversal exits set the cooldown",
      "_gap_blind(o, now," in _gm and 'gap_blind' in _gm and "now - 900" in _gm and _gm.count("own.net(") == 2 and "set(self.store.get_param(\"gap_stops\"" in _gm
      and "FROM orders WHERE purpose='cheapfav_exit' AND placed_at > ?" in _gm)

_cl = lambda pos, **kw: _gc([_gr("Ohio Governor", "D", "n1", 0.60, 0.53, 0.55), _gr("Ohio Governor", "R", "n2", 0.40, 0.44, 0.47)], pos, **kw)
check("gap: clean races only: another sleeve's inventory (> 100 shares on any leg) keeps the pilot out (netting, and the cash raise must keep those races); the pilot's own legs are exempt",
      _cl({"n2": 150.0}) == [] and _cl({"n2": -150.0}) == [] and len(_cl({"n2": 50.0})) == 1 and len(_cl({"n1": 150.0}, own_ex={"n1"})) == 1)
check("gap: clean races: wiring passes the pilot's own legs; no entries while a shock is open or the shock reserve is short",  "own_ex=set(legs)" in _gm and 'shock_active' in _gm and "spendable_cash(self) < 0" in _gm)

check("gap: review 3: any exception (timeouts escape the API client as URLError / TimeoutError) on an entry counts it as blind; definite 4xx rejects do not; exits are stamped before the call and any exception is caught",
      "except Exception as e:" in _gm and _gm.count("except Exception as e:") >= 2 and "gap_exit_try" in _gm
      and _gm.index('self.store.set_param("gap_exit_try", tries)') < _gm.index('key=self.api.new_key("gpx")') and "(408, 409, 429)" in _gm)
check("gap: review 3: the pilot avoids races where cheapfav has a fresh entry, and cheapfav's exit loop leaves the pilot's legs alone",
      'o["purpose"] == "cheapfav" and str(o["exchange_id"]) not in legs' in _gm and "ex in gap_own" in _grs)

check("gap: review 4: an exit with unknown outcome (exception other than a definite 4xx, or no orderId) is never re-sent by itself (gap_exit_unknown, cleared only when the ledger shows the leg closed); cooldown, busy and the stop count are set before the call; the 4xx entry path re-reads the cooldown",
      "gap_exit_unknown" in _gm and "ex in unknown_exit" in _gm and "if k in open_pos" in _gm and "the block ends for good" in _gm and "outcome unknown (%s); no automatic re-send" in _gm
      and _gm.index('cool[ex] = now                                              # no re-entry on a leg the pilot is leaving') < _gm.index('key=self.api.new_key("gpx")')
      and "cool2 = " in _gm)

check("gap: sim fixes: each order gets a fresh 60 s expiry (slow writes), and the pilot spends only cash above the larger of the cheapfav and the high-frequency arb's cash floors",
      _gm.count('ttl = (datetime.now(timezone.utc) + timedelta(seconds=60))') >= 2 and 'getattr(self.cfg, "churn_cash_floor", 0.0)' in _gm)


_exps = open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("shock: each entry / exit order gets an expiry computed right before it (slow writes made a batch-wide expiry expire every order after the first)",
      'expiration=expiry_in(60), key=self.api.new_key("shk")' in _exps and 'expiration=expiry_in(15), key=self.api.new_key("shx")' in _exps
      and R.expiry_in(60) > datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") and R.expiry_in(15) < R.expiry_in(60))

# ---------------------------------------------------------------- batch-wide order expiry (sim slow_writes audit, every place_order site)
# An expiry computed ONCE before a batch of writes makes every order after the first few arrive expired when a write takes 12 s.
# Each sleeve that sends several orders per call now computes expiry_in(seconds) inside the place_order call, with the seconds it had.
import inspect as _insp  # noqa: E402


def _per_order_expiry(fn, seconds, *keys):
    """fn's source has no batch-wide `ttl` and every order key in `keys` takes expiry_in(seconds) inline in its own call."""
    s = _insp.getsource(fn)
    return ("expiration=ttl" not in s and "ttl = (datetime.now" not in s and "expiry_in(" in s
            and all(f'expiration=expiry_in({seconds}), key=self.api.new_key("{k}")' in s for k in keys))


check("expiry: cash raise: each clip's expiry is computed right before its own order (cancel writes sit between clips)",
      _per_order_expiry(R.Bot.raise_cycle, 60, "raise"))
check("expiry: catch-up: the exits and the entries each take expiry_in(60) right before their own order (one cycle sends several)",
      _per_order_expiry(R.Bot.catchup_cycle, 60, "cux") and _per_order_expiry(R.Bot._catchup_trade, 60, "cu"))
_cft = _insp.getsource(R.Bot._cheapfav_trade)
check("expiry: cheap favourites: exits and entries take expiry_in(60) in their own call; no batch-wide ttl is computed before the trade lock or passed down",
      _per_order_expiry(R.Bot._cheapfav_trade, 60, "cfx", "cf") and "ttl" not in _insp.getsource(R.Bot.cheapfav_cycle)
      and "ttl" not in str(_insp.signature(R.Bot._cheapfav_trade)))
_el = _insp.getsource(R.Bot.election_loop)
check("expiry: election night: its 15 s TTL starts inside the trade lock, per order (one batch-wide TTL left every order after the first expired)",
      _per_order_expiry(R.Bot.election_loop, "en.cfg.ttl_sec", "elec") and _el.index("with self.trade_lock:") < _el.index("expiry_in(en.cfg.ttl_sec)")
      and __import__("sigbot.proposals.election_night", fromlist=["x"]).Config().ttl_sec == 15.0)


def _lag_slow_writes(n_orders, write_s=12.0):
    """lag_cycle with n_orders exits on a fake clock where every write takes write_s seconds before the order arrives; returns
    (orders sent, orders that arrived with an expiry not after the arrival time). The expiry must come from R.expiry_in (the
    patched clock): the old batch-wide string never does."""
    clock, late, sent = [1000.0], [], []

    def place_order(ex, side, act, q, px, expiration=None, key=None):
        clock[0] += write_s
        if not (isinstance(expiration, float) and expiration > clock[0]):
            late.append(ex)
        sent.append((ex, key))
        return {"orderId": len(sent)}
    ords = [{"exchangeId": f"x{i}", "side": "yes", "price": 0.5, "quantity": 20, "reason": "exit"} for i in range(n_orders)]
    b = types.SimpleNamespace(dry_run=False, direction=types.SimpleNamespace(rows=[{"exchange_id": "x0"}], cfg=types.SimpleNamespace(per_market_cap=300.0)),
                              fresh_book=lambda ex, age=None: None, lag=types.SimpleNamespace(cycle=lambda now, rows, touch: ords, open={}),
                              api=types.SimpleNamespace(place_order=place_order, new_key=lambda p: p),
                              store=types.SimpleNamespace(record_order=lambda *a, **k: None))
    _orig_ei = R.expiry_in
    R.expiry_in = lambda s: clock[0] + s
    try:
        R.Bot.lag_cycle(b, {}, set())
    finally:
        R.expiry_in = _orig_ei
    return sent, late


_lg_sent, _lg_late = _lag_slow_writes(10)
check("expiry: lag follow: ten orders in one call with 12 s writes all arrive inside their 60 s expiry (a batch-wide expiry expired the fifth on)",
      len(_lg_sent) == 10 and _lg_late == [] and all(k == "lag" for _, k in _lg_sent) and _per_order_expiry(R.Bot.lag_cycle, 60, "lag"),
      f"sent {len(_lg_sent)}, late {_lg_late}")

check("gap: failure-path fixes (29 Oct): blind entries keep their direction and count in the own net-D band; a cleared unknown-exit mark is written back and such legs are kept out of re-entry; an exit right after an exit waits when the venue is already flat or reversed",
      "_blind_rec(v)" in _gm and 'own_pd += b["n"]' in _gm and "cooldown_ex=set(cool) | set(unknown_exit)" in _gm and "ex in recent_exit" in _gm and "now - 2700" in _gm and "filled_by_order.get(oid, 0.0) < 1" in _gm and "n = min(n, int(abs(vp)))" in _gm
      and R._blind_rec(120.0) == {"n": 120.0, "win": None} and R._blind_rec({"n": 5, "win": "R"}) == {"n": 5.0, "win": "R"})
_shk = _exps if "_exps" in dir() else open(os.path.join(ROOT, "sigbot", "run.py")).read()
check("shock: every exit stamp (taker and maker paths) and the ingest stamp are on the wall clock (one clock: a maker exit after a late pull must still force a fills pull); no batch-wide ttl/xttl left in shock_tick",
      "self.shock_exit_sent[ex] = t_send" in _shk and "self.shock_exit_sent[ex] = now" not in _shk and _shk.count("self.shock_exit_sent[ex] = time.time()") == 5 and "self.shock_hold[ex] = t_send + 20.0" in _shk and "self.shock_ingest_ts = t_pull" in _shk
      and "xttl" not in _shk[_shk.index("    def shock_tick("):_shk.index("    def shock_tick(") + 40000])
# ---------------------------------------------------------------- MM: a gated ladder pulls its resting quotes (29 Oct hole)
# sim 'day' 29 Oct, leg 872 (Nevada Governor R): the partisan block left bids only, then the outside-fair gate closed the bid
# side; build_quotes returned nothing and quote_cycle skipped the leg before its cancel, so an 1,800-s bid rested 19 min on
# the gated side and was swept (mm_filled_on_gated_side). Real build_quotes and store; the venue and the outside fairs stubbed.
from sigbot.config import MMConfig as _MMCfg  # noqa: E402


class _GVenue:
    def __init__(self):
        self.cancel_alls, self.cancels, self.batches, self.n, self.tournament_id = [], [], [], 100, "t"

    def cancel_all(self, exchange_id=None, **k):
        self.cancel_alls.append(exchange_id)
        return {}

    def cancel_order(self, order_id):
        self.cancels.append(order_id)
        return {}

    def place_batch(self, orders, **k):
        self.batches.append(list(orders))
        res = []
        for i, o in enumerate(orders):
            self.n += 1
            res.append({"index": i, "ok": True, "data": {"orderId": self.n, "open": True}})
        return {"results": res}


def _gmm(blocked=(), fair=None, dry_run=False):
    m = _MM.__new__(_MM)
    m.cfg = _MMCfg()
    m.u, m.slug, m.starting_cash, m.dry_run, m.root = _U, "s", 100000.0, dry_run, "."
    m.state, m.overrides, m.overrides_mtime, m.quoted = {}, {}, 0.0, ["872"]
    m.vol, m.blocked, m.wave, m.prices, m.meanrev_pred, m.vol5m = {}, set(blocked), False, {}, {}, {}
    m.hot_until, m.hot_anchor = {}, {}
    m.load_overrides = lambda: None
    m.hours_to_end = lambda: 100.0
    m.api = _GVenue()
    m.store = _Store(os.path.join(tempfile.mkdtemp(), "g.sqlite"))
    m.xv_fair = {} if fair is None else {"872": (fair, fair, fair, time.time() - 30)}
    return m


def _gfair(m, fair):
    m.xv_fair = {"872": (fair, fair, fair, time.time() - 30)}


_gbook = {"872": _rbk(0.235, 0.24, 2000)}       # mid 0.2375; a fair of 0.213 sits 4.9 ticks below it: xv_gate closes the bid side
_gstat = lambda m: {r["order_id"]: r["status"] for r in m.store.rows("SELECT order_id, status FROM orders")}
# control: D blocked (buying NO on the R leg adds D) leaves bids only; the gate is open (fair at the mid)
_m = _gmm(blocked={"D"}, fair=0.2375)
_s1 = _MM.quote_cycle(_m, _gbook, {}, {}, {"872": []}, {}, 50000.0, 1, 10)
_g1 = [o for b in _m.api.batches for o in b]
check("mm gate-pull: control: with D blocked the R leg quotes bids only and the gate is open",
      _s1["requoted"] == ["872"] and _g1 and all(o["side"] == "yes" for o in _g1) and _m.api.cancel_alls == [] and _m.state["872"].last_gate == (True, True),
      f"{_s1} {_g1}")
# (a) the outside fair drifts 4.9 ticks under the mid: the gate closes the bid side, the only side the partisan block allows
_oo = {"872": [{"id": i} for i in _gstat(_m)]}
_gfair(_m, 0.213)
_nb = len(_m.api.batches)
_s2 = _MM.quote_cycle(_m, _gbook, {}, {}, _oo, {}, 50000.0, 2, 10)
check("mm gate-pull (a): the gate closes the only quotable side: the resting bids are pulled with one cancel-all, nothing is posted, "
      "one write, the closed gate and the touch are recorded, a quotes row is written",
      _m.api.cancel_alls == ["872"] and len(_m.api.batches) == _nb and _s2.get("pulled") == ["872"] and _s2["requoted"] == [] and _s2["writes"] == 1
      and _m.state["872"].last_gate == (False, True) and _m.state["872"].last_touch == (0.235, 0.24)
      and set(_gstat(_m).values()) == {"cancelled"} and _m.store.last_quote("872")["cycle_id"] == 2,
      f"{_s2} {_m.api.cancel_alls} {_gstat(_m)} {_m.state['872']}")
# (e) the fast path's open-orders cache still lists the pulled orders until the next cycle's read: no second cancel
_m.force_cancel = True                                   # the fast path and the gate sweep: a list that may predate the cancel
_s3 = _MM.quote_cycle(_m, _gbook, {}, {}, _oo, {}, 50000.0, 3, 10)
_m.force_cancel = False
check("mm gate-pull (e): a stale open-orders list (the fast path's cache, the gate sweep: force_cancel set) does not repeat the cancel",
      _m.api.cancel_alls == ["872"] and not _s3.get("pulled") and _s3["writes"] == 0 and len(_m.api.batches) == _nb, f"{_s3} {_m.api.cancel_alls}")
_m5b = _gmm(blocked={"D"}, fair=0.213)
_m5b.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_MM.quote_cycle(_m5b, _gbook, {}, {}, {"872": [{"id": 11, "side": "yes"}]}, {}, 50000.0, 3, 10)
_s3b = _MM.quote_cycle(_m5b, _gbook, {}, {}, {"872": [{"id": 11, "side": "yes"}]}, {}, 50000.0, 4, 10)
check("mm gate-pull (e): a fresh venue list that still shows the order (a cancel-all that answered OK but left it resting) is trusted: pulled again",
      _m5b.api.cancel_alls == ["872", "872"] and _s3b.get("pulled") == ["872"], f"{_s3b} {_m5b.api.cancel_alls}")
# (c) the gate re-opens: a gate change needs_requote acts on by itself (orders complete, reserve unchanged), and the bids come back
_rs = _m.state["872"].last_reserve
check("mm gate-pull (c): the recorded closed gate makes the re-open a requote trigger on its own",
      _m.needs_requote("872", {"reserve": _rs, "gate": (True, True)}, 0, 5, 5) is True
      and _m.needs_requote("872", {"reserve": _rs, "gate": (False, True)}, 0, 5, 5) is False)
_gfair(_m, 0.2375)
_s4 = _MM.quote_cycle(_m, _gbook, {}, {}, {"872": []}, {}, 50000.0, 4, 10)
_g4 = _m.api.batches[-1] if len(_m.api.batches) > _nb else []
check("mm gate-pull (c): the gate re-opens: the bids are posted again at once, no cancel (nothing rests)",
      _s4["requoted"] == ["872"] and len(_m.api.batches) == _nb + 1 and _g4 and all(o["side"] == "yes" for o in _g4)
      and _m.api.cancel_alls == ["872"] and _m.state["872"].last_gate == (True, True), f"{_s4} {_g4}")
# (b) book unknown (a side missing, or no book at all): never cancel blind, nothing recorded
_m2 = _gmm(blocked={"D"}, fair=0.213)
_m2.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_m2.state["872"] = QuoteState("872", last_reserve=0.23, last_quote_ts=time.time(), fills_seen=0, last_gate=(True, True))
_s5 = _MM.quote_cycle(_m2, {"872": {"bids": [], "asks": [{"price": 0.24, "quantity": 2000}]}}, {}, {}, {"872": [{"id": 11}]}, {}, 50000.0, 5, 10)
_s6 = _MM.quote_cycle(_m2, {}, {}, {}, {"872": [{"id": 11}]}, {}, 50000.0, 6, 10)
check("mm gate-pull (b): an unknown book (a side missing, or none) never cancels: the bid rests, the state is untouched, no quotes row",
      _m2.api.cancel_alls == [] and _m2.api.cancels == [] and not _s5.get("pulled") and not _s6.get("pulled") and _s5["writes"] == _s6["writes"] == 0
      and _gstat(_m2) == {11: "open"} and _m2.state["872"].last_gate == (True, True) and _m2.store.last_quote("872") is None,
      f"{_s5} {_s6} {_gstat(_m2)}")
# (d) something else rests on the leg (a zero-EV exit, an order the store does not know): ours go order by order, the rest stays
_m3 = _gmm(blocked={"D"}, fair=0.213)
_m3.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_m3.store.record_order(12, "872", "183", "yes", "buy", 0.23, 100, "inside")
_m3.store.record_order(40, "872", "183", "no", "buy", 0.76, 100, "exit")
_s7 = _MM.quote_cycle(_m3, _gbook, {}, {}, {"872": [{"id": 11}, {"id": 12}, {"id": 40}, {"id": 99}]}, {}, 50000.0, 7, 10)
check("mm gate-pull (d): with an exit and an unknown order on the leg the MM's and the inside quoter's orders are cancelled one by one, "
      "never a cancel-all; the exit and the unknown order stay",
      _m3.api.cancel_alls == [] and _m3.api.cancels == [11, 12] and _s7.get("pulled") == ["872"] and _s7["writes"] == 2
      and _gstat(_m3) == {11: "cancelled", 12: "cancelled", 40: "open"}, f"{_s7} {_m3.api.cancels} {_gstat(_m3)}")
# (f) no write budget left: nothing is cancelled, nothing recorded, the pull happens next cycle
_m4 = _gmm(blocked={"D"}, fair=0.213)
_m4.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_s8 = _MM.quote_cycle(_m4, _gbook, {}, {}, {"872": [{"id": 11}]}, {}, 50000.0, 8, 0)
_st8 = "872" in _m4.state
_s9 = _MM.quote_cycle(_m4, _gbook, {}, {}, {"872": [{"id": 11}]}, {}, 50000.0, 9, 1)
check("mm gate-pull (f): with no write budget the pull waits (counted as skipped_budget, state not recorded); it is done next cycle",
      _s8["skipped_budget"] == 1 and not _s8.get("pulled") and not _st8 and _s9.get("pulled") == ["872"] and _m4.api.cancel_alls == ["872"]
      and _m4.state["872"].last_gate == (False, True), f"{_s8} {_s9} {_m4.api.cancel_alls}")
# a cancel-all that fails (any exception: the API client re-raises URLError / ConnectionError after its retries) is logged,
# counted as a write, records no state and is retried next cycle; the clean retry records the gate
_m5 = _gmm(blocked={"D"}, fair=0.213)
_m5.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_m5.api.cancel_all = lambda exchange_id=None, **k: (_ for _ in ()).throw(ConnectionError("reset"))
_s10 = _MM.quote_cycle(_m5, _gbook, {}, {}, {"872": [{"id": 11, "side": "yes"}]}, {}, 50000.0, 10, 10)
_st10, _os10 = "872" in _m5.state, dict(_gstat(_m5))
_m5.api.cancel_all = lambda exchange_id=None, **k: _m5.api.cancel_alls.append(exchange_id)
_s11 = _MM.quote_cycle(_m5, _gbook, {}, {}, {"872": [{"id": 11, "side": "yes"}]}, {}, 50000.0, 11, 10)
check("mm gate-pull: a failed cancel-all (any exception, not only ApiError) is logged, counted as a write, records no state and is retried "
      "next cycle (the store keeps the order open); the clean retry records the gate",
      _s10.get("pulled") == ["872"] and _s10["writes"] == 1 and not _st10 and _os10 == {11: "open"} and _s11.get("pulled") == ["872"]
      and _gstat(_m5) == {11: "cancelled"} and _m5.api.cancel_alls == ["872"] and _m5.state["872"].last_gate == (False, True),
      f"{_s10} {_s11} {_gstat(_m5)} {_st10} {_os10}")
# dry run: no venue call, the write is counted
_m6 = _gmm(blocked={"D"}, fair=0.213, dry_run=True)
_m6.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_s12 = _MM.quote_cycle(_m6, _gbook, {}, {}, {"872": [{"id": 11}]}, {}, 50000.0, 12, 10)
check("mm gate-pull: dry run pulls nothing on the venue and counts the write", _m6.api.cancel_alls == [] and _s12.get("pulled") == ["872"] and _s12["writes"] == 1, str(_s12))
# the other reason for an empty ladder, the inventory cap on both sides, pulls too (hard_inventory_stop + the other side blocked)
_m7 = _gmm(blocked={"D"}, fair=0.2375)
_m7.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_s13 = _MM.quote_cycle(_m7, _gbook, {"872": 1e6}, {}, {"872": [{"id": 11}]}, {}, 50000.0, 13, 10)
check("mm gate-pull: a ladder emptied by the inventory cap (bids) and the partisan block (asks) pulls the resting bid as well",
      _s13.get("pulled") == ["872"] and _m7.api.cancel_alls == ["872"] and _m7.state["872"].last_gate == (True, True), f"{_s13}")
# the fast path (scanner thread): the touch moves into the gate, the resting quotes are pulled, the write counts against the
# per-minute budget, the touch is remembered so the move is not re-checked every second; force_cancel never cancels blind
_fcalls = []
_fst = types.SimpleNamespace(last_touch=(0.235, 0.24), last_reserve=0.23)
_fmm = types.SimpleNamespace(quoted=["872"], state={"872": _fst}, force_cancel=False,
                             quote_cycle=lambda books, *a, **k: _fcalls.append((dict(books), _fmm.force_cancel)) or {"pulled": ["872"], "requoted": []})
_fbk = {"bestBid": 0.24, "bestAsk": 0.245, "bids": [{"price": 0.24, "quantity": 2000}], "asks": [{"price": 0.245, "quantity": 2000}]}
_fb2 = types.SimpleNamespace(cfg=types.SimpleNamespace(mm_fast_requotes_per_min=3, mm=types.SimpleNamespace(enabled=True), shock_enabled=False),
                             feed_ok=lambda: True, mm=_fmm, killed_until=0.0, wind_down=None, strategy_kill=None, cash=1000.0,
                             fresh_book=lambda ex, age=None: _fbk, trade_lock=threading.Lock(), mm_positions=lambda p: {}, positions_snapshot={},
                             pressures=lambda: {}, open_orders_cache={"872": [{"id": 11}]}, fills_count_cache={}, last_cycle_id=1, book_cache={}, book_ts={})
_fsc = R.ArbScanner.__new__(R.ArbScanner)
_fsc.bot, _fsc.fast_log, _fsc.stats = _fb2, [], {}
_fsc.fast_requote_pass()
check("mm gate-pull: fast path: a touch move that closes the last quotable side pulls the quotes under the trade lock with force_cancel set, "
      "counts the write against the per-minute budget and remembers the new touch",
      len(_fcalls) == 1 and _fcalls[0][1] is True and _fmm.force_cancel is False and len(_fsc.fast_log) == 1 and _fsc.stats.get("fast_pulls") == 1
      and _fst.last_touch == (0.24, 0.245) and _fb2.book_cache["872"][0] is _fbk, f"{_fcalls} {_fsc.fast_log} {_fsc.stats} {_fst.last_touch}")
_m8 = _gmm(blocked={"D"}, fair=0.213)
_m8.force_cancel = True
_s14 = _MM.quote_cycle(_m8, _gbook, {}, {}, {"872": []}, {}, 50000.0, 14, 2)
check("mm gate-pull: the fast path's force_cancel never cancels a leg where nothing of ours rests",
      _m8.api.cancel_alls == [] and _m8.api.cancels == [] and not _s14.get("pulled") and _s14["writes"] == 0, str(_s14))

# review 1 (Opus) blocker: nothing is recorded while a quote of ours may still rest, and a quote resting on a side the ladder no
# longer has is a requote trigger of its own (needs_requote stale_side), so a re-open can never leave gated-side quotes behind
check("mm gate-pull: needs_requote fires on a quote resting on a side the ladder no longer has, everything else unchanged",
      mm2.needs_requote("9", info2, 0, 2, 2, stale_side=True) is True and mm2.needs_requote("9", info2, 0, 2, 2) is False)
# P1 / P2: the fast path's cache does not list this cycle's bids; the gate closes the bid side (D blocked: nothing to post):
# the pull finds them through the store (open MM orders placed since the open-orders read) and pulls them
_mb = _gmm(blocked={"D"}, fair=0.2375)
_mb.open_orders_ts = time.time() - 1
_MM.quote_cycle(_mb, _gbook, {}, {}, {"872": []}, {}, 50000.0, 20, 10)               # the bids are posted after the read
_ids_b = [{"id": i, "side": "yes"} for i in _gstat(_mb)]
_gfair(_mb, 0.213)
_mb.force_cancel = True
_sb1 = _MM.quote_cycle(_mb, _gbook, {}, {}, {"872": []}, {}, 50000.0, 20, 2)          # a fast pass on a stale (empty) cache
_mb.force_cancel = False
check("mm gate-pull P1/P2: a fast pass whose cache misses this cycle's bids still pulls them (the store knows them: open, placed since the "
      "read): one cancel-all, the gate recorded",
      _sb1.get("pulled") == ["872"] and _mb.api.cancel_alls == ["872"] and set(_gstat(_mb).values()) == {"cancelled"}
      and _mb.state["872"].last_gate == (False, True), f"{_sb1} {_mb.api.cancel_alls} {_gstat(_mb)}")
# the stale-side trigger itself: a closed gate recorded while our bids still rest (the failure mode review 1 found); the block
# lifts with the gate unchanged: needs_requote fires on the resting bids (a side the ladder no longer has): cancel-all, asks only
_mb2 = _gmm(blocked={"D"}, fair=0.2375)
_MM.quote_cycle(_mb2, _gbook, {}, {}, {"872": []}, {}, 50000.0, 21, 10)
_ids_b2 = [{"id": i, "side": "yes"} for i in _gstat(_mb2)]
_gfair(_mb2, 0.213)
_mb2.state["872"].last_gate = (False, True)
_mb2.blocked = set()
_sb2 = _MM.quote_cycle(_mb2, _gbook, {}, {}, {"872": _ids_b2}, {}, 50000.0, 22, 10)
_gb2 = _mb2.api.batches[-1]
check("mm gate-pull blocker P2: with the closed gate already recorded and the bids still resting, the block lifting (same gate, same reserve, "
      "orders complete) still requotes through the stale-side trigger: cancel-all, asks only",
      _sb2["requoted"] == ["872"] and _mb2.api.cancel_alls == ["872"] and _gb2 and all(o["side"] == "no" for o in _gb2)
      and _mb2.state["872"].last_gate == (False, True), f"{_sb2} {_mb2.api.cancel_alls}")
# P9: a failed pull, then the block lifts with the gate unchanged
_mb9 = _gmm(blocked={"D"}, fair=0.2375)
_MM.quote_cycle(_mb9, _gbook, {}, {}, {"872": []}, {}, 50000.0, 22, 10)
_ids_9 = [{"id": i, "side": "yes"} for i in _gstat(_mb9)]
_gfair(_mb9, 0.213)
_mb9.api.cancel_all = lambda exchange_id=None, **k: (_ for _ in ()).throw(_ApiErr(503, "DOWN", "down"))
_sb9 = _MM.quote_cycle(_mb9, _gbook, {}, {}, {"872": _ids_9}, {}, 50000.0, 23, 10)
_gate_9 = _mb9.state["872"].last_gate
_mb9.api.cancel_all = lambda exchange_id=None, **k: _mb9.api.cancel_alls.append(exchange_id)
_mb9.blocked = set()
_sb9b = _MM.quote_cycle(_mb9, _gbook, {}, {}, {"872": _ids_9}, {}, 50000.0, 24, 10)
check("mm gate-pull blocker P9: after a failed pull the gate is not recorded; the block lifting with the gate unchanged still requotes (cancel-all, "
      "asks only)",
      _sb9.get("pulled") == ["872"] and _gate_9 == (True, True) and _sb9b["requoted"] == ["872"] and _mb9.api.cancel_alls == ["872"]
      and all(o["side"] == "no" for o in _mb9.api.batches[-1]) and _mb9.state["872"].last_gate == (False, True), f"{_sb9} {_sb9b} {_mb9.api.cancel_alls}")
# P10: a pull cut short by the budget (an exit beside ours, so order by order): not clean, nothing recorded, finished next cycle
_mb10 = _gmm(blocked={"D"}, fair=0.2375)
_MM.quote_cycle(_mb10, _gbook, {}, {}, {"872": []}, {}, 50000.0, 25, 10)
_mb10.store.record_order(40, "872", "183", "no", "buy", 0.76, 100, "exit")
_ids_10 = [{"id": i, "side": "yes"} for i in _gstat(_mb10) if i != 40] + [{"id": 40, "side": "no"}]
_n10 = len(_ids_10) - 1
_gfair(_mb10, 0.213)
_sb10 = _MM.quote_cycle(_mb10, _gbook, {}, {}, {"872": _ids_10}, {}, 50000.0, 26, 2)
_left10 = [i for i, v in _gstat(_mb10).items() if v == "open" and i != 40]
_gate_10 = _mb10.state["872"].last_gate
_sb10b = _MM.quote_cycle(_mb10, _gbook, {}, {}, {"872": _ids_10}, {}, 50000.0, 27, 10)
check("mm gate-pull blocker P10: a pull cut short by the budget records nothing; the next cycle pulls the rest, the exit untouched, then the gate "
      "is recorded",
      _n10 >= 3 and _sb10["writes"] == 2 and len(_left10) == _n10 - 2 and _gate_10 == (True, True) and _sb10b.get("pulled") == ["872"]
      and _mb10.api.cancel_alls == [] and _gstat(_mb10)[40] == "open" and all(v == "cancelled" for i, v in _gstat(_mb10).items() if i != 40)
      and _mb10.state["872"].last_gate == (False, True), f"{_n10} {_sb10} {_left10} {_sb10b} {_gstat(_mb10)}")
# P3: an order another sleeve placed on the leg since the open-orders read (the store knows it, the list does not) turns the
# cancel-all into an order-by-order pull; one placed long before the read (and gone from the list) does not
_mn = _gmm(blocked={"D"}, fair=0.213)
_mn.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_mn.store.record_order(41, "872", "183", "no", "buy", 0.76, 100, "exit")
_mn.open_orders_ts = time.time()
_sn = _MM.quote_cycle(_mn, _gbook, {}, {}, {"872": [{"id": 11, "side": "yes"}]}, {}, 50000.0, 28, 10)
_mn2 = _gmm(blocked={"D"}, fair=0.213)
_mn2.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_mn2.store.record_order(41, "872", "183", "no", "buy", 0.76, 100, "exit")
_mn2.store.exec("UPDATE orders SET placed_at=? WHERE order_id=41", (time.time() - 7200,))
_mn2.open_orders_ts = time.time()
_sn2 = _MM.quote_cycle(_mn2, _gbook, {}, {}, {"872": [{"id": 11, "side": "yes"}]}, {}, 50000.0, 29, 10)
check("mm gate-pull P3: a non-MM order placed on the leg since the open-orders read makes the pull order by order (no cancel-all); one placed "
      "2 h earlier and gone from the list does not",
      _mn.api.cancel_alls == [] and _mn.api.cancels == [11] and _sn.get("pulled") == ["872"] and _gstat(_mn) == {11: "cancelled", 41: "open"}
      and _mn2.api.cancel_alls == ["872"] and _mn2.api.cancels == [] and _sn2.get("pulled") == ["872"], f"{_sn} {_gstat(_mn)} {_sn2}")

# ---------------------------------------------------------------- MM: the poll lands after the MM block: gate sweep in the same cycle
# sim 'catchup' 29 Oct (leg 1067, Alaska Senate R, live params): the outside fair moved 3 pts at 15:14:30; the bot's poll saw it
# at 15:15:00, after the MM had quoted that cycle with the old fair; a crowd sell hit the resting bid at 15:15:15 on the side
# the new fair gated; the MM only reacted at 15:15:30. gate_for is the one gate; gate_changed finds the legs; mm_gate_sweep acts.
_m9 = _gmm(fair=0.2375)
check("mm gate-sweep: gate_for is xv_gate at the mid (open with no fair, with the gate off, or on a stale fair)",
      _m9.gate_for("872", 0.2375) == (True, True) and (_gfair(_m9, 0.213) or _m9.gate_for("872", 0.2375) == (False, True))
      and (_gfair(_m9, 0.262) or _m9.gate_for("872", 0.2375) == (True, False)) and _m9.gate_for("871", 0.5) == (True, True)
      and (_m9.xv_fair.__setitem__("872", (0.213, 0.213, 0.213, time.time() - 900)) or _m9.gate_for("872", 0.2375) == (True, True))
      and (setattr(_m9.cfg, "xv_gate_ticks", 0) or _gfair(_m9, 0.213) or _m9.gate_for("872", 0.2375) == (True, True)))
_m9 = _gmm(fair=0.2375)
_m9.open_orders_ts = time.time() - 1
_s15 = _MM.quote_cycle(_m9, _gbook, {}, {}, {"872": []}, {}, 50000.0, 15, 10)        # both sides quoted, gate open, after the read
_g15 = _m9.api.batches[-1]
_gfair(_m9, 0.213)                                                                     # the poll: 4.9 ticks under the mid: bids closed
_cl = _m9.gate_closed(_gbook)
_cl_unknown = _m9.gate_closed({"872": {"bids": [], "asks": [{"price": 0.24, "quantity": 2000}]}})
_m9.state["872"].last_reserve = None
_cl_wave = _m9.gate_closed(_gbook)
_m9.state["872"].last_reserve = 0.23
_m9o = _gmm(fair=0.213)
_m9o.state["872"] = QuoteState("872", last_reserve=0.23, last_quote_ts=time.time(), fills_seen=0, last_gate=(False, True))
_gfair(_m9o, 0.2375)                                                                   # a side only OPENED: not the sweep's business
check("mm gate-sweep: gate_closed lists a quoted leg whose gate closed a side with the new fair, not one with an unknown mid, not one forced "
      "to requote anyway, not one never quoted, not one whose gate only opened",
      {o["side"] for o in _g15} == {"yes", "no"} and _cl == {"872": ((True, True), (False, True))} and _cl_unknown == {} and _cl_wave == {}
      and _gmm(fair=0.213).gate_closed(_gbook) == {} and _m9o.gate_closed(_gbook) == {}, f"{_cl} {_cl_unknown} {_cl_wave}")
# the sweep itself on the real MM: the cycle's list predates the quotes (empty); the quotes are found in the store and pulled
# with one cancel-all; nothing is posted; the gate is recorded; the next cycle's requote posts the asks only
_s16 = _m9.pull_closed(_gbook, {"872": []}, 6)
_st16, _gate16, _nb16 = dict(_gstat(_m9)), _m9.state["872"].last_gate, len(_m9.api.batches)
_cl16 = _m9.pull_closed(_gbook, {"872": []}, 6)
_s16b = _MM.quote_cycle(_m9, _gbook, {}, {}, {"872": []}, {}, 50000.0, 16, 10)
_g16 = _m9.api.batches[-1]
check("mm gate-sweep: pull_closed pulls the leg's quotes (one cancel-all, nothing posted) and records the gate, so a second sweep finds nothing; "
      "the next cycle posts the open side only",
      _s16 == {"legs": ["872"], "pulled": ["872"], "writes": 1, "skipped_budget": 0} and _m9.api.cancel_alls == ["872"] and _nb16 == 1
      and set(_st16.values()) == {"cancelled"} and _gate16 == (False, True) and _cl16["legs"] == []
      and _s16b["requoted"] == ["872"] and len(_m9.api.batches) == 2 and all(o["side"] == "no" for o in _g16) and _m9.api.cancel_alls == ["872"],
      f"{_s16} {_st16} {_cl16} {_s16b} {_m9.api.cancel_alls}")
# with the other side blocked too, and an exit beside ours on the leg (in the list): order by order, the exit untouched; no budget: reported
_m10 = _gmm(blocked={"D"}, fair=0.2375)
_MM.quote_cycle(_m10, _gbook, {}, {}, {"872": []}, {}, 50000.0, 16, 10)
_m10.store.record_order(40, "872", "183", "no", "buy", 0.76, 100, "exit")
_oo10 = {"872": [{"id": i, "side": "yes"} for i in _gstat(_m10) if i != 40] + [{"id": 40, "side": "no"}]}
_gfair(_m10, 0.213)
_s17z = _m10.pull_closed(_gbook, _oo10, 0)
_s17 = _m10.pull_closed(_gbook, _oo10, 6)
check("mm gate-sweep: with an exit beside ours the sweep cancels ours one by one and leaves the exit; with no budget it only reports",
      _s17z == {"legs": ["872"], "pulled": [], "writes": 0, "skipped_budget": 1} and _s17["pulled"] == ["872"] and _s17["writes"] == len(_oo10["872"]) - 1
      and _m10.api.cancel_alls == [] and sorted(_m10.api.cancels) == sorted(i for i in _gstat(_m10) if i != 40) and _gstat(_m10)[40] == "open"
      and _m10.state["872"].last_gate == (False, True), f"{_s17z} {_s17} {_m10.api.cancels} {_gstat(_m10)}")
# both sides quoted and a catch-up order placed on the leg since the read (sim 'catchup' 29 Oct, three legs at once): only our
# bids (the closed side) go, one by one; the asks keep resting and the next cycle leaves them alone
_m11 = _gmm(fair=0.2375)
_m11.open_orders_ts = time.time() - 1
_MM.quote_cycle(_m11, _gbook, {}, {}, {"872": []}, {}, 50000.0, 17, 10)
_m11.store.record_order(50, "872", "183", "yes", "buy", 0.24, 100, "catchup")
_sides11 = {r["order_id"]: r["side"] for r in _m11.store.rows("SELECT order_id, side FROM orders")}
_gfair(_m11, 0.213)
_s18 = _m11.pull_closed(_gbook, {"872": []}, 8)
_st18 = dict(_gstat(_m11))
_s18b = _MM.quote_cycle(_m11, _gbook, {}, {}, {"872": [{"id": i, "side": _sides11[i]} for i, v in _st18.items() if v == "open"]}, {}, 50000.0, 18, 10)
check("mm gate-sweep: with another sleeve's order placed on the leg since the read, only our orders on the closed side are cancelled, one by "
      "one; the open side's quotes keep resting and the next cycle does not touch them",
      _s18["pulled"] == ["872"] and _m11.api.cancel_alls == [] and sorted(_m11.api.cancels) == sorted(i for i, sd in _sides11.items() if sd == "yes" and i != 50)
      and _s18["writes"] == len(_m11.api.cancels) >= 1 and all(_st18[i] == "cancelled" for i in _m11.api.cancels)
      and all(_st18[i] == "open" for i, sd in _sides11.items() if sd == "no") and _st18[50] == "open"
      and _s18b["requoted"] == [] and not _s18b.get("pulled") and _m11.state["872"].last_gate == (False, True), f"{_s18} {_st18} {_s18b} {_m11.api.cancels}")
# review 2 (Opus), findings 1, 2 and note 4. (1) a requote's cancel-all marks every order of ours on the leg, listed or not, so a later
# pull spends no cancels on phantoms; (2) the cycle's list holding the previous ladder's ids (ours, cancelled) does not stop the one
# cancel-all; (4) a cancel answered 409 / 404 (the order is not open any more) marks the order gone and counts as clean
_m12 = _gmm(fair=0.2375)
_m12.open_orders_ts = time.time() - 1
_MM.quote_cycle(_m12, _gbook, {}, {}, {"872": []}, {}, 50000.0, 30, 10)               # ladder A
_ids_a = sorted(_gstat(_m12))
_m12.force_cancel = True
_s19 = _MM.quote_cycle(_m12, _gbook, {}, {}, {"872": []}, {}, 50000.0, 30, 10)        # a fast requote on an empty cache: ladder B
_m12.force_cancel = False
_st19 = dict(_gstat(_m12))
_ids_b = sorted(i for i in _st19 if i not in _ids_a)
check("mm review 2 (1): a requote's cancel-all marks this cycle's earlier quotes cancelled too, listed or not",
      _s19["requoted"] == ["872"] and _m12.api.cancel_alls == ["872"] and _ids_b and all(_st19[i] == "cancelled" for i in _ids_a)
      and all(_st19[i] == "open" for i in _ids_b), f"{_s19} {_st19}")
_gfair(_m12, 0.213)                                                                    # the poll closes the bid side
_m12.store.record_order(9, "872", "183", "yes", "buy", 0.2, 50, "mm")               # an old quote of ours the list lacks (expired, say)
_m12.store.exec("UPDATE orders SET placed_at=? WHERE order_id=9", (time.time() - 7200,))
_s20 = _m12.pull_closed(_gbook, {"872": [{"id": i, "side": "yes"} for i in _ids_a]}, 8)   # the cycle's list: ladder A's ids
_st20 = dict(_gstat(_m12))
check("mm review 2 (2): a sweep on a leg requoted this cycle (the list holds the old ladder's ids, ours) is one cancel-all, not order by "
      "order; ladder B and every older open row of ours on the leg are marked cancelled",
      _s20["pulled"] == ["872"] and _s20["writes"] == 1 and _m12.api.cancel_alls == ["872", "872"] and _m12.api.cancels == []
      and all(_st20[i] == "cancelled" for i in _ids_b) and _st20[9] == "cancelled" and _m12.state["872"].last_gate == (False, True), f"{_s20} {_st20}")
_m13 = _gmm(blocked={"D"}, fair=0.213)
for _i in (11, 12, 13):
    _m13.store.record_order(_i, "872", "183", "yes", "buy", 0.235, 100, "mm")
_m13.store.record_order(40, "872", "183", "no", "buy", 0.76, 100, "exit")
_m13.api.cancel_order = lambda oid: (_ for _ in ()).throw(_ApiErr(409, "NOT_OPEN", "not open")) if oid == 12 else _m13.api.cancels.append(oid)
_s21 = _MM.quote_cycle(_m13, _gbook, {}, {}, {"872": [{"id": 11, "side": "yes"}, {"id": 12, "side": "yes"}, {"id": 13, "side": "yes"}, {"id": 40, "side": "no"}]},
                       {}, 50000.0, 31, 10)
_m14 = _gmm(blocked={"D"}, fair=0.213)
_m14.store.record_order(11, "872", "183", "yes", "buy", 0.235, 100, "mm")
_m14.store.record_order(40, "872", "183", "no", "buy", 0.76, 100, "exit")
_m14.api.cancel_order = lambda oid: (_ for _ in ()).throw(_ApiErr(500, "ERR", "boom"))
_s22 = _MM.quote_cycle(_m14, _gbook, {}, {}, {"872": [{"id": 11, "side": "yes"}, {"id": 40, "side": "no"}]}, {}, 50000.0, 32, 10)
check("mm review 2 (4): a cancel answered 409 (the order is gone) is marked cancelled and the pull is clean (gate recorded); a 500 is not",
      _s21.get("pulled") == ["872"] and _m13.api.cancels == [11, 13] and _gstat(_m13) == {11: "cancelled", 12: "cancelled", 13: "cancelled", 40: "open"}
      and getattr(_m13.state.get("872"), "last_gate", None) == (False, True) and _s22.get("pulled") == ["872"] and _gstat(_m14) == {11: "open", 40: "open"}
      and "872" not in _m14.state, f"{_s21} {_gstat(_m13)} {_s22} {_gstat(_m14)}")
# Bot.mm_gate_sweep wiring on a stub: pull_closed under the trade lock with the given budget; nothing to do -> {}
_swcalls = []


class _SwMM:
    def __init__(self):
        self.locked_seen = None

    def pull_closed(self, books, open_orders, budget):
        self.locked_seen = _swbot.trade_lock.locked()
        _swcalls.append((sorted(books), sorted(open_orders), budget))
        return {"legs": ["A"], "pulled": ["A"], "writes": 1, "skipped_budget": 0} if "A" in books else {"legs": [], "pulled": [], "writes": 0, "skipped_budget": 0}


_swbot = types.SimpleNamespace(mm=_SwMM(), trade_lock=threading.Lock())
_sw = R.Bot.mm_gate_sweep(_swbot, {"A": _rbk(0.5, 0.505), "B": _rbk(0.4, 0.405)}, {"A": [{"id": 1}]}, 5)
_swn = R.Bot.mm_gate_sweep(_swbot, {"B": _rbk(0.4, 0.405)}, {}, 5)
check("mm gate-sweep: Bot.mm_gate_sweep runs pull_closed under the trade lock with the cycle's books, list and budget; nothing closed -> {}",
      _swcalls == [(["A", "B"], ["A"], 5), (["B"], [], 5)] and _swbot.mm.locked_seen is True and not _swbot.trade_lock.locked()
      and _sw == {"legs": ["A"], "pulled": ["A"], "writes": 1, "skipped_budget": 0} and _swn == {}, f"{_swcalls} {_sw} {_swn}")
_rs_gs = open(os.path.join(ROOT, "sigbot", "run.py")).read()
_i_ref = _rs_gs.find("# ---- outside fair for the MM's gate, refreshed right after the directional poll")
_i_sw = _rs_gs.find("sw = self.mm_gate_sweep(mm_books, open_orders, max(0, min(8, left)))")
_i_cf = _rs_gs.find("# ---- cheap safe favourites")
check("mm gate-sweep: wired right after the post-poll xv_fair refresh (before cheap favourites), only after a poll, with the MM, inside and "
      "tilt writes and the directional sleeve's 2 taken off the budget, capped at 8",
      0 < _i_ref < _i_sw < _i_cf and 'if mm_books and dir_summary.get("polled") and not killed and gate_ready and not mm_paused:' in _rs_gs
      and "mm_books = books" in _rs_gs and _rs_gs.count("mm_summary, mm_books = {}, {}") == 1)

# ---------------------------------------------------------------- sim churn_blowout false positive, 29 Oct 11:3x BST
# The precheck failed on `sets_left_at_sell_all {'IA-02 House race': 75}`: sim taker flow hit our MM bids on both legs of a race AFTER
# the sell-all (none at the sell-all cycle itself); the old check re-read every later cycle and judged the end of the run.
sys.path.insert(0, os.path.join(ROOT, "tests"))
import sim_pipeline as _spl  # noqa: E402


# ChurnBlowout.after_cycle: the snapshot is the sell-all cycle's, kept; later MM fills do not overwrite it, and a venue set left
# in a churn race at the sell-all cycle (churn's own or not: the sell-all must sell every set in its races) is still reported.
_pos_now, _calls_now = {}, []
_mm_fills = {}
class _MmStore:
    def rows(self, q, a=()):
        return [{"exchange_id": e, "q": x} for e, x in _mm_fills.items()] if "purpose IN ('mm','inside')" in q else []
_bsim = types.SimpleNamespace(v=types.SimpleNamespace(calls=_calls_now, signed_positions=lambda: dict(_pos_now)),
                              legs=lambda race: {"A": ["1", "2"], "IA": ["905", "904"]}[race], bot=types.SimpleNamespace(store=_MmStore()))
_bo = _spl.ChurnBlowout()
_bo.races, _bo.sell_all = ["A", "IA"], _spl.T0 + 100.0
_tr0 = _spl.ChurnBase.after_cycle
_spl.ChurnBase.after_cycle = lambda self, sim, i: None
_t_save = _spl.CLOCK.t
try:
    _spl.CLOCK.t = _spl.T0 + 90.0
    _bo.after_cycle(_bsim, 1)                                                          # before the deadline: no snapshot
    _b0 = getattr(_bo, "left_after", None)
    _spl.CLOCK.t = _spl.T0 + 100.0
    _bo.after_cycle(_bsim, 2)                                                          # the sell-all cycle: clean
    _b1 = dict(_bo.left_after)
    _pos_now.update({"905": -744.0, "904": -75.0})                                     # later taker flow on the MM bids of IA
    _spl.CLOCK.t = _spl.T0 + 130.0
    _bo.after_cycle(_bsim, 3)
    _b2 = dict(_bo.left_after)
    _bo2 = _spl.ChurnBlowout()                                                         # a run where a set is left at the deadline
    _bo2.races, _bo2.sell_all = ["A", "IA"], _spl.T0 + 100.0
    _pos_now.clear()
    _pos_now.update({"1": -50.0, "2": -50.0})
    _spl.CLOCK.t = _spl.T0 + 100.0
    _bo2.after_cycle(_bsim, 2)
    _b3 = dict(_bo2.left_after)
    # 31 Oct: MM inventory present AT the sell-all cycle (IA-02: -19 / -740, purpose mm only) is not a churn set; churn's own set next to it is
    _bo3 = _spl.ChurnBlowout()
    _bo3.races, _bo3.sell_all = ["A", "IA"], _spl.T0 + 100.0
    _pos_now.clear(); _pos_now.update({"905": -740.0, "904": -19.0}); _mm_fills.update({"905": -740.0, "904": -19.0})
    _spl.CLOCK.t = _spl.T0 + 100.0
    _bo3.after_cycle(_bsim, 2)
    _b4 = dict(_bo3.left_after)
    _bo4 = _spl.ChurnBlowout()
    _bo4.races, _bo4.sell_all = ["A", "IA"], _spl.T0 + 100.0
    _pos_now.update({"905": -840.0, "904": -119.0})            # 100 more on both legs from a basket (not MM fills): a real set of 100 on top of the MM's
    _bo4.after_cycle(_bsim, 2)
    _b5 = dict(_bo4.left_after)
    _bo5 = _spl.ChurnBlowout()                                  # Kansas Senate: MM long 120 on one leg, short 560 on the other, flat venue leg: no set at all
    _bo5.races, _bo5.sell_all = ["A", "IA"], _spl.T0 + 100.0
    _pos_now.clear(); _pos_now.update({"904": -680.0}); _mm_fills.clear(); _mm_fills.update({"904": -560.0, "905": 120.0})
    _bo5.after_cycle(_bsim, 2)
    _b6 = dict(_bo5.left_after)
finally:
    _spl.ChurnBase.after_cycle = _tr0
    _spl.CLOCK.t = _t_save
    _mm_fills.clear()
check("sim churn_blowout: sets_left_at_sell_all reads the sell-all cycle only (MM fills after it do not count) and still reports a set "
      "the venue holds there",
      _b0 is None and _b1 == {} and _b2 == {} and _b3 == {"A": 50}, f"{_b0} {_b1} {_b2} {_b3}")
check("sim churn_blowout: MM inventory at the sell-all cycle (mm-purpose fills on both legs) is not counted as sets, a churn set next to it still is, and an MM long on one leg never turns a flat race into a set",
      _b4 == {} and _b5 == {"IA": 100} and _b6 == {}, f"{_b4} {_b5} {_b6}")

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
