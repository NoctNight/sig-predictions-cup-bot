"""Candidate market-making rules replayed with the shadow tuner's calibrated fill model. Paper only (no API, DB read-only).

`simulate_x` is sigbot.shadow.simulate_mm with extra knobs; with none of them set it reproduces simulate_mm exactly
(checked by `--selftest`). New knobs (all optional in the parameter dict):
  improve_ge     level 1 improves the touch when the spread is >= this many ticks (live 3; shadow hard-codes 2)
  fill_model     'bucket' (shadow: share of through-volume by distance from mid) or 'spread' (share by distance AND the
                 touch spread at that moment, so a level-1 order joining a one-tick DMM queue and one improving a wide
                 spread are calibrated separately; shrunk toward the bucket rate)
  dmm            None | 'wide_only' (no level-1 quote on a one-tick spread) | 'wide_all' (no quotes at all on one-tick
                 spreads) | ('queue_max', N) (no level-1 quote at a touch price whose resting size is >= N shares)
  xv_w, xv_cap   fair += xv_w * clip(outside fair - SIG mid, +-xv_cap ticks): Polymarket/Kalshi fair as reservation
  xv_pull        pull level 1 on the side the outside fair says is mispriced by >= xv_pull ticks (fair above mid: no ask)
  xv_lead        (ticks, seconds): outside fair moved >= ticks over the last `seconds` and the SIG mid moved less than
                 half of it -> pull every level on the side the move runs into
  mr_mode        'price' (live: mean-reversion prediction shifts fair) | 'size' (no price shift; sizes skewed instead,
                 side the prediction favours x (1 + mr_size_k * |mr|/cap), other side x (1 - mr_size_k * |mr|/cap))
                 | 'none'
  band           (lo, hi): quote only when lo <= mid <= hi (favourite-longshot / tail filter)
  pause_windows  list of (t0, t1): no quotes inside (scheduled-event withdrawal)
Extra outputs: edge5 / edge30 = sum over fills of qty x (signed mid move to +5 / +30 min, in price units) plus the
concession to the mid at the fill (i.e. P&L of each fill marked 5 / 30 minutes later), and per-fill records when
p['keep_fills'] is set.
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict
from typing import Optional

from ..config import PRICE_MAX, PRICE_MIN, TICK, round_tick, ticks
from .. import shadow as S

SPREAD_PRIOR_SHARES = 3000.0      # shrinkage of each (bucket, spread class) participation toward its bucket rate


def spread_class(bb: float, ba: float) -> int:
    return max(1, min(3, ticks(ba) - ticks(bb)))


class MMDataX(S.MMData):
    """MMData plus (bucket, spread class) participation and the outside-venue fair series per exchange."""

    def __init__(self, con, t0, t1, fit_until, cfg, xv: bool = True):
        super().__init__(con, t0, t1, fit_until, cfg)
        self.stimes = {ex: [s[0] for s in v] for ex, v in self.snaps.items()}
        self.part2 = self._calibrate_spread(fit_until)
        self.xv_t: dict = {}
        self.xv_f: dict = {}
        if xv:
            rows = con.execute("SELECT ts, exchange_id, fair FROM venue_fair WHERE ts>=? AND ts<=? AND fair IS NOT NULL "
                               "AND exchange_id IS NOT NULL ORDER BY ts", (t0 - 3600, t1)).fetchall()
            for r in rows:
                self.xv_t.setdefault(r["exchange_id"], []).append(r["ts"])
                self.xv_f.setdefault(r["exchange_id"], []).append(r["fair"])

    def xv_at(self, ex: str, ts: float, max_age: float = 600.0) -> Optional[float]:
        tt = self.xv_t.get(ex)
        if not tt:
            return None
        i = bisect.bisect_right(tt, ts) - 1
        if i < 0 or ts - tt[i] > max_age:
            return None
        return self.xv_f[ex][i]

    def mid_at(self, ex: str, ts: float) -> Optional[float]:
        """First snapshot mid at or after ts (None past the end of the series)."""
        tt = self.stimes.get(ex)
        if not tt:
            return None
        i = bisect.bisect_left(tt, ts)
        return self.snaps[ex][i][5] if i < len(tt) else None

    def _calibrate_spread(self, fit_until: float) -> dict:
        """Same order-life walk as MMData._calibrate, keyed by (distance bucket, spread class of the snapshot)."""
        thr: dict = defaultdict(float)
        fil: dict = defaultdict(float)
        by_ex: dict = defaultdict(list)
        for o in self.orders:
            by_ex[o["exchange_id"]].append(o)
        fills_by_order: dict = defaultdict(list)
        for f in self.fills:
            if f["order_id"] is not None:
                fills_by_order[int(f["order_id"])].append((S._ts(f["filled_at"]), float(f["q"])))
        for ex, orders in by_ex.items():
            snaps = self.snaps.get(ex) or []
            if len(snaps) < 2 or not self.tape.get(ex):
                continue
            times = sorted(o["placed_at"] for o in orders)
            clusters: list = []
            for t in times:
                if not clusters or t - clusters[-1][1] > 3.0:
                    clusters.append([t, t])
                else:
                    clusters[-1][1] = t
            starts = [c[0] for c in clusters]
            stimes = [s[0] for s in snaps]
            for o in orders:
                start = o["placed_at"]
                ci = bisect.bisect_right(starts, start) - 1
                end = starts[ci + 1] if ci + 1 < len(starts) else fit_until
                end = min(end, fit_until)
                if o["expires_at"]:
                    end = min(end, S._ts(o["expires_at"]))
                ofills = sorted(fills_by_order.get(int(o["order_id"]), []))
                if ofills and sum(q for _, q in ofills) >= o["quantity"] - 0.5:
                    end = min(end, ofills[-1][0] + 1.0)
                if end <= start:
                    continue
                yes_px = o["price"] if o["side"] == "yes" else round(1 - o["price"], 3)
                k = max(0, bisect.bisect_right(stimes, start) - 1)
                while k < len(stimes) and stimes[k] < end:
                    a = max(start, stimes[k]); b = min(end, stimes[k + 1] if k + 1 < len(stimes) else end)
                    if b > a:
                        s = snaps[k]
                        d = (s[5] - yes_px) / TICK if o["side"] == "yes" else (yes_px - s[5]) / TICK
                        key = (self.bucket(d), spread_class(s[1], s[2]))
                        thr[key] += self.through(ex, a, b, o["side"], yes_px)
                        for ft, fq in ofills:
                            if a <= ft < b:
                                fil[key] += fq
                    k += 1
        out = {}
        for key in set(thr) | set(fil):
            base = self.part.get(key[0], 0.0)
            out[key] = min(1.0, (fil.get(key, 0.0) + base * SPREAD_PRIOR_SHARES) / (thr.get(key, 0.0) + SPREAD_PRIOR_SHARES))
        self.calib["participation_by_spread"] = {f"{k[0]}|{k[1]}": [round(v, 4), round(thr.get(k, 0.0)), round(fil.get(k, 0.0))]
                                                  for k, v in sorted(out.items())}
        return out


def simulate_x(data: MMDataX, t_from: float, t_to: float, p: dict) -> dict:
    """sigbot.shadow.simulate_mm plus the knobs in the module docstring."""
    cfg = data.cfg.mm
    live_mr_cap = float(cfg.meanrev_max_ticks) * TICK
    mr_cap = min(float(p["meanrev_max_ticks"]), float(cfg.meanrev_max_ticks)) * TICK
    lticks, lmult = list(p["level_ticks"]), list(p["level_size_mult"])
    nlev = min(len(lticks), len(lmult))
    lticks, lmult = lticks[:nlev], lmult[:nlev]
    requote = float(p["requote_ticks"]) * TICK
    fade = float(p["vol_gate_fade_mult"])
    cap_frac = float(p.get("capital_frac", cfg.capital_frac))
    per_market = min(cap_frac * data.cfg.starting_cash, 0.9 * data.cfg.starting_cash) / max(1, cfg.n_markets)
    half = 0.5 * per_market
    base_qty = float(p.get("base_qty", cfg.base_qty))
    max_inv = float(p.get("max_inventory_notional", cfg.max_inventory_notional))
    prop = p.get("size_model", "fixed") == "prop"
    improve_ge = int(p.get("improve_ge", S.MM_IMPROVE_SPREAD_GE))
    spread_fill = p.get("fill_model", "bucket") == "spread"
    dmm = p.get("dmm")
    xv_w, xv_cap = float(p.get("xv_w", 0.0)), float(p.get("xv_cap", 2.0)) * TICK
    xv_pull = p.get("xv_pull")
    xv_lead = p.get("xv_lead")
    mr_mode = p.get("mr_mode", "price")
    mr_size_k = float(p.get("mr_size_k", 0.5))
    band = p.get("band")
    pauses = p.get("pause_windows") or []
    keep = p.get("keep_fills", False)
    lvl_fill: dict = defaultdict(float)
    lvl_mo: dict = defaultdict(lambda: [0.0, 0])
    max_inv_shares = max_inv_notional = max_coll_total = 0.0
    coll_by_ex: dict = {}
    refresh = float(cfg.refresh_sec)
    pnl_total = cap_hours = 0.0
    n_fill_events = fill_qty = 0.0
    edge5 = edge30 = 0.0
    peak = dd = 0.0
    path: dict = {}
    markouts: list = []
    mo_w = [0.0, 0.0]
    fills_out: list = []
    for ex, snaps in data.snaps.items():
        i0 = next((i for i, s in enumerate(snaps) if s[0] >= t_from), len(snaps))
        i1 = next((i for i in range(len(snaps) - 1, i0 - 1, -1) if snaps[i][0] <= t_to), i0 - 1) + 1
        rows = snaps[i0:i1]
        if len(rows) < 3:
            continue
        idx15, idx30 = data.idx15[ex], data.idx30[ex]
        tape = data.tape.get(ex) or []
        tape_i = 0
        while tape_i < len(tape) and tape[tape_i][0] < t_from:
            tape_i += 1
        sigma = data.vol.get(ex, 0.01)
        inv = cash = 0.0
        orders: list = []
        last_reserve, last_quote_ts, fill_since = None, 0.0, False
        hot_until, anchor = 0.0, None
        ex_pnl_prev = 0.0
        for i, s in enumerate(rows):
            ts, bb, ba, bq, aq, mid, micro, fair_logged, pressure = s
            gi = i0 + i
            if i > 0 and ts - rows[i - 1][0] > 900:
                orders = []; last_reserve = None
            mr_logged = max(-live_mr_cap, min(live_mr_cap, fair_logged - micro - pressure * S.MM_PRESSURE_SKEW_TICKS * TICK))
            mr = max(-mr_cap, min(mr_cap, mr_logged))
            fair = micro + pressure * S.MM_PRESSURE_SKEW_TICKS * TICK + (mr if mr_mode == "price" else 0.0)
            xvf = data.xv_at(ex, ts) if (xv_w or xv_pull or xv_lead) else None
            if xv_w and xvf is not None:
                fair += xv_w * max(-xv_cap, min(xv_cap, xvf - mid))
            inv_notional = inv * (mid if inv > 0 else (1 - mid))
            inv_frac = max(-1.0, min(1.0, inv_notional / max_inv))
            T = min(24.0, max(0.0, (data.stop_at - ts) / 3600.0))
            shift = max(-S.MM_INV_SKEW_TICKS, min(S.MM_INV_SKEW_TICKS, inv_frac * S.MM_INV_SKEW_TICKS + S.MM_GAMMA * inv_frac * (sigma / TICK) ** 2 * T / 100.0))
            reserve = fair - shift * TICK
            jb = idx15[gi]
            ref = snaps[jb][5] if jb >= i0 else rows[0][5]
            if abs(mid - ref) >= S.MM_VOL_GATE_MOVE_TICKS * TICK - 1e-9:
                hot_until = ts + S.MM_VOL_GATE_COOLDOWN
                if anchor is None:
                    anchor = ref
            hot = hot_until > ts
            if not hot:
                anchor = None
            spread_add = S.MM_VOL_GATE_SPREAD_ADD if hot else 0
            if hot and anchor is not None:
                if inv > 0:
                    reserve = max(reserve, anchor)
                elif inv < 0:
                    reserve = min(reserve, anchor)
            off = (band is not None and not (band[0] <= mid <= band[1])) or any(a <= ts < b for a, b in pauses)
            spread_ticks = ticks(ba) - ticks(bb)
            if dmm == "wide_all" and spread_ticks <= 1:
                off = True
            need = (last_reserve is None or abs(reserve - last_reserve) >= requote - 1e-9 or fill_since or ts - last_quote_ts > refresh
                    or len(orders) < 2 * nlev)
            if off:
                orders = []; last_reserve = None
            elif need:
                bbid = [base_qty * m for m in lmult]; bask = list(bbid)
                if inv_frac > 0:
                    bbid = [q * (1 - S.MM_SIZE_SKEW_AT_MAX * inv_frac) for q in bbid]; bask = [q * (1 + 0.5 * S.MM_SIZE_SKEW_AT_MAX * inv_frac) for q in bask]
                elif inv_frac < 0:
                    bask = [q * (1 - S.MM_SIZE_SKEW_AT_MAX * -inv_frac) for q in bask]; bbid = [q * (1 + 0.5 * S.MM_SIZE_SKEW_AT_MAX * -inv_frac) for q in bbid]
                if pressure > 0:
                    bask = [q * (1 - S.MM_PRESSURE_SIZE_SKEW * min(1.0, pressure)) for q in bask]
                elif pressure < 0:
                    bbid = [q * (1 - S.MM_PRESSURE_SIZE_SKEW * min(1.0, -pressure)) for q in bbid]
                if mr_mode == "size" and mr_cap > 0 and mr != 0:
                    a = min(1.0, mr_size_k * abs(mr) / mr_cap)
                    if mr > 0:       # predicted up: buy more, sell less
                        bbid = [q * (1 + a) for q in bbid]; bask = [q * (1 - a) for q in bask]
                    else:
                        bask = [q * (1 + a) for q in bask]; bbid = [q * (1 - a) for q in bbid]
                if inv_frac >= 1.0:
                    bbid = [0.0] * nlev
                elif inv_frac <= -1.0:
                    bask = [0.0] * nlev
                if hot:
                    if inv > 0:
                        bbid = [q * fade for q in bbid]; bask = [q * S.MM_VOL_GATE_SIZE_MULT for q in bask]
                    elif inv < 0:
                        bask = [q * fade for q in bask]; bbid = [q * S.MM_VOL_GATE_SIZE_MULT for q in bbid]
                    else:
                        bbid = [q * S.MM_VOL_GATE_SIZE_MULT for q in bbid]; bask = [q * S.MM_VOL_GATE_SIZE_MULT for q in bask]
                pull_bid = pull_ask = False          # level-1 pulls
                pull_bid_all = pull_ask_all = False
                if xv_pull and xvf is not None:
                    if xvf - mid >= xv_pull * TICK - 1e-9:
                        pull_ask = True
                    elif mid - xvf >= xv_pull * TICK - 1e-9:
                        pull_bid = True
                if xv_lead and xvf is not None:
                    xv_old = data.xv_at(ex, ts - xv_lead[1], max_age=max(600.0, xv_lead[1]))
                    m_old = None
                    tt = data.stimes[ex]
                    jo = bisect.bisect_right(tt, ts - xv_lead[1]) - 1
                    if jo >= 0:
                        m_old = snaps[jo][5]
                    if xv_old is not None and m_old is not None:
                        mv, smv = xvf - xv_old, mid - m_old
                        if abs(mv) >= xv_lead[0] * TICK - 1e-9 and abs(smv) < 0.5 * abs(mv):
                            if mv > 0:
                                pull_ask_all = True
                            else:
                                pull_bid_all = True
                if dmm == "wide_only" and spread_ticks <= 1:
                    pull_bid = pull_ask = True
                sb = min(1.0, half / (sum(q * mid for q in bbid) or 1.0)); sa = min(1.0, half / (sum(q * (1 - mid) for q in bask) or 1.0))
                new: dict = {}
                for k, dt in enumerate(lticks):
                    dt += spread_add
                    improve = TICK if (k == 0 and spread_ticks >= improve_ge) else 0.0
                    bpx = min(round_tick(reserve - dt * TICK), round_tick(bb + improve), round_tick(ba - TICK))
                    qb = int(bbid[k] * sb)
                    if k == 0 and (pull_bid or (isinstance(dmm, tuple) and dmm[0] == "queue_max" and abs(bpx - bb) < 1e-9 and bq >= dmm[1])):
                        qb = 0
                    if pull_bid_all:
                        qb = 0
                    if qb >= 1 and PRICE_MIN <= bpx <= PRICE_MAX:
                        new.setdefault(("yes", bpx), ["yes", bpx, 0, k + 1])[2] += qb
                    apx = max(round_tick(reserve + dt * TICK), round_tick(ba - improve), round_tick(bb + TICK))
                    qa = int(bask[k] * sa)
                    if k == 0 and (pull_ask or (isinstance(dmm, tuple) and dmm[0] == "queue_max" and abs(apx - ba) < 1e-9 and aq >= dmm[1])):
                        qa = 0
                    if pull_ask_all:
                        qa = 0
                    if qa >= 1 and PRICE_MIN <= apx <= PRICE_MAX:
                        new.setdefault(("no", apx), ["no", apx, 0, k + 1])[2] += qa
                orders = list(new.values())
                last_reserve, last_quote_ts, fill_since = reserve, ts, False
            if i + 1 < len(rows):
                t_next = rows[i + 1][0]
                dt_h = (t_next - ts) / 3600.0
                seg = []
                while tape_i < len(tape) and tape[tape_i][0] < t_next:
                    if tape[tape_i][0] >= ts:
                        seg.append(tape[tape_i])
                    tape_i += 1
                has_tape = bool(tape)
                sc = spread_class(bb, ba)
                for o in orders:
                    side, px, q = o[0], o[1], o[2]
                    if q < 1:
                        continue
                    d = (mid - px) / TICK if side == "yes" else (px - mid) / TICK
                    bk = S.MMData.bucket(d)
                    if has_tape:
                        thr = sum(tq for tt_, tp, tq in seg if (tp <= px + 1e-9 if side == "yes" else tp >= px - 1e-9)) if seg else 0.0
                        share = data.part2.get((bk, sc), data.part.get(bk, 0.0)) if spread_fill else data.part.get(bk, 0.0)
                        if prop:
                            share = min(1.0, share * q / max(1.0, data.qref.get(bk, q)))
                        eq = min(q, share * thr)
                        pfill = 1.0 if eq > 0 else 0.0
                    else:
                        lam = data.hazard.get(bk, 0.0)
                        pfill = 1.0 - math.exp(-lam * dt_h)
                        eq = pfill * q * data.fillfrac.get(bk, 0.85)
                    if eq <= 0:
                        continue
                    if side == "yes":
                        inv += eq; cash -= eq * px
                    else:
                        inv -= eq; cash += eq * px
                    o[2] = q - eq
                    n_fill_events += pfill; fill_qty += eq; fill_since = True
                    lvl_fill[o[3]] += eq
                    k30 = idx30[gi]
                    if 0 <= k30 < i1:
                        m30 = snaps[k30][5]
                        mo = ((m30 - px) if side == "yes" else (px - m30)) / TICK
                        markouts.append(mo)
                        mo_w[0] += mo * eq; mo_w[1] += eq
                        lvl_mo[o[3]][0] += mo * eq; lvl_mo[o[3]][1] += eq
                    # fill P&L marked 5 / 30 minutes later (fill time ~ the interval midpoint; mark = first snapshot after)
                    tf = 0.5 * (ts + t_next)
                    for h, acc in ((300.0, 0), (1800.0, 1)):
                        mh = data.mid_at(ex, tf + h)
                        if mh is None or tf + h > t_to + 1800.0:
                            mh = rows[-1][5] if h == 300.0 else mh
                        if mh is None:
                            continue
                        v = eq * ((mh - px) if side == "yes" else (px - mh))
                        if acc == 0:
                            edge5 += v
                        else:
                            edge30 += v
                    if keep:
                        fills_out.append((tf, ex, side, px, eq, mid, sc, o[3], bq if side == "yes" else aq, xvf))
                orders = [o for o in orders if o[2] >= 1]
                inv_coll = abs(inv) * (mid if inv > 0 else (1 - mid))
                collateral = inv_coll + sum(o[2] * (o[1] if o[0] == "yes" else 1 - o[1]) for o in orders)
                cap_hours += collateral * dt_h
                max_inv_shares = max(max_inv_shares, abs(inv)); max_inv_notional = max(max_inv_notional, inv_coll)
                coll_by_ex[ex] = inv_coll
                max_coll_total = max(max_coll_total, sum(coll_by_ex.values()))
            ex_pnl = cash + inv * mid
            path[ts] = path.get(ts, 0.0) + (ex_pnl - ex_pnl_prev)
            ex_pnl_prev = ex_pnl
        pnl_total += ex_pnl_prev
    cum = 0.0
    for t in sorted(path):
        cum += path[t]
        peak = max(peak, cum); dd = max(dd, peak - cum)
    cap_days = cap_hours / 24.0
    out = dict(pnl=pnl_total, cap_days=cap_days, drawdown=dd, score=S._score(pnl_total, cap_days), fill_events=round(n_fill_events, 1),
               fill_qty=round(fill_qty), markout30_ticks=(mo_w[0] / mo_w[1] if mo_w[1] > 0 else None), n_markout=len(markouts),
               by_level={lv: {"fill_qty": round(lvl_fill[lv]), "markout30_ticks": (round(lvl_mo[lv][0] / lvl_mo[lv][1], 2) if lvl_mo[lv][1] else None)}
                         for lv in sorted(lvl_fill)},
               max_inv_shares=round(max_inv_shares), max_inv_notional=round(max_inv_notional), max_inv_collateral_total=round(max_coll_total),
               edge5=edge5, edge30=edge30, fill_qty_raw=fill_qty)
    if keep:
        out["fills"] = fills_out
    return out


def selftest(con, cfg, t0, fit_until, t1) -> bool:
    """simulate_x with no new knobs (and improve_ge = shadow's constant) must equal shadow.simulate_mm."""
    md = MMDataX(con, t0, t1, fit_until, cfg)
    lp = {k.split(".", 1)[1]: v for k, v in S.live_params(cfg).items() if k.startswith("mm.")}
    a = S.simulate_mm(md, fit_until, t1, lp)
    b = simulate_x(md, fit_until, t1, lp)
    keys = ["pnl", "cap_days", "drawdown", "fill_qty", "markout30_ticks", "max_inv_shares"]
    ok = all((a[k] is None and b[k] is None) or abs((a[k] or 0) - (b[k] or 0)) < 1e-6 for k in keys)
    print("selftest", "OK" if ok else "MISMATCH", {k: (a[k], b[k]) for k in keys})
    return ok


if __name__ == "__main__":
    import os
    from ..config import BotConfig
    cfg = BotConfig.load()
    con = S.open_ro(os.path.join(S.ROOT, cfg.db_path))
    t1 = con.execute("SELECT MAX(ts) FROM quotes").fetchone()[0]
    t0 = con.execute("SELECT MIN(ts) FROM quotes").fetchone()[0]
    selftest(con, cfg, t0, t0 + 0.7 * (t1 - t0), t1)
