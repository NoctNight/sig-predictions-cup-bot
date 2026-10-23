"""Flow patterns: is some public taker flow systematically inefficient? Read-only. python3 research_notes/flow_patterns_study.py
Classifies every public print in `tape` (our own fills removed), measures the taker's markout at 1 min / 5 min / 30 min / 2 h
against the half-spread paid, train = first half of prints by time, test = second half, t-stats clustered by market and by
hour (the smaller |t| is reported). Outputs data/research_flow_patterns/results.json and research_notes/figures/flow_patterns.png.
No venue calls, no key."""
import bisect, collections, json, math, os, sqlite3, sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sigbot.proposals.hft_study import universe  # noqa: E402

TICK = 0.005
ET = -4 * 3600
HZ = {"1m": 60, "5m": 300, "30m": 1800, "2h": 7200}
FIXED = (100, 120, 150, 200, 300, 1000)
MM_CAP = (300, 150)  # MM base ladder: level 1, level 2
OUT = os.path.join(ROOT, "data", "research_flow_patterns")
FIG = os.path.join(ROOT, "research_notes", "figures", "flow_patterns.png")
os.makedirs(OUT, exist_ok=True)
db = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)


def iso(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        a, b = s.split(".", 1)
        frac, tz = b[:b.index("+")], b[b.index("+"):]
        s = f"{a}.{frac[:6]}{tz}"
    return datetime.fromisoformat(s).timestamp()


def ctstat(rows):
    """rows: (value, cluster). Mean, cluster-robust se, t, n, clusters."""
    if len(rows) < 10:
        return dict(mean=float("nan"), se=float("nan"), t=float("nan"), n=len(rows), k=0)
    n = len(rows); m = sum(v for v, _ in rows) / n
    g = collections.defaultdict(float)
    for v, c in rows:
        g[c] += v - m
    k = len(g)
    se = math.sqrt(sum(s * s for s in g.values()) * (k / max(k - 1, 1))) / n
    return dict(mean=round(m, 3), se=round(se, 3), t=round(m / se, 2) if se > 0 else float("nan"), n=n, k=k)


def both_t(rows):
    """rows: (value, market, hour). Market- and hour-clustered; keep the more conservative (smaller |t|)."""
    a = ctstat([(v, m) for v, m, h in rows]); b = ctstat([(v, h) for v, m, h in rows])
    r = dict(a); r["t_mkt"] = a["t"]; r["t_hour"] = b["t"]
    r["t"] = min((a["t"], b["t"]), key=lambda x: abs(x) if x == x else 1e9)
    r["se"] = max(a["se"], b["se"]) if a["se"] == a["se"] else a["se"]
    return r


# ---------------- market types ----------------
uni = universe()
mtype = {}
for name, r in uni.races.items():
    kind = "chamber" if name.startswith("U.S.") else "House seat" if name.endswith(" race") else name.rsplit(" ", 1)[-1]
    for lg in r.legs.values():
        mtype[str(lg.exchange_id)] = kind

# ---------------- book snapshots (mid) ----------------
book = collections.defaultdict(list)
for ts, ex, b, a in db.execute("select ts, exchange_id, best_bid, best_ask from quotes where best_bid>0 and best_ask>best_bid"):
    book[ex].append((ts, b, a))
for ts, ex, b, a in db.execute("select ts, instrument, bid, ask from venue_quotes where venue='sig' and bid>0 and ask>bid"):
    book[ex].append((ts, b, a))
for ts, ex, b, a in db.execute("select ts, exchange_id, sig_bid, sig_ask from venue_fair where exchange_id is not null and sig_bid>0 and sig_ask>sig_bid"):
    book[ex].append((ts, b, a))
for v in book.values():
    v.sort()
bkt = {ex: [r[0] for r in v] for ex, v in book.items()}


def snap(ex, t, max_age):
    ks = bkt.get(ex)
    if not ks:
        return None
    i = bisect.bisect_right(ks, t) - 1
    if i < 0 or t - ks[i] > max_age:
        return None
    return book[ex][i]


# ---------------- tape, own fills removed ----------------
fills = collections.defaultdict(list)
for ex, p, q, t in db.execute("select exchange_id, price, quantity, filled_at from fills"):
    fills[(ex, round(p, 4), round(abs(q), 2))].append(iso(t))
tape = []
own = 0
for tid, ex, p, q, t, ag in db.execute("select trade_id, exchange_id, price, quantity, executed_at, aggressor from tape"):
    ts = iso(t)
    mine = any(abs(ts - f) < 3 for f in fills.get((ex, round(p, 4), round(q, 2)), ()))
    own += mine
    tape.append(dict(id=tid, ex=ex, p=p, q=q, t=ts, ag=ag, own=mine))
tape.sort(key=lambda r: r["t"])
print(f"tape {len(tape)}, own fills matched and removed {own}, public {len(tape) - own}")

# burst flag: same size in >= 3 distinct markets within 5 s
bysize = collections.defaultdict(list)
for r in tape:
    bysize[r["q"]].append(r)
for q, rs in bysize.items():
    ts = [r["t"] for r in rs]
    for i, r in enumerate(rs):
        lo, hi = bisect.bisect_left(ts, r["t"] - 5), bisect.bisect_right(ts, r["t"] + 5)
        r["burst"] = len({x["ex"] for x in rs[lo:hi]}) >= 3


def size_class(q):
    if q <= 2:
        return "1-2 sh"
    if q < 100:
        return "3-99 sh"
    if q in FIXED:
        return f"{int(q)} sh"
    return "1500+ sh" if q >= 1500 else "other 100-1499"


def band(m):
    c = min(m, 1 - m)
    return "longshot <15c" if c < 0.15 else "15-40c" if c < 0.40 else "40-60c"


rows = []
for r in tape:
    s0 = snap(r["ex"], r["t"], 90)
    if not s0:
        continue
    _, b0, a0 = s0
    m0 = (b0 + a0) / 2
    side = 1 if r["ag"] == "buy" else -1 if r["ag"] == "sell" else (1 if r["p"] > m0 else -1 if r["p"] < m0 else 0)
    if side == 0:
        continue
    half = side * (r["p"] - m0) / TICK  # half-spread paid by the taker, ticks
    if half < -0.01 or half > 6:  # stale snapshot (print outside the snapshot's mid on the wrong side), skip
        continue
    hr = int(((r["t"] + ET) % 86400) // 3600)
    d = dict(own=r["own"], age0=r["t"] - s0[0], t=r["t"], ex=r["ex"], q=r["q"], side=side, half=half, spread=(a0 - b0) / TICK, m0=m0,
             size=size_class(r["q"]), burst="burst" if r["burst"] else "lone", tod=f"{hr // 6 * 6:02d}-{hr // 6 * 6 + 6:02d} ET",
             mkt=mtype.get(r["ex"], "?"), band=band(m0),
             dog="buys underdog" if (side == 1 and r["p"] < 0.5) or (side == -1 and r["p"] > 0.5) else "buys favourite",
             hour=int(r["t"] // 3600))
    for k, h in HZ.items():
        s1 = snap(r["ex"], r["t"] + h, max(45, h / 4))
        if s1 and s1[0] > r["t"]:
            d[k] = side * ((s1[1] + s1[2]) / 2 - m0) / TICK  # taker's markout from pre-trade mid, ticks
    rows.append(d)
ours = [d for d in rows if d["own"]]
rows = [d for d in rows if not d["own"]]
tsplit = sorted(d["t"] for d in rows)[len(rows) // 2]
for d in ours:
    d["part"] = "train" if d["t"] < tsplit else "test"
for d in rows:
    d["part"] = "train" if d["t"] < tsplit else "test"
print(f"classified {len(rows)} (pre-trade snapshot <= 90 s); split {datetime.fromtimestamp(tsplit, timezone.utc):%d %b %H:%M} UTC")

DIMS = ["size", "burst", "tod", "mkt", "band", "dog"]
types = [("all", lambda d: True)]
for dim in DIMS:
    for v in sorted({d[dim] for d in rows}):
        types.append((v, (lambda dim, v: lambda d: d[dim] == v)(dim, v)))
# a few combinations the per-dimension cut suggests (bots: fixed-size bursts)
for sz in ("1-2 sh", "100 sh", "1000 sh", "1500+ sh"):
    types.append((f"{sz} burst", (lambda sz: lambda d: d["size"] == sz and d["burst"] == "burst")(sz)))

types.append(("fresh mid (<=35 s)", lambda d: d["age0"] <= 35))
types.append(("spread 2+ ticks", lambda d: d["spread"] >= 1.99))
span_test = (max(d["t"] for d in rows) - tsplit) / 86400
R = dict(n_public=len(tape), n_own_removed=own, n_classified=len(rows), split_utc=tsplit, test_days=round(span_test, 2), types={})
for name, f in types:
    sel = [d for d in rows if f(d)]
    out = dict(n=len(sel), shares=sum(d["q"] for d in sel),
               test_shares_day=sum(d["q"] for d in sel if d["part"] == "test") / span_test,
               test_capped_day=sum(min(d["q"], MM_CAP[0]) for d in sel if d["part"] == "test") / span_test,
               wide_capped_day=sum(min(d["q"], MM_CAP[0]) for d in sel if d["part"] == "test" and d["spread"] >= 1.99) / span_test,
               spread=round(sum(d["spread"] for d in sel) / max(len(sel), 1), 2))
    for part in ("train", "test"):
        ps = [d for d in sel if d["part"] == part]
        o = dict(n=len(ps), half=round(sum(d["half"] for d in ps) / len(ps), 3) if ps else float("nan"))
        for k in HZ:
            o[k] = both_t([(d[k], d["ex"], d["hour"]) for d in ps if k in d])
            # passive side's keep per share = half-spread paid - taker's markout
            o["keep_" + k] = both_t([(d["half"] - d[k], d["ex"], d["hour"]) for d in ps if k in d])
        # keep if we had been one tick inside the DMM, only where the spread was 2+ ticks
        o["inside_30m"] = both_t([(d["half"] - d["30m"] - 1, d["ex"], d["hour"]) for d in ps if "30m" in d and d["spread"] >= 1.99])
        out[part] = o
    R["types"][name] = out

# our own passive MM fills, same yardstick
R["ours"] = {part: both_t([(d["half"] - d["30m"], d["ex"], d["hour"]) for d in ours if d["part"] == part and "30m" in d])
             for part in ("train", "test")}


def spearman(a, b):
    ra = {k: i for i, k in enumerate(sorted(a, key=a.get))}; rb = {k: i for i, k in enumerate(sorted(b, key=b.get))}
    n = len(a); return 1 - 6 * sum((ra[k] - rb[k]) ** 2 for k in a) / (n * (n * n - 1))


# stability of the cross-section: do types that kept more in training keep more in test?
exante = {v for dim in ("tod", "mkt", "band") for v in {d[dim] for d in rows}}
for lab, keys in (("all types", [k for k in R["types"] if k != "all"]), ("known before the trade", [k for k in R["types"] if k in exante])):
    a = {k: R["types"][k]["train"]["keep_30m"]["mean"] for k in keys}; b = {k: R["types"][k]["test"]["keep_30m"]["mean"] for k in keys}
    R.setdefault("stability", {})[lab] = dict(n_types=len(keys), spearman=round(spearman(a, b), 2))

# ---------------- predictability (test half) ----------------
pred = {}
for name, f in types[1:]:
    sel = [d for d in rows if f(d)]
    by = collections.defaultdict(list)
    for d in sel:
        by[d["ex"]].append(d)
    gaps, same, sec = [], [], []
    for ex, ds in by.items():
        for a, b in zip(ds, ds[1:]):
            g = b["t"] - a["t"]
            if g < 1:
                continue
            gaps.append(g); same.append(a["side"] == b["side"])
            sec.append(abs(((b["t"] - a["t"]) + 30) % 60 - 30) < 2)  # same second of the minute (+-2 s)
    if len(gaps) < 20:
        continue
    gs = sorted(gaps); mg = sum(gs) / len(gs)
    sd = math.sqrt(sum((g - mg) ** 2 for g in gs) / len(gs))
    pred[name] = dict(n_gaps=len(gs), median_gap_s=round(gs[len(gs) // 2], 1), cv=round(sd / mg, 2),
                      same_side=round(sum(same) / len(same), 3), same_second_of_minute=round(sum(sec) / len(sec), 3))
R["predictability"] = pred

# ---------------- follows a requote? (25-min full feed capture, 22 Oct) ----------------
cap = os.path.join(ROOT, "data", "feed_capture_20261022.jsonl")
lastbbo, chg, trades = {}, collections.defaultdict(list), []
for line in open(cap):
    x = json.loads(line)
    if x.get("t") == "book":
        ex = x["ex"]; bbo = (x["bids"][0][0] if x["bids"] else None, x["asks"][0][0] if x["asks"] else None)
        ts = iso(x["as_of"]) if x.get("as_of") else x["rx"]
        if lastbbo.get(ex) not in (None, bbo):
            chg[ex].append(ts)
        lastbbo[ex] = bbo
    elif x.get("t") == "trade":
        trades.append((iso(x["at"]) if x.get("at") else x["rx"], str(x.get("ex")), float(x.get("qty") or 0)))
req = {}
if trades:
    t0, t1 = min(t for t, _, _ in trades), max(t for t, _, _ in trades)
    base = sum(len(v) for v in chg.values()) * 2 / ((t1 - t0) * max(len(chg), 1))  # P(a 2-s window holds a requote)
    for lab, f in (("1-2 sh", lambda q: q <= 2), ("3-99 sh", lambda q: 2 < q < 100), ("fixed 100-1000", lambda q: q in FIXED),
                   ("other 100-1499", lambda q: 100 <= q < 1500 and q not in FIXED), ("1500+ sh", lambda q: q >= 1500)):
        hits = n = 0
        for t, ex, q in trades:
            if not f(q):
                continue
            ks = chg.get(ex, [])
            i = bisect.bisect_left(ks, t - 2)
            # a top-of-book change 0.05-2 s before the print (exclude the print's own book update)
            hits += any(t - 2 <= k < t - 0.05 for k in ks[i:i + 5]); n += 1
        req[lab] = dict(n=n, after_requote=round(hits / n, 3) if n else None, base=round(base, 3))
R["after_requote"] = req
json.dump(R, open(os.path.join(OUT, "results.json"), "w"), indent=1)
json.dump(rows, open(os.path.join(OUT, "rows.json"), "w"))

# ---------------- print summary ----------------
print(f"{'type':20s} {'n':>6s} sprd | train: half m1 m5 m30 m2h | keep30 (t) | test: half m1 m5 m30 m2h | keep30 (t) keep2h inside30 | cap/day")
for name, o in R["types"].items():
    s = f"{name:20s} {o['n']:6d} {o['spread']:4.2f}"
    for part in ("train", "test"):
        p = o[part]
        s += f" | {p['half']:4.2f} " + " ".join(f"{p[k]['mean']:5.2f}" for k in HZ) + f" | {p['keep_30m']['mean']:5.2f} ({p['keep_30m']['t']:5.1f})"
    s += f" {o['test']['keep_2h']['mean']:5.2f} {o['test']['inside_30m']['mean']:5.2f} | {o['test_capped_day']:8.0f}"
    print(s)
print("ours:", R["ours"]); print("stability:", R["stability"])
print("predictability:", json.dumps(pred))
print("after requote:", json.dumps(req))

# ---------------- decision figure ----------------
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SHOW = ["all", "1-2 sh", "3-99 sh", "100 sh", "120 sh", "150 sh", "200 sh", "300 sh", "1000 sh", "other 100-1499", "1500+ sh",
        "burst", "lone", "House seat", "Senate", "Governor", "chamber", "longshot <15c", "15-40c", "40-60c",
        "00-06 ET", "06-12 ET", "12-18 ET", "18-24 ET"]
LAB = {"all": "all flow", "other 100-1499": "other\n100-1499 sh", "burst": "bursts\n(3+ mkts, 5 s)", "lone": "single\ntrades",
       "longshot <15c": "longshot\n<15c", "House seat": "House\nseat", "chamber": "chamber\ncontrol"}
GROUPS = [(0, 0, "all"), (1, 10, "trade size"), (11, 12, "clustering"), (13, 16, "market type"), (17, 19, "price band"), (20, 23, "time of day")]
x = list(range(len(SHOW)))
T = R["types"]
# excess keep over all flow, test half (se of the type; flags |t| > 3 in both halves)
for k in SHOW[1:]:
    ex = {p: (T[k][p]["keep_30m"]["mean"] - T["all"][p]["keep_30m"]["mean"]) / T[k][p]["keep_30m"]["se"] for p in ("train", "test")}
    T[k]["excess_t"] = {p: round(v, 2) for p, v in ex.items()}
json.dump(R, open(os.path.join(OUT, "results.json"), "w"), indent=1)

plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10, "xtick.labelsize": 9, "ytick.labelsize": 9.5})
fig = plt.figure(figsize=(13, 18))
gs = fig.add_gridspec(5, 1, height_ratios=[1, 1, 0.9, 0.22, 0.62], hspace=0.62)
axA = fig.add_subplot(gs[0]); axB = fig.add_subplot(gs[1], sharex=axA); axC = fig.add_subplot(gs[2], sharex=axA)
BLUE, RED, GREY = "#2471a3", "#c0392b", "#7f8c8d"


def line(ax, key, part, col, ls, label, sub="mean"):
    ys = [T[k][part][key]["mean"] for k in SHOW]; se = [T[k][part][key]["se"] for k in SHOW]
    ax.plot(x, ys, ls=ls, marker="o" if part == "test" else "^", ms=4, color=col, lw=1.6, label=label)
    ax.fill_between(x, [y - 2 * s for y, s in zip(ys, se)], [y + 2 * s for y, s in zip(ys, se)], color=col, alpha=0.10)


line(axA, "30m", "train", BLUE, ":", "Taker's gain from the price move, training half (21 Oct - 23 Oct 02:08 UTC)")
line(axA, "30m", "test", BLUE, "-", "Taker's gain from the price move, test half (23 Oct 02:08 - 22:50 UTC)")
axA.plot(x, [T[k]["test"]["half"] for k in SHOW], ls="none", marker="_", ms=14, mew=2, color=GREY, label="Half-spread the taker paid (test half)")
axA.axhline(0, color="0.5", lw=0.8)
axA.set_ylabel("ticks per share,\n30 min after the trade")
axA.set_title("A. Does any taker flow lose money to the move after it trades? No: the price moves toward the taker in every group", loc="left", fontweight="bold")
axA.set_ylim(-0.5, 2.2)

line(axB, "keep_30m", "train", RED, ":", "Passive side's profit (half-spread minus the move), training half")
line(axB, "keep_30m", "test", RED, "-", "Passive side's profit, test half")
axB.plot(x, [T[k]["test"]["inside_30m"]["mean"] for k in SHOW], ls="--", marker="s", ms=4, color="#8e44ad", lw=1.4,
         label="Our profit if we had quoted one tick inside SIG's market maker (only where the spread was 2+ ticks), test half")
ou = R["ours"]["test"]
axB.axhline(ou["mean"], color="#27ae60", lw=1.2, ls="-.", label=f"Our own market-maker fills today, same yardstick: {ou['mean']:+.2f} tick (n {ou['n']})")
axB.axhline(0, color="0.5", lw=0.8)
axB.set_ylabel("ticks per share kept\nafter 30 min")
axB.set_title("B. What the passive side keeps: positive but under the half-spread everywhere; stepping inside the market maker loses", loc="left", fontweight="bold")
axB.set_ylim(-1.3, 1.3)

ub = [T[k]["test_capped_day"] * T[k]["test"]["keep_30m"]["mean"] * TICK for k in SHOW]
fe = [T[k]["wide_capped_day"] * T[k]["test"]["inside_30m"]["mean"] * TICK if T[k]["test"]["inside_30m"]["n"] >= 10 else 0 for k in SHOW]
w = 0.38
axC.bar([i - w / 2 for i in x], ub, w, color="#aed6f1", edgecolor=BLUE, label="Ceiling: we are first in the queue for up to 300 shares of every such trade (impossible while SIG's market maker is first)")
axC.bar([i + w / 2 for i in x], fe, w, color=RED, label="Feasible: we quote one tick inside SIG's market maker, 300 shares, where the spread is 2+ ticks")
axC.axhline(0, color="0.5", lw=0.8)
axC.set_yscale("symlog", linthresh=100)
axC.set_ylabel("S per day (test half,\nrecorded tape)")
axC.set_title("C. Extra S per day for the bot: the only way ahead of the market maker's queue loses money", loc="left", fontweight="bold")
axC.set_xticks(x); axC.set_xticklabels([LAB.get(k, k).replace("\n", " ") for k in SHOW], rotation=40, ha="right", fontsize=9)
axC.set_ylim(-3000, 20000)
plt.setp(axA.get_xticklabels(), visible=False); plt.setp(axB.get_xticklabels(), visible=False)
for ax in (axA, axB, axC):
    ax.grid(alpha=0.25)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.04 if ax is not axC else -0.45), ncol=1, fontsize=9, frameon=False)
    for a, b, _ in GROUPS[1:]:
        ax.axvline(a - 0.5, color="0.75", lw=0.8)
for a, b, lab in GROUPS:
    axA.text((a + b) / 2, 2.12, lab, ha="center", va="top", fontsize=9.5, color="0.3", fontstyle="italic")
axC.set_xlim(-0.6, len(SHOW) - 0.4)
fig.suptitle("Is some of SIG's taker flow easy money for a market maker?", fontsize=15, fontweight="bold", y=0.94)
fig.text(0.5, 0.912, f"{R['n_classified']:,} public trades from the venue tape (our own {R['n_own_removed']:,} fills removed), grouped by size, clustering, market and time; "
         "taker's side measured 30 min later", ha="center", fontsize=10, color="0.3", va="bottom")
gax = fig.add_subplot(gs[4]); gax.axis("off")
gax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=gax.transAxes, facecolor="#f4f4f0", edgecolor="0.6"))
gax.text(0.012, 0.95, "Glossary", ha="left", va="top", fontsize=10.5, fontweight="bold", transform=gax.transAxes)
g1 = ("tick: 0.005, SIG's smallest price step (half a cent)\n"
      "taker: the trader whose order crossed the spread and traded at once\n"
      "passive side: the resting order that was hit, mostly SIG's market maker\n"
      "half-spread paid: how far the trade price was from the mid just before\n"
      "taker's gain from the move: change in the mid over 30 min, in the\n    taker's direction (positive = the price kept going their way)\n"
      "passive side's profit: half-spread minus the taker's gain")
g2 = ("bursts: same trade size in 3+ markets within 5 seconds (a bot)\n"
      "longshot: the cheaper side is under 15c; ET = US Eastern time\n"
      "training / test: first and second half of the trades by time;\n    a real pattern shows in both\n"
      "shaded bands: 95% range, standard errors clustered by market or hour\n    (the wider is used); |t| > 3 needed with ~250 tests\n"
      "S: SUSQies, the tournament's play money; the tape we record is a\n    sample of all trades, so totals are lower bounds")
gax.text(0.012, 0.86, g1, ha="left", va="top", fontsize=9, transform=gax.transAxes, linespacing=1.4)
gax.text(0.51, 0.86, g2, ha="left", va="top", fontsize=9, transform=gax.transAxes, linespacing=1.4)
fig.savefig(FIG, dpi=110, facecolor="white", bbox_inches="tight")
print("figure", FIG)
print("excess t:", {k: T[k]["excess_t"] for k in SHOW[1:]})
print("panel C ceiling/feasible:", {k: (round(a), round(b)) for k, a, b in zip(SHOW, ub, fe)})
