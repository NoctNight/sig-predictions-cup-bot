"""HFT study on the realtime feed: set-arb episodes, DMM touch moves, Polymarket lead. Read-only, stdlib.

    python3 -m sigbot.proposals.hft_study arbs  <capture.jsonl> [...]   # set-arb episode lifetimes, sizes, who ends them
    python3 -m sigbot.proposals.hft_study moves <capture.jsonl> [...]   # DMM touch moves: bursts, what follows in 1-30 s
    python3 -m sigbot.proposals.hft_study lead  <recording.jsonl>        # Polymarket best bid/ask vs SIG mid, lag profile
    python3 -m sigbot.proposals.hft_study pairs                          # D+R mid sum vs 1-x from venue_fair (DB, read-only)

Captures: data/feed_capture_20261022.jsonl (no Polymarket) and sigbot/proposals/hft_data/rec_*.jsonl (hft_record.py).
Times are the feed's asOf (engine time) for books and executedAt for trades; rx (our receive time) is used for latency.
"""
from __future__ import annotations

import bisect, collections, json, math, os, sqlite3, statistics, sys
from datetime import datetime

from sigbot.universe import Universe

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
X_DEFAULT = {"senate": 0.012, "governor": 0.020, "house": 0.002, "chamber": 0.0}
TICK = 0.005


def iso(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:
        a, b = s.split(".", 1)
        frac, tz = b[:b.index("+")] if "+" in b else b, b[b.index("+"):] if "+" in b else ""
        s = f"{a}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).timestamp()


def universe():
    return Universe.from_markets(json.load(open(os.path.join(ROOT, "data", "markets.json"))))


def load(paths):
    """Yield (kind, rec) in file order, normalised: books get 'ts' (asOf), trades 'ts' (executedAt)."""
    for p in paths:
        for line in open(p):
            d = json.loads(line)
            t = d.get("t")
            if t == "book":
                d["ts"] = iso(d.get("as_of")) or d["rx"]
                yield "book", d
            elif t == "trade":
                d["ts"] = iso(d.get("at")) or d["rx"]
                yield "trade", d
            elif t in ("pm", "pmt", "map"):
                yield t, d


def pct(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


# ---------------------------------------------------------------- arbs
def arbs(paths, min_edge=0.005, min_ret=0.0):
    uni = universe()
    races = {n: r for n, r in uni.races.items() if len(r.legs) >= 2}
    leg_race = {l.exchange_id: n for n, r in races.items() for l in r.legs.values()}
    xs = {n: X_DEFAULT.get(r.office, 0.02) for n, r in races.items()}
    book, open_ep, eps = {}, {}, []
    trades_by_ex = collections.defaultdict(list)
    t_first = t_last = None
    for kind, d in load(paths):
        if kind == "trade":
            trades_by_ex[d["ex"]].append((d["ts"], d["price"], d["qty"]))
            continue
        if kind != "book":
            continue
        ex = d["ex"]
        book[ex] = d
        t_first, t_last = t_first or d["ts"], d["ts"]
        n = leg_race.get(ex)
        if not n:
            continue
        exs = races[n].exchanges() if hasattr(races[n], "exchanges") else [l.exchange_id for l in races[n].legs.values()]
        if any(e not in book or not book[e]["bids"] or not book[e]["asks"] for e in exs):
            continue
        for kindset in ("sell", "buy"):
            if kindset == "sell":
                edge = sum(book[e]["bids"][0][0] for e in exs) - 1
                cost = len(exs) - sum(book[e]["bids"][0][0] for e in exs)
                lv = {e: book[e]["bids"] for e in exs}
            else:
                edge = 1 - sum(book[e]["asks"][0][0] for e in exs) - xs[n]
                cost = sum(book[e]["asks"][0][0] for e in exs)
                lv = {e: book[e]["asks"] for e in exs}
            key = (n, kindset)
            if edge > min_edge + 1e-9 and edge / cost >= min_ret:
                # walk levels for total profitable sets
                sets, prof = walk(lv, exs, kindset, xs[n], min_edge, min_ret)
                ep = open_ep.get(key)
                if ep is None:
                    open_ep[key] = {"race": n, "kind": kindset, "t0": d["ts"], "rx0": d["rx"], "edge0": edge, "ret0": edge / cost,
                                    "standing": d["ts"] - t_first < 5,
                                    "qty0": min(lv[e][0][1] for e in exs), "sets0": sets, "prof0": prof, "max_edge": edge,
                                    "max_prof": prof, "exs": exs, "trigger": ex}
                else:
                    ep["max_edge"], ep["max_prof"] = max(ep["max_edge"], edge), max(ep["max_prof"], prof)
            elif key in open_ep:
                ep = open_ep.pop(key)
                ep["t1"], ep["closer"] = d["ts"], ex
                eps.append(ep)
    for ep in open_ep.values():
        ep["t1"], ep["closer"] = t_last, None
        eps.append(ep)
    # who closed it: a print on any leg inside [t0, t1 + 1.5 s]
    for ep in eps:
        prints = [(ts, e, p, q) for e in ep["exs"] for (ts, p, q) in trades_by_ex.get(e, []) if ep["t0"] - 0.5 <= ts <= ep["t1"] + 0.5]
        ep["prints"] = len(prints)
        ep["print_qty"] = sum(q for *_, q in prints)
        ep["dur"] = ep["t1"] - ep["t0"]
    # ours: arb_trades rows for the race inside [t0 - 2, t1 + 5]
    try:
        c = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
        ours = c.execute("select ts, race, kind, quantity, ok from arb_trades where ts between ? and ?",
                         ((t_first or 0) - 10, (t_last or 0) + 10)).fetchall()
    except sqlite3.Error:
        ours = []
    for ep in eps:
        mine = [o for o in ours if o[1] == ep["race"] and o[2].startswith(ep["kind"]) and ep["t0"] - 2 <= o[0] <= ep["t1"] + 5]
        ep["ours_ok"] = sum(o[3] for o in mine if o[4])
        ep["ours_fail"] = sum(1 for o in mine if not o[4])
    return eps, (t_last - t_first) / 3600 if t_first else 0


def walk(lv, exs, kindset, x, min_edge, min_ret=0.0):
    ptr = {e: 0 for e in exs}
    rem = {e: lv[e][0][1] for e in exs}
    sets = prof = 0.0
    while True:
        try:
            pr = {e: lv[e][ptr[e]][0] for e in exs}
        except IndexError:
            break
        edge = (sum(pr.values()) - 1) if kindset == "sell" else (1 - sum(pr.values()) - x)
        cost = (len(exs) - sum(pr.values())) if kindset == "sell" else sum(pr.values())
        if edge <= min_edge or edge / cost < min_ret:
            break
        q = min(rem[e] for e in exs)
        sets += q
        prof += q * edge
        for e in exs:
            rem[e] -= q
            if rem[e] <= 0:
                ptr[e] += 1
                if ptr[e] < len(lv[e]):
                    rem[e] = lv[e][ptr[e]][1]
    return sets, prof


def report_arbs(paths, min_ret=0.0):
    eps, hours = arbs(paths, min_ret=min_ret)
    print(f"min_ret {min_ret:.3f}; open at the first 5 s of the file (standing): {sum(x['standing'] for x in eps)}")
    eps = [x for x in eps if not x["standing"]]
    print(f"capture {hours:.2f} h, episodes {len(eps)}")
    for kind in ("sell", "buy"):
        e = [x for x in eps if x["kind"] == kind]
        if not e:
            continue
        durs = [x["dur"] for x in e]
        print(f"\n{kind}-set: {len(e)} episodes ({len(e)/hours:.1f}/h), races {len({x['race'] for x in e})}")
        print(f"  lifetime s: p10 {pct(durs,.1):.1f} p25 {pct(durs,.25):.1f} med {pct(durs,.5):.1f} p75 {pct(durs,.75):.1f} p90 {pct(durs,.9):.1f}")
        print(f"  edge at open: med {statistics.median(x['edge0'] for x in e):.4f}; max edge med {statistics.median(x['max_edge'] for x in e):.4f}")
        print(f"  sets at touch med {statistics.median(x['qty0'] for x in e):.0f}; walkable sets med {statistics.median(x['sets0'] for x in e):.0f}")
        print(f"  profit available at open: sum {sum(x['prof0'] for x in e):.1f} ({sum(x['prof0'] for x in e)/hours:.1f}/h), "
              f"med {statistics.median(x['prof0'] for x in e):.2f}; max over life sum {sum(x['max_prof'] for x in e):.1f}")
        for lo, hi in ((0, 1.01), (1.01, 2.01), (2.01, 5.01), (5.01, 30.01), (30.01, 1e9)):
            sub = [x for x in e if lo <= x["dur"] < hi]
            if sub:
                print(f"  life [{lo:.0f},{hi:.0f}) s: n {len(sub)}, with a print {sum(x['prints'] > 0 for x in sub)}, "
                      f"we took {sum(x['ours_ok'] > 0 for x in sub)} ({sum(x['ours_ok'] for x in sub):.0f} sets), we failed {sum(x['ours_fail'] for x in sub)}, "
                      f"profit at open {sum(x['prof0'] for x in sub):.1f}, sets {sum(x['sets0'] for x in sub):.0f}, ret med {statistics.median(x['ret0'] for x in sub):.3f}")
    return eps


# ---------------------------------------------------------------- moves
def touches(paths):
    """Per exchange: list of (ts, rx, bb, bq, ba, aq) for every book push; trades per exchange."""
    tb = collections.defaultdict(list)
    tr = collections.defaultdict(list)
    pm = collections.defaultdict(list)
    pmt = collections.defaultdict(list)
    legs = None
    for kind, d in load(paths):
        if kind == "book":
            if d["bids"] and d["asks"]:
                tb[d["ex"]].append((d["ts"], d["rx"], d["bids"][0][0], d["bids"][0][1], d["asks"][0][0], d["asks"][0][1], d))
        elif kind == "trade":
            tr[d["ex"]].append((d["ts"], d["price"], d["qty"], d["rx"]))
        elif kind == "pm":
            if d.get("bb") is not None and d.get("ba") is not None:
                pm[d["a"]].append((float(d["ts"]) / 1000 if d.get("ts") else d["rx"], d["rx"], d["bb"], d["ba"]))
        elif kind == "pmt":
            pmt[d["a"]].append((float(d["ts"]) / 1000 if d.get("ts") else d["rx"], d["p"], d["q"], d["side"]))
        elif kind == "map":
            legs = d["legs"]
    return tb, tr, pm, pmt, legs


def move_events(tb, tr):
    """Touch moves: a change of best bid or best ask price between consecutive pushes. Classified as 'sweep' when a
    print on that exchange landed between the two pushes at the old touch price, else 'requote'."""
    ev = []
    for ex, rows in tb.items():
        tts = [t[0] for t in tr.get(ex, [])]
        for a, b in zip(rows, rows[1:]):
            dbid, dask = round((b[2] - a[2]) / TICK), round((b[4] - a[4]) / TICK)
            if not dbid and not dask:
                continue
            i, j = bisect.bisect_left(tts, a[0] - 0.05), bisect.bisect_right(tts, b[0] + 0.05)
            printed = j > i
            midmove = ((b[2] + b[4]) - (a[2] + a[4])) / 2 / TICK
            ev.append({"ex": ex, "ts": b[0], "rx": b[1], "dbid": dbid, "dask": dask, "mid": midmove, "sweep": printed,
                       "prev": a, "new": b, "spread0": round((a[4] - a[2]) / TICK), "spread1": round((b[4] - b[2]) / TICK)})
    ev.sort(key=lambda e: e["ts"])
    return ev


def report_moves(paths):
    tb, tr, pm, pmt, legs = touches(paths)
    ev = move_events(tb, tr)
    t0 = min(r[0][0] for r in tb.values())
    t1 = max(r[-1][0] for r in tb.values())
    hours = (t1 - t0) / 3600
    req = [e for e in ev if not e["sweep"]]
    shifts = [e for e in req if e["dbid"] and e["dask"] and (e["dbid"] > 0) == (e["dask"] > 0)]
    print(f"{hours:.2f} h, {len(tb)} exchanges, touch changes {len(ev)} ({len(ev)/hours:.0f}/h); with a print {sum(e['sweep'] for e in ev)}; "
          f"no print {len(req)}; both sides shifted same way (DMM requote) {len(shifts)} ({len(shifts)/hours:.0f}/h) on {len({e['ex'] for e in shifts})} markets")
    print("  requote size (mid ticks):", collections.Counter(round(abs(e['mid'])) for e in shifts).most_common(6))
    # bursts: requote shifts per engine second across markets
    sec = collections.Counter(int(e["ts"]) for e in shifts)
    burst = [e for e in shifts if sec[int(e["ts"])] >= 3]
    print(f"  shifts in a second with >= 3 market shifts: {len(burst)} of {len(shifts)}; seconds with >=3: {sum(1 for v in sec.values() if v >= 3)}")
    # reversal / continuation and what trades print at the old price after the shift (stale orders hit?)
    cont = rev = flat = 0
    for e in shifts:
        rows = tb[e["ex"]]
        k = next((i for i, r in enumerate(rows) if r[0] >= e["ts"]), None)
        later = [r for r in rows[k:] if r[0] <= e["ts"] + 300]
        if not later:
            continue
        m_end = (later[-1][2] + later[-1][4]) / 2
        m_new = (e["new"][2] + e["new"][4]) / 2
        d = (m_end - m_new) / TICK * (1 if e["mid"] > 0 else -1)
        cont, rev, flat = cont + (d > 0.4), rev + (d < -0.4), flat + (abs(d) <= 0.4)
    print(f"  next 5 min after a requote shift: continue {cont}, reverse {rev}, flat {flat}")
    return tb, tr, ev, shifts, pm, legs


# ---------------------------------------------------------------- pairs
def pairs():
    c = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    uni = universe()
    two = {n: r for n, r in uni.races.items() if len(r.legs) == 2}
    rows = c.execute("select ts, race, party, sig_bid, sig_ask, fair from venue_fair where sig_bid is not null and sig_ask is not null").fetchall()
    snap = collections.defaultdict(dict)
    for ts, race, party, b, a, f in rows:
        snap[(race, round(ts))][party] = (b, a, f)
    out = collections.defaultdict(list)
    for (race, ts), legs in snap.items():
        if race in two and len(legs) == 2:
            (b1, a1, f1), (b2, a2, f2) = legs.values()
            x = X_DEFAULT.get(two[race].office, 0.02)
            out[race].append((ts, (b1 + a1) / 2 + (b2 + a2) / 2 - (1 - x), (a1 - b1) + (a2 - b2), b1 + b2 - 1, 1 - a1 - a2 - x))
    return out


def report_pairs():
    out = pairs()
    devs = [d for v in out.values() for (_, d, *_r) in v]
    print(f"2-leg races {len(out)}, snapshots {len(devs)}")
    print(f"  mid-sum deviation from 1-x (ticks): p5 {pct(devs,.05)/TICK:.1f} p25 {pct(devs,.25)/TICK:.1f} med {pct(devs,.5)/TICK:.1f} "
          f"p75 {pct(devs,.75)/TICK:.1f} p95 {pct(devs,.95)/TICK:.1f}")
    sell = sum(1 for v in out.values() for r in v if r[3] > 0.005)
    buy = sum(1 for v in out.values() for r in v if r[4] > 0.005)
    print(f"  snapshots with riskless sell-set edge > 0.5c: {sell}; buy-set (after x) > 0.5c: {buy}")
    # mean reversion of the deviation: does |dev| >= 2 ticks shrink in the next snapshots?
    for thr in (1.5, 2.5, 4):
        nxt = []
        for v in out.values():
            v.sort()
            for (t, d, sp, *_), (t2, d2, *_r) in zip(v, v[1:]):
                if abs(d) / TICK >= thr and 60 <= t2 - t <= 400:
                    nxt.append((abs(d2) - abs(d)) / TICK)
        if nxt:
            print(f"  |dev| >= {thr} ticks: n {len(nxt)}, next-snapshot change in |dev| med {statistics.median(nxt):+.2f} mean {statistics.mean(nxt):+.2f} ticks")


# ---------------------------------------------------------------- lead
def series(tb, pm, legs):
    """Per SIG exchange with a single mapped Polymarket token: SIG (ts, mid) and PM (ts, mid) lists."""
    out = {}
    for ex, info in (legs or {}).items():
        tok = info.get("pm_token")
        if not tok or ex not in tb or tok not in pm:
            continue
        s = [(r[0], (r[2] + r[4]) / 2, r[2], r[4]) for r in tb[ex]]
        p = sorted((t, (bb + ba) / 2, bb, ba) for (t, rx, bb, ba) in pm[tok] if ba - bb <= 0.1)
        if len(s) > 2 and len(p) > 2:
            out[ex] = (s, p)
    return out


def at(rows, t, k=1):
    i = bisect.bisect_right([r[0] for r in rows], t) - 1
    return rows[i][k] if i >= 0 else None


def report_lead(paths, pm_move=0.01, window=30.0):
    tb, tr, pm, pmt, legs = touches(paths)
    ser = series(tb, pm, legs)
    t0 = min(r[0][0] for r in tb.values())
    t1 = max(r[-1][0] for r in tb.values())
    print(f"{(t1 - t0) / 3600:.2f} h; legs with SIG and PM series: {len(ser)}")
    # level relation: SIG mid - PM mid
    gaps = []
    for ex, (s, p) in ser.items():
        for t in range(int(t0) + 60, int(t1), 60):
            a, b = at(s, t), at(p, t)
            if a is not None and b is not None:
                gaps.append((a - b) / TICK)
    print(f"  SIG mid - PM mid (ticks of 0.005), sampled each minute: p10 {pct(gaps,.1):+.1f} med {pct(gaps,.5):+.1f} p90 {pct(gaps,.9):+.1f}; "
          f"|gap| <= 1 tick {sum(abs(g) <= 1 for g in gaps) / max(1, len(gaps)):.0%}")
    # PM moves: mid change >= pm_move vs `window` s before, first crossing; then signed SIG mid change after
    hs = (1, 2, 5, 10, 30, 60, 120, 300, 600, 1200)
    ev = []
    for ex, (s, p) in ser.items():
        last_ev = -1e18
        pts = [r[0] for r in p]
        for i, (t, m, bb, ba) in enumerate(p):
            j = bisect.bisect_right(pts, t - window) - 1
            if j < 0 or t - last_ev < window:
                continue
            d = m - p[j][1]
            if abs(d) >= pm_move - 1e-9:
                sg = 1 if d > 0 else -1
                base = at(s, t)
                if base is None:
                    continue
                row = {"ex": ex, "t": t, "d": d, "sig0_vs_pm": (base - m) / TICK * sg}
                for h in hs:
                    if t + h <= t1:
                        v = at(s, t + h)
                        row[h] = (v - base) / TICK * sg if v is not None else None
                ev.append(row)
                last_ev = t
    print(f"  PM moves >= {pm_move:.3f} in {window:.0f} s: {len(ev)} on {len({e['ex'] for e in ev})} legs")
    for h in hs:
        xs = [e[h] for e in ev if e.get(h) is not None]
        if xs:
            print(f"    SIG mid follow after {h:5d} s: mean {statistics.mean(xs):+.2f} ticks, moved same way {sum(x > 0.2 for x in xs)}/{len(xs)}, "
                  f"against {sum(x < -0.2 for x in xs)}")
    # SIG requotes (both sides shift, no print): PM change in the window before, signed by SIG move
    ev2 = move_events(tb, tr)
    shifts = [e for e in ev2 if not e["sweep"] and e["dbid"] and e["dask"] and (e["dbid"] > 0) == (e["dask"] > 0) and e["ex"] in ser]
    print(f"  SIG DMM shifts on mapped legs: {len(shifts)}")
    for h in (2, 5, 10, 30, 60, 120, 300, 600):
        xs = []
        for e in shifts:
            s, p = ser[e["ex"]]
            a, b = at(p, e["ts"] - h), at(p, e["ts"])
            if a is not None and b is not None:
                xs.append((b - a) / TICK * (1 if e["mid"] > 0 else -1))
        if xs:
            print(f"    PM mid change in the {h:4d} s before a SIG shift (signed): mean {statistics.mean(xs):+.2f} ticks, "
                  f"same way {sum(x > 0.2 for x in xs)}/{len(xs)}, against {sum(x < -0.2 for x in xs)}")
    return ev, shifts, ser


def report_standing(paths, rets=(0.0, 0.01, 0.015, 0.02, 0.024, 0.03, 0.05)):
    """Standing set arbs in the books at the end of each file: sets, capital and profit to settlement by return bar."""
    uni = universe()
    races = {n: r for n, r in uni.races.items() if len(r.legs) >= 2}
    xs = {n: X_DEFAULT.get(r.office, 0.02) for n, r in races.items()}
    book = {}
    for kind, d in load(paths):
        if kind == "book":
            book[d["ex"]] = d
    print(f"books held {len(book)}")
    for mr in rets:
        tot_sets = tot_prof = tot_cap = 0.0
        nr = 0
        for n, r in races.items():
            exs = [l.exchange_id for l in r.legs.values()]
            if any(e not in book or not book[e]["bids"] or not book[e]["asks"] for e in exs):
                continue
            for kindset, side in (("sell", "bids"), ("buy", "asks")):
                lv = {e: book[e][side] for e in exs}
                sets, prof = walk(lv, exs, kindset, xs[n], 0.0049, mr)
                if sets:
                    nr += 1
                    tot_sets, tot_prof = tot_sets + sets, tot_prof + prof
                    # capital: sell = sum of chunks (n - sum bids) = (n - 1) sets - prof; buy = sum asks = sets - prof - x sets
                    tot_cap += ((len(exs) - 1) * sets - prof) if kindset == "sell" else (sets - prof - xs[n] * sets)
        print(f"  ret >= {mr:.3f}: {nr} race-sides, {tot_sets:,.0f} sets, capital {tot_cap:,.0f}, profit to settlement {tot_prof:,.0f} "
              f"({tot_prof / tot_cap:.2%} of capital)" if tot_cap else f"  ret >= {mr:.3f}: none")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "arbs":
        mr = float(args.pop(0)[len("--min-ret="):]) if args and args[0].startswith("--min-ret=") else 0.0
        report_arbs(args, mr)
    elif cmd == "moves":
        report_moves(args)
    elif cmd == "pairs":
        report_pairs()
    elif cmd == "lead":
        report_lead(args)
    elif cmd == "standing":
        report_standing(args)
