"""Failure-scenario tests of the offload executor (sigbot/proposals/offload_executor.py). Offline: a fake venue behind the adapter.

    python3 tests/test_offload.py

Each test names a way the sale could go wrong and checks it cannot.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sigbot.proposals import offload_executor as OE  # noqa: E402
from sigbot.proposals.punt_executor import sha256_file  # noqa: E402
from sigbot import strategy_stops as SS  # noqa: E402
from sigbot.config import BotConfig  # noqa: E402
from datetime import datetime  # noqa: E402

FAILS = []
BUILT = "2026-11-01 00:00 UTC"
START, STOP = "2026-11-01T01:00:00+00:00", "2026-11-01T01:30:00+00:00"
T0 = datetime.fromisoformat(START).timestamp()


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Err(Exception):
    def __init__(self, status, code="", details=None):
        super().__init__(f"{status} {code}")
        self.status, self.code, self.details = status, code, details


AON = lambda: Err(400, "VALIDATION_ERROR", {"rejectionReasonCode": "AllOrNoneNotFilled", "leg": 0})


def lad(*lv):
    return [{"price": p, "quantity": q} for p, q in lv]


class Fake:
    """Baskets fill all-or-none within their limits against static books; `fail` is a list of exceptions raised by the next sends. Keys are
    idempotent: a replay returns the first response and moves nothing. `rest`: batch orders rest and fill only at fill_resting()."""

    def __init__(self, pos, races, books, dry=False, bal=20000.0):
        self.dry, self.cap = dry, 5000.0
        self.t = T0
        self.pos, self.races, self.books, self.bal = dict(pos), races, books, bal
        self.cf, self.sent, self.calls, self.fail, self.cancels = {}, [], [], [], []
        self.abort_flag = self.block = self.cf_block = None
        self.busy_r, self.pull_ok, self.locked, self.nd_fn = {}, True, False, lambda p: 0.0
        self.cap_fn = lambda b, a: True
        self.steal = None                       # called before each send: another sleeve moves positions
        self.no_move = self.no_cash = self.lose_resp = self.cf_lag = False
        self.tags = []
        self.keys, self.rest, self.resting, self.lag, self.lag_pos, self.lag_left = {}, False, [], 0, None, 0
        self.pull_writes, self.after = 0, 0
        self.sell_ok, self.floors = True, {}
        self.lose_batch = False

    def now(self):
        return self.t

    def lock(self):
        @contextlib.contextmanager
        def cm():
            self.locked = True
            yield
            self.locked = False
        return cm()

    def positions(self):
        if self.lag_left > 0:                   # /positions behind the fills for this many reads
            self.lag_left -= 1
            return dict(self.lag_pos)
        return dict(self.pos)

    def fill_resting(self):
        for o in self.resting:
            self.pos[o["exchangeId"]] += o["quantity"] if o["side"] == "yes" else -o["quantity"]
        self.resting = []

    def pull_cost(self, exs):
        return self.pull_writes

    def after_batch(self):
        self.after += 1

    def sale_view(self, p):
        return {e: q - self.cf.get(e, 0.0) for e, q in p.items()}

    def cheapfav_open(self):
        return dict(self.cf)

    def balance(self):
        return self.bal

    def book(self, ex):
        return self.books.get(ex)

    def set_floor(self, r):
        return self.floors.get(r)

    def race_legs(self, r):
        return self.races[r]

    def busy(self, r):
        return self.busy_r.get(r)

    def blocked(self):
        return self.block

    def cheapfav_ok(self):
        return self.cf_block

    def pull(self, exs, now):
        self.calls.append(("pull", tuple(exs), self.t))
        return self.pull_ok

    def cap_ok(self, b, a):
        return self.cap_fn(b, a)

    def net_d(self, p):
        return self.nd_fn(p)

    def abort_requested(self):
        return bool(self.abort_flag)

    def basket(self, race, kind, legs, key, q, proceeds, payout, sgn):
        assert self.locked, "basket outside the trade lock"
        if self.steal:
            self.steal()
        self.calls.append(("basket", race, self.t))
        self.sent.append(dict(race=race, kind=kind, legs=legs, key=key, q=q, t=self.t))
        if self.fail:
            e = self.fail.pop(0)
            if e is not None:
                raise e
        if key in self.keys:
            return self.keys[key]
        if self.lose_resp:                      # it traded, the response was lost; a resend replays it
            self.lose_resp = False
            for l in legs:
                self.pos[l["exchangeId"]] += sgn * q
            self.bal += q * proceeds
            self.keys[key] = {"results": [{"data": {"exchangeId": l["exchangeId"], "quantityTraded": q, "open": False,
                                                    "totalCost": q * l["price"]}} for l in legs]}
            raise Err(502, "ORDER_STATUS_UNKNOWN")
        if not self.no_move:
            for l in legs:
                self.pos[l["exchangeId"]] += sgn * q
        if not self.no_cash:
            self.bal += q * proceeds
        return {"results": [{"data": {"exchangeId": l["exchangeId"], "quantityTraded": q, "open": False,
                                      "totalCost": q * l["price"]}} for l in legs]}

    def batch(self, orders, key):
        assert self.locked, "batch outside the trade lock"
        self.calls.append(("batch", len(orders), self.t))
        self.sent.append(dict(batch=[dict(o) for o in orders], key=key, t=self.t))
        if self.fail:
            e = self.fail.pop(0)
            if isinstance(e, list):
                return e
            if e is not None:
                raise e
        if self.lose_batch:                     # the orders landed, the response was lost
            self.lose_batch = False
            self.keys[key] = True
            if self.rest:
                self.resting.extend(dict(o) for o in orders)
            else:
                for o in orders:
                    self.pos[o["exchangeId"]] += o["quantity"] if o["side"] == "yes" else -o["quantity"]
            raise Err(502, "ORDER_STATUS_UNKNOWN")
        out = []
        if self.rest:
            self.resting.extend(dict(o) for o in orders)
            return [{"ok": True, "status": 201, "data": {"orderId": 500 + i, "quantityTraded": 0, "open": True}} for i in range(len(orders))]
        for o in orders:
            self.pos[o["exchangeId"]] += o["quantity"] if o["side"] == "yes" else -o["quantity"]
            if o["exchangeId"] in self.cf and not self.cf_lag:
                self.cf[o["exchangeId"]] += o["quantity"] if o["side"] == "yes" else -o["quantity"]
            self.bal += o["quantity"] * (1 - o["price"])
            out.append({"ok": True, "status": 201, "data": {"orderId": len(self.sent) * 100, "quantityTraded": o["quantity"], "open": False}})
        return out

    def record_leg(self, data, order, purpose):
        self.tags.append((order["exchangeId"], purpose))

    def cancel(self, ids):
        self.cancels.extend(ids)


def row(step, name, typ, qty, cash, legs, worst):
    return {"step": step, "holding": name, "type": typ, "qty": qty, "cash": cash, "legs": legs,
            "worst_cash_per_share": dict(zip(legs, worst))}


HOLD = [dict(name="MN-05 House race", race="MN-05 House race", kind="buy_set", legs=["m1", "m2"]),
        dict(name="RI", race="RI", kind="sell_set", legs=["r1", "r2", "r3"]),
        dict(name="FL", race="FL", kind="sell_set", legs=["f1", "f2"]),
        dict(name="CO D NO", race="CO", kind="no", legs=["c1"]),
        dict(name="NV R YES", race="NV", kind="yes", legs=["n1"]),
        dict(name="MD D YES", race="MD", kind="yes", legs=["d1"])]
RACES = {"MN-05 House race": ["m1", "m2"], "RI": ["r1", "r2", "r3"], "FL": ["f1", "f2"], "CO": ["c1", "c2"], "NV": ["n1", "n2"], "MD": ["d1", "d2"]}
ROWS = [row(1, "RI", "set_late", 2000, 3840.0, ["r1", "r2", "r3"], [0.96, 0.5, 0.48]),
        row(2, "CO D NO", "leg_held", 100, 40.0, ["c1"], [0.40]),
        row(3, "FL", "set_early", 300, 294.0, ["f1", "f2"], [0.60, 0.38]),
        row(4, "NV R YES", "leg", 200, 120.0, ["n1"], [0.60]),
        row(5, "MN-05 House race", "set_early", 2500, 2400.0, ["m1", "m2"], [0.02, 0.94])]
TARGET = 6900


def books(mn_bid=0.94):
    return {"m1": {"bids": lad((0.02, 90000)), "asks": lad((0.03, 90000))}, "m2": {"bids": lad((mn_bid, 90000)), "asks": lad((mn_bid + 0.01, 90000))},
            "r1": {"bids": lad((0.03, 90000)), "asks": lad((0.04, 90000))}, "r2": {"bids": lad((0.49, 90000)), "asks": lad((0.5, 90000))},
            "r3": {"bids": lad((0.51, 90000)), "asks": lad((0.52, 90000))},
            "f1": {"bids": lad((0.39, 90000)), "asks": lad((0.4, 90000))}, "f2": {"bids": lad((0.61, 90000)), "asks": lad((0.62, 90000))},
            "c1": {"bids": lad((0.59, 9000)), "asks": lad((0.6, 9000))}, "n1": {"bids": lad((0.6, 9000)), "asks": lad((0.61, 9000))},
            "d1": {"bids": lad((0.96, 9000)), "asks": lad((0.965, 9000))}}


def pos0():
    return {"m1": 2500.0, "m2": 2500.0, "r1": -2000.0, "r2": -2000.0, "r3": -2000.0, "f1": -300.0, "f2": -300.0, "c1": -100.0, "n1": 200.0}


def setup(rows=None, target=TARGET, pos=None, bks=None, dry=False, built=BUILT, sha=None, start=START, stop=STOP, state=None, reached=True, **kw):
    d = tempfile.mkdtemp(prefix="off_t_")
    plan = {"built": built, "cash": 20000, "holdings": HOLD, "speeds": {"5 min": [{"target": target, "reached": reached, "order": rows if rows is not None else ROWS}]}}
    path = os.path.join(d, "plan.json")
    json.dump(plan, open(path, "w"))
    f = Fake(pos0() if pos is None else pos, RACES, books() if bks is None else bks, dry=dry)
    e = OE.OffloadExecutor(path, state or os.path.join(d, "state.json"), sha256_file(path) if sha is None else sha, f, target, "5 min", start, stop, **kw)
    return e, f


def run(e, f, secs=400, step=2.0):
    for _ in range(int(secs / step)):
        e.tick()
        f.t += step


def baskets(f):
    return [s for s in f.sent if "race" in s]


def batches(f):
    return [s for s in f.sent if "batch" in s]


# ---------------------------------------------------------------- switched off by default
cfg = BotConfig()
live = json.load(open(os.path.join(ROOT, "config.json")))
_ba = types.SimpleNamespace(cfg=types.SimpleNamespace(offload_conventional_sell=False, wind_down_set_floor={"RI": 1.905}), churn=None, catchup_open={}, catchup=types.SimpleNamespace(ctx={"e1": {"target": 100}}),
                            universe=types.SimpleNamespace(races={"X": types.SimpleNamespace(exchange_ids=["e1"]), "Y": types.SimpleNamespace(exchange_ids=["e2"])}),
                            wind_down_hold_path=os.path.join(tempfile.mkdtemp(), "none.json"), punt_races=lambda: set(), punt_held_races=lambda: set(), shock_races=lambda: set())
import sigbot.run as _R  # noqa: E402
_ad = _R._OffloadAdapter(_ba)
check("adapter: a catch-up trade with a context is left alone even before its fill is in the ledger; conventional sells default off; the wind-down floor is read",
      _ad.busy("X") == "catch-up sweep" and _ad.busy("Y") is None and _ad.sell_ok is False and _ad.set_floor("RI") == 1.905, str(_ad.busy("X")))
check("off: offload_enabled defaults false, offload_dry_run true, no approved hash", not cfg.offload_enabled and cfg.offload_dry_run and not cfg.offload_plan_sha256)
check("off: config.json does not enable it or turn the dry run off", not live.get("offload_enabled") and live.get("offload_dry_run", True))
check("tags: baskets and legs carry purposes the strategy stops own", all(p in SS.PURPOSE_LEDGER for p in ("raise_set", "raise", "cheapfav_exit")))

# ---------------------------------------------------------------- refusals
e, f = setup(sha="0" * 64)
run(e, f, 30)
check("refused: a plan whose bytes are not the approved hash sends nothing", bool(e.refused) and "hash" in e.refused and not f.sent, str(e.refused))
e, f = setup(sha="")
check("refused: no approved hash", bool(e.refused))
e, f = setup(built="2026-10-31 00:00 UTC")
check("refused: a plan built over 6 h before the start", bool(e.refused) and "built" in e.refused, str(e.refused))
e, f = setup(start="", stop="")
check("refused: no window (a flag left on must never sell later)", bool(e.refused), str(e.refused))
e, f = setup(stop="2026-11-02T01:30:00+00:00")
check("refused: a window over 2 h", bool(e.refused), str(e.refused))
e, f = setup(reached=False)
check("refused: a target the plan did not reach", bool(e.refused) and "reached" in e.refused, str(e.refused))
e, f = setup(rows=[row(1, "RI", "hf_set", 100, 100.0, ["r1", "r2", "r3"], [0.9, 0.5, 0.5])])
check("refused: a row type this executor does not sell (high-frequency arb lots)", bool(e.refused), str(e.refused))
e, f = setup(dry=True, state=os.path.join(tempfile.mkdtemp(), "offload_state.json"))
check("refused: a dry run on the live state file", bool(e.refused) and "state" in e.refused, str(e.refused))

# ---------------------------------------------------------------- the window and the switches
e, f = setup()
f.t = T0 - 60
for _ in range(20):
    e.tick()
    f.t += 2
check("window: nothing before the start", not f.sent and e.state["status"] == "waiting")
e, f = setup()
f.t = T0 + 3600
e.tick()
check("window: past the stop the sale closes without a send", not f.sent and e.state["status"] == "done", e.state["status"])
e, f = setup(dry=True)
run(e, f)
check("dry: no races are kept from the other sleeves in a dry run", e.races() == set())
e, f = setup()
f.t = T0 + 1
f.steal = None
e.tick()
check("live: while the sale runs its races are protected", "MN-05 House race" in e.races() and "RI" in e.races(), str(e.races()))

# ---------------------------------------------------------------- the test basket
e, f = setup()
run(e, f, 400)
b = baskets(f)
check("test: the first send is an MN-05 basket, the plan's 1/5", b[0]["race"] == "MN-05 House race" and b[0]["q"] == 500, str(b[0]))
check("test: nothing else goes until it is confirmed at the venue", all(s["t"] >= b[0]["t"] + OE.TEST_WAIT for s in f.sent[1:]))
check("test: it is a buy of NO on a YES set, at 1 - the bid", b[0]["kind"] == "buy_set" and all(l["side"] == "no" and l["action"] == "buy" for l in b[0]["legs"]))
check("sale: the whole list sells", e.state["status"] == "done" and all(abs(f.pos.get(k, 0)) < 1 for k in pos0()), str((e.state["status"], f.pos)))
e, f = setup(bks=books(mn_bid=0.90))
run(e, f)
check("test: a live price worse than the plan stops the whole sale, nothing sent", e.state["status"] == "aborted" and not f.sent, str(e.state.get("why")))
e, f = setup()
f.fail = [AON(), AON(), AON(), AON()]
run(e, f)
check("test: a basket that never fills stops the sale after its retries, each on a new key", e.state["status"] == "aborted" and len(f.sent) == 4 and len({s["key"] for s in f.sent}) == 4,
      str((e.state["status"], len(f.sent))))
e, f = setup()
f.no_move = True
run(e, f)
check("test: positions that do not fall (a YES set does not net as the plan assumes) stop the sale", e.state["status"] == "aborted" and len(f.sent) == 1, str(e.state.get("why")))
e, f = setup()
f.no_cash = True
run(e, f)
check("test: cash that does not arrive stops the sale", e.state["status"] == "aborted" and len(f.sent) == 1, str(e.state.get("why")))
e, f = setup(rows=[r for r in ROWS if r["step"] != 5], target=4000)
run(e, f)
check("test: a list without a YES set needs none", e.state["test"]["status"] == "passed" and e.state["status"] == "done" and f.sent, str(e.state["test"]))
p2 = pos0()
p2["m1"] = p2["m2"] = 0.0
e, f = setup(pos=p2, rows=ROWS, target=5000)
run(e, f)
check("test: the test race no longer held: the sale stops (the plan is stale), it does not hang", e.state["status"] == "aborted", str((e.state["status"], e.state.get("why"))))

# ---------------------------------------------------------------- baskets
_d = OE.OffloadExecutor._proceeds({"q": 100, "legs": [{"action": "sell", "price": 0.96}, {"action": "sell", "price": 0.98}]},
                                  [{"action": "buy", "totalCost": 4.0}, {"action": "buy", "totalCost": 2.0}])
check("proceeds: a sell the venue turned into its complement buy counts as that buy", abs(_d - 1.94) < 1e-9, str(_d))
e, f = setup()
run(e, f)
ri = [s for s in baskets(f) if s["race"] == "RI"]
check("baskets: a large set goes in 4 baskets (ceil 2,000 / 500), at least 60 s apart", len(ri) == 4 and all(b2["t"] - a["t"] >= OE.BASKET_GAP for a, b2 in zip(ri, ri[1:])), str([s["t"] - T0 for s in ri]))
check("baskets: a small set goes in one", len([s for s in baskets(f) if s["race"] == "FL"]) == 1)
check("quotes: every basket is preceded by a pull of its legs", all(
    any(c[0] == "pull" and set(c[1]) == set(RACES[s["race"]]) and c[2] == s["t"] for c in f.calls) for s in baskets(f)))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=1500)
f.pos["r1"] = -900.0
run(e, f)
check("clamp: never more sets than held now (fewer than the plan)", sum(s["q"] for s in baskets(f) if s["race"] == "RI") == 900 and f.pos["r1"] == 0, str(f.pos))
e, f = setup()
f.pull_ok = False
run(e, f, 60)
check("quotes: if they cannot be pulled (an order we did not place) nothing is sent", not f.sent)
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.bal = 300.0
run(e, f)
s0 = baskets(f)[0]
check("cash: a buy that would gross above the cash balance goes as a conventional sell of what is held", all(l["action"] == "sell" and l["side"] == "no" for l in s0["legs"]), str(s0["legs"]))
check("cash: the sell's limit is 1 - the buy's (the same floor)", abs(sum(l["price"] for l in s0["legs"]) - 1.94) < 1e-6, str(s0["legs"]))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.bal, f.sell_ok = 300.0, False
run(e, f, 60)
check("cash: with conventional sells off a basket too big for the cash waits: nothing is sent", not f.sent, str(f.sent[:1]))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
run(e, f)
_wf = [len(s["legs"]) - sum(l["price"] for l in s["legs"]) for s in baskets(f)]
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.floors = {"RI": max(_wf) + 0.5}
run(e, f, 60)
check("floor: a race's set floor above the book sends nothing", not f.sent, str(f.sent[:1]))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.floors = {"RI": min(_wf)}
run(e, f)
check("floor: at a floor the worst level meets, the sale goes and no basket's limits pass it",
      baskets(f) and all(len(s["legs"]) - sum(l["price"] for l in s["legs"]) >= min(_wf) - 1e-9 for s in baskets(f)), str(_wf))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.fail = [AON()]
run(e, f)
k = [s["key"] for s in baskets(f)]
check("retry: a basket that did not fill is resent on a fresh read with a new key, and the sale finishes", len(k) == len(set(k)) and f.pos["r1"] == 0, str(k))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.fail = [AON(), AON(), AON(), AON()]
run(e, f)
check("retry: after its retries the row is given up, not looped", e.st("1")["status"] == "failed" and len(f.sent) == 4, str(e.st("1")))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.fail = [Err(502, "ORDER_STATUS_UNKNOWN"), None]
run(e, f, 20)
check("unknown: a 5xx resends the same key and payload", len(f.sent) >= 2 and f.sent[0]["key"] == f.sent[1]["key"] and f.sent[0]["legs"] == f.sent[1]["legs"], str(f.sent[:2]))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.lose_resp = True
f.fail = []
run(e, f, 400)
check("unknown: a lost response never sells twice: positions end flat, never through zero", f.pos["r1"] == 0, str(f.pos))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.fail = [Err(403, "FORBIDDEN"), Err(403, "FORBIDDEN"), Err(403, "FORBIDDEN")]
run(e, f)
check("rejects: 3 hard rejections in a row stop the sale", e.state["status"] == "aborted" and len(f.sent) == 3, str(e.state.get("why")))
bk = books()
bk["r2"]["asks"] = lad((0.5, 100), (0.6, 90000))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800, bks=bk)
run(e, f, 30)
check("price: a thin book shrinks a basket to what prices at the plan, it never walks past it", baskets(f) and baskets(f)[0]["q"] <= 125, str(baskets(f)[:1]))
bk = books()
bk["r2"]["asks"] = lad((0.56, 90000))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800, bks=bk)
run(e, f, 120)
check("price: a dearer book makes a basket wait, not send", not f.sent and e.st("1")["status"] == "pending")

# ---------------------------------------------------------------- partisan cap
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.cap_fn = lambda b, a: False
run(e, f, 60)
check("cap: a basket the cap refuses waits", not f.sent)
_legs = [r for r in ROWS if r["step"] in (2, 4)]
e, f = setup(rows=_legs, target=150)
f.nd_fn = lambda p: 4950.0 + (p.get("n1", 0.0) - 200.0) * -1.0        # selling n1 YES raises net D by 1 a share
run(e, f, 30)
check("cap: a leg that would pass the cap is held out of the batch", f.pos["n1"] == 200.0 and f.pos["c1"] == 0.0, str(f.pos))
e, f = setup(rows=_legs, target=150)
f.nd_fn = lambda p: 5400.0 + (p.get("n1", 0.0) - 200.0) * 1.0         # above the cap: selling n1 brings it down
run(e, f, 30)
check("cap: a leg that narrows a breach still sells", f.pos["n1"] == 0.0, str(f.pos))

# ---------------------------------------------------------------- legs
e, f = setup(rows=_legs, target=150)
run(e, f, 30)
b = batches(f)
check("legs: legs go by one batch with a short expiry", len(b) == 1 and len(b[0]["batch"]) == 2 and all("expirationDate" in o for o in b[0]["batch"]), str(b))
check("legs: YES held sells by buying NO at 1 - bid; NO held by buying YES at the ask", {(o["exchangeId"], o["side"], o["price"]) for o in b[0]["batch"]} == {("c1", "yes", 0.6), ("n1", "no", 0.4)}, str(b[0]["batch"]))
check("legs: tagged raise", set(p for _, p in f.tags) == {"raise"}, str(f.tags))
e, f = setup(rows=_legs, target=150)
f.fail = [Err(503, "SERVICE_UNAVAILABLE"), None]
run(e, f, 30)
check("legs: an unknown batch resends its key, it is not repriced", len(batches(f)) >= 2 and batches(f)[0]["key"] == batches(f)[1]["key"] and f.pos["n1"] == 0.0, str(batches(f)[:2]))
e, f = setup(rows=_legs, target=150)
f.fail = [[{"ok": True, "status": 201, "data": {"orderId": 7, "quantityTraded": 0, "open": True}},
           {"ok": False, "status": 502, "data": {"code": "ORDER_STATUS_UNKNOWN"}}]]
run(e, f, 8)
check("legs: an item of unknown outcome holds the batch's key (a repriced order could sell through zero)",
      len({s["key"] for s in batches(f)}) == 1, str(batches(f)))
e, f = setup(rows=_legs, target=150)
f.fail = [Err(503, "SERVICE_UNAVAILABLE"), Err(409, "REQUEST_IN_FLIGHT"), Err(409, "REQUEST_IN_FLIGHT"), Err(502, "X")] + [Err(503, "Y")] * 20
run(e, f, 4)
n0 = {s["key"] for s in batches(f)}
run(e, f, 34)
check("legs: in flight is not a rejection, and it keeps the key", e.state["hard"] == 0 and {s["key"] for s in batches(f)} == n0, str([s["key"] for s in batches(f)]))
e, f = setup(rows=_legs, target=150)
_w = f.lock
def _slow():
    f.t += 45.0
    return _w()
f.lock = _slow
run(e, f, 6)
_o = batches(f)[0]["batch"][0]
check("legs: a slow lock never sends an expired order (the expiry is stamped under the lock)",
      datetime.fromisoformat(_o["expirationDate"].replace("Z", "+00:00")).timestamp() >= batches(f)[0]["t"] + OE.TTL - 1, str((_o, batches(f)[0]["t"])))
e, f = setup(rows=[row(2, "CO D NO", "leg_held", 100, 40.0, ["c1"], [0.40])], target=30)
f.fail = [[{"ok": True, "status": 201, "data": {"orderId": 9, "quantityTraded": 40, "open": True}}]]
run(e, f, 60)
check("legs: a remainder is cancelled before the row is sent again", 9 in f.cancels, str(f.cancels))

# ---------------------------------------------------------------- double sales: a fake with idempotent keys and resting orders
_one = [row(4, "NV R YES", "leg", 200, 120.0, ["n1"], [0.60])]
_big = dict(pos0(), n1=500.0)
e, f = setup(rows=_one, target=100, pos=_big)
f.rest, f.lose_batch = True, True
f.fail = [None, Err(403, "FORBIDDEN")]
run(e, f, 6)
k0 = {s["key"] for s in batches(f)}
run(e, f, 20)
f.fill_resting()
run(e, f, 8)
check("unknown then rejected while it rests: no new order until it is dead (the gate on amb_until)", {s["key"] for s in batches(f)} == k0, str([s["t"] - T0 for s in batches(f)]))
f.rest = False
run(e, f, 120)
check("unknown then rejected: the fill is read and the row is not sold again: 200 of 200, 300 left", f.pos["n1"] == 300.0 and e.st("4")["done"] == 200, str((f.pos["n1"], e.st("4"))))
e, f = setup(rows=_one, target=100, pos=_big)
f.lose_batch = True
f.fail = [None] + [Err(503, "X")] * 40
run(e, f, 200)
check("a lost batch that filled, dropped at expiry: the row marked unknown is not sold again", f.pos["n1"] == 300.0, str((f.pos["n1"], e.st("4"))))
e, f = setup(rows=_one, target=100, pos=_big)
f.rest = True
run(e, f, 6)
f.fill_resting()
f.rest = False
run(e, f, 100)
check("a remainder that filled after its response: not sold again", f.pos["n1"] == 300.0 and e.st("4")["done"] == 200, str((f.pos["n1"], e.st("4"))))
e, f = setup(rows=_one, target=100, pos=_big)
f.rest = True
run(e, f, 6)
f.lag_pos, f.lag_left = dict(f.pos), 1      # the fill lands, the first read after it does not show it
f.fill_resting()
f.rest = False
f.t = T0 + 34
run(e, f, 100)
check("a position read that lags a fill by one read does not resell it (two reads must agree)", f.pos["n1"] == 300.0, str((f.pos["n1"], e.st("4"))))
_two = [row(4, "NV R YES", "leg", 200, 120.0, ["n1"], [0.60]), row(2, "CO D NO", "leg_held", 100, 40.0, ["c1"], [0.40])]
e, f = setup(rows=_two, target=150, pos=dict(_big))
f.fail = [[{"ok": True, "status": 201, "data": {"orderId": 9, "quantityTraded": 200, "open": False}}, {"ok": False, "status": 503, "data": {}}],
          [{"ok": True, "status": 201, "data": {"orderId": 9, "quantityTraded": 200, "open": False}}, {"ok": True, "status": 201, "data": {"orderId": 10, "quantityTraded": 0, "open": True}}]]
run(e, f, 8)
check("a resend replays settled items: they are counted once", abs(e.state["raised"] - 120.0) < 1e-6 and len({s["key"] for s in batches(f)}) == 1, str((e.state["raised"], [s["key"] for s in batches(f)])))
e, f = setup(rows=_one, target=100, pos=_big)
f.fail = [[]] + [Err(503, "X")] * 40
run(e, f, 20)
check("a 2xx that leaves items out is unknown, not done", len({s["key"] for s in batches(f)}) == 1 and e.st("4")["unk"], str(e.st("4")))
e, f = setup(rows=_one, target=100, pos=_big)
_at_send = {}
_b0 = f.batch
def _crash(orders, key):
    _at_send["state"] = open(e.state_path).read()        # the file as it stood when the process died inside the POST
    return _b0(orders, key)
f.batch = _crash
e.tick()
f.batch = _b0
open(e.state_path, "w").write(_at_send["state"])
f.t += 200
e2 = OE.OffloadExecutor(e.plan_path, e.state_path, e.sha, f, 100, "5 min", START, STOP)
run(e2, f, 120)
check("a crash after the POST: the restart does not sell the row again", f.pos["n1"] == 300.0, str((f.pos["n1"], e2.st("4"))))
e, f = setup(rows=_one, target=100, pos=_big)
f.fail = [Err(503, "X")] * 40
e.tick()
_exp = e.state["pending"]["exp"]
f.t = _exp + 1.0
e.tick()
check("a batch is dropped only once its orders are dead (expiry + 2 s), not before", e.state["pending"] is not None, str(e.state["pending"]))
_cap2 = [row(4, "NV R YES", "leg", 200, 120.0, ["n1"], [0.60]), row(2, "CO D NO", "leg_held", 100, 40.0, ["c1"], [0.40])]
e, f = setup(rows=_cap2, target=100)
f.nd_fn = lambda p: 4700.0 + (200.0 - p.get("n1", 0.0)) * 1.0 + (100.0 + p.get("c1", 0.0)) * 2.0     # each widens: 200 then 200
f.rest = True
e.st("2")["next"] = T0 + 10.0
run(e, f, 20)
check("cap: another row's remainder counts in the next batch, it is not left out (300 + 200 would pass 5,000)", len(batches(f)) == 1 and not f.cancels, str((len(batches(f)), f.cancels)))

# ---------------------------------------------------------------- pacing
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.fail = [Err(409, "REQUEST_IN_FLIGHT")] * 50
run(e, f, 60)
check("pacing: a key in flight waits the spec's 90 s, it is not resent every tick", len(f.sent) == 1, str(len(f.sent)))
run(e, f, 30)
check("pacing: a basket in flight is not repriced under a new key inside the 90 s lease", len({s["key"] for s in f.sent}) == 1, str([s["key"] for s in f.sent]))
_e429 = Err(429, "RATE_LIMITED")
_e429.retry_after = 30.0
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.fail = [_e429, None]
run(e, f, 20)
n = len(f.sent)
run(e, f, 16)
check("pacing: Retry-After is honoured", n == 1 and len(f.sent) >= 2, str((n, len(f.sent))))
e, f = setup(rows=_legs, target=150)
f.fail = [Err(503, "X")] * 30
run(e, f, 28)
check("pacing: a batch's resends back off", len(batches(f)) <= 5, str(len(batches(f))))
e, f = setup(writes_per_min=3)
f.pull_writes = 1
run(e, f, 58)
check("pacing: pulls count against the write limit", len(e.state["writes"]) <= 3 + 1, str(len(e.state["writes"])))
e, f = setup(rows=_legs, target=150)
run(e, f, 8)
check("snapshot: after_batch runs", f.after == 1, str(f.after))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.pull_writes = 2
run(e, f, 2)
check("pacing: a pull's cancels are counted as writes, with the send", len(e.state["writes"]) == 3, str(e.state["writes"]))
e, f = setup()
e.rows = [dict(r, type="set_late", kind="buy_set", race=n, qty=q) for r, n, q in zip(ROWS, ("TX", "NV"), (2, 318))]
check("test: without MN-05 the list's largest YES set is the test", e._test_row()["qty"] == 318, str(e._test_row()))
e, f = setup(rows=_legs, target=150)
f.fail = [Err(503, "X"), None]
run(e, f, 2)
_lk = f.lock
def _k2():
    f.block = "kill switch"
    return _lk()
f.lock = _k2
n = len(f.sent)
run(e, f, 20)
check("gate: a resend whose lock wait met the kill switch does not go", len(f.sent) == n, str((n, len(f.sent))))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
_lk2 = f.lock
def _late():
    f.t = e.t1 + 1.0
    return _lk2()
f.lock = _late
run(e, f, 4)
check("gate: a window that closed during the lock wait stops the send", not f.sent)

# ---------------------------------------------------------------- rows that would hold a race for ever
e, f = setup(rows=_legs, target=150)
f.fail = [[{"ok": True, "status": 201, "data": {"orderId": None, "quantityTraded": 0, "open": False}},
           {"ok": True, "status": 201, "data": {"orderId": None, "quantityTraded": 0, "open": False}}]] * 200
run(e, f, OE.STUCK_MAX + 60)
check("stuck: orders accepted with no fill (orderId null) end the row in time", e.state["status"] == "done", str((e.state["status"], e.st("2"))))
e, f = setup(rows=_legs, target=150)
f.bal = 1.0
run(e, f, OE.STUCK_MAX + 60)
check("stuck: a row short of cash ends in time, its race goes back", e.state["status"] == "done" and e.races() == set(), e.state["status"])

# ---------------------------------------------------------------- cheap favourites
cf_row = row(6, "MD D YES", "cheapfav", 400, 384.0, ["d1"], [0.96])
_p = dict(pos0(), d1=400.0)
e, f = setup(rows=[cf_row], target=380, pos=_p)
f.cf = {"d1": 400.0}
f.cf_block = "the cheap-favourites sleeve is not paused"
run(e, f, 120)
check("cheap favourites: not sold while the sleeve is not paused", not f.sent and f.pos["d1"] == 400.0)
check("preflight: rows that cannot sell do not count toward the target", e.state["status"] == "aborted", str(e.state.get("why")))
e, f = setup(rows=[ROWS[0], cf_row], target=3000, pos=_p)
f.cf = {}
run(e, f, 8)
f.cf = {"d1": 400.0}
run(e, f, 60)
check("zero read: one empty ledger read does not end a row for good", f.pos["d1"] == 0.0, str((f.pos["d1"], e.st("6"))))
_shared = [row(6, "MD D YES", "cheapfav", 300, 288.0, ["d1"], [0.96]), row(7, "MD D YES", "leg_held", 100, 96.0, ["d1"], [0.96])]
e, f = setup(rows=_shared, target=380, pos=_p)
f.cf = {"d1": 300.0}
f.cf_lag = True
e.st("6")["next"] = T0 + 10.0               # row 7 is seen first, so its base is set before the sleeve's row sells
_ob = f.books["d1"]
f.books["d1"] = {"bids": lad((0.90, 9000)), "asks": _ob["asks"]}
run(e, f, 6)
f.books["d1"] = _ob
e.st("7")["next"] = T0 + 30.0
run(e, f, 24)
_r = e.state["raised"]
check("shared leg: while the sleeve's fills are unread, its sale is not booked as a sale of the other row", f.pos["d1"] == 100.0 and e.st("7")["done"] == 0 and _r < 300, str((f.pos["d1"], e.st("7"), _r)))
f.cf = {}
run(e, f, 40)
check("shared leg: once read, the other row sells", f.pos["d1"] == 0.0, str(f.pos["d1"]))

# ---------------------------------------------------------------- other sleeves
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.busy_r = {"RI": "shock mode"}
run(e, f, 60)
check("busy: a race a punt, shock or high-frequency arb position sits in is left alone", not f.sent)
e, f = setup()
f.block = "exit:raise paused"
run(e, f, 60)
check("stops: exit:raise paused or the kill switch hold every send", not f.sent and e.state["status"] != "aborted")
f.block = None
run(e, f, 400)
check("stops: the sale resumes inside its window when the hold clears", e.state["status"] == "done", e.state["status"])
e, f = setup()
run(e, f, 20)
n = len(f.sent)
f.block = "exit:raise paused"
run(e, f, OE.HELD_MAX + 30)
check("stops: a long hold ends the sale and gives its races back", e.state["status"] == "aborted" and e.races() == set() and len(f.sent) == n, str(e.state.get("why")))
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
_w2 = f.lock
def _kill():
    f.block = "kill switch"
    return _w2()
f.lock = _kill
run(e, f, 6)
check("stops: a kill switch set while the lock is awaited stops the send", not f.sent)
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
_w3 = f.lock
def _ab():
    f.abort_flag = True
    return _w3()
f.lock = _ab
run(e, f, 6)
check("stops: an abort file written while the lock is awaited stops the send", not f.sent and e.state["status"] == "aborted")
e, f = setup(rows=[r for r in ROWS if r["step"] in (1, 3)], target=300)
f.busy_r = {"RI": "high-frequency arb"}
run(e, f, 20)
check("busy: a race another sleeve owns is not kept from it (churn's exit skips punt races)", "RI" not in e.races(), str(e.races()))
run(e, f, OE.STUCK_MAX + 30)
check("stuck: a row that cannot sell is given up in time, so the sale ends", e.st("1")["status"] == "failed" and e.state["status"] == "done", str(e.st("1")))
e, f = setup(pos={"m1": 100.0, "m2": 100.0})
run(e, f, 20)
check("preflight: positions far from the plan (under 80% of the target) stop the sale before any send", e.state["status"] == "aborted" and not f.sent, str(e.state.get("why")))

# ---------------------------------------------------------------- abort and restart
e, f = setup(rows=[row(2, "CO D NO", "leg_held", 100, 40.0, ["c1"], [0.40])], target=30)
f.fail = [[{"ok": True, "status": 201, "data": {"orderId": 9, "quantityTraded": 40, "open": True}}]]
run(e, f, 4)
f.abort_flag = True
n = len(f.sent)
run(e, f, 60)
check("abort: the file stops the sale, cancels what rests, sends no more", e.state["status"] == "aborted" and 9 in f.cancels and len(f.sent) == n, str(f.cancels))
check("abort: its races go back to the other sleeves", e.races() == set())
e, f = setup()
run(e, f)
e2 = OE.OffloadExecutor(e.plan_path, e.state_path, e.sha, f, TARGET, "5 min", START, STOP)
n = len(f.sent)
f.pos["r1"] = -500.0
f.pos["r2"] = -500.0
f.pos["r3"] = -500.0
f.t = T0 + 500
run(e2, f, 100)
check("restart: a finished sale never runs again from its state file", len(f.sent) == n and e2.state["status"] == "done")
e, f = setup(rows=[r for r in ROWS if r["step"] == 1], target=3800)
f.fail = [Err(502, "ORDER_STATUS_UNKNOWN")]
run(e, f, 2)
pend = e.state["pending"]
e2 = OE.OffloadExecutor(e.plan_path, e.state_path, e.sha, f, 3800, "5 min", START, STOP)
f.fail = []
run(e2, f, 6)
check("restart: an unconfirmed basket resumes on its saved key", pend and baskets(f)[1]["key"] == pend["key"], str(pend))
open(e.state_path, "w").write("{bad")
e3 = OE.OffloadExecutor(e.plan_path, e.state_path, e.sha, f, 3800, "5 min", START, STOP)
check("restart: an unreadable state file refuses (it may hold a finished sale), never starts fresh", bool(e3.refused), str(e3.refused))

print("\n%d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
