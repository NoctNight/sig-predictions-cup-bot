"""Follow-ups to new_strategies_overnight.md (24 Oct 2026): F6 stale-quote inside resting, idea 4 weekend lag.

  <venv>/python research_notes/overnight_followups_study.py f6        F6 replay on the tape + venue_fair, plot
  <venv>/python research_notes/overnight_followups_study.py weekend   idea 4 interim: SIG's copy of outside moves, plot

Read-only: data/sigbot.sqlite opened with mode=ro. No venue calls. Needs numpy, pandas, matplotlib.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB = os.path.join(ROOT, "data", "sigbot.sqlite")
OUT = os.path.join(ROOT, "data", "research_overnight_followups")
FIG = os.path.join(HERE, "figures")
TICK = 0.005
TRAIN_END = pd.Timestamp("2026-10-23", tz="UTC").timestamp()      # F6: train 22 Oct, test from 23 Oct


def con():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=20)


def cluster_t(y, g):
    y, g = np.asarray(y, float), np.asarray(g)
    ok = ~np.isnan(y)
    y, g = y[ok], g[ok]
    if len(y) < 2:
        return (float(y.mean()) if len(y) else np.nan), np.nan, np.nan, len(y)
    m = y.mean()
    s = pd.Series(y - m).groupby(g).sum()
    G = len(s)
    se = np.sqrt((s ** 2).sum()) / len(y) * np.sqrt(G / max(G - 1, 1))
    return float(m), float(se), float(m / se) if se > 0 else np.nan, len(y)


def load_vf():
    vf = pd.read_sql_query("select ts, race, party, pm, kalshi, fair, sig_bid, sig_ask, exchange_id from venue_fair "
                           "where exchange_id is not null", con())
    vf["sig_mid"] = (vf.sig_bid + vf.sig_ask) / 2
    return vf.sort_values("ts")


# --------------------------------------------------------------------------- lag events
def lag_events(vf, min_move=0.02, look=3600, gap_between=3600, max_win=3600):
    """Event start: both venues moved the same way, fair moved >= min_move over the last `look` s, venues agree within
    4 pts, SIG copied < 1/3. Window ends at max_win, when SIG has copied >= 2/3, or when the outside move fades < 1 pt."""
    ev = []
    for (race, party), g in vf.groupby(["race", "party"]):
        g = g.dropna(subset=["fair", "sig_mid"])
        T, F, PM, K, S = (g[c].values for c in ("ts", "fair", "pm", "kalshi", "sig_mid"))
        X = g.exchange_id.astype(str).values
        last = -1e18
        for i in range(len(g)):
            t = T[i]
            j = np.searchsorted(T, t - look)
            if j >= i or T[j] > t - look + 300 or t - last < gap_between:
                continue
            mv = F[i] - F[j]
            if abs(mv) < min_move or np.isnan([PM[i], PM[j], K[i], K[j]]).any():
                continue
            if (PM[i] - PM[j]) * mv <= 0 or (K[i] - K[j]) * mv <= 0 or abs(PM[i] - K[i]) > 0.04:
                continue
            if (S[i] - S[j]) / mv >= 1 / 3:
                continue
            d = 1 if mv > 0 else -1
            end = t + max_win
            for k in range(i + 1, len(g)):
                if T[k] > t + max_win:
                    break
                if (S[k] - S[j]) / mv >= 2 / 3 or (F[k] - F[j]) * d < 0.01:
                    end = T[k]
                    break
            ev.append(dict(race=race, party=party, ex=X[i], t0=t, t1=end, dir=d, move=100 * mv, sig0=S[j], fair=F[i]))
            last = t
    return pd.DataFrame(ev)


def f6():
    c = con()
    vf = load_vf()
    ev = lag_events(vf)
    tape = pd.read_sql_query("select exchange_id ex, price, quantity qty, executed_at, aggressor from tape", c)
    tape["t"] = pd.to_datetime(tape.executed_at, utc=True, format="ISO8601").astype("int64") / 1e9
    fills = pd.read_sql_query("select exchange_id ex, price, abs(quantity) qty, filled_at, purpose from fills", c)
    fills["t"] = pd.to_datetime(fills.filled_at, utc=True, format="ISO8601").astype("int64") / 1e9
    orders = pd.read_sql_query("select exchange_id ex, side, action, price, quantity, purpose, placed_at, expires_at "
                               "from orders where purpose in ('mm','inside') and action='buy'", c)
    orders["exp"] = pd.to_datetime(orders.expires_at, utc=True, format="ISO8601", errors="coerce").astype("int64") / 1e9
    orders["yes_bid"] = np.where(orders.side == "yes", orders.price, np.nan)
    orders["yes_ask"] = np.where(orders.side == "no", 1 - orders.price, np.nan)
    # an MM order is live from placement until the next MM placement on that market (each cycle re-posts) or its expiry
    orders = orders.sort_values("placed_at")
    place_times = {ex: np.unique(g.placed_at.values) for ex, g in orders.groupby("ex")}

    def mm_best(ex, t):
        pt = place_times.get(ex)
        if pt is None:
            return np.nan, np.nan
        i = np.searchsorted(pt, t, side="right") - 1
        if i < 0:
            return np.nan, np.nan
        # all orders placed within 5 s of the last placement time belong to that cycle
        g = orders[(orders.ex == ex) & (orders.placed_at > pt[i] - 5) & (orders.placed_at <= pt[i]) & (orders.exp > t)]
        return g.yes_bid.max(), g.yes_ask.min()

    vfs = {ex: (g.ts.values, g.sig_bid.values, g.sig_ask.values) for ex, g in vf.groupby(vf.exchange_id.astype(str))}

    def touch(ex, t, tol=300):
        T, B, A = vfs[ex]
        i = np.searchsorted(T, t, side="right") - 1
        return (B[i], A[i]) if i >= 0 and t - T[i] <= tol else (np.nan, np.nan)

    def mid_after(ex, t, tol=240):
        T, B, A = vfs[ex]
        i = np.searchsorted(T, t, side="left")
        return (B[i] + A[i]) / 2 if i < len(T) and T[i] - t <= tol else np.nan

    own = fills.groupby("ex")
    rows = []
    for e in ev.itertuples():
        tp = tape[(tape.ex == e.ex) & (tape.t >= e.t0) & (tape.t < e.t1)].sort_values("t")
        ownf = own.get_group(e.ex) if e.ex in own.groups else fills.iloc[:0]
        for r in tp.itertuples():
            if (((ownf.t - r.t).abs() < 3) & ((ownf.price - r.price).abs() < 1e-6)).any():
                continue                                          # our own print
            bid, ask = touch(e.ex, r.t)
            if np.isnan(bid):
                continue
            spread = round((ask - bid) / TICK)
            if e.dir > 0:      # outside up: rest a bid one tick above the DMM bid; against-move takers sell
                p = bid + TICK
                hit = r.aggressor == "sell" and r.price <= p + 1e-9
                ok_price = p < ask - 1e-9 and p <= e.fair - TICK
            else:
                p = ask - TICK
                hit = r.aggressor == "buy" and r.price >= p - 1e-9
                ok_price = p > bid + 1e-9 and p >= e.fair + TICK
            with_move = (r.aggressor == "buy") if e.dir > 0 else (r.aggressor == "sell")
            mb, ma = mm_best(e.ex, r.t)
            mm_there = (mb >= p - 1e-9) if e.dir > 0 else (ma <= p + 1e-9)
            m5, m30 = mid_after(e.ex, r.t + 300), mid_after(e.ex, r.t + 1800)
            rows.append(dict(ex=e.ex, race=e.race, t=r.t, t0=e.t0, dir=e.dir, move=e.move, qty=r.qty, price=r.price,
                             aggressor=r.aggressor, hit=bool(hit), ok_price=bool(ok_price), spread=spread, p=p,
                             with_move=with_move, mm_there=bool(mm_there),
                             mk5=e.dir * (m5 - p) / TICK, mk30=e.dir * (m30 - p) / TICK,
                             mk5_px=e.dir * (m5 - r.price) / TICK, mk30_px=e.dir * (m30 - r.price) / TICK,
                             mm_at_px=bool((mb >= r.price - 1e-9) if e.dir > 0 else (ma <= r.price + 1e-9))))
    fr = pd.DataFrame(rows)
    fr["test"] = fr.t >= TRAIN_END
    fr["day"] = pd.to_datetime(fr.t, unit="s", utc=True).dt.strftime("%m-%d")
    os.makedirs(OUT, exist_ok=True)
    ev.to_csv(os.path.join(OUT, "f6_events.csv"), index=False)
    fr.to_csv(os.path.join(OUT, "f6_prints.csv"), index=False)
    # MM's own fills inside lag windows on the with-move side (what it already earns)
    mmf = []
    for e in ev.itertuples():
        g = fills[(fills.ex == e.ex) & (fills.t >= e.t0) & (fills.t < e.t1) & fills.purpose.isin(["mm", "inside"])]
        for f in g.itertuples():
            m5, m30 = mid_after(e.ex, f.t + 300), mid_after(e.ex, f.t + 1800)
            mmf.append(dict(ex=e.ex, t=f.t, price=f.price, qty=f.qty, m5=m5, m30=m30, dir=e.dir))
    mmf = pd.DataFrame(mmf)

    md = []
    P = md.append
    span_h = (vf.ts.max() - vf.ts.min()) / 3600
    P(f"venue_fair span {span_h:.1f} h ({pd.to_datetime(vf.ts.min(), unit='s')} to {pd.to_datetime(vf.ts.max(), unit='s')} UTC); "
      f"lag events (2+ pts, both venues, SIG < 1/3 copied) {len(ev)}: train {int((ev.t0 < TRAIN_END).sum())}, test {int((ev.t0 >= TRAIN_END).sum())}; "
      f"median window {((ev.t1 - ev.t0) / 60).median():.0f} min")
    P(f"prints inside windows {len(fr)}: with the move {int(fr.with_move.sum())} ({fr[fr.with_move].qty.sum():,.0f} sh), "
      f"against {int((~fr.with_move).sum())} ({fr[~fr.with_move].qty.sum():,.0f} sh)")
    P("DMM spread at against-move prints: " + ", ".join(f"{k} tick {v}" for k, v in fr[~fr.with_move].spread.value_counts().sort_index().items()))
    res = {}
    for Q in (200, 500, 1000):
        for nm, d in (("train", fr[~fr.test]), ("test", fr[fr.test])):
            h = d[d.hit & d.ok_price].copy()
            # cap our fills at Q shares per event
            h["fq"] = 0.0
            for t0, g in h.groupby("t0"):
                left = Q
                for i in g.sort_values("t").index:
                    q = min(left, h.at[i, "qty"])
                    h.at[i, "fq"] = q
                    left -= q
            h = h[h.fq > 0]
            inc = h[~h.mm_there]
            w = lambda x, c: (np.average(x[c].dropna(), weights=x.loc[x[c].notna(), "fq"]) if x[c].notna().any() else np.nan)
            key = f"Q{Q}_{nm}"
            res[key] = dict(fills=len(h), shares=float(h.fq.sum()), incr_fills=len(inc), incr_shares=float(inc.fq.sum()),
                            mk5=cluster_t(h.mk5, h.ex), mk30=cluster_t(h.mk30, h.ex),
                            incr_mk5=cluster_t(inc.mk5, inc.ex), incr_mk30=cluster_t(inc.mk30, inc.ex),
                            wmk30=w(h, "mk30"), incr_wmk30=w(inc, "mk30"),
                            pnl_incr_30=float((inc.mk30 * inc.fq).sum() * TICK), days=d.day.nunique())
            r = res[key]
            P(f"Q {Q} {nm}: fills {r['fills']} ({r['shares']:,.0f} sh), not already covered by our MM {r['incr_fills']} ({r['incr_shares']:,.0f} sh); "
              f"markout 5 min {r['mk5'][0]:+.2f} t (t {r['mk5'][2]:+.1f}), 30 min {r['mk30'][0]:+.2f} (t {r['mk30'][2]:+.1f}); "
              f"incremental 30 min {r['incr_mk30'][0]:+.2f} (t {r['incr_mk30'][2]:+.1f}, n {r['incr_mk30'][3]}); "
              f"incremental P&L at 30 min {r['pnl_incr_30']:+.1f} S over {r['days']} day(s)")
    # upper bound: join the DMM's stale touch and take EVERY against-move print at its own price (ignores the DMM's
    # 1,500-2,500 shares queued ahead of us)
    ag = fr[~fr.with_move]
    for nm, d in (("train", ag[~ag.test]), ("test", ag[ag.test])):
        a5, a30 = cluster_t(d.mk5_px, d.ex), cluster_t(d.mk30_px, d.ex)
        inc = d[~d.mm_at_px]
        i30 = cluster_t(inc.mk30_px, inc.ex)
        net = cluster_t(inc.mk30_px - 0.5, inc.ex)
        res[f"upper_{nm}"] = dict(prints=len(d), shares=float(d.qty.sum()), mk5=a5, mk30=a30, incr_mk30=i30, incr_net=net,
                                  share_mm_there=float(d.mm_at_px.mean()) if len(d) else np.nan)
        P(f"UPPER BOUND join the stale touch, every against-move print ours, {nm}: {len(d)} prints ({d.qty.sum():,.0f} sh); "
          f"markout 5 min {a5[0]:+.2f} (t {a5[2]:+.1f}), 30 min {a30[0]:+.2f} (t {a30[2]:+.1f}); our MM already at that price "
          f"{d.mm_at_px.mean():.0%}; the rest, 30 min {i30[0]:+.2f} (t {i30[2]:+.1f}), after 0.5-tick exit {net[0]:+.2f} (t {net[2]:+.1f}, n {net[3]})")
    if len(mmf):
        mmf["mk30"] = mmf.dir * (mmf.m30 - mmf.price) / TICK
        P(f"our MM/inside fills inside lag windows: {len(mmf)}, {mmf.qty.sum():,.0f} sh")
    res["n_events"] = len(ev)
    json.dump(res, open(os.path.join(OUT, "f6_results.json"), "w"), indent=1, default=float)
    open(os.path.join(OUT, "f6_out.md"), "w").write("\n".join(md) + "\n")
    print("\n".join(md))
    plot_f6(fr)


def plot_f6(fr):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 10.5})
    fig, axs = plt.subplots(3, 1, figsize=(13, 22), gridspec_kw={"hspace": 0.62})
    fig.suptitle("Resting one tick inside SIG's stale quote while it lags the outside price", fontsize=16, weight="bold", y=0.995)
    fig.text(0.5, 0.981, "F6: after a 2+ point Polymarket/Kalshi move SIG has not copied, sit one tick inside the DMM on the side the move favours",
             ha="center", fontsize=11, color="#444")
    mins = np.array([5, 15, 30, 45, 60])

    def by_age(d, col):
        age = (d.t - d.t0) / 60
        out = []
        for lo, hi, m in ((0, 10, 5), (10, 20, 15), (20, 40, 30), (40, 50, 45), (50, 61, 60)):
            sel = (age >= lo) & (age < hi)
            out.append((m, *cluster_t(d.loc[sel, col], d.loc[sel, "ex"])))
        return [o for o in out if o[4] >= 2]

    ax = axs[0]
    for nm, ls, mk in (("train", ":", "^"), ("test", "-", "o")):
        d = fr[fr.test == (nm == "test")]
        age = (d.t - d.t0) / 60
        for col, lab in (("with", "#1f77b4"), ("against", "#d62728")):
            sel = d.with_move if col == "with" else ~d.with_move
            cnt = [d[sel & (age >= lo) & (age < hi)].qty.sum() / max(d.t0.nunique(), 1) for lo, hi in ((0, 10), (10, 20), (20, 40), (40, 50), (50, 61))]
            ax.plot(mins, cnt, ls=ls, marker=mk, color=lab, lw=2 if nm == "test" else 1.5,
                    label=f"takers trading {'with' if col == 'with' else 'against'} the move, {'training (22 Oct)' if nm == 'train' else 'test (23-24 Oct)'}")
    ax.set_title("A. How many shares do takers trade against the move while SIG lags?", loc="left", weight="bold")
    ax.set_ylabel("Shares per event in that\ntime bin (all SIG prints)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False, fontsize=10)

    ax = axs[1]
    for nm, ls, mk in (("train", ":", "^"), ("test", "-", "o")):
        d = fr[(fr.test == (nm == "test")) & ~fr.with_move]
        for col, colr, lab in (("mk5_px", "#2ca02c", "5 min"), ("mk30_px", "#9467bd", "30 min")):
            pts = by_age(d, col)
            if not pts:
                continue
            m, mu, se, t, n = map(np.array, zip(*pts))
            ax.plot(m, mu, ls=ls, marker=mk, color=colr, lw=2 if nm == "test" else 1.5,
                    label=f"markout {lab} after each against-move print, {'training' if nm == 'train' else 'test'} days ({int(n.sum())} prints)")
            if nm == "test":
                ax.fill_between(m, mu - 1.96 * se, mu + 1.96 * se, color=colr, alpha=0.13)
    ax.axhline(0, color="grey", lw=1)
    ax.set_title("B. Upper bound: if every against-move print had been ours, how far did SIG move our way?", loc="left", weight="bold")
    ax.set_ylabel("Mid-price move in our favour\nafter the print, before costs (ticks)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False, fontsize=10)

    ax = axs[2]
    for nm, ls, mk in (("train", ":", "^"), ("test", "-", "o")):
        d = fr[(fr.test == (nm == "test")) & fr.hit & fr.ok_price]
        u = fr[(fr.test == (nm == "test")) & ~fr.with_move & ~fr.mm_at_px]
        for sub, colr, lab in ((u.assign(mk30=u.mk30_px), "#ff7f0e", "upper bound: join the stale touch, every against-move print ours"),
                               (d, "#1f77b4", "the real F6 order: one tick inside the DMM (needs a 2+ tick spread)")):
            pts = by_age(sub.assign(net=sub.mk30 - 0.5), "net")
            if not pts:
                continue
            m, mu, se, t, n = map(np.array, zip(*pts))
            ax.plot(m, mu, ls=ls, marker=mk, color=colr, lw=2 if nm == "test" else 1.5,
                    label=f"{lab}, {'training' if nm == 'train' else 'test'} days ({int(n.sum())})")
            if nm == "test":
                ax.fill_between(m, mu - 1.96 * se, mu + 1.96 * se, color=colr, alpha=0.13)
    ax.axhline(0, color="grey", lw=1)
    ax.set_title("C. Profit per share after exit costs (held 30 min, then sold at the touch)", loc="left", weight="bold")
    ax.set_ylabel("Profit per share (ticks)")
    ax.set_xlabel("Minutes since the outside move was detected (time bins 0-10, 10-20, 20-40, 40-50, 50-60)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.14), frameon=False, fontsize=10)
    for a in axs:
        a.grid(alpha=0.3)
        a.set_xticks(mins)
    gl = ("Glossary\n"
          "outside move: the Polymarket/Kalshi fair for the same party moved 2+ points within an hour, both venues the same way and\n"
          "    within 4 points of each other, while SIG had copied under a third of it; the window ends after 60 min, once SIG has\n"
          "    copied two thirds, or when the outside move fades below 1 point\n"
          "DMM: SIG's designated market maker, whose quote is stale during the window\n"
          "against-move print: a SIG taker trade the opposite way to the outside move (selling after an outside rise)\n"
          "real F6 order: one tick inside the DMM, only where its spread is 2+ ticks; 340 of 395 against-move prints came at a\n"
          "    1-tick spread, where there is no inside;  upper bound: ignores the DMM's 1,500-2,500 shares queued ahead of us\n"
          "markout: SIG mid later minus our price, in our favour;  tick: 0.005;  exit cost: half a 1-tick spread = 0.5 tick\n"
          "our MM already quoting: our market maker had a bid (ask) at or better than our price at that moment\n"
          "dotted = training day (22 Oct), solid = test days (23-24 Oct);  bands: 95% range, clustered by market")
    fig.text(0.07, 0.008, gl, fontsize=10, va="bottom", ha="left", bbox=dict(boxstyle="round", fc="#f4f4f0", ec="#999"))
    fig.subplots_adjust(top=0.96, bottom=0.17, left=0.1, right=0.97)
    fig.savefig(os.path.join(FIG, "f6_stale_inside.png"), dpi=100)
    print("wrote figure")


# --------------------------------------------------------------------------- idea 4: weekend lag (interim)
SAT_START = pd.Timestamp("2026-10-24 04:00", tz="UTC").timestamp()   # Saturday 00:00 ET


def jump_events(vf, min_move=0.015, max_dt=240, gap_between=1800):
    """Both-venue jump: between consecutive polls (<= max_dt s apart) Polymarket and Kalshi both moved the same way, the
    fair moved >= min_move, venues agree within 4 pts after, SIG had not moved more than a third of it by then. Copied share
    at +h = (SIG mid(t0 + h) - SIG mid before) / fair move; outside kept = (fair(t0 + h) - fair before) / fair move."""
    ev = []
    for (race, party), g in vf.groupby(["race", "party"]):
        g = g.dropna(subset=["fair", "sig_mid", "pm", "kalshi"])
        T, F, PM, K, S = (g[c].values for c in ("ts", "fair", "pm", "kalshi", "sig_mid"))
        last = -1e18
        for i in range(1, len(g)):
            if T[i] - T[i - 1] > max_dt or T[i] - last < gap_between:
                continue
            mv = F[i] - F[i - 1]
            if abs(mv) < min_move or (PM[i] - PM[i - 1]) * mv <= 0 or (K[i] - K[i - 1]) * mv <= 0 or abs(PM[i] - K[i]) > 0.04:
                continue
            if (S[i] - S[i - 1]) / mv >= 1 / 3:
                continue
            r = dict(race=race, party=party, t0=T[i], move=100 * mv)
            for h in (10, 30, 60):
                k = np.searchsorted(T, T[i] + 60 * h)
                ok = k < len(T) and T[k] - (T[i] + 60 * h) <= 300
                r[f"c{h}"] = (S[k] - S[i - 1]) / mv if ok else np.nan
                r[f"o{h}"] = (F[k] - F[i - 1]) / mv if ok else np.nan
            ev.append(r)
            last = T[i]
    return pd.DataFrame(ev)


def weekend():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    vf = load_vf()
    ev = jump_events(vf)
    ev["when"] = np.where(ev.t0 >= SAT_START, "Saturday (ET)", "weekday")
    md, res = [], {}
    P = md.append
    P(f"venue_fair to {pd.to_datetime(vf.ts.max(), unit='s')} UTC; Saturday ET from {pd.to_datetime(SAT_START, unit='s')} UTC "
      f"({(vf.ts.max() - SAT_START) / 3600:.1f} h so far)")
    for w, d in ev.groupby("when"):
        res[w] = {}
        for h in (10, 30, 60):
            x = d[f"c{h}"].dropna()
            o = d[f"o{h}"].dropna()
            q = x.quantile([0.25, 0.5, 0.75]).values if len(x) else [np.nan] * 3
            res[w][h] = dict(n=len(x), median=float(np.median(x)) if len(x) else None, mean=float(x.mean()) if len(x) else None,
                             q25=float(q[0]), q75=float(q[2]), outside_kept=float(np.median(o)) if len(o) else None)
        P(f"{w}: {len(d)} jumps of 1.5+ pts; SIG copied (median) at 10 min {res[w][10]['median']}, 30 min {res[w][30]['median']}, "
          f"60 min {res[w][60]['median']} (n {res[w][10]['n']}/{res[w][30]['n']}/{res[w][60]['n']}); outside kept at 30 min "
          f"{res[w][30]['outside_kept']}")
    # bootstrap difference at 10 min
    a = ev[ev.when == "weekday"].c10.dropna().values
    b = ev[ev.when != "weekday"].c10.dropna().values
    if len(a) > 3 and len(b) > 1:
        rng = np.random.default_rng(3)
        bs = [np.median(rng.choice(b, len(b))) - np.median(rng.choice(a, len(a))) for _ in range(4000)]
        res["diff10_ci"] = [float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))]
        P(f"Saturday minus weekday, median copied at 10 min: 95% bootstrap range {res['diff10_ci'][0]:+.2f} to {res['diff10_ci'][1]:+.2f}")
    P("Decision rule (Monday 26 Oct): act only if the weekend copy at 10 min is under 15% with n >= 10.")
    os.makedirs(OUT, exist_ok=True)
    ev.to_csv(os.path.join(OUT, "weekend_events.csv"), index=False)
    json.dump(res, open(os.path.join(OUT, "weekend_results.json"), "w"), indent=1, default=float)
    open(os.path.join(OUT, "weekend_out.md"), "w").write("\n".join(md) + "\n")
    print("\n".join(md))

    plt.rcParams.update({"font.size": 10.5})
    fig, axs = plt.subplots(3, 1, figsize=(13, 20), gridspec_kw={"hspace": 0.6})
    fig.suptitle("Does SIG copy outside moves more slowly on weekends? (interim)", fontsize=16, weight="bold", y=0.995)
    fig.text(0.5, 0.981, "Polymarket and Kalshi both jump 1.5+ points between two polls; how much of it has SIG copied after x minutes",
             ha="center", fontsize=11, color="#444")
    hs = np.array([10, 30, 60])
    cols = {"weekday": "#1f77b4", "Saturday (ET)": "#d62728"}
    ax = axs[0]
    for w, d in ev.groupby("when"):
        med = [np.nanmedian(d[f"c{h}"]) if d[f"c{h}"].notna().any() else np.nan for h in hs]
        q1 = [np.nanpercentile(d[f"c{h}"].dropna(), 25) if d[f"c{h}"].notna().sum() > 1 else np.nan for h in hs]
        q3 = [np.nanpercentile(d[f"c{h}"].dropna(), 75) if d[f"c{h}"].notna().sum() > 1 else np.nan for h in hs]
        ax.plot(hs, med, "-o", color=cols[w], lw=2, label=f"{w}: median share SIG copied ({len(d)} jumps)")
        ax.fill_between(hs, q1, q3, color=cols[w], alpha=0.13)
    ax.plot([10, 30, 60], [0.29, 0.44, 0.53], "k:", marker="^", label="weekday kernel from the 22 Oct feed capture (29% / 44% / 53%)")
    ax.axhline(0.15, color="#777", ls="-.", label="decision line: act if weekends copy under 15% at 10 min (n >= 10)")
    ax.set_title("A. Share of the outside move SIG has copied", loc="left", weight="bold")
    ax.set_ylabel("Share of the outside move\ncopied by SIG's mid")
    ax.set_ylim(-0.3, 1.2)
    ax = axs[1]
    for w, d in ev.groupby("when"):
        med = [np.nanmedian(d[f"o{h}"]) if d[f"o{h}"].notna().any() else np.nan for h in hs]
        ax.plot(hs, med, "-o", color=cols[w], lw=2, label=f"{w}: share of the move the outside venues kept")
    ax.set_title("B. Did the outside move stick? (a fading move needs no copying)", loc="left", weight="bold")
    ax.set_ylabel("Share of the outside move\nstill there")
    ax.set_ylim(-0.3, 1.5)
    ax = axs[2]
    for w, d in ev.groupby("when"):
        x = d.c10.values
        ax.scatter(np.abs(d.move), x, color=cols[w], s=28, alpha=0.6, label=f"{w}: one jump")
    ax.axhline(0.15, color="#777", ls="-.")
    ax.set_title("C. Each jump: share copied after 10 min against the size of the outside move", loc="left", weight="bold")
    ax.set_ylabel("Share copied at 10 min")
    ax.set_xlabel("Minutes after the outside jump (panels A, B);  size of the outside jump in points (panel C)")
    for a in axs:
        a.grid(alpha=0.3)
        a.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False, fontsize=10)
    gl = ("Glossary\n"
          "outside jump: between two consecutive polls (at most 4 min apart) the Polymarket and Kalshi prices for the same party both\n"
          "    moved the same way, their average moved 1.5+ points and SIG had not yet moved a third of that; one per market per 30 min\n"
          "share copied: SIG mid change since the poll before the jump divided by the outside move (1 = fully copied)\n"
          "Saturday (ET): from 04:00 UTC 24 Oct;  weekday: 22-23 Oct (our venue_fair polls start 21 Oct 23:49 UTC)\n"
          "bands: middle half of the jumps (25th to 75th percentile);  interim: the decision is due Monday 26 Oct")
    fig.text(0.07, 0.008, gl, fontsize=10, va="bottom", ha="left", bbox=dict(boxstyle="round", fc="#f4f4f0", ec="#999"))
    fig.subplots_adjust(top=0.96, bottom=0.15, left=0.1, right=0.97)
    fig.savefig(os.path.join(FIG, "weekend_lag.png"), dpi=100)
    print("wrote figure")


# --------------------------------------------------------------------------- idea 3: liquidation watch history + plot
def liq():
    """Rolling-24 h detector every 3 h on the live database (needs sigbot/liquidation_watch.py from
    research_notes/liquidation_absorb_wiring.diff, or LIQ_WATCH_ROOT pointing at a tree that has it)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    sys.path.insert(0, os.environ.get("LIQ_WATCH_ROOT", ROOT))
    from sigbot import liquidation_watch as LW
    c = con()
    t_end = c.execute("select max(ts) from venue_fair").fetchone()[0]
    t_start = c.execute("select min(ts) from venue_fair").fetchone()[0] + 86400
    rows = []
    for t in np.arange(t_start, t_end + 1, 3 * 3600):
        on, info = LW.trigger(c, float(t))
        base = LW.leg_baselines(c, float(t))
        legs = LW.fav_legs(c, t - 3600, t)
        sag = []
        for ex, f, m, pm, k in c.execute("select exchange_id, fair, sig_mid, pm, kalshi from venue_fair where ts >= ? and ts < ? "
                                         "and fair is not null and sig_mid is not null", (t - 900, t)):
            ex = str(ex)
            if ex in legs and ex in base and pm is not None and k is not None and abs(pm - k) <= 0.04:
                sag.append((ex, (m - f - base[ex]) / TICK))
        last = {}
        for ex, v in sag:
            last[ex] = v
        rows.append(dict(t=float(t), on=on, sell_share=info["sell_share"], base_share=info.get("baseline_sell_share"),
                         shares=info["shares"], gap=info["gap_ticks"],
                         base=info["baseline_gap_ticks"], change=info["gap_change_ticks"], legs=len(legs),
                         n_base=sum(1 for ex in legs if ex in base), n_sag3=sum(1 for v in last.values() if v <= -3),
                         n_seen=len(last)))
    df = pd.DataFrame(rows)
    os.makedirs(OUT, exist_ok=True)
    df.to_csv(os.path.join(OUT, "liq_watch_history.csv"), index=False)
    print(df.round(2).assign(t=pd.to_datetime(df.t, unit="s")).to_string())
    plt.rcParams.update({"font.size": 10.5})
    fig, axs = plt.subplots(3, 1, figsize=(13, 19), sharex=True, gridspec_kw={"hspace": 0.5})
    fig.suptitle("Are rivals dumping favourites before the punt? (detector, live data)", fontsize=16, weight="bold", y=0.997)
    fig.text(0.5, 0.982, "Rolling 24-hour reading every 3 hours since 22 Oct; the absorb sleeve may only trade while both A and B are past their lines",
             ha="center", fontsize=11, color="#444")
    x = pd.to_datetime(df.t, unit="s")
    ax = axs[0]
    ax.plot(x, 100 * df.sell_share, "-o", color="#d62728", lw=2, label="share of taker volume on favourites that was selling (our own prints removed)")
    ax.axhline(60, color="black", ls="--", label="trigger line 1: more than 60% sells")
    ax.plot(x, 100 * (df.base_share + 0.10), ":", color="black", lw=1.5,
            label="trigger line 2: at least 10 points above its own median of the 3 days before (added after review)")
    ax.set_ylabel("Taker sells (% of shares)")
    ax.set_title("A. One-sided selling into SIG's bids?", loc="left", weight="bold")
    ax.set_ylim(30, 80)
    ax = axs[1]
    ax.plot(x, df.change, "-o", color="#1f77b4", lw=2, label="favourites' SIG-minus-outside gap, last 24 h minus the 3 days before")
    ax.plot(x, df.gap, ":", color="#1f77b4", lw=1.5, label="the gap itself (SIG mid minus Polymarket/Kalshi fair)")
    ax.axhline(-2, color="black", ls="--", label="trigger line: gap 2+ ticks wider than usual (more negative)")
    ax.set_ylabel("Ticks")
    ax.set_title("B. Do SIG's favourites sag under the outside price more than usual?", loc="left", weight="bold")
    ax = axs[2]
    ok = df.n_seen > 0                                    # no usual gap yet (first day) or no poll in the window
    ax.plot(x[ok], df.n_sag3[ok], "-o", color="#2ca02c", lw=2, label="favourite legs sitting 3+ ticks under their own usual gap (what the sleeve would buy)")
    ax.plot(x[ok], df.n_seen[ok], ":", color="#2ca02c", lw=1.5, label="favourite legs with a usual gap to compare against")
    ax.set_ylabel("Legs")
    ax.set_title("C. How much would there be to buy if the trigger fired?", loc="left", weight="bold")
    ax.set_xlabel("Time (UTC)")
    for a in axs:
        a.grid(alpha=0.3)
        a.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False, fontsize=10)
    for a in axs:
        on = df[df.on]
        for t in on.t:
            a.axvspan(pd.to_datetime(t - 5400, unit="s"), pd.to_datetime(t + 5400, unit="s"), color="#ffbb78", alpha=0.3)
    gl = ("Glossary\n"
          "favourite: a leg whose Polymarket/Kalshi fair is 0.70 or more;  tick: 0.005;  taker: the side that crossed the spread\n"
          "gap: SIG mid minus the outside fair, averaged over polls where Polymarket and Kalshi agree within 4 points;\n"
          "    favourites usually sit about 5-7 ticks cheap on SIG, so only a change against the 3 days before counts\n"
          "trigger: A above both lines and B below -2 ticks over the last 24 h (orange shading when on; none so far)\n"
          "usual gap of a leg: its median gap over the 3 days before the last 24 h (so a day of selling cannot hide itself)\n"
          "the decision is due 31 Oct-3 Nov; the absorb sleeve is built, tested and switched off")
    fig.text(0.07, 0.008, gl, fontsize=10, va="bottom", ha="left", bbox=dict(boxstyle="round", fc="#f4f4f0", ec="#999"))
    fig.subplots_adjust(top=0.955, bottom=0.14, left=0.1, right=0.97)
    fig.savefig(os.path.join(FIG, "liquidation_watch.png"), dpi=100)
    print("wrote figure")


if __name__ == "__main__":
    {"f6": f6, "weekend": weekend, "liq": liq}[sys.argv[1]]()
