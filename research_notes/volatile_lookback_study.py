"""1-day lookback on volatile SIG moves (22 Oct 10:30 - 23 Oct 10:30 UTC), with the earlier local history as out of sample.

Read-only: opens data/sigbot.sqlite with mode=ro, reads logs and JSON in data/, writes only data/research_volatile_lookback/.
No venue calls. Reproduce: python3 research_notes/volatile_lookback_study.py
One tick = 0.005. 'dir' = +1 for a YES-price rise. P&L in ticks per share unless stated.
"""
import bisect
import collections
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(DATA, "research_volatile_lookback")
os.makedirs(OUT, exist_ok=True)
T = 0.005
W0 = datetime(2026, 10, 22, 10, 30, tzinfo=timezone.utc).timestamp()   # window start (11:30 BST)
W1 = datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc).timestamp()   # window end
CAP_NOTIONAL = 600.0
PARTISAN_CAP = 5000.0


def iso(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, tail = s.split(".", 1)
        digits = "".join(ch for ch in tail if ch.isdigit())
        tz = tail[len(digits):]
        s = f"{head}.{digits[:6]}{tz}"
    return datetime.fromisoformat(s).timestamp()


def fmt(ts, tz="utc"):
    if ts is None:
        return "-"
    if tz == "bst":
        return datetime.fromtimestamp(ts, timezone(timedelta(hours=1))).strftime("%d %H:%M")
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %H:%M")


def tstat(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    sd = statistics.pstdev(xs)
    return None if sd == 0 else statistics.mean(xs) / (sd / math.sqrt(len(xs) - 1))


def clustered_t(rows, key, cluster="race"):
    """mean and t clustered by race (sum of residuals per cluster)."""
    xs = [(r[cluster], r[key]) for r in rows if r.get(key) is not None]
    if len(xs) < 2:
        return (statistics.mean([x for _, x in xs]) if xs else None), None
    m = statistics.mean([x for _, x in xs])
    g = collections.defaultdict(float)
    for c, x in xs:
        g[c] += x - m
    G = len(g)
    if G < 2:
        return m, None
    var = sum(v * v for v in g.values()) / (len(xs) ** 2) * G / (G - 1)
    return m, (m / math.sqrt(var) if var > 0 else None)


# ----------------------------------------------------------------------------------------------- load
con = sqlite3.connect(f"file:{os.path.join(DATA, 'sigbot.sqlite')}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

EX = {}          # exchange -> (race, party)
for m in json.load(open(os.path.join(DATA, "markets.json"))):
    t = m["title"].replace("Will the ", "").replace(" Party win the ", "|").rstrip("?")
    party, race = t.split("|", 1)
    for x in m.get("exchanges") or []:
        if x.get("option") == "YES":
            EX[str(x["id"])] = (race, party[0])
RACE_LEGS = collections.defaultdict(list)
for ex, (race, party) in EX.items():
    RACE_LEGS[race].append(ex)

# SIG quotes: venue_fair (all legs, ~85 s) + quotes (MM-quoted legs, ~35 s, with touch sizes)
obs = collections.defaultdict(list)      # ex -> [(ts, bid, ask, bidq, askq)]
outside = collections.defaultdict(list)  # ex -> [(ts, pm, kalshi, fair)]
for r in con.execute("SELECT ts, race, party, pm, kalshi, fair, sig_bid, sig_ask, exchange_id FROM venue_fair ORDER BY ts"):
    ex = r["exchange_id"]
    if ex is None:
        continue
    if r["sig_bid"] is not None and r["sig_ask"] is not None and r["sig_ask"] > r["sig_bid"]:
        obs[ex].append((r["ts"], r["sig_bid"], r["sig_ask"], None, None))
    if r["fair"] is not None:
        outside[ex].append((r["ts"], r["pm"], r["kalshi"], r["fair"]))
for r in con.execute("SELECT ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty FROM quotes ORDER BY ts"):
    if r["best_bid"] is not None and r["best_ask"] is not None and r["best_ask"] > r["best_bid"]:
        obs[r["exchange_id"]].append((r["ts"], r["best_bid"], r["best_ask"], r["bid_qty"], r["ask_qty"]))
for ex in obs:
    obs[ex].sort()
OBS_TS = {ex: [o[0] for o in v] for ex, v in obs.items()}
OUT_TS = {ex: [o[0] for o in v] for ex, v in outside.items()}
T_START = min(v[0][0] for v in obs.values())
T_END = max(v[-1][0] for v in obs.values())

# tape: prints with aggressor
tape = collections.defaultdict(list)     # ex -> [(ts, px, qty, sgn)]
for r in con.execute("SELECT exchange_id, price, quantity, executed_at, aggressor FROM tape"):
    s = 1 if r["aggressor"] == "buy" else (-1 if r["aggressor"] == "sell" else 0)
    tape[r["exchange_id"]].append((iso(r["executed_at"]), r["price"], abs(r["quantity"]), s))
for ex in tape:
    tape[ex].sort()
TAPE_TS = {ex: [x[0] for x in v] for ex, v in tape.items()}

# our fills
fills = []
for r in con.execute("SELECT exchange_id, side, price, quantity, filled_at, purpose, level FROM fills WHERE filled_at >= '2026-10-21T22'"):
    fills.append(dict(ex=r["exchange_id"], ts=iso(r["filled_at"]), px=r["price"], q=r["quantity"], purpose=r["purpose"] or "unknown",
                      level=r["level"]))
fills.sort(key=lambda f: f["ts"])

# bot up-time: cycles
cyc = [r[0] for r in con.execute("SELECT ts FROM cycles WHERE ts > ? ORDER BY ts", (T_START - 3600,))]
BOT_GAPS = [(a, b) for a, b in zip(cyc, cyc[1:]) if b - a > 15 * 60]


def bot_down(t0, t1):
    return sum(max(0, min(b, t1) - max(a, t0)) for a, b in BOT_GAPS)


# netD from the log (local BST timestamps)
NETD = []
for line in open(os.path.join(DATA, "sigbot.log"), errors="ignore"):
    m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ .*netD=([+-]?\d+)", line)
    if m:
        t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone(timedelta(hours=1))).timestamp()
        NETD.append((t, float(m.group(2))))
NETD.sort()
NETD_TS = [x[0] for x in NETD]


def netd_at(t):
    i = bisect.bisect_right(NETD_TS, t) - 1
    return NETD[i][1] if i >= 0 else 0.0


# ----------------------------------------------------------------------------------------------- accessors
def quote_at(ex, t, stale=360):
    ts = OBS_TS.get(ex)
    if not ts:
        return None
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > stale:
        return None
    return obs[ex][i]


def mid_at(ex, t, stale=360, tape_fallback=True):
    q = quote_at(ex, t, stale)
    if q:
        return 0.5 * (q[1] + q[2])
    if tape_fallback:
        ts = TAPE_TS.get(ex) or []
        lo, hi = bisect.bisect_left(ts, t - 300), bisect.bisect_right(ts, t + 300)
        px = [tape[ex][k][1] for k in range(lo, hi)]
        if px:
            return statistics.median(px)
    return None


def depth_at(ex, t):
    """touch sizes from the quotes table (MM-quoted legs only)."""
    ts = OBS_TS.get(ex) or []
    i = bisect.bisect_right(ts, t) - 1
    while i >= 0 and t - ts[i] < 600:
        o = obs[ex][i]
        if o[3] is not None:
            return o[3], o[4]
        i -= 1
    return None, None


def fair_at(ex, t, stale=420):
    ts = OUT_TS.get(ex)
    if not ts:
        return None
    i = bisect.bisect_right(ts, t) - 1
    if i < 0 or t - ts[i] > stale:
        return None
    return outside[ex][i]


def tape_flow(ex, t0, t1):
    ts = TAPE_TS.get(ex) or []
    lo, hi = bisect.bisect_left(ts, t0), bisect.bisect_right(ts, t1)
    buy = sum(tape[ex][k][2] for k in range(lo, hi) if tape[ex][k][3] > 0)
    sell = sum(tape[ex][k][2] for k in range(lo, hi) if tape[ex][k][3] < 0)
    tot = sum(tape[ex][k][2] for k in range(lo, hi))
    return buy, sell, tot


# ----------------------------------------------------------------------------------------------- 1-min grid
GRID = [T_START + 60 * k for k in range(int((T_END - T_START) / 60) + 1)]


def grid_series(ex):
    out = []
    for t in GRID:
        q = quote_at(ex, t, stale=300)
        out.append(None if q is None else 0.5 * (q[1] + q[2]))
    return out


def detect(ex, mids, thr, window, max_ext=180, quiet=60):
    """In-flight detection: the first grid minute at which mid is >= thr ticks from the opposite extreme of the trailing
    `window` minutes. Start = that extreme; the move extends while it makes new extremes, until it gives back
    max(2 ticks, half the move) or makes no new extreme for `quiet` minutes or reaches max_ext minutes. End = the extreme."""
    evs = []
    n = len(mids)
    LO = [math.inf if v is None else v for v in mids]
    HI = [-math.inf if v is None else v for v in mids]
    i = 0
    while i < n:
        p = mids[i]
        if p is None or not (0.03 <= p <= 0.97):
            i += 1
            continue
        a0 = max(0, i - window)
        lo_v = min(LO[a0:i]) if i > a0 else math.inf
        hi_v = max(HI[a0:i]) if i > a0 else -math.inf
        d = 0
        if lo_v < math.inf and p - lo_v >= thr * T - 1e-9:
            d = 1
            s_k = max(k for k in range(a0, i) if LO[k] == lo_v)
        elif hi_v > -math.inf and hi_v - p >= thr * T - 1e-9:
            d = -1
            s_k = max(k for k in range(a0, i) if HI[k] == hi_v)
        if not d:
            i += 1
            continue
        p0 = mids[s_k]
        ext_k = i
        for k in range(i + 1, min(n, i + max_ext + 1)):
            v = mids[k]
            if v is None:
                continue
            if (v - mids[ext_k]) * d > 1e-9:
                ext_k = k
            give = (mids[ext_k] - v) * d
            if give >= max(2 * T, 0.5 * abs(mids[ext_k] - p0)) - 1e-9 or k - ext_k > quiet:
                break
        evs.append(dict(ex=ex, race=EX.get(ex, ("?", "?"))[0], party=EX.get(ex, ("?", "?"))[1], dir=d,
                        t_start=GRID[s_k], t_det=GRID[i], t_end=GRID[ext_k], p0=p0, p_det=p, p_end=mids[ext_k],
                        size=round(abs(mids[ext_k] - p0) / T, 1), det_size=round(abs(p - p0) / T, 1)))
        i = ext_k + 30
    return evs


def gap_moves(ex):
    """moves hidden inside a bot gap: mid before vs after the gap (quotes), >= 5 ticks."""
    evs = []
    for a, b in BOT_GAPS:
        if b < W0 - 3600 or a > W1:
            continue
        m0, m1 = mid_at(ex, a, 600, False), mid_at(ex, b + 120, 600, False)
        if m0 is None or m1 is None or not (0.03 <= m0 <= 0.97 or 0.03 <= m1 <= 0.97):
            continue
        if abs(m1 - m0) >= 5 * T - 1e-9:
            evs.append(dict(ex=ex, race=EX.get(ex, ("?", "?"))[0], party=EX.get(ex, ("?", "?"))[1], dir=1 if m1 > m0 else -1,
                            t_start=a, t_det=b + 120, t_end=b + 120, p0=m0, p_det=m1, p_end=m1, size=round(abs(m1 - m0) / T, 1),
                            det_size=round(abs(m1 - m0) / T, 1), in_gap=True))
    return evs


def enrich(e):
    ex, d = e["ex"], e["dir"]
    qs, qe = quote_at(ex, e["t_start"], 600), quote_at(ex, e["t_end"], 600)
    e["two_sided"] = bool(qs and qe and (qe[1] - qs[1]) * d >= 0.5 * e["size"] * T - 1e-9 and (qe[2] - qs[2]) * d >= 0.5 * e["size"] * T - 1e-9)
    e["spread_end"] = None if not qe else round((qe[2] - qe[1]) / T)
    for h in (15, 60, 240):
        m = mid_at(ex, e["t_end"] + 60 * h)
        e[f"ret{h}"] = None if m is None or e["size"] == 0 else round((e["p_end"] - m) * d / (e["size"] * T), 2)
        e[f"mid{h}"] = m
    # outside
    def f(t):
        x = fair_at(ex, t)
        return None if x is None else x[3]
    e["fair_m60"], e["fair_start"], e["fair_end"], e["fair_p60"], e["fair_p240"] = (f(e["t_start"] - 3600), f(e["t_start"]), f(e["t_end"]),
                                                                                     f(e["t_end"] + 3600), f(e["t_end"] + 14400))
    pre = None if e["fair_m60"] is None or e["fair_start"] is None else (e["fair_start"] - e["fair_m60"]) * d / 0.01
    dur = None if e["fair_start"] is None or e["fair_end"] is None else (e["fair_end"] - e["fair_start"]) * d / 0.01
    post = None if e["fair_end"] is None or e["fair_p240"] is None else (e["fair_p240"] - e["fair_end"]) * d / 0.01
    e["out_pre_pts"], e["out_dur_pts"], e["out_post_pts"] = [None if v is None else round(v, 2) for v in (pre, dur, post)]
    # lead: first time in [start-120min, end] the outside fair has moved >= 1 pt in the move's direction vs 30 min earlier
    lead = None
    t = e["t_start"] - 7200
    while t <= e["t_end"]:
        a, b = f(t - 1800), f(t)
        if a is not None and b is not None and (b - a) * d >= 0.01 - 1e-9:
            lead = round((e["t_start"] - t) / 60)
            break
        t += 120
    e["lead_min"] = lead
    if pre is not None and pre >= 1.0:
        e["cls"] = "outside-led"
    elif dur is not None and dur >= 1.0:
        e["cls"] = "concurrent"
    elif post is not None and post >= 1.5:
        e["cls"] = "outside-followed"
    elif e["fair_start"] is None:
        e["cls"] = "no outside"
    else:
        e["cls"] = "SIG-only"
    if e["fair_start"] is not None and e["fair_end"] is not None:
        e["gap_before"] = round((e["fair_start"] - e["p0"]) * d / T, 1)        # >0: fair was on the move's side
        e["gap_after"] = round((e["fair_end"] - e["p_end"]) * d / T, 1)
    else:
        e["gap_before"] = e["gap_after"] = None
    bq, aq = depth_at(ex, e["t_start"])
    e["touch_bid"], e["touch_ask"] = bq, aq
    buy, sell, tot = tape_flow(ex, e["t_start"], e["t_end"] + 60)
    e["tape_vol"] = round(tot)
    e["flow_with"] = None if buy + sell == 0 else round((buy if d > 0 else sell) / (buy + sell), 2)
    b0, s0, t0 = tape_flow(ex, e["t_start"] - 1800, e["t_start"])
    e["pre_flow_with"] = None if b0 + s0 == 0 else round((b0 if d > 0 else s0) / (b0 + s0), 2)
    e["pre_vol"] = round(t0)
    e["bot_down_min"] = round(bot_down(e["t_start"] - 600, e["t_end"] + 3600) / 60)
    e["netD"] = netd_at(e["t_start"])
    # our fills on this leg in [start-10m, end+60m]
    legs = set(RACE_LEGS.get(e["race"], [ex]))
    fl = [x for x in fills if x["ex"] in legs and e["t_start"] - 600 <= x["ts"] <= e["t_end"] + 3600]
    e["fills"] = []
    for x in fl:
        dx = d if x["ex"] == ex else -d
        m1, m4 = mid_at(x["ex"], x["ts"] + 3600), mid_at(x["ex"], x["ts"] + 14400)
        e["fills"].append(dict(purpose=x["purpose"], ts=x["ts"], q=x["q"], px=x["px"], adverse=x["q"] * dx < 0 and x["ts"] <= e["t_end"],
                               mo1=None if m1 is None else x["q"] * (m1 - x["px"]), mo4=None if m4 is None else x["q"] * (m4 - x["px"])))
    # vol gate (live rule): |mid - mid 15 min ago| >= 3 ticks -> hot. First hot minute.
    hot = None
    t = e["t_start"]
    while t <= e["t_end"]:
        a, b = mid_at(ex, t - 900, 360, False), mid_at(ex, t, 360, False)
        if a is not None and b is not None and abs(b - a) >= 3 * T - 1e-9:
            hot = t
            break
        t += 60
    e["gate_hot_at"] = hot
    return e


# ----------------------------------------------------------------------------------------------- rules
def qty_for(price_paid):
    """600 of cost per leg, and no more than 2,000 shares (about one DMM touch)."""
    return min(2000, int(CAP_NOTIONAL / max(price_paid, 0.02)))


def adds_d(ex, buy_yes):
    party = EX.get(ex, ("?", "?"))[1]
    return (party == "D" and buy_yes) or (party == "R" and not buy_yes)


def fade_trades(e, hold_h=4):
    """(b) fade at detection. taker: hit the touch against the move; passive: rest at the post-move far touch (offer after an up
    move), filled if within 60 min the book's opposite touch reaches it or a print trades one tick through it. Exit: taker at
    the touch at the first quote where the mid has given back half the detected move, else after hold_h."""
    ex, d = e["ex"], e["dir"]
    q = quote_at(ex, e["t_det"], 360)
    if q is None:
        return None
    bid, ask = q[1], q[2]
    half = e["p_det"] - d * 0.5 * abs(e["p_det"] - e["p0"])
    out = {}

    def exit_from(t_in):
        ts = OBS_TS[ex]
        i = bisect.bisect_right(ts, t_in)
        while i < len(ts) and ts[i] <= t_in + hold_h * 3600:
            o = obs[ex][i]
            m = 0.5 * (o[1] + o[2])
            if (m - half) * d <= 1e-9:
                return (o[2] if d > 0 else o[1]), ts[i]          # buy back at ask after fading an up move
            i += 1
        o = quote_at(ex, t_in + hold_h * 3600, 900)
        if o is None:
            return None, None
        return (o[2] if d > 0 else o[1]), t_in + hold_h * 3600

    # taker: sell at bid (up move) / buy at ask (down move)
    px_in = bid if d > 0 else ask
    px_out, t_out = exit_from(e["t_det"])
    if px_out is not None:
        out["taker"] = (px_in - px_out) * d / T
    # passive
    px_rest = ask if d > 0 else bid
    filled = None
    ts = OBS_TS[ex]
    i = bisect.bisect_right(ts, e["t_det"])
    while i < len(ts) and ts[i] <= e["t_det"] + 3600:
        o = obs[ex][i]
        if (d > 0 and o[1] >= px_rest - 1e-9) or (d < 0 and o[2] <= px_rest + 1e-9):
            filled = ts[i]
            break
        i += 1
    tts = TAPE_TS.get(ex) or []
    j = bisect.bisect_right(tts, e["t_det"])
    while j < len(tts) and tts[j] <= e["t_det"] + 3600 and (filled is None or tts[j] < filled):
        p = tape[ex][j][1]
        if (d > 0 and p >= px_rest + T - 1e-9) or (d < 0 and p <= px_rest - T + 1e-9):
            filled = tts[j]
            break
        j += 1
    out["passive_filled"] = filled is not None
    if filled is not None:
        px_out, _ = exit_from(filled)
        if px_out is not None:
            out["passive"] = (px_rest - px_out) * d / T
    # momentum (follow the SIG move at detection, taker both ways, hold 1h / 4h)
    for h in (1, 4):
        o = quote_at(ex, e["t_det"] + h * 3600, 900)
        if o is not None:
            out[f"momo{h}"] = ((o[1] if d > 0 else -o[2]) - (ask if d > 0 else -bid)) / T
    # caps
    buy_yes_fade = d < 0
    out["fade_adds_d"] = adds_d(ex, buy_yes_fade)
    out["qty"] = qty_for(px_in if buy_yes_fade else 1 - px_in)
    return out


def leg_mid_series(ex, t0, t1, step=120):
    t = t0
    while t <= t1:
        yield t, mid_at(ex, t, 360, False)
        t += step


def catchup_signals(t0, t1, pts=2.0, window=1800, sig_frac=1 / 3, step=120):
    """(a) outside fair (venue_fair.fair, PM/Kalshi) moves >= pts within `window`; SIG mid moved < sig_frac of it. One per race
    per hour. Uses the leg with the outside quote (D preferred)."""
    sigs = []
    last = {}
    for ex in sorted(outside):
        race, party = EX.get(ex, ("?", "?"))
        if party != "D" and any(EX.get(o, ("", ""))[1] == "D" and o in outside for o in RACE_LEGS[race]):
            continue
        t = t0
        while t <= t1:
            a, b = fair_at(ex, t - window), fair_at(ex, t)
            if a is None or b is None or b[1] is None or b[2] is None or abs(b[1] - b[2]) > 0.04:
                t += step
                continue
            dm = (b[3] - a[3]) / 0.01
            if abs(dm) >= pts and t - last.get(race, -1e9) >= 3600:
                ma, mb = mid_at(ex, t - window, 360, False), mid_at(ex, t, 360, False)
                if ma is not None and mb is not None and 0.03 <= mb <= 0.97:
                    sm = (mb - ma) / 0.01
                    if sm * math.copysign(1, dm) < sig_frac * abs(dm):
                        sigs.append(dict(ex=ex, race=race, party=party, t=t, dir=1 if dm > 0 else -1, out_pts=round(dm, 2), sig_pts=round(sm, 2),
                                         gap=round((b[3] - mb) / T, 1)))
                        last[race] = t
            t += step
    return sigs


def trade_from_signal(s, holds=(1, 4)):
    ex, d = s["ex"], s["dir"]
    q = quote_at(ex, s["t"], 360)
    if q is None:
        return None
    px_in = q[2] if d > 0 else q[1]
    res = dict(s)
    for h in holds:
        o = quote_at(ex, s["t"] + h * 3600, 900)
        res[f"tt{h}"] = None if o is None else ((o[1] - px_in) if d > 0 else (px_in - o[2])) / T
        m = mid_at(ex, s["t"] + h * 3600, 900, False)
        m0 = 0.5 * (q[1] + q[2])
        res[f"mm{h}"] = None if m is None else (m - m0) * d / T
    # did a >= 5 tick SIG move in the signal's direction start within 60 min?
    hit = None
    for t, m in leg_mid_series(ex, s["t"], s["t"] + 3600, 60):
        if m is None:
            continue
        m0 = 0.5 * (q[1] + q[2])
        if (m - m0) * d >= 5 * T - 1e-9:
            hit = round((t - s["t"]) / 60)
            break
    res["move_within_60"] = hit
    res["adds_d"] = adds_d(ex, d > 0)
    res["qty"] = qty_for(px_in if d > 0 else 1 - px_in)
    return res


def summarize(rows, key, label, days, qty_key="qty"):
    xs = [r for r in rows if r.get(key) is not None]
    if not xs:
        return dict(label=label, n=0)
    vals = [r[key] for r in xs]
    m, t = clustered_t(xs, key)
    sday = sum(r[key] * T * r.get(qty_key, 700) for r in xs) / days
    return dict(label=label, n=len(xs), hit=round(sum(v > 0 for v in vals) / len(vals), 2), mean=round(m, 2),
                t=None if t is None else round(t, 1), worst=round(min(vals), 1), s_day=round(sday))


def catchup_candles(pts=2.0, holds=(1, 4), since="2026-10-07T12:00:00Z", until="2026-10-21T18:00:00Z"):
    """Same catch-up rule on the 14-day history (set A of directional_impact.md): SIG = 5-min candle close (forward-filled),
    outside fair = mean of the Polymarket and Kalshi D-leg history (data/model_history.json), both present and within 4 pts.
    Signal: fair moved >= pts in 30 min, SIG moved < 1/3 of it; one per race per hour. Taker in and out: 1 tick round trip."""
    sys.path.insert(0, HERE)
    from sigbot.proposals import volatile_moves as VM      # read-only use of its loaders
    markets, candles, out = VM.load_markets(), VM.load_candles(), VM.load_outside()
    legs = collections.defaultdict(dict)
    for ex, (race, party, _) in markets.items():
        legs[race][party] = ex
    t0, t1 = iso(since), iso(until)
    rows = []
    for race, o in out.items():
        ex = legs.get(race, {}).get("D")
        if ex not in candles or not o.get("pm") or not o.get("kalshi"):
            continue
        c = candles[ex]

        def fair(t):
            a, b = VM.at(o["pm"], t), VM.at(o["kalshi"], t)
            if a is None or b is None or abs(a - b) > 0.04:
                return None
            return 0.5 * (a + b)
        last = -1e9
        t = t0
        while t <= t1:
            f0, f1 = fair(t - 1800), fair(t)
            if f0 is not None and f1 is not None and abs(f1 - f0) >= pts / 100 - 1e-9 and t - last >= 3600:
                s0, sp = VM._ff(c, t), VM._ff(c, t - 1800)
                d = 1 if f1 > f0 else -1
                if s0 is not None and sp is not None and 0.03 <= s0 <= 0.97 and (s0 - sp) * d < abs(f1 - f0) / 3:
                    last = t
                    r = dict(race=race, t=t, dir=d, day=fmt(t)[:2], qty=qty_for(s0 if d > 0 else 1 - s0))
                    for h in holds:
                        s1 = VM._ff(c, t + h * 3600)
                        r[f"mm{h}"] = None if s1 is None else (s1 - s0) * d / T
                        r[f"tt{h}"] = None if s1 is None else (s1 - s0) * d / T - 1.0
                    rows.append(r)
            t += 300
    return rows


def deep_ladder(t0, t1, k_ticks=8, tail=0.2, hold_h=4):
    """Ex-ante spike catcher: every hour, on legs with mid <= tail or >= 1-tail, rest a bid k ticks below and an offer k ticks
    above the mid for one hour. Filled if a print trades one tick through the price. Markout to the mid hold_h later, then
    a half-tick taker exit. Ticks per filled share."""
    rows = []
    for ex in obs:
        t = t0
        while t < t1:
            m0 = mid_at(ex, t, 300, False)
            if m0 is None or not (0.03 <= m0 <= tail or 1 - tail <= m0 <= 0.97):
                t += 3600
                continue
            for side in (1, -1):        # +1: offer above (we sell YES), -1: bid below (we buy YES)
                px = m0 + side * k_ticks * T
                ts = TAPE_TS.get(ex) or []
                lo, hi = bisect.bisect_left(ts, t), bisect.bisect_right(ts, t + 3600)
                fill = next((tape[ex][j][0] for j in range(lo, hi) if (tape[ex][j][1] - px) * side >= T - 1e-9), None)
                if fill is None:
                    continue
                m1 = mid_at(ex, fill + hold_h * 3600, 900)
                if m1 is None:
                    continue
                rows.append(dict(race=EX.get(ex, ("?", "?"))[0], ex=ex, t=fill, pnl=(px - m1) * side / T - 0.5,
                                 qty=qty_for(px if side < 0 else 1 - px)))
            t += 3600
    return rows


# ----------------------------------------------------------------------------------------------- main
def run():
    res = {}
    for pts in (1.5, 2.0, 3.0):
        rows = catchup_candles(pts)
        days = (iso("2026-10-21T18:00:00Z") - iso("2026-10-07T12:00:00Z")) / 86400
        res[f"catchup_A_{pts}"] = [summarize(rows, "tt1", f"A: catch-up >= {pts} pt, taker, +1 h", days),
                                   summarize(rows, "tt4", f"A: catch-up >= {pts} pt, taker, +4 h", days),
                                   summarize(rows, "mm4", f"A: catch-up >= {pts} pt, mid/mid +4 h", days)]
        byday = collections.defaultdict(list)
        for r in rows:
            if r.get("tt4") is not None:
                byday[r["day"]].append(r["tt4"])
        res[f"catchup_A_{pts}_byday_tt4"] = {k: (len(v), round(statistics.mean(v), 2)) for k, v in sorted(byday.items())}
    mids = {ex: grid_series(ex) for ex in obs}
    fast, slow = [], []
    for ex, ms in mids.items():
        fast += detect(ex, ms, 5, 60)
        slow += detect(ex, ms, 10, 240, max_ext=360, quiet=120)
        fast += gap_moves(ex)
    for e in fast + slow:
        enrich(e)

    def dedupe(evs):
        evs = sorted(evs, key=lambda e: (-(e["two_sided"]), -e["size"]))
        kept = []
        for e in evs:
            dup = None
            for k in kept:
                if k["race"] == e["race"] and e["t_start"] <= k["t_end"] + 1800 and k["t_start"] <= e["t_end"] + 1800:
                    dup = k
                    break
            if dup is None:
                e["mates"] = []
                kept.append(e)
            else:
                dup["mates"].append(f"{e['party']} {e['size']:+.0f}" if False else f"{e['party']}{'+' if e['dir'] > 0 else '-'}{e['size']:.0f}")
        return sorted(kept, key=lambda e: e["t_start"])

    fast_d, slow_d = dedupe(fast), dedupe(slow)
    # slow moves not already covered by a fast one
    slow_only = [s for s in slow_d if not any(f["race"] == s["race"] and f["t_start"] <= s["t_end"] and s["t_start"] <= f["t_end"] for f in fast_d)]

    def inwin(e):
        return W0 <= e["t_start"] < W1

    def prewin(e):
        return e["t_start"] < W0

    win_fast = [e for e in fast_d if inwin(e)]
    pre_fast = [e for e in fast_d if prewin(e)]
    res["counts"] = dict(window_fast=len(win_fast), window_fast_two_sided=sum(e["two_sided"] for e in win_fast),
                         window_slow_only=len([e for e in slow_only if inwin(e)]), pre_fast=len(pre_fast),
                         pre_days=round((W0 - T_START) / 86400, 2), bot_gaps=[(fmt(a), round((b - a) / 60)) for a, b in BOT_GAPS])

    # rules per move
    days_win, days_pre = (W1 - W0) / 86400, (W0 - T_START) / 86400
    for e in fast_d:
        if e.get("in_gap"):
            continue
        e["rule"] = fade_trades(e)
    fade_rows = {}
    for lab, evs, days in (("window", win_fast, days_win), ("pre", pre_fast, days_pre)):
        rows = []
        for e in evs:
            r = e.get("rule")
            if not r or not e["two_sided"]:
                continue
            row = dict(race=e["race"], qty=r["qty"], taker=r.get("taker"), passive=r.get("passive"), momo1=r.get("momo1"), momo4=r.get("momo4"),
                       feasible=not (r["fade_adds_d"] and e["netD"] >= PARTISAN_CAP - 200), cls=e["cls"],
                       widen=None if e["gap_after"] is None or e["gap_before"] is None else (abs(e["gap_after"]) > abs(e["gap_before"])))
            rows.append(row)
        fade_rows[lab] = rows
        out = []
        out.append(summarize(rows, "taker", "fade taker, exit half-retrace / 4 h", days))
        out.append(summarize(rows, "passive", "fade passive (filled only), exit half-retrace / 4 h", days))
        out.append(summarize([r for r in rows if r["widen"]], "passive", "fade passive, gap to fair widened", days))
        out.append(summarize([r for r in rows if r["widen"] is False], "passive", "fade passive, gap to fair closed", days))
        out.append(summarize([r for r in rows if r["feasible"]], "passive", "fade passive, partisan-feasible", days))
        out.append(summarize(rows, "momo1", "momentum taker at detection, +1 h", days))
        out.append(summarize(rows, "momo4", "momentum taker at detection, +4 h", days))
        res[f"fade_{lab}"] = out
        res[f"fade_{lab}_fillrate"] = round(sum(1 for e in evs if e.get("rule") and e["rule"].get("passive_filled")) / max(1, sum(1 for e in evs if e.get("rule"))), 2)

    # (a) catch-up
    cu = {}
    for lab, a, b, days in (("window", W0, W1, days_win), ("pre", T_START + 1800, W0, days_pre)):
        for pts in (1.5, 2.0, 3.0):
            sigs = catchup_signals(a, b, pts=pts)
            rows = [x for x in (trade_from_signal(s) for s in sigs) if x]
            for r in rows:
                r["feasible"] = not (r["adds_d"] and netd_at(r["t"]) >= PARTISAN_CAP - 200)
            cu[(lab, pts)] = rows
            res[f"catchup_{lab}_{pts}"] = [summarize(rows, "tt1", f"catch-up >= {pts} pt, taker, +1 h", days),
                                           summarize(rows, "tt4", f"catch-up >= {pts} pt, taker, +4 h", days),
                                           summarize(rows, "mm4", f"catch-up >= {pts} pt, mid/mid +4 h (gross)", days),
                                           summarize([r for r in rows if r["feasible"]], "tt4", f"catch-up >= {pts} pt, partisan-feasible, taker +4 h", days),
                                           dict(label="signals that preceded a >=5-tick SIG move within 60 min",
                                                n=len(rows), hits=sum(r["move_within_60"] is not None for r in rows))]
    # pooled out-of-sample view of the catch-up rule: A (candles) + B-pre + B-window, and without the 3 best races
    for pts in (2.0, 3.0):
        pool = [dict(r, src="A") for r in catchup_candles(pts)] + [dict(r, src="Bpre") for r in cu[("pre", pts)]] + [dict(r, src="Bwin") for r in cu[("window", pts)]]
        pool = [r for r in pool if r.get("tt4") is not None]
        by = collections.Counter()
        for r in pool:
            by[r["race"]] += r["tt4"]
        top = {k for k, _ in by.most_common(3)}
        days_all = (iso("2026-10-21T18:00:00Z") - iso("2026-10-07T12:00:00Z")) / 86400 + days_pre + days_win
        res[f"catchup_pooled_{pts}"] = [summarize(pool, "tt4", f"pooled A+B, >= {pts} pt, taker +4 h", days_all),
                                        summarize([r for r in pool if r["race"] not in top], "tt4", f"pooled, without top 3 races {sorted(top)}", days_all),
                                        summarize([r for r in pool if r["src"] != "A"], "tt4", "B only (live quotes, 1.4 d)", days_pre + days_win)]
    for k in (6, 8, 12):
        for lab, a_, b_, days in (("window", W0, W1 - 4 * 3600, days_win - 4 / 24), ("pre", T_START + 1800, W0, days_pre)):
            rows = deep_ladder(a_, b_, k)
            res[f"deep_ladder_{k}_{lab}"] = summarize(rows, "pnl", f"deep tail ladder +-{k} ticks, {lab}", days)
    res["catchup_rows_window_2"] = [dict((k, (fmt(v) if k == "t" else (round(v, 2) if isinstance(v, float) else v))) for k, v in r.items()) for r in cu[("window", 2.0)]]

    # (c) MM fills during moves and counterfactual pulls
    mmf = [x for x in fills if x["purpose"] in ("mm", "inside")]
    for x in mmf:
        for h in (1, 4):
            m = mid_at(x["ex"], x["ts"] + h * 3600)
            x[f"mo{h}"] = None if m is None else x["q"] * (m - x["px"]) / T     # ticks x shares (S = x T)
    def mm_rule(ev_list, t0, t1, mode):
        """blocked fills: fills in [t_det, t_end + 60 min] on the moved leg that trade against the move (mode 'after_det'),
        or in [t_start, t_end] (oracle, 'whole_move')."""
        blocked = []
        for x in mmf:
            if not (t0 <= x["ts"] < t1):
                continue
            for e in ev_list:
                if e["ex"] != x["ex"] and not (e["race"] == EX.get(x["ex"], ("", ""))[0]):
                    continue
                # direction on this leg: same leg d, other leg -d
                d = e["dir"] if e["ex"] == x["ex"] else -e["dir"]
                lo, hi = (e["t_det"], e["t_end"] + 3600) if mode == "after_det" else (e["t_start"] - 300, e["t_end"])
                if lo <= x["ts"] <= hi and x["q"] * d < 0:
                    blocked.append(x)
                    break
        return blocked

    mmres = {}
    for lab, evs, a, b in (("window", win_fast, W0, W1), ("pre", pre_fast, T_START, W0)):
        allf = [x for x in mmf if a <= x["ts"] < b]
        mmres[lab] = dict(all_fills=len(allf), all_sh=round(sum(abs(x["q"]) for x in allf)),
                          all_mo1_S=round(sum(x["mo1"] for x in allf if x["mo1"] is not None) * T),
                          all_mo4_S=round(sum(x["mo4"] for x in allf if x["mo4"] is not None) * T))
        for mode in ("whole_move", "after_det"):
            bl = mm_rule(evs, a, b, mode)
            mmres[lab][mode] = dict(n=len(bl), sh=round(sum(abs(x["q"]) for x in bl)),
                                    mo1_S=round(sum(x["mo1"] for x in bl if x["mo1"] is not None) * T),
                                    mo4_S=round(sum(x["mo4"] for x in bl if x["mo4"] is not None) * T))
        # outside-move pre-pull: after an outside move >= 1.5 pt / 30 min that SIG has not followed, pull the side the
        # outside move points at (asks if fair up) on that leg and its race-mates for 60 min
        sigs = catchup_signals(a, b, pts=1.5)
        bl = []
        for x in allf:
            race = EX.get(x["ex"], ("", ""))[0]
            for s in sigs:
                if s["race"] != race:
                    continue
                d = s["dir"] if EX.get(x["ex"], ("", ""))[1] == s["party"] else -s["dir"]
                if s["t"] <= x["ts"] <= s["t"] + 3600 and x["q"] * d < 0:
                    bl.append(x)
                    break
        mmres[lab]["outside_prepull_1.5"] = dict(n=len(bl), signals=len(sigs), sh=round(sum(abs(x["q"]) for x in bl)),
                                                 mo1_S=round(sum(x["mo1"] for x in bl if x["mo1"] is not None) * T),
                                                 mo4_S=round(sum(x["mo4"] for x in bl if x["mo4"] is not None) * T))
    res["mm"] = mmres

    # (d) pre-move signals: imbalance and tape flow, fired on a 5-min grid on MM-quoted / traded legs
    sig_res = {}
    for lab, a, b in (("window", W0, W1), ("pre", T_START + 1800, W0)):
        days = (b - a) / 86400
        for name in ("imbalance>=0.6", "flow>=80%&>=10k/15m", "outside drift>=1pt/30m"):
            fired, hits, leads, pnl = 0, 0, [], []
            last = {}
            base_n, base_hit = 0, 0
            for ex in obs:
                race = EX.get(ex, ("?", "?"))[0]
                t = a
                while t < b:
                    q = quote_at(ex, t, 120)
                    if q is None:
                        t += 300
                        continue
                    m0 = 0.5 * (q[1] + q[2])
                    if not (0.05 <= m0 <= 0.95):
                        t += 300
                        continue
                    d = 0
                    if name.startswith("imb"):
                        if q[3] is not None and q[4] is not None and (q[3] + q[4]) > 0:
                            qb, qa = min(3000, q[3]), min(3000, q[4])
                            imb = (qb - qa) / (qb + qa)
                            d = 1 if imb >= 0.6 else (-1 if imb <= -0.6 else 0)
                        else:
                            t += 300
                            continue
                    elif name.startswith("flow"):
                        bu, se, tot = tape_flow(ex, t - 900, t)
                        if tot >= 10000 and bu + se > 0:
                            fr = (bu - se) / (bu + se)
                            d = 1 if fr >= 0.6 else (-1 if fr <= -0.6 else 0)     # 80/20 split
                    else:
                        f0, f1 = fair_at(ex, t - 1800), fair_at(ex, t)
                        ma = mid_at(ex, t - 1800, 360, False)
                        if f0 and f1 and ma is not None:
                            dm = (f1[3] - f0[3]) / 0.01
                            if abs(dm) >= 1.0 and (m0 - ma) / 0.01 * math.copysign(1, dm) < abs(dm) / 3:
                                d = 1 if dm > 0 else -1
                    # outcome: >=5 tick move in direction d (or either direction for base rate) within 60 min
                    up = dn = None
                    for k in range(1, 13):
                        m = mid_at(ex, t + 300 * k, 360, False)
                        if m is None:
                            continue
                        if up is None and m - m0 >= 5 * T - 1e-9:
                            up = 5 * k
                        if dn is None and m0 - m >= 5 * T - 1e-9:
                            dn = 5 * k
                    base_n += 1
                    base_hit += (up is not None) or (dn is not None)
                    if d and t - last.get((race, name), -1e9) >= 3600:
                        last[(race, name)] = t
                        fired += 1
                        hh = up if d > 0 else dn
                        if hh is not None:
                            hits += 1
                            leads.append(hh)
                        o = quote_at(ex, t + 3600, 900)
                        if o is not None:
                            pnl.append(((o[1] - q[2]) if d > 0 else (q[1] - o[2])) / T)
                    t += 300
            sig_res[(lab, name)] = dict(fired=fired, per_day=round(fired / days, 1), hits=hits, hit_rate=round(hits / fired, 3) if fired else None,
                                        base_rate_either_dir=round(base_hit / base_n, 3) if base_n else None,
                                        median_lead_min=statistics.median(leads) if leads else None,
                                        taker_1h_mean=round(statistics.mean(pnl), 2) if pnl else None, taker_1h_t=None if len(pnl) < 3 else round(tstat(pnl), 1))
    res["signals"] = {f"{k[0]}|{k[1]}": v for k, v in sig_res.items()}

    # tables
    def ev_row(e):
        fl = e.get("fills") or []
        by = collections.Counter()
        adv_S = 0.0
        for x in fl:
            by[x["purpose"]] += 1
            if x["adverse"] and x["mo1"] is not None:
                adv_S += x["mo1"]
        r = e.get("rule") or {}
        return dict(race=e["race"], leg=f"{e['party']} {e['ex']}", mates=",".join(e.get("mates", [])), dir="+" if e["dir"] > 0 else "-",
                    start_utc=fmt(e["t_start"]), det_utc=fmt(e["t_det"]), end_utc=fmt(e["t_end"]),
                    dur_min=round((e["t_end"] - e["t_start"]) / 60), size=e["size"], p0=round(e["p0"], 4), p_end=round(e["p_end"], 4),
                    two_sided=e["two_sided"], in_gap=e.get("in_gap", False), cls=e["cls"], lead_min=e["lead_min"],
                    out_pre=e["out_pre_pts"], out_dur=e["out_dur_pts"], out_post=e["out_post_pts"],
                    gap_before=e["gap_before"], gap_after=e["gap_after"], ret15=e["ret15"], ret60=e["ret60"], ret240=e["ret240"],
                    touch=f"{e['touch_bid']:.0f}/{e['touch_ask']:.0f}" if e["touch_bid"] is not None else "-",
                    tape_vol=e["tape_vol"], flow_with=e["flow_with"], pre_flow_with=e["pre_flow_with"], pre_vol=e["pre_vol"],
                    bot_down_min=e["bot_down_min"], netD=e["netD"], fills=dict(by), adverse_mo1_S=round(adv_S * T, 1),
                    gate_hot=fmt(e["gate_hot_at"]),
                    fade_taker=None if r.get("taker") is None else round(r["taker"], 1),
                    fade_passive=None if r.get("passive") is None else round(r["passive"], 1),
                    fade_filled=r.get("passive_filled"), qty=r.get("qty"), fade_adds_d=r.get("fade_adds_d"))

    res["window_moves"] = [ev_row(e) for e in win_fast]
    res["window_slow_only"] = [ev_row(e) for e in slow_only if inwin(e)]
    res["pre_moves"] = [ev_row(e) for e in pre_fast]
    with open(os.path.join(OUT, "results.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)
    return res


if __name__ == "__main__":
    r = run()
    print(json.dumps({k: v for k, v in r.items() if k not in ("window_moves", "pre_moves", "window_slow_only", "catchup_rows_window_2")}, indent=1, default=str))
    print("window moves:", len(r["window_moves"]))
