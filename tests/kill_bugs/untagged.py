"""An order booked to no strategy: a code path inside the MM's quote cycle crosses the spread both ways on its first
market every cycle (2,000 shares) and never records the orders. Caught: a pause that stops it (the MM, or all entries)."""
SCENARIO = "calm"
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import round_trip, stop_pauses, calls_after  # noqa: E402

MM = mods["mm"].MarketMaker
_orig = MM.quote_cycle


def _qc(self, books, *a, **k):
    out = _orig(self, books, *a, **k)
    if len(books) > 1 and self.quoted and not self.dry_run:
        ex = self.quoted[0]
        b = books.get(ex) or {}
        if b.get("bestBid") and b.get("bestAsk"):
            round_trip(self.api, ex, b["bestBid"], b["bestAsk"], 2000, "rogue")
    return out


MM.quote_cycle = _qc


def caught(sim):
    k = stop_pauses(sim)
    stopper = [e for n, e in k.items() if n in ("mm", "all")]
    if not stopper:
        return False, f"no MM / all pause (pauses: {sorted(k)})"
    t = min(e["ts"] for e in stopper)
    late = calls_after(sim, "rogue", t + 60)
    return (not late), f"paused {sorted(k)}; untagged orders after the pause: {len(late)}"
