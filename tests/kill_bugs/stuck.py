"""A stuck resting order: the venue keeps MM quotes far past the expiry the bot sent, and the MM stops quoting one of
its markets at cycle 10 (it never cancels quotes on a market it dropped), so those quotes rest forever. Caught: the
stops cancel them and pause the MM's entries."""
SCENARIO = "calm"
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import stop_pauses  # noqa: E402
import sim_venue as SV  # noqa: E402

_val = SV.Venue._validate


def _v(self, ex, side, action, qty, price, expiration, key, aon=False):
    exp = _val(self, ex, side, action, qty, price, expiration, key, aon)
    if exp is not None and str(key).startswith("bat"):
        exp += 30 * 86400
    return exp


SV.Venue._validate = _v
R = mods["run"]
_cyc = R.Bot.cycle
DROP = {}


def _cycle(self):
    _cyc(self)
    n = DROP.setdefault("n", 0) + 1
    DROP["n"] = n
    if n == 10 and self.mm.quoted:
        DROP["ex"] = self.mm.quoted[0]
    if DROP.get("ex") in self.mm.quoted:
        self.mm.quoted = [e for e in self.mm.quoted if e != DROP["ex"]]


R.Bot.cycle = _cycle


def caught(sim):
    k = stop_pauses(sim)
    ex = DROP.get("ex")
    left = [o for o in sim.v.orders.values() if o.open and o.ex == ex and o.prefix == "bat"]
    if "mm" not in k:
        return False, f"MM not paused (pauses {sorted(k)}); stuck quotes on {ex} still open: {len(left)}"
    return (not left), f"paused {sorted(k)}; stuck quotes on {ex} still open at the end: {len(left)}"
