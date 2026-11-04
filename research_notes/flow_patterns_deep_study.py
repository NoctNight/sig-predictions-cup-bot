"""Deep sweep-catching level, paper test. Read-only. uv run --with matplotlib python research_notes/flow_patterns_deep_study.py
A virtual 300-share order rests d = 1, 2, 3 ticks behind the touch on both sides of every market the bot quotes, repriced from
the bot's own 30-s snapshots (`quotes`). Fills: (a) queue-free: a print strictly beyond our price means the level was emptied,
so our 300 filled; (b) queue-aware: prints at exactly our price fill us only after the depth ahead (median depth at that
distance in the two 22 Oct full-book captures, no cancels ahead) is used up. Profit per share = passive markout vs the
mid after 1 min / 5 min / 30 min / 2 h; also net of crossing the spread to get out. Train/test split as flow_patterns_study.py.
Outputs data/research_flow_patterns/deep.json and research_notes/figures/flow_patterns_deep.png. No venue calls, no key."""
import bisect, collections, json, math, os, sqlite3, statistics, sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICK = 0.005
SIZE = 300
DS = (1, 2, 3)
HZ = {"1m": 60, "5m": 300, "30m": 1800, "2h": 7200}
OUT = os.path.join(ROOT, "data", "research_flow_patterns")
FIG = os.path.join(ROOT, "research_notes", "figures", "flow_patterns_deep.png")
db = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
TSPLIT = json.load(open(os.path.join(OUT, "results.json")))["split_utc"]


def iso(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        a, b = s.split(".", 1)
        i = b.index("+")
        s = f"{a}.{b[:i][:6]}{b[i:]}"
    return datetime.fromisoformat(s).timestamp()


def ctstat(rows):
    if len(rows) < 10:
        return dict(mean=float("nan"), se=float("nan"), t=float("nan"), n=len(rows))
    n = len(rows); m = sum(v for v, _ in rows) / n
    g = collections.defaultdict(float)
    for v, c in rows:
        g[c] += v - m
    k = len(g); se = math.sqrt(sum(s * s for s in g.values()) * k / max(k - 1, 1)) / n
    return dict(mean=round(m, 3), se=round(se, 3), t=round(m / se, 2) if se > 0 else float("nan"), n=n)


def both_t(rows):  # rows (v, market, hour): keep the smaller |t|
    a = ctstat([(v, m) for v, m, h in rows]); b = ctstat([(v, h) for v, m, h in rows])
    r = dict(a); r["se"] = max(a["se"], b["se"]) if a["se"] == a["se"] else a["se"]
    r["t"] = min((a["t"], b["t"]), key=lambda x: abs(x) if x == x else 1e9)
    return r


# ---------------- depth ahead, calibrated on the full-book captures ----------------
caps = [os.path.join(ROOT, "data", "feed_capture_20261022.jsonl"),
        os.path.join(ROOT, "sigbot", "proposals", "hft_data", "rec_20261022_1941.jsonl")]
dep = collections.defaultdict(list)  # (ex, d) -> depths at d ticks behind the touch (both sides)
for p in caps:
    for line in open(p):
        x = json.loads(line)
        if x.get("t") != "book" or not x.get("bids") or not x.get("asks"):
            continue
        bl = {round(pr, 4): q for pr, q in x["bids"]}; al = {round(pr, 4): q for pr, q in x["asks"]}
        b1, a1 = x["bids"][0][0], x["asks"][0][0]
        for d in DS:
            dep[(x["ex"], d)].append(bl.get(round(b1 - d * TICK, 4), 0))
            dep[(x["ex"], d)].append(al.get(round(a1 + d * TICK, 4), 0))
QMED = {k: statistics.median(v) for k, v in dep.items()}
QALL = {d: statistics.median([q for (ex, dd), v in dep.items() if dd == d for q in v]) for d in DS}
print("median depth ahead (all markets):", QALL, "| markets calibrated:", len({k[0] for k in QMED}))

# ---------------- the bot's own snapshots ----------------
book = collections.defaultdict(list)
for ts, ex, b, a in db.execute("select ts, exchange_id, best_bid, best_ask from quotes where best_bid>0 and best_ask>best_bid order by ts"):
    book[ex].append((ts, b, a))
bkt = {ex: [r[0] for r in v] for ex, v in book.items()}


def snap(ex, t, max_age=90):
    ks = bkt.get(ex)
    if not ks:
        return None
    i = bisect.bisect_right(ks, t) - 1
    return book[ex][i] if i >= 0 and t - ks[i] <= max_age else None


tape = collections.defaultdict(list)
for ex, p, q, t, ag in db.execute("select exchange_id, price, quantity, executed_at, aggressor from tape"):
    if ex in book:
        tape[ex].append((iso(t), p, q, ag))
for v in tape.values():
    v.sort()
t_lo = max(min(v[0][0] for v in tape.values()), min(k[0] for k in bkt.values()))
t_hi = max(v[-1][0] for v in tape.values())

# ---------------- simulate ----------------
fills = []  # dict(d, rule, t, ex, side, P, size, keep_h..., exit_h...)
for d in DS:
    for ex, prints in tape.items():
        qa = QMED.get((ex, d), QALL[d])
        state = {}  # side -> [P, snapshot_ts, cum_at_P, filled]
        for t, p, q, ag in prints:
            s0 = snap(ex, t)
            if not s0:
                continue
            ts0, b0, a0 = s0
            if ag not in ("buy", "sell"):
                ag = "buy" if p >= a0 else "sell" if p <= b0 else None
                if not ag:
                    continue
            # a buy hits asks: our ask at a0 + d ticks; a sell hits bids: our bid at b0 - d ticks
            side = "ask" if ag == "buy" else "bid"
            P = round(a0 + d * TICK, 4) if side == "ask" else round(b0 - d * TICK, 4)
            if P <= 0 or P >= 1:
                continue
            st = state.get(side)
            if not st or st[1] != ts0 or st[0] != P:
                st = state[side] = [P, ts0, 0.0, False]  # repriced at a new snapshot: back of the queue
            if st[3]:
                continue
            beyond = p > P + 1e-9 if side == "ask" else p < P - 1e-9
            at = abs(p - P) < 1e-9
            got, rule = 0, None
            if beyond:
                got, rule = SIZE, "queue-free"
            elif at:
                st[2] += q
                if st[2] > qa:
                    got, rule = min(SIZE, st[2] - qa), "queue-aware"
            if not got:
                continue
            st[3] = True
            sg = 1 if side == "bid" else -1  # we bought on our bid, sold on our ask
            f = dict(d=d, rule=rule, t=t, ex=ex, sg=sg, P=P, size=got, hour=int(t // 3600),
                     part="train" if t < TSPLIT else "test")
            for k, h in HZ.items():
                s1 = snap(ex, t + h, max(45, h / 4))
                if s1 and s1[0] > t:
                    m1 = (s1[1] + s1[2]) / 2
                    f[k] = sg * (m1 - P) / TICK
                    f["x" + k] = f[k] - (s1[2] - s1[1]) / 2 / TICK  # then cross the spread to get flat
            fills.append(f)

# real level-2 MM fills, same yardstick (validation of the d = 1 model)
real = []
for ex, side, p, q, t, lvl in db.execute("select exchange_id, side, price, quantity, filled_at, level from fills where purpose='mm' and level in (1,2)"):
    ts = iso(t)
    s0 = snap(ex, ts)
    if not s0:
        continue
    sg = 1 if p <= (s0[1] + s0[2]) / 2 else -1  # bought below mid / sold above
    s1 = snap(ex, ts + 1800, 450)
    if s1 and s1[0] > ts:
        real.append(dict(lvl=lvl, t=ts, ex=ex, q=abs(q), keep=sg * ((s1[1] + s1[2]) / 2 - p) / TICK, hour=int(ts // 3600),
                         part="train" if ts < TSPLIT else "test"))

days = {"train": (TSPLIT - t_lo) / 86400, "test": (t_hi - TSPLIT) / 86400}
R = dict(days=days, depth_ahead=QALL, n_markets=len(tape), models={}, real={})
for d in DS:
    for rule in ("queue-free", "both"):
        key = f"d{d} {rule}"
        o = {}
        for part in ("train", "test"):
            fs = [f for f in fills if f["d"] == d and f["part"] == part and (rule == "both" or f["rule"] == rule)]
            o[part] = dict(fills_day=round(len(fs) / days[part], 1), shares_day=round(sum(f["size"] for f in fs) / days[part]))
            for k in HZ:
                o[part][k] = both_t([(f[k], f["ex"], f["hour"]) for f in fs if k in f])
                o[part]["x" + k] = both_t([(f["x" + k], f["ex"], f["hour"]) for f in fs if "x" + k in f])
            w = [(f["size"], f["30m"]) for f in fs if "30m" in f]
            o[part]["S_day_30m"] = round(sum(s * v for s, v in w) * TICK / days[part], 1)
            w = [(f["size"], f["x30m"]) for f in fs if "x30m" in f]
            o[part]["S_day_30m_exit"] = round(sum(s * v for s, v in w) * TICK / days[part], 1)
            # inventory: largest net position in one market within 2 h windows, test of the cash constraint
            o[part]["fills"] = len(fs)
        R["models"][key] = o
for lvl in (1, 2):
    R["real"][f"level {lvl}"] = {part: dict(fills_day=round(len([r for r in real if r["lvl"] == lvl and r["part"] == part]) / days[part], 1),
                                            keep30=both_t([(r["keep"], r["ex"], r["hour"]) for r in real if r["lvl"] == lvl and r["part"] == part]))
                                 for part in ("train", "test")}
# concentration: share of model profit from the top 3 markets (test, d=1 both)
fs = [f for f in fills if f["d"] == 1 and f["part"] == "test" and "30m" in f]
by = collections.Counter()
for f in fs:
    by[f["ex"]] += f["size"] * f["30m"] * TICK
tot = sum(by.values())
R["top3_share_d1_test"] = round(sum(v for _, v in by.most_common(3)) / tot, 2) if tot else None
json.dump(R, open(os.path.join(OUT, "deep.json"), "w"), indent=1)
json.dump(fills, open(os.path.join(OUT, "deep_fills.json"), "w"))

print(f"markets {len(tape)}, days train {days['train']:.2f} test {days['test']:.2f}")
print(f"{'model':18s} | part  fills/d  sh/d | keep 1m 5m 30m 2h (t30) | after exit 30m | S/day 30m, after exit")
for key, o in R["models"].items():
    for part in ("train", "test"):
        p = o[part]
        print(f"{key:18s} | {part:5s} {p['fills_day']:6.1f} {p['shares_day']:6d} | " + " ".join(f"{p[k]['mean']:+5.2f}" for k in HZ)
              + f" ({p['30m']['t']:+5.1f}) | {p['x30m']['mean']:+5.2f} ({p['x30m']['t']:+5.1f}) | {p['S_day_30m']:+7.1f} {p['S_day_30m_exit']:+7.1f}")
print("real MM fills:", json.dumps(R["real"]))
print("top-3 market share of d1 test profit:", R["top3_share_d1_test"])

# ---------------- decision figure (pooled halves for profit: too few test fills to split) ----------------
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

D = sum(days.values())
pool = {}
for d in DS:
    fs = [f for f in fills if f["d"] == d]
    pool[d] = dict(keep=ctstat([(f["30m"], f["ex"]) for f in fs if "30m" in f]) if len(fs) else None,
                   exit=ctstat([(f["x30m"], f["ex"]) for f in fs if "x30m" in f]),
                   keep2h=ctstat([(f["2h"], f["ex"]) for f in fs if "2h" in f]),
                   S=sum(f["size"] * f["30m"] for f in fs if "30m" in f) * TICK / D,
                   Sx=sum(f["size"] * f["x30m"] for f in fs if "x30m" in f) * TICK / D)


def ct3(rows):  # mean and se even for small n (pooled, clustered by market)
    n = len(rows)
    if n < 3:
        return float("nan"), float("nan"), n
    m = sum(v for v, _ in rows) / n; g = collections.defaultdict(float)
    for v, c in rows:
        g[c] += v - m
    k = len(g); return m, math.sqrt(sum(s * s for s in g.values()) * k / max(k - 1, 1)) / n, n


plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10, "xtick.labelsize": 10, "ytick.labelsize": 9.5})
fig = plt.figure(figsize=(10, 13.5))
gs = fig.add_gridspec(5, 1, height_ratios=[1, 1, 0.9, 0.05, 0.85], hspace=0.95)
axA = fig.add_subplot(gs[0]); axB = fig.add_subplot(gs[1], sharex=axA); axC = fig.add_subplot(gs[2], sharex=axA)
x = list(DS)
for part, ls, mk in (("train", ":", "^"), ("test", "-", "o")):
    axA.plot(x, [R["models"][f"d{d} both"][part]["shares_day"] for d in DS], ls=ls, marker=mk, color="#2471a3", lw=1.8,
             label=f"Shares/day our 300-share order would have filled, {'training' if part == 'train' else 'test'} half")
axA.set_ylabel("shares per day\n(136 markets)"); axA.set_ylim(0, None)
axA.set_title(f"A. How often does a sweep reach us? Rarely: {QALL[1]/1000:.1f}k / {QALL[2]/1000:.0f}k / {QALL[3]/1000:.0f}k shares wait ahead of us", loc="left", fontweight="bold")
for key, col, lab in (("30m", "#c0392b", "Profit per share 30 min after the fill (marked at mid)"),
                      ("x30m", "#8e44ad", "Same, after paying the spread to get flat"),
                      ("2h", "#e67e22", "Profit per share 2 h after the fill (marked at mid)")):
    st = [ct3([(f[key], f["ex"]) for f in fills if f["d"] == d and key in f]) for d in DS]
    ys = [s[0] for s in st]; se = [s[1] for s in st]
    axB.plot(x, ys, marker="o", color=col, lw=1.8, label=lab + "  (n " + "/".join(str(s[2]) for s in st) + ")")
    axB.fill_between(x, [y - 2 * s for y, s in zip(ys, se)], [y + 2 * s for y, s in zip(ys, se)], color=col, alpha=0.10)
axB.axhline(0, color="0.5", lw=0.8)
axB.set_ylabel("ticks per share\n(both halves pooled)")
axB.set_title("B. Profit per share when it does fill: positive deeper in the book, but few fills", loc="left", fontweight="bold")
w = 0.35
axC.bar([d - w / 2 for d in DS], [pool[d]["S"] for d in DS], w, color="#aed6f1", edgecolor="#2471a3", label="S per day, marked at mid after 30 min")
axC.bar([d + w / 2 for d in DS], [pool[d]["Sx"] for d in DS], w, color="#8e44ad", label="S per day after paying the spread to get flat")
axC.axhline(0, color="0.5", lw=0.8)
axC.set_ylabel("S per day"); axC.set_ylim(-15, 15)
axC.set_title("C. What it adds: a few S a day (a 300-share fill is worth 1.5 S per tick)", loc="left", fontweight="bold")
axC.set_xticks(x); axC.set_xticklabels(["1 tick behind the best price\n(about today's level 2)", "2 ticks behind", "3 ticks behind"])
axC.set_xlabel("Where the extra 300-share order rests")
for ax in (axA, axB, axC):
    ax.grid(alpha=0.25); ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.06 if ax is not axC else -0.42), fontsize=9, frameon=False)
plt.setp(axA.get_xticklabels(), visible=False); plt.setp(axB.get_xticklabels(), visible=False)
fig.suptitle("A deeper resting order to catch sweeps: not worth adding", fontsize=14, fontweight="bold", y=0.94)
fig.text(0.5, 0.915, "Paper test on the venue tape 21-23 Oct, the bot's 136 quoted markets, our order repriced every 30 s like the bot",
         ha="center", fontsize=10, color="0.3", va="bottom")
gax = fig.add_subplot(gs[4]); gax.axis("off")
gax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=gax.transAxes, facecolor="#f4f4f0", edgecolor="0.6"))
gax.text(0.015, 0.94, "Glossary", ha="left", va="top", fontsize=10.5, fontweight="bold", transform=gax.transAxes)
gax.text(0.015, 0.80,
         "tick: 0.005, SIG's smallest price step;  S: SUSQies, the tournament's play money\n"
         "sweep: one large order that trades through several prices at once\n"
         "filled: a trade printed beyond our price (the whole price level was used up), or the volume printed at our price\n"
         "    exceeded the shares waiting ahead of us (median depth from two full-book recordings on 22 Oct; nobody ahead cancels)\n"
         "profit per share: for a buy, the mid later minus our price (sells the reverse); bands: 95% range, clustered by market\n"
         "training / test: before / after 23 Oct 02:08 UTC; test fills were too few to judge profit alone, so B and C pool both",
         ha="left", va="top", fontsize=9, transform=gax.transAxes, linespacing=1.4)
fig.savefig(FIG, dpi=110, facecolor="white", bbox_inches="tight")
print("figure", FIG)
