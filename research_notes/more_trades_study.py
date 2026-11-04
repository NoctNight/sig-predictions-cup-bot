"""More-trades study (24 Oct). Read-only. uv run --with numpy --with matplotlib python research_notes/more_trades_study.py `--fetch` does the leaderboard reads (counted in data/research_more_trades/reads.log);
default runs the analysis on cached data and the sqlite tape. Never prints the API key."""
import json, os, sys, time, argparse
from datetime import datetime, timezone
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "data/research_more_trades")
DB = os.path.join(ROOT, "data/sigbot.sqlite")


def fetch():
    from sigbot.config import BotConfig
    from sigbot.status import api
    cfg = BotConfig.load()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    for period in ("all", "7d", "1d"):
        for sort in ("volume", "trades", "pnl"):
            path = f"/tournaments/midterm-elections/leaderboard?period={period}&sort={sort}&limit=100&offset=0"
            r = api(cfg, path)
            json.dump({"path": path, "at": stamp, "body": r}, open(os.path.join(OUT, f"lb_{period}_{sort}_{stamp}.json"), "w"))
            open(os.path.join(OUT, "reads.log"), "a").write(f"{stamp}\t{path}\n")
            rows = r.get("leaderboard") or r.get("rows") or []
            print(period, sort, len(rows))
            time.sleep(1.0)




# ---------------------------------------------------------------- tape helpers
def iso(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        a, b = s.split(".", 1)
        k = b.find("+")
        frac, tz = (b[:k], b[k:]) if k >= 0 else (b, "")
        s = f"{a}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).timestamp()


def load_tape(since="2026-10-22"):
    import sqlite3
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute("SELECT exchange_id, price, quantity, executed_at, aggressor FROM tape WHERE executed_at>=? ORDER BY executed_at",
                       (since,)).fetchall()
    return [(r[0], r[1], r[2], iso(r[3]), r[4]) for r in rows]


def race_of():
    from sigbot.proposals.hft_study import universe
    uni = universe()
    leg = {}
    for name, r in uni.races.items():
        for party, lg in r.legs.items():
            leg[str(lg.exchange_id)] = (name, party, len(r.legs))
    return leg


def set_flow(tape, leg, window=2.0, shift=0.0):
    """Same-quantity prints on two different legs of one race within `window` s (other leg's clock shifted by `shift`)."""
    import bisect, collections
    by_race = collections.defaultdict(list)
    for ex, p, q, t, ag in tape:
        if ex in leg and leg[ex][2] >= 2:
            by_race[leg[ex][0]].append((t, ex, p, q, ag))
    matched, gaps, vol_m, vol_all = 0, [], 0.0, 0.0
    for race, prints in by_race.items():
        prints.sort()
        ts = [x[0] for x in prints]
        used = set()
        for i, (t, ex, p, q, ag) in enumerate(prints):
            vol_all += p * q
            if i in used or q < 20:
                continue
            j0 = bisect.bisect_left(ts, t + shift - window)
            for j in range(j0, len(prints)):
                t2, ex2, p2, q2, ag2 = prints[j]
                if t2 > t + shift + window:
                    break
                if j != i and j not in used and ex2 != ex and q2 == q and leg[ex][2] == 2:
                    used.update((i, j)); matched += 1; vol_m += p * q + p2 * q2
                    gaps.append((p + p2, ag, ag2, q, t))
                    break
    return matched, vol_m, vol_all, gaps


def hf_capacity(Ks=(6_000, 12_000, 24_000, 32_000, 48_000, 64_000)):
    """Re-run the high-frequency arb simulator (arb_convergence_study) on panel B (real SIG touch, never used to pick the rule)
    at several capital levels. Returns per-day gain on the typical mark and the touch mark, and the per-1k split by half."""
    import numpy as np
    sys.path.insert(0, os.path.join(ROOT, "research_notes"))
    import arb_convergence_study as A
    vq, arb, qs, tape, cyc = A.load_db()
    races, mk = A.races_from_markets()
    xt = A.x_table(races)
    PB, spreads, leg, fresh = A.panel_B(vq, races)
    cc = A.closes_from_candles(os.path.join(A.DATA, "candles_5m.json"))
    PA, _ = A.proxy_panel("A", cc, races, spreads, np.arange(A.A0, A.A1 + 1, A.STEP), smooth=6)
    trail = A.trailing_mid([PA, PB])
    churn, hold = A.candidates(PB, xt, trail["B"], 4), A.candidates(PB, xt, trail["B"], None)
    NB = len(PB.grid)
    days = (PB.grid[-1] - PB.grid[0]) / 86400
    half = NB // 2
    out = []
    for K in Ks:
        row = dict(K=K, days=days)
        for nm, (a, b) in (("all", (0, NB - 1)), ("h1", (0, half)), ("h2", (half, NB - 1))):
            c = A.simulate(PB, churn, a, b, K=K, k=2, maxhold=86400)
            h = A.simulate(PB, hold, a, b, K=K)
            d = (PB.grid[b] - PB.grid[a]) / 86400
            row[nm] = {m_: (c[m_] - h[m_]) / d for m_ in ("typ_gain", "cash_gain") if m_ in c}
            row[nm]["exits"] = c.get("exits")
        out.append(row)
        print(K, {k: v for k, v in row.items() if k != "K"})
    json.dump(out, open(os.path.join(OUT, "hf_capacity.json"), "w"), indent=1, default=float)
    return out


def our_markouts(horizons=(1800, 10800)):
    """Markout of our own fills (ticks, our favour) vs the logged mid h seconds later, by purpose x day x level."""
    import sqlite3, bisect, collections
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    q = collections.defaultdict(lambda: ([], []))
    for ex, ts, mid in con.execute("SELECT exchange_id, ts, mid FROM quotes WHERE mid IS NOT NULL ORDER BY ts"):
        q[ex][0].append(ts); q[ex][1].append(mid)
    res = collections.defaultdict(list)
    for ex, purpose, price, qty, at, level in con.execute(
            "SELECT exchange_id, purpose, price, quantity, filled_at, level FROM fills WHERE purpose IN ('mm','tilt','inside','exit','direction')"):
        t = iso(at)
        ts, mids = q.get(ex, ([], []))
        for h in horizons:
            i = bisect.bisect_left(ts, t + h)
            if i < len(ts) and ts[i] - (t + h) < 600:
                mk = (1 if qty > 0 else -1) * (mids[i] - price) / 0.005
                for key in ((purpose, at[:10], h), (purpose, "all", h), (purpose, f"L{level}", h)):
                    res[key].append((mk, abs(qty)))
    out = {}
    for k, v in res.items():
        w = sum(a for _, a in v); m = sum(x * a for x, a in v) / w
        mu = sum(x for x, _ in v) / len(v)
        sd = (sum((x - mu) ** 2 for x, _ in v) / max(len(v) - 1, 1)) ** 0.5
        out["|".join(map(str, k))] = dict(n=len(v), shares=w, wmean=m, mean=mu, t=mu / (sd / len(v) ** 0.5) if sd else 0)
    return out


def resting_by_race(h=1800, cap=500):
    """Venue-wide resting-side markout (ticks) per race, train = 22 Oct UTC, test = 23 Oct UTC. Mark = last print on the same
    leg in [t+h-15min, t+h]. Per print, size capped at `cap` (what one clip of ours could get)."""
    import bisect, collections, random
    tape = load_tape(); leg = race_of()
    by_ex = collections.defaultdict(lambda: ([], []))
    for ex, p, q, t, ag in tape:
        by_ex[ex][0].append(t); by_ex[ex][1].append(p)
    acc = collections.defaultdict(lambda: [0.0, 0.0, 0])
    for ex, p, q, t, ag in tape:
        if ag not in ("buy", "sell"):
            continue
        ts, ps = by_ex[ex]
        i = bisect.bisect_right(ts, t + h) - 1
        if i < 0 or ts[i] < t + h - 900:
            continue
        mk = (-1 if ag == "buy" else 1) * (ps[i] - p) / 0.005
        day = "train" if datetime.fromtimestamp(t, timezone.utc).day == 22 else "test"
        w = min(q, cap)
        a = acc[(leg[ex][0], day)]; a[0] += mk * w; a[1] += w; a[2] += 1
    races = sorted({r for r, _ in acc})
    tr = {r: acc[(r, "train")][0] / acc[(r, "train")][1] for r in races if acc[(r, "train")][2] >= 30}
    te = {r: acc[(r, "test")][0] / acc[(r, "test")][1] for r in races if acc[(r, "test")][2] >= 30}
    both = sorted(set(tr) & set(te))
    def rank(xs):
        o = sorted(range(len(xs)), key=lambda i: xs[i]); r = [0] * len(xs)
        for k, i in enumerate(o): r[i] = k
        return r
    a, b = rank([tr[r] for r in both]), rank([te[r] for r in both])
    n = len(both); mu = (n - 1) / 2
    rho = sum((x - mu) * (y - mu) for x, y in zip(a, b)) / sum((x - mu) ** 2 for x in a)
    top = set(sorted(both, key=lambda r: -tr[r])[: max(1, n // 5)])
    diff = sum(te[r] for r in top) / len(top) - sum(te[r] for r in both if r not in top) / (n - len(top))
    rng = random.Random(1); null = []
    for _ in range(2000):
        s = set(rng.sample(both, len(top)))
        null.append(sum(te[r] for r in s) / len(s) - sum(te[r] for r in both if r not in s) / (n - len(s)))
    p = sum(x >= diff for x in null) / len(null)
    allw = lambda d: sum(acc[(r, d)][0] for r in races) / sum(acc[(r, d)][1] for r in races)
    import statistics
    return dict(null_sd=statistics.pstdev(null), n_races=n, spearman=rho, top_fifth_test=sum(te[r] for r in top) / len(top), top_minus_rest=diff, perm_p=p,
                venue_train=allw("train"), venue_test=allw("test"), tr=tr, te=te, top=sorted(top))


# ---------------------------------------------------------------- leaders
def leaders(top=30):
    import glob
    L = {}
    for f in sorted(glob.glob(os.path.join(OUT, "lb_*_*_*.json"))):
        _, p, s, _ = os.path.basename(f).split("_", 3)
        L[(p, s)] = json.load(open(f))["body"]["leaderboard"]
    idx = {}
    for (p, s), rows in L.items():
        for r in rows:
            idx.setdefault(r["username"], {})[p] = r
    rows = []
    for r in L[("all", "pnl")][:top]:
        d = idx[r["username"]]; a = r; w = d.get("7d"); o = d.get("1d")
        p7 = w["pnl"] if w else 0.0; t7 = w["tradesCount"] if w else 0; v7 = w["volume"] if w else 0.0
        bp7 = 1e4 * p7 / v7 if v7 else float("nan")
        recent = p7 / a["pnl"] if a["pnl"] else 0
        if t7 >= 2000 and bp7 < 100:
            style = "high-count scalper / market maker"
        elif w is None or (recent < 0.05 and t7 < 2000):
            style = "early winner, little since"
        elif t7 < 300 and abs(bp7) >= 150:
            style = "few large directional bets"
        else:
            style = "mixed: arbs + directional"
        rows.append(dict(user=r["username"], rank=a["rank"], pnl=a["pnl"], vol=a["volume"], trades=a["tradesCount"],
                         clip=a["volume"] / a["tradesCount"], bp=1e4 * a["pnl"] / a["volume"], win=a["winRate"],
                         p7=p7, t7=t7, v7=v7, bp7=bp7, recent=recent, p1=o["pnl"] if o else 0, t1=o["tradesCount"] if o else 0,
                         style=style))
    pop = [d["7d"] for d in idx.values() if "7d" in d and d["7d"]["volume"] > 0]
    me = idx.get("sunny-beacon-7397", {})
    return rows, pop, me


# ---------------------------------------------------------------- figure
def figure(rows, pop, me, cap, mk, rr30, rr3h):
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from arb_convergence_study import _header, _glossary
    fig = plt.figure(figsize=(13, 25), constrained_layout=True)
    gs = fig.add_gridspec(6, 1, height_ratios=[0.5, 7, 6, 6, 0.1, 2.3])
    _header(fig.add_subplot(gs[0]), "Can more trades make money for us?",
            "Who trades a lot on SIG, what they earn per trade, and whether our working trades scale up")
    # A: leaders
    ax = fig.add_subplot(gs[1])
    ax.scatter([r["tradesCount"] for r in pop], [1e4 * r["pnl"] / r["volume"] for r in pop], s=12, color="0.75",
               label="Other accounts in the week's top-100 lists")
    cols = {"high-count scalper / market maker": "#d62728", "mixed: arbs + directional": "#9467bd",
            "few large directional bets": "#2ca02c", "early winner, little since": "#1f77b4"}
    for st_, c in cols.items():
        g = [r for r in rows if r["style"] == st_ and r["t7"] > 0]
        ax.scatter([r["t7"] for r in g], [r["bp7"] for r in g], s=55, color=c, edgecolor="k", lw=0.5,
                   label=f"Top 30 all-time, {st_} ({len([r for r in rows if r['style'] == st_])} accounts)")
    w = me.get("7d")
    if w:
        ax.scatter([w["tradesCount"]], [1e4 * w["pnl"] / w["volume"]], marker="*", s=380, color="gold", edgecolor="k",
                   zorder=5, label=f"Us: {w['tradesCount']:,} trades, {w['pnl']:+,.0f} on {w['volume'] / 1e3:,.0f}k traded")
    bins = [1, 100, 500, 2000, 5000, 30000]
    xs, med, lo, hi = [], [], [], []
    for a, b in zip(bins[:-1], bins[1:]):
        v = sorted(1e4 * r["pnl"] / r["volume"] for r in pop if a <= r["tradesCount"] < b)
        if len(v) >= 5:
            xs.append(math.sqrt(a * b)); med.append(v[len(v) // 2]); lo.append(v[len(v) // 4]); hi.append(v[3 * len(v) // 4])
    ax.plot(xs, med, "k-", lw=2, label="Typical (median) account at that trade count")
    ax.fill_between(xs, lo, hi, color="k", alpha=0.08, label="Middle half of accounts at that trade count")
    ax.set_xscale("log"); ax.set_ylim(-400, 1200); ax.axhline(0, color="0.4", lw=0.8)
    ax.set_xlabel("Trades in the last 7 days (log scale)"); ax.set_ylabel("Profit per 10,000 traded, last 7 days\n(basis points of volume)")
    ax.set_title("A. Accounts that trade more earn less per trade; top accounts made 95% of their profit before 16 Oct",
                 loc="left", fontweight="bold", fontsize=12)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), fontsize=9.5, ncol=1, frameon=False)
    ax.grid(alpha=0.3)
    # B: HF arb capacity
    ax = fig.add_subplot(gs[2])
    K = [c["K"] / 1e3 for c in cap]
    for nm, ls, lab in (("h1", ":", "First day of real quotes"), ("h2", "-", "Second day of real quotes"),
                        ("all", "-.", "Both days together")):
        t = [c[nm]["typ_gain"] for c in cap]; e = [c[nm]["cash_gain"] for c in cap]
        col = {"h1": "#1f77b4", "h2": "#d62728", "all": "0.3"}[nm]
        ax.plot(K, t, ls=ls, marker="o", color=col, lw=2, label=f"{lab}: sets valued at a typical exit price")
        ax.fill_between(K, [min(a, b) for a, b in zip(t, e)], [max(a, b) for a, b in zip(t, e)], color=col, alpha=0.12)
    ax.axvline(12, color="0.3", lw=1, ls="--"); ax.text(12.4, ax.get_ylim()[1] * 0.92, "live cap today (12k)", fontsize=9.5)
    ax.axhline(0, color="0.4", lw=0.8)
    ax.set_xlabel("Capital given to the high-frequency arb (thousand SUSQies)")
    ax.set_ylabel("Gain over just holding, per day (SUSQies)")
    ax.set_title("B. More capital for the high-frequency arb: each single day says it scales, the two days together say barely",
                 loc="left", fontweight="bold", fontsize=12)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), fontsize=9.5, ncol=1, frameon=False)
    ax.grid(alpha=0.3)
    # C: candidate edges train vs test
    ax = fig.add_subplot(gs[3])
    items = []
    def se(d):
        return abs(d["mean"] / d["t"]) if d.get("t") else float("nan")
    for lab, h in (("Our market making,\nchecked 30 min later", 1800), ("Our market making,\nchecked 3 h later", 10800)):
        tr, te = mk.get(f"mm|2026-10-22|{h}"), mk.get(f"mm|2026-10-23|{h}")
        items.append((lab, tr["mean"], 1.96 * se(tr), te["mean"], 1.96 * se(te)))
    for lab, rr in (("Quote the races where the\nresting side did best (30 min)", rr30),
                    ("Quote the races where the\nresting side did best (3 h)", rr3h)):
        trv = sum(rr["tr"][r] for r in rr["top"]) / len(rr["top"])
        items.append((lab, trv, float("nan"), rr["top_minus_rest"], 1.96 * rr["null_sd"]))
    for i, (lab, a, ea, b, eb) in enumerate(items):
        ax.errorbar(i - 0.12, a, yerr=None if math.isnan(ea) else ea, fmt="^", ms=9, mfc="white", color="#1f77b4", capsize=4,
                    ls=":", label="Training day (22 Oct)" if i == 0 else None)
        ax.errorbar(i + 0.12, b, yerr=eb, fmt="o", ms=9, color="#d62728", capsize=4,
                    label="Test day (23 Oct)" if i == 0 else None)
    ax.axhline(0, color="0.4", lw=0.8)
    ax.set_xticks(range(len(items))); ax.set_xticklabels([x[0] for x in items], fontsize=10)
    ax.set_ylabel("Profit per share after the spread (ticks)")
    ax.set_title("C. Ways to trade more that we tested: none is reliably above zero on the test day",
                 loc="left", fontweight="bold", fontsize=12)
    ax.legend(loc="upper right", fontsize=9.5)
    ax.grid(alpha=0.3, axis="y")
    _glossary(fig.add_subplot(gs[5]), [
        ("profit per 10,000 traded", "leaderboard profit divided by leaderboard volume, x 10,000 (basis points)"),
        ("trades", "leaderboard trade count; one order can be several trades"),
        ("early winner, little since", "under 5% of all-time profit made in the last 7 days and fewer than 2,000 trades"),
        ("high-count scalper", "2,000+ trades in 7 days at under 100 basis points of volume"),
        ("high-frequency arb", "buy a two-leg set when its gap to payout is 4+ ticks wider than its 24-h median, sell at +2 ticks"),
        ("typical exit price / touch", "band edges in B: held sets valued at their usual exit price, or at the last best bid"),
        ("tick", "0.005, the smallest SIG price step"),
        ("resting side", "the order that was waiting in the book when a trade printed (mostly SIG's market maker)"),
        ("bars in C", "95% range: from the spread of our fills (market making) or from 2,000 random picks of races (quoting)"),
        ("training / test day", "22 Oct UTC is used to pick, 23 Oct UTC to check; panel B has no training choice"),
    ], ncol=2, width=62, fontsize=9.5)
    p = os.path.join(ROOT, "research_notes/figures/more_trades.png")
    fig.savefig(p, dpi=100)
    return p


def main():
    rows, pop, me = leaders()
    cap = json.load(open(os.path.join(OUT, "hf_capacity.json"))) if os.path.exists(os.path.join(OUT, "hf_capacity.json")) else hf_capacity()
    mk = our_markouts(); json.dump(mk, open(os.path.join(OUT, "our_markouts.json"), "w"), indent=1)
    rr30, rr3h = resting_by_race(1800), resting_by_race(10800)
    json.dump({"rows": rows}, open(os.path.join(OUT, "leaders.json"), "w"), indent=1)
    print(figure(rows, pop, me, cap, mk, rr30, rr3h))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--capacity", action="store_true", help="re-run the high-frequency arb capital sweep")
    a = ap.parse_args()
    if a.fetch:
        fetch()
    elif a.capacity:
        hf_capacity()
    else:
        main()
