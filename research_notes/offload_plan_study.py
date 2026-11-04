"""Cheapest way to turn holdings into cash for a "high-frequency punt" (Sizhe, 31 Oct). Read-only, local data only, no SIG calls.

    <venv with numpy+pandas+matplotlib>/bin/python research_notes/offload_plan_study.py          recompute plan.json (~1 min)
    <venv ...>/bin/python research_notes/offload_plan_study.py study                             rerun the impact study (~3 min)
    <venv ...>/bin/python research_notes/offload_plan_study.py plot                              redraw the decision figure

Run the first form the moment a trade is decided: it reads the latest positions and books, so the ordered list is fresh.
The study form needs deploy/simq.sh (heavy); the plan and plot forms do not.

Inputs (all read-only): data/sigbot.sqlite (?mode=ro: fills, cycles, orders, tape, quotes, venue_quotes, venue_fair, arb_trades),
data/books_latest.json, data/books_history/*.json, data/markets.json, params/churn_state.json, config.json.
Outputs: data/research_offload/plan.json, impact.json; research_notes/figures/offload_plan_decision.png.

What it does
  1. Splits the book the way the wind-down does: cheap favourites, catch-up and shock ledgers first, then high-frequency arb
     lots, then whole sets per race, then leftover single legs.
  2. Gives each holding a keep value: what it brings if left alone. Two views:
       as configured   late sets and legs the wind-down sells: its expected exit; early-call sets: payout; cheap favourites and
                       every leg in their races (the wind-down never touches those races): the outside fair at settlement.
       all sold by 18:00 ET   every holding valued at what a sale before the 3 Nov punt would bring, as the punt plan assumes.
  3. Sells greedily, cheapest value given up per unit of cash first, chunk by chunk, sharing each leg's book between holdings
     and refusing any chunk that would leave |net D| above the cap or widen a breach. Speeds:
       one sweep  everything in one pass through today's ladder (seconds; the worst case).
       5 min      sets: 5 baskets 1 min apart, a fresh ladder for each (see `study`: our set exits, alone or in fast runs, left the
                  exit cost unchanged); legs: the better of the ladder and the measured square-root impact, which does not decay.
       1 h        as 5 min, 12 baskets 5 min apart.
       6 h        as 1 h, 12 baskets 30 min apart, each at the race's usual (24-h median) touch cost when today's is worse.
"""
import glob, json, math, os, sqlite3, sys, time
from datetime import datetime, timezone

sys.dont_write_bytecode = True                  # no __pycache__ under sigbot/: this script writes only its own outputs
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import numpy as np
import pandas as pd

from sigbot import risk                                     # noqa: E402
from sigbot.proposals import cheap_favourites as CF         # noqa: E402
from sigbot.proposals import wind_down as WD                # noqa: E402
from sigbot.universe import Universe                        # noqa: E402

OUT = f'{ROOT}/data/research_offload'
FIG = f'{ROOT}/research_notes/figures/offload_plan_decision.png'
TICK = 0.005
CAP = 5000.0                    # partisan cap (config mm.portfolio_max_partisan_notional)
RESERVE = 15000.0               # shock reserve (config default)
TARGETS = (5000, 10000, 20000, 40000, 60000, 80000)
SPEEDS = ('one sweep', '5 min', '1 h', '6 h')
CHUNK_CASH = 250.0              # greedy step, in cash
CLIPS = {'5 min': 5, '1 h': 12, '6 h': 12}  # set baskets per race in the window
SET_TYPES = ('set_late', 'set_early', 'hf_set')
LAW_A, LAW_B = 0.19, 0.55       # fallback impact law: ticks = a * (shares / 1000) ** b; `study` refits it into impact.json
LAW_BAND = (0.08, 0.92)         # the fit covers legs priced here; outside it only the real ladder is used


def db():
    return sqlite3.connect(f'file:{ROOT}/data/sigbot.sqlite?mode=ro', uri=True, timeout=30)


def utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


# ---------------------------------------------------------------- loaders
def universe():
    return Universe.from_markets(json.load(open(f'{ROOT}/data/markets.json')))


def load_books(path=None):
    b = json.load(open(path or f'{ROOT}/data/books_latest.json'))
    return {str(k): v for k, v in b.items()}


def positions(c):
    f = pd.read_sql("select exchange_id x, sum(quantity) q from fills group by 1", c)
    f['x'] = f.x.astype(str)
    return {r.x: float(r.q) for r in f.itertuples() if abs(r.q) >= 1}


def ledger(c, purposes):
    """Open position per leg of a sleeve's own fills."""
    ph = ','.join('?' * len(purposes))
    f = pd.read_sql(f"select exchange_id x, sum(quantity) q from fills where purpose in ({ph}) group by 1", c, params=purposes)
    f['x'] = f.x.astype(str)
    return {r.x: float(r.q) for r in f.itertuples() if abs(r.q) >= 1}


def cheapfav_ledger(c, u):
    rows = pd.read_sql("select order_id, exchange_id, quantity, price from fills where purpose in ('cheapfav','cheapfav_exit') "
                       "and price is not null order by filled_at", c).to_dict('records')
    race_of = {ex: (r[0], r[1]) for ex, r in u.by_exchange.items()}
    open_pos = CF.ledger(rows, race_of)[0]
    return {str(k): float(v) for k, v in open_pos.items() if abs(v) >= 1}


def churn_lots():
    try:
        lots = json.load(open(f'{ROOT}/params/churn_state.json')).get('lots') or []
    except (OSError, ValueError):
        return {}
    out = {}
    for x in lots:
        out[x['race']] = out.get(x['race'], 0) + int(x['qty'])
    return out


def fairs(c, now):
    f = pd.read_sql("select exchange_id x, fair, ts from venue_fair where ts > ? and fair is not null and exchange_id is not null",
                    c, params=(now - 6 * 3600,))
    f['x'] = f.x.astype(str)
    return f.sort_values('ts').groupby('x').fair.last().to_dict()


def own_resting(c, now):
    """Our likely resting orders: (leg, side of the book in YES terms) -> {price: qty}. The orders table never closes most rows,
    so only orders under 30 min old (the MM's ttl) count; an overestimate only makes the walk a little worse."""
    r = pd.read_sql("select exchange_id x, side, price, quantity q from orders where status='open' and placed_at > ?", c,
                    params=(now - 1800,))
    out = {}
    for x in r.itertuples():
        if x.price is None:
            continue
        ybook, px = ('bids', float(x.price)) if x.side == 'yes' else ('asks', round(1.0 - float(x.price), 3))
        d = out.setdefault((str(x.x), ybook), {})
        d[round(px, 3)] = d.get(round(px, 3), 0.0) + float(x.q)
    return out


def netted(book, own, x, side):
    lv = [(float(l['price']), float(l['quantity'])) for l in (book.get(side) or [])]
    me = own.get((x, side)) or {}
    if not me:
        return lv
    return [(p, q - me.get(round(p, 3), 0.0)) for p, q in lv if q - me.get(round(p, 3), 0.0) > 0]


# ---------------------------------------------------------------- holdings
def build_holdings(c, u, books, now):
    pos, cf = positions(c), cheapfav_ledger(c, u)
    cu, sh = ledger(c, ('catchup', 'catchup_exit')), ledger(c, ('shock', 'shock_exit'))
    lots = churn_lots()
    fair = fairs(c, now)
    mids = {}
    for x, b in books.items():
        if b.get('bids') and b.get('asks'):
            mids[x] = 0.5 * (float(b['bids'][0]['price']) + float(b['asks'][0]['price']))
    fv = lambda x: fair.get(x, mids.get(x))
    vq = pd.read_sql("select race, party, ts, bid, ask from venue_quotes where venue='sig' and ts > ?", c, params=(now - 88200.0,))
    legs_of = {r.name: list(r.legs) for r in u.races.values()}
    med = WD.cost_medians(list(vq.itertuples(index=False, name=None)), legs_of, now)
    quoted = set(pd.read_sql("select distinct exchange_id x from orders where purpose='mm' and placed_at > ?", c,
                             params=(now - 3600,)).x.astype(str))
    snap = dict(pos)
    for led in (cf, cu, sh):
        for x, q in led.items():
            snap[x] = snap.get(x, 0.0) - q
    hs, cf_races = [], {u.by_exchange[x][0] for x in cf if x in u.by_exchange}
    for race in u.races.values():
        exs = race.exchange_ids
        n = len(exs)
        hf = lots.get(race.name, 0)
        s2 = {e: snap.get(e, 0.0) for e in exs}
        if hf and n >= 2 and all(s2[e] < 0 for e in exs):
            take = min(hf, int(min(-s2[e] for e in exs)))
            for e in exs:
                s2[e] += take
            wd = _set_keep(race.name, 'sell_set', exs, books, med, n - 1.0)
            hs.append(dict(name=f'{race.name} (high-frequency arb lots)', race=race.name, type='hf_set', kind='sell_set', legs=exs, qty=take,
                           payout=n - 1.0, keep=wd, keep_cash=wd))
        kind, k, res = WD.decompose(exs, s2)
        if kind and k >= 1:
            payout = n - 1.0 if kind == 'sell_set' else 1.0
            favs = [fv(e) for e in exs if fv(e) is not None]
            cls, why = WD.call_class(race.name, max(favs) if favs else None)
            early = cls == 'early' or race.name in cf_races
            sale = _set_keep(race.name, kind, exs, books, med, payout, k)
            stuck = not early and wd_stuck(sale, payout)
            hs.append(dict(name=race.name, race=race.name, type='set_early' if early else 'set_late', kind=kind, legs=exs, qty=k,
                           payout=payout, why=why if race.name not in cf_races else 'cheap-favourites race: the wind-down leaves it alone',
                           keep=payout if early or stuck else sale, keep_cash=sale, wd_stuck=stuck,
                           bot_refuses=race.name in cf_races))     # arb.unwind_* refuse cheap-favourite races: an executor must allow it
        for e, r in res.items():
            party = race.legs and [p for p, l in race.legs.items() if l.exchange_id == e][0]
            hs.append(_leg(race.name, party, e, r, 'leg_held' if race.name in cf_races else 'mm_leg' if e in quoted else 'leg', books, fv(e)))
    for x, q in cf.items():
        if x in u.by_exchange:
            rn, party = u.by_exchange[x]
            hs.append(_leg(rn, party, x, q, 'cheapfav', books, fv(x)))
    for x, q in {**cu, **sh}.items():
        if x in u.by_exchange:
            rn, party = u.by_exchange[x]
            hs.append(_leg(rn, party, x, q, 'other_sleeve', books, fv(x)))
    return [h for h in hs if h and h['qty'] >= 1], pos, dict(cf=cf, catchup=cu, shock=sh, lots=lots), fair, med, mids


def wd_stuck(sale, payout):
    """True when the wind-down's last cost cap (5.5% of proceeds) rules out the set's usual exit: it sells only on dips, if at all."""
    return (payout - sale) / sale > WD.CAP_END_FRAC + 1e-9


def _set_keep(race, kind, exs, books, med, payout, held=1000):
    """What the wind-down should bring per set: payout less the race's usual (24-h median) touch exit cost, today's if better or no
    median, less the walk of its own clip (at most 1,000 sets, shrunk to a 1-tick walk)."""
    m24 = (med.get(race) or {}).get(kind, (None, None))[1]
    now_cost = WD.touch_cost(kind, books, exs)
    usual = now_cost if m24 is None else m24 if now_cost is None else m24         # the usual level, not tonight's dip
    walk = 0.0
    q, wq = WD.clip_quote(kind, books, exs, min(held, 1000), TICK, min(held, 100))
    if wq:
        walk = max(0.0, wq['walk'])
    return payout - (usual if usual is not None else 0.03 * payout) - walk


def _leg(race, party, x, q, typ, books, fair):
    b = books.get(x) or {}
    side = 'bids' if q > 0 else 'asks'
    lv = b.get(side) or []
    if not lv:
        return None
    touch = float(lv[0]['price']) if q > 0 else 1.0 - float(lv[0]['price'])
    fv = None if fair is None else (fair if q > 0 else 1.0 - fair)
    # a sale before the punt brings about today's touch: the gap to the fair lasts (review: gaps over 3 ticks closed to 2 on 0.4% of polls).
    # The wind-down sells a leg at the touch only if the gap fits its last cost cap, max(3 ticks, 5.5% of the price); otherwise it rides.
    wd = touch
    rides = typ in ('cheapfav', 'leg_held') or (fv is not None and fv - touch > max(3 * TICK, WD.CAP_END_FRAC * touch) + 1e-9)
    keep = (fv if fv is not None else touch) if rides else wd      # riding to settlement: the fair
    return dict(name=f'{race} {party} {"YES" if q > 0 else "NO"}', race=race, type=typ, kind='yes' if q > 0 else 'no', legs=[x],
                qty=int(abs(q)), payout=None, keep=keep, keep_cash=wd, fair=fv, touch=touch)


# ---------------------------------------------------------------- pricing
class Ladder:
    """One leg's closing side: price per share received for the next share at cumulative quantity Q, walking the netted ladder.
    `cursor` is what this speed has already taken from the leg; holdings share it."""

    def __init__(self, levels, sell_yes, law=None, mid=None):
        self.px = np.array([p if sell_yes else 1.0 - p for p, _ in levels], float)        # cash per share at each level
        self.cum = np.cumsum([q for _, q in levels], dtype=float)
        self.cursor = 0.0
        self.law = law
        self.touch = float(self.px[0]) if len(self.px) else None

    def _book_avg(self, a, b):
        """Average cash per share from cumulative a to b through the real ladder; what lies beyond the book gets nothing."""
        if b <= a:
            return 0.0
        lo = np.concatenate(([0.0], self.cum[:-1]))
        take = np.clip(np.minimum(b, self.cum) - np.maximum(a, lo), 0.0, None)
        return float((take * self.px).sum() / (b - a))

    def _law_avg(self, a, b):
        ca, cb = self.law
        f = lambda s: ca * (s / 1000.0) ** cb * TICK                                       # displacement at cumulative s
        total = (1000.0 / (cb + 1)) * (f(b) * (b / 1000.0) - f(a) * (a / 1000.0))          # integral of f over [a, b]
        return self.touch - total / (b - a)

    def avg(self, q):
        a, b = self.cursor, self.cursor + q
        v = self._book_avg(a, b)
        return max(v, self._law_avg(a, b)) if self.law else v

    def take(self, q):
        self.cursor += q


def set_tranche(h, d, ladders, clips, better):
    """Cash per set for d more sets of holding h. With clips > 1 each basket sees a fresh ladder, so the sets are spread over the
    first holding/clips of each leg's ladder. `better` is what 6 h gains per set by waiting for the race's usual touch cost."""
    tot = 0.0
    for e in h['legs']:
        l = ladders[e]
        if clips > 1:
            done = h['done'] / clips
            tot += l._book_avg(done, done + d / clips)
        else:
            tot += l.avg(d)
    return tot + better


def run_speed(speed, holdings, books, own, pos, u, med, prices, law, allow=lambda h: True, keep='keep', cap=CAP):
    """Greedy: at each step sell the chunk with the lowest value given up per unit of cash. Returns the chunks in order."""
    ladders = {}
    leg_side = {}
    for h in holdings:
        for e in h['legs']:
            sell_yes = h['kind'] in ('yes', 'buy_set')
            key = (e, sell_yes)
            if key not in leg_side:
                lv = netted(books.get(e) or {}, own, e, 'bids' if sell_yes else 'asks')
                mid = prices.get(e, {}).get('mid')
                use_law = law if speed != 'one sweep' and mid is not None and LAW_BAND[0] <= mid <= LAW_BAND[1] else None
                leg_side[key] = Ladder(lv, sell_yes, use_law, mid)
    p = dict(pos)
    hs = [dict(h, done=0.0, v=h[keep], cancels=sum(1 for e in h['legs'] if (e, 'bids') in own or (e, 'asks') in own)) for h in holdings if allow(h)]
    for h in hs:
        h['ladders'] = {e: leg_side[(e, h['kind'] in ('yes', 'buy_set'))] for e in h['legs']}
    netd = risk.exposure(u, p, prices).net_d_notional
    out, blocked = [], set()
    clips = CLIPS.get(speed, 1)

    def quote(h):
        left = h['qty'] - h['done']
        if left < 1 or any(len(l.px) == 0 for l in h['ladders'].values()):
            return None
        unit = max(h['v'], 0.02)
        d = float(min(left, max(10.0, math.ceil(CHUNK_CASH / unit))))
        if h['type'] in SET_TYPES:
            better = 0.0
            if speed == '6 h':
                m24 = (med.get(h['race']) or {}).get(h['kind'], (None, None))[1]
                tc = WD.touch_cost(h['kind'], books, h['legs'])
                better = 0.0 if m24 is None or tc is None else max(0.0, tc - m24)
            cash = d * set_tranche(h, d, h['ladders'], clips, better)
        else:
            cash = d * h['ladders'][h['legs'][0]].avg(d)
        if cash <= 0:
            return None
        return d, cash, d * h['v'] - cash

    while True:
        best = None
        for i, h in enumerate(hs):
            if i in blocked:
                continue
            q = quote(h)
            if q is None:
                continue
            d, cash, given = q
            r = given / cash
            if best is None or r < best[0]:
                best = (r, i, d, cash, given)
        if best is None:
            break
        _, i, d, cash, given = best
        h = hs[i]
        p2 = dict(p)
        for e in h['legs']:
            p2[e] = p2.get(e, 0.0) + (-d if h['kind'] in ('yes', 'buy_set') else d)
        nd2 = risk.exposure(u, p2, prices).net_d_notional
        if abs(nd2) > cap + 1e-6 and abs(nd2) >= abs(netd) - 1e-6:
            blocked.add(i)                       # the cap: try the others; it may reopen once they move net D
            continue
        blocked.clear()
        p, netd = p2, nd2
        if clips == 1 or h['type'] not in SET_TYPES:
            for e in h['legs']:
                h['ladders'][e].take(d)
        h['done'] += d
        out.append(dict(i=i, name=h['name'], type=h['type'], d=d, cash=cash, given=given, netd=nd2,
                        lim={e: h['ladders'][e].px[np.searchsorted(h['ladders'][e].cum, h['done'] / clips if clips > 1 and h['type'] in SET_TYPES
                                                                   else h['ladders'][e].cursor).clip(0, len(h['ladders'][e].px) - 1)] for e in h['legs']}))
    return out, hs


def summarise(chunks, hs, targets, clips):
    res = []
    for T in targets:
        got, given, spread, rows, nd = 0.0, 0.0, 0.0, {}, None
        for c in chunks:
            if got >= T:
                break
            f = min(1.0, (T - got) / c['cash'])
            got += c['cash'] * f
            given += c['given'] * f
            nd = c['netd']
            h = hs[c['i']]
            r = rows.setdefault(c['i'], dict(step=len(rows) + 1, cancels=h['cancels'], bot_refuses=h.get('bot_refuses', False), wd_stuck=h.get('wd_stuck', False), holding=c['name'], type=c['type'], qty=0.0, cash=0.0, given_up=0.0,
                                             vs_payout_or_fair=0.0, keep_per_unit=round(h['v'], 4), legs=h['legs'], worst_cash_per_share={}))
            r['qty'] += c['d'] * f
            r['cash'] += c['cash'] * f
            r['given_up'] += c['given'] * f
            r['worst_cash_per_share'] = {e: round(float(v), 3) for e, v in c['lim'].items()}
        for r in rows.values():
            h = hs[[i for i, v in rows.items() if v is r][0]]
            r['vs_payout'] = None if h.get('payout') is None else round(r['qty'] * h['payout'] - r['cash'], 1)
            r['qty'] = int(round(r['qty']))
            r['cash'], r['given_up'] = round(r['cash'], 1), round(r['given_up'], 1)
            r['cost_pct_of_cash'] = round(100 * r['given_up'] / r['cash'], 2) if r['cash'] else None
            r.pop('vs_payout_or_fair')
        full = got >= T - 0.5
        n_legs = sum(1 for r in rows.values() if r['type'] not in SET_TYPES)
        baskets = sum(min(clips, max(1, math.ceil(r['qty'] / 500))) for r in rows.values() if r['type'] in SET_TYPES)
        res.append(dict(target=T, raised=round(got, 1), reached=bool(full), value_given_up=round(given, 1),
                        writes=math.ceil(1.25 * baskets) + math.ceil(n_legs / 50) + sum(r['cancels'] for r in rows.values()),   # 26% of past baskets failed all-or-none
                        set_baskets=baskets, single_leg_orders=n_legs,
                        below_payout_sets=round(sum(r['vs_payout'] or 0.0 for r in rows.values())),
                        cost_per_1000=round(1000 * given / got, 2) if got else None, net_d_after=None if nd is None else round(nd),
                        n_holdings=len(rows), order=list(rows.values())))
    return res


def plan():
    t0 = time.time()
    c = db()
    u = universe()
    books = load_books()
    meta = json.load(open(f'{ROOT}/data/books_latest.meta.json'))
    cyc = c.execute("select ts, cash, positions_value, total_value, n_positions from cycles order by cycle_id desc limit 1").fetchone()
    now = cyc[0]
    holdings, pos, ledgers, fair, med, mids = build_holdings(c, u, books, now)
    own = own_resting(c, now)
    prices = {x: dict(bestBid=float(b['bids'][0]['price']) if b.get('bids') else None,
                      bestAsk=float(b['asks'][0]['price']) if b.get('asks') else None, mid=mids.get(x)) for x, b in books.items()}
    law = (LAW_A, LAW_B)
    imp = None
    if os.path.exists(f'{OUT}/impact.json'):
        imp = json.load(open(f'{OUT}/impact.json'))
        law = tuple(imp['law'].get('1h') or law)
    nd0 = risk.exposure(u, pos, prices).net_d_notional
    by_type = {}
    for h in holdings:
        cash = 0.0
        for e in h['legs']:
            lv = (books.get(e) or {}).get('bids' if h['kind'] in ('yes', 'buy_set') else 'asks') or []
            if lv:
                px = float(lv[0]['price'])
                cash += h['qty'] * (px if h['kind'] in ('yes', 'buy_set') else 1.0 - px)
        h['touch_value'] = cash
        if h['type'] in SET_TYPES:
            tc = WD.touch_cost(h['kind'], books, h['legs'])
            h['touch_cost_ticks'] = None if tc is None else round(tc / TICK, 1)
            m = (med.get(h['race']) or {}).get(h['kind'], (None, None))
            h['usual_cost_ticks'] = None if m[1] is None else round(m[1] / TICK, 1)
        h['keep_value'] = h['qty'] * h['keep']
        d = by_type.setdefault(h['type'], dict(n=0, touch_value=0.0, keep_value=0.0))
        d['n'] += 1
        d['touch_value'] += cash
        d['keep_value'] += h['keep_value']
    tie = sum(h['touch_value'] for h in holdings)
    expo = risk.exposure(u, pos, prices)
    room = dict(net_d=round(expo.net_d_notional), long_d=round(expo.long_d), short_d=round(expo.short_d), d_room_now=round(CAP - expo.net_d_notional),
                r_room_now=round(CAP + expo.net_d_notional), d_room_if_all_long_d_sold=round(CAP + expo.short_d), r_room_if_all_short_d_sold=round(CAP + expo.long_d))
    # room for a D or R punt bought by selling only the single legs that lean the other way, cheapest value given up per unit of room first
    lean = []
    for h in holdings:
        if h['type'] in SET_TYPES or h['type'] == 'other_sleeve':
            continue
        e = h['legs'][0]
        party = u.by_exchange[e][1]
        if party == 'I':
            continue
        m = prices[e]['mid']
        d = h['qty'] * (m if h['kind'] == 'yes' else 1.0 - m) * (1 if (party == 'D') == (h['kind'] == 'yes') else -1)
        lean.append((d, h['touch_value'], h['keep_value'] - h['touch_value'], h['name'], h['qty']))
    for side, sg in (('D', 1), ('R', -1)):
        xs = sorted([x for x in lean if x[0] * sg > 0], key=lambda x: x[2] / abs(x[0]))
        r0, cum, cash, tiers, wanted = room['d_room_now'] if sg > 0 else room['r_room_now'], 0.0, 0.0, [], [5000, 10000, 15000]
        room[f'make_{side}_room_order'] = [dict(holding=x[3], qty=x[4], cash=round(x[1], 1), given_up=round(x[2], 1), room_gained=round(abs(x[0]))) for x in xs]
        for d, tv, given, _, _ in xs:
            r0 += abs(d)
            cum += given
            cash += tv
            while wanted and r0 >= wanted[0]:
                tiers.append(dict(room=wanted.pop(0), value_given_up=round(cum), cash_raised=round(cash)))
        room[f'make_{side}_room'] = tiers
    routes = {}
    for typ in sorted({h['type'] for h in holdings}):
        hh = [h for h in holdings if h['type'] == typ]
        row = dict(value_at_touch=round(sum(h['touch_value'] for h in hh)))
        for s_, allow in (('sweep', 'one sweep'), ('baskets', '5 min')):
            ch, hx = run_speed(allow, hh, books, own, pos, u, med, prices, law, keep='keep', cap=1e12)
            cash, giv = sum(x['cash'] for x in ch), sum(x['given'] for x in ch)
            ladder = cash - sum(h['touch_value'] for h in hh)
            row[s_] = dict(cash=round(cash), given_up=round(giv), lost_to_walking=round(-ladder))
        tc = 0.0
        for h in hh:
            caps = []
            for e in h['legs']:
                yes = h['kind'] in ('yes', 'buy_set')
                lv = netted(books.get(e) or {}, own, e, 'bids' if yes else 'asks')
                caps.append(lv[0][1] if lv else 0.0)
            tc += min([h['qty']] + caps) * h['touch_value'] / max(h['qty'], 1)
        row['cash_at_touch_only'] = round(tc)
        routes[typ] = row
    allow_default = lambda h: h['type'] not in ('other_sleeve', 'hf_set')
    speeds, curves, cash_view = {}, {}, {}
    for s in SPEEDS:
        chunks, hs = run_speed(s, holdings, books, own, pos, u, med, prices, law, allow_default)
        speeds[s] = summarise(chunks, hs, TARGETS, CLIPS.get(s, 1))
        cum = np.cumsum([x['cash'] for x in chunks])
        giv = np.cumsum([x['given'] for x in chunks])
        step = max(1, len(chunks) // 300)
        typ = {}
        for x in chunks:
            typ.setdefault(x['type'], np.zeros(len(chunks)))
        for j, x in enumerate(chunks):
            typ[x['type']][j] = x['cash']
        curves[s] = dict(cash=[round(float(v), 1) for v in cum[::step]], given=[round(float(v), 1) for v in giv[::step]],
                         netd=[round(float(x['netd'])) for x in chunks[::step]], max_cash=round(float(cum[-1]), 1) if len(cum) else 0.0,
                         by_type={k: [round(float(v), 1) for v in np.cumsum(a)[::step]] for k, a in typ.items()})
        ch2, hs2 = run_speed(s, holdings, books, own, pos, u, med, prices, law, allow_default, keep='keep_cash')
        cash_view[s] = [{k: v for k, v in r.items() if k != 'order'} for r in summarise(ch2, hs2, TARGETS, CLIPS.get(s, 1))]
        c2, g2 = np.cumsum([x['cash'] for x in ch2]), np.cumsum([x['given'] for x in ch2])
        curves[s]['all_sold'] = dict(cash=[round(float(v), 1) for v in c2[::step]], given=[round(float(v), 1) for v in g2[::step]])
    out = dict(built=utc(time.time()), positions_as_of=utc(now), books_as_of=meta.get('utc'), book_age_min=round((now - meta['ts']) / 60, 1),
               cash=round(cyc[1], 1), spendable_after_reserve=round(cyc[1] - RESERVE, 1), positions_mark=round(cyc[2], 1), account_mark=round(cyc[3], 1),
               n_positions_fills=len(pos), n_positions_bot=cyc[4], net_d_now=round(nd0), cap=CAP, reserve=RESERVE, impact_law_ticks=law,
               holdings_by_type={k: {a: round(b, 1) for a, b in v.items()} for k, v in by_type.items()},
               holdings=[{k: (round(v, 4) if isinstance(v, float) else v) for k, v in h.items() if k in
                          ('name', 'race', 'type', 'kind', 'legs', 'qty', 'payout', 'keep', 'keep_cash', 'fair', 'touch', 'touch_value', 'keep_value', 'why', 'touch_cost_ticks', 'usual_cost_ticks', 'wd_stuck', 'bot_refuses')}
                         for h in sorted(holdings, key=lambda x: -x['touch_value'])],
               holdings_value_at_touch=round(tie, 1), partisan=room, routes=routes, speeds=speeds, speeds_all_sold_by_1800=cash_view, curves=curves)
    os.makedirs(OUT, exist_ok=True)
    tmp = f'{OUT}/plan.json.tmp'
    with open(tmp, 'w') as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, f'{OUT}/plan.json')
    print(f"positions {out['positions_as_of']}, books {out['books_as_of']} (age {out['book_age_min']} min), cash {out['cash']:.0f}, net D {nd0:+.0f}, "
          f"{len(pos)} legs (bot says {cyc[4]}), {time.time() - t0:.0f} s")
    print('partisan:', room)
    print('routes:', routes)
    print('holdings:', {k: (v['n'], round(v['touch_value']), round(v['keep_value'])) for k, v in by_type.items()})
    for s in SPEEDS:
        print(s, 'max cash', curves[s]['max_cash'])
        for r, r2 in zip(speeds[s], cash_view[s]):
            print(f"  {r['target']:>6}: raised {r['raised']:>8.0f} give up {r['value_given_up']:>7.0f} = {r['cost_per_1000'] or 0:>6.1f} per 1k, "
                  f"{r['n_holdings']:>3} holdings, net D {r['net_d_after']}; if all sold by 18:00 ET anyway {r2['value_given_up']:>6.0f}")
    return out


# ---------------------------------------------------------------- study: how the ladder behaves
def tape_frame(c, since):
    t = pd.read_sql("select exchange_id x, price p, quantity q, executed_at e, aggressor a from tape where executed_at > ?", c, params=(since,))
    t['ts'] = (pd.to_datetime(t.e, utc=True, format='ISO8601') - pd.Timestamp('1970-01-01', tz='UTC')).dt.total_seconds()
    t = t.dropna(subset=['a'])
    t['x'] = t.x.astype(str)
    t['sv'] = t.q * np.where(t.a == 'buy', 1, -1)
    return t


def flow_impact(t, q, vf):
    """Mid change against signed public flow in fixed windows, where the outside fair moved at most 1 tick: does impact decay?"""
    bins = [0, 3000, 10000, 30000, 1e9]
    out, pts = {}, {}
    for W, name in ((300, '5 min'), (1800, '30 min'), (3600, '1 h'), (6 * 3600, '6 h')):
        rows = []
        for x, g in q.groupby('x'):
            if not g.m.between(*LAW_BAND).median() > 0.5:
                continue
            m = g.groupby(np.floor(g.ts / W)).m.agg(['first', 'last'])
            tt = t[t.x == x]
            sv = tt.groupby(np.floor(tt.ts / W)).sv.sum().rename('sv')
            f = vf[vf.x == x]
            ff = f.groupby(np.floor(f.ts / W)).fair.agg(fa='first', fb='last')
            rows.append(m.join(sv).join(ff).fillna({'sv': 0.0}))
        d = pd.concat(rows)
        d['dm'] = np.sign(d.sv) * (d['last'] - d['first']) / TICK
        d = d[((d.fb - d.fa).abs() / TICK <= 1) & (d.dm.abs() < 40) & (d.sv.abs() >= 1000)]
        d['b'] = pd.cut(d.sv.abs(), bins)
        g = d.groupby('b', observed=True)
        tab = pd.DataFrame(dict(n=g.size(), shares=g.sv.apply(lambda s: s.abs().mean()), ticks=g.dm.mean(), se=g.dm.std() / np.sqrt(g.size())))
        slope, lna = np.polyfit(np.log(tab.shares / 1000), np.log(tab.ticks.clip(lower=0.02)), 1)
        out[name] = tab.round(3).reset_index(drop=True).to_dict('records')
        pts[name] = (round(float(np.exp(lna)), 3), round(float(slope), 3))
    return out, pts


def sweeps(t, q):
    """Public sweeps of >= 3,000 shares (one side, gaps <= 20 s): the eaten touch and its size at 30 s to 6 h."""
    t = t.sort_values(['x', 'ts']).copy()
    t['new'] = (t.groupby('x').ts.diff().fillna(1e9) > 20) | (t.groupby('x').a.shift() != t.a)
    t['cl'] = t.new.cumsum()
    cl = t.groupby('cl').agg(x=('x', 'first'), a=('a', 'first'), t0=('ts', 'min'), t1=('ts', 'max'), q=('q', 'sum'), lo=('p', 'min'), hi=('p', 'max'))
    cl = cl[cl.q >= 3000]
    qq = {k: g.sort_values('ts') for k, g in q.groupby('x')}
    taus = (30, 120, 300, 1800, 3600, 21600)
    rows = []
    for r in cl.itertuples():
        g = qq.get(r.x)
        if g is None:
            continue
        ts = g.ts.values
        i0 = np.searchsorted(ts, r.t0) - 1
        if i0 < 0 or r.t0 - ts[i0] > 90 or not LAW_BAND[0] < g.m.values[i0] < LAW_BAND[1]:
            continue
        s = 1 if r.a == 'buy' else -1
        px, sz = (g.a.values, g.aq.values) if s > 0 else (g.b.values, g.bq.values)
        sub = g[(g.ts > r.t0 - 1800) & (g.ts < r.t0)]
        d = dict(q=r.q, walk=(r.hi - r.lo) / TICK, usual=(sub.aq if s > 0 else sub.bq).median())
        for tau in taus:
            j = np.searchsorted(ts, r.t1 + tau)
            if j < len(ts) and ts[j] - (r.t1 + tau) < 120:
                d[f'p{tau}'], d[f's{tau}'] = s * (px[j] - px[i0]) / TICK, sz[j]
        rows.append(d)
    e = pd.DataFrame(rows)
    e['b'] = pd.cut(e.q, [3000, 10000, 30000, 1e9])
    res = {}
    for b, g in e.groupby('b', observed=True):
        res[str(b)] = dict(n=len(g), walk=round(g.walk.mean(), 2), touch_moved={str(k): round(g[f'p{k}'].mean(), 2) for k in taus},
                           touch_size_vs_usual={str(k): round(float((g[f's{k}'] / g.usual).median()), 2) for k in taus})
    return res


def set_exit_series(v, race):
    g = v[v.race == race]
    a = g.pivot_table(index='ts', columns='party', values='ask').sort_index().ffill().sum(axis=1, skipna=False)
    b = g.pivot_table(index='ts', columns='party', values='bid').sort_index().ffill().sum(axis=1, skipna=False)
    return a, b


def own_set_exits(c):
    """Touch exit cost before and after our own set exits, one by one and in runs (gaps <= 1 h)."""
    a = pd.read_sql("select ts, race, kind, quantity q, cost_per_set p from arb_trades where ok=1 and kind like 'unwind%' order by race, ts", c)
    v = pd.read_sql("select ts, race, party, bid, ask from venue_quotes where venue='sig'", c)
    ser = {r: set_exit_series(v, r) for r in a.race.unique()}

    def delta(race, kind, t0, t1):
        x = ser[race][0] if kind == 'unwind_set' else -ser[race][1]
        ts = x.index.values
        i = np.searchsorted(ts, t0) - 1
        j = np.searchsorted(ts, t1, side='right') - 1
        return None if i < 0 or t0 - ts[i] > 900 else float(x.values[j] - x.values[i]) / TICK
    one = []
    for r in a[a.q >= 500].itertuples():
        d = dict(q=r.q)
        for tau in (120, 300, 600, 1800, 3600):
            d[f'm{tau}'] = delta(r.race, r.kind, r.ts - 1, r.ts + tau)
        one.append(d)
    one = pd.DataFrame(one).dropna()
    a['g'] = (a.groupby('race').ts.diff().fillna(1e9) > 3600).cumsum()
    s = a.groupby('g').agg(race=('race', 'first'), kind=('kind', 'first'), t0=('ts', 'min'), t1=('ts', 'max'), n=('q', 'size'), q=('q', 'sum'), pfirst=('p', 'first'), plast=('p', 'last'))
    s = s[(s.n >= 2) & (s.q >= 1500)].sort_values('q', ascending=False)
    runs = []
    for r in s.itertuples():
        runs.append(dict(race=r.race, baskets=r.n, sets=int(r.q), minutes=round((r.t1 - r.t0) / 60, 1), cost_change_ticks=delta(r.race, r.kind, r.t0 - 1, r.t1 + 90),
                         cost_change_30min_after=delta(r.race, r.kind, r.t0 - 1, r.t1 + 1800), first_price=round(r.pfirst, 4), last_price=round(r.plast, 4)))
    rr = pd.DataFrame(runs).dropna(subset=['cost_change_ticks'])
    return dict(n_single=len(one), single_mean_sets=round(one.q.mean()), single_ticks={k[1:]: round(one[k].mean(), 2) for k in one.columns if k != 'q'},
                single_se={k[1:]: round(one[k].std() / np.sqrt(len(one)), 2) for k in one.columns if k != 'q'},
                n_runs=len(rr), runs_mean_change=round(rr.cost_change_ticks.mean(), 2), runs_se=round(rr.cost_change_ticks.std() / np.sqrt(len(rr)), 2),
                runs_sets_weighted=round((rr.cost_change_ticks * rr.sets).sum() / rr.sets.sum(), 2), runs=rr.to_dict('records'))


def validate_model(c, u):
    """Our real set exits within 3 h of a saved book, against the ladder-walk price on that book, per set in ticks."""
    a = pd.read_sql("select ts, race, kind, quantity q, cost_per_set p from arb_trades where ok=1 and kind like 'unwind%' and quantity >= 200", c)
    snaps = {}
    for f in sorted(glob.glob(f'{ROOT}/data/books_history/books_*.json')):
        snaps[datetime.strptime(os.path.basename(f)[6:-5], '%Y%m%dT%H%MZ').replace(tzinfo=timezone.utc).timestamp()] = f
    keys = np.array(sorted(snaps))
    rows, cache = [], {}
    for r in a.itertuples():
        if r.race not in u.races:
            continue
        k = keys[np.abs(keys - r.ts).argmin()]
        if abs(k - r.ts) > 3 * 3600:
            continue
        b = cache.setdefault(k, load_books(snaps[k]))
        exs = u.races[r.race].exchange_ids
        tot, touch = 0.0, 0.0
        ok = True
        for e in exs:
            lv = [(float(l['price']), float(l['quantity'])) for l in ((b.get(e) or {}).get('asks') or [])]
            if not lv:
                ok = False
                break
            l = Ladder(lv, False)
            tot += l.avg(float(r.q))
            touch += 1.0 - lv[0][0]
        if ok:
            rows.append(dict(race=r.race, q=r.q, gap_h=round(abs(k - r.ts) / 3600, 1), actual=r.p, model=tot, touch=touch))
    d = pd.DataFrame(rows)
    if d.empty:
        return dict(n=0)
    d['err_ticks'] = (d.actual - d.model) / TICK
    return dict(n=len(d), mean_err_ticks=round(d.err_ticks.mean(), 2), median_err=round(d.err_ticks.median(), 2), sd=round(d.err_ticks.std(), 2),
                big=d[d.q >= 1000].round(4).to_dict('records'))


def book_stability(plan_json):
    """Whole-holding sweep cost (ticks a set beyond a zero-cost exit) on every saved book, for the biggest sets and the median leg."""
    files = sorted(glob.glob(f'{ROOT}/data/books_history/books_*.json'))
    tops = [h for h in plan_json['holdings'] if h['type'] in ('set_late', 'set_early')][:3]
    res = {h['name']: [] for h in tops}
    depth = []
    held = sorted({e for h in plan_json['holdings'] for e in h['legs']})
    for f in files:
        b = load_books(f)
        d = []
        for x in held:
            bk = b.get(x) or {}
            if bk.get('bids') and bk.get('asks'):
                t = float(bk['bids'][0]['price'])
                d.append(sum(float(l['quantity']) for l in bk['bids'] if float(l['price']) >= t - 5 * TICK))
        depth.append((os.path.basename(f)[6:19], float(np.median(d)) if d else None))
        for h in tops:
            q, tot, touch, ok = float(h['qty']), 0.0, 0.0, True
            yes = h['kind'] == 'buy_set'
            for e in h['legs']:
                lv = [(float(l['price']), float(l['quantity'])) for l in ((b.get(e) or {}).get('bids' if yes else 'asks') or [])]
                if not lv:
                    ok = False
                    break
                tot += Ladder(lv, yes).avg(q)
                touch += lv[0][0] if yes else 1.0 - lv[0][0]
            if ok:
                res[h['name']].append(((h['payout'] - tot) / TICK, (h['payout'] - touch) / TICK))
    out = {}
    for k, v in res.items():
        a = np.array(v)
        out[k] = dict(n=len(a), sweep_cost_ticks_p10_50_90=[round(float(x), 1) for x in np.percentile(a[:, 0], [10, 50, 90])], latest=round(float(a[-1, 0]), 1),
                      touch_cost_ticks_p10_50_90=[round(float(x), 1) for x in np.percentile(a[:, 1], [10, 50, 90])], latest_touch=round(float(a[-1, 1]), 1))
    return out, depth


def depth_now(plan_json):
    b, d = load_books(), []
    for x in sorted({e for h in plan_json['holdings'] for e in h['legs']}):
        bk = b.get(x) or {}
        if bk.get('bids') and bk.get('asks'):
            t = float(bk['bids'][0]['price'])
            d.append(sum(float(l['quantity']) for l in bk['bids'] if float(l['price']) >= t - 5 * TICK))
    return float(np.median(d))


def robust(c, u, now, n=16):
    """Today's positions priced on each of the last saved books: is tonight's cost typical?"""
    out = []
    law = tuple(json.load(open(f'{OUT}/impact.json'))['law']['1h']) if os.path.exists(f'{OUT}/impact.json') else (LAW_A, LAW_B)
    files = sorted(glob.glob(f'{ROOT}/data/books_history/books_*.json'))[-n:] + [f'{ROOT}/data/books_latest.json']
    for f in files:
        b = load_books(f)
        hs, pos, _, _, med, mids = build_holdings(c, u, b, now)
        prices = {x: dict(bestBid=float(k['bids'][0]['price']) if k.get('bids') else None, bestAsk=float(k['asks'][0]['price']) if k.get('asks') else None,
                          mid=mids.get(x)) for x, k in b.items()}
        row = dict(book=os.path.basename(f)[:-5])
        for sp in ('one sweep', '5 min'):
            ch, hx = run_speed(sp, hs, b, {}, pos, u, med, prices, law, lambda h: h['type'] not in ('other_sleeve', 'hf_set'))
            for r in summarise(ch, hx, (20000, 60000), CLIPS.get(sp, 1)):
                row[f"{sp} {r['target']}"] = r['value_given_up']
            ch2, hx2 = run_speed(sp, hs, b, {}, pos, u, med, prices, law, lambda h: h['type'] not in ('other_sleeve', 'hf_set'), keep='keep_cash')
            for r in summarise(ch2, hx2, (20000, 60000), CLIPS.get(sp, 1)):
                row[f"{sp} {r['target']} all sold"] = r['value_given_up']
        out.append(row)
    return out


def maker(t, plan_json, now):
    """Public aggressor flow per hour on the side that would fill a resting exit, last 72 h, for the biggest single legs and set legs."""
    rec = t[t.ts > now - 72 * 3600]
    rows = []
    for h in plan_json['holdings'][:25]:
        for e in h['legs']:
            yes = h['kind'] in ('yes', 'buy_set')
            g = rec[(rec.x == e) & (rec.a == ('buy' if yes else 'sell'))]
            rows.append(dict(holding=h['name'], leg=e, flow_per_hour=round(float(g.q.sum()) / 72), median_print=float(g.q.median()) if len(g) else 0.0))
    return rows


def hour_of_day(c, plan_json, now):
    v = pd.read_sql("select ts, race, party, bid, ask from venue_quotes where venue='sig' and ts > ?", c, params=(now - 5 * 86400,))
    out = {}
    for h in [x for x in plan_json['holdings'] if x['type'] in ('set_late', 'set_early')][:3]:
        a, b = set_exit_series(v, h['race'])
        x = (a - 1.0 if h['kind'] == 'sell_set' else 1.0 - b) / TICK
        x = x.dropna()
        s = pd.Series(x.values, index=pd.to_datetime(x.index, unit='s', utc=True)).resample('5min').last().ffill()
        out[h['name']] = {str(k): round(float(v_), 1) for k, v_ in s.groupby(s.index.hour // 4 * 4).median().items()}
    return out


def study():
    c = db()
    u = universe()
    p = json.load(open(f'{OUT}/plan.json'))
    now = c.execute("select max(ts) from cycles").fetchone()[0]
    t = tape_frame(c, '2026-10-24')
    q = pd.read_sql("select ts, exchange_id x, best_bid b, best_ask a, bid_qty bq, ask_qty aq from quotes where ts > ?", c,
                    params=(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp(),))
    q['x'] = q.x.astype(str)
    q['m'] = (q.a + q.b) / 2
    q = q.sort_values('ts')
    vf = pd.read_sql("select ts, exchange_id x, fair from venue_fair where ts > ? and fair is not null", c,
                     params=(datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp(),))
    vf['x'] = vf.x.astype(str)
    vf = vf.sort_values('ts')
    res = {}
    res['flow_impact'], res['law'] = flow_impact(t, q, vf)
    res['law']['1h'] = list(res['law']['1 h'])
    print('law', res['law'])
    res['sweeps'] = sweeps(t, q)
    print(res['sweeps'])
    res['own_set_exits'] = own_set_exits(c)
    print({k: v for k, v in res['own_set_exits'].items() if k != 'runs'})
    res['validate'] = validate_model(c, u)
    print({k: v for k, v in res['validate'].items() if k != 'big'})
    res['book_stability'], res['depth_trend'] = book_stability(p)
    print(res['book_stability'], res['depth_trend'][::6])
    res['depth_trend'].append(('latest', depth_now(p)))
    res['robust'] = robust(c, u, now)
    print(res['robust'])
    res['maker_flow'] = maker(t, p, now)
    res['exit_cost_by_utc_hour'] = hour_of_day(c, p, now)
    print(res['exit_cost_by_utc_hour'])
    cy = pd.read_sql("select ts, writes from cycles where ts > ? order by ts", c, params=(now - 6 * 3600,))
    o = pd.read_sql("select cast(placed_at / 60 as int) m, count(*) n from orders where placed_at > ? group by 1", c, params=(now - 86400,))
    res['bot_orders_per_active_min'] = dict(p50=float(o.n.quantile(.5)), p90=float(o.n.quantile(.9)), p99=float(o.n.quantile(.99)), active_minutes=len(o))
    print(res['bot_orders_per_active_min'])
    with open(f'{OUT}/impact.json', 'w') as f:
        json.dump(res, f, indent=1, default=str)


def draw():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    p = json.load(open(f'{OUT}/plan.json'))
    INK, MUTED, GRID, SURF = '#0b0b0b', '#52514e', '#e7e6e1', '#fcfcfb'
    C = dict(blue='#2a78d6', orange='#eb6834', aqua='#1baf7a', yellow='#eda100', magenta='#e87ba4')
    plt.rcParams.update({'font.size': 10, 'text.color': INK, 'axes.labelcolor': MUTED, 'xtick.color': MUTED, 'ytick.color': MUTED,
                         'axes.edgecolor': GRID, 'axes.spines.top': False, 'axes.spines.right': False, 'figure.facecolor': SURF, 'axes.facecolor': SURF})
    fig, ax = plt.subplots(4, 1, figsize=(10, 13.5), sharex=True, height_ratios=[3.2, 2.2, 2.6, 2.2])
    fig.subplots_adjust(left=0.1, right=0.97, top=0.935, bottom=0.135, hspace=0.3)
    fig.suptitle('Raising cash for a quick punt: cost by amount', x=0.1, y=0.975, ha='left', fontsize=15, fontweight='bold')
    cv = p['curves']
    k = lambda v: np.array(v, float) / 1000
    # A
    a = ax[0]
    style = (('one sweep', C['orange'], 'Everything in one pass, seconds (worst case)'), ('5 min', C['blue'], 'In 5 minutes (5 baskets per race)'),
             ('6 h', C['aqua'], 'In 6 hours (12 baskets, waiting for usual prices)'))
    for s_, col, lab in style:
        a.plot(k(cv[s_]['cash']), cv[s_]['given'], color=col, lw=2, label=lab)
    a.plot(k(cv['5 min']['all_sold']['cash']), cv['5 min']['all_sold']['given'], color=C['blue'], lw=2, ls=':',
           label='In 5 minutes, if everything would be sold by 18:00 ET 3 Nov anyway (tonight\'s prices beat usual)')
    a.set_ylabel('Value given up in total (S)')
    a.set_title('A. What does it cost? Waiting beyond 5 minutes saves almost nothing', loc='left', fontsize=11)
    a.legend(frameon=False, loc='upper left', fontsize=9)
    # B
    b = ax[1]
    x, y = k(cv['5 min']['cash']), np.array(cv['5 min']['given'], float)
    edges = np.arange(0, 85, 5.0)
    gi = np.interp(edges, x, y)
    b.bar(edges[:-1] + 2.5, np.diff(gi) / 5.0, width=4.4, color=C['blue'], align='center')
    b.set_ylabel('S given up per\nextra 1,000 raised')
    pc = np.diff(np.interp([0, 20, 60, 80], x, y)) / np.array([20, 40, 20]) / 10
    b.set_title(f'B. Which part is expensive? First 20k about {pc[0]:.1f}%, the next 40k {pc[1]:.1f}%, the last 20k {pc[2]:.1f}% (in 5 minutes)', loc='left', fontsize=11)
    b.axhline(0, color=MUTED, lw=0.8)
    # C
    c = ax[2]
    groups = (('Late-call sets (wind-down sells them anyway)', ['set_late'], C['blue']), ('Single legs the wind-down sells', ['leg', 'mm_leg'], C['orange']),
              ('Legs in cheap-favourite races (ride to settlement)', ['leg_held'], C['aqua']), ('Cheap favourites', ['cheapfav'], C['yellow']),
              ('Early-call sets, mainly MN-05', ['set_early'], C['magenta']))
    t = cv['5 min']['by_type']
    n = len(cv['5 min']['cash'])
    ys = [k(np.sum([np.array(t[z], float) if z in t else np.zeros(n) for z in zs], axis=0)) for _, zs, _ in groups]
    c.stackplot(k(cv['5 min']['cash']), ys, colors=[g[2] for g in groups], labels=[g[0] for g in groups], edgecolor=SURF, linewidth=1)
    c.set_ylabel('Cash raised from\neach kind (thousands)')
    c.set_title('C. What gets sold, cheapest first?', loc='left', fontsize=11)
    c.legend(frameon=False, loc='upper left', fontsize=9)
    # D
    d = ax[3]
    pt = p['partisan']
    d.plot(k(cv['5 min']['cash']), cv['5 min']['netd'], color=C['blue'], lw=2, label='Net Democrat exposure left after selling')
    d.axhspan(-5000, 5000, color=GRID, alpha=0.5, lw=0, label='Allowed by the 5,000 cap')
    d.axhline(0, color=MUTED, lw=0.8)
    d.set_ylim(-7500, 7500)
    d.set_ylabel('Net Democrat\nexposure (S)')
    d.set_xlabel(f"Cash raised (thousands of S, on top of the {p['cash'] / 1000:.1f}k held, of which 15k is the shock reserve)")
    d.set_title(f"D. Can the cash be used? Today a Republican-side trade fits up to {pt['r_room_now'] / 1000:.1f}k, a Democrat-side one {pt['d_room_now'] / 1000:.1f}k", loc='left', fontsize=11)
    d.legend(frameon=False, loc='lower left', fontsize=9)
    for q in ax:
        q.grid(axis='y', color=GRID, lw=0.7)
        q.set_axisbelow(True)
        for T in TARGETS:
            q.axvline(T / 1000, color=GRID, lw=0.8, zorder=0)
    ax[3].set_xticks([T / 1000 for T in TARGETS])
    ax[3].set_xlim(0, 83)
    fig.text(0.1, 0.012, 'Glossary.  Tick: 0.005, the smallest price step.  Touch: the best price on offer now.  Ladder: the designated market\n'
             'maker\'s resting orders, deeper levels holding more size.  Arb set: one position on every leg of a race, paying a fixed sum\n'
             'whoever wins.  Basket: all legs of a set sold together, all or nothing.  S: SUSQies.  Value given up: what the holding would\n'
             'otherwise bring (wind-down sale or settlement) minus the cash from selling now; below zero means selling now beats it by a hair.\n'
             f"Positions {p['positions_as_of']}, books {p['books_as_of'][:16]} UTC.",
             fontsize=8.5, color=MUTED, va='bottom', bbox=dict(boxstyle='round,pad=0.5', fc='#f4f3ef', ec=GRID))
    fig.savefig(FIG, dpi=150)
    print('saved', FIG)


if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'plan'
    if mode == 'plan':
        plan()
    elif mode == 'study':
        study()
    elif mode == 'plot':
        draw()
