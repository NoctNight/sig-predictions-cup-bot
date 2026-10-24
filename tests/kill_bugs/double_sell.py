"""Double sells (23-24 Oct class): every high-frequency arb exit is sent a second time on churn's next cycle, after the
book has refilled, so the second unwind buys YES on legs whose NO the first one already closed (what the stale-snapshot
bugs did). Caught: the unwinds exit path and the set strategies are paused at the first unwind leg that ends long YES."""
SCENARIO = "churn"
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import stop_pauses  # noqa: E402

A = mods["arb"].ArbEngine
R = mods["run"]
_unw = A.unwind_set
AGAIN = []


def _note(self, rname, books, qty, why="offload", *a, **k):
    r = _unw(self, rname, books, qty, why, *a, **k)
    if r and not r.get("dry") and str(why).startswith("churn:") and "(again)" not in str(why):
        AGAIN.append((self, rname, qty, why, a, k))
    return r


A.unwind_set = _note
_cc = R.Bot.churn_cycle


def _cc2(self, positions, killed, cycle_id=None):
    while AGAIN:
        eng, rname, qty, why, a, k = AGAIN.pop(0)
        books = {e: self.get_book(e, 50) for e in self.universe.races[rname].exchange_ids}
        _unw(eng, rname, books, qty, why + " (again)", *a, **k)
    return _cc(self, positions, killed, cycle_id)


R.Bot.churn_cycle = _cc2


def caught(sim):
    k = stop_pauses(sim)
    hit = sorted(n for n in k if n in ("exit:unwinds", "hf_arb", "arb", "all"))
    return bool(hit) and "exit:unwinds" in k, f"pauses {sorted(k)}"
