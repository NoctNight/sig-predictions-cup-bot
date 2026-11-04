"""EV hunt (24 Oct 2026): mean reversion we don't trade, sweep-to-fair angles, outside-venue calibration.

Read-only. Opens data/sigbot.sqlite with mode=ro (no copy), no SIG API calls, no config or code changes.
Run:  nice -n 5 <venv with numpy/pandas/matplotlib/scipy>/bin/python research_notes/ev_hunt_study.py part1|part2|part3|figs
Writes data/research_ev_hunt/*.json and research_notes/figures/ev_hunt_*.png. Notes: research_notes/ev_hunt.md.
"""
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "research_ev_hunt"
OUT.mkdir(parents=True, exist_ok=True)
FIG = ROOT / "research_notes" / "figures"
TICK = 0.005
SPLIT = pd.Timestamp("2026-10-23 12:00", tz="UTC").timestamp()  # IS before, OOS after
HOUR = 3600.0


def db():
    return sqlite3.connect(f"file:{ROOT / 'data' / 'sigbot.sqlite'}?mode=ro", uri=True)


def load_vf():
    vf = pd.read_sql("select ts, race, party, pm, kalshi, fair, sig_bid, sig_ask, exchange_id from venue_fair", db())
    vf = vf[(vf.sig_bid > 0) & (vf.sig_ask > vf.sig_bid) & (vf.sig_ask < 1)]
    vf["mid"] = (vf.sig_bid + vf.sig_ask) / 2
    return vf


def wide(vf, col):
    return vf.pivot_table(index="ts", columns="exchange_id", values=col, aggfunc="last").sort_index()


def ctstat(x, groups):
    """Mean and t-stat with standard errors clustered by group."""
    x = np.asarray(x, float)
    g = np.asarray(groups)
    n = len(x)
    if n < 3:
        return float(np.mean(x)) if n else float("nan"), float("nan"), n
    m = x.mean()
    e = pd.Series(x - m).groupby(g).sum().values
    k = len(e)
    se = np.sqrt((e ** 2).sum() * k / max(k - 1, 1)) / n
    return float(m), float(m / se) if se > 0 else float("nan"), n


def at(ts_index, t):
    """Index of the last poll at or before t."""
    return np.searchsorted(ts_index, t, side="right") - 1


# ---------------------------------------------------------------- part 1
def part1():
    vf = load_vf()
    races = vf.drop_duplicates("exchange_id").set_index("exchange_id")[["race", "party"]]
    B, A, F = wide(vf, "sig_bid"), wide(vf, "sig_ask"), wide(vf, "fair")
    M = (B + A) / 2
    T = B.index.values
    res = {}

    # 1a. Fade SIG-only moves (SIG mid moves >= k ticks in 10 min while the outside fair is still).
    rows = []
    W = 10 * 60
    H = {"30m": 1800, "2h": 7200, "6h": 21600, "12h": 43200}
    for ex in M.columns:
        m, f, b, a = M[ex].values, F[ex].values, B[ex].values, A[ex].values
        last = -1e18
        for i in range(len(T)):
            j = at(T, T[i] - W)
            if j < 0 or T[i] - T[j] > W + 200 or T[i] - last < 1800:
                continue
            if np.isnan(m[i]) or np.isnan(m[j]) or np.isnan(f[i]) or np.isnan(f[j]):
                continue
            dm, df_ = m[i] - m[j], f[i] - f[j]
            if abs(dm) < 2 * TICK - 1e-9 or abs(df_) > 0.003:
                continue
            e = i + 1  # one poll (~85 s) of latency
            if e >= len(T) or np.isnan(b[e]):
                continue
            s = -np.sign(dm)  # fade: +1 = buy YES at the ask
            entry = a[e] if s > 0 else b[e]
            g0, g1 = m[j] - f[j], m[i] - f[i]
            rec = dict(ex=ex, race=races.race.get(ex), ts=T[i], oos=T[i] >= SPLIT, k=abs(dm) / TICK,
                       away=abs(g1) > abs(g0), spread=(a[e] - b[e]) / TICK)
            for h, sec in H.items():
                x = at(T, T[e] + sec)
                if x >= len(T) or T[x] < T[e] + sec - 600 or np.isnan(b[x]):
                    rec[h] = np.nan
                    rec[h + "_mid"] = np.nan
                    rec[h + "_fol"] = np.nan
                    continue
                exitp = b[x] if s > 0 else a[x]
                rec[h] = s * (exitp - entry) / TICK
                rec[h + "_mid"] = s * (m[x] - entry) / TICK
                rec[h + "_fol"] = -s * ((a[x] if s > 0 else b[x]) - (b[e] if s > 0 else a[e])) / TICK  # follow the move
            rows.append(rec)
            last = T[i]
    ev = pd.DataFrame(rows)
    ev.to_pickle(OUT / "p1a_events.pkl")
    tab = []
    for kmin in (2, 3, 4):
        for away in (None, True, False):
            for oos in (False, True):
                d = ev[(ev.k >= kmin - 1e-9) & (ev.oos == oos)]
                if away is not None:
                    d = d[d.away == away]
                for h in H:
                    for mark in ("", "_mid", "_fol"):
                        dd = d.dropna(subset=[h + mark])
                        mu, t, n = ctstat(dd[h + mark], dd.race)
                        tab.append(dict(k=kmin, away=away, oos=oos, h=h, mark=mark or "touch", mean=mu, t=t, n=n))
    res["1a_fade_sig_only"] = tab

    # 1b. Cross-race basket: party-signed deviation of SIG-minus-fair from each leg's trailing 24 h median.
    G = M - F
    med = G.rolling(window=int(24 * HOUR / 85), min_periods=int(12 * HOUR / 85)).median()
    D = G - med
    sign = pd.Series({ex: (1 if races.party.get(ex) == "D" else -1 if races.party.get(ex) == "R" else 0) for ex in D.columns})
    keep = sign[sign != 0].index
    basket = (D[keep] * sign[keep]).mean(axis=1) / TICK
    sigpart = ((M[keep] - M[keep].shift(1)) * sign[keep]).mean(axis=1).cumsum() / TICK
    bk = pd.DataFrame({"basket": basket, "sig_level": sigpart}).dropna()
    out = {"basket_sd_ticks": float(bk.basket.std()), "n_polls": len(bk)}
    for h in (1, 4, 12):
        lag = int(h * HOUR / 85)
        y = bk.basket.shift(-lag) - bk.basket
        ok = y.notna()
        b_ = np.polyfit(bk.basket[ok], y[ok], 1)[0]
        out[f"reversion_slope_{h}h"] = float(b_)  # -1 = full reversion, 0 = none
    out["basket_abs_p95_ticks"] = float(bk.basket.abs().quantile(0.95))
    out["leg_dev_sd_ticks"] = float((D / TICK).stack().std())
    res["1b_basket"] = out
    bk.to_pickle(OUT / "p1b_basket.pkl")

    # 1c. Buy-set side of the deviation churn (YES on every leg of a race).
    set_rows = []
    for race, grp in races.groupby("race"):
        legs = list(grp.index)
        if len(legs) < 2 or not all(l in A.columns for l in legs):
            continue
        sa, sb = A[legs].sum(axis=1, min_count=len(legs)), B[legs].sum(axis=1, min_count=len(legs))
        ov = (sa + sb) / 2 - 1
        md = ov.rolling(window=int(24 * HOUR / 85), min_periods=int(12 * HOUR / 85)).median()
        sav, sbv, ovv, mdv = sa.values, sb.values, ov.values, md.values
        busy = -1e18
        for i in range(len(T)):
            if T[i] < busy or np.isnan(mdv[i]) or np.isnan(sav[i]):
                continue
            if ovv[i] > mdv[i] - 4 * TICK + 1e-9:
                continue
            e = i + 1
            if e >= len(T) or np.isnan(sav[e]):
                continue
            ent = sav[e]
            out_ = None
            for x in range(e + 1, len(T)):
                if T[x] > T[e] + 24 * HOUR:
                    break
                if not np.isnan(sbv[x]) and sbv[x] - ent >= 2 * TICK - 1e-9:
                    out_ = (x, sbv[x] - ent, "take")
                    break
            if out_ is None:
                x = at(T, T[e] + 24 * HOUR)
                if T[x] < T[e] + 24 * HOUR - 600:
                    x = len(T) - 1
                    out_ = (x, sbv[x] - ent, "open")
                else:
                    out_ = (x, sbv[x] - ent, "24h")
            set_rows.append(dict(race=race, ts=T[i], oos=T[i] >= SPLIT, legs=len(legs), sum_ask=ent,
                                 below_payout=ent <= 1 - TICK, dev=(ovv[i] - mdv[i]) / TICK,
                                 pnl=out_[1] / TICK, exit=out_[2], hold_h=(T[out_[0]] - T[e]) / HOUR))
            busy = T[out_[0]] + 1800
    st = pd.DataFrame(set_rows)
    st.to_pickle(OUT / "p1c_buyset.pkl")
    tab = []
    for oos in (False, True):
        for filt in ("all", "below_payout"):
            d = st[st.oos == oos]
            if filt == "below_payout":
                d = d[d.below_payout]
            for ex_ in ("all", "closed"):
                dd = d if ex_ == "all" else d[d.exit != "open"]
                mu, t, n = ctstat(dd.pnl, dd.race) if len(dd) else (np.nan, np.nan, 0)
                tab.append(dict(oos=oos, filt=filt, exits=ex_, mean=mu, t=t, n=n,
                                take=int((dd.exit == "take").sum()) if len(dd) else 0))
    res["1c_buyset"] = tab

    # 1d. After our own fills: MM markout path, and recovery of the touch after our taker fills.
    fl = pd.read_sql("select f.exchange_id, f.price, f.quantity, f.filled_at, f.purpose, f.order_id from fills f", db())
    fl["ts"] = pd.to_datetime(fl.filled_at, utc=True, format="ISO8601").map(lambda x: x.timestamp())
    fl["ex"] = fl.exchange_id.astype(str)
    Mc = M.copy()
    Mc.columns = Mc.columns.astype(str)
    Bc, Ac = B.copy(), A.copy()
    Bc.columns = Bc.columns.astype(str)
    Ac.columns = Ac.columns.astype(str)
    hs = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "3h": 10800, "6h": 21600, "12h": 43200, "24h": 86400}
    mk = []
    for _, r in fl.iterrows():
        if r.ex not in Mc.columns:
            continue
        i0 = at(T, r.ts - 1)
        if i0 < 0 or r.ts - T[i0] > 300:
            continue
        sgn = np.sign(r.quantity)
        m0 = Mc[r.ex].values[i0]
        touch0 = Ac[r.ex].values[i0] if sgn > 0 else Bc[r.ex].values[i0]
        rec = dict(purpose=r.purpose, ex=r.ex, ts=r.ts, oos=r.ts >= SPLIT, q=abs(r.quantity), order=r.order_id,
                   taker=bool(sgn * (r.price - touch0) >= -1e-9),
                   through=sgn * (r.price - touch0) / TICK, edge0=sgn * (m0 - r.price) / TICK)
        for h, sec in hs.items():
            x = at(T, r.ts + sec)
            if x >= len(T) - 1 and T[-1] < r.ts + sec - 300:
                rec["mo_" + h] = np.nan
                rec["imp_" + h] = np.nan
                continue
            rec["mo_" + h] = sgn * (Mc[r.ex].values[x] - r.price) / TICK
            rec["imp_" + h] = sgn * (Mc[r.ex].values[x] - m0) / TICK
        mk.append(rec)
    mk = pd.DataFrame(mk)
    rmap = {str(k): v for k, v in races.race.items()}
    mk["race"] = mk.ex.map(lambda e: rmap.get(e, e))
    mk.to_pickle(OUT / "p1d_fills.pkl")
    tab = []
    for purpose in ("mm", "taker_all", "arb", "exit", "cheapfav", "direction", "tilt"):
        d = mk[mk.taker] if purpose == "taker_all" else mk[mk.purpose == purpose]
        for oos in (False, True, None):
            dd = d if oos is None else d[d.oos == oos]
            row = dict(purpose=purpose, oos=oos, n=len(dd), shares=float(dd.q.sum()))
            for h in hs:
                for kind in ("mo_", "imp_"):
                    z = dd.dropna(subset=[kind + h])
                    w = z.q.clip(upper=2000)
                    row[kind + h] = float(np.average(z[kind + h], weights=w)) if len(z) else np.nan
                    mu, t, n = ctstat(z[kind + h], z.race) if len(z) > 2 else (np.nan, np.nan, len(z))
                    row["t_" + kind + h] = t
            tab.append(row)
    res["1d_fills"] = tab
    (OUT / "part1.json").write_text(json.dumps(res, indent=1, default=str))
    print("done part1")


# ---------------------------------------------------------------- part 2
STEP = 300.0
SPLIT_A = pd.Timestamp("2026-10-10 00:00", tz="UTC").timestamp()  # long-horizon IS before, OOS after


def stitched_panel():
    """Per leg, a 5-min grid 1 Oct - 23 Oct 22:45 UTC: SIG mid/bid/ask and the same-party outside fair.

    Sample A (to 21 Oct 22:00) is the sig_convergence panel (SIG 5-min candle close +- a constant half spread, Polymarket and
    Kalshi same-party fair); sample B (real touches, ~85 s) is resampled to the same grid. A's closes are trade prices, so
    they are smoothed with a 30-min rolling median to take out most of the bid-ask bounce.
    """
    P = pd.read_pickle(ROOT / "data" / "research_sig_convergence" / "panel.pkl")
    out = {}
    for ex, d in P.items():
        if "A" not in d or "B" not in d:
            continue
        A_, B_ = d["A"], d["B"]
        a = pd.DataFrame({"mid": A_["mid"], "fair": A_["fair"]}, index=pd.to_datetime(A_["t"], unit="s", utc=True))
        a["mid"] = a.mid.rolling(6, min_periods=3).median()
        a["hs"] = d["hs"]
        b = pd.DataFrame({"mid": B_["mid"], "fair": B_["fair"], "hs": (np.asarray(B_["ask"]) - np.asarray(B_["bid"])) / 2},
                         index=pd.to_datetime(B_["t"], unit="s", utc=True))
        b = b.resample("5min").last()
        x = pd.concat([a, b]).sort_index()
        x = x[~x.index.duplicated(keep="last")].resample("5min").last()
        x[["mid", "fair", "hs"]] = x[["mid", "fair", "hs"]].ffill(limit=12)  # at most 1 h stale
        out[ex] = dict(leg=d["leg"], df=x)
    return out


def part2():
    P = stitched_panel()
    res = {}
    # 2a. Raw gap held to a fixed date (the cash objective): buy SIG legs cheap vs the outside fair, sell H days later.
    rows = []
    for ex, d in P.items():
        x, leg = d["df"], d["leg"]
        t = x.index
        days = pd.date_range("2026-10-02 18:00", "2026-10-21 18:00", freq="24h", tz="UTC")
        for t0 in days:
            if t0 not in x.index:
                continue
            r0 = x.loc[t0]
            if np.isnan(r0.mid) or np.isnan(r0.fair) or np.isnan(r0.hs):
                continue
            g0 = (r0.mid - r0.fair) / TICK
            side = -np.sign(g0)
            if side == 0:
                continue
            band = "fav" if r0.fair > 0.7 else "dog" if r0.fair < 0.3 else "mid"
            for H in (1, 3, 5, 7, 10):
                t1 = t0 + pd.Timedelta(days=H)
                if t1 not in x.index or t1 > t[-1]:
                    continue
                r1 = x.loc[t1]
                if np.isnan(r1.mid) or np.isnan(r1.fair):
                    continue
                cost = (r0.hs + (r1.hs if not np.isnan(r1.hs) else r0.hs)) / TICK
                pnl = side * (r1.mid - r0.mid) / TICK - cost
                closure = side * ((r1.mid - r1.fair) - (r0.mid - r0.fair)) / TICK  # gap part only (outside held fixed)
                rows.append(dict(ex=ex, race=leg["race"], cat=leg["cat"], t0=t0.timestamp(), oos=t0.timestamp() >= SPLIT_A,
                                 H=H, g0=abs(g0), band=band, pnl=pnl, gap_pnl=closure - cost, cost=cost))
    gp = pd.DataFrame(rows)
    gp.to_pickle(OUT / "p2a_rawgap.pkl")
    tab = []
    for X in (4, 8, 12, 16):
        for band in ("all", "fav", "dog"):
            for oos in (False, True):
                for H in (1, 3, 5, 7, 10):
                    d = gp[(gp.g0 >= X) & (gp.oos == oos) & (gp.H == H)]
                    if band != "all":
                        d = d[d.band == band]
                    mu, tt, n = ctstat(d.pnl, d.race) if len(d) > 2 else (np.nan, np.nan, len(d))
                    mg, tg, _ = ctstat(d.gap_pnl, d.race) if len(d) > 2 else (np.nan, np.nan, 0)
                    tab.append(dict(X=X, band=band, oos=oos, H=H, n=n, races=d.race.nunique(), pnl=mu, t=tt,
                                    gap_pnl=mg, t_gap=tg))
    res["2a_rawgap_fixed_date"] = tab

    # 2b. Deviation trade on legs whose gap was stable and reverted fast in training (A before 10 Oct).
    stats = {}
    for ex, d in P.items():
        x = d["df"]
        g = (x.mid - x.fair) / TICK
        dev = g - g.rolling(288, min_periods=144).median()
        x["dev"] = dev
        tr = x[x.index < pd.Timestamp(SPLIT_A, unit="s", tz="UTC")]
        dv = tr.dev.dropna()
        if len(dv) < 500:
            continue
        y = tr.dev.shift(-288) - tr.dev  # 24 h change
        ok = y.notna() & tr.dev.notna()
        if ok.sum() < 300:
            continue
        stats[ex] = dict(gap_sd=float(g[tr.index].std()), slope24=float(np.polyfit(tr.dev[ok], y[ok], 1)[0]))
    st = pd.DataFrame(stats).T
    fast = set(st[(st.slope24 <= st.slope24.median()) & (st.gap_sd <= st.gap_sd.median())].index)
    rows = []
    for ex, d in P.items():
        x = d["df"]
        if "dev" not in x:
            continue
        busy = None
        idx = x.index
        dv, mid, hs = x.dev.values, x.mid.values, x.hs.values
        for i in range(len(idx)):
            if idx[i].timestamp() < SPLIT_A or (busy is not None and idx[i] < busy):
                continue
            if np.isnan(dv[i]) or abs(dv[i]) < 4 or np.isnan(hs[i]):
                continue
            side = -np.sign(dv[i])
            for H in (1, 3):
                j = i + int(H * 288)
                if j >= len(idx) or np.isnan(mid[j]):
                    continue
                pnl = side * (mid[j] - mid[i]) / TICK - (hs[i] + (hs[j] if not np.isnan(hs[j]) else hs[i])) / TICK
                rows.append(dict(ex=ex, race=d["leg"]["race"], H=H, x0=abs(dv[i]), fast=ex in fast, pnl=pnl,
                                 sampleB=idx[i] >= pd.Timestamp("2026-10-21 23:49", tz="UTC")))
            busy = idx[i] + pd.Timedelta(hours=24)
    dt = pd.DataFrame(rows)
    dt.to_pickle(OUT / "p2b_fastlegs.pkl")
    tab = []
    for X in (4, 6, 8):
        for H in (1, 3):
            for grp in ("all", "fast", "slow"):
                d = dt[(dt.x0 >= X) & (dt.H == H)]
                if grp != "all":
                    d = d[d.fast == (grp == "fast")]
                mu, tt, n = ctstat(d.pnl, d.race) if len(d) > 2 else (np.nan, np.nan, len(d))
                tab.append(dict(X=X, H=H, legs=grp, n=n, pnl=mu, t=tt))
    res["2b_fast_legs"] = dict(n_fast=len(fast), n_scored=len(st), table=tab)

    # 2c. Timed liquidation: a holding must be sold inside an L-day window anyway (spread is sunk). Sell when the leg is rich
    # against its own usual gap (dev >= X for a YES holding, <= -X for a NO holding), else at the window's end; compare with
    # selling evenly through the window (TWAP) and with selling at the start.
    rows = []
    for ex, d in P.items():
        x = d["df"]
        if "dev" not in x:
            continue
        for L in (1, 3):
            starts = pd.date_range("2026-10-03 00:00", "2026-10-23 00:00", freq="12h", tz="UTC")
            for w0 in starts:
                w1 = w0 + pd.Timedelta(days=L)
                if w1 > x.index[-1]:
                    continue
                win = x.loc[w0:w1]
                if win.mid.isna().mean() > 0.3 or win.dev.isna().mean() > 0.3:
                    continue
                for hold in (1, -1):  # 1 = YES holding (sell at the bid), -1 = NO holding (sell NO, i.e. buy YES at the ask)
                    px = (win.mid - hold * win.hs).values  # sale price in YES terms
                    val = hold * px
                    twap = np.nanmean(val[::12])  # hourly clips
                    start = val[0] if not np.isnan(val[0]) else np.nan
                    rec = dict(ex=ex, race=d["leg"]["race"], L=L, w0=w0.timestamp(), oos=w0.timestamp() >= SPLIT_A,
                               hold=hold, sampleB=w0 >= pd.Timestamp("2026-10-21 23:49", tz="UTC"))
                    for X in (2, 3, 4, 6):
                        hit = np.where(hold * win.dev.values >= X)[0]
                        k = hit[0] if len(hit) else len(val) - 1
                        sale = val[k] if not np.isnan(val[k]) else np.nanmean(val[-12:])
                        rec[f"vs_twap_{X}"] = (sale - twap) / TICK
                        rec[f"vs_start_{X}"] = (sale - start) / TICK
                        rec[f"hit_{X}"] = bool(len(hit))
                    rows.append(rec)
    tl = pd.DataFrame(rows)
    tl.to_pickle(OUT / "p2c_timed_liq.pkl")
    tab = []
    for L in (1, 3):
        for X in (2, 3, 4, 6):
            for oos in (False, True):
                d = tl[(tl.L == L) & (tl.oos == oos)].dropna(subset=[f"vs_twap_{X}"])
                mu, tt, n = ctstat(d[f"vs_twap_{X}"], d.race) if len(d) > 2 else (np.nan, np.nan, len(d))
                ms, ts_, _ = ctstat(d[f"vs_start_{X}"].dropna(), d.dropna(subset=[f"vs_start_{X}"]).race) if len(d) > 2 else (np.nan, np.nan, 0)
                hitm = d[d[f"hit_{X}"]]
                mh, th, nh = ctstat(hitm[f"vs_twap_{X}"], hitm.race) if len(hitm) > 2 else (np.nan, np.nan, len(hitm))
                tab.append(dict(L=L, X=X, oos=oos, n=n, vs_twap=mu, t=tt, vs_start=ms, t_start=ts_,
                                hit_share=float(d[f"hit_{X}"].mean()) if len(d) else np.nan, hit_vs_twap=mh, t_hit=th))
    res["2c_timed_liquidation"] = tab
    (OUT / "part2.json").write_text(json.dumps(res, indent=1, default=str))
    print("done part2")


def part2_real():
    """Real-quote checks of the two part-2 leads (venue_fair, 21 Oct 23:49 onward; no candles, no stitch)."""
    res = {}
    gp = pd.read_pickle(OUT / "p2a_rawgap.pkl")
    stitch = pd.Timestamp("2026-10-21 22:00", tz="UTC").timestamp()
    gp["exit_in_B"] = gp.t0 + gp.H * 86400 > stitch
    tab = []
    for X in (8, 12):
        for H in (3, 5, 7):
            for lab, d in (("IS (t0 2-9 Oct)", gp[~gp.oos]), ("OOS, exit before 21 Oct 22:00 (candles both ends)", gp[gp.oos & ~gp.exit_in_B]),
                           ("OOS, exit on real quotes (stitched)", gp[gp.oos & gp.exit_in_B])):
                d = d[(d.g0 >= X) & (d.H == H)]
                mu, tt, n = ctstat(d.pnl, d.race) if len(d) > 2 else (np.nan, np.nan, len(d))
                tab.append(dict(X=X, H=H, sample=lab, n=n, pnl=mu, t=tt))
    res["2a_split"] = tab

    # Timed liquidation on real touches only.
    vf = load_vf()
    races = vf.drop_duplicates("exchange_id").set_index("exchange_id")[["race", "party"]]
    B, A, F = wide(vf, "sig_bid"), wide(vf, "sig_ask"), wide(vf, "fair")
    M = (B + A) / 2
    G = (M - F) / TICK
    dev = G - G.rolling(window=int(12 * HOUR / 85), min_periods=int(6 * HOUR / 85)).median()
    T = B.index.values
    rows = []
    for L in (0.5, 1.0):
        for w0 in np.arange(T[0] + 6 * HOUR, T[-1] - L * 86400, 3 * HOUR):
            i0, i1 = at(T, w0), at(T, w0 + L * 86400)
            for ex in B.columns:
                bb, aa, dv = B[ex].values[i0:i1 + 1], A[ex].values[i0:i1 + 1], dev[ex].values[i0:i1 + 1]
                if np.isnan(bb).mean() > 0.2 or np.isnan(dv).mean() > 0.2:
                    continue
                for hold in (1, -1):
                    val = bb if hold > 0 else -aa  # YES holding sells at the bid; NO holding pays the ask to close
                    clips = val[:: max(1, int(HOUR / 85))]
                    twap = np.nanmean(clips)
                    rec = dict(ex=ex, race=races.race.get(ex), L=L, w0=w0, oos=w0 >= SPLIT, hold=hold)
                    for X in (2, 3, 4):
                        hit = np.where(hold * dv >= X)[0]
                        k = hit[0] + 1 if len(hit) and hit[0] + 1 < len(val) else len(val) - 1  # one poll of latency
                        sale = val[k] if not np.isnan(val[k]) else np.nanmean(val[-10:])
                        rec[f"vs_twap_{X}"] = (sale - twap) / TICK
                        rec[f"hit_{X}"] = bool(len(hit))
                    rows.append(rec)
    tl = pd.DataFrame(rows)
    tl.to_pickle(OUT / "p2c_timed_liq_real.pkl")
    tab = []
    for L in (0.5, 1.0):
        for X in (2, 3, 4):
            for oos in (False, True, None):
                d = tl[tl.L == L] if oos is None else tl[(tl.L == L) & (tl.oos == oos)]
                d = d.dropna(subset=[f"vs_twap_{X}"])
                # windows overlap (3 h step), so cluster by race AND count independent windows
                mu, tt, n = ctstat(d[f"vs_twap_{X}"], d.race) if len(d) > 2 else (np.nan, np.nan, len(d))
                daym = d.groupby(d.w0 // (L * 86400))[f"vs_twap_{X}"].mean()
                tab.append(dict(L=L, X=X, oos=oos, n=n, windows=d.w0.nunique(), vs_twap=mu, t=tt,
                                hit_share=float(d[f"hit_{X}"].mean()), by_block=daym.round(2).tolist()))
    res["2c_real"] = tab

    # Same test for whole sets (most of what the wind-down sells). A sell-set (NO on every leg) is closed by buying YES on
    # every leg: proceeds n - 1 - (sum of asks - 1) per set relative to payout, so sell when the sum of asks is low.
    rows = []
    for race, grp in races.groupby("race"):
        legs = [l for l in grp.index if l in A.columns]
        if len(legs) != len(grp) or len(legs) < 2:
            continue
        sa = A[legs].sum(axis=1, min_count=len(legs))
        sm = M[legs].sum(axis=1, min_count=len(legs))
        sdev = (sm - sm.rolling(window=int(12 * HOUR / 85), min_periods=int(6 * HOUR / 85)).median()) / TICK
        for L in (0.5, 1.0):
            for w0 in np.arange(T[0] + 6 * HOUR, T[-1] - L * 86400, 3 * HOUR):
                i0, i1 = at(T, w0), at(T, w0 + L * 86400)
                val, dv = -sa.values[i0:i1 + 1], sdev.values[i0:i1 + 1]
                if np.isnan(val).mean() > 0.2 or np.isnan(dv).mean() > 0.2:
                    continue
                twap = np.nanmean(val[:: max(1, int(HOUR / 85))])
                rec = dict(race=race, legs=len(legs), L=L, w0=w0, oos=w0 >= SPLIT)
                for X in (1, 2, 3):
                    hit = np.where(dv <= -X)[0]
                    k = hit[0] + 1 if len(hit) and hit[0] + 1 < len(val) else len(val) - 1
                    sale = val[k] if not np.isnan(val[k]) else np.nanmean(val[-10:])
                    rec[f"vs_twap_{X}"] = (sale - twap) / TICK
                    rec[f"hit_{X}"] = bool(len(hit))
                rows.append(rec)
    ts_ = pd.DataFrame(rows)
    ts_.to_pickle(OUT / "p2c_timed_sets_real.pkl")
    tab = []
    for L in (0.5, 1.0):
        for X in (1, 2, 3):
            for oos in (False, True, None):
                d = ts_[ts_.L == L] if oos is None else ts_[(ts_.L == L) & (ts_.oos == oos)]
                d = d.dropna(subset=[f"vs_twap_{X}"])
                if len(d) < 3:
                    continue
                mu, tt, n = ctstat(d[f"vs_twap_{X}"], d.race)
                blk = d.groupby(d.w0 // (L * 86400))[f"vs_twap_{X}"].mean()
                tab.append(dict(L=L, X=X, oos=oos, n=n, races=d.race.nunique(), vs_twap=mu, t=tt,
                                hit_share=float(d[f"hit_{X}"].mean()), by_block=blk.round(2).tolist()))
    res["2c_sets_real"] = tab
    (OUT / "part2_real.json").write_text(json.dumps(res, indent=1, default=str))
    print("done part2_real")


# ---------------------------------------------------------------- part 3: calibration of the outside venues
CAL = OUT / "calib"
HORIZONS = [60, 30, 14, 7, 3, 1, 0.5, 0]
HLAB = {60: "60 d", 30: "30 d", 14: "14 d", 7: "7 d", 3: "3 d", 1: "1 d", 0.5: "eve", 0: "day"}
BUCKETS = [0, 0.05, 0.15, 0.3, 0.5, 0.7, 0.85, 0.95, 1.0001]


def _lg(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def _sig(x):
    return 1 / (1 + np.exp(-x))


def fit_ll(p, y, prior=None):
    """Logit-linear recalibration p' = sigmoid(a + b logit p) by maximum likelihood (optional normal prior on a, b)."""
    from scipy.optimize import minimize
    x = _lg(np.asarray(p, float))
    y = np.asarray(y, float)

    def nll(th):
        z = th[0] + th[1] * x
        v = np.sum(np.logaddexp(0, z) - y * z)
        if prior:
            v += th[0] ** 2 / (2 * prior[0] ** 2) + (th[1] - 1) ** 2 / (2 * prior[1] ** 2)
        return v

    r = minimize(nll, [0.0, 1.0], method="Nelder-Mead", options=dict(xatol=1e-6, fatol=1e-8, maxiter=4000))
    return r.x


def calib_panel(stale_max=3.0, two_party=True):
    legs = pd.read_csv(CAL / "legs_all.csv", dtype={"market_id": str})
    paths = pd.read_csv(CAL / "paths.csv", dtype={"market_id": str})
    legs = legs[(legs.us == 1) & legs.outcome.isin([0, 1])]
    if two_party:  # SIG-like legs: D or R in a state/district race (no third-party legs, national winner or mayoral fields)
        legs = legs[legs.party.isin(["D", "R"]) & legs.office.isin(["president", "senate", "governor", "house"])
                    & ~legs.race_id.str.contains("US-winner|popvote|control", regex=True)]
    legs = legs.assign(cycle=legs.cycle.where(legs.cycle.isin([2020, 2022, 2024]), 2025))  # off-years pooled as "2025"
    key = ["venue", "market_id", "party"] if "party" in paths.columns else ["venue", "market_id"]
    paths = paths[paths.days_before.isin(HORIZONS) & paths.price.notna() & (paths.stale_days.fillna(0) <= stale_max)]
    df = legs.merge(paths, on=key, how="inner")
    df = df[(df.price > 0) & (df.price < 1)]
    return df


def boot_races(df, fn, n=1000, seed=7):
    rng = np.random.default_rng(seed)
    df = df.reset_index(drop=True)
    groups = list(df.groupby("race_id").indices.values())
    out = []
    for _ in range(n):
        pick = rng.integers(0, len(groups), len(groups))
        out.append(fn(df.iloc[np.concatenate([groups[i] for i in pick])]))
    return np.array(out)


def part3():
    df = calib_panel()
    df.to_pickle(OUT / "p3_panel.pkl")
    cov = df.groupby(["venue", "cycle", "days_before"]).race_id.nunique().unstack().fillna(0).astype(int)
    res = {"coverage": {f"{v} {c}": {str(k): int(x) for k, x in row.items()} for (v, c), row in cov.iterrows()}}
    # Reliability per horizon and bucket (all legs; bootstrap resamples whole races).
    rel = []
    for h in HORIZONS:
        for venue in ("all", "polymarket", "kalshi"):
            d = df[df.days_before == h]
            if venue != "all":
                d = d[d.venue == venue]
            if venue == "all":  # one venue per race to avoid counting a race twice: prefer Polymarket
                d = d.sort_values("venue", ascending=False).drop_duplicates(["race_id", "party"], keep="first")
            if len(d) < 20:
                continue
            d = d.assign(b=pd.cut(d.price, BUCKETS, right=False, labels=False))
            for b, dd in d.groupby("b"):
                if len(dd) < 3:
                    continue
                bs = boot_races(dd, lambda x: x.outcome.mean() - x.price.mean(), n=400)
                rel.append(dict(h=h, venue=venue, bucket=int(b), lo=BUCKETS[int(b)], hi=min(BUCKETS[int(b) + 1], 1), n=len(dd),
                                races=dd.race_id.nunique(), price=float(dd.price.mean()), won=float(dd.outcome.mean()),
                                diff=float(dd.outcome.mean() - dd.price.mean()), ci_lo=float(np.percentile(bs, 2.5)),
                                ci_hi=float(np.percentile(bs, 97.5))))
    res["reliability"] = rel
    # Logit-linear fits per horizon (all cycles), with race-bootstrap CIs.
    fits = []
    for h in HORIZONS:
        d = df[df.days_before == h].sort_values("venue", ascending=False).drop_duplicates(["race_id", "party"], keep="first")
        if len(d) < 30:
            continue
        a, b = fit_ll(d.price, d.outcome)
        bs = boot_races(d, lambda x: fit_ll(x.price, x.outcome)[1], n=300)
        am, bm = fit_ll(d.price, d.outcome, prior=(0.25, 0.25))
        fits.append(dict(h=h, n=len(d), races=d.race_id.nunique(), a=a, b=b, b_lo=float(np.percentile(bs, 2.5)),
                         b_hi=float(np.percentile(bs, 97.5)), a_map=am, b_map=bm,
                         p_at={str(p): float(_sig(a + b * _lg(p))) for p in (0.6, 0.7, 0.8, 0.9, 0.95)}))
    res["fits"] = fits
    # Out of sample by election cycle: fit on the other cycles, score the held-out one (log loss, Brier) vs raw prices.
    oos = []
    for h in HORIZONS:
        d = df[df.days_before == h].sort_values("venue", ascending=False).drop_duplicates(["race_id", "party"], keep="first")
        for cyc in sorted(d.cycle.unique()):
            tr, te = d[d.cycle != cyc], d[d.cycle == cyc]
            if len(te) < 10 or len(tr) < 30:
                continue
            for lab, prior in (("ml", None), ("map", (0.25, 0.25))):
                a, b = fit_ll(tr.price, tr.outcome, prior=prior)
                p0 = np.clip(te.price.values, 1e-4, 1 - 1e-4)
                p1 = np.clip(_sig(a + b * _lg(p0)), 1e-4, 1 - 1e-4)
                y = te.outcome.values
                ll0 = -np.mean(y * np.log(p0) + (1 - y) * np.log(1 - p0))
                ll1 = -np.mean(y * np.log(p1) + (1 - y) * np.log(1 - p1))
                # per-race paired difference for a CI
                dd = te.assign(l0=-(y * np.log(p0) + (1 - y) * np.log(1 - p0)), l1=-(y * np.log(p1) + (1 - y) * np.log(1 - p1)))
                per = dd.groupby("race_id").apply(lambda x: (x.l0 - x.l1).sum(), include_groups=False)
                m, t, _ = ctstat(per.values, per.index.values)
                oos.append(dict(h=h, cycle=int(cyc), fit=lab, a=a, b=b, n=len(te), races=te.race_id.nunique(), ll_raw=ll0,
                                ll_recal=ll1, gain_per_race=m, t=t, brier_raw=float(np.mean((p0 - y) ** 2)),
                                brier_recal=float(np.mean((p1 - y) ** 2))))
    res["oos_by_cycle"] = oos
    # Betting test: buy the favourite (price 0.5-0.97) at each horizon and hold to resolution, by cycle.
    bet = []
    for h in HORIZONS:
        d = df[(df.days_before == h)].sort_values("venue", ascending=False).drop_duplicates(["race_id", "party"], keep="first")
        for side, sel in (("favourite 0.70-0.97", (d.price >= 0.7) & (d.price <= 0.97)), ("longshot 0.03-0.30", (d.price >= 0.03) & (d.price <= 0.30))):
            for cyc in ["all"] + sorted(d.cycle.unique()):
                dd = d[sel] if cyc == "all" else d[sel & (d.cycle == cyc)]
                if len(dd) < 5:
                    continue
                ret = (dd.outcome - dd.price) / dd.price * 1000
                bet.append(dict(h=h, side=side, cycle=str(cyc), n=len(dd), won=int(dd.outcome.sum()), exp=float(dd.price.sum()),
                                per_1k=float(ret.mean())))
    res["bets"] = bet
    # Same race, Polymarket vs Kalshi (same-terms gap).
    both = df[df.days_before.isin([7, 1])].pivot_table(index=["race_id", "party", "days_before"], columns="venue", values="price")
    if {"polymarket", "kalshi"} <= set(both.columns):
        bb = both.dropna()
        res["pm_vs_kalshi"] = dict(n=len(bb), mean_abs_diff=float((bb.polymarket - bb.kalshi).abs().mean()),
                                   mean_diff=float((bb.polymarket - bb.kalshi).mean()))
    (OUT / "part3.json").write_text(json.dumps(res, indent=1, default=str))
    print("done part3")


def part3_apply():
    """What the recalibration does to today's fairs (latest venue_fair poll), the MM gate, cheap favourites and the punt."""
    r3 = json.loads((OUT / "part3.json").read_text())
    fits = {f["h"]: f for f in r3["fits"]}
    con = db()
    vf = pd.read_sql("select race, party, fair, sig_bid, sig_ask, exchange_id from venue_fair "
                     "where ts = (select max(ts) from venue_fair)", con)
    vf = vf[(vf.fair > 0) & (vf.fair < 1)]
    out = {}
    for lab, h in (("today (10 days out: 7-day map)", 7.0), ("punt (election eve map)", 0.5)):
        f = fits.get(h)
        if f is None:
            continue
        for kind in ("ml", "map"):
            a, b = (f["a"], f["b"]) if kind == "ml" else (f["a_map"], f["b_map"])
            p2 = _sig(a + b * _lg(vf.fair.values))
            # renormalise within each race so the legs still sum to the race's outside total
            d = vf.assign(p2=p2)
            tot = d.groupby("race").fair.transform("sum")
            d["p2"] = d.p2 * tot / d.groupby("race").p2.transform("sum")
            d["shift"] = (d.p2 - d.fair) / TICK
            mid = (d.sig_bid + d.sig_ask) / 2
            gate_old = (mid - d.fair).abs() > 3 * TICK
            gate_new = (mid - d.p2).abs() > 3 * TICK
            cf_old = (d.fair >= 0.8) & (d.fair - d.sig_ask >= 0.02)
            cf_new = (d.p2 >= 0.8) & (d.p2 - d.sig_ask >= 0.02)
            # NO side of underdog legs (buying the favourite through the other leg)
            cfn_old = (1 - d.fair >= 0.8) & ((1 - d.fair) - (1 - d.sig_bid) >= 0.02)
            cfn_new = (1 - d.p2 >= 0.8) & ((1 - d.p2) - (1 - d.sig_bid) >= 0.02)
            bands = pd.cut(d.fair, [0, 0.05, 0.15, 0.3, 0.5, 0.7, 0.85, 0.95, 1], labels=False)
            out[f"{lab} [{kind}: a {a:+.2f}, b {b:.2f}]"] = dict(
                legs=len(d), mean_abs_shift_ticks=float(d["shift"].abs().mean()),
                shift_by_band={str([0, 0.05, 0.15, 0.3, 0.5, 0.7, 0.85, 0.95, 1][int(k)]): float(v)
                               for k, v in d.groupby(bands)["shift"].mean().items()},
                legs_shift_over_1_tick=int((d["shift"].abs() > 1).sum()), legs_shift_over_3_ticks=int((d["shift"].abs() > 3).sum()),
                mm_gate_flips=int((gate_old != gate_new).sum()),
                cheapfav_yes_routes_old=int(cf_old.sum()), cheapfav_yes_routes_new=int(cf_new.sum()),
                cheapfav_no_routes_old=int(cfn_old.sum()), cheapfav_no_routes_new=int(cfn_new.sum()),
                cheapfav_edge_old_per_share=float((d.fair - d.sig_ask)[cf_old].mean()) if cf_old.any() else None,
                cheapfav_edge_new_per_share=float((d.p2 - d.sig_ask)[cf_old].mean()) if cf_old.any() else None,
                biggest=d.reindex(d["shift"].abs().sort_values(ascending=False).index).head(8)[
                    ["race", "party", "fair", "p2", "sig_bid", "sig_ask"]].round(3).to_dict("records"))
    # the punt screener's single map (A_MAP 0.14, B_MAP 1.37) vs the eve fit
    eve = fits.get(0.5)
    if eve:
        out["screener_map_vs_eve_fit"] = {str(p): dict(screener=float(_sig(0.14 + 1.37 * _lg(p))),
                                                       eve_ml=float(_sig(eve["a"] + eve["b"] * _lg(p))),
                                                       eve_map=float(_sig(eve["a_map"] + eve["b_map"] * _lg(p))))
                                          for p in (0.6, 0.7, 0.8, 0.9, 0.95)}
    (OUT / "part3_apply.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str)[:5000])


# ---------------------------------------------------------------- figures
def _ci(vals, groups):
    m, t, n = ctstat(vals, groups)
    se = abs(m / t) if t == t and t != 0 else np.nan
    return m, m - 1.96 * se, m + 1.96 * se, n


def _glossary(fig, text, y=0.01, h=0.13):
    ax = fig.add_axes([0.07, y, 0.88, h])
    ax.axis("off")
    ax.add_patch(__import__("matplotlib").patches.FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0.01",
                 transform=ax.transAxes, facecolor="#f4f4f0", edgecolor="#999"))
    ax.text(0.01, 0.95, "Glossary", fontsize=10, fontweight="bold", va="top", transform=ax.transAxes)
    ax.text(0.01, 0.80, text, fontsize=8.2, va="top", transform=ax.transAxes, linespacing=1.35)


def figs():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    BLUE, RED, GREY, GREEN = "#1f77b4", "#d62728", "#555555", "#2ca02c"

    # Part 1: mean reversion
    ev = pd.read_pickle(OUT / "p1a_events.pkl")
    fl = pd.read_pickle(OUT / "p1d_fills.pkl")
    hs = ["30m", "2h", "6h", "12h"]
    hx = [0.5, 2, 6, 12]
    fig = plt.figure(figsize=(13, 22))
    fig.suptitle("Mean reversion we don't trade yet: does any of it pay after costs?", fontsize=16, fontweight="bold", y=0.985)
    fig.text(0.5, 0.968, "SIG real quotes 22-24 Oct; training before 23 Oct 12:00 UTC, test after. Every rule pays the bid-ask "
             "spread and waits one bot poll (~85 s) before trading.", ha="center", fontsize=10, color=GREY)
    axes = [fig.add_axes([0.08, y, 0.88, 0.2]) for y in (0.71, 0.445, 0.18)]
    ax = axes[0]
    for lab, sel, col in (("all SIG-only moves", ev.k >= 2, GREY), ("moves away from the outside price", (ev.k >= 2) & ev.away, RED),
                          ("moves toward the outside price", (ev.k >= 2) & ~ev.away, BLUE)):
        for oos, ls in ((False, ":"), (True, "-")):
            d = ev[sel & (ev.oos == oos)]
            st = [_ci(d[h].dropna(), d.dropna(subset=[h]).race) for h in hs]
            ax.plot(hx, [s[0] for s in st], ls, marker="o" if oos else "^", color=col,
                    label=f"{lab}, {'test' if oos else 'training'} ({st[0][3]} moves)")
            if oos:
                ax.fill_between(hx, [s[1] for s in st], [s[2] for s in st], color=col, alpha=0.12)
    ax.axhline(0, color="k", lw=0.8)
    ax.set_title("A. Bet that a SIG price move with no outside move reverses (fade it)", loc="left", fontweight="bold")
    ax.set_ylabel("Profit per share after costs (ticks)")
    ax.set_xscale("log"); ax.set_xticks(hx); ax.set_xticklabels(hs)
    ax.set_xlabel("Holding time before selling back")
    ax.legend(fontsize=8, loc="lower left", ncol=2)
    ax = axes[1]
    for lab, sel, col in (("follow a 2+ tick move toward the outside price", (ev.k >= 2) & ~ev.away, BLUE),
                          ("follow a 3+ tick move toward the outside price", (ev.k >= 3) & ~ev.away, GREEN)):
        for oos, ls in ((False, ":"), (True, "-")):
            d = ev[sel & (ev.oos == oos)]
            st = [_ci(d[h + "_fol"].dropna(), d.dropna(subset=[h + "_fol"]).race) for h in hs]
            ax.plot(hx, [s[0] for s in st], ls, marker="o" if oos else "^", color=col,
                    label=f"{lab}, {'test' if oos else 'training'} ({st[0][3]} moves)")
            if oos:
                ax.fill_between(hx, [s[1] for s in st], [s[2] for s in st], color=col, alpha=0.12)
    ax.axhline(0, color="k", lw=0.8)
    ax.set_title("B. The only lead from A: bet that a SIG move toward the outside price keeps going", loc="left", fontweight="bold")
    ax.set_ylabel("Profit per share after costs (ticks)")
    ax.set_xscale("log"); ax.set_xticks(hx); ax.set_xticklabels(hs)
    ax.set_xlabel("Holding time before selling back")
    ax.legend(fontsize=8, loc="lower right")
    ax = axes[2]
    fh = ["1m", "5m", "15m", "30m", "1h", "3h", "6h", "12h", "24h"]
    fx = [1 / 60, 5 / 60, 0.25, 0.5, 1, 3, 6, 12, 24]
    for lab, sel, col_, key in (("our market-making fills: value vs our fill price", fl.purpose == "mm", BLUE, "mo_"),
                                ("our taking fills (arb, exits, sleeves): SIG mid move in our direction", fl.taker, RED, "imp_")):
        for oos, ls in ((False, ":"), (True, "-")):
            d = fl[sel & (fl.oos == oos)]
            st = [_ci(d[key + h].dropna(), d.dropna(subset=[key + h]).race) for h in fh]
            ax.plot(fx, [s[0] for s in st], ls, marker="o" if oos else "^", color=col_,
                    label=f"{lab}, {'test' if oos else 'training'} ({len(d)} fills)")
            if oos:
                ax.fill_between(fx, [s[1] for s in st], [s[2] for s in st], color=col_, alpha=0.12)
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xscale("log"); ax.set_xticks(fx); ax.set_xticklabels(fh)
    ax.set_title("C. After our own fills: does the price come back (something to harvest)?", loc="left", fontweight="bold")
    ax.set_ylabel("Ticks per share")
    ax.set_xlabel("Time after the fill")
    ax.legend(fontsize=8, loc="lower left")
    r1 = json.loads((OUT / "part1.json").read_text())
    bk = r1["1b_basket"]
    bs = pd.DataFrame(r1["1c_buyset"])
    bso = bs[(bs.filt == "all") & (bs.exits == "all")]
    _glossary(fig, (
        "SIG-only move: SIG's mid moves 2+ ticks within 10 min while the Polymarket/Kalshi fair moves under 0.3 points. "
        "tick: 0.005. costs: the bid-ask spread (one full spread per round trip).\n"
        "away / toward the outside price: whether the move widened or narrowed SIG's gap to the fair. fade = trade against the "
        "move; follow = trade with it.\n"
        "C, market-making fills: mid minus our fill price, signed so + is good for us. Taking fills: how far SIG's mid moved in the "
        "direction we traded (+ = the move stuck, no bounce-back to harvest).\n"
        f"Not drawn: cross-race basket (party-signed average deviation of all legs) has SD {bk['basket_sd_ticks']:.2f} tick vs "
        f"{bk['leg_dev_sd_ticks']:.1f} per leg: nothing to trade after a 1-tick spread.\nBuy-set deviation churn: "
        f"{int(bso[~bso.oos].n.iloc[0])} training / {int(bso[bso.oos].n.iloc[0])} test trades, "
        f"{bso[~bso.oos]['mean'].iloc[0]:+.1f} / {bso[bso.oos]['mean'].iloc[0]:+.1f} ticks per set: too few.\n"
        "dotted = training days (22 Oct - 23 Oct 12:00 UTC); solid = test days (to 24 Oct 13:00 UTC). shaded = 95% range of the "
        "test average, standard errors clustered by race."), y=0.01, h=0.115)
    fig.savefig(FIG / "ev_hunt_reversion.png", dpi=100)
    plt.close(fig)

    # Part 2: sweeping toward the outside fair under the cash objective
    gp = pd.read_pickle(OUT / "p2a_rawgap.pkl")
    stitch = pd.Timestamp("2026-10-21 22:00", tz="UTC").timestamp()
    gp["exit_in_B"] = gp.t0 + gp.H * 86400 > stitch
    tl = pd.read_pickle(OUT / "p2c_timed_liq_real.pkl")
    tsr = pd.read_pickle(OUT / "p2c_timed_sets_real.pkl")
    fig = plt.figure(figsize=(13, 17))
    fig.suptitle("Sweeping SIG toward the Polymarket/Kalshi fair when everything is sold by 3 Nov", fontsize=16,
                 fontweight="bold", y=0.985)
    fig.text(0.5, 0.965, "A: buy SIG legs far from the fair and sell them H days later. B: we must sell our holdings anyway; "
             "does waiting for a rich moment beat selling evenly?", ha="center", fontsize=10, color=GREY)
    axes = [fig.add_axes([0.08, y, 0.88, 0.3]) for y in (0.6, 0.2)]
    ax = axes[0]
    Hs = [1, 3, 5, 7, 10]
    for lab, sel, ls, col, mk in (("training: entries 2-9 Oct (trade prices both ends)", ~gp.oos, ":", GREY, "^"),
                                  ("test: entries 10-20 Oct, exit before 21 Oct 22:00 (trade prices both ends)", gp.oos & ~gp.exit_in_B, "-", BLUE, "o"),
                                  ("test, SUSPECT: exit priced on real quotes, entry on trade prices", gp.oos & gp.exit_in_B, "-.", RED, "s")):
        d = gp[sel & (gp.g0 >= 8)]
        st = [(_ci(d[d.H == H].pnl, d[d.H == H].race) if (d.H == H).sum() > 2 else (np.nan,) * 4) for H in Hs]
        ax.plot(Hs, [s[0] for s in st], ls, marker=mk, color=col, label=f"{lab} ({int(np.nansum([s[3] for s in st]))} trades)")
        if ls != ":":
            ax.fill_between(Hs, [s[1] for s in st], [s[2] for s in st], color=col, alpha=0.12)
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xticks(Hs)
    ax.set_xlabel("Days held before selling (the cash objective sells everything by 3 Nov)")
    ax.set_ylabel("Profit per share after costs (ticks)")
    ax.set_title("A. Buy SIG legs 8+ ticks from the outside fair (cheap ones, or sell rich ones), sell H days later", loc="left",
                 fontweight="bold")
    ax.legend(fontsize=8, loc="upper left")
    ax = axes[1]
    for lab, d, col, xs in (("single legs we hold (sell when rich vs their usual gap to the fair)", tl[tl.L == 0.5], BLUE, (2, 3, 4)),
                            ("whole 2-leg sets we hold (sell when the set is cheap to close vs its usual level)", tsr[tsr.L == 0.5], RED, (1, 2, 3))):
        for oos, ls, mk in ((False, ":", "^"), (True, "-", "o")):
            dd = d[d.oos == oos]
            st = [_ci(dd[f"vs_twap_{X}"].dropna(), dd.dropna(subset=[f"vs_twap_{X}"]).race) for X in xs]
            ax.plot(xs, [s[0] for s in st], ls, marker=mk, color=col, label=f"{lab}, {'test' if oos else 'training'} ({st[0][3]} sales)")
            if oos:
                ax.fill_between(xs, [s[1] for s in st], [s[2] for s in st], color=col, alpha=0.12)
    ax.axhline(0, color="k", lw=0.8)
    ax.set_xticks([1, 2, 3, 4])
    ax.set_xlabel("Wait until the holding is at least this many ticks better than its usual level (12-hour selling window)")
    ax.set_ylabel("Extra sale proceeds vs selling evenly\n(ticks per share or per set)")
    ax.set_title("B. Timed selling of what we already hold (real quotes only)", loc="left", fontweight="bold")
    ax.legend(fontsize=8, loc="upper right")
    _glossary(fig, (
        "outside fair: average of the same party's Polymarket and Kalshi prices. gap: SIG mid minus the fair. tick: 0.005.\n"
        "trade prices (A): before 21 Oct the only SIG prices are 5-min trade closes (smoothed over 30 min) +- a fixed half spread; "
        "after 21 Oct the bot recorded real bid/ask quotes.\nThe red line mixes the two price sources: the jump between them, not the "
        "market, can create its profit, so it is not evidence.\n"
        "usual level (B): the leg's gap, or the set's sum of mids, over the previous 12 hours (median). selling evenly: equal "
        "clips every hour at the touch over the same window.\n"
        "dotted = training, solid = test (A: by entry date; B: windows before / after 23 Oct 12:00 UTC). shaded = 95% range of "
        "the test average, clustered by race."), y=0.01, h=0.12)
    fig.savefig(FIG / "ev_hunt_sweep.png", dpi=100)
    plt.close(fig)

    # Part 3: calibration
    if (OUT / "part3.json").exists():
        r3 = json.loads((OUT / "part3.json").read_text())
        ap = json.loads((OUT / "part3_apply.json").read_text()) if (OUT / "part3_apply.json").exists() else {}
        fits = pd.DataFrame(r3["fits"]).sort_values("h", ascending=False)
        oos = pd.DataFrame(r3["oos_by_cycle"])
        rel = pd.DataFrame(r3["reliability"])
        hpos = {h: i for i, h in enumerate(HORIZONS)}
        fig = plt.figure(figsize=(13, 26))
        fig.suptitle("Are Polymarket/Kalshi election prices calibrated? (resolved US races)", fontsize=16, fontweight="bold", y=0.988)
        fig.text(0.5, 0.975, "Each point: markets priced that many days before the election, against who actually won. "
                 "A slope above 1 = favourites underpriced, longshots overpriced.", ha="center", fontsize=10, color=GREY)
        axs = [fig.add_axes([0.08, y, 0.88, 0.17]) for y in (0.775, 0.565, 0.355)]
        ax = axs[0]
        xs = [hpos[h] for h in fits.h]
        ax.plot(xs, fits.b, "-o", color=BLUE, label="slope fitted on all cycles (maximum likelihood)")
        ax.fill_between(xs, fits.b_lo.clip(upper=8), fits.b_hi.clip(upper=8), color=BLUE, alpha=0.12)
        ax.plot(xs, fits.b_map, "-s", color=GREEN, label="same, shrunk toward 1 (prior SD 0.25): what we would use")
        cyc_cols = {2020: "#9467bd", 2022: "#ff7f0e", 2023: "#8c564b", 2024: RED, 2025: "#17becf", 2026: GREY}
        for cyc, d in oos[oos.fit == "ml"].groupby("cycle"):
            d = d.sort_values("h", ascending=False)
            ax.plot([hpos[h] for h in d.h], d.b.clip(upper=8), ":^", color=cyc_cols.get(cyc, GREY),
                    label=f"fitted WITHOUT {cyc} (used to score {cyc})")
        ax.axhline(1, color="k", lw=0.8)
        ax.set_xticks(range(len(HORIZONS))); ax.set_xticklabels([HLAB[h] for h in HORIZONS])
        ax.set_ylim(0, 8)
        ax.set_ylabel("Calibration slope b")
        ax.set_title("A. How compressed toward 50% are outside prices, by time before the election", loc="left", fontweight="bold")
        ax.legend(fontsize=8, loc="upper left", ncol=2)
        ax = axs[1]
        for fitk, col, mk in (("ml", BLUE, "o"), ("map", GREEN, "s")):
            for cyc, d in oos[oos.fit == fitk].groupby("cycle"):
                d = d.sort_values("h", ascending=False)
                ax.plot([hpos[h] for h in d.h], d.gain_per_race * 1000, "-" + mk, color=cyc_cols.get(cyc, col), alpha=0.9,
                        label=f"{'raw fit' if fitk == 'ml' else 'shrunk fit'}, held-out cycle {cyc}")
                se = (d.gain_per_race / d.t).abs() * 1000
                if fitk == "map":
                    ax.fill_between([hpos[h] for h in d.h], d.gain_per_race * 1000 - 1.96 * se,
                                    d.gain_per_race * 1000 + 1.96 * se, color=cyc_cols.get(cyc, col), alpha=0.08)
        ax.axhline(0, color="k", lw=0.8)
        ax.set_ylim(-250, 250)
        low = oos[(oos.gain_per_race * 1000 < -250)]
        for _, rr in low.iterrows():
            ax.annotate(f"raw fit, {int(rr.cycle)} held out: {rr.gain_per_race * 1000:.0f} (training cycles perfectly separated, "
                        "slope blows up; the shrunk fit avoids it)", (hpos[rr.h], -240), fontsize=7.5, color=RED)
        ax.set_xticks(range(len(HORIZONS))); ax.set_xticklabels([HLAB[h] for h in HORIZONS])
        ax.set_ylabel("Better forecast per race\n(log-loss drop x 1000)")
        ax.set_title("B. Out of sample: does the recalibrated price beat the raw price on a cycle it never saw?", loc="left",
                     fontweight="bold")
        ax.legend(fontsize=8, loc="upper left", ncol=2)
        ax = axs[2]
        for h, col in ((30, GREY), (7, BLUE), (1, GREEN), (0, RED)):
            d = rel[(rel.h == h) & (rel.venue == "all")]
            if not len(d):
                continue
            ax.errorbar(d.price, d.won, yerr=[d.won - (d.price + d.ci_lo), (d.price + d.ci_hi) - d.won], fmt="-o", color=col,
                        capsize=3, label=f"{HLAB[h]} before ({int(d.n.sum())} legs)")
        f7 = fits[fits.h == 7]
        if len(f7):
            pp = np.linspace(0.01, 0.99, 99)
            ax.plot(pp, _sig(f7.a_map.iloc[0] + f7.b_map.iloc[0] * _lg(pp)), "--", color=BLUE, lw=1, label="7-day shrunk fit")
        ax.plot([0, 1], [0, 1], "k-", lw=0.8)
        ax.set_xlabel("Market price (average in the bucket)")
        ax.set_ylabel("Share that won")
        ax.set_title("C. Reliability: price vs how often it happened (bars: 95% range, races resampled)", loc="left", fontweight="bold")
        ax.legend(fontsize=8, loc="upper left")
        ax = fig.add_axes([0.08, 0.17, 0.88, 0.14])
        keys = [k for k in ap if k.startswith("today") or k.startswith("punt")]
        width = 0.8 / max(len(keys), 1)
        for i, k in enumerate(keys):
            sb = ap[k]["shift_by_band"]
            bands = sorted(sb, key=float)
            ax.bar(np.arange(len(bands)) + i * width, [sb[b] for b in bands], width=width, label=k)
        if keys:
            bl = sorted(ap[keys[0]]["shift_by_band"], key=float)
            edges = [0, 0.05, 0.15, 0.3, 0.5, 0.7, 0.85, 0.95, 1]
            ax.set_xticks(np.arange(len(bl)) + 0.4 - width / 2)
            ax.set_xticklabels([f"{float(b):.2f}-{edges[edges.index(float(b)) + 1]:.2f}" for b in bl])
        ax.axhline(0, color="k", lw=0.8)
        ax.set_xlabel("Today's outside fair of the leg")
        ax.set_ylabel("Fair moves by (ticks)")
        ax.set_title("D. What recalibration would do to today's fairs (latest bot poll)", loc="left", fontweight="bold")
        ax.legend(fontsize=7, loc="upper left")
        _glossary(fig, (
            "outside price: Polymarket (preferred) or Kalshi price of a party's YES contract, d days before election day "
            "(12:00 UTC); eve = 00:00 UTC on election day; day = 12:00 UTC on election day.\n"
            "calibration slope b: from p' = 1/(1+exp(-(a + b*logit p))). b = 1: prices are right; b > 1: a 90% favourite really "
            "wins more than 90% of the time.\n"
            "shrunk fit: the same fit with a prior pulling a toward 0 and b toward 1 (SD 0.25), so a few lucky cycles cannot "
            "push it far.\n"
            "held-out cycle (dotted in A, lines in B): the map is fitted on the other election cycles and scored on this one; "
            "above 0 in B = the recalibrated price forecast that cycle better.\n"
            "log-loss: -log(probability given to what happened); x 1000 per race. Bars and bands: 95% range, whole races "
            "resampled. tick = 0.005."), y=0.01, h=0.13)
        fig.savefig(FIG / "ev_hunt_calibration.png", dpi=100)
        plt.close(fig)
    print("figs written")


if __name__ == "__main__":
    {"part1": part1, "part2": part2, "part2_real": part2_real, "part3": part3, "part3_apply": part3_apply, "figs": figs}[sys.argv[1]]()
