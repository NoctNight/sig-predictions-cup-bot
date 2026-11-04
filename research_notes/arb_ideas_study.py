"""Three cash-by-3-Nov ideas (25 Oct 2026). Read-only, local data only, no SIG calls.

  python3 research_notes/arb_ideas_study.py idea1   # wide-overround sets held for days, sold before the deadline
  python3 research_notes/arb_ideas_study.py idea2   # multi-day drift of SIG favourites (buy now, sell 3 Nov pre-call)
  python3 research_notes/arb_ideas_study.py idea3   # election-night cash by punt time
  python3 research_notes/arb_ideas_study.py plot
Outputs: data/research_arb_ideas/*.json, research_notes/figures/arb_ideas_decision.png
"""
import json, sqlite3, sys, itertools, math
from collections import defaultdict
import numpy as np
import pandas as pd

ROOT = __file__.rsplit('/research_notes/', 1)[0]
DB = f'{ROOT}/data/sigbot.sqlite'
OUT = f'{ROOT}/data/research_arb_ideas'
TICK = 0.005
import os; os.makedirs(OUT, exist_ok=True)


def ctstat(vals, groups):
    """mean and cluster-robust t (clusters = groups)."""
    v = np.asarray(vals, float); g = np.asarray(groups)
    n = len(v)
    if n < 3: return (float(v.mean()) if n else float('nan')), float('nan'), n, len(set(g))
    m = v.mean(); e = v - m
    s = pd.Series(e).groupby(g).sum().values
    G = len(s)
    se = math.sqrt((s ** 2).sum()) / n * math.sqrt(G / max(G - 1, 1))
    return float(m), float(m / se) if se > 0 else float('nan'), n, G


# ---------------------------------------------------------------- panels
def real_panel():
    con = sqlite3.connect(DB)
    q = pd.read_sql("select ts, race, party, bid, ask, bid_sz, ask_sz from venue_quotes where venue='sig'", con)
    q['b'] = (q.ts // 1800 * 1800).astype(int)
    q = q.sort_values('ts').groupby(['race', 'party', 'b']).last().reset_index()
    nlegs = q.groupby('race').party.nunique()
    ok = q.dropna(subset=['bid', 'ask'])
    g = ok.groupby(['race', 'b'])
    p = pd.DataFrame({'sb': g.bid.sum(), 'sa': g.ask.sum(), 'n': g.party.count(),
                      'bsz': g.bid_sz.min(), 'asz': g.ask_sz.min()}).reset_index()
    p = p[p.n == p.race.map(nlegs)]
    p['t'] = pd.to_datetime(p.b, unit='s')
    return p[['race', 't', 'sb', 'sa', 'n', 'bsz', 'asz']]


def proxy_panel():
    p = pd.read_csv(f'{ROOT}/data/research_arb_convergence/race_paths_30min.csv')
    p = p[p.panel == 'A'].rename(columns={'sum_bids': 'sb', 'sum_asks': 'sa', 'legs': 'n'})
    p['t'] = pd.to_datetime(p.ts_utc)
    p['bsz'] = np.nan; p['asz'] = np.nan
    return p[['race', 't', 'sb', 'sa', 'n', 'bsz', 'asz']]


# ---------------------------------------------------------------- idea 1
_PREP = {}


def prep(p, L, key):
    if (key, L) in _PREP: return _PREP[(key, L)]
    lst = []
    for race, r in p.groupby('race'):
        r = r.set_index('t').sort_index()
        r = r[~r.index.duplicated()].asfreq('30min').ffill(limit=2)
        if len(r) < 2 * L: continue
        lb = int(L * 2)
        f = lambda c, w, mp: r[c].rolling(w, min_periods=mp).median().shift(1).values
        lst.append((race, r.index, r.sb.values, r.sa.values, f('sb', lb, lb // 2), f('sa', lb, lb // 2),
                    f('sa', 48, 24), f('sb', 48, 24), r.bsz.values, r.asz.values, r.n.values))
    _PREP[(key, L)] = lst
    return lst


def sim1(p, L, k, H, exit_rule, side, key):
    """One position per race at a time. Entry when the sum is k ticks rich (sell) / cheap (buy) vs its trailing
    L-hour median. Exit at H hours, or (level0) at the first bar where the exit price is at least as good as the
    trailing 24-h median exit price, else at H. P&L per set in ticks; trades past the data end are dropped."""
    out = []
    end = p.t.max(); hb = int(H * 2)
    for race, idx, sb, sa, msb, msa, m24a, m24b, bsz, asz, n in prep(p, L, key):
        N = len(idx)
        dev = (sb - msb) / TICK if side == 'sell' else (msa - sa) / TICK
        i = 0
        while i < N:
            if not dev[i] >= k: i += 1; continue
            if idx[i] + pd.Timedelta(hours=H) > end: break
            hi = min(i + hb, N - 1); xj = None
            if exit_rule == 'level0':
                for j in range(i + 1, hi + 1):
                    if (side == 'sell' and sa[j] <= m24a[j]) or (side == 'buy' and sb[j] >= m24b[j]): xj = j; break
            if xj is None:
                xj = hi
                while xj > i and np.isnan(sa[xj]): xj -= 1
            pnl = (sb[i] - sa[xj]) / TICK if side == 'sell' else (sb[xj] - sa[i]) / TICK
            if not np.isnan(pnl) and xj > i:
                out.append(dict(race=race, t=str(idx[i]), pnl=pnl, dev=dev[i], hold_h=(xj - i) / 2,
                                size=bsz[i] if side == 'sell' else asz[i], n=n[i],
                                cost=(n[i] - sb[i]) if side == 'sell' else sa[i]))
            i = xj + 1
    return out


def idea1():
    A, B = proxy_panel(), real_panel()
    grid = []
    for side, L, k, H, ex in itertools.product(['sell', 'buy'], [24, 48, 72], [2, 3, 4, 6, 8], [6, 24, 48, 72],
                                               ['fixed', 'level0']):
        row = dict(side=side, L=L, k=k, H=H, exit=ex)
        for name, p in (('A', A), ('B', B)):
            if name == 'B' and (L > 48 or H > 48): continue
            tr = sim1(p, L, k, H, ex, side, name)
            m, t, n, G = ctstat([x['pnl'] for x in tr], [x['race'] for x in tr])
            row.update({f'{name}_mean': m, f'{name}_t': t, f'{name}_n': n, f'{name}_races': G})
            if name == 'B' and n:
                days = (p.t.max() - p.t.min()).total_seconds() / 86400 - L / 24
                sz = np.nan_to_num([min(x['size'] or 0, 2000) for x in tr])
                row['B_sets_per_day'] = float(sz.sum() / days)
                row['B_S_per_day'] = float((sz * np.array([x['pnl'] for x in tr]) * TICK).sum() / days)
                row['B_capital_peak'] = float(max(sz * np.array([x['cost'] for x in tr])))
        grid.append(row)
        print({k_: (round(v, 2) if isinstance(v, float) else v) for k_, v in row.items()}, flush=True)
    g = pd.DataFrame(grid)
    g.to_csv(f'{OUT}/idea1_grid.csv', index=False)
    # IS selection: best A t among cells with A_n >= 20 that also have a B evaluation
    cand = g[(g.A_n >= 20) & g.B_n.notna()].sort_values('A_t', ascending=False)
    best = cand.head(5)
    print('\nIS-best (selected on proxy A, evaluated on real B):\n', best.to_string())
    json.dump({'best_is': best.to_dict('records'),
               'B_positive_t2': g[(g.B_t > 2) & (g.B_n >= 10)].to_dict('records'),
               'A_days': [str(A.t.min()), str(A.t.max())], 'B_days': [str(B.t.min()), str(B.t.max())]},
              open(f'{OUT}/idea1.json', 'w'), indent=1, default=str)



# ---------------------------------------------------------------- idea 2
def daily_legs():
    """One price per leg per day: median 5-min VWAP (candles, 14-21 Oct) or median quote mid (22-25 Oct) over
    15:00-21:00 UTC; plus the leg's median quoted spread (real days)."""
    con = sqlite3.connect(DB)
    q = pd.read_sql("select ts, race, party, instrument, bid, ask from venue_quotes where venue='sig'", con)
    q['t'] = pd.to_datetime(q.ts, unit='s'); q = q[(q.t.dt.hour >= 15) & (q.t.dt.hour < 21)].dropna()
    q['mid'] = (q.bid + q.ask) / 2; q['spr'] = q.ask - q.bid; q['d'] = q.t.dt.normalize()
    real = q.groupby(['instrument', 'd']).agg(p=('mid', 'median'), spr=('spr', 'median'),
                                              race=('race', 'first'), party=('party', 'first')).reset_index()
    meta = real.groupby('instrument')[['race', 'party']].first()
    c = json.load(open(f'{ROOT}/data/candles_5m.json'))
    rows = []
    for ex, v in c.items():
        df = pd.DataFrame(v['candles'])
        if df.empty: continue
        df['t'] = pd.to_datetime(df.time).dt.tz_localize(None)
        df = df[(df.t.dt.hour >= 15) & (df.t.dt.hour < 21) & (df.volume > 0)]
        for d, g in df.groupby(df.t.dt.normalize()):
            rows.append(dict(instrument=ex, d=d, p=float(g.vwap.median()), spr=np.nan))
    prox = pd.DataFrame(rows)
    prox = prox[prox.instrument.isin(meta.index)].join(meta, on='instrument')
    return pd.concat([prox, real], ignore_index=True)


def idea2():
    L = daily_legs()
    spr = L.groupby('instrument').spr.median().fillna(TICK)
    piv = L.pivot_table(index='d', columns='instrument', values='p')
    race = L.groupby('instrument').race.first()
    days = list(piv.index)
    res = {}
    rec = []
    for i, d in enumerate(days):
        for h in (1, 2, 3, 5, 7):
            if i + h >= len(days): continue
            p0, p1 = piv.iloc[i], piv.iloc[i + h]
            for ex in piv.columns:
                a, b = p0[ex], p1[ex]
                if np.isnan(a) or np.isnan(b): continue
                rec.append(dict(d=d, h=h, ex=ex, race=race[ex], p=a, dp=(b - a) / TICK, cost=spr[ex] / TICK))
    R = pd.DataFrame(rec)
    R['split'] = np.where(R.d <= pd.Timestamp('2026-10-19'), 'train', 'test')
    bins = [0, .1, .25, .45, .55, .75, .9, 1]
    R['bucket'] = pd.cut(R.p, bins).astype(str)
    out = []
    for (h, sp, bk), g in R.groupby(['h', 'split', 'bucket']):
        # favourites: buy YES (gain dp); longshots (< 0.45): sell YES = buy NO (gain -dp); both pay the spread once
        sgn = 1 if g.p.iloc[0] >= 0.5 else -1
        net = sgn * g.dp - g.cost
        m, t, n, G = ctstat(net, g.race)
        mg, tg, _, _ = ctstat(sgn * g.dp, g.race)
        md, td, _, Gd = ctstat(net, g.d.astype(str))
        out.append(dict(h=h, split=sp, bucket=bk, gross=mg, gross_t=tg, net=m, t_race=t, t_day=td, n=n, races=G, days=Gd))
    O = pd.DataFrame(out)
    pd.set_option('display.width', 250)
    print(O.round(2).to_string(index=False))
    # slope of dp on (p - 0.5), clustered by race
    for (h, sp), g in R.groupby(['h', 'split']):
        x = (g.p - 0.5).values; y = g.dp.values
        b = (x * (y - y.mean())).sum() / ((x - x.mean()) ** 2).sum()
        e = y - y.mean() - b * (x - x.mean())
        s = pd.Series((x - x.mean()) * e).groupby(g.race.values).sum().values
        se = math.sqrt((s ** 2).sum()) / ((x - x.mean()) ** 2).sum()
        print(f'h={h} {sp}: slope {b:+.2f} ticks per unit (p-0.5), t {b / se:+.2f}, n {len(g)}')
    O.to_csv(f'{OUT}/idea2_buckets.csv', index=False)
    R.to_csv(f'{OUT}/idea2_rows.csv', index=False)
    print('days', [str(x.date()) for x in days])


# ---------------------------------------------------------------- idea 3
ST = dict(zip(('Alabama Alaska Arizona Arkansas California Colorado Connecticut Delaware Florida Georgia Hawaii Idaho '
               'Illinois Indiana Iowa Kansas Kentucky Louisiana Maine Maryland Massachusetts Michigan Minnesota '
               'Mississippi Missouri Montana Nebraska Nevada New_Hampshire New_Jersey New_Mexico New_York '
               'North_Carolina North_Dakota Ohio Oklahoma Oregon Pennsylvania Rhode_Island South_Carolina '
               'South_Dakota Tennessee Texas Utah Vermont Virginia Washington West_Virginia Wisconsin Wyoming').split(),
              'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND '
              'OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split()))
# post-call SIG prices per scenario: (winner YES bid, loser YES bid, loser YES ask)
SCEN = {'A persist': (0.945, 0.055, 0.065),   # today's SIG on legs with outside fair >= 0.97 / <= 0.03
        'B decompress': (0.985, 0.005, 0.015),
        'D settle at call': (1.0, 0.0, 0.0)}


def state_of(race):
    if race[2:3] == '-': return race[:2]
    for k, v in ST.items():
        if race.startswith(k.replace('_', ' ')): return v
    return None


def close_h(st, pc):
    hh, mm = map(int, pc[st]['last_close_et'].split(':'))
    return (hh + 24 if hh < 12 else hh) + mm / 60


def walk(levels, q):
    """VWAP shortfall vs the touch (price units) of trading q into a ladder [(price, qty)...]."""
    if not levels or q <= 0: return 0.0
    left, cost = q, 0.0
    for p, n in levels:
        x = min(left, n); cost += x * abs(p - levels[0][0]); left -= x
        if left <= 0: break
    return cost / q if left <= 0 else cost / (q - left) + 0.01


def idea3():
    con = sqlite3.connect(DB)
    pc = json.load(open(f'{ROOT}/data/election_night/poll_close_2026.json'))
    books = json.load(open(f'{ROOT}/data/books_latest.json'))
    lad = lambda ex, side: [(x['price'], x['quantity']) for x in books.get(str(ex), {}).get(side, [])]
    vf = pd.read_sql("select race, party, fair, exchange_id ex from venue_fair where ts=(select max(ts) from venue_fair)", con)
    vq = pd.read_sql("select ts, race, party, instrument ex, bid, ask from venue_quotes where venue='sig' and "
                     "ts > (select max(ts) from venue_quotes where venue='sig') - 7200 and bid is not null", con)
    vq = vq.sort_values('ts').groupby('ex').last().reset_index().drop(columns='ts')
    L = vq.merge(vf[['ex', 'fair']], on='ex', how='left')
    L['fair'] = L.fair.fillna((L.bid + L.ask) / 2)
    L['st'] = L.race.map(state_of)
    wd = json.load(open(f'{ROOT}/params/wind_down_report.json'))
    out = {'scen': SCEN, 'holdings': [], 'buy_now': []}
    # --- holdings that turn into cash at the call: MN-05 sets + cheap-favourite legs
    hold = []
    for r in wd['rows']:
        if r.get('call') == 'early':
            legs = L[L.race == r['holding']]
            hold.append(dict(name=r['holding'], kind=r['kind'], q=r['qty'], st=state_of(r['holding']),
                             today=r['sale_value'], legs=legs.to_dict('records')))
    f = pd.read_sql("select exchange_id ex, sum(quantity) q from fills where purpose='cheapfav' group by ex", con)
    for _, x in f.merge(L, on='ex').iterrows():
        side = 'yes' if x.q > 0 else 'no'
        today = abs(x.q) * (x.bid if side == 'yes' else 1 - x.ask)
        hold.append(dict(name=f'{x.race} {x.party} {side}', kind='cheapfav_' + side, q=abs(x.q), st=x.st, today=today,
                         fair=x.fair, ex=x.ex))
    rows = []
    for h in hold:
        t = close_h(h['st'], pc) + 0.5
        row = dict(name=h['name'], kind=h['kind'], q=h['q'], avail_et=t, today=round(h['today'], 1))
        for sn, (wb, lb, la) in SCEN.items():
            if h['kind'] == 'buy_set':
                # winner = the leg with the higher fair; sell every leg, walked like today's ladder
                legs = sorted(h['legs'], key=lambda z: -z['fair'])
                per = wb + lb * (len(legs) - 1)
                wk = 0 if sn.startswith('D') else sum(walk(lad(z['ex'], 'bids'), h['q']) for z in legs)
                clip = 0 if sn.startswith('D') else sum(walk(lad(z['ex'], 'bids'), 1000) for z in legs)
                row[sn] = h['q'] * (per - wk); row[sn + ' clips'] = h['q'] * (per - clip)
            else:
                pw = h['fair'] if h['kind'] == 'cheapfav_yes' else 1 - h['fair']   # P(the position pays)
                v_win = wb if h['kind'] == 'cheapfav_yes' else 1 - la
                v_lose = lb if h['kind'] == 'cheapfav_yes' else 1 - wb
                wk = 0 if sn.startswith('D') else walk(lad(h['ex'], 'bids' if h['kind'] == 'cheapfav_yes' else 'asks'), h['q'])
                row[sn] = h['q'] * (pw * v_win + (1 - pw) * v_lose - wk); row[sn + ' clips'] = row[sn]
        rows.append(row)
    H = pd.DataFrame(rows)
    pd.set_option('display.width', 250)
    print(H.round(0).to_string(index=False))
    out['holdings'] = H.to_dict('records')
    # --- punt time table: extra cash vs selling every early holding before 18:00 at today's value
    T = [19, 20, 20.5, 21, 21.5, 22, 22.5, 23, 24, 25]
    tab = []
    for t in T:
        a = H[H.avail_et <= t]
        r = {'punt_et': f'{int(t) % 24:02d}:{int(t % 1 * 60):02d}', 'holdings_called': len(a),
             'today_value_called': a.today.sum()}
        for sn in SCEN:
            r['extra ' + sn] = (a[sn] - a.today).sum()
            if sn != 'D settle at call':
                # 1,000-set clips every 30 min for the MN-05 sets
                mn = a[a.kind == 'buy_set']
                done = np.clip((t - mn.avail_et) * 2, 0, None) * 1000
                frac = np.minimum(done / mn.q, 1) if len(mn) else 0
                r['extra clips ' + sn] = ((a[a.kind != 'buy_set'][sn] - a[a.kind != 'buy_set'].today).sum()
                                          + ((mn[sn + ' clips'] - mn.today) * frac).sum())
        tab.append(r)
    TB = pd.DataFrame(tab); print(TB.round(0).to_string(index=False)); out['punt_table'] = TB.to_dict('records')
    # --- buy-now: early-call buy-sets (all legs in states closing by 21:00 ET, favourite fair >= 0.95)
    bn = []
    for race, g in L.groupby('race'):
        st = g.st.iloc[0]
        if not isinstance(st, str) or g.ask.isna().any() or close_h(st, pc) > 21 or g.fair.max() < 0.95: continue
        sa = g.ask.sum()
        for q in (1000, 2000, 5000):
            cost = sa + sum(walk(lad(e, 'asks'), q) for e in g.ex)
            r = dict(race=race, legs=len(g), fav_fair=round(g.fair.max(), 3), sa=round(sa, 3), q=q, cost=round(cost, 4))
            for sn, (wb, lb, la) in SCEN.items():
                wk = 0 if sn.startswith('D') else sum(walk(lad(e, 'bids'), q) for e in g.ex)
                r[sn] = round((wb + lb * (len(g) - 1) - wk - cost) * q, 1)
            bn.append(r)
    BN = pd.DataFrame(bn).sort_values(['q', 'A persist'], ascending=[True, False])
    print(BN[BN['A persist'] > 0].to_string(index=False)); print('races checked', BN.race.nunique())
    out['buy_now'] = BN.to_dict('records')
    json.dump(out, open(f'{OUT}/idea3.json', 'w'), indent=1, default=float)


# ---------------------------------------------------------------- figure
def plot():
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    g = pd.read_csv(f'{OUT}/idea1_grid.csv'); i3 = json.load(open(f'{OUT}/idea3.json'))
    fig, ax = plt.subplots(4, 1, figsize=(10, 21), gridspec_kw={'height_ratios': [1, 1, .7, 1]})
    fig.suptitle('Buying sets when the overround is unusually wide, held for days, sold before 3 Nov', fontsize=15,
                 fontweight='bold', y=.995)
    fig.text(.5, .978, 'Sell-sets (NO on every leg) entered when the sum of YES bids is k ticks above its trailing median; '
             'per-set profit after both spreads', ha='center', fontsize=9.5, color='#444')
    ci = lambda r, c: abs(r[f'{c}_mean'] / r[f'{c}_t']) * 1.96 if r[f'{c}_t'] and np.isfinite(r[f'{c}_t']) else np.nan
    a = ax[0]
    for L, col in ((24, '#1f77b4'), (48, '#d62728')):
        for pan, ls, mk, lab in (('A', ':', '^', 'training days 14-21 Oct (trade-price proxy)'),
                                 ('B', '-', 'o', 'test days 22-25 Oct (real quotes)')):
            r = g[(g.side == 'sell') & (g.L == L) & (g.H == 48) & (g.exit == 'fixed')].sort_values('k')
            m = r[f'{pan}_mean'].values; e = np.array([ci(x, pan) for _, x in r.iterrows()])
            a.plot(r.k, m, ls=ls, marker=mk, color=col, label=f'{L}-h median, held 48 h, {lab}')
            if pan == 'B': a.fill_between(r.k, m - e, m + e, color=col, alpha=.12)
            for k_, m_, n_ in zip(r.k, m, r[f'{pan}_n']):
                if pan == 'B' and np.isfinite(m_): a.annotate(f'n={int(n_)}', (k_, m_), fontsize=7, color=col,
                                                               xytext=(4, 4 if L == 24 else -10), textcoords='offset points')
    a.axhline(0, color='grey', lw=1); a.set_ylim(-4, 10)
    a.set_title('A. Profit per set vs how unusual the overround must be at entry', loc='left', fontweight='bold')
    a.set_xlabel('entry threshold k (ticks above the trailing median)'); a.set_ylabel('profit per set after costs (ticks)')
    a.legend(fontsize=8, loc='upper center', bbox_to_anchor=(.5, -.13), ncol=1, frameon=False)
    a = ax[1]
    for (side, ex), col, ls in ((('sell', 'fixed'), '#d62728', '-'), (('sell', 'level0'), '#d62728', '--'),
                                (('buy', 'fixed'), '#1f77b4', '-'), (('buy', 'level0'), '#1f77b4', '--')):
        r = g[(g.side == side) & (g.L == 24) & (g.k == 4) & (g.exit == ex)].sort_values('H')
        a.plot(r.H, r.A_mean, ls=':', marker='^', color=col, alpha=.6)
        rb = r[r.B_n.notna()]
        a.errorbar(rb.H + (0.6 if ex == 'level0' else 0), rb.B_mean, yerr=[ci(x, 'B') for _, x in rb.iterrows()],
                   ls=ls, marker='o', color=col, capsize=3,
                   label=f'{side}-sets, exit {"at the 48-h mark" if ex == "fixed" else "at the usual exit price (wind-down rule) or the max hold"}')
    a.axhline(0, color='grey', lw=1); a.set_xscale('log'); a.minorticks_off(); a.set_xticks([6, 24, 48, 72]); a.set_xticklabels(['6', '24', '48', '72'])
    a.set_title('B. Does holding longer help? (k = 4 ticks, 24-h median; dotted = training, solid = test with 95% range)',
                loc='left', fontweight='bold', fontsize=10)
    a.set_xlabel('maximum hold (hours)'); a.set_ylabel('profit per set (ticks)')
    a.legend(fontsize=8, loc='upper center', bbox_to_anchor=(.5, -.13), frameon=False)
    a = ax[2]
    sys.argv = ['', 'x']; B = real_panel(); tr = pd.DataFrame(sim1(B, 48, 4, 48, 'fixed', 'sell', 'B'))
    tt = pd.to_datetime(tr.t)
    a.scatter(tt, tr.pnl, color='#d62728', zorder=3)
    for _, x in tr.iterrows(): a.annotate(x.race.replace(' House race', ''), (pd.Timestamp(x.t), x.pnl), fontsize=7,
                                          xytext=(3, 3), textcoords='offset points')
    a.axhline(0, color='grey', lw=1); a.set_xlim(pd.Timestamp('2026-10-22'), pd.Timestamp('2026-10-26'))
    a.set_title('C. Why the test is weak: every test trade of the best test cell (48-h median, k = 4, 48 h) entered on '
                '23 Oct', loc='left', fontweight='bold', fontsize=10)
    a.set_ylabel('profit per set (ticks)'); a.set_xlabel('entry time (UTC)')
    a = ax[3]
    T = pd.DataFrame(i3['punt_table'])
    for c, col, ls in (('extra clips A persist', '#1f77b4', '-'), ('extra clips B decompress', '#2ca02c', '-'),
                       ('extra D settle at call', '#9467bd', '--')):
        a.plot(T.punt_et, T[c] / 1000, ls=ls, marker='o', color=col,
               label={'extra clips A persist': 'A: SIG keeps today\'s compression (winner bid 0.945, loser 0.055)',
                      'extra clips B decompress': 'B: SIG reprices to near-certain (winner 0.985, loser 0.005)',
                      'extra D settle at call': 'D: SIG settles the race at the call (pays 1.00)'}[c])
    a.axhline(0, color='grey', lw=1)
    a.set_title('D. Idea 3: extra cash from holding early-call positions (MN-05 sets + cheap favourites) to their call,\n'
                'vs selling them before 18:00 ET at today\'s prices', loc='left', fontweight='bold', fontsize=10)
    a.set_xlabel('punt time (ET, 3-4 Nov)'); a.set_ylabel('extra cash (thousand S)')
    a.legend(fontsize=8, loc='upper center', bbox_to_anchor=(.5, -.13), frameon=False)
    fig.text(.07, .005, 'Glossary\n'
             'set: one contract on every outcome of a race; sell-set = NO on every leg, buy-set = YES on every leg. '
             'tick: 0.005.\n'
             'overround: sum of YES bids (sell side) above 1; "unusually wide" = k ticks above its own trailing 24 h '
             'or 48 h median.\n'
             'profit per set: entry minus exit, both at the touch, so it pays both spreads; no depth walk (sizes not '
             'logged).\n'
             'training / test: rules ranked on 14-21 Oct (trade-price proxy), checked on 22-25 Oct real SIG quotes.\n'
             'early-call: polls close by 21:00 ET and the favourite is safe; available 30 min after the last poll close; '
             'MN-05 sold in 1,000-set clips every 30 min.',
             fontsize=8.5, va='bottom', bbox=dict(boxstyle='round', fc='#f4f4f0', ec='#999'))
    plt.tight_layout(rect=(0, .07, 1, .975))
    fig.savefig(f'{ROOT}/research_notes/figures/arb_ideas_decision.png', dpi=110)


if __name__ == '__main__':
    globals()[sys.argv[1]]()
