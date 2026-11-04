"""High-variance plan study (25 Oct 2026). Local data only, read-only DB.

    <venv with numpy+pandas+matplotlib>/bin/python research_notes/high_variance_study.py      (about 1 min)

Part 1: realised vol per SIG leg (1/6/24 h), outside vol, touch depth, big-swing ranking.
Part 2: direction signals (gap, outside/SIG momentum, favourite/calibration, order flow) in the high-vol subset,
        sign chosen on training days (22-23 Oct), scored on test days (24-25 Oct), t clustered by race.
Part 3: Monte Carlo of the plan vs keeping the book, to 3 Nov 18:00 ET.  Writes figures/high_variance_decision.png.
"""
import json, math, os, sqlite3, sys
import numpy as np, pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(HERE, "data", "sigbot.sqlite")
OUT = os.path.join(HERE, "research_notes", "high_variance_out.json")
TICK = 0.005
SPLIT = pd.Timestamp("2026-10-24 00:00", tz="UTC")
NOW = pd.Timestamp("2026-10-25 22:30", tz="UTC")
END = pd.Timestamp("2026-11-03 23:00", tz="UTC")          # 18:00 ET (EST after 1 Nov)
T_DAYS = (END - NOW).total_seconds() / 86400


def load():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = pd.read_sql("select ts, venue, race, party, instrument, bid, ask, mid, bid_sz, ask_sz from venue_quotes "
                    "where ts > strftime('%s','2026-10-21 23:00')", con)
    tape = pd.read_sql("select exchange_id, price, quantity, executed_at, aggressor from tape "
                       "where executed_at >= '2026-10-21' and aggressor is not null", con)
    con.close()
    q["t"] = pd.to_datetime(q.ts, unit="s", utc=True).dt.floor("5min")
    return q, tape


def grids(q):
    sig = q[q.venue == "sig"].copy()
    sig = sig[(sig.bid > 0) & (sig.ask > sig.bid)]
    sig["mid"] = (sig.bid + sig.ask) / 2
    sig["spread"] = sig.ask - sig.bid
    sig["touch"] = np.minimum(sig.bid_sz, sig.ask_sz)
    legs = sig.groupby("instrument")[["race", "party"]].first()
    idx = pd.date_range(sig.t.min(), sig.t.max(), freq="5min")
    piv = lambda df, col: df.pivot_table(index="t", columns="instrument", values=col, aggfunc="last").reindex(idx).ffill(limit=3)
    smid, sspr, stouch = piv(sig, "mid"), piv(sig, "spread"), piv(sig, "touch")
    out = q[(q.venue != "sig") & q.mid.between(0.003, 0.997)]
    out = out.groupby(["t", "race", "party"]).mid.mean().reset_index()
    omid = {}
    for ex, (race, party) in legs.iterrows():
        o = out[(out.race == race) & (out.party == party)].set_index("t").mid
        if len(o) > 50:
            omid[ex] = o.reindex(idx).ffill(limit=6)
    omid = pd.DataFrame(omid, index=idx)
    return legs, smid, sspr, stouch, omid


def rv(m, h_steps):
    d = m.diff(h_steps)
    return np.sqrt((d ** 2).mean())


def part1(legs, smid, sspr, stouch, omid):
    books = json.load(open(os.path.join(HERE, "data", "books_latest.json")))
    rows = []
    for ex in smid.columns:
        m = smid[ex].dropna()
        if len(m) < 300:
            continue
        p = m.iloc[-1]
        r1, r6, r24 = rv(smid[ex], 12), rv(smid[ex], 72), rv(smid[ex], 288)
        o24 = rv(omid[ex], 288) if ex in omid else np.nan
        b = books.get(ex, {})
        bb = b.get("bids", []); aa = b.get("asks", [])
        best_b = bb[0]["price"] if bb else np.nan; best_a = aa[0]["price"] if aa else np.nan
        d3_ask = sum(l["quantity"] for l in aa if l["price"] <= best_a + 3 * TICK + 1e-9) if aa else 0
        d3_bid = sum(l["quantity"] for l in bb if l["price"] >= best_b - 3 * TICK - 1e-9) if bb else 0
        spr = sspr[ex].median()
        # daily sigma from the larger of the 24 h and scaled 6 h measures (24 h has only ~4 independent days)
        sd_day = np.nanmax([r24, r6 * 2])
        sd_T = min(sd_day * math.sqrt(T_DAYS), math.sqrt(p * (1 - p)))
        e_abs = sd_T * math.sqrt(2 / math.pi)
        gap_now = float(omid[ex].dropna().iloc[-1] - p) if ex in omid and omid[ex].notna().any() else 0.0
        rows.append(dict(ex=ex, gap_now=gap_now, race=legs.loc[ex, "race"], party=legs.loc[ex, "party"], mid=p,
                         rv1=r1, rv6=r6, rv24=r24, out24=o24, spread=spr, touch=stouch[ex].median() if ex in stouch else np.nan,
                         depth3_ask=d3_ask, depth3_bid=d3_bid, sd_T=sd_T, e_abs=e_abs,
                         net_swing=e_abs - spr, swing_per_capital=(e_abs - spr) / max(p, 0.01)))
    df = pd.DataFrame(rows)
    # big-swing score: expected net move (points) x shares fillable within 3 ticks, capped at 70k capital
    df["cap_shares"] = np.minimum(df.depth3_ask, 70000 / df.mid.clip(lower=0.01))
    df["swing_usd"] = df.net_swing * df.cap_shares
    df = df.sort_values("swing_usd", ascending=False).reset_index(drop=True)
    return df


def clustered_t(x, g):
    s = pd.DataFrame({"x": x, "g": g}).groupby("g").x.agg(["sum", "count"])
    n = s["count"].sum(); mu = s["sum"].sum() / n
    if len(s) < 3:
        return mu, np.nan, n, len(s)
    resid = pd.DataFrame({"x": x - mu, "g": g}).groupby("g").x.sum()
    se = math.sqrt((resid ** 2).sum()) / n * math.sqrt(len(s) / (len(s) - 1))
    return mu, mu / se if se > 0 else np.nan, n, len(s)


def part2(legs, smid, sspr, omid, tape, hv):
    ofi = tape.copy()
    ofi["t"] = pd.to_datetime(ofi.executed_at, utc=True, format="mixed").dt.floor("5min")
    ofi["s"] = np.where(ofi.aggressor == "buy", 1, -1) * ofi.quantity
    ofi = ofi.pivot_table(index="t", columns="exchange_id", values="s", aggfunc="sum").reindex(smid.index).fillna(0)
    ofi6 = ofi.rolling(72, min_periods=1).sum()
    lg = lambda p: np.log(p / (1 - p))
    sigs = {}
    for ex in hv:
        if ex not in smid:
            continue
        m = smid[ex]; o = omid[ex] if ex in omid else pd.Series(np.nan, index=smid.index)
        po = o.clip(0.01, 0.99)
        cal = 1 / (1 + np.exp(-1.8 * lg(po)))       # ev_hunt part 3 / favourite_drift slope (lower end)
        sigs[ex] = {
            "gap (outside - SIG)": o - m,
            "outside momentum 6 h": o - o.shift(72),
            "outside momentum 24 h": o - o.shift(288),
            "SIG momentum 6 h": m - m.shift(72),
            "SIG momentum 24 h": m - m.shift(288),
            "favourite (mid - 0.5)": m - 0.5,
            "calibrated outside - SIG": cal - m,
            "order flow 6 h": ofi6[ex] if ex in ofi6 else pd.Series(0.0, index=smid.index),
        }
    res = []
    for name in next(iter(sigs.values())):
        for h in (6, 12, 24, 48):
            hs = h * 12
            recs = []
            for ex, d in sigs.items():
                m = smid[ex]; fut = m.shift(-hs) - m
                s = d[name]; spr = sspr[ex]
                # one anchor every 6 h per leg (overlapping for 12-48 h; t is clustered by race, which absorbs it)
                anchors = m.index[::72]
                for t in anchors:
                    sv, fv, sp = s.get(t, np.nan), fut.get(t, np.nan), spr.get(t, np.nan)
                    if np.isnan(sv) or np.isnan(fv) or sv == 0 or np.isnan(sp):
                        continue
                    recs.append((t, legs.loc[ex, "race"], np.sign(sv), fv, sp))
            if not recs:
                continue
            r = pd.DataFrame(recs, columns=["t", "race", "sgn", "fut", "spr"])
            split = r.t.sort_values().iloc[len(r) // 2]           # median anchor: train = earlier half
            tr, te = r[r.t < split], r[r.t >= split]
            if len(tr) < 5 or len(te) < 5:
                res.append(dict(signal=name, h=h, n_train=len(tr), n_test=len(te)))
                continue
            direction = np.sign((tr.sgn * tr.fut).mean()) or 1.0
            gross_tr = direction * tr.sgn * tr.fut / TICK
            gross_te = direction * te.sgn * te.fut / TICK
            net_te = gross_te - te.spr / TICK
            mtr, ttr, ntr, _ = clustered_t(gross_tr.values, tr.race.values)
            mte, tte, nte, gte = clustered_t(gross_te.values, te.race.values)
            mnet, tnet, _, _ = clustered_t(net_te.values, te.race.values)
            res.append(dict(signal=name, h=h, rule="follow" if direction > 0 else "reverse",
                            train_ticks=mtr, train_t=ttr, n_train=ntr, test_ticks=mte, test_t=tte,
                            test_net_ticks=mnet, test_net_t=tnet, n_test=nte, races_test=gte))
    return pd.DataFrame(res)


# ---------------------------------------------------------------- part 3: EV of the plan, Monte Carlo
SETTLE_NOW = 103800.0           # settlement value of the book, main session 25 Oct
RHO = 0.43                       # national-swing share of race variance (sig-correlation-model: 37-49%)
CAP = 5000.0                     # portfolio_max_partisan_notional (hard limit)


def part3(v, d3, n_sims=20000, seed=7):
    rng = np.random.default_rng(seed)
    wd = json.load(open(os.path.join(HERE, "params", "wind_down_report.json")))
    books = json.load(open(os.path.join(HERE, "data", "books_latest.json")))
    vi = v.set_index("ex")
    sets = [r for r in wd["rows"] if r["kind"] in ("sell_set", "buy_set")]
    set_payout = sum(r["payout"] for r in sets); set_sale = sum(r["sale_value"] for r in sets)
    legs_sale = wd["totals"]["legs_sale"]; cash = wd["totals"]["cash_now"]
    # exempt legs (cheap favourites, HF arb): value at the bid, mark at mid, hold risk from their own vol
    ex_q = {}
    for r in wd["rows"]:
        if r["kind"] == "exempt":
            for ex, qty in r.get("legs", {}).items():
                ex_q[ex] = ex_q.get(ex, 0) + qty
    ex_mid_val = ex_bid_val = 0.0; ex_sd = []
    for ex, qty in ex_q.items():
        b = books.get(ex, {}); bb = b.get("bids", []); aa = b.get("asks", [])
        if not bb or not aa:
            continue
        mid = (bb[0]["price"] + aa[0]["price"]) / 2
        # qty < 0 = short this leg (long the complement): value (1-mid) per share, sold by buying at the ask
        if qty > 0:
            ex_mid_val += qty * mid; ex_bid_val += qty * bb[0]["price"]
        else:
            ex_mid_val += -qty * (1 - mid); ex_bid_val += -qty * (1 - aa[0]["price"])
        ex_sd.append((ex, qty, vi.sd_T.get(ex, 0.03), vi.party.get(ex, "D")))
    exempt_exit = ex_mid_val - ex_bid_val
    proceeds = cash + set_sale + legs_sale + ex_bid_val          # everything sold now
    exit_cost = (set_payout - set_sale) + exempt_exit + 0.005 * legs_sale / 0.5   # legs ~1 tick per share at ~0.5

    top = v.head(15).copy()
    gap_edge = d3[(d3.signal == "gap (outside - SIG)") & (d3.h == 48)].iloc[0]

    def sim_book(names, capital, sides, drift_ticks, cap_partisan):
        """Equal capital per leg, shares limited by 3-tick depth; returns value at 3 Nov 18:00 ET after a taker exit."""
        n = len(names); per = capital / n
        sh, sd, sgn, cost, notional = [], [], [], 0.0, []
        for (_, r), s in zip(names.iterrows(), sides):
            px = r.mid if s > 0 else 1 - r.mid                   # s>0 buy this leg, s<0 buy its complement
            q = min(per / max(px, 0.02), r.depth3_ask if s > 0 else r.depth3_bid)
            sh.append(q); sd.append(r.sd_T)
            sgn.append(s * (1 if r.party == "D" else -1 if r.party == "R" else 0))
            cost += q * r.spread                                 # half spread in + half spread out
            notional.append(q * px * sgn[-1])
        sh, sd, sgn, notional = map(np.array, (sh, sd, sgn, notional))
        scale = 1.0
        if cap_partisan and abs(notional.sum()) > CAP:
            # shrink the side that breaks the cap until |net| = CAP (gross on the other side is kept)
            heavy = np.sign(notional.sum()) == np.sign(notional)
            k = (CAP + -notional[~heavy].sum() * np.sign(notional.sum())) / abs(notional[heavy].sum())
            k = min(max(k, 0.0), 1.0); sh = np.where(heavy, sh * k, sh); scale = k
        deployed = float((sh * np.array([r.mid if s > 0 else 1 - r.mid for (_, r), s in zip(names.iterrows(), sides)])).sum())
        F = rng.standard_normal(n_sims)[:, None]; E = rng.standard_normal((n_sims, n))
        z = math.sqrt(RHO) * F * np.where(sgn != 0, np.sign(sgn), 0) + math.sqrt(1 - RHO) * E
        mv = sd * z + drift_ticks * TICK
        pnl = (sh * mv).sum(1) - cost
        return pnl, deployed, scale, float(np.abs(notional).sum()), float(notional.sum())

    # keep: settlement value; exempt legs marked with their own vol (zero drift, conservative)
    sdv = np.array([s for _, _, s, _ in ex_sd]); qv = np.array([abs(q) for _, q, _, _ in ex_sd])
    pv = np.array([(1 if p == "D" else -1) * np.sign(q) for _, q, _, p in ex_sd])
    F = rng.standard_normal(n_sims)[:, None]; E = rng.standard_normal((n_sims, len(sdv)))
    keep = SETTLE_NOW + ((math.sqrt(RHO) * F * pv + math.sqrt(1 - RHO) * E) * sdv * qv).sum(1)

    capital = proceeds
    base = SETTLE_NOW - exit_cost
    rand_sides = np.array([1 if r.mid < 0.5 else -1 for _, r in top.iterrows()])   # cheaper side: most swing per dollar
    gap_sides = []
    # sides from the current gap sign (outside above SIG -> buy the leg)
    for _, r in top.iterrows():
        gap_sides.append(1 if r.get("gap_now", 0) >= 0 else -1)
    plans = {}
    plans["Keep the book (no change)"] = keep
    for label, sides, drift, capf in [
        ("Sell all, 15 volatile legs, cheaper side, no signal", rand_sides, 0.0, False),
        ("Sell all, 15 volatile legs, gap signal (test edge)", np.array(gap_sides), gap_edge.test_ticks, False),
        ("Same, inside the 5,000 partisan cap", np.array(gap_sides), gap_edge.test_ticks, True),
        ("Sell all, top 3 legs only, cheaper side", rand_sides[:3], 0.0, False),
    ]:
        names = top if len(sides) == 15 else top.head(3)
        pnl, dep, scale, gross, net = sim_book(names, capital, sides, drift, capf)
        plans[label] = base + pnl
        plans[label + " _meta"] = dict(deployed=dep, scale=scale, gross_partisan=gross, net_partisan=net)
    summary = {}
    for k, x in plans.items():
        if k.endswith("_meta"):
            continue
        summary[k] = dict(ev=float(x.mean() - SETTLE_NOW), p_gain=float((x > SETTLE_NOW).mean()),
                          p5=float(np.percentile(x, 5)), p50=float(np.percentile(x, 50)), p95=float(np.percentile(x, 95)),
                          p_999=float(np.percentile(x, 99.9)), **plans.get(k + " _meta", {}))
    info = dict(cash=cash, set_payout=set_payout, set_sale=set_sale, legs_sale=legs_sale, exempt_mid=ex_mid_val,
                exempt_bid=ex_bid_val, exit_cost=exit_cost, proceeds=proceeds, gap_edge=gap_edge.to_dict())
    return summary, plans, info


def plot(v, d3, d15, summary, plans, info):
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig = plt.figure(figsize=(13, 24))
    gs = fig.add_gridspec(4, 1, height_ratios=[1, 1.05, 1.05, 0.42], hspace=0.62)
    fig.suptitle("Selling the book to swing for the fences in the most volatile markets", fontsize=17, fontweight="bold", y=0.93)
    fig.text(0.5, 0.912, "Volatility measured 22-25 Oct; signals chosen on the first half of the days, checked on the second; "
             "outcomes to 3 Nov 18:00 ET (EST)", ha="center", fontsize=11, color="#444")
    # A: volatility ranking
    ax = fig.add_subplot(gs[0]); t = v.head(15).iloc[::-1]
    lab = [f"{r.race} {r.party} ({r.mid:.2f})" for _, r in t.iterrows()]
    y = np.arange(len(t))
    ax.barh(y, t.e_abs * 100, color="#1f77b4", alpha=.85, label="Expected size of the move by 3 Nov 18:00 ET, either way (points)")
    ax.barh(y, t.spread * 100, color="#d62728", alpha=.9, label="Cost of getting in and out: the bid-ask spread (points)")
    for yy, (_, r) in zip(y, t.iterrows()):
        ax.text(r.e_abs * 100 + .15, yy, f"{r.swing_usd/1000:.1f}k expected swing, depth {r.depth3_ask/1000:.0f}k sh", va="center", fontsize=8.5, color="#333")
    ax.set_yticks(y); ax.set_yticklabels(lab, fontsize=9); ax.set_xlabel("points of probability (1 point = 0.01)")
    ax.set_xlim(0, max(t.e_abs * 100) * 1.45); ax.grid(axis="x", alpha=.3)
    ax.set_title("A. Which markets swing most? Top 15 by expected swing in money, after the spread", loc="left", fontweight="bold")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), frameon=False, fontsize=9.5)
    # B: direction signals, test net ticks per share vs horizon
    ax = fig.add_subplot(gs[1]); H = [6, 12, 24, 48]
    cols = {"gap (outside - SIG)": "#d62728", "outside momentum 24 h": "#1f77b4", "SIG momentum 6 h": "#2ca02c",
            "calibrated outside - SIG": "#9467bd", "order flow 6 h": "#8c564b"}
    for name, c in cols.items():
        r = d3[d3.signal == name].set_index("h").reindex(H)
        se = (r.test_net_ticks / r.test_net_t).abs()
        ax.plot(H, r.test_net_ticks, "-o", color=c, label=f"{name}: test days, after the spread")
        ax.plot(H, r.test_ticks, ":^", color=c, alpha=.7)
        if name.startswith("gap"):
            ax.fill_between(H, r.test_net_ticks - 1.96 * se, r.test_net_ticks + 1.96 * se, color=c, alpha=.15)
    ax.axhline(0, color="#555", lw=1); ax.set_xscale("log"); ax.minorticks_off(); ax.set_xticks(H); ax.set_xticklabels([f"{h} h" for h in H])
    ax.set_xlabel("How long the trade is held"); ax.set_ylabel("Profit per share, test days (ticks)"); ax.grid(alpha=.3)
    ax.set_title("B. Does anything call the direction in the volatile third of markets?", loc="left", fontweight="bold")
    h1, l1 = ax.get_legend_handles_labels()
    from matplotlib.lines import Line2D
    h1 += [Line2D([], [], color="#555", ls=":", marker="^"), Line2D([], [], color="#555", marker="o")]
    l1 += ["dotted: before paying the spread", "solid: after paying the spread (shaded: 95% range for the gap signal)"]
    ax.legend(h1, l1, loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False, fontsize=9, ncol=2)
    # C: outcome distributions
    ax = fig.add_subplot(gs[2]); keys = list(summary)
    colors = ["#555555", "#1f77b4", "#d62728", "#ff7f0e", "#9467bd"]
    for i, k in enumerate(keys):
        x = plans[k] / 1000; s_ = summary[k]
        parts = ax.violinplot([x], positions=[i], vert=False, showextrema=False, widths=.8)
        for b in parts["bodies"]:
            b.set_facecolor(colors[i]); b.set_alpha(.35)
        ax.plot([s_["p5"] / 1000, s_["p95"] / 1000], [i, i], color=colors[i], lw=3)
        ax.plot(s_["p50"] / 1000, i, "o", color=colors[i], ms=8)
        ax.text(s_["p95"] / 1000 + 1, i + .18, f"average {s_['ev']/1000:+.1f}k, gain in {s_['p_gain']*100:.0f}%, "
                f"5-95%: {s_['p5']/1000:.0f}k-{s_['p95']/1000:.0f}k", fontsize=9, color="#222")
    ax.axvline(SETTLE_NOW / 1000, color="#555", ls="--", lw=1)
    ax.axvline(900, color="#2ca02c", ls="--", lw=1)
    ax.set_yticks(range(len(keys))); ax.set_yticklabels(keys, fontsize=9.5)
    ax.set_xlabel("Account value at 3 Nov 18:00 ET (thousands; dashed grey = today 103.8k)"); ax.grid(axis="x", alpha=.3)
    xmax = max(summary[k]["p95"] for k in keys) / 1000 + 45
    ax.set_xlim(min(summary[k]["p5"] for k in keys) / 1000 - 10, xmax)
    ax.text(xmax - 1, len(keys) - .4, "top 3 needs ~900k: off the chart\n(about 8x to the right)", ha="right", fontsize=9, color="#2ca02c")
    ax.set_title("C. What selling the book and swinging does to the account", loc="left", fontweight="bold")
    # glossary
    ax = fig.add_subplot(gs[3]); ax.axis("off")
    g = ("Glossary\n"
         "point: 0.01 of probability; tick: 0.005, the smallest SIG price step; spread: ask minus bid, paid once to get in and out\n"
         "expected swing: the typical size of the move by 3 Nov if prices keep moving as they did 22-25 Oct (24 h volatility x sqrt(9 days))\n"
         "depth: shares on offer within 3 ticks of the best price now (books_latest)\n"
         "gap: Polymarket/Kalshi average minus the SIG mid; momentum: the change over the last 6 or 24 h; order flow: buys minus sells on the tape\n"
         "calibrated: the outside price stretched toward the favourite (the ev_hunt / favourite_drift fit)\n"
         "test days: the later half of the 22-25 Oct trades; the follow/fade rule was picked on the earlier half only\n"
         f"selling the book: sets sold at walked bids ({info['set_payout']-info['set_sale']:,.0f} below payout), cheap-favourite and HF-arb legs at the bid, "
         f"other legs about 1 tick;\n   total cost {info['exit_cost']:,.0f}; cash raised {info['proceeds']:,.0f}\n"
         "national swing: 43% of each race's moves shared across races (the correlation model); partisan cap: net D minus R exposure at most 5,000")
    ax.text(0.0, 1.0, g, va="top", fontsize=9.5, family="DejaVu Sans", bbox=dict(boxstyle="round", fc="#f4f4f0", ec="#999"))
    out = os.path.join(HERE, "research_notes", "figures", "high_variance_decision.png")
    fig.savefig(out, dpi=100, bbox_inches="tight"); print("wrote", out)


if __name__ == "__main__":
    q, tape = load()
    legs, smid, sspr, stouch, omid = grids(q)
    v = part1(legs, smid, sspr, stouch, omid)
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
    cols = ["race", "party", "mid", "rv1", "rv6", "rv24", "out24", "spread", "depth3_ask", "gap_now", "e_abs", "swing_per_capital", "swing_usd"]
    print(v[cols].head(15).round(4).to_string())
    print("legs:", len(v), "median rv1/6/24", v[["rv1", "rv6", "rv24"]].median().round(4).tolist(), "T days", round(T_DAYS, 2))
    top15 = list(v.ex.head(15)); topthird = list(v.sort_values("rv24", ascending=False).ex.head(len(v) // 3))
    d15 = part2(legs, smid, sspr, omid, tape, top15)
    d3 = part2(legs, smid, sspr, omid, tape, topthird)
    if "dir" in sys.argv:
        print(d15.round(2).to_string()); print(d3.round(2).to_string())
    summary, plans, info = part3(v, d3)
    print(json.dumps(info, default=float, indent=0)[:900])
    print(pd.DataFrame(summary).T.round(3).to_string())
    plot(v, d3, d15, summary, plans, info)
