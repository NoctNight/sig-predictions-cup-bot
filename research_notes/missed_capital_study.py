"""What did holding capital cost us, 24 Oct 00:00 UTC to now (29 Oct)? Read-only, local data only, no SIG calls.

Question (Sizhe, 29 Oct): "we've quite literally stayed at the same pnl for 5 days ... look for opportunities that we clearly
missed due to holding capital".

    <venv with numpy+pandas+matplotlib>/bin/python research_notes/missed_capital_study.py run    (~1 min)
    <venv ...>/bin/python research_notes/missed_capital_study.py plot

Inputs (all read-only): data/sigbot.sqlite (?mode=ro: fills, orders, cycles, arb_trades, venue_quotes, venue_fair),
data/live.log (sleeve skip / cut / raise lines), data/books_history/*.json (full SIG books every ~6 h),
data/research_contract_rules/tc_mapping.json, params/strategy_monitor_state.json, config.json.
Outputs: data/research_missed_capital/*.json|csv, research_notes/figures/missed_capital_decision.png

Parts
  1. hold side: the 24 Oct 00:00 book frozen, split into arb sets by race / RI Governor / MN-05 / leftover legs, valued at
     the touch and walked through the full book on 24 Oct 00:01 and 29 Oct 10:04; cheap favourites (all bought in the
     window) from their own fills.
  2. missed side: every live sleeve's log lines where it skipped or cut for cash, the MM's size against its budget,
     valued at each sleeve's measured live edge (strategy monitor), plus a hindsight replay of the high-frequency arb skips.
  3. hindsight scan: SIG touch >= 3c through the Polymarket/Kalshi fair with both venues within 4 pts, >= 10 min, same
     contract terms; P&L = later SIG touch exit minus entry touch; each episode labelled with what actually blocked it.
"""
import json, os, re, sqlite3, sys, glob
from datetime import datetime, timezone
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = f'{ROOT}/data/research_missed_capital'
TICK = 0.005
T0 = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
RESERVE_ON = datetime(2026, 10, 24, 19, 0, 50, tzinfo=timezone.utc).timestamp()   # 'SHOCK mode enabled' in live.log
KILL_TS = datetime(2026, 10, 27, 3, 57, 32, tzinfo=timezone.utc).timestamp()      # strategy_stops pingpong: arb + hf_arb
GAP_ON = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc).timestamp()
RESERVE, CAP, HEADROOM, CF_FLOOR = 15000.0, 5000.0, 1000.0, 1000.0
BOOK0 = ('books_20261024T0001Z.json', 'books_20261023T2317Z.json', 'books_20261024T0603Z.json')   # first snapshot holding a leg wins
BOOK1 = ('books_20261029T1004Z.json', 'books_20261029T0401Z.json')


def db():
    return sqlite3.connect(f'file:{ROOT}/data/sigbot.sqlite?mode=ro', uri=True)


def ts_of(s):
    return datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------- shared loaders
def legs_map(c):
    vf = pd.read_sql("select race, party, exchange_id xid from venue_fair group by race, party, exchange_id", c)   # all history:
    # a leg whose outside quotes stopped early (Hawaii Governor R, 22 Oct) is still a held position
    return vf.drop_duplicates('xid').set_index('xid')


def fills(c):
    f = pd.read_sql("select exchange_id xid, price p, quantity q, filled_at, purpose from fills", c)
    f['ts'] = (pd.to_datetime(f.filled_at, utc=True) - pd.Timestamp('1970-01-01', tz='UTC')).dt.total_seconds()
    return f


def book(names):
    out = {}
    for name in names:
        b = json.load(open(f'{ROOT}/data/books_history/{name}'))
        for k, v in b.items():
            if k not in out and (v.get('bids') or v.get('asks')):
                out[k] = ([(x['price'], x['quantity']) for x in v.get('bids', [])],
                          [(x['price'], x['quantity']) for x in v.get('asks', [])])
    return out


def walk(levels, n):
    """Cash from crossing n shares through levels [(price, qty)], best first; shares beyond the book get nothing."""
    got, left = 0.0, n
    for px, qt in levels:
        t = min(left, qt); got += t * px; left -= t
        if left <= 0: break
    return got, left


def leg_value(bk, xid, q):
    """Real cash from liquidating a net-YES position q (q < 0 = |q| NO): (touch, walked)."""
    if q == 0 or xid not in bk: return 0.0, 0.0
    bids, asks = bk[xid]
    if q > 0:
        tb = bids[0][0] if bids else 0.0
        w, left = walk(bids, q)
        return q * tb, w
    n = -q
    ta = asks[0][0] if asks else 1.0
    cost, left = walk(asks, n)
    cost += left * 1.0
    return n * (1 - ta), n - cost


def log_lines(pat):
    rx = re.compile(pat)
    with open(f'{ROOT}/data/live.log', errors='replace') as fh:
        for ln in fh:
            if ln[:10] < '2026-10-24': continue
            m = rx.search(ln)
            if m: yield ts_of(ln), m


# ---------------------------------------------------------------- part 1: hold side
def part1(c, L, F, out):
    bk0, bk1 = book(BOOK0), book(BOOK1)
    t_b1 = datetime(2026, 10, 29, 10, 4, tzinfo=timezone.utc).timestamp()
    q0 = F[F.ts < T0].groupby('xid').q.sum()
    q1 = F[F.ts < t_b1].groupby('xid').q.sum()
    q0 = q0[q0.abs() > 0.5]; q1 = q1[q1.abs() > 0.5]
    fair1 = pd.read_sql("select exchange_id xid, fair from venue_fair where ts > (select max(ts) from venue_fair) - 6 * 3600 "
                        "and fair is not null order by ts", c).groupby('xid').fair.last()     # last outside fair in the last 6 h
    races = L.groupby('race').apply(lambda g: list(g.index))
    rows = []
    for race, xs in races.items():
        qs = {x: float(q0.get(x, 0.0)) for x in xs}
        if not any(qs.values()): continue
        sgn = 0
        if all(v < 0 for v in qs.values()): sgn = -1
        elif all(v > 0 for v in qs.values()): sgn = 1
        sets = min(abs(v) for v in qs.values()) if sgn else 0.0
        bucket = ('RI Governor sets' if race == 'Rhode Island Governor' else 'MN-05 sets' if race.startswith('MN-05')
                  else 'arb sets')
        for x, v in qs.items():
            if v == 0: continue
            s_part = sgn * sets
            parts = [(bucket if sets else None, s_part), ('leftover legs', v - s_part)]
            v0t, v0w = leg_value(bk0, x, v); v1t, v1w = leg_value(bk1, x, v)
            for b, qq in parts:
                if b is None or abs(qq) < 0.5: continue
                w = qq / v
                fv = fair1.get(x, np.nan)
                ev = qq * fv if qq > 0 else -qq * (1 - fv)
                if not np.isfinite(ev): ev = w * v1t           # no outside fair in the last 6 h: mark at the touch
                rows.append(dict(race=race, xid=x, party=L.party.get(x), bucket=b, q=qq, sets=sets if b != 'leftover legs' else 0,
                                 n_legs=len(xs), v0_touch=w * v0t, v0_walk=w * v0w, v1_touch=w * v1t, v1_walk=w * v1w, ev_fair=ev))
    H = pd.DataFrame(rows)
    H.to_csv(f'{OUT}/hold_legs.csv', index=False)
    # set payout = n-1 for NO sets, 1 for YES sets: per race
    R = H.groupby(['bucket', 'race']).agg(sets=('sets', 'max'), n_legs=('n_legs', 'max'), v0_touch=('v0_touch', 'sum'),
                                          v0_walk=('v0_walk', 'sum'), v1_touch=('v1_touch', 'sum'), v1_walk=('v1_walk', 'sum'),
                                          ev_fair=('ev_fair', 'sum'), q=('q', 'sum')).reset_index()
    R['payout'] = np.where(R.bucket == 'leftover legs', R.ev_fair,
                           R.sets * np.where(R.q < 0, R.n_legs - 1, 1))
    R['hold_touch'] = R.v1_touch - R.v0_touch
    R['hold_walk'] = R.v1_walk - R.v0_walk
    R['exit_cost0_tick_per_set'] = np.where(R.sets > 0, (R.payout - R.v0_touch) / R.sets.clip(lower=1) / TICK, np.nan)
    R['exit_cost1_tick_per_set'] = np.where(R.sets > 0, (R.payout - R.v1_touch) / R.sets.clip(lower=1) / TICK, np.nan)
    R.sort_values('v0_touch', ascending=False).to_csv(f'{OUT}/hold_races.csv', index=False)
    B = R.groupby('bucket')[['v0_touch', 'v0_walk', 'v1_touch', 'v1_walk', 'payout', 'hold_touch', 'hold_walk']].sum()
    B['races'] = R.groupby('bucket').size()
    # cheap favourites: every fill is in the window
    cf = F[F.purpose == 'cheapfav'].copy()
    cf['cost'] = np.where(cf.q > 0, cf.q * cf.p, -cf.q * (1 - cf.p))
    cq = cf.groupby('xid').agg(q=('q', 'sum'), cost=('cost', 'sum'))
    cv = [leg_value(bk1, x, r.q) for x, r in cq.iterrows()]
    cq['v1_touch'] = [a for a, _ in cv]; cq['v1_walk'] = [b for _, b in cv]
    cq['ev_fair'] = [r.q * fair1.get(x, np.nan) if r.q > 0 else -r.q * (1 - fair1.get(x, np.nan)) for x, r in cq.iterrows()]
    cq['ev_fair'] = cq.ev_fair.fillna(cq.v1_touch)                  # no outside fair in the last 6 h: mark at the touch
    cq.to_csv(f'{OUT}/cheapfav_legs.csv')
    out['cheapfav'] = dict(legs=len(cq), cost=cq.cost.sum(), v1_touch=cq.v1_touch.sum(), v1_walk=cq.v1_walk.sum(),
                           ev_fair=cq.ev_fair.sum(), first=cf.filled_at.min(), last=cf.filled_at.max())
    out['hold_buckets'] = B.reset_index().to_dict('records')
    out['n_positions_t0'] = int(len(q0)); out['n_positions_now'] = int(len(q1))
    cyc = pd.read_sql("select ts, cash, positions_value, total_value from cycles where ts >= ? order by ts", c, params=(T0 - 600,))
    out['account'] = dict(t0=cyc.iloc[0].to_dict(), t1=cyc.iloc[-1].to_dict(),
                          cash_median=float(cyc[cyc.ts > RESERVE_ON].cash.median()),
                          cash_min_after_reserve=float(cyc[cyc.ts > RESERVE_ON + 6 * 3600].cash.min()),
                          spendable_median=float((cyc[cyc.ts > RESERVE_ON].cash - RESERVE).median()))
    cyc.to_csv(f'{OUT}/cycles.csv', index=False)
    print(B.round(0).to_string()); print('cheapfav', {k: round(v) if isinstance(v, float) else v for k, v in out['cheapfav'].items()})
    print(R.sort_values('v0_touch', ascending=False).head(25)[['bucket', 'race', 'sets', 'v0_touch', 'v0_walk', 'v1_touch', 'hold_touch',
          'payout', 'exit_cost0_tick_per_set', 'exit_cost1_tick_per_set']].round(1).to_string(index=False))
    return R


# ---------------------------------------------------------------- part 2: missed side
def sig_quotes(c, xids, t_from):
    q = pd.read_sql(f"select ts, instrument xid, bid, ask from venue_quotes where venue='sig' and ts >= ? and instrument in "
                    f"({','.join('?' * len(xids))}) order by ts", c, params=[t_from] + list(xids))
    return q


def hf_replay(c, L, ep):
    """Buy the sell-set (NO on both legs) at the logged price/set; sell at +2 ticks/set or after 24 h (or now)."""
    xs = list(L[L.race == ep['race']].index)
    if len(xs) != 2: return np.nan, 'not two legs', ep['t']
    q = sig_quotes(c, xs, ep['t'])
    p = q.pivot_table(index='ts', columns='xid', values='ask').dropna()
    p = p[(p.index > ep['t'] + 60) & (p.index <= ep['t'] + 86400)]
    if p.empty: return np.nan, 'no quotes', ep['t']
    exitv = 2 - p.sum(axis=1)                      # proceeds per set from selling both NOs (buy YES at ask)
    hit = exitv[exitv >= ep['px'] + 2 * TICK]
    if len(hit): return float(hit.iloc[0] - ep['px']), 'take profit', float(hit.index[0])
    return float(exitv.iloc[-1] - ep['px']), '24 h' if p.index[-1] >= ep['t'] + 86000 else 'open (marked now)', float(p.index[-1])


def part2(c, L, F, out):
    mon = json.load(open(f'{ROOT}/params/strategy_monitor_state.json'))['last']['results']
    edges = {k: (v.get('mean'), v.get('n'), v.get('verdict')) for k, v in mon.items()}
    out['monitor'] = edges
    # high-frequency arb skips / cuts for cash
    rx = r'CHURN (skipped|cut) for cash: (.+?) wanted x(\d+) at ([\d.]+)/set, cash allowed x(\d+) \((\d+) unmet\)'
    hs = pd.DataFrame([dict(t=t, kind=m[1], race=m[2], want=int(m[3]), px=float(m[4]), allowed=int(m[5]), unmet=int(m[6]))
                       for t, m in log_lines(rx)])
    hs = hs.sort_values(['race', 't'])
    hs['ep'] = ((hs.race != hs.race.shift()) | (hs.t - hs.t.shift() > 3600)).cumsum()
    E = hs.groupby('ep').agg(race=('race', 'first'), t=('t', 'first'), t_end=('t', 'last'), px=('px', 'first'),
                             unmet=('unmet', 'max'), lines=('t', 'size')).reset_index(drop=True).sort_values('t')
    E['sets'] = np.minimum(np.floor(E.unmet / E.px), np.floor(4000 / E.px))   # 'unmet' is logged as notional; 4k per race
    pnl, why, tx = zip(*[hf_replay(c, L, r) for _, r in E.iterrows()])
    E['replay_per_set'] = pnl; E['replay_exit'] = why; E['replay_t_exit'] = tx
    # 4k pot: an episode enters only if the pot has room (lots held until their replay exit, at most 24 h);
    # the sleeve's own open lots are ignored (it had none from 25 Oct: open_lots 0), so this is generous
    held, take = [], []
    for _, r in E.iterrows():
        held = [(te, cap) for te, cap in held if te > r.t]
        room = 4000 - sum(cap for _, cap in held)
        s = max(0.0, min(r.sets, np.floor(room / r.px)))
        take.append(s); held.append((r.replay_t_exit, s * r.px))
    E['sets_pot'] = take
    E['value_live_edge'] = E.sets_pot * (edges['hf_arb'][0] or 0) * TICK
    E['value_replay'] = E.sets_pot * E.replay_per_set
    E.to_csv(f'{OUT}/hf_arb_skips.csv', index=False)
    out['hf_arb'] = dict(lines=len(hs), episodes=len(E), first=float(hs.t.min()), last=float(hs.t.max()),
                         capital_wanted=float((E.sets_pot * E.px).sum()), sets=float(E.sets_pot.sum()),
                         value_live_edge=float(E.value_live_edge.sum()), value_replay=float(np.nansum(E.value_replay)),
                         replay_mean_tick=float(np.nanmean(E.replay_per_set) / TICK), replay_win=float((E.replay_per_set > 0).mean()),
                         after_kill_note='no skip lines after the 27 Oct 03:57 pingpong kill (sleeve off)')
    print(E[['race', 't', 'px', 'unmet', 'sets_pot', 'replay_per_set', 'replay_exit']].assign(
        t=lambda d: pd.to_datetime(d.t, unit='s').dt.strftime('%m-%d %H:%M')).to_string(index=False))
    # cash raise needs board
    rn = pd.DataFrame([dict(t=t, needs=m[1], short=float(m[2])) for t, m in
                       log_lines(r"RAISE: \[([^\]]*)\] short (\d+); no clip")])
    rc = pd.DataFrame([dict(t=t, proceeds=float(m[1]), cost=float(m[2]), need=m[3]) for t, m in
                       log_lines(r'RAISE clip \d+: .*? proceeds (\d+), cost ([\d.]+) .*?; for (\w+)')])
    out['raise'] = dict(no_clip_lines=rn.groupby('needs').size().to_dict(),
                        clips=rc.groupby('need').agg(n=('proceeds', 'size'), proceeds=('proceeds', 'sum'), cost=('cost', 'sum')).reset_index().to_dict('records'))
    # cheap favourites: shortfall it posted, vs its budget room
    cfn = rn[rn.needs.str.contains('cheapfav')]
    cf = out['cheapfav']
    room = 15000 - cf['cost']
    cfn_day = cfn.assign(d=pd.to_datetime(cfn.t, unit='s').dt.strftime('%m-%d')).groupby('d').short.max()
    unmet = min(room, float(cfn.short.max())) if len(cfn) else 0.0
    ret_touch = (cf['v1_touch'] - cf['cost']) / cf['cost']; ret_fair = (cf['ev_fair'] - cf['cost']) / cf['cost']
    out['cheapfav_missed'] = dict(lines=len(cfn), first=float(cfn.t.min()), last=float(cfn.t.max()), short_max_by_day=cfn_day.to_dict(),
                                  budget_room=room, unmet_capital=unmet, ret_touch_now=ret_touch, ret_fair_now=ret_fair,
                                  value_touch=unmet * ret_touch, value_fair=unmet * ret_fair)
    # MM: order size vs budget; 30-min markouts per share before and in the window
    o = pd.read_sql("select placed_at ts, quantity, price from orders where purpose='mm' and placed_at > ?", c, params=(T0 - 2 * 86400,))
    o['notional'] = o.quantity * o.price
    o['d'] = pd.to_datetime(o.ts, unit='s').dt.floor('6h')
    mm_size = o.groupby('d').notional.mean()
    mmf = F[(F.purpose == 'mm') & (F.ts > T0 - 2 * 86400)].copy()
    q = sig_quotes(c, list(mmf.xid.unique()), T0 - 2 * 86400 - 3600)
    q['mid'] = (q.bid + q.ask) / 2; q = q.dropna().astype({'ts': float, 'xid': str}).sort_values('ts')
    mmf = mmf.astype({'xid': str, 'ts': float})
    mmf['t30'] = mmf.ts + 1800
    mmf = pd.merge_asof(mmf.sort_values('t30'), q[['ts', 'xid', 'mid']].rename(columns={'ts': 't30'}), on='t30', by='xid', direction='backward')
    mmf['mo'] = np.sign(mmf.q) * (mmf.mid - mmf.p) * mmf.q.abs()
    mmf['era'] = np.select([mmf.ts < T0 + 13 * 3600, mmf.ts < KILL_TS], ['pre: 22-24 Oct 13:00 (full size)', 'window to 27 Oct'], 'window from 27 Oct')
    mmf = mmf[mmf.ts < pd.read_sql("select max(ts) from cycles", c).iloc[0, 0] - 1800]
    span = mmf.groupby('era').ts.agg(lambda s: (s.max() - s.min()) / 86400)
    M = mmf.groupby('era').agg(fills=('q', 'size'), shares=('q', lambda s: s.abs().sum()), markout=('mo', 'sum')).assign(days=span)
    M['shares_per_day'] = M.shares / M.days; M['markout_per_share_tick'] = M.markout / M.shares / TICK; M['markout_per_day'] = M.markout / M.days
    win = mmf[mmf.ts >= T0 + 13 * 3600]
    win_days = (win.ts.max() - win.ts.min()) / 86400
    pre = M.loc['pre: 22-24 Oct 13:00 (full size)']
    w_mo = win.mo.sum(); w_sh = win.q.abs().sum()
    extra_sh = max(0.0, pre.shares_per_day * win_days - w_sh)
    out['mm'] = dict(table=M.reset_index().to_dict('records'), order_notional_6h=mm_size.rename(lambda x: str(x)).to_dict(),
                     window_days=win_days, window_markout=w_mo, window_shares=w_sh,
                     extra_shares_at_pre_rate=extra_sh,
                     value_extra_at_window_markout=extra_sh * w_mo / w_sh,
                     value_extra_at_min_markout=extra_sh * min(w_mo / w_sh, pre.markout / pre.shares),
                     budget_rule='budget_total = min(0.2 * 100k, 0.9 * spendable cash); spendable = cash - 15k shock reserve')
    print(M.round(2).to_string())
    # recycles / set arbs: off since the pingpong kill (not cash). Rate before the kill
    at = pd.read_sql("select ts, race, kind, quantity, edge_per_set, cost_per_set from arb_trades where ok=1 and ts >= ?", c, params=(T0,))
    at['era'] = np.where(at.ts < KILL_TS, 'before kill', 'after kill')
    at['cap'] = at.quantity * at.cost_per_set; at['edge_d'] = at.quantity * at.edge_per_set
    A = at.groupby(['era', 'kind']).agg(n=('quantity', 'size'), capital=('cap', 'sum'), edge=('edge_d', 'sum')).reset_index()
    pre_days = (KILL_TS - T0) / 86400
    rec = at[(at.era == 'before kill') & (at.kind == 'unwind_set')]
    rate = rec.cap.sum() / pre_days * (edges['recycle'][0] or 0) / 100
    kill_days = (pd.read_sql("select max(ts) from cycles", c).iloc[0, 0] - KILL_TS) / 86400
    pre = at[at.era == 'before kill']
    out['recycle'] = dict(table=A.to_dict('records'), freed_per_day=rec.cap.sum() / pre_days, live_edge_pct=edges['recycle'][0],
                          arb_trades_net_per_day=pre.edge_d.sum() / pre_days,
                          value_per_day=rate, kill_days=kill_days, value_lost_to_kill=rate * kill_days)
    print(A.round(1).to_string(index=False))
    # catch-up / shock: any cash lines?
    out['catchup_cash_lines'] = sum(1 for _ in log_lines(r'CATCHUP.*(cash|floor|reserve|budget)'))
    out['shock_entries'] = sum(1 for _ in log_lines(r'SHOCK (?!fast detector|mode enabled)'))
    out['shock_reserve_days_idle'] = (pd.read_sql("select max(ts) from cycles", c).iloc[0, 0] - RESERVE_ON) / 86400
    return E


# ---------------------------------------------------------------- part 3: hindsight scan
def part3(c, L, F, out, thr=0.03):
    tc = json.load(open(f'{ROOT}/data/research_contract_rules/tc_mapping.json'))
    same = {k for k, v in tc.items() if isinstance(v, dict) and v.get('terms') == 'same'
            and not any(str(d).endswith('late_settle') for d in (v.get('differences') or []))}
    src = open(f'{ROOT}/sigbot/proposals/cheap_favourites.py').read()
    fast = set(re.findall(r'"([A-Z]{2})": [\d.]+', src.split('FAST_CLOSE_ET =')[1].split('}')[0]))
    punt = set(re.findall(r'"([^"]+)"', src.split('PUNT_RACES =')[1].split(')')[0]))
    rm = json.load(open(f'{ROOT}/data/race_map.json'))['races']
    st_abbr = {'Alabama': 'AL', 'Alaska': 'AK', 'Arizona': 'AZ', 'Arkansas': 'AR', 'California': 'CA', 'Colorado': 'CO', 'Connecticut': 'CT',
               'Delaware': 'DE', 'Florida': 'FL', 'Georgia': 'GA', 'Hawaii': 'HI', 'Idaho': 'ID', 'Illinois': 'IL', 'Indiana': 'IN', 'Iowa': 'IA',
               'Kansas': 'KS', 'Kentucky': 'KY', 'Louisiana': 'LA', 'Maine': 'ME', 'Maryland': 'MD', 'Massachusetts': 'MA', 'Michigan': 'MI',
               'Minnesota': 'MN', 'Mississippi': 'MS', 'Missouri': 'MO', 'Montana': 'MT', 'Nebraska': 'NE', 'Nevada': 'NV', 'New Hampshire': 'NH',
               'New Jersey': 'NJ', 'New Mexico': 'NM', 'New York': 'NY', 'North Carolina': 'NC', 'North Dakota': 'ND', 'Ohio': 'OH', 'Oklahoma': 'OK',
               'Oregon': 'OR', 'Pennsylvania': 'PA', 'Rhode Island': 'RI', 'South Carolina': 'SC', 'South Dakota': 'SD', 'Tennessee': 'TN', 'Texas': 'TX',
               'Utah': 'UT', 'Vermont': 'VT', 'Virginia': 'VA', 'Washington': 'WA', 'West Virginia': 'WV', 'Wisconsin': 'WI', 'Wyoming': 'WY'}
    state = {r: st_abbr.get(v.get('state'), '') for r, v in rm.items()}
    vf = pd.read_sql("select ts, race, party, exchange_id xid, pm, kalshi, fair, sig_bid, sig_ask from venue_fair where ts >= ? "
                     "and pm is not null and kalshi is not null and sig_bid is not null and sig_ask is not null order by ts", c, params=(T0,))
    t_end = vf.ts.max()
    vf['agree'] = (vf.pm - vf.kalshi).abs() <= 0.04
    vf['e_buy'] = vf.fair - vf.sig_ask; vf['e_sell'] = vf.sig_bid - vf.fair
    cyc = pd.read_csv(f'{OUT}/cycles.csv').sort_values('ts')
    nd = pd.DataFrame([dict(ts=t, nd=float(m[1])) for t, m in log_lines(r'cycle \d+ done .*? netD=([+-]?\d+)')]).sort_values('ts')
    ours = F[F.purpose.notna() & ~F.purpose.isin(['mm'])]
    eps = []
    for xid, g in vf.groupby('xid'):
        g = g.reset_index(drop=True)
        for side, col in (('buy_yes', 'e_buy'), ('buy_no', 'e_sell')):
            ok = (g[col] >= thr) & g.agree
            if not ok.any(): continue
            run = (ok != ok.shift()).cumsum()
            for _, seg in g[ok].groupby(run[ok]):
                if len(seg) < 2: continue
                t0, t1 = seg.ts.iloc[0], seg.ts.iloc[-1]
                if t1 - t0 < 600: continue
                e = seg.iloc[1]                                   # enter at the second poll (~2 min of latency)
                entry = e.sig_ask if side == 'buy_yes' else 1 - e.sig_bid
                fut = g[(g.ts > e.ts) & (g.ts <= e.ts + 72 * 3600)]
                if side == 'buy_yes':
                    exit_px = fut.sig_bid; conv = fut.sig_bid >= fut.fair - TICK
                else:
                    exit_px = 1 - fut.sig_ask; conv = fut.sig_ask <= fut.fair + TICK
                if conv.any():
                    i = conv.idxmax(); ex, how, tx = exit_px[i], 'converged', fut.ts[i]
                elif len(fut):
                    ex, how, tx = exit_px.iloc[-1], 'open, marked now' if fut.ts.iloc[-1] >= t_end - 600 else '72 h', fut.ts.iloc[-1]
                else:
                    continue
                race, party = e.race, e.party
                p_fav = e.fair if side == 'buy_yes' else 1 - e.fair
                notional = 500.0; sh = notional / entry
                dsign = {'D': 1, 'R': -1}.get(party, 0) * (1 if side == 'buy_yes' else -1)
                ndv = float(np.interp(e.ts, nd.ts, nd.nd)) if len(nd) else 0.0
                cash = float(np.interp(e.ts, cyc.ts, cyc.cash))
                spend = cash - (RESERVE if e.ts >= RESERVE_ON else 0.0)
                cap_block = dsign != 0 and abs(ndv + dsign * sh) > CAP - HEADROOM and abs(ndv + dsign * sh) > abs(ndv)
                cf_cover = (xid in same and state.get(race, '') in fast and p_fav >= 0.80 and race not in punt and party != 'I')
                gap_cover = xid in same and e.ts >= GAP_ON and party != 'I'
                tk = ours[(ours.xid == xid) & (ours.ts >= t0 - 300) & (ours.ts <= t1 + 300)]
                taken = len(tk[np.sign(tk.q) == (1 if side == 'buy_yes' else -1)]) > 0
                cash_short = spend - CF_FLOOR < notional
                if taken: why = 'taken'
                elif xid not in same: why = 'different contract terms'
                elif cap_block: why = 'partisan cap'
                elif (cf_cover or gap_cover) and cash_short: why = 'cash'
                elif cf_cover or gap_cover: why = 'sleeve rule (cash was free)'
                elif cash_short: why = 'no sleeve (cash also short)'
                else: why = 'no sleeve (cash was free)'
                eps.append(dict(race=race, party=party, xid=xid, side=side, t=e.ts, minutes=(t1 - t0) / 60, edge=e[col], fair=e.fair,
                                p_fav=p_fav, entry=entry, exit=ex, exit_how=how, hold_h=(tx - e.ts) / 3600,
                                pnl_per_share=ex - entry, pnl_500=(ex - entry) * sh, spendable=spend, netD=ndv, blocker=why))
    P = pd.DataFrame(eps).sort_values('t')
    P.to_csv(f'{OUT}/hindsight_{int(thr * 100)}c.csv', index=False)
    S = P.groupby('blocker').agg(n=('pnl_500', 'size'), pnl_500=('pnl_500', 'sum'), mean_tick=('pnl_per_share', lambda s: s.mean() / TICK),
                                 converged=('exit_how', lambda s: (s == 'converged').mean())).reset_index()
    out[f'hindsight_{int(thr * 100)}c'] = dict(table=S.to_dict('records'), n=len(P))
    print(f'threshold {thr}'); print(S.round(2).to_string(index=False))
    return P


def run():
    os.makedirs(OUT, exist_ok=True)
    c = db(); L = legs_map(c); F = fills(c)
    out = {'generated': datetime.now(timezone.utc).isoformat(), 'window_from': T0}
    part1(c, L, F, out)
    part2(c, L, F, out)
    for thr in (0.02, 0.03, 0.05):
        part3(c, L, F, out, thr)
    json.dump(out, open(f'{OUT}/summary.json', 'w'), indent=1, default=float)


# ---------------------------------------------------------------- figure
def plot():
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    S = json.load(open(f'{OUT}/summary.json'))
    cyc = pd.read_csv(f'{OUT}/cycles.csv'); E = pd.read_csv(f'{OUT}/hf_arb_skips.csv'); P = pd.read_csv(f'{OUT}/hindsight_3c.csv')
    fig, ax = plt.subplots(4, 1, figsize=(10, 22), gridspec_kw={'height_ratios': [1, .9, .9, 1]})
    fig.suptitle('Did locked-up capital cost us anything, 24-29 Oct?', fontsize=15, fontweight='bold', y=.995)
    fig.text(.5, .979, 'Holdings frozen at 24 Oct 00:00 and marked through the SIG book; blocked trades valued at the sleeve\'s\n'
             'measured live edge, or at the price SIG actually reached later', ha='center', fontsize=9.5, color='#444', va='top')
    a = ax[0]; t = pd.to_datetime(cyc.ts, unit='s')
    res = np.where(cyc.ts >= RESERVE_ON, RESERVE, 0)
    a.plot(t, cyc.cash / 1000, color='#1f77b4', label='free cash')
    a.plot(t, (cyc.cash - res) / 1000, color='#d62728', label='cash other sleeves could spend (free cash - 15k shock reserve)')
    a.axhline(0, color='grey', lw=.6)
    a2 = a.twinx(); a2.plot(t, cyc.total_value / 1000, color='#333', lw=1.5)
    a2.set_ylim(96, 106); a2.set_ylabel('account value at mid (thousand S, black)')
    a.plot([], [], color='#333', label='account value at mid (right scale)')
    for _, r in E.iterrows():
        a.axvspan(pd.to_datetime(r.t, unit='s'), pd.to_datetime(max(r.t_end, r.t + 600), unit='s'), color='#ff7f0e', alpha=.25, lw=0)
    a.axvline(pd.to_datetime(KILL_TS, unit='s'), color='k', ls='--', lw=1)
    a.text(pd.to_datetime(KILL_TS, unit='s'), 23, ' pingpong stop:\n arb + high-frequency arb off', fontsize=8)
    a.axvline(pd.to_datetime(RESERVE_ON, unit='s'), color='#d62728', ls='--', lw=1)
    a.text(pd.to_datetime(RESERVE_ON, unit='s'), 23, ' 15k shock\n reserve on', fontsize=8, color='#d62728')
    a.set_ylim(-16, 26); a.set_ylabel('cash (thousand S)')
    a.set_title('A. Flat account; other sleeves could spend ~2k, not ~17k\n(orange = high-frequency arb skipped for cash)',
                loc='left', fontweight='bold', fontsize=10)
    a.legend(fontsize=8, loc='upper center', bbox_to_anchor=(.5, -.08), frameon=False, ncol=1)
    a = ax[1]
    B = pd.DataFrame(S['hold_buckets']).set_index('bucket')
    cf = S['cheapfav']
    B.loc['cheap favourites (bought in window)'] = dict(v0_touch=cf['cost'], v0_walk=cf['cost'], v1_touch=cf['v1_touch'],
                                                        v1_walk=cf['v1_walk'], hold_touch=cf['v1_touch'] - cf['cost'],
                                                        hold_walk=cf['v1_walk'] - cf['cost'])
    y = np.arange(len(B))
    a.barh(y, B.hold_touch, color=np.where(B.hold_touch >= 0, '#2ca02c', '#d62728'), height=.5, label='mark change at the touch')
    a.scatter(B.hold_walk, y, color='k', zorder=3, marker='D', label='mark change walking the whole book')
    for i, (k, r) in enumerate(B.iterrows()):
        a.text(-440, i + .3, f'tied up {r.v0_touch / 1000:.1f}k at the touch, {r.v0_walk / 1000:.1f}k if all dumped on 24 Oct',
               fontsize=7.5, color='#444')
    a.set_yticks(y); a.set_yticklabels(B.index); a.axvline(0, color='grey', lw=1)
    a.set_xlim(-450, 250); a.set_ylim(-.5, len(B) - .2)
    a.set_xlabel('S earned by holding, 24 Oct 00:00 -> 29 Oct 10:00 (positive = holding beat selling on 24 Oct)')
    a.set_title('B. What the holding capital earned over the window (cheap favourites: since purchase)', loc='left', fontweight='bold', fontsize=10)
    a.legend(fontsize=8, loc='upper center', bbox_to_anchor=(.5, -.13), frameon=False, ncol=2)
    a = ax[2]
    mm, hf, cm, rc = S['mm'], S['hf_arb'], S['cheapfav_missed'], S['recycle']
    rows = [('MM at its pre-24 Oct size\n(cut by cash)', mm['value_extra_at_min_markout'], mm['value_extra_at_window_markout']),
            ('high-frequency arb skips\n(cut by cash)', hf['value_live_edge'], hf['value_replay']),
            ('cheap favourites\n(budget filled; cash only delayed it)', cm['value_touch'], cm['value_fair']),
            ('recycles + set arbs off\n(pingpong stop, not cash)', rc['arb_trades_net_per_day'] * rc['kill_days'], rc['value_lost_to_kill']),
            ('shock (reserve idle,\nno shock fired)', 0.0, 0.0)]
    y = np.arange(len(rows))
    a.barh(y - .17, [r[1] for r in rows], height=.32, color='#1f77b4', label='at the measured live edge (recycles: arb_trades edges; MM: worse markout)')
    a.barh(y + .17, [r[2] for r in rows], height=.32, color='#9467bd', label='optimistic: hindsight replay / outside-fair mark / window markout / monitor +0.93%')
    a.set_yticks(y); a.set_yticklabels([r[0] for r in rows], fontsize=8.5); a.axvline(0, color='grey', lw=1)
    a.set_xlabel('S missed over the window'); a.legend(fontsize=8, loc='upper center', bbox_to_anchor=(.5, -.13), frameon=False)
    a.set_title('C. Trades the bot skipped or shrank, by sleeve', loc='left', fontweight='bold', fontsize=10)
    a = ax[3]
    cols = {'cash': '#d62728', 'partisan cap': '#ff7f0e', 'taken': '#2ca02c', 'sleeve rule (cash was free)': '#8c564b',
            'no sleeve (cash was free)': '#7f7f7f', 'no sleeve (cash also short)': '#bcbd22', 'different contract terms': '#17becf'}
    for k, g in P.groupby('blocker'):
        a.scatter(pd.to_datetime(g.t, unit='s'), g.pnl_500, s=12 + g.minutes.clip(upper=600) / 8, color=cols.get(k, 'k'), alpha=.7,
                  label=f'{k}: n={len(g)}, sum {g.pnl_500.sum():+.0f}')
    a.axhline(0, color='grey', lw=1)
    a.set_title('D. Hindsight: every SIG mispricing >= 3c through the Polymarket/Kalshi fair\n(venues within 4 pts, >= 10 min):\n'
                '500 S in at the touch, out at the later touch (on convergence, else after 72 h or now)', loc='left', fontweight='bold', fontsize=10)
    a.set_ylabel('S per 500 S ticket'); a.set_xlabel('entry time (UTC); dot size = how long the gap lasted')
    a.legend(fontsize=8, loc='upper center', bbox_to_anchor=(.5, -.1), frameon=False, ncol=2)
    fig.text(.07, .004, 'Glossary\n'
             'touch: best bid/ask; walked: selling the whole position through the 24 Oct 00:01 / 29 Oct 10:04 book snapshots.\n'
             'set: one contract on every leg of a race (here NO on every leg); exit cost = payout minus touch sale value.\n'
             'spendable cash: free cash minus the 15k shock reserve (live from 24 Oct 19:00); every sleeve but shock plans with it.\n'
             'measured live edge: strategy monitor (MM +0.31 ticks/fill, high-frequency arb -0.16 ticks/set, recycles +0.93%, '
             'cheap favourites marked at today\'s touch).\n'
             'blocker: what actually stopped the hindsight trade; only "cash" counts as missed because of capital. tick = 0.005.',
             fontsize=8.5, va='bottom', bbox=dict(boxstyle='round', fc='#f4f4f0', ec='#999'))
    plt.tight_layout(rect=(0, .06, 1, .975))
    fig.savefig(f'{ROOT}/research_notes/figures/missed_capital_decision.png', dpi=110)


if __name__ == '__main__':
    globals()[sys.argv[1] if len(sys.argv) > 1 else 'run']()
