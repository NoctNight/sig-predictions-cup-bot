"""The recycler pump (closed 24 Oct by protecting high-frequency arb's races): the recycler may unwind the sets churn
holds and buy into churn races, so the two sleeves trade the same sets back and forth through the spread. Both
protections (arb.protected, arb.no_buy) are switched off. Caught: the recycler (arb), high-frequency arb or all entries
paused, and no recycle into a churn race after that pause."""
SCENARIO = "churn_blowout"
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import stop_pauses  # noqa: E402

R = mods["run"]
_init = R.Bot.__init__


def _i(self, *a, **k):
    _init(self, *a, **k)
    self.arb.protected = lambda: set()
    self.arb.no_buy = lambda race: False


R.Bot.__init__ = _i


def caught(sim):
    k = stop_pauses(sim)
    hit = sorted(n for n in k if n in ("arb", "hf_arb", "all"))
    return bool(hit), f"pauses {sorted(k)}; recycles {getattr(sim.sc, 'summary', {}).get('recycle_buys')} buys into churn races"
