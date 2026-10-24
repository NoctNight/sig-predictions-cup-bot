"""An exit crossing zero: zero-EV exits send twice the quantity they sized (the 22 Oct double-exit class of bug), so
they close past zero and flip the holding. Caught: the zero-EV exit path is paused (and its owners' entries)."""
SCENARIO = "exits"
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import stop_pauses, calls_after  # noqa: E402

R = mods["run"]
_orig = R.Bot.zero_ev_exits


def _zx(self, positions, rows, write_budget=2):
    api = self.api
    real = api.place_order

    def doubled(ex, side, action, qty, price, **k):
        return real(ex, side, action, qty * 2, price, **k)
    api.place_order = doubled
    try:
        return _orig(self, positions, rows, write_budget)
    finally:
        api.place_order = real


R.Bot.zero_ev_exits = _zx


def caught(sim):
    k = stop_pauses(sim)
    if "exit:zero_ev" not in k:
        return False, f"zero-EV exits not paused (pauses: {sorted(k)})"
    late = calls_after(sim, "exit", k["exit:zero_ev"]["ts"] + 1)
    return (not late), f"paused {sorted(k)}; zero-EV exits after the pause: {len(late)}"
