"""Not a bug: a shock-sized move that IS attributed (acceptance (e)). At cycle 120 SIG and the outside fair jump 15 points
against every cheap-favourite holding (both legs of each race). The loss is cheap favourites' own: at most cheap
favourites may be paused; nothing else, never all entries, and the unattributed loss stays near 0."""
SCENARIO = "cheapfav"
NOT_A_BUG = True        # (e): today's switch stops everything on a big attributed loss; the new design only its owners
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import stop_pauses  # noqa: E402

SPm = sys.modules.get("sim_pipeline") or sys.modules["__main__"]
_bc = SPm.CheapFav.before_cycle
DONE = {}


def _b(self, sim, i):
    _bc(self, sim, i)
    if i == 120:
        v = sim.v
        for ex, q in sorted((getattr(sim.bot, "cheapfav_open", None) or {}).items()):
            race = sim.universe.race_of(ex)
            if race is None or ex in DONE:
                continue
            d = -30 if q > 0 else 30                       # 15 points against the holder
            for e in race.exchange_ids:
                if e in DONE:
                    continue
                dd = d if e == ex else -d
                v.shift(e, dd)
                if e in v.outside:
                    v.outside[e] = round(min(0.99, max(0.01, v.outside[e] + dd * 0.005)), 4)
                DONE[e] = dd


SPm.CheapFav.before_cycle = _b
UN = []
_ac = SPm.CheapFav.after_cycle


def _a(self, sim, i):
    _ac(self, sim, i)
    s = getattr(sim.bot, "stops", None)
    u = (getattr(s, "last", {}) or {}).get("status", {}).get("unattributed") if s is not None else None
    if u is not None:
        UN.append(u)


SPm.CheapFav.after_cycle = _a


def caught(sim):
    """Correct behaviour: a strategy is paused only for its OWN loss (the stop / drop rules, or the monitor's evidence);
    nothing through the unattributed, bypass or crossing rules, never all entries, unattributed loss ~0 throughout."""
    k = stop_pauses(sim)
    st = getattr(getattr(sim.bot, "stops", None), "last", {}).get("status", {}) if getattr(sim.bot, "stops", None) else {}
    un = st.get("unattributed")
    pnl = st.get("pnl") or {}
    # own-loss rules; the bot-wide backstop counts too for a strategy that itself lost (it pauses only those)
    wrong = sorted(n for n, e in k.items() if n == "all" or e.get("rule") not in ("stop", "drop", "markout", "sleeve", "combined")
                   or (e.get("rule") == "combined" and pnl.get(n, 0.0) > -100))
    worst = min(UN) if UN else None
    ok = not wrong and (worst is None or worst > -100)
    return ok, (f"moved {len(DONE)} legs; stop pauses {sorted((n, e.get('rule')) for n, e in k.items())}; paused for "
                f"someone else's loss: {wrong or 'none'}; worst unattributed during the run {worst}; P&L since armed {st.get('pnl')}")
