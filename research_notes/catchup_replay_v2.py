"""Replay of the catch-up sleeve's own code (sigbot/proposals/outside_sweep.py) over every raw poll, old exits against new exits.
Successor to research_notes/dir_alpha/d19.py (31 Oct), which replayed the old code; this one drives the NEW CatchupSweep.signals()
and CatchupSweep.exit_reason() themselves, so what it measures is what the bot will do. Entry at the next poll's touch (within 4 min),
exit at the touch of the poll where the rule fires. Size and position limits are ignored in the per-trade columns (cents a share);
the dollar columns size each trade with CatchupSweep.target_notional and charge 0.02c per 1,000 shares each way (FINDINGS "Size").

    python3 research_notes/catchup_replay_v2.py /path/to/sigbot.sqlite [net_d_start]

Prints the exit comparison, the kind / points / right-side splits, and the partisan-cap capacity (net D starting at net_d_start,
default +4965, the 07:46 UTC 31 Oct reading): the share of signals and of notional that can trade, simulated in time order with
each trade's net D released at its exit."""
import sqlite3, sys, json, os, numpy as np, pandas as pd

sys.path.insert(0, ".")
from sigbot.proposals import outside_sweep as OS

pd.set_option("display.width", 220, "display.max_rows", 200, "display.max_columns", 40)
DB = sys.argv[1]
NET_D0 = float(sys.argv[2]) if len(sys.argv) > 2 else 4965.0
db = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
v = pd.read_sql_query("select ts,race,party,pm,kalshi,sig_bid,sig_ask,exchange_id from venue_fair where exchange_id is not null order by ts", db)
v["sig_mid"] = (v.sig_bid + v.sig_ask) / 2
polls = {t: g for t, g in v.groupby("ts", sort=True)}
ts = sorted(polls)
print("rows", len(v), "polls", len(ts), flush=True)
split = ts[0] + .5 * (ts[-1] - ts[0])
LIVE_FROM = pd.Timestamp("2026-10-24 19:00", tz="UTC").timestamp()
model = json.load(open("params/fair_model.json"))
key = ["exchange_id", "race", "party", "pm", "kalshi", "sig_bid", "sig_ask", "sig_mid"]


def collect(own_max):
    """Signals of the new code over all polls; own_max = 2 switches the slow 70c filter off (the old signal set, tagged)."""
    OS.SLOW_OWN_MAX = own_max
    c = OS.CatchupSweep(notional=OS.CatchupSweep().notional, max_positions=10 ** 6)
    c.max_shares = 10 ** 9
    c.inputs = OS.inputs_from_model(model)
    sig = []
    for i, t in enumerate(ts):
        g = polls[t]
        rows = [dict(zip(key, x), ts=t) for x in g[key].itertuples(index=False, name=None)]
        for r in rows:
            for k_ in ("pm", "kalshi", "sig_bid", "sig_ask", "sig_mid"):
                if r[k_] != r[k_]:
                    r[k_] = None
        c.push(rows)
        for s in c.signals(rows, t):
            c.last_signal[s["race"]] = t
            sig.append(dict(s, ts=t, i=i))
        if i % 4000 == 0:
            print(" poll", i, "signals", len(sig), flush=True)
    return c, sig


byleg = {ex: g.sort_values("ts") for ex, g in v.groupby("exchange_id")}


def enter(sig):
    out = []
    for s in sig:
        g = byleg[s["exchange_id"]]
        t = g.ts.values
        e = np.searchsorted(t, s["ts"], side="right")
        if e >= len(t) or t[e] - s["ts"] > 240:
            continue
        b, a = g.sig_bid.values, g.sig_ask.values
        if not (np.isfinite(a[e]) and np.isfinite(b[e])):
            continue
        d = s["d"]
        px = a[e] if d > 0 else 1 - b[e]
        out.append(dict(s=s, e=e, g=g, t=t, b=b, a=a, m=g.sig_mid.values, pm=g.pm.values, k=g.kalshi.values, d=d, px=px))
    return out


def run(out, rule, name):
    """rule(o, i, age) -> True when the position is closed at poll i. Returns a frame, one row per trade."""
    res = []
    for o in out:
        s, e, t, b, a, d, px = o["s"], o["e"], o["t"], o["b"], o["a"], o["d"], o["px"]
        xval = hrs = why = None
        for i in range(e + 1, len(t)):
            if not (np.isfinite(a[i]) and np.isfinite(b[i])):
                continue
            val = (b[i] if d > 0 else 1 - a[i]) - px
            age = t[i] - s["ts"]
            w = rule(o, i, age, val)
            if w:
                xval, hrs, why = val, age / 3600, w
                break
        if xval is None:
            continue
        shares = min(OS.CatchupSweep().target_notional(s) / max(px, .01), 15000.0)
        res.append(dict(net=xval, hrs=hrs, why=why, kind=s["kind"], pts=s["pts"], right=s["right"], train=s["ts"] < split, party=s["party"], d=d,
                        race=s["race"], day=pd.Timestamp(s["ts"], unit="s").strftime("%m-%d"), ts=s["ts"], t_exit=s["ts"] + hrs * 3600, px=px, shares=shares,
                        dollars=xval * shares - 2 * 0.0002 * (shares / 1000.0) * shares,
                        dollars_nocost=xval * shares, adds=("D" if (s["party"] == "D") == (d > 0) else "R")))
    Q = pd.DataFrame(res)
    Q["rule"] = name
    return Q


def old_rule(o, i, age, val):
    s, m, pm, k, d = o["s"], o["m"], o["pm"], o["k"], o["d"]
    if age >= 12 * 3600:
        return "12h"
    if (m[i] - s["mid"]) * d >= .70 * s["gap"]:
        return "converged"
    if np.isfinite(pm[i]) and np.isfinite(k[i]):
        if ((pm[i] + k[i]) / 2 - s["f_then"]) * d < OS.REVERT * abs(s["move"]):
            return "reverted"
        if abs(pm[i] - k[i]) > OS.AGREE:
            return "disagree"
    return None


def new_rule_factory(cs, disagree=True, tp=OS.TAKE_PROFIT_SLOW, fast_h=8, slow_h=20, tp_fast=None):
    """The bot's exit_reason on a row built from the poll; options switch parts off for the comparison."""
    def rule(o, i, age, val):
        s, d = o["s"], o["d"]
        kind = s["kind"]
        if age >= (fast_h if kind == "fast" else slow_h) * 3600:
            return "time"
        pm, k = o["pm"][i], o["k"][i]
        if kind == "slow" and tp is not None and val >= tp - 1e-9:
            return "take-profit"
        if tp_fast is not None and kind == "fast" and val >= tp_fast - 1e-9:
            return "take-profit"
        if np.isfinite(pm) and np.isfinite(k):
            if disagree and abs(pm - k) > OS.AGREE:
                return "disagree"
            if ((pm + k) / 2 - s["f_then"]) * d < OS.REVERT * abs(s["move"]):
                return "reverted"
        return None
    return rule


def summary(Q, label):
    d_ = Q.groupby("day").net.mean()
    return dict(rule=label, n=len(Q), races=Q.race.nunique(), cents=100 * Q.net.mean(), win=(Q.net > 0).mean(), hrs=Q.hrs.mean(), train=100 * Q[Q.train].net.mean(),
                test=100 * Q[~Q.train].net.mean(), days="%d/%d" % ((d_ > 0).sum(), len(d_)), worst=100 * Q.net.min(), usd=Q.dollars.sum(), usd_nocost=Q.dollars_nocost.sum(),
                avg_shares=Q.shares.mean())


def table(rows):
    R = pd.DataFrame(rows)
    print(R.round(2).to_string(index=False))


def verify_exit_reason(cs, out, rule, n=300):
    """The replay's rules and CatchupSweep.exit_reason agree: sample positions' poll rows through the real method."""
    bad = tot = 0
    rng = np.random.default_rng(1)
    for o in rng.choice(out, size=min(n, len(out)), replace=False):
        s, e, t, d, px = o["s"], o["e"], o["t"], o["d"], o["px"]
        cs.ctx = {s["exchange_id"]: {"ts": s["ts"], "d": d, "f_then": s["f_then"], "move": s["move"], "mid0": s["mid"], "gap": s["gap"], "kind": s["kind"]}}
        avg_yes = px if d > 0 else 1 - px
        qty = 1000.0 * d
        for i in range(e + 1, min(len(t), e + 400)):
            if not (np.isfinite(o["a"][i]) and np.isfinite(o["b"][i])):
                continue
            val = (o["b"][i] if d > 0 else 1 - o["a"][i]) - px
            age = t[i] - s["ts"]
            row = dict(ts=t[i], pm=o["pm"][i] if np.isfinite(o["pm"][i]) else None, kalshi=o["k"][i] if np.isfinite(o["k"][i]) else None,
                       sig_mid=o["m"][i], sig_bid=o["b"][i], sig_ask=o["a"][i])
            mine = rule(o, i, age, val)
            real = cs.exit_reason(s["exchange_id"], row, t[i], avg_yes, qty)
            tot += 1
            if bool(mine) != bool(real):
                bad += 1
            if mine or real:
                break
    return bad, tot


def capacity(Q, net_d0, label):
    """Time-ordered replay of the partisan room: each trade is sized to min(target, room under the cap less 200); its net D counts until its exit."""
    ev = []
    for r in Q.itertuples():
        ev.append((r.ts, 1, r))
    open_ = []   # (t_exit, signed notional)
    net = net_d0
    full = part = none = 0
    want = got = 0.0
    byside = {"D": [0, 0], "R": [0, 0]}
    for t, _, r in sorted(ev, key=lambda x: x[0]):
        keep = []
        for te, nd in open_:
            if te <= t:
                net -= nd
            else:
                keep.append((te, nd))
        open_ = keep
        tgt = r.shares * r.px
        room = OS.CatchupSweep.room(r.adds, net)
        take = min(tgt, room, 1e12)
        want += tgt
        got += take
        sh = take / max(r.px, .01)
        okk = sh >= 100
        byside[r.adds][0] += 1
        if okk:
            byside[r.adds][1] += 1
            nd = take if r.adds == "D" else -take
            net += nd
            open_.append((r.t_exit, nd))
        if not okk:
            none += 1
        elif take >= .9 * tgt:
            full += 1
        else:
            part += 1
    n = len(Q)
    print("%s net D start %+.0f: %d signals, %d full size (%.0f%%), %d part (%.0f%%), %d none (%.0f%%); notional %.0f%% of wanted; D-adding %d (%d traded), R-adding %d (%d traded)" % (
        label, net_d0, n, full, 100 * full / n, part, 100 * part / n, none, 100 * none / n, 100 * got / want, byside["D"][0], byside["D"][1], byside["R"][0], byside["R"][1]))
    return dict(n=n, full=full, part=part, none=none, share_notional=got / want)


if __name__ == "__main__":
    own_default = OS.SLOW_OWN_MAX
    cs_new, sig_new = collect(own_default)
    out_new = enter(sig_new)
    _, sig_all = collect(2.0)                                   # the old signal set (no 70c filter), tagged
    out_all = enter(sig_all)
    print("signals with the 70c slow filter", len(sig_new), "entered", len(out_new), "| without it", len(sig_all), "entered", len(out_all), flush=True)
    OS.SLOW_OWN_MAX = own_default
    cs = OS.CatchupSweep()
    rows = []
    print("\n=== A. same signals (old signal set, no 70c filter): old exits against new exits ===")
    for lab, rule in (("OLD exits: 70% converged, 12 h, revert, disagree", old_rule),
                      ("NEW exits: fast 8 h, slow 20 h, slow TP +3c, revert, disagree", new_rule_factory(cs)),
                      ("NEW without the disagree exit", new_rule_factory(cs, disagree=False)),
                      ("NEW, slow TP +2.5c", new_rule_factory(cs, tp=.025)),
                      ("NEW, slow TP +3.5c", new_rule_factory(cs, tp=.035)),
                      ("NEW, no take-profit at all", new_rule_factory(cs, tp=None)),
                      ("NEW, TP +3c on fast too", new_rule_factory(cs, tp_fast=.03)),
                      ("NEW, fast 6 h / slow 20 h", new_rule_factory(cs, fast_h=6)),
                      ("NEW, fast 12 h / slow 20 h", new_rule_factory(cs, fast_h=12)),
                      ("NEW, fast 8 h / slow 16 h", new_rule_factory(cs, slow_h=16)),
                      ("NEW, fast 8 h / slow 24 h", new_rule_factory(cs, slow_h=24))):
        rows.append(summary(run(out_all, rule, lab), lab))
    table(rows)
    print("\n=== B. new signals (slow with our side at 70c+ skipped), new exits ===")
    Q = run(out_new, new_rule_factory(cs), "new")
    Qo = run(out_new, old_rule, "old")
    table([summary(Qo, "old exits"), summary(Q, "new exits")])
    print("\nexit reasons:"); print(Q.groupby(["kind", "why"]).net.agg(n="size", cents=lambda x: 100 * x.mean(), hrs=lambda x: 0).round(2).to_string())
    print("\nby kind:"); print(Q.groupby("kind").agg(n=("net", "size"), cents=("net", lambda x: 100 * x.mean()), win=("net", lambda x: (x > 0).mean()), hrs=("hrs", "mean")).round(2).to_string())
    print("\nby points (clipped at 3):"); print(Q.groupby(Q.pts.clip(upper=3)).agg(n=("net", "size"), cents=("net", lambda x: 100 * x.mean()), win=("net", lambda x: (x > 0).mean()), avg_shares=("shares", "mean")).round(2).to_string())
    print("\nby right side of the MM line:"); print(Q.groupby("right").agg(n=("net", "size"), cents=("net", lambda x: 100 * x.mean()), win=("net", lambda x: (x > 0).mean()), avg_shares=("shares", "mean")).round(2).to_string())
    print("\nby side added:"); print(Q.groupby("adds").agg(n=("net", "size"), cents=("net", lambda x: 100 * x.mean())).round(2).to_string())
    live = Q[Q.ts >= LIVE_FROM]
    print("\nsince the sleeve went live (24 Oct 19:00 UTC): n", len(live), "cents %.2f" % (100 * live.net.mean()), "usd %.0f (cost-free %.0f)" % (live.dollars.sum(), live.dollars_nocost.sum()))
    bad, tot = verify_exit_reason(cs, out_new, new_rule_factory(cs))
    print("\nexit_reason vs the replay's rule on sampled polls: %d disagreements in %d checks" % (bad, tot))
    print("\n=== C. partisan cap: what fits at net D +%.0f (signals since 24 Oct 19:00 UTC, all trades from the full replay in time order) ===" % NET_D0)
    capacity(Q, NET_D0, "all days   ")
    capacity(live, NET_D0, "since live  ")
    capacity(Q, 0.0, "all days   ")
    capacity(Q, -NET_D0, "all days   ")
    Q.to_pickle(os.environ.get("REPLAY_OUT", "/tmp/catchup_replay_v2.pkl"))
