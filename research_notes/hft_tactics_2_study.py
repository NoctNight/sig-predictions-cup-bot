"""HFT tactics, round 2: five new candidates, read-only. python3 research_notes/hft_tactics_2_study.py
Outputs data/research_hft2/results.json and research_notes/figures/hft2_decision.png. No venue calls, no key."""
import bisect, collections, json, math, os, random, sqlite3, sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sigbot.proposals.hft_study import touches, move_events, iso, universe  # noqa: E402

TICK = 0.005
OUT = os.path.join(ROOT, "data", "research_hft2")
FIG = os.path.join(ROOT, "research_notes", "figures", "hft2_decision.png")
SPLIT = datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()  # train < 23 Oct 00:00 UTC <= test
ET = -4 * 3600
random.seed(7)
os.makedirs(OUT, exist_ok=True)
db = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
R = {}
E_ROWS = []
A_ROWS = []


def ctstat(rows):
    """rows: (value, cluster). Mean, cluster-robust se, t, n, clusters."""
    if len(rows) < 3:
        return dict(mean=float("nan"), se=float("nan"), t=float("nan"), n=len(rows), k=0)
    n = len(rows); m = sum(v for v, _ in rows) / n
    g = collections.defaultdict(float)
    for v, c in rows:
        g[c] += v - m
    k = len(g)
    se = math.sqrt(sum(s * s for s in g.values()) * (k / max(k - 1, 1))) / n
    return dict(mean=round(m, 3), se=round(se, 3), t=round(m / se, 2) if se > 0 else float("nan"), n=n, k=k)


def binom_z(h, n, p=0.5):
    return round((h - n * p) / math.sqrt(n * p * (1 - p)), 2) if n else float("nan")


# ---------------- data ----------------
uni = universe()
leg_race = {}
for name, r in uni.races.items():
    for lg in r.legs.values():
        leg_race[str(lg.exchange_id)] = (name, lg.party, name.rsplit(" ", 1)[-1])
book = collections.defaultdict(list)  # ex -> (ts, bid, ask)
for ts, ex, b, a in db.execute("select ts, exchange_id, best_bid, best_ask from quotes where best_bid>0 and best_ask>best_bid"):
    book[ex].append((ts, b, a))
fair = collections.defaultdict(list)
for ts, ex, b, a, f in db.execute("select ts, exchange_id, sig_bid, sig_ask, fair from venue_fair where exchange_id is not null"):
    if b and a and a > b:
        book[ex].append((ts, b, a))
    if f is not None:
        fair[ex].append((ts, f))
for v in list(book.values()) + list(fair.values()):
    v.sort()
bkt = {ex: [r[0] for r in v] for ex, v in book.items()}
fkt = {ex: [r[0] for r in v] for ex, v in fair.items()}


def quote_at(ex, t, stale=300, after=False):
    ts = bkt.get(ex)
    if not ts:
        return None
    if after:
        i = bisect.bisect_left(ts, t)
        if i < len(ts) and ts[i] - t <= stale:
            return book[ex][i]
        return None
    i = bisect.bisect_right(ts, t) - 1
    if i >= 0 and t - ts[i] <= stale:
        return book[ex][i]
    return None


def mid_at(ex, t, **k):
    q = quote_at(ex, t, **k)
    return (q[1] + q[2]) / 2 if q else None


def fair_at(ex, t, stale=420):
    ts = fkt.get(ex)
    if not ts:
        return None
    i = bisect.bisect_right(ts, t) - 1
    return fair[ex][i][1] if i >= 0 and t - ts[i] <= stale else None


ours = collections.defaultdict(list)
for ex, fa, q in db.execute("select exchange_id, filled_at, abs(quantity) from fills"):
    ours[ex].append((iso(fa), q))
tape = collections.defaultdict(list)  # public prints only: (ts, price, qty, +1 buy / -1 sell)
n_own = 0
for ex, p, q, at, ag in db.execute("select exchange_id, price, quantity, executed_at, aggressor from tape where aggressor in ('buy','sell')"):
    t = iso(at)
    if any(abs(t - ft) < 2.0 and abs(q - fq) < 1 for ft, fq in ours.get(ex, [])):
        n_own += 1
        continue
    tape[ex].append((t, p, q, 1 if ag == "buy" else -1))
for v in tape.values():
    v.sort()
ttk = {ex: [r[0] for r in v] for ex, v in tape.items()}
T0 = min(r[0] for v in tape.values() for r in v); T1 = max(r[0] for v in tape.values() for r in v)
R["data"] = dict(public_prints=sum(map(len, tape.values())), own_removed=n_own, quote_legs=len(book),
                 span=[datetime.fromtimestamp(T0, timezone.utc).isoformat()[:16], datetime.fromtimestamp(T1, timezone.utc).isoformat()[:16]])
DAYS_TR = (SPLIT - max(T0, datetime(2026, 10, 21, 23, 49, tzinfo=timezone.utc).timestamp())) / 86400
DAYS_TE = (T1 - SPLIT) / 86400


def flow(ex, t0, t1):
    ts = ttk.get(ex, [])
    i, j = bisect.bisect_left(ts, t0), bisect.bisect_right(ts, t1)
    return sum(r[2] * r[3] for r in tape[ex][i:j]), sum(r[2] for r in tape[ex][i:j])


# ---------------- A. DMM requote clock and direction ----------------
def test_a():
    out = {}
    recs = [os.path.join(ROOT, "data", "feed_capture_20261022.jsonl"), os.path.join(ROOT, "sigbot/proposals/hft_data/rec_20261022_1941.jsonl")]
    rq = []
    mins = 0.0
    for p in recs:
        tb, tr, *_ = touches([p])
        ev = move_events(tb, tr)
        allts = [r[0] for v in tb.values() for r in v]
        mins += (max(allts) - min(allts)) / 60
        rq += [e for e in ev if not e["sweep"] and e["dbid"] and e["dask"] and (e["dbid"] > 0) == (e["dask"] > 0)]
    sec = [int(e["ts"]) % 60 for e in rq]
    bins = collections.Counter(s // 10 for s in sec)
    exp = len(sec) / 6
    chi = sum((bins[b] - exp) ** 2 / exp for b in range(6))
    C = sum(math.cos(2 * math.pi * s / 60) for s in sec); S = sum(math.sin(2 * math.pi * s / 60) for s in sec)
    rayl_z = (C * C + S * S) / len(sec)
    byex = collections.defaultdict(list)
    for e in rq:
        byex[e["ex"]].append(e["ts"])
    cvs = []
    for v in byex.values():
        d = [b - a for a, b in zip(sorted(v), sorted(v)[1:]) if b - a > 1]
        if len(d) >= 3:
            m = sum(d) / len(d); cvs.append(math.sqrt(sum((x - m) ** 2 for x in d) / len(d)) / m)

    def burst(times_by_ex):
        allt = sorted((t, ex) for ex, v in times_by_ex.items() for t in v)
        ts = [t for t, _ in allt]; c = 0
        for t, ex in allt:
            i, j = bisect.bisect_left(ts, t - 2), bisect.bisect_right(ts, t + 2)
            c += any(allt[k][1] != ex for k in range(i, j))
        return c / len(allt)
    obs = burst(byex)
    lo, hi = min(e["ts"] for e in rq), max(e["ts"] for e in rq)
    perm = []
    for _ in range(200):
        sh = {ex: [lo + ((t - lo + random.uniform(0, hi - lo)) % (hi - lo)) for t in v] for ex, v in byex.items()}
        perm.append(burst(sh))
    out["recordings"] = dict(minutes=round(mins), requotes=len(rq), per_hour=round(len(rq) / mins * 60, 1), markets=len(byex),
                             sec_of_min_chi2_5df=round(chi, 1), rayleigh_z=round(rayl_z, 2), rayleigh_p=round(math.exp(-rayl_z), 3),
                             interarrival_cv_median=round(sorted(cvs)[len(cvs) // 2], 2) if cvs else None, cv_markets=len(cvs),
                             burst_share_2s=round(obs, 3), burst_perm_mean=round(sum(perm) / len(perm), 3),
                             burst_perm_p=round(sum(p >= obs for p in perm) / len(perm), 3),
                             size_ticks=collections.Counter(abs(e["dbid"]) for e in rq).most_common(4))
    # two-day panel from 35-s quotes table (MM legs): requote = bid and ask both shift the same way, no public print between
    ev = []
    for ex, rows in book.items():
        q = [r for r in rows]
        for a, b in zip(q, q[1:]):
            if b[0] - a[0] > 90:
                continue
            db_, da = round((b[1] - a[1]) / TICK), round((b[2] - a[2]) / TICK)
            if not db_ or not da or (db_ > 0) != (da > 0):
                continue
            net, vol = flow(ex, a[0] - 0.5, b[0] + 0.5)
            if vol:
                continue
            ev.append((b[0], ex, 1 if db_ > 0 else -1, a))
    rows = {"fair_gap": [], "dmm_inventory_30m": [], "last_requote": []}
    last = {}
    for t, ex, s, a in sorted(ev):
        part = "train" if t < SPLIT else "test"
        m0 = (a[1] + a[2]) / 2
        f = fair_at(ex, a[0])
        if f is not None and abs(f - m0) >= 2 * TICK:
            rows["fair_gap"].append((part, (1 if f > m0 else -1) == s))
            A_ROWS.append((t, 1 if (1 if f > m0 else -1) == s else -1))
        net, vol = flow(ex, t - 1800, t - 60)
        if vol >= 1000 and net:
            rows["dmm_inventory_30m"].append((part, (1 if net > 0 else -1) == s))  # DMM sold into buys -> short -> skews up
        if ex in last and t - last[ex] [0] < 3600:
            rows["last_requote"].append((part, last[ex][1] == s))
        last[ex] = (t, s)
    pan = dict(requotes=len(ev), per_day=round(len(ev) / (DAYS_TR + DAYS_TE), 1))
    for k, v in rows.items():
        for part in ("train", "test"):
            x = [h for p, h in v if p == part]
            pan[f"{k}_{part}"] = dict(n=len(x), hit=round(sum(x) / len(x), 3) if x else None, z=binom_z(sum(x), len(x)))
    # hazard: does traded volume in the previous 5 min raise the chance of a requote in the next 60 s?
    haz = collections.defaultdict(lambda: [0, 0])
    evk = collections.defaultdict(list)
    for t, ex, s, a in ev:
        evk[ex].append(t)
    for ex in evk:
        evk[ex].sort()
        for t in range(int(min(bkt[ex])) + 300, int(max(bkt[ex])) - 60, 60):
            _, vol = flow(ex, t - 300, t)
            b = 0 if vol == 0 else 1 if vol < 2000 else 2 if vol < 10000 else 3
            i = bisect.bisect_left(evk[ex], t)
            haz[b][0] += 1; haz[b][1] += i < len(evk[ex]) and evk[ex][i] < t + 60
    pan["hazard_by_prior5m_volume"] = {["0", "<2k", "2-10k", ">=10k"][b]: dict(minutes=v[0], p_requote_60s=round(v[1] / v[0], 4)) for b, v in sorted(haz.items())}
    out["panel"] = pan
    R["A_dmm_requotes"] = out


# ---------------- B. Large-taker footprints ----------------
def test_b(Q=5000, W=60):
    evs = []
    for ex, rows in tape.items():
        lastev = -1e18; i0 = 0
        for j, r in enumerate(rows):
            while rows[i0][0] < r[0] - W:
                i0 += 1
            for side in (1, -1):
                v = sum(x[2] for x in rows[i0:j + 1] if x[3] == side)
                if v >= Q and r[0] - lastev > 1800:
                    t0 = rows[i0][0]
                    pre = quote_at(ex, t0 - 1, stale=180)
                    post = quote_at(ex, r[0] + 5, stale=180, after=True)
                    if pre and post:
                        evs.append(dict(ex=ex, t=r[0], s=side, vol=v, pre=pre, post=post))
                        lastev = r[0]
    res = {}
    paths = collections.defaultdict(list)
    for e in evs:
        ex, s, t = e["ex"], e["s"], e["t"]
        mpre, mpost = (e["pre"][1] + e["pre"][2]) / 2, (e["post"][1] + e["post"][2]) / 2
        e["impact"] = s * (mpost - mpre) / TICK
        sp0 = (e["post"][2] - e["post"][1]) / TICK
        for h in (5, 30, 120):
            qh = quote_at(ex, t + h * 60, stale=300)
            if not qh:
                continue
            mh = (qh[1] + qh[2]) / 2
            c = s * (mh - mpost) / TICK
            cost = (sp0 + (qh[2] - qh[1]) / TICK) / 2
            e[f"cont{h}"] = c; e[f"follow{h}"] = c - cost; e[f"fade{h}"] = -c - cost
    for part in ("train", "test"):
        sel = [e for e in evs if (e["t"] < SPLIT) == (part == "train")]
        d = dict(events=len(sel), impact_ticks=ctstat([(e["impact"], e["ex"]) for e in sel]))
        for h in (5, 30, 120):
            for k in ("cont", "follow", "fade"):
                d[f"{k}{h}"] = ctstat([(e[f"{k}{h}"], e["ex"]) for e in sel if f"{k}{h}" in e])
        big = [e for e in sel if e["impact"] >= 2]
        d["impact>=2_fade30"] = ctstat([(e["fade30"], e["ex"]) for e in big if "fade30" in e])
        d["impact>=2_cont30"] = ctstat([(e["cont30"], e["ex"]) for e in big if "cont30" in e])
        res[part] = d
    res["threshold"] = f">= {Q} shares one side in {W} s, 30-min cooldown per market"
    R["B_large_taker"] = res
    return evs


# ---------------- C. Time of day: passive markout, spread, volume ----------------
def test_c(h=30):
    rows = []
    for ex, v in tape.items():
        for t, p, q, s in v:
            m = mid_at(ex, t + h * 60, stale=300)
            if m is None:
                continue
            rows.append((t, ex, q, s * (m - p) / TICK * -1, int(((t + ET) % 86400) // 3600)))  # passive gain per share
    prof = {}
    for part in ("train", "test"):
        by = collections.defaultdict(list)
        for t, ex, q, g, hr in rows:
            if (t < SPLIT) == (part == "train"):
                by[hr // 3].append((g, ex))
        prof[part] = {f"{b*3:02d}-{b*3+3:02d} ET": ctstat(v) for b, v in sorted(by.items())}
    keys = sorted(set(prof["train"]) & set(prof["test"]))
    a = [prof["train"][k]["mean"] for k in keys]; b = [prof["test"][k]["mean"] for k in keys]

    def rank(x):
        o = sorted(range(len(x)), key=lambda i: x[i]); r = [0] * len(x)
        for i, j in enumerate(o):
            r[j] = i
        return r
    ra, rb = rank(a), rank(b)
    rho = 1 - 6 * sum((x - y) ** 2 for x, y in zip(ra, rb)) / (len(keys) * (len(keys) ** 2 - 1))
    perm = []
    for _ in range(5000):
        rp = rb[:]; random.shuffle(rp)
        perm.append(1 - 6 * sum((x - y) ** 2 for x, y in zip(ra, rp)) / (len(keys) * (len(keys) ** 2 - 1)))
    # rule: quote only in the train-best half of the buckets; test-period passive gain inside vs outside those buckets
    good = set(sorted(keys, key=lambda k: -prof["train"][k]["mean"])[: len(keys) // 2])
    inside = [(g, ex) for t, ex, q, g, hr in rows if t >= SPLIT and f"{hr//3*3:02d}-{hr//3*3+3:02d} ET" in good]
    outside = [(g, ex) for t, ex, q, g, hr in rows if t >= SPLIT and f"{hr//3*3:02d}-{hr//3*3+3:02d} ET" not in good]
    ci, co = ctstat(inside), ctstat(outside)
    # our own MM fills by bucket (markout at 30 min, per share)
    mm = collections.defaultdict(list)
    for ex, fa, side, price, q, purpose in db.execute("select exchange_id, filled_at, side, price, abs(quantity), purpose from fills where purpose in ('mm','tilt','inside')"):
        t = iso(fa); m = mid_at(ex, t + h * 60, stale=300)
        if m is None or price is None:
            continue
        sgn = 1 if side == "yes" else -1
        mm[int(((t + ET) % 86400) // 3600) // 3].append((sgn * (m - price) / TICK, ex))
    R["C_time_of_day"] = dict(horizon_min=h, profile=prof, spearman_train_vs_test=round(rho, 2),
                              perm_p_one_sided=round(sum(p >= rho for p in perm) / len(perm), 3), train_best_buckets=sorted(good),
                              test_in_best=ci, test_outside=co,
                              test_diff=round(ci["mean"] - co["mean"], 3), test_diff_t=round((ci["mean"] - co["mean"]) / math.sqrt(ci["se"] ** 2 + co["se"] ** 2), 2),
                              our_fills_by_bucket={f"{b*3:02d}-{b*3+3:02d} ET": ctstat(v) for b, v in sorted(mm.items())})
    return rows, good


# ---------------- D. Queue age of our resting orders ----------------
def test_d():
    rows = []
    for ex, fa, side, price, q, purpose, placed in db.execute(
            "select f.exchange_id, f.filled_at, f.side, f.price, abs(f.quantity), f.purpose, o.placed_at from fills f join orders o on o.order_id=f.order_id "
            "where f.purpose in ('mm','tilt','inside')"):
        t = iso(fa); age = t - placed
        if age < 0 or price is None:
            continue
        sgn = 1 if side == "yes" else -1
        r = dict(t=t, ex=ex, age=age, q=q, purpose=purpose)
        for h in (5, 30):
            m = mid_at(ex, t + h * 60, stale=300)
            if m is not None:
                r[f"mk{h}"] = sgn * (m - price) / TICK
        rows.append(r)
    edges = [(0, 30), (30, 120), (120, 600), (600, 1e9)]
    res = {}
    for part in ("train", "test", "all"):
        sel = [r for r in rows if part == "all" or (r["t"] < SPLIT) == (part == "train")]
        res[part] = {f"{a}-{b if b < 1e9 else 'inf'}s": dict(mk5=ctstat([(r["mk5"], r["ex"]) for r in sel if a <= r["age"] < b and "mk5" in r]),
                                                             mk30=ctstat([(r["mk30"], r["ex"]) for r in sel if a <= r["age"] < b and "mk30" in r]),
                                                             shares=int(sum(r["q"] for r in sel if a <= r["age"] < b)))
                     for a, b in edges}
        old = [r["mk30"] for r in sel if r["age"] >= 120 and "mk30" in r]
        young = [r["mk30"] for r in sel if r["age"] < 120 and "mk30" in r]
        if old and young:
            o, y = ctstat([(x, 0) for x in old]), ctstat([(x, 0) for x in young])
            res[part]["old_minus_young_mk30"] = round(o["mean"] - y["mean"], 3)
            # permutation on the age label
            pool = old + young; obs = o["mean"] - y["mean"]; cnt = 0
            for _ in range(2000):
                random.shuffle(pool)
                d = sum(pool[:len(old)]) / len(old) - sum(pool[len(old):]) / len(young)
                cnt += abs(d) >= abs(obs)
            res[part]["perm_p_two_sided"] = round(cnt / 2000, 3)
    res["fills"] = len(rows); res["median_age_s"] = round(sorted(r["age"] for r in rows)[len(rows) // 2])
    R["D_queue_age"] = res
    return rows


# ---------------- E. Cross-market: same-state pairs and the national factor ----------------
def test_e(step=300, H=1800):
    dlegs = {ex: v for ex, v in leg_race.items() if v[1] == "D" and ex in book}
    lo = max(min(bkt[ex]) for ex in dlegs if len(bkt[ex]) > 100); hi = T1
    grid = list(range(int(datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()), int(hi) - H, step))
    mids = {ex: [mid_at(ex, t, stale=600) for t in grid] for ex in dlegs}
    k = H // step
    state = collections.defaultdict(list)
    for ex, (race, party, office) in dlegs.items():
        state[race.rsplit(" ", 1)[0]].append(ex)
    pairs = [(a, b) for v in state.values() for a in v for b in v if a != b and leg_race[a][0] != leg_race[b][0]]
    rows = []
    for a, b in pairs:
        ma, mb = mids[a], mids[b]
        for i in range(k, len(grid) - k, k):
            if None in (ma[i - k], ma[i], mb[i - k], mb[i], mb[i + k]):
                continue
            x = (ma[i] - ma[i - k]) / TICK; own = (mb[i] - mb[i - k]) / TICK; y = (mb[i + k] - mb[i]) / TICK
            if abs(x) >= 2 and abs(own) < 1:  # leader moved 2+ ticks, follower did not
                rows.append(("train" if grid[i] < SPLIT else "test", (1 if x > 0 else -1) * y, leg_race[a][0] + "|" + leg_race[b][0], grid[i]))
    # national factor: mean 30-min change of all other D legs vs this leg's next change
    nat = []
    for i in range(k, len(grid) - k, k):
        ch = {ex: (mids[ex][i] - mids[ex][i - k]) / TICK for ex in dlegs if mids[ex][i] is not None and mids[ex][i - k] is not None}
        if len(ch) < 50:
            continue
        tot = sum(ch.values())
        for ex, c in ch.items():
            f = (tot - c) / (len(ch) - 1)
            if abs(f) >= 0.25 and mids[ex][i + k] is not None:
                nat.append(("train" if grid[i] < SPLIT else "test", (1 if f > 0 else -1) * (mids[ex][i + k] - mids[ex][i]) / TICK, ex))
    # longer history: 5-min candle closes (last trade, forward-filled), 6-21 Oct; train 6-14 Oct, test 15-21 Oct
    cd = json.load(open(os.path.join(ROOT, "data", "candles_5m.json")))
    g0, g1 = int(datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp()), int(datetime(2026, 10, 21, 22, tzinfo=timezone.utc).timestamp())
    csplit = datetime(2026, 10, 15, tzinfo=timezone.utc).timestamp()
    cgrid = list(range(g0, g1, step)); cm = {}
    for ex in dlegs:
        cs = sorted((iso(c["time"]), c["close"]) for c in (cd.get(ex) or {}).get("candles", []))
        if len(cs) < 50:
            continue
        ts = [c[0] for c in cs]
        cm[ex] = [cs[i][1] if (i := bisect.bisect_right(ts, t) - 1) >= 0 and t - ts[i] < 86400 else None for t in cgrid]
    crow = []
    for a, b in pairs:
        if a not in cm or b not in cm:
            continue
        ma, mb = cm[a], cm[b]
        for i in range(k, len(cgrid) - k, k):
            if None in (ma[i - k], ma[i], mb[i - k], mb[i], mb[i + k]):
                continue
            x = (ma[i] - ma[i - k]) / TICK; own = (mb[i] - mb[i - k]) / TICK; y = (mb[i + k] - mb[i]) / TICK
            if abs(x) >= 2 and abs(own) < 1:
                crow.append(("train" if cgrid[i] < csplit else "test", (1 if x > 0 else -1) * y, leg_race[a][0] + "|" + leg_race[b][0], cgrid[i]))
    cnat = []
    for i in range(k, len(cgrid) - k, k):
        ch = {ex: (cm[ex][i] - cm[ex][i - k]) / TICK for ex in cm if cm[ex][i] is not None and cm[ex][i - k] is not None}
        if len(ch) < 30:
            continue
        tot = sum(ch.values())
        for ex, c in ch.items():
            f = (tot - c) / (len(ch) - 1)
            if abs(f) >= 0.25 and cm[ex][i + k] is not None:
                cnat.append(("train" if cgrid[i] < csplit else "test", (1 if f > 0 else -1) * (cm[ex][i + k] - cm[ex][i]) / TICK, ex))
    res = {"same_state_pairs": len(pairs), "chamber_market_on_SIG": False}
    for part in ("train", "test"):
        res[f"candles_pair_follow_{part}"] = ctstat([(y, c) for p, y, c, t in crow if p == part])
        res[f"candles_national_follow_{part}"] = ctstat([(y, c) for p, y, c in cnat if p == part])
    res["candles_split"] = "train 6-14 Oct, test 15-21 Oct"
    R["E_candles_rows"] = len(crow)
    E_ROWS[:] = crow
    for part in ("train", "test"):
        res[f"pair_follow_{part}"] = ctstat([(y, c) for p, y, c, t in rows if p == part])
        res[f"national_follow_{part}"] = ctstat([(y, c) for p, y, c in nat if p == part])
    R["E_cross_market"] = res
    return [(t, y) for p, y, c, t in rows]


# ---------------- figure ----------------
def figure(bevs, crows, good, drows, erows):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    plt.rcParams.update({"font.size": 10, "axes.titlesize": 11, "axes.labelsize": 10, "xtick.labelsize": 9.5, "ytick.labelsize": 9.5})
    fig = plt.figure(figsize=(11, 16.5))
    gs = fig.add_gridspec(6, 1, height_ratios=[0.8, 1, 1, 1, 0.8, 0.55], hspace=0.42)
    axa = fig.add_subplot(gs[0])
    axes = [fig.add_subplot(gs[1], sharex=axa)]
    axes += [fig.add_subplot(gs[i], sharex=axa) for i in (2, 3, 4)]
    split_dt = datetime.fromtimestamp(SPLIT, timezone.utc)

    def cum(ax, pts, col, label, scale=1.0):
        pts = sorted(pts)
        if not pts:
            return
        for part, ls in (("train", ":"), ("test", "-")):
            sel = [(t, v) for t, v in pts if (t < SPLIT) == (part == "train")]
            if not sel:
                continue
            xs, ys, lo, hi = [], [], [], []
            s = 0.0; ss = 0.0
            for j, (t, v) in enumerate(sel, 1):
                s += v * scale; ss += (v * scale) ** 2
                xs.append(datetime.fromtimestamp(t, timezone.utc)); ys.append(s)
                sd = math.sqrt(max(ss / j - (s / j) ** 2, 0) * j)
                lo.append(s - 1.96 * sd); hi.append(s + 1.96 * sd)
            ax.plot(xs, ys, ls=ls, color=col, lw=1.8, label=label if part == "test" else None)
            ax.fill_between(xs, lo, hi, color=col, alpha=0.10)
        ax.axvline(split_dt, color="0.5", lw=0.8)
        ax.axhline(0, color="0.6", lw=0.6)

    cum(axa, A_ROWS, "#34495e", "DMM requotes toward the Polymarket/Kalshi fair (+1) minus away from it (-1)")
    axa.set_title("A. DMM requotes (both sides shift, no print): direction leans to the outside fair, but each is 1 tick")
    axa.set_ylabel("cumulative\ntoward - away")
    cum(axes[0], [(e["t"], e["fade30"]) for e in bevs if "fade30" in e], "#c0392b", "fade the big taker (sell after buys), exit 30 min")
    cum(axes[0], [(e["t"], e["follow30"]) for e in bevs if "follow30" in e], "#2471a3", "follow the big taker, exit 30 min")
    axes[0].set_title("B. After a big taker (5k+ shares in 60 s): neither fading nor following pays the spread")
    axes[0].set_ylabel("cumulative ticks\nper share, summed")
    cum(axes[1], [(t, g) for t, ex, q, g, hr in crows if f"{int(((t + ET) % 86400) // 3600)//3*3:02d}-{int(((t + ET) % 86400) // 3600)//3*3+3:02d} ET" in good], "#27ae60",
        "resting side of public prints, in the 4 best training time slots", 1 / 100)
    cum(axes[1], [(t, g) for t, ex, q, g, hr in crows if f"{int(((t + ET) % 86400) // 3600)//3*3:02d}-{int(((t + ET) % 86400) // 3600)//3*3+3:02d} ET" not in good], "#7f8c8d",
        "resting side of public prints, in the other 4 slots", 1 / 100)
    axes[1].set_title("C. Time of day: does the resting side earn more in some 3-hour slots?")
    axes[1].set_ylabel("cumulative ticks per\nshare / 100, summed")
    cum(axes[2], [(r["t"], r["mk30"]) for r in drows if "mk30" in r and r["age"] >= 120], "#8e44ad", "our fills from orders resting 2+ min")
    cum(axes[2], [(r["t"], r["mk30"]) for r in drows if "mk30" in r and r["age"] < 120], "#e67e22", "our fills from orders resting under 2 min")
    axes[2].set_title("D. Queue age: are our old resting orders picked off more than fresh ones?")
    axes[2].set_ylabel("cumulative 30-min\nmarkout, ticks/share")
    pts = [(t, y) for t, y in erows]
    cum(axes[3], pts, "#16a085", "same-state other race, after its partner moved 2+ ticks (signed)")
    axes[3].set_title("E. Same-state races: does the other race follow within 30 min? (here n 27; 6-21 Oct candles n 728: +0.02 / 0.00 tick)")
    axes[3].set_ylabel("cumulative ticks\nper share, summed")
    for ax in [axa] + axes:
        ax.legend(loc="lower left" if ax is axes[0] else "upper left", fontsize=8.5, frameon=False)
        ax.grid(alpha=0.25)
    axes[3].xaxis.set_major_formatter(mdates.DateFormatter("%d Oct %H:%M"))
    axes[3].set_xlabel("time (UTC); grey line = split: dotted = training (before 23 Oct 00:00 UTC), solid = test")
    fig.suptitle("HFT tactics round 2: none of the five new candidates beats its cost on SIG", fontsize=13, fontweight="bold", y=0.905)
    gax = fig.add_subplot(gs[5]); gax.axis("off")
    gax.text(0.01, 0.97, "Glossary", ha="left", va="top", fontsize=10, fontweight="bold", transform=gax.transAxes)
    txt = ("tick = 0.005 (half a cent). Lines go up when the strategy makes money; a flat or falling line means no edge.\n"
           "Big taker: one side's public aggressive volume reaches 5,000 shares within 60 s in one market. Fade = trade against it; "
           "follow = trade with it.\nBoth pay the bid-ask spread (entry and exit at the touch).\n"
           "Resting side: the passive counterparty of a public print (the DMM, us, other ladders); markout = its gain 30 min later vs the mid.\n"
           "Queue age: seconds between placing our order and its fill.  Band = 95% range of the running sum.\n"
           "DMM requote: both best bid and best ask move the same way between two snapshots with no public print; a taker who\nguesses a 1-tick requote right still pays a spread of 1+ tick, so it nets <= 0.")
    gax.text(0.01, 0.80, txt, ha="left", va="top", fontsize=9, transform=gax.transAxes, linespacing=1.5,
             bbox=dict(boxstyle="round", fc="#f7f7f7", ec="0.7"))
    fig.savefig(FIG, dpi=120, facecolor="white", bbox_inches="tight")


if __name__ == "__main__":
    test_a()
    bevs = test_b()
    crows, good = test_c()
    drows = test_d()
    erows = test_e()
    R["days"] = dict(train=round(DAYS_TR, 2), test=round(DAYS_TE, 2))
    json.dump(R, open(os.path.join(OUT, "results.json"), "w"), indent=1, default=str)
    figure(bevs, crows, good, drows, erows)
    print(json.dumps(R, indent=1, default=str))
