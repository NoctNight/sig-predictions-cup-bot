"""Drill-down 19: replay the live catch-up sleeve's own decision code (sigbot/proposals/outside_sweep.py) over every raw poll, ignoring its size and position limits,
with the live exit and with the exits the research suggests. Entry at the next poll's touch, exit at the touch of the poll where the rule fires."""
import sqlite3, sys, numpy as np, pandas as pd
sys.path.insert(0, ".")
from sigbot.proposals import outside_sweep as OS
pd.set_option("display.width", 200, "display.max_rows", 100, "display.max_columns", 30)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
v = pd.read_sql_query("select ts,race,party,pm,kalshi,sig_bid,sig_ask,exchange_id from venue_fair where exchange_id is not null order by ts", db)
print(len(v), flush=True)
v["sig_mid"] = (v.sig_bid + v.sig_ask) / 2
polls = {t: g for t, g in v.groupby("ts", sort=True)}
ts = sorted(polls)
print("polls", len(ts), flush=True)
split = ts[0] + .5 * (ts[-1] - ts[0])
c = OS.CatchupSweep(notional=300, max_positions=10**6)
c.max_shares = 10**9
sig = []
key = ["exchange_id", "race", "party", "pm", "kalshi", "sig_bid", "sig_ask", "sig_mid"]
rowsby = {}
for i, t in enumerate(ts):
    g = polls[t]
    rows = [dict(zip(key, x), ts=t) for x in g[key].itertuples(index=False, name=None)]
    for r in rows:
        for k_ in ("pm", "kalshi", "sig_bid", "sig_ask", "sig_mid"):
            if r[k_] != r[k_]: r[k_] = None
    c.push(rows)
    for s in c.signals(rows, t):
        c.last_signal[s["race"]] = t
        sig.append(dict(s, ts=t, i=i))
    if i % 2000 == 0: print(i, len(sig), flush=True)
print("signals", len(sig), flush=True)
# price paths per leg for exits
byleg = {ex: g.sort_values("ts") for ex, g in v.groupby("exchange_id")}
out = []
for s in sig:
    g = byleg[s["exchange_id"]]
    t = g.ts.values
    e = np.searchsorted(t, s["ts"], side="right")
    if e >= len(t) or t[e] - s["ts"] > 240: continue
    b, a, m, pm, k = g.sig_bid.values, g.sig_ask.values, g.sig_mid.values, g.pm.values, g.kalshi.values
    d = s["d"]
    if not (np.isfinite(a[e]) and np.isfinite(b[e])): continue
    px = a[e] if d > 0 else 1 - b[e]
    out.append(dict(s=s, e=e, t=t, b=b, a=a, m=m, pm=pm, k=k, d=d, px=px))
print("entered", len(out), flush=True)


def run(conv=OS.CONVERGE, tstop=OS.TIME_STOP, revert=OS.REVERT, disagree=True, tp=None, sl=None):
    res = []
    for o in out:
        s, e, t, b, a, m, pm, k, d, px = o["s"], o["e"], o["t"], o["b"], o["a"], o["m"], o["pm"], o["k"], o["d"], o["px"]
        xval, hrs = None, None
        for i in range(e + 1, len(t)):
            if not (np.isfinite(a[i]) and np.isfinite(b[i])): continue
            val = (b[i] if d > 0 else 1 - a[i]) - px
            age = t[i] - s["ts"]
            hit = age >= tstop
            if conv is not None and (m[i] - s["mid"]) * d >= conv * s["gap"]: hit = True
            if revert is not None and np.isfinite(pm[i]) and np.isfinite(k[i]) and ((pm[i] + k[i]) / 2 - s["f_then"]) * d < revert * abs(s["move"]): hit = True
            if disagree and np.isfinite(pm[i]) and np.isfinite(k[i]) and abs(pm[i] - k[i]) > OS.AGREE: hit = True
            if tp is not None and val >= tp: hit = True
            if sl is not None and val <= -sl: hit = True
            if hit:
                xval, hrs = val, age / 3600; break
        if xval is None: continue
        res.append(dict(net=xval, hrs=hrs, train=s["ts"] < split, dside=d * (1 if s["party"] == "D" else -1), race=s["race"], day=pd.Timestamp(s["ts"], unit="s").strftime("%m-%d"), sigm=abs(s["move"]), ts=s["ts"]))
    return pd.DataFrame(res)


rows = []
for lab, kw in (("live exit: 70% converged, 12 h, outside reverts, venues disagree", {}), ("no convergence exit", dict(conv=None)), ("convergence 100%", dict(conv=1.0)), ("no convergence, no revert, no disagree, 12 h", dict(conv=None, revert=None, disagree=False)), ("same, 8 h", dict(conv=None, revert=None, disagree=False, tstop=8 * 3600)), ("same, 24 h", dict(conv=None, revert=None, disagree=False, tstop=24 * 3600)), ("same, 36 h", dict(conv=None, revert=None, disagree=False, tstop=36 * 3600)), ("take +3.5c, stop -3c, 24 h", dict(conv=None, revert=None, disagree=False, tstop=24 * 3600, tp=.035, sl=.03)), ("take +3.5c, stop -3c, 36 h", dict(conv=None, revert=None, disagree=False, tstop=36 * 3600, tp=.035, sl=.03)), ("keep revert exit, take +3.5c, 24 h", dict(conv=None, disagree=False, tstop=24 * 3600, tp=.035))):
    Q = run(**kw)
    d_ = Q.groupby("day").net.mean()
    r = dict(exit=lab, n=len(Q), races=Q.race.nunique(), net=100 * Q.net.mean(), win=(Q.net > 0).mean(), hours=Q.hrs.mean(), train=100 * Q[Q.train].net.mean(), test=100 * Q[~Q.train].net.mean(), D=100 * Q[Q.dside > 0].net.mean(), nD=int((Q.dside > 0).sum()), R=100 * Q[Q.dside < 0].net.mean(), nR=int((Q.dside < 0).sum()), days=f"{(d_>0).sum()}/{len(d_)}", worst=100 * Q.net.min())
    rows.append(r)
R = pd.DataFrame(rows); print(R.round(2).to_string(index=False))
Q = run(); Q.to_pickle("data/research_diralpha/catchup_replay.pkl")
live = Q[Q.ts >= pd.Timestamp("2026-10-24 19:00", tz="UTC").timestamp()]
print("\nreplay since the sleeve went live", len(live), "net %.2fc" % (100 * live.net.mean()), "win %.2f" % (live.net > 0).mean(), "(real sleeve: 10 trades, +0.70c, blocked by the cap, 4 positions, 1 in-flight)")
print("by move size:"); print(Q.groupby(pd.cut(Q.sigm, [.015, .025, .035, .05, .5]), observed=True).net.agg(n="size", net=lambda v_: 100 * v_.mean(), win=lambda v_: (v_ > 0).mean()).round(2).to_string())
