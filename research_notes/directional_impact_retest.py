"""Re-test (23 Oct 2026) of directional_impact_study.py on the live quotes logged since that study's sample end.

Research only: DB opened read-only, no venue calls, nothing under sigbot/ or config.json touched.

    python3 research_notes/directional_impact_retest.py

Method is the study's own code (imported): find_events / clustered / drop_top on venue_fair (set B), liquid legs from the
21 Oct 22:03 books with the median spread measured on the ORIGINAL B window (so the leg universe is the one the study
fixed), D leg per race, consensus PM/Kalshi within 4 pts, one event per race per hour, taker in / taker out at the touch,
exit on gap close or after H hours. t = cluster-robust by race (plus 2-h entry-block clustering as a check).

"New" = an event whose H-hour outcome was not observable in the study (entry + H > OLD_END = 22 Oct 22:21:59 UTC).
"""
import bisect
import collections
import importlib.util
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("dis", os.path.join(HERE, "research_notes", "directional_impact_study.py"))
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

TICK = S.TICK
OLD_END = 1792707719.0       # 2026-10-22 22:21:59 UTC, end of the study's OOS2 window
VARIANTS = [(6, 99, 12), (6, 99, 4), (4, 99, 12), (4, 99, 4), (10, 99, 12), (10, 99, 4),
            (4, 6, 12), (6, 10, 12), (4, 6, 4), (6, 10, 4), (6, 99, 1), (6, 99, 24)]


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d")


def hm(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%d %H:%M")


def sel(evs, lo, hi, H):
    return [e for e in evs if lo <= abs(e["gap"]) < hi and e.get(f"pnl_tt_{H}h") is not None]


def row(es, H):
    k = f"pnl_tt_{H}h"
    m, t, n, G = S.clustered(es, k)
    if n == 0:
        return dict(n=0)
    mm, tm, _, _ = S.clustered(es, f"pnl_mm_{H}h")
    _, t2, _, G2 = S.clustered_by(es, k, lambda e: int(e["ts"] // 7200))
    dr, _ = S.drop_top(es, k)
    md, td, _, _ = S.clustered(dr, k)
    return dict(n=n, G=G, m=m, t=t, mm=mm, tm=tm, t2=t2, G2=G2, md=md, td=td,
                hold=sum(e[f"hold_{H}h"] for e in es) / n, span=f"{hm(min(e['ts'] for e in es))}-{hm(max(e['ts'] for e in es))}")


def label(lo, hi):
    return f"{lo}+" if hi >= 99 else f"{lo}-{hi}"


def f(x, nd=2):
    return "-" if x is None else f"{x:+.{nd}f}"


def print_row(name, lo, hi, H, r):
    if not r.get("n"):
        print(f"| {name} | {label(lo, hi)} | {H} | 0 | - | - | - | - | - | - |")
        return
    print(f"| {name} | {label(lo, hi)} | {H} | {r['n']} | {r['G']} | {f(r['m'])} ({f(r['t'], 1)}) | {f(r['t2'], 1) if r['G2'] >= 2 else '-'} ({r['G2']}) | "
          f"{f(r['mm'])} | {f(r['md'])} ({f(r['td'], 1)}) | {r['span']} |")


def events_B():
    con = S.db()
    books = S.load_books()
    legsB = S.load_B(con)
    legs_old = {k: [p for p in v if p[0] <= OLD_END] for k, v in legsB.items()}
    legs_old = {k: v for k, v in legs_old.items() if v}
    spreads_old = S.B_spreads(legs_old)
    liq = S.leg_liquidity(books, spreads_old)
    liq_new = S.leg_liquidity(books, S.B_spreads(legsB))
    n_liq = sum(v["liquid"] for v in liq.values())
    n_liq_new = sum(v["liquid"] for v in liq_new.values())
    ev_old = S.find_events(S.series_B(legs_old, liq), "B-old")      # reproduction of the study's OOS2
    ev = S.find_events(S.series_B(legsB, liq), "B")
    tsmax = max(p[0] for v in legsB.values() for p in v)
    return con, books, liq, n_liq, n_liq_new, ev_old, ev, tsmax, spreads_old


def events_A(liq, spreads_old):
    mk = S.load_markets()
    evA = S.find_events(S.series_A(mk, liq, spreads_old), "A")
    for e in evA:
        e["split"] = "IS" if e["ts"] < S.SPLIT_TS else "OOS1"
    return evA


# ----------------------------------------------------------------------------- live direction sleeve
def mid_series(con):
    vf = collections.defaultdict(list)
    for ts, ex, b, a in con.execute("SELECT ts, exchange_id, sig_bid, sig_ask FROM venue_fair WHERE sig_bid IS NOT NULL AND sig_ask IS NOT NULL"):
        vf[str(ex)].append((ts, (a + b) / 2))
    for ts, ex, b, a in con.execute("SELECT ts, exchange_id, best_bid, best_ask FROM quotes WHERE best_bid IS NOT NULL AND best_ask IS NOT NULL"):
        vf[str(ex)].append((ts, (a + b) / 2))
    for ex in vf:
        vf[ex].sort()
    return {ex: ([x[0] for x in v], [x[1] for x in v]) for ex, v in vf.items()}


def mid_at(ms, ex, t, max_age=600):
    s = ms.get(ex)
    if not s:
        return None
    i = bisect.bisect_right(s[0], t) - 1
    if i < 0 or t - s[0][i] > max_age:
        return None
    return s[1][i]


def direction_sleeve(con, tsmax):
    ms = mid_series(con)
    fills = collections.defaultdict(list)
    for oid, ex, price, qty in con.execute("SELECT order_id, exchange_id, price, quantity FROM fills WHERE purpose='direction'"):
        fills[oid].append((str(ex), price, abs(qty)))
    orders = {oid: (pa, side, fair, mid) for oid, pa, side, fair, mid in con.execute(
        "SELECT order_id, placed_at, side, fair, mid FROM orders WHERE purpose='direction'")}
    race = {str(ex): r for ex, r in con.execute("SELECT DISTINCT exchange_id, race FROM venue_fair")}
    out = []
    for oid, fl in fills.items():
        if oid not in orders:
            continue
        pa, side, fair, mid0 = orders[oid]
        ex = fl[0][0]
        q = sum(x[2] for x in fl)
        sg = 1.0 if side == "yes" else -1.0
        vw_yes = sum(x[1] * x[2] for x in fl) / q            # fills carry the YES price
        r = dict(oid=oid, ex=ex, race=race.get(ex, ex), q=q, ts=pa, side=side, cost=q * (vw_yes if sg > 0 else 1 - vw_yes),
                 edge_fair=(sg * (fair - vw_yes) / TICK) if fair is not None else None)
        m0 = mid_at(ms, ex, pa, 300)
        r["half_spread_paid"] = sg * (vw_yes - m0) / TICK if m0 is not None else None
        for name, h in (("5m", 300), ("30m", 1800), ("2h", 7200), ("12h", 43200)):
            if pa + h > tsmax:
                r["mo_" + name] = None
                continue
            m = mid_at(ms, ex, pa + h)
            r["mo_" + name] = sg * (m - vw_yes) / TICK if m is not None else None   # vs fill price, ticks/share
        m = mid_at(ms, ex, tsmax, 3600)
        r["mo_now"] = sg * (m - vw_yes) / TICK if m is not None else None
        out.append(r)
    return out


def wmean_t(rs, key):
    """share-weighted mean and race-clustered t of a per-order value."""
    xs = [(r["race"], r["q"], r[key]) for r in rs if r.get(key) is not None]
    if len(xs) < 2:
        return None, None, len(xs), sum(q for _, q, _ in xs)
    W = sum(q for _, q, _ in xs)
    m = sum(q * x for _, q, x in xs) / W
    g = collections.defaultdict(float)
    for c, q, x in xs:
        g[c] += q * (x - m)
    G = len(g)
    var = sum(v * v for v in g.values()) / W / W * G / max(G - 1, 1)
    return m, (m / var ** 0.5 if var > 0 else None), len(xs), W


def main():
    con, books, liq, n_liq, n_liq_new, ev_old, ev, tsmax, spreads_old = events_B()
    print(f"# liquid legs (study universe): {n_liq}; with spreads re-measured on the full window: {n_liq_new}")
    print(f"# B window now ends {datetime.fromtimestamp(tsmax, timezone.utc):%Y-%m-%d %H:%M} UTC; study ended 2026-10-22 22:21")
    print(f"# events: study window {len(ev_old)}, full window {len(ev)}")

    print("\n## 1. Reproduction of the study's OOS2 (data truncated at 22 Oct 22:21)\n")
    print("| sample | gap | H | n | races | T/T (t race) | t 2-h blocks (blocks) | mid/mid | T/T w/o top-3 (t) | entries |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for lo, hi, H in VARIANTS[:6]:
        print_row("study OOS2", lo, hi, H, row(sel(ev_old, lo, hi, H), H))

    print("\n## 2. New outcomes only (entry + H after 22 Oct 22:21; never seen by the study)\n")
    print("| sample | gap | H | n | races | T/T (t race) | t 2-h blocks (blocks) | mid/mid | T/T w/o top-3 (t) | entries |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    new = {}
    for lo, hi, H in VARIANTS:
        es = [e for e in sel(ev, lo, hi, H) if e["ts"] + H * 3600 > OLD_END]
        new[(lo, hi, H)] = es
        print_row("new", lo, hi, H, row(es, H))

    print("\n## 3. Full B window (21 Oct 23:49 - now), by UTC entry day\n")
    print("| day | gap | H | n | races | T/T (t race) | t 2-h blocks (blocks) | mid/mid | T/T w/o top-3 (t) | entries |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for lo, hi, H in VARIANTS:
        es = sel(ev, lo, hi, H)
        byd = collections.defaultdict(list)
        for e in es:
            byd[day(e["ts"])].append(e)
        for d in sorted(byd):
            print_row(d, lo, hi, H, row(byd[d], H))
        print_row("all B", lo, hi, H, row(es, H))

    print("\n## 3b. 6+ ticks, 12 h, by 4-h entry block (full B)\n")
    print("| block start (UTC) | n | T/T | mid/mid |")
    print("|---|---|---|---|")
    blk = collections.defaultdict(list)
    for e in sel(ev, 6, 99, 12):
        blk[int(e["ts"] // 14400)].append(e)
    for b in sorted(blk):
        es = blk[b]
        print(f"| {hm(b * 14400)} | {len(es)} | {f(S.clustered(es, 'pnl_tt_12h')[0])} | {f(S.clustered(es, 'pnl_mm_12h')[0])} |")

    print("\n## 4. Pooled over all days (method A 7-21 Oct + B 21-23 Oct; liquid legs, cluster t by race)\n")
    evA = events_A(liq, spreads_old)
    print("| pool | gap | H | n | races | T/T (t race) | t 2-h blocks (blocks) | mid/mid | T/T w/o top-3 (t) | entries |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for lo, hi, H in VARIANTS[:6]:
        a_is = [e for e in sel(evA, lo, hi, H) if e["split"] == "IS"]
        a_oos = [e for e in sel(evA, lo, hi, H) if e["split"] == "OOS1"]
        b = sel(ev, lo, hi, H)
        print_row("IS (A, 7-17 Oct)", lo, hi, H, row(a_is, H))
        print_row("OOS1 (A, 17-21 Oct)", lo, hi, H, row(a_oos, H))
        print_row("B full (21-23 Oct)", lo, hi, H, row(b, H))
        print_row("all OOS (OOS1 + B)", lo, hi, H, row(a_oos + b, H))
        print_row("everything", lo, hi, H, row(a_is + a_oos + b, H))
    # A by day + B by day for 6+/12h
    print("\n## 4b. Per-day T/T, 6+ ticks, 12 h (A days by candle method, B days by live quotes)\n")
    print("| day | set | n | T/T | t race |")
    print("|---|---|---|---|---|")
    byd = collections.defaultdict(list)
    for e in sel(evA, 6, 99, 12):
        byd[(day(e["ts"]), "A")].append(e)
    for e in sel(ev, 6, 99, 12):
        byd[(day(e["ts"]), "B")].append(e)
    pos = 0
    for k in sorted(byd):
        m, t, n, _ = S.clustered(byd[k], "pnl_tt_12h")
        pos += (m or 0) > 0
        print(f"| {k[0]} | {k[1]} | {n} | {f(m)} | {f(t, 1)} |")
    print(f"\nPositive day-sets: {pos} of {len(byd)}")

    print("\n## 5. Live direction sleeve (fills purpose='direction'), markout vs fill price in ticks/share, share-weighted\n")
    rs = direction_sleeve(con, tsmax)
    print(f"orders {len(rs)}, shares {sum(r['q'] for r in rs):,.0f}, cost {sum(r['cost'] for r in rs):,.0f} S, "
          f"races {len({r['race'] for r in rs})}, first {hm(min(r['ts'] for r in rs))}, last {hm(max(r['ts'] for r in rs))}")
    print("\n| horizon | orders | shares | markout (t race) | S |")
    print("|---|---|---|---|---|")
    for k in ("half_spread_paid", "edge_fair", "mo_5m", "mo_30m", "mo_2h", "mo_12h", "mo_now"):
        m, t, n, W = wmean_t(rs, k)
        print(f"| {k} | {n} | {W:,.0f} | {f(m)} ({f(t, 1)}) | {f((m or 0) * W * TICK, 1)} |")
    print("\nBy 4-h entry block (mo_12h, mo_now):\n")
    blk = collections.defaultdict(list)
    for r in rs:
        blk[int(r["ts"] // 14400)].append(r)
    for b in sorted(blk):
        a, c = wmean_t(blk[b], "mo_12h"), wmean_t(blk[b], "mo_now")
        print(f"- {hm(b * 14400)}: orders {len(blk[b])}, shares {sum(r['q'] for r in blk[b]):,.0f}, 12h {f(a[0])} (n {a[2]}), now {f(c[0])}")
    print("\nBy UTC day of entry (mo_12h, mo_now):\n")
    byd = collections.defaultdict(list)
    for r in rs:
        byd[day(r["ts"])].append(r)
    for d in sorted(byd):
        a = wmean_t(byd[d], "mo_12h")
        b = wmean_t(byd[d], "mo_now")
        print(f"- {d}: orders {len(byd[d])}, 12h {f(a[0])} (t {f(a[1], 1)}, n {a[2]}), now {f(b[0])} (t {f(b[1], 1)}), "
              f"now S {f((b[0] or 0) * b[3] * TICK, 1)}")


if __name__ == "__main__":
    main()
