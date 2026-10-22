"""Paper backtest of the set-arbitrage return threshold on recorded SIG quotes.

    python3 -m sigbot.backtest_arb                      # writes research_notes/arb_threshold_backtest.md
    python3 -m sigbot.backtest_arb --print-only         # stdout only

Stdlib only, no network, no orders. The SQLite database is COPIED (with its -wal/-shm files) to a temporary
directory and the copy is opened read-only, so the live database is never touched.

Data: table venue_quotes, venue='sig'. Rows are written only when a leg's quote changes, so the full book state at
each poll timestamp is rebuilt by forward-filling every leg's last (bid, ask). bid_sz / ask_sz are NULL in this
data, so top-of-book size is ASSUMED (500 sets; a 2,000-set variant is also run).

Simulation (per threshold), walking polls in time order:
  1. free unwinds (optional): sell-set lots held in a race are closed when the legs' asks sum to <= 1, i.e.
     unwinding (buy YES against each NO, proceeds n - sum(asks)) returns at least the settlement payout n - 1.
     NOTE the task text wrote "<= n_legs - 1"; that only equals "<= 1" for 2-leg races. For a 3-leg race
     "<= 2" would unwind at a loss, so the payout-based condition (sum asks <= 1, as in sigbot/arb.py
     free_unwinds, which tests forgone_edge <= 0) is used. Profit realised = qty * (entry sum_bids - sum asks now).
  2. entries: every race with sell-set (sum bids > 1) or buy-set (sum asks < 1 - x) whose return >= threshold, ranked
     by return, size = min(assumed top size, capital left in the race and in total) / cost. At most one capture
     per race per poll. Caps: 25,000 total, 5,000 per race. Minimum 20 sets (live min_set_qty).
  3. End of data: remaining sets are valued at locked profit (sell-sets: sum_bids - 1; buy-sets: expected edge,
     1 - sum_asks - x, which is NOT locked, it carries the x risk).

Limitations are written into the report: our own trades do not move later polls (displayed size is assumed to
refresh every poll), sizes are assumed, quote-change logging, etc.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THRESHOLDS = [round(0.002 * i, 4) for i in range(0, 21)]     # 0.0% .. 4.0%
TOTAL_CAP = 25_000.0
RACE_CAP = 5_000.0
MIN_SETS = 20
DEFAULT_X = 0.003


# ------------------------------------------------------------------ data
def open_copy_ro(path: str):
    tmp = tempfile.mkdtemp(prefix="sigbt_")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(path + suf):
            shutil.copy2(path + suf, os.path.join(tmp, "db.sqlite" + suf))
    con = sqlite3.connect("file:" + os.path.join(tmp, "db.sqlite") + "?mode=ro", uri=True)
    return con, tmp


def load(db_path: str):
    con, tmp = open_copy_ro(db_path)
    rows = con.execute("SELECT ts, race, party, instrument, bid, ask, bid_sz, ask_sz FROM venue_quotes "
                       "WHERE venue='sig' ORDER BY ts").fetchall()
    arb = con.execute("SELECT ts, race, kind, quantity, edge_per_set, cost_per_set, ok FROM arb_trades ORDER BY ts").fetchall()
    con.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return rows, arb


def build_polls(rows):
    """-> (polls, races, sizes_present). polls: list of (ts, {race: ([bids], [asks]) or None when a leg is missing})."""
    legs = defaultdict(dict)                   # race -> party -> instrument
    for _, race, party, inst, *_ in rows:
        legs[race][party] = inst
    races = {r: sorted(p) for r, p in legs.items()}
    state = {}                                 # instrument -> (bid, ask)
    by_ts = defaultdict(list)
    sizes = False
    for ts, race, party, inst, bid, ask, bsz, asz in rows:
        by_ts[ts].append((inst, bid, ask))
        sizes = sizes or bsz is not None or asz is not None
    polls = []
    for ts in sorted(by_ts):
        for inst, bid, ask in by_ts[ts]:
            state[inst] = (bid, ask)
        snap = {}
        for race, parties in races.items():
            qs = [state.get(legs[race][p]) for p in parties]
            if any(q is None for q in qs):
                continue
            snap[race] = (tuple(q[0] for q in qs), tuple(q[1] for q in qs))
        polls.append((ts, snap))
    return polls, races, sizes


def x_table(races):
    try:
        by = json.load(open(os.path.join(ROOT, "params", "x_live.json")))["by_race"]
    except (OSError, ValueError, KeyError):
        by = {}
    x = {}
    for r, parties in races.items():
        x[r] = 0.0 if "I" in parties else float(by.get(r, {}).get("x", DEFAULT_X))
    return x


def settlement_ts():
    try:
        m = json.load(open(os.path.join(ROOT, "data", "markets.json")))
        d = max(x["settlementDate"] for x in m if x.get("settlementDate"))
        return datetime.fromisoformat(d.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ------------------------------------------------------------ simulation
def opportunities(snap_race, x):
    """-> list of (kind, edge, cost, ret) available in one race at one poll."""
    bids, asks = snap_race
    n = len(bids)
    out = []
    if all(b is not None for b in bids):
        sb = sum(bids)
        edge, cost = sb - 1.0, n - sb
        if edge > 1e-12 and cost > 0:
            out.append(("sell_set", edge, cost, edge / cost, sb))
    if all(a is not None for a in asks):
        sa = sum(asks)
        edge, cost = 1.0 - sa - x, sa
        if edge > 1e-12 and cost > 0:
            out.append(("buy_set", edge, cost, edge / cost, sa))
    return out


def simulate(polls, races, xs, threshold, size, unwinds=True, restack=True, settle=None):
    t0, t1 = polls[0][0], polls[-1][0]
    lots = []                 # dict(race, kind, qty, edge, cost, n, sumbids, ts)
    cap_race = defaultdict(float)
    cap_tot = 0.0
    cap_hours = 0.0
    realised = 0.0
    by_race = defaultdict(float)
    entries = sets = unw_n = unw_sets = 0
    realised_unwind_profit = 0.0
    prev_ts = t0
    active_run = set()        # (race, kind) currently inside a qualifying run (for restack=False)
    peak = 0.0
    for ts, snap in polls:
        cap_hours += cap_tot * (ts - prev_ts) / 3600.0       # capital held since the previous poll
        prev_ts = ts
        # 1. free unwinds
        if unwinds:
            # per race: FIFO, up to `size` sets per poll (unwind is a buy of YES at the asks, top-of-book size)
            for race in {l["race"] for l in lots if l["kind"] == "sell_set" and l["qty"] > 0}:
                s = snap.get(race)
                if not s or any(a is None for a in s[1]) or sum(s[1]) > 1.0 + 1e-12:
                    continue
                sa = sum(s[1])
                room = size
                for lot in lots:
                    if room <= 0:
                        break
                    if lot["race"] != race or lot["kind"] != "sell_set" or lot["qty"] <= 0:
                        continue
                    q = min(lot["qty"], room)
                    pnl = q * (lot["sumbids"] - sa)
                    realised += pnl
                    by_race[race] += pnl
                    realised_unwind_profit += pnl
                    freed = q * lot["cost"]
                    cap_tot -= freed
                    cap_race[race] -= freed
                    lot["qty"] -= q
                    room -= q
                    unw_n += 1
                    unw_sets += q
            lots = [l for l in lots if l["qty"] > 0]
        # 2. entries
        cands = []
        live = set()
        for race, srow in snap.items():
            for kind, edge, cost, ret, ssum in opportunities(srow, xs[race]):
                if ret + 1e-12 >= threshold:
                    live.add((race, kind))
                    if not restack and (race, kind) in active_run:
                        continue
                    cands.append((ret, race, kind, edge, cost, ssum, len(srow[0])))
        active_run = live if not restack else active_run
        cands.sort(key=lambda c: -c[0])
        for ret, race, kind, edge, cost, ssum, n in cands:
            room_cap = min(TOTAL_CAP - cap_tot, RACE_CAP - cap_race[race])
            q = min(size, int(room_cap // cost))
            if q < MIN_SETS:
                continue
            lots.append(dict(race=race, kind=kind, qty=q, edge=edge, cost=cost, n=n, sumbids=ssum if kind == "sell_set" else 0.0, ts=ts))
            cap_tot += q * cost
            cap_race[race] += q * cost
            entries += 1
            sets += q
        peak = max(peak, cap_tot)
    # 3. mark remaining at locked / expected profit
    held_sell = sum(l["qty"] * l["edge"] for l in lots if l["kind"] == "sell_set")
    held_buy = sum(l["qty"] * l["edge"] for l in lots if l["kind"] == "buy_set")
    held_cap = sum(l["qty"] * l["cost"] for l in lots)
    for l in lots:
        by_race[l["race"]] += l["qty"] * l["edge"]
    hours = (t1 - t0) / 3600.0
    profit = realised + held_sell + held_buy
    days = hours / 24.0
    avg_cap = cap_hours / hours if hours else 0.0
    ppd = profit / (cap_hours / 24.0 / 1000.0) if cap_hours > 0 else 0.0
    ppd_settle = None
    if settle and settle > t1:
        ch = cap_hours + held_cap * (settle - t1) / 3600.0       # held sets keep tying up capital until settlement
        ppd_settle = profit / (ch / 24.0 / 1000.0) if ch > 0 else 0.0
    return dict(threshold=threshold, entries=entries, sets=sets, profit=profit, realised=realised, held_sell=held_sell,
                held_buy=held_buy, held_cap=held_cap, avg_cap=avg_cap, cap_hours=cap_hours, per1k_day=ppd,
                per1k_day_settle=ppd_settle, unwinds=unw_n, unwind_sets=unw_sets, peak_cap=peak, hours=hours, by_race=dict(by_race))


def sweep(polls, races, xs, size, unwinds, restack=True, settle=None):
    return [simulate(polls, races, xs, t, size, unwinds, restack, settle) for t in THRESHOLDS]


# ------------------------------------------------------------- reporting
def fmt_table(res, with_unw=True):
    h = "| thr | entries | sets | profit | realised | held (sell/buy) | avg cap | cap-hours | $/1k cap/day (window) | $/1k cap/day (to settle) |"
    h += " free unwinds |" if with_unw else ""
    lines = [h, "|" + "---|" * (h.count("|") - 1)]
    for r in res:
        ps = "n/a" if r["per1k_day_settle"] is None else f"{r['per1k_day_settle']:.1f}"
        row = (f"| {100*r['threshold']:.1f}% | {r['entries']} | {r['sets']:,} | {r['profit']:,.0f} | {r['realised']:,.0f} | "
               f"{r['held_sell']:,.0f}/{r['held_buy']:,.0f} | {r['avg_cap']:,.0f} | {r['cap_hours']:,.0f} | {r['per1k_day']:.1f} | {ps} |")
        if with_unw:
            row += f" {r['unwinds']} ({r['unwind_sets']:,} sets) |"
        lines.append(row)
    return "\n".join(lines)


def best(res):
    return max(res, key=lambda r: r["profit"]), max(res, key=lambda r: r["per1k_day"])


def actual_summary(arb, t_first, t_last):
    out = []
    for kind in ("sell_set", "buy_set", "unwind_set"):
        ok = [a for a in arb if a[2] == kind and a[6] == 1]
        if not ok:
            continue
        q = sum(a[3] for a in ok)
        edge = sum(a[3] * a[4] for a in ok)
        cost = sum(a[3] * a[5] for a in ok)
        out.append((kind, len(ok), q, edge, cost, (edge / cost if cost else 0.0),
                    min(a[0] for a in ok), max(a[0] for a in ok)))
    return out


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "sigbot.sqlite"))
    ap.add_argument("--out", default=os.path.join(ROOT, "research_notes", "arb_threshold_backtest.md"))
    ap.add_argument("--print-only", action="store_true")
    a = ap.parse_args()

    rows, arb = load(a.db)
    polls, races, sizes_present = build_polls(rows)
    xs = x_table(races)
    settle = settlement_ts()
    t0, t1 = polls[0][0], polls[-1][0]
    first_full = sum(1 for r in polls[0][1])

    runs = {}
    for size in (500, 2000):
        runs[(size, True, True)] = sweep(polls, races, xs, size, True, True, settle)
        runs[(size, False, True)] = sweep(polls, races, xs, size, False, True, settle)
    runs[(500, True, False)] = sweep(polls, races, xs, 500, True, False, settle)
    runs[(500, False, False)] = sweep(polls, races, xs, 500, False, False, settle)

    # opportunity census (no capital limits): qualifying race-polls by threshold
    census = []
    for t in THRESHOLDS:
        s = b = 0
        for _, snap in polls:
            for race, row in snap.items():
                for kind, edge, cost, ret, _ in opportunities(row, xs[race]):
                    if ret + 1e-12 >= t:
                        s += kind == "sell_set"
                        b += kind == "buy_set"
        census.append((t, s, b))

    L = []
    L.append("# Set-arb return threshold backtest (paper, SIG quotes)\n")
    L.append(f"Generated by `python3 -m sigbot.backtest_arb`. Data: `venue_quotes` venue='sig', {len(polls)} polls, "
             f"{iso(t0)} to {iso(t1)} UTC ({(t1-t0)/3600:.1f} h), {len(races)} races "
             f"({sum(1 for r in races.values() if 'I' in r)} with an Independent leg). "
             f"Order sizes in the table are {'present' if sizes_present else 'ABSENT (bid_sz/ask_sz are NULL), so top-of-book size is assumed'}.\n")
    L.append("## Method and limitations\n")
    L.append("* venue_quotes stores a row only when a leg's quote changes; the full state at each poll is rebuilt by forward-filling "
             "each leg's last bid/ask. Polls with no change anywhere are not logged (state is unchanged, so no new opportunity), "
             "but the first poll must already contain all legs.")
    L.append("* Sell-set: sum(bids) > 1, edge = sum_bids - 1, cost = n - sum_bids. Buy-set: sum(asks) < 1 - x, edge = 1 - sum_asks - x, cost = sum_asks "
             "(x from params/x_live.json by_race, default 0.003, 0 for races with an Independent leg). Buy-set profit is an EXPECTED value "
             "(it loses everything if no listed party wins), sell-set profit is locked.")
    L.append("* Entry when return >= threshold, ranked by return, size = assumed top-of-book size capped by 25,000 total / 5,000 per race, minimum 20 sets, "
             "at most one capture per race per poll. Threshold 0.0% means any positive edge (the live bot additionally requires an absolute edge of 0.5c "
             "and uses min_ret 1.5%; neither floor is applied here).")
    L.append("* Free unwind: held sell-sets are closed when sum(asks) <= 1 (proceeds n - sum_asks >= payout n - 1). The brief wrote `<= n_legs - 1`; "
             "that equals `<= 1` only for 2-leg races, and for a 3-leg race it would unwind at a loss, so the payout-based rule from sigbot/arb.py is used. "
             "Unwinds are FIFO, at most the assumed top size per race per poll, and realise entry sum_bids - sum_asks per set.")
    L.append("* Held sets are valued at locked profit at the end of data. Window metrics divide by capital-hours inside the observation window only; "
             "held sets keep their capital locked until settlement (2026-11-04 17:00 UTC), so the `to settle` column also charges that capital for the "
             "remaining time. That is the fair comparison across thresholds, because the window metric rewards sets that merely have not matured yet.")
    L.append("* LIMITATIONS: (1) our trades do not move later polls: a standing mispricing is assumed to refill to the assumed size every poll, so it "
             "is re-captured each poll until a cap binds. This is the main source of optimism; the `no re-stack` runs enter a race once per continuous "
             "run of qualifying polls instead. (2) Sizes are assumed (500, and 2,000), not observed. (3) Fills are assumed at the logged touch with "
             "no latency, no fees, no allOrNone rejection (the live log has 8 rejected arb orders out of 22). (4) The window is "
             f"only {(t1-t0)/3600:.1f} hours, a single evening, so thresholds are compared on one regime and a few races (see concentration below). "
             "(5) Unwinding only considers the top of the ask book. (6) Quotes come from a 60-120 s poll, so intra-poll opportunities are invisible.\n")

    for key, title in [((500, True, True), "A. Assumed size 500 sets, free unwinds ON (main case)"),
                       ((500, False, True), "B. Size 500, free unwinds OFF"),
                       ((2000, True, True), "C. Size 2,000, free unwinds ON"),
                       ((2000, False, True), "D. Size 2,000, free unwinds OFF"),
                       ((500, True, False), "E. Size 500, unwinds ON, no re-stack (one entry per race per qualifying run)"),
                       ((500, False, False), "F. Size 500, unwinds OFF, no re-stack")]:
        res = runs[key]
        bp, bq = best(res)
        L.append(f"## {title}\n")
        L.append(fmt_table(res, with_unw=key[1]))
        L.append(f"\nBest total profit: {100*bp['threshold']:.1f}% ({bp['profit']:,.0f}); best profit per 1k capital-day (to settlement): "
                 f"{100*max((r for r in res if r['per1k_day_settle'] is not None), key=lambda r: r['per1k_day_settle'])['threshold']:.1f}%; "
                 f"best window profit per 1k capital-day: {100*bq['threshold']:.1f}%.\n")

    L.append("## What the free-unwind rule adds (size 500, re-stack)\n")
    L.append("| thr | profit ON | profit OFF | delta | avg cap ON | avg cap OFF | per1k/day (window) ON | OFF | per1k/day (to settle) ON | OFF | unwinds |")
    L.append("|" + "---|" * 11)
    for on, off in zip(runs[(500, True, True)], runs[(500, False, True)]):
        so = "n/a" if on["per1k_day_settle"] is None else f"{on['per1k_day_settle']:.1f}"
        sf = "n/a" if off["per1k_day_settle"] is None else f"{off['per1k_day_settle']:.1f}"
        L.append(f"| {100*on['threshold']:.1f}% | {on['profit']:,.0f} | {off['profit']:,.0f} | {on['profit']-off['profit']:+,.0f} | {on['avg_cap']:,.0f} | "
                 f"{off['avg_cap']:,.0f} | {on['per1k_day']:.1f} | {off['per1k_day']:.1f} | {so} | {sf} | {on['unwinds']} |")
    L.append("")

    L.append("## Concentration and unwind near-misses\n")
    L.append("Standing mispricings dominate: most qualifying races are qualifying at every one of the polls, so capital fills at the first polls and the "
             "threshold mostly decides WHICH races get the capital.\n")
    L.append("| case | thr | races traded | top-5 share of profit | top races (profit) |")
    L.append("|---|---|---|---|---|")
    for key, nm in (((500, True, True), "A"), ((500, True, False), "E")):
        for r in runs[key]:
            if abs(r["threshold"] - t) > 1e-9 if False else round(100 * r["threshold"], 1) in (0.0, 1.6, 3.2, 4.0):
                br = {k: v for k, v in r["by_race"].items() if abs(v) > 1e-9}
                top = sorted(br.items(), key=lambda kv: -kv[1])[:5]
                tot = sum(br.values()) or 1.0
                L.append(f"| {nm} | {100*r['threshold']:.1f}% | {len(br)} | {100*sum(v for _, v in top)/tot:.0f}% | "
                         + ", ".join(f"{k} {v:,.0f}" for k, v in top) + " |")
    sell_races = {race for _, snap in polls for race, row in snap.items() if any(o[0] == "sell_set" for o in opportunities(row, xs[race]))}
    mins = sorted((min(sum(snap[r][1]) for _, snap in polls if r in snap and None not in snap[r][1]), r) for r in sell_races)
    L.append(f"\nFree-unwind trigger (sum of asks <= 1) among the {len(sell_races)} races that ever offered a sell-set: "
             f"{sum(1 for m, _ in mins if m <= 1.0 + 1e-12)} races ever reach it; lowest sums: "
             + ", ".join(f"{r} {m:.3f}" for m, r in mins[:6]) + ".\n")

    L.append("## Opportunity census (no capital limits, race-polls that qualify)\n")
    L.append("| thr | sell-set race-polls | buy-set race-polls |")
    L.append("|---|---|---|")
    for t, s, b in census:
        L.append(f"| {100*t:.1f}% | {s} | {b} |")
    L.append("")

    L.append("## Our actual arb trades (arb_trades, ok=1) for comparison\n")
    L.append("| kind | rows | sets | sum edge x sets | sum cost x sets | avg return | first | last |")
    L.append("|---|---|---|---|---|---|---|---|")
    for kind, n, q, e, c, r, f, l in actual_summary(arb, t0, t1):
        L.append(f"| {kind} | {n} | {q:,} | {e:,.0f} | {c:,.0f} | {100*r:.2f}% | {iso(f)} | {iso(l)} |")
    L.append("\nThe actual trades start about 1 h before the first recorded poll (the live bot was already trading at min_ret 1.5%), so the periods only partly overlap. "
             "Real unwind edge values are negative forgone edge, not profit.\n")

    text = "\n".join(L)
    print(text)
    if not a.print_only:
        os.makedirs(os.path.dirname(a.out), exist_ok=True)
        with open(a.out, "w") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
