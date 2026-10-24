"""A mis-tagged order: a code path inside the catch-up sleeve crosses the spread both ways every cycle (2,000 shares)
and records the orders as purpose 'mm'. The MM's ledger takes the loss and the MM is paused, but the orders keep coming
(the catch-up is not paused): the bypass check must pause every entry. Caught: no rogue order after that."""
SCENARIO = "calm"
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import round_trip, stop_pauses, calls_after  # noqa: E402

R = mods["run"]
_orig = R.Bot.catchup_cycle


def _cc(self, dir_summary, positions, prices, killed):
    _orig(self, dir_summary, positions, prices, killed)
    if killed or R.strategy_paused(self, "catchup") or self.dry_run or not self.mm.quoted:
        return
    ex = self.mm.quoted[0]
    p = prices.get(ex) or {}
    if p.get("bestBid") and p.get("bestAsk"):
        round_trip(self.api, ex, p["bestBid"], p["bestAsk"], 2000, "rogue",
                   record=lambda oid, ex_, side, px, q: self.store.record_order(oid, ex_, None, side, "buy", px, q, "mm"))


R.Bot.catchup_cycle = _cc


def caught(sim):
    k = stop_pauses(sim)
    if "all" not in k:
        return False, f"no KILL-ENTRIES (pauses: {sorted(k)})"
    late = calls_after(sim, "rogue", k["all"]["ts"] + 60)
    return (not late), f"paused {sorted(k)}; mis-tagged orders after KILL-ENTRIES: {len(late)}"
