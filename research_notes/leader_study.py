"""How did the leaders make +850k-1M?  SIG prices vs the outside fair, 1 Oct onwards (24 Oct 2026).

    <venv with numpy+matplotlib>/bin/python research_notes/leader_study.py analyse   # ~20 s, no venue calls
    <venv with numpy+matplotlib>/bin/python research_notes/leader_study.py plot

Inputs (all cached): data/candles_1d.json (237 legs, daily, 1-21 Oct), data/opening_5m_oct1-5.json (10 big legs, 5-min),
data/candles_5m.json (5-min; 75 legs reach back to 1 Oct), data/research_sig_convergence/raw/<ex>.json (same-party
Polymarket ~5-min and Kalshi hourly history from 1 Oct per leg), data/research_leader/lb_periods.json (leaderboard
all / 7d / 1d, 24 Oct), data/leaderboard_all_20261022.json (full board), the `tape` and `venue_fair` tables.
Output: data/research_leader/results.json, research_notes/figures/leader_study.png.
"""
import bisect, collections, datetime as dt, json, os, sqlite3, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = os.path.join(ROOT, "data")
OUT = os.path.join(D, "research_leader")
T0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def day(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%m-%d")


def load_fair():
    """ex -> (times, prices): Polymarket where it has a print in the last 2 h, else Kalshi."""
    out, names = {}, {}
    rd = os.path.join(D, "research_sig_convergence", "raw")
    for f in os.listdir(rd):
        d = json.load(open(os.path.join(rd, f)))
        pm = sorted(d.get("pm") or [])
        k = sorted(d.get("k") or [])
        if len(pm) + len(k) < 20:
            continue
        out[str(d["ex"])] = (pm, k)
        names[str(d["ex"])] = f'{d["leg"]["race"]} {d["leg"]["party"]}'
    return out, names


def fair_at(series, t):
    pm, k = series
    for s, stale in ((pm, 7200), (k, 4 * 3600)):
        if not s:
            continue
        i = _bis(s, t)
        if i >= 0 and t - s[i][0] <= stale:
            return s[i][1]
    return None


_cache = {}


def _bis(s, t):
    key = id(s)
    if key not in _cache:
        _cache[key] = [x[0] for x in s]
    return bisect.bisect_right(_cache[key], t) - 1


def load_bars():
    """ex -> {t: bar} 5-min bars, opening file first, candles_5m fills the rest."""
    bars = collections.defaultdict(dict)
    op = json.load(open(os.path.join(D, "opening_5m_oct1-5.json")))
    for ex, v in op.items():
        for c in v["candles"]:
            bars[ex][ts(c["time"])] = c
    c5 = json.load(open(os.path.join(D, "candles_5m.json")))
    for ex, v in c5.items():
        for c in v["candles"]:
            bars[ex].setdefault(ts(c["time"]), c)
    return bars


def analyse():
    fair, names = load_fair()
    bars = load_bars()
    c1 = json.load(open(os.path.join(D, "candles_1d.json")))
    mk = {m["id"]: m for m in json.load(open(os.path.join(D, "markets.json")))}
    exname = {ex: names.get(ex) or mk.get(v["marketId"], {}).get("title", ex) for ex, v in c1.items()}
    res = {}

    # 1. Daily, every leg with a fair: notional, |VWAP - fair| x volume (lower bound on the edge traded), median gap.
    daily = collections.defaultdict(lambda: {"notional": 0.0, "edge_vol_day": 0.0, "gaps": [], "edge_vol_bar": 0.0,
                                             "bar_notional": 0.0, "transient": 0.0, "ref_notional": 0.0})
    for ex, v in c1.items():
        for c in v["candles"]:
            t = ts(c["time"])
            d = daily[day(t)]
            d["notional"] += c["volume"] * c["vwap"]
            if ex not in fair:
                continue
            fs = [fair_at(fair[ex], t + h * 3600) for h in range(0, 24, 2)]
            fs = [f for f in fs if f is not None]
            if not fs:
                continue
            f = sum(fs) / len(fs)
            d["edge_vol_day"] += c["volume"] * abs(c["vwap"] - f)
            d["gaps"].append(abs(c["close"] - f))

    # 2. 5-min bars vs fair at the bar: the edge traded and the mispricing episodes.
    #    ref = SIG's own 5-min close about a day later (first bar at or after t + 24 h, within t + 12-36 h): the price the
    #    leaderboard would mark the position at, and roughly where it could be closed.
    rows = []  # (ex, t, vwap, fair, vol, high, low, close, ref)
    for ex, bs in bars.items():
        if ex not in fair:
            continue
        tt = sorted(bs)
        for t in tt:
            c = bs[t]
            f = fair_at(fair[ex], t)
            if f is None:
                continue
            j = bisect.bisect_left(tt, t + 86400)
            ref = None
            if j < len(tt) and tt[j] <= t + 36 * 3600:
                ref = bs[tt[j]]["close"]
            elif j > 0 and tt[j - 1] >= t + 12 * 3600:
                ref = bs[tt[j - 1]]["close"]
            rows.append((ex, t, c["vwap"], f, c["volume"], c["high"], c["low"], c["close"], ref))
            d = daily[day(t)]
            d["edge_vol_bar"] += c["volume"] * abs(c["vwap"] - f)
            d["bar_notional"] += c["volume"] * c["vwap"]
            if ref is not None:
                sg = 1 if c["vwap"] > f else -1
                d["transient"] += c["volume"] * max(0.0, (c["vwap"] - ref) * sg)
                d["ref_notional"] += c["volume"] * c["vwap"]
    res["daily"] = {k: {"notional_M": round(v["notional"] / 1e6, 2),
                        "edge_traded_day_vwap_M": round(v["edge_vol_day"] / 1e6, 2),
                        "edge_traded_5min_M": round(v["edge_vol_bar"] / 1e6, 2),
                        "notional_with_5min_M": round(v["bar_notional"] / 1e6, 2),
                        "toward_fair_marked_next_day_M": round(v["transient"] / 1e6, 2),
                        "notional_with_next_day_mark_M": round(v["ref_notional"] / 1e6, 2),
                        "median_close_gap_c": round(100 * sorted(v["gaps"])[len(v["gaps"]) // 2], 1) if v["gaps"] else None,
                        "legs_gap_ge_10c": sum(g >= 0.10 for g in v["gaps"])}
                    for k, v in sorted(daily.items())}

    # Episodes: consecutive bars (<= 30 min apart) with |vwap - fair| >= 10c on the same side.
    rows.sort()
    eps, cur = [], None
    for r in rows:
        ex, t, vw, f, vol, hi, lo, cl, ref = r
        g = vw - f
        if abs(g) < 0.10:
            continue
        sgn = 1 if g > 0 else -1
        if cur and cur["ex"] == ex and cur["sgn"] == sgn and t - cur["t1"] <= 1800:
            cur["t1"] = t
            cur["shares"] += vol
            cur["edge"] += vol * abs(g)
            cur["bars"] += 1
            cur["max_gap"] = max(cur["max_gap"], abs(g))
            cur["notional"] += vol * vw
        else:
            if cur:
                eps.append(cur)
            cur = {"ex": ex, "sgn": sgn, "t0": t, "t1": t, "shares": vol, "edge": vol * abs(g), "bars": 1,
                   "max_gap": abs(g), "fair0": f, "notional": vol * vw}
    if cur:
        eps.append(cur)
    for e in eps:
        e["minutes"] = (e["t1"] - e["t0"]) / 60 + 5
        e["vwap"] = e["notional"] / e["shares"] if e["shares"] else None
    eps.sort(key=lambda e: -e["edge"])
    res["episodes_total"] = len(eps)
    by_day = collections.Counter(day(e["t0"]) for e in eps)
    edge_by_day = collections.defaultdict(float)
    for e in eps:
        edge_by_day[day(e["t0"])] += e["edge"]
    res["episodes_by_day"] = {k: {"n": by_day[k], "edge_k": round(edge_by_day[k] / 1e3)} for k in sorted(by_day)}
    res["top_episodes"] = [{
        "leg": exname.get(e["ex"], e["ex"]), "start_utc": dt.datetime.fromtimestamp(e["t0"], dt.timezone.utc).strftime("%m-%d %H:%M"),
        "minutes": round(e["minutes"]), "side": "SIG above fair (sell)" if e["sgn"] > 0 else "SIG below fair (buy)",
        "fair": round(e["fair0"], 3), "vwap": round(e["vwap"], 3), "max_gap_c": round(100 * e["max_gap"]),
        "shares_k": round(e["shares"] / 1e3), "edge_traded_k": round(e["edge"] / 1e3)} for e in eps[:25]]

    # Duration: per leg on 1 Oct, hours from first print until the 5-min close stays within 5c of fair for 2 h.
    settle = []
    for ex, bs in bars.items():
        if ex not in fair:
            continue
        tt = sorted(t for t in bs if t < T0 + 4 * 86400)
        if not tt or tt[0] > T0 + 86400:
            continue
        first = tt[0]
        ok_since = None
        done = None
        for t in tt:
            f = fair_at(fair[ex], t)
            if f is None:
                continue
            if abs(bs[t]["close"] - f) <= 0.05:
                ok_since = ok_since or t
                if t - ok_since >= 7200:
                    done = ok_since
                    break
            else:
                ok_since = None
        g0 = fair_at(fair[ex], first)
        settle.append({"leg": exname.get(ex, ex), "first_print_utc": dt.datetime.fromtimestamp(first, dt.timezone.utc).strftime("%m-%d %H:%M"),
                       "first_close": bs[first]["close"], "fair": g0,
                       "hours_to_fair": round((done - first) / 3600, 1) if done else None})
    res["time_to_fair_n"] = len(settle)
    hs = sorted(s["hours_to_fair"] for s in settle if s["hours_to_fair"] is not None)
    res["time_to_fair_hours"] = {"n_settled": len(hs), "not_within_4d": len(settle) - len(hs),
                                 "median": hs[len(hs) // 2] if hs else None, "p75": hs[3 * len(hs) // 4] if hs else None}
    gaps0 = sorted(abs(s["first_close"] - s["fair"]) for s in settle if s["fair"] is not None)
    res["first_print_gap_c"] = {"n": len(gaps0), "median": round(100 * gaps0[len(gaps0) // 2], 1),
                                "share_within_5c": round(sum(g <= 0.05 for g in gaps0) / len(gaps0), 2),
                                "share_ge_15c": round(sum(g >= 0.15 for g in gaps0) / len(gaps0), 2)}
    res["first_prints"] = sorted(settle, key=lambda s: -abs(s["first_close"] - (s["fair"] or s["first_close"])))[:12]

    # 3. A few-trade account: 100k cash, at most K trades, one per 6-hour block, each taking up to `share` of one 5-min
    #    bar's volume at its VWAP on the side toward fair. Collateral = price paid on the side bought (YES p; selling YES
    #    = buying NO at 1 - p). Gain marked at SIG's own price a day later (what the leaderboard shows), and, for
    #    comparison, at the outside fair (what settlement pays if the outside venues are right). Gains are recycled.
    cand = []
    for ex, t, vw, f, vol, hi, lo, cl, ref in rows:
        if ref is None or abs(vw - f) < 0.05:
            continue
        sg = 1 if vw > f else -1
        coll = max((1 - vw) if sg > 0 else vw, 0.005)
        cand.append((t, ex, coll, vol, (vw - ref) * sg, abs(vw - f)))
    def run(a, b, share, K, mark):
        cash = 100000.0
        blocks = collections.defaultdict(list)
        for c in cand:
            if a <= c[0] < b:
                blocks[int((c[0] - a) // 21600)].append(c)
        used, best = 0, []
        for bk in sorted(blocks):
            if used >= K:
                break
            t, ex, coll, vol, g_ref, g_fair = max(blocks[bk], key=lambda c: min(c[3] * share, cash / c[2]) * c[4 if mark == "ref" else 5])
            g = g_ref if mark == "ref" else g_fair
            q = min(vol * share, cash / coll)
            if q * g <= 0:
                continue
            cash += q * g
            used += 1
            best.append((exname.get(ex, ex), dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%m-%d %H:%M"), round(q), round(q * g)))
        return round(cash - 100000), best[:5]
    sims = {}
    periods = {"1-4 Oct": (T0, T0 + 4 * 86400), "6-9 Oct": (T0 + 5 * 86400, T0 + 9 * 86400),
               "14-17 Oct": (T0 + 13 * 86400, T0 + 17 * 86400), "18-21 Oct": (T0 + 17 * 86400, T0 + 20.5 * 86400)}
    for period, (a, b) in periods.items():
        for share in (0.1, 0.25):
            for K in (3, 10):
                for mark in ("ref", "fair"):
                    sims[f"{period} | {int(share*100)}% of a bar | {K} trades | marked at {'SIG next day' if mark == 'ref' else 'outside fair'}"] = run(a, b, share, K, mark)[0]
    res["few_trade_account_pnl"] = sims
    res["few_trade_best_trades_1_4oct"] = run(T0, T0 + 4 * 86400, 0.25, 10, "ref")[1]
    # One trade at the peak: the 1 Oct 16:05-16:50 RI Senate R episode, full 100k into NO.
    res["one_trade_peak_example"] = "see top_episodes[RI Senate R 10-01 16:05]: VWAP 0.621 vs fair 0.009; NO costs 0.379"

    # 4. Leaders: P&L by period.
    lb = json.load(open(os.path.join(OUT, "lb_periods.json")))
    wk = {r["profileId"]: r for r in lb["7d"]["rows"]}
    dy = {r["profileId"]: r for r in lb["1d"]["rows"]}
    old = {r["profileId"]: r for r in json.load(open(os.path.join(D, "leaderboard_snapshot.json")))}
    lead = []
    for r in lb["all"]["rows"][:50]:
        w = wk.get(r["profileId"])
        o = old.get(r["profileId"])
        lead.append({"rank": r["rank"], "user": r["username"], "pnl_k": round(r["pnl"] / 1e3, 1), "trades": r["tradesCount"],
                     "volume_M": round(r["volume"] / 1e6, 2), "roi_pct": r["roi"],
                     "pnl_since_18oct_k": round(w["pnl"] / 1e3, 1) if w else f"< {round(lb['7d']['rows'][-1]['pnl']/1e3,1)}",
                     "volume_since_18oct_M": round(w["volume"] / 1e6, 2) if w else None,
                     "pnl_22oct_0843_k": round(o["pnl"] / 1e3, 1) if o else None})
    res["leaders"] = lead
    full = json.load(open(os.path.join(D, "leaderboard_all_20261022.json")))["rows"]
    bins = [(0, 10), (11, 100), (101, 1000), (1001, 10**9)]
    res["field_by_trades_22oct"] = {f"{a}-{b if b < 10**9 else 'max'} trades": {
        "accounts": sum(a <= r["tradesCount"] <= b for r in full),
        "gains_M": round(sum(max(r["pnl"], 0) for r in full if a <= r["tradesCount"] <= b) / 1e6, 2),
        "losses_M": round(sum(min(r["pnl"], 0) for r in full if a <= r["tradesCount"] <= b) / 1e6, 2)} for a, b in bins}
    big_few = [r for r in full if r["pnl"] >= 100000 and r["tradesCount"] <= 1000]
    res["winners_100k_with_le_1000_trades"] = [{"user": r["username"], "pnl_k": round(r["pnl"] / 1e3), "trades": r["tradesCount"],
                                                "volume_M": round(r["volume"] / 1e6, 2), "roi": r["roi"]} for r in big_few]
    losers = [r for r in full if r["pnl"] <= -50000 and r["tradesCount"] <= 10]
    res["losers_50k_with_le_10_trades"] = {"n": len(losers), "sum_k": round(sum(r["pnl"] for r in losers) / 1e3),
                                           "median_loss_per_volume": sorted(-r["pnl"] / r["volume"] for r in losers if r["volume"])[len(losers) // 2] if losers else None}

    # 5. Alternative explanations from the daily candles: directional drift and longshots that rose.
    first_v, now_c = {}, {}
    for ex, v in c1.items():
        cs = v["candles"]
        if cs and cs[0]["time"].startswith("2026-10-01"):
            first_v[ex] = (cs[0]["vwap"], cs[0]["volume"])
            now_c[ex] = cs[-1]["close"]
    risers = sorted(((now_c[ex] - first_v[ex][0], ex) for ex in first_v), reverse=True)
    res["biggest_risers_day1_vwap_to_21oct"] = [{"leg": exname.get(ex, ex), "day1_vwap": round(first_v[ex][0], 3),
                                                 "close_21oct": now_c[ex], "rise_c": round(100 * r)} for r, ex in risers[:10]]
    lo = [(ex, first_v[ex][0], now_c[ex]) for ex in first_v if first_v[ex][0] <= 0.2]
    res["longshots_day1_vwap_le_20c"] = {"n": len(lo), "rose_to_ge_2x": sum(n >= 2 * p for _, p, n in lo),
                                         "rose_to_ge_40c": sum(n >= 0.4 for _, p, n in lo)}
    # outside move vs SIG gap change, legs with a fair on 1 Oct and 21 Oct
    dec = []
    for ex in first_v:
        if ex not in fair:
            continue
        f1 = fair_at(fair[ex], T0 + 20 * 3600)
        f2 = fair_at(fair[ex], T0 + 20 * 86400 + 20 * 3600)
        if f1 is None or f2 is None:
            continue
        dec.append((ex, first_v[ex][0], now_c[ex], f1, f2, first_v[ex][1]))
    res["day1_to_21oct_decomposition"] = {
        "legs": len(dec),
        "median_abs_outside_move_c": round(100 * sorted(abs(d[4] - d[3]) for d in dec)[len(dec) // 2], 1),
        "median_abs_day1_gap_c": round(100 * sorted(abs(d[1] - d[3]) for d in dec)[len(dec) // 2], 1),
        "vol_weighted_abs_outside_move_c": round(100 * sum(abs(d[4] - d[3]) * d[5] for d in dec) / sum(d[5] for d in dec), 1),
        "vol_weighted_abs_day1_gap_c": round(100 * sum(abs(d[1] - d[3]) * d[5] for d in dec) / sum(d[5] for d in dec), 1),
        "legs_outside_moved_ge_10c": [exname.get(d[0], d[0]) + f" {d[3]:.2f}->{d[4]:.2f}" for d in dec if abs(d[4] - d[3]) >= 0.10]}

    # 5b. Day-level: trade toward fair at a day's VWAP, hold to the 21 Oct close (SIG mark). Per day, all legs with a fair.
    hold = {}
    for ex, v in c1.items():
        if ex not in fair or not v["candles"]:
            continue
        close21 = v["candles"][-1]["close"]
        for c in v["candles"][:-1]:
            t = ts(c["time"])
            f = fair_at(fair[ex], t + 12 * 3600)
            if f is None or abs(c["vwap"] - f) < 0.02:
                continue
            sg = 1 if c["vwap"] > f else -1
            h = hold.setdefault(day(t), [0.0, 0.0])
            h[0] += c["volume"] * (c["vwap"] - close21) * sg
            h[1] += c["volume"] * c["vwap"]
    res["toward_fair_held_to_21oct_marks"] = {k: {"gain_M": round(a / 1e6, 2), "pct_of_notional": round(100 * a / b, 1)}
                                             for k, (a, b) in sorted(hold.items())}

    # 5c. 1-minute opening path (2 reads, data/research_leader/opening_1m.json): RI Senate R and US House D.
    p1 = os.path.join(OUT, "opening_1m.json")
    if os.path.exists(p1):
        om = json.load(open(p1))
        r1 = om["1077"]
        hi = [c for c in r1 if c["vwap"] >= 0.5]
        sh = sum(c["volume"] for c in hi)
        vw = sum(c["volume"] * c["vwap"] for c in hi) / sh
        res["ri_senate_r_1oct_minutes_above_50c"] = {"minutes": len(hi), "shares": round(sh), "vwap": round(vw, 3),
                                                     "cash_to_sell_all_k": round(sh * (1 - vw) / 1e3),
                                                     "gain_marked_at_0.10_k": round(sh * (vw - 0.10) / 1e3),
                                                     "gain_at_settlement_if_R_loses_k": round(sh * vw / 1e3)}
        h1 = [c for c in om["840"] if c["vwap"] <= 0.80]
        sh = sum(c["volume"] for c in h1)
        vw = sum(c["volume"] * c["vwap"] for c in h1) / sh
        res["us_house_d_1_2oct_minutes_below_80c"] = {"minutes": len(h1), "shares": round(sh), "vwap": round(vw, 3),
                                                      "fair": 0.925, "gain_marked_at_0.885_k": round(sh * (0.885 - vw) / 1e3)}

    # 6. Now: latest SIG mid vs outside fair (venue_fair), and big prints since 18 Oct far from fair.
    con = sqlite3.connect(f"file:{os.path.join(D, 'sigbot.sqlite')}?mode=ro", uri=True)
    tmax = con.execute("select max(ts) from venue_fair").fetchone()[0]
    rows_now = con.execute("select race, party, fair, sig_bid, sig_ask, sig_mid from venue_fair where ts > ? and fair is not null "
                           "and sig_mid is not null", (tmax - 600,)).fetchall()
    latest = {}
    for race, party, f, b, a, m in rows_now:
        latest[(race, party)] = (f, b, a, m)
    g = sorted(((m - f), race, party, f, b, a) for (race, party), (f, b, a, m) in latest.items())
    res["now"] = {"as_of_utc": dt.datetime.fromtimestamp(tmax, dt.timezone.utc).strftime("%m-%d %H:%M"), "legs": len(latest),
                  "median_abs_mid_gap_c": round(100 * sorted(abs(x[0]) for x in g)[len(g) // 2], 1),
                  "legs_touch_ge_5c_through_fair": sum(1 for x in g if (x[4] is not None and x[4] - x[3] >= 0.05) or (x[5] is not None and x[3] - x[5] >= 0.05)),
                  "largest_gaps": [{"leg": f"{r} {p}", "fair": round(f, 3), "bid": b, "ask": a, "mid_minus_fair_c": round(100 * x)}
                                   for x, r, p, f, b, a in (g[:5] + g[-5:])]}
    # Big tape prints (>= 5,000 shares) far (>= 10c) from the outside fair since 18 Oct
    vf = collections.defaultdict(list)
    for t, ex, f in con.execute("select ts, exchange_id, fair from venue_fair where fair is not null and exchange_id is not null"):
        vf[ex].append((t, f))
    for ex in vf:
        vf[ex].sort()
    big = collections.Counter()
    nbig = 0
    for ex, p, q, at in con.execute("select exchange_id, price, quantity, executed_at from tape where executed_at >= '2026-10-21' and quantity >= 5000"):
        s = vf.get(ex)
        if not s:
            continue
        t = ts(at)
        i = bisect.bisect_right([x[0] for x in s], t) - 1
        if i < 0 or t - s[i][0] > 900:
            continue
        nbig += 1
        gap = abs(p - s[i][1])
        big["ge_10c" if gap >= 0.10 else "5_10c" if gap >= 0.05 else "lt_5c"] += 1
    res["now"]["big_prints_since_21oct_vs_fair"] = dict(big, n=nbig)
    con.close()
    json.dump(res, open(os.path.join(OUT, "results.json"), "w"), indent=1)
    return res


def plot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    res = json.load(open(os.path.join(OUT, "results.json")))
    days = sorted(res["daily"])
    x = [dt.datetime(2026, int(d[:2]), int(d[3:]), 12) for d in days]
    dd = [res["daily"][d] for d in days]
    fig, ax = plt.subplots(5, 1, figsize=(11, 15), sharex=True, gridspec_kw={"height_ratios": [0.8, 1.1, 1, 1, 1.1]})
    fig.suptitle("Leaders' gains came from minutes-long sweeps on 1-4 Oct, not from wrong listing prices; that size has not recurred",
                 fontsize=12, y=0.997)
    # A
    ax[0].bar(x, [d["notional_M"] for d in dd], color="#8aa6c1", width=0.8)
    ax[0].set_ylabel("SUSQies traded\nper day (millions)")
    ax[0].set_title("A. How much traded on SIG each day", loc="left", fontsize=10)
    # B
    rate = [100 * d["toward_fair_marked_next_day_M"] / d["notional_with_next_day_mark_M"] if d["notional_with_next_day_mark_M"] > 1 else None for d in dd]
    ax[1].bar(x, [r or 0 for r in rate], color="#c0504d", width=0.8)
    for xi, r, d in zip(x, rate, dd):
        if r is not None and r > 4:
            ax[1].text(xi, r + 0.4, f"{r:.0f}%", ha="center", fontsize=8)
    ax[1].set_ylabel("gain next day\n(% of SUSQies traded)")
    ax[1].set_title("B. Trading toward the outside fair: what it was worth a day later at SIG's own price", loc="left", fontsize=10)
    ax[1].text(x[7], 17, "1 Oct: RI Senate R (fair 1%) traded 664k shares at 83c in 8 minutes;\n"
               "US House D (fair 92.5%) 1.5M shares at 69c in 9 minutes", fontsize=8, va="top")
    # C
    ax[2].plot(x, [d["median_close_gap_c"] for d in dd], "o-", color="#4f81bd", label="typical leg: distance from fair at the daily close (cents)")
    ax2 = ax[2].twinx()
    ax2.bar(x, [d["legs_gap_ge_10c"] for d in dd], color="#f2b880", alpha=0.6, width=0.5, label="legs closing 10c+ from fair (right axis)")
    ax2.set_ylabel("legs", fontsize=8)
    ax[2].set_ylabel("cents")
    ax[2].set_ylim(0, 6)
    ax[2].set_title("C. Daily closing prices were near fair on day 1; the steady gap since is wider but does not close", loc="left", fontsize=10)
    h1, l1 = ax[2].get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax[2].legend(h1 + h2, l1 + l2, fontsize=8, loc="lower right")
    ax[2].set_zorder(ax2.get_zorder() + 1)
    ax[2].patch.set_visible(False)
    # D: few-trade account
    win = {"1-4 Oct": dt.datetime(2026, 10, 3), "6-9 Oct": dt.datetime(2026, 10, 8), "14-17 Oct": dt.datetime(2026, 10, 16),
           "18-21 Oct": dt.datetime(2026, 10, 20)}
    ft = res["few_trade_account_pnl"]
    for share, col in ((25, "#c0504d"), (10, "#e8a09e")):
        for mark, ls in (("SIG next day", "-"), ("outside fair", ":")):
            ys = [ft[f"{w} | {share}% of a bar | 10 trades | marked at {mark}"] / 1e3 for w in win]
            ax[3].plot(list(win.values()), ys, "o" + ls, color=col, ms=4,
                       label=f"takes {share}% of a 5-min bar, valued at {mark}")
    ax[3].set_ylabel("gain on 100k\n(thousand SUSQies)")
    ax[3].set_title("D. A 100k account making 10 trades (best 5-min bar per 6 h) in each 4-day window", loc="left", fontsize=10)
    ax[3].text(dt.datetime(2026, 10, 22, 12), 150, "after 4 Oct the 'outside fair' value is the steady\nlongshot/favourite gap: "
               "it pays only at settlement,\nnot in cash before the 3 Nov punt", fontsize=7.5, va="top")
    ax[3].legend(fontsize=7.5, loc="upper right")
    # E: leaders
    for r in res["leaders"][:6]:
        px, py = [dt.datetime(2026, 10, 1)], [0]
        w = r["pnl_since_18oct_k"]
        if isinstance(w, (int, float)):
            px.append(dt.datetime(2026, 10, 18))
            py.append(r["pnl_k"] - w)
        if r["pnl_22oct_0843_k"] is not None:
            px.append(dt.datetime(2026, 10, 22, 8))
            py.append(r["pnl_22oct_0843_k"])
        px.append(dt.datetime(2026, 10, 24, 13))
        py.append(r["pnl_k"])
        ax[4].plot(px, py, "o-", ms=3, lw=1.2, label=f'#{r["rank"]}: {r["trades"]:,} trades')
    ax[4].axvspan(dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 5), color="#c0504d", alpha=0.08)
    ax[4].text(dt.datetime(2026, 10, 5, 6), 700, "no leaderboard reading between 1 and 18 Oct;\nthe line is drawn straight", fontsize=7.5, color="#555")
    ax[4].set_ylabel("gain since start\n(thousand SUSQies)")
    ax[4].set_title("E. Leaders: 93-99.5% of their gain was in place by 18 Oct; this week they add 0-60k each", loc="left", fontsize=10)
    ax[4].legend(fontsize=7.5, ncol=3, loc="lower right")
    for a_ in ax:
        a_.axvline(dt.datetime(2026, 11, 3, 18), color="k", lw=0.7)
    ax[4].text(dt.datetime(2026, 11, 3, 10), 450, "3 Nov\npunt", fontsize=8, ha="right")
    ax[4].xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax[4].set_xlim(dt.datetime(2026, 9, 30, 12), dt.datetime(2026, 11, 4, 12))
    ax[4].set_xlabel("date (UTC)")
    gl = ("Glossary. Leg: one YES market, e.g. 'Rhode Island Senate R'. Fair: the same leg's Polymarket price (Kalshi when "
          "Polymarket is stale). 5-min bar: all SIG trades in one leg in 5 minutes,\nat their average price. Panel B: for every bar "
          "away from fair, shares x how far SIG's price a day later had moved back toward fair, as a share of all SUSQies traded in "
          "those bars\n(5-min data covers 33-100% of each day's trading). Panel D: cash 100k, one trade per 6 hours on the best bar, "
          "gains re-used; 'valued at SIG next day' is what the leaderboard would show,\n'valued at outside fair' is what "
          "settlement pays if Polymarket is right. Bar averages hide the 0.5c-99.5c prints inside the minute, D picks bars with hindsight (an upper bound for that rule).")
    fig.text(0.01, 0.003, gl, fontsize=7.2, va="bottom", ha="left", bbox=dict(fc="#f6f6f6", ec="#ccc"))
    plt.tight_layout(rect=(0, 0.055, 1, 0.99))
    os.makedirs(os.path.join(ROOT, "research_notes", "figures"), exist_ok=True)
    fig.savefig(os.path.join(ROOT, "research_notes", "figures", "leader_study.png"), dpi=105)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "analyse"
    if cmd == "analyse":
        r = analyse()
        print(json.dumps({k: v for k, v in r.items() if k not in ("leaders",)}, indent=1)[:20000])
    elif cmd == "plot":
        plot()
