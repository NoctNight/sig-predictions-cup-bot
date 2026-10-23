"""Long-volatility study (2026-10-23, long-vol side session). Read-only; no venue calls.

Part 1: ways to be long volatility on SIG, tested net of spread.
Part 2: which SIG legs are most volatile, and does anything predict future volatility out of sample.

Usage:  <venv with numpy+scipy+matplotlib>/bin/python research_notes/long_vol_study.py [build|part2|part1|night|fig|all]
Outputs go to data/research_long_vol/ and research_notes/figures/long_vol*.png.

Data: SIG mids on a 5-min UTC grid. Before 21 Oct 22:43 the mid is the last candle close (5m where the 5m span covers,
else 1h, else 1d; forward-filled). From 21 Oct 22:43 it is the live mid (venue_fair, ~85 s; quotes, ~35 s, on MM legs).
Outside fair: Polymarket 1-min + Kalshi 1-min D-leg history (data/research_outside_sweep/raw, 8-23 Oct); R leg = 1 - D.
Model fair: params/fair_explained.json ('model', one snapshot from 22 Oct).
Periods: TRAIN 8-16 Oct; OOS1 17 Oct - 21 Oct 21:30 (candles); OOS2 21 Oct 22:43 - 23 Oct (live quotes).
One tick = 0.005; P&L in ticks per share.
"""
import json, os, sqlite3, sys, math
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
D = lambda *p: os.path.join(ROOT, 'data', *p)
OUT = D('research_long_vol')
FIG = os.path.join(ROOT, 'research_notes', 'figures')
os.makedirs(OUT, exist_ok=True)
TICK = 0.005
STEP = 300
T0 = datetime(2026, 10, 7, tzinfo=timezone.utc).timestamp()
TRAIN_END = datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()
OOS1_END = datetime(2026, 10, 21, 21, 30, tzinfo=timezone.utc).timestamp()
TRAIN_START = datetime(2026, 10, 8, tzinfo=timezone.utc).timestamp()


def iso(s):
    return datetime.fromisoformat(s[:19] + '+00:00').timestamp()


def category(race):
    if race.startswith('U.S.'):
        return 'chamber'
    if 'House race' in race:
        return 'house'
    if 'Senate' in race:
        return 'senate'
    if 'Governor' in race:
        return 'governor'
    return 'other'


# ----------------------------------------------------------------------------------------------------------- build
def build():
    con = sqlite3.connect('file:' + D('sigbot.sqlite') + '?mode=ro', uri=True)
    live0 = con.execute('select min(ts) from venue_fair').fetchone()[0]
    t_end = con.execute('select max(ts) from venue_fair').fetchone()[0]
    grid = np.arange(T0, t_end, STEP)
    T = len(grid)
    legs = {}
    for ex, race, party in con.execute('select distinct exchange_id, race, party from venue_fair'):
        legs[ex] = (race, party)
    exs = sorted(legs, key=lambda e: (legs[e][0], legs[e][1]))
    L = len(exs)
    idx = {e: i for i, e in enumerate(exs)}
    mid = np.full((L, T), np.nan)
    src = np.zeros((L, T), np.int8)
    vol = np.zeros((L, T))
    hi = np.full((L, T), np.nan)
    lo = np.full((L, T), np.nan)
    spr = np.full((L, T), np.nan)
    dep = np.full((L, T), np.nan)
    bid = np.full((L, T), np.nan)
    ask = np.full((L, T), np.nan)
    ilive = int(np.searchsorted(grid, live0))
    # candles: merged event stream by bar end, finest resolution wins inside its span
    c5, c1h, c1d = (json.load(open(D(f))) for f in ('candles_5m.json', 'hist.json', 'candles_1d.json'))
    res = {'5m': 300, '1h': 3600, '1d': 86400}
    for e in exs:
        i = idx[e]
        ev = []
        spans = {}
        for rk, cc in (('5m', c5), ('1h', c1h), ('1d', c1d)):
            v = cc.get(e)
            if not v or not v.get('candles'):
                continue
            spans[rk] = iso(v['candles'][0]['time'])
            for b in v['candles']:
                ev.append((iso(b['time']) + res[rk], b['close'], rk, b['volume'], b['high'], b['low']))
        f5, f1h = spans.get('5m', 1e18), spans.get('1h', 1e18)
        ev = [x for x in ev if x[2] == '5m' or (x[2] == '1h' and x[0] <= f5) or (x[2] == '1d' and x[0] <= min(f5, f1h))]
        ev.sort()
        code = {'1d': 1, '1h': 2, '5m': 3}
        k = 0
        last = np.nan
        lastsrc = 0
        for j in range(ilive):
            g = grid[j]
            while k < len(ev) and ev[k][0] <= g:
                endt, close, rk, v_, h_, l_ = ev[k]
                last, lastsrc = close, code[rk]
                vol[i, j] += v_
                if rk == '5m':
                    hi[i, j] = h_ if np.isnan(hi[i, j]) else max(hi[i, j], h_)
                    lo[i, j] = l_ if np.isnan(lo[i, j]) else min(lo[i, j], l_)
                k += 1
            mid[i, j] = last
            src[i, j] = lastsrc
    # live period: venue_fair (all legs) then quotes (MM legs, fresher)
    obs = defaultdict(list)
    for ts, ex, b, a in con.execute('select ts, exchange_id, sig_bid, sig_ask from venue_fair where sig_bid is not null and sig_ask is not null'):
        if ex in idx and 0 < b < a < 1:
            obs[ex].append((ts, b, a, np.nan))
    for ts, ex, b, a, bq, aq in con.execute('select ts, exchange_id, best_bid, best_ask, bid_qty, ask_qty from quotes'):
        if ex in idx and b and a and 0 < b < a < 1:
            obs[ex].append((ts, b, a, min(bq or 0, aq or 0)))
    for ex, rows in obs.items():
        i = idx[ex]
        rows.sort()
        ts = np.array([r[0] for r in rows])
        for j in range(ilive, T):
            g = grid[j]
            k1 = np.searchsorted(ts, g, 'right')
            k0 = np.searchsorted(ts, g - STEP, 'right')
            if k1 == 0 or g - ts[k1 - 1] > 1800:
                continue
            b, a, d = rows[k1 - 1][1:]
            mid[i, j] = (b + a) / 2
            bid[i, j], ask[i, j] = b, a
            spr[i, j] = (a - b) / TICK
            seg = rows[k0:k1]
            if seg:
                # live fills: our resting sell at <= max bid fills; our resting buy at >= min ask fills
                hi[i, j] = max(r[1] for r in seg)   # max bid in the bucket: our resting sell at <= this fills
                lo[i, j] = min(r[2] for r in seg)   # min ask in the bucket: our resting buy at >= this fills
            dd = [r[3] for r in seg if not np.isnan(r[3])]
            if dd:
                dep[i, j] = np.median(dd)
            src[i, j] = 4
    # live-period volume from the tape
    for ex, px, q, t in con.execute('select exchange_id, price, quantity, executed_at from tape'):
        if ex in idx:
            tt = iso(t)
            j = int((tt - T0) // STEP) + 1
            if ilive <= j < T:
                vol[idx[ex], j] += abs(q)
    # outside fair, D legs from the raw 1-min cache; R = 1 - D
    fair = np.full((L, T), np.nan)
    rawdir = D('research_outside_sweep', 'raw')
    for fn in os.listdir(rawdir):
        x = json.load(open(os.path.join(rawdir, fn)))
        race = x['race']
        pm = np.array(x.get('pm_hist') or [], float).reshape(-1, 2)
        kc = np.array(x.get('k_candles') or [], float).reshape(-1, 3)
        def at(arr, col, g, stale):
            if len(arr) == 0:
                return np.full(len(g), np.nan)
            o = np.argsort(arr[:, 0]); arr = arr[o]
            k = np.searchsorted(arr[:, 0], g, 'right') - 1
            v = np.where(k >= 0, col(arr[np.maximum(k, 0)]), np.nan)
            v[(k < 0) | (g - arr[np.maximum(k, 0), 0] > stale)] = np.nan
            return v
        p1 = at(pm, lambda r: r[:, 1], grid, 1800)
        p2 = at(kc, lambda r: (r[:, 1] + r[:, 2]) / 2, grid, 3600)
        fD = np.nanmean(np.vstack([p1, p2]), axis=0)
        for e, (rc, pa) in legs.items():
            if rc == race:
                fair[idx[e]] = fD if pa == 'D' else (1 - fD if pa == 'R' else np.nan)
    # after the raw cache ends, venue_fair.fair
    vf = defaultdict(list)
    for ts, ex, f in con.execute('select ts, exchange_id, fair from venue_fair where fair is not null'):
        if ex in idx:
            vf[ex].append((ts, f))
    for ex, rows in vf.items():
        i = idx[ex]
        rows.sort(); ts = np.array([r[0] for r in rows]); fv = np.array([r[1] for r in rows])
        miss = np.isnan(fair[i]) & (grid >= live0)
        k = np.searchsorted(ts, grid, 'right') - 1
        ok = miss & (k >= 0)
        fair[i, ok] = fv[k[ok]]
    fe = json.load(open(os.path.join(ROOT, 'params', 'fair_explained.json')))['legs']
    model = np.array([(fe.get(f'{legs[e][0]} [{legs[e][1]}]') or {}).get('model', np.nan) or np.nan for e in exs], float)
    np.savez_compressed(os.path.join(OUT, 'panel.npz'), grid=grid, mid=mid, src=src, vol=vol, hi=hi, lo=lo, spr=spr, dep=dep,
                        bid=bid, ask=ask, fair=fair, model=model, ilive=ilive)
    json.dump({'exs': exs, 'race': [legs[e][0] for e in exs], 'party': [legs[e][1] for e in exs],
               'cat': [category(legs[e][0]) for e in exs], 'live0': live0}, open(os.path.join(OUT, 'meta.json'), 'w'))
    print('panel', L, 'legs x', T, 'steps; live from', ilive, '; mid coverage', np.isfinite(mid).mean().round(3),
          '; fair coverage', np.isfinite(fair).mean().round(3), '; model legs', np.isfinite(model).sum())


def load():
    z = np.load(os.path.join(OUT, 'panel.npz'))
    P = {k: z[k] for k in z.files}
    P['ilive'] = int(P['ilive'])
    P.update(json.load(open(os.path.join(OUT, 'meta.json'))))
    return P


# ----------------------------------------------------------------------------------------------------------- part 2
def periods(P):
    return [('train', TRAIN_START, TRAIN_END), ('oos1', TRAIN_END, OOS1_END), ('oos2', P['live0'] + 600, 1e20)]


def hourly(P):
    """Hourly mid/fair panels (ticks) with daily-candle-only points masked."""
    m = np.where(P['src'] >= 2, P['mid'], np.nan)
    H = np.arange(0, len(P['grid']), 12)
    return H, m[:, H] / TICK, P['fair'][:, H] / TICK, m / TICK


def roll_rv(d, back, n):
    """sqrt(sum of squared changes) over changes d[n-back .. n-1] for every n, rescaled for missing changes;
    NaN if more than a quarter are missing (the live period has bot-downtime holes)."""
    L, N = d.shape
    out = np.full((L, N + 1), np.nan)
    c = np.concatenate([np.zeros((L, 1)), np.cumsum(np.nan_to_num(d) ** 2, 1)], 1)
    cm = np.concatenate([np.zeros((L, 1)), np.cumsum(np.isnan(d), 1)], 1)
    for k in range(back, N + 1):
        miss = cm[:, k] - cm[:, k - back]
        ok = miss <= back // 4
        out[ok, k] = np.sqrt((c[ok, k] - c[ok, k - back]) * back / (back - miss[ok]))
    return out


def features(P):
    H, mh, fh, m5 = hourly(P)
    g = P['grid'][H]
    d = np.diff(mh, axis=1)                       # change from hour n to n+1, column n
    L, N = mh.shape
    F = {}
    past = {k: roll_rv(d, k, N) for k in (1, 6, 24)}   # column n = changes ending at n
    for k in (1, 6, 24):
        F[f'pastvol_{k}h'] = past[k][:, :N]
    fut = {}
    for k in (1, 6, 24):
        r = roll_rv(d, k, N)                      # r[:, n+k] covers changes n .. n+k-1
        f = np.full((L, N), np.nan)
        f[:, :N - k] = r[:, k:N] if k <= N else f[:, :0]
        fut[k] = f
    jump = np.full((L, N), np.nan)
    a = np.abs(d) >= 5
    for n in range(N - 24):
        w = d[:, n:n + 24]
        jump[:, n] = np.where(np.isnan(w).sum(1) > 6, np.nan, a[:, n:n + 24].any(1))
    # 5-min RV in the past hour (bid-ask bounce inflates it on candles)
    d5 = np.diff(m5, axis=1)
    r5 = roll_rv(d5, 12, d5.shape[1])
    F['rv5m_past_1h'] = r5[:, H]
    F['gap_outside'] = np.abs(mh - fh)
    F['gap_model'] = np.abs(mh - (P['model'][:, None] / TICK))
    pm = mh * TICK
    F['p(1-p) level'] = pm * (1 - pm)
    cv = np.concatenate([np.zeros((L, 1)), np.cumsum(P['vol'], 1)], 1)
    i24 = np.maximum(H - 288, 0)
    F['volume_24h'] = np.log1p(cv[:, H + 1] - cv[:, i24 + 1])
    F['volume_24h'][:, H < 288] = np.nan
    df = np.diff(fh, axis=1)
    F['outside_vol_24h'] = roll_rv(df, 24, N)[:, :N]
    F['spread'] = P['spr'][:, H]
    F['touch_depth'] = np.log1p(P['dep'][:, H])
    cat = np.array(P['cat'])
    return g, F, fut, jump, cat, d


def block_spearman(x, y, g, a, b, block_h, min_n=20):
    sel = np.where((g >= a) & (g < b))[0]
    rows = []
    for n in sel:
        ok = np.isfinite(x[:, n]) & np.isfinite(y[:, n])
        if ok.sum() >= min_n and np.ptp(x[ok, n]) > 0 and np.ptp(y[ok, n]) > 0:
            rows.append((g[n], stats.spearmanr(x[ok, n], y[ok, n])[0], ok.sum()))
    if not rows:
        return None
    t = np.array([r[0] for r in rows]); rho = np.array([r[1] for r in rows])
    blk = ((t - a) // (block_h * 3600)).astype(int)
    bm = np.array([rho[blk == k].mean() for k in np.unique(blk)])
    se = bm.std(ddof=1) / np.sqrt(len(bm)) if len(bm) > 1 else np.nan
    return dict(rho=float(rho.mean()), t=float(bm.mean() / se) if se and se > 0 else np.nan, blocks=len(bm),
                n_legs=int(np.median([r[2] for r in rows])))


def ranking(P):
    H, mh, fh, m5 = hourly(P)
    d = np.diff(mh, axis=1)
    il = P['ilive'] + 2
    d5 = np.diff(m5[:, il:], axis=1)
    dd = np.diff(mh[:, ::24], axis=1)
    rows = []
    for i in range(len(P['exs'])):
        ok = np.isfinite(d[i])
        if ok.sum() < 100:
            continue
        rows.append(dict(leg=f"{P['race'][i]} [{P['party'][i]}]", cat=P['cat'][i],
                         vol_1h=float(np.nanstd(d[i])), vol_1d=float(np.nanstd(dd[i])) if np.isfinite(dd[i]).sum() > 5 else np.nan,
                         vol_5m_live=float(np.nanstd(d5[i])) if np.isfinite(d5[i]).sum() > 100 else np.nan,
                         jumps_1h=int((np.abs(d[i]) >= 5).sum()), hours=int(ok.sum()),
                         jumps_5m_live=int((np.abs(d5[i]) >= 5).sum()),
                         price=float(np.nanmedian(mh[i])) * TICK))
    return rows


def design(F, cat, sel, names, cats):
    """Design matrix (legs x selected hours, raveled): log(0.5 + x) for tick-valued predictors, category dummies."""
    cols = []
    for k in names:
        v = F[k][:, sel]
        cols.append(np.log(0.5 + v) if k not in ('p(1-p) level', 'volume_24h') else v)
    for c in cats[1:]:
        cols.append(np.repeat((cat == c)[:, None], sel.sum(), 1).astype(float))
    return np.stack([c.ravel() for c in cols], 1)


def part2():
    P = load()
    g, F, fut, jump, cat, d = features(P)
    R = {'ranking': ranking(P)}
    res = {}
    for name, x in F.items():
        for h in (1, 6, 24):
            for lab, a, b in periods(P):
                r = block_spearman(x, fut[h], g, a, b, max(6, h))
                if r:
                    res[f'{name}|{h}|{lab}'] = r
    R['spearman'] = res
    # category: mean future 24h vol by category per period
    R['category'] = {}
    for lab, a, b in periods(P):
        sel = (g >= a) & (g < b)
        R['category'][lab] = {c: float(np.nanmean(fut[24][cat == c][:, sel])) for c in sorted(set(cat))}
        groups = [np.nanmean(fut[24][cat == c][:, sel], 1) for c in sorted(set(cat))]
        groups = [x[np.isfinite(x)] for x in groups]
        groups = [x for x in groups if len(x) > 2]
        R['category'][lab]['kruskal_p'] = float(stats.kruskal(*groups).pvalue) if len(groups) > 1 else np.nan
    # market-wide trend: daily median of per-leg hourly abs change
    days = ((g - T0) // 86400).astype(int)
    dm = []
    for k in np.unique(days[:-1]):
        cols = np.where(days[:-1] == k)[0]
        dm.append((k, float(np.nanmean(np.abs(d[:, cols])))))
    R['daily_market_vol'] = dm
    dk = np.array([x for x in dm if x[0] >= 1])
    R['trend_spearman'] = [float(v) for v in stats.spearmanr(dk[:, 0], dk[:, 1])]
    # combined forecast: OLS on log targets, fit on train
    R['model'] = {}
    names = [k for k in F if k not in ('spread', 'touch_depth')]
    cats = sorted(set(cat))
    X = lambda sel: design(F, cat, sel, names, cats)
    per = periods(P)
    sel_tr = (g >= per[0][1]) & (g < per[0][2])
    Xtr = X(sel_tr)
    med = np.nanmedian(Xtr, 0)
    fill = lambda A: np.where(np.isnan(A), med, A)
    for h in (1, 6, 24):
        ytr = np.log(0.5 + fut[h][:, sel_tr].ravel())
        ok = np.isfinite(ytr) & np.isfinite(Xtr[:, :3]).all(1)
        A = np.column_stack([np.ones(ok.sum()), fill(Xtr[ok])])
        beta = np.linalg.lstsq(A, ytr[ok], rcond=None)[0]
        b1 = np.polyfit(Xtr[ok, names.index('pastvol_24h')], ytr[ok], 1)
        out = {'beta': dict(zip(['const'] + names + cats[1:], map(float, beta)))}
        for lab, a, b in per:
            sel = (g >= a) & (g < b)
            Xs = X(sel); ys = np.log(0.5 + fut[h][:, sel].ravel())
            o = np.isfinite(ys) & np.isfinite(Xs[:, :3]).all(1)
            pred = np.column_stack([np.ones(o.sum()), fill(Xs[o])]) @ beta
            p1 = np.polyval(b1, Xs[o, 0 + names.index('pastvol_24h')])
            sst = ((ys[o] - ytr[ok].mean()) ** 2).sum()
            out[lab] = dict(n=int(o.sum()), r2_full=float(1 - ((ys[o] - pred) ** 2).sum() / sst),
                            r2_pastvol=float(1 - ((ys[o] - p1) ** 2).sum() / sst),
                            rho_full=float(stats.spearmanr(pred, ys[o])[0]))
        R['model'][h] = out
    json.dump(R, open(os.path.join(OUT, 'part2.json'), 'w'), indent=1, default=float)
    np.savez_compressed(os.path.join(OUT, 'beta.npz'), med=med, **{f'b{h}': np.array(list(R['model'][h]['beta'].values())) for h in (1, 6, 24)})
    # print tables
    rk = sorted(R['ranking'], key=lambda r: -r['vol_1h'])
    print('top legs by hourly vol (ticks/h std):')
    for r in rk[:15]:
        print(f"  {r['leg']:<34} {r['cat']:<8} p~{r['price']:.2f} vol1h {r['vol_1h']:.2f} vol1d {r['vol_1d']:.1f} vol5m_live {r['vol_5m_live']:.2f} jumps5+/h {r['jumps_1h']} ({r['hours']} h) live5m-jumps {r['jumps_5m_live']}")
    for c in sorted(set(cat)):
        v = [r['vol_1h'] for r in R['ranking'] if r['cat'] == c]
        print(f'  cat {c}: n {len(v)} median vol1h {np.median(v):.2f} p90 {np.percentile(v, 90):.2f}')
    print('daily market vol', [(k, round(v, 3)) for k, v in dm], 'trend rho,p', R['trend_spearman'])
    print('category', json.dumps(R['category'], default=lambda x: round(x, 3)))
    print(f"{'predictor':<18} h  " + '  '.join(f'{p:>18}' for p in ('train', 'oos1', 'oos2')))
    for name in F:
        for h in (1, 6, 24):
            cells = []
            for lab in ('train', 'oos1', 'oos2'):
                r = res.get(f'{name}|{h}|{lab}')
                cells.append(f"{r['rho']:+.3f} t{r['t']:+5.1f} b{r['blocks']:>2}" if r else ' ' * 18)
            print(f'{name:<18} {h:>2} ' + '  '.join(cells))
    for h in (1, 6, 24):
        print('model', h, {k: v for k, v in R['model'][h].items() if k != 'beta'})
    print('beta24', {k: round(v, 3) for k, v in R['model'][24]['beta'].items()})


# ----------------------------------------------------------------------------------------------------------- part 1
def ctstat(x, cl):
    """Mean, race-clustered t, n."""
    x = np.asarray(x, float); cl = np.asarray(cl)
    n = len(x)
    if n < 3:
        return float(np.mean(x)) if n else np.nan, np.nan, n
    mu = x.mean()
    s = sum(((x[cl == c] - mu).sum()) ** 2 for c in np.unique(cl))
    G = len(np.unique(cl))
    se = math.sqrt(s * G / max(G - 1, 1)) / n
    return float(mu), float(mu / se) if se > 0 else np.nan, n


def half_spread(P, i, j):
    s = P['spr'][i, j]
    return 0.5 * s if np.isfinite(s) else 0.5     # candles: assume the 1-tick median spread


def period_of(P, t):
    for lab, a, b in periods(P):
        if a <= t < b:
            return lab
    return None


def summarise(trades, key, extra=''):
    out = {}
    for lab in ('train', 'oos1', 'oos2'):
        tr = [t for t in trades if t['per'] == lab]
        if not tr:
            continue
        x = [t[key] for t in tr if np.isfinite(t[key])]
        cl = [t['race'] for t in tr if np.isfinite(t[key])]
        mu, tt, n = ctstat(x, cl)
        days = {'train': 9.0, 'oos1': 4.9, 'oos2': 2.0}[lab]
        out[lab] = dict(n=n, mean=mu, t=tt, hit=float(np.mean(np.array(x) > 0)) if x else np.nan,
                        p05=float(np.percentile(x, 5)) if x else np.nan, worst=float(np.min(x)) if x else np.nan,
                        per_day=n / days, S_day_1k=mu * TICK * 1000 * n / days if x else np.nan)
    return out


def momentum_trades(P, thr=5, look=12, cool=48, delay=1):
    """Synthetic long straddle on a linear contract: follow a 5+ tick move in an hour, taker in and out."""
    m = np.where(P['src'] >= 3, P['mid'], np.nan) / TICK
    g = P['grid']; L, T = m.shape
    out = []
    for i in range(L):
        nxt = -1
        for j in range(look, T - 1 - delay):
            if j < nxt or not np.isfinite(m[i, j]) or not np.isfinite(m[i, j - look]):
                continue
            mv = m[i, j] - m[i, j - look]
            p = m[i, j] * TICK
            if abs(mv) < thr or not 0.03 <= p <= 0.97:
                continue
            nxt = j + cool
            per = period_of(P, g[j])
            if per is None:
                continue
            side = np.sign(mv)
            je = j + delay
            if not np.isfinite(m[i, je]):
                continue
            entry = m[i, je] + side * half_spread(P, i, je)
            tr = dict(race=P['race'][i], leg=i, t=g[j], per=per, side=side, move=mv, p=p)
            for h, hk in ((1, 12), (4, 48), (24, 288)):
                jx = je + hk
                if jx < T and np.isfinite(m[i, jx]):
                    tr[f'pnl_{h}h'] = side * (m[i, jx] - side * half_spread(P, i, jx) - entry)
                else:
                    tr[f'pnl_{h}h'] = np.nan
            out.append(tr)
    return out


def deep_trades(P, ks=(5, 10, 20)):
    """Resting orders k ticks from the mid, renewed hourly; fill needs a trade through (candles) or a crossed touch (live)."""
    m = P['mid'] / TICK; hi = P['hi'] / TICK; lo = P['lo'] / TICK; src = P['src']
    g = P['grid']; L, T = m.shape
    out = []
    for k in ks:
        for i in range(L):
            for j in range(0, T - 12, 12):
                if src[i, j] < 3 or not np.isfinite(m[i, j]):
                    continue
                per = period_of(P, g[j])
                if per is None:
                    continue
                live = src[i, j] == 4
                for side in (+1, -1):                    # +1 = resting buy below, -1 = resting sell above
                    lvl = m[i, j] - side * k
                    if not 0.01 <= lvl * TICK <= 0.99:
                        continue
                    jf = None
                    for jj in range(j + 1, j + 13):
                        if side > 0:
                            hit = (lo[i, jj] <= lvl) if live else (lo[i, jj] <= lvl - 1)
                        else:
                            hit = (hi[i, jj] >= lvl) if live else (hi[i, jj] >= lvl + 1)
                        if hit:
                            jf = jj; break
                    if jf is None:
                        continue
                    tr = dict(race=P['race'][i], leg=i, t=g[j], per=per, k=k, side=side, p=m[i, j] * TICK)
                    for h, hk in (('flip_1h', 12), ('hold_24h', 288)):
                        jx = jf + hk
                        ok = jx < T and np.isfinite(m[i, jx])
                        tr[h] = side * (m[i, jx] - side * half_spread(P, i, jx) - lvl) if ok else np.nan
                    out.append(tr)
    return out


def basket_trades(P, lo_p=0.05, hi_p=0.30, hold_h=24, step_h=24, start_hour=16):
    """Cheap legs on both sides (convex in the latent margin). Equal shares, taker in and out."""
    m = np.where(P['src'] >= 3, P['mid'], np.nan) / TICK
    g = P['grid']; L, T = m.shape
    party = np.array(P['party']); race = np.array(P['race'])
    dlegs = np.where(party == 'D')[0]
    j0 = int(np.searchsorted(g, T0 + start_hour * 3600))
    hk = hold_h * 12
    out = []
    for j in range(j0, T - hk, step_h * 12):
        per = period_of(P, g[j])
        if per is None or period_of(P, g[j + hk]) != per:
            continue
        sw = m[dlegs, j + hk] - m[dlegs, j]
        swing = float(np.nanmean(sw)) if np.isfinite(sw).sum() > 20 else np.nan
        row = dict(t=g[j], per=per, swing=swing)
        for lab, pr, plo, phi in (('tailD', 'D', lo_p, hi_p), ('tailR', 'R', lo_p, hi_p),
                                  ('favD', 'D', 1 - hi_p, 1 - lo_p), ('favR', 'R', 1 - hi_p, 1 - lo_p)):
            sel = [i for i in range(L) if party[i] == pr and np.isfinite(m[i, j]) and np.isfinite(m[i, j + hk])
                   and plo <= m[i, j] * TICK <= phi]
            pn = [m[i, j + hk] - m[i, j] - half_spread(P, i, j) - half_spread(P, i, j + hk) for i in sel]
            row[lab] = float(np.mean(pn)) if pn else np.nan
            row[lab + '_n'] = len(sel)
            row[lab + '_legs'] = [(race[i], pn[q]) for q, i in enumerate(sel)]
        row['straddle'] = np.nanmean([row['tailD'], row['tailR']])
        row['favs'] = np.nanmean([row['favD'], row['favR']])
        out.append(row)
    return out


def part1():
    P = load()
    R = {}
    mo = momentum_trades(P)
    R['momentum'] = {h: summarise(mo, f'pnl_{h}h') for h in (1, 4, 24)}
    R['momentum_imm'] = {h: summarise(momentum_trades(P, delay=0), f'pnl_{h}h') for h in (1, 4, 24)}
    dp = deep_trades(P)
    R['deep'] = {f'{k}|{h}': summarise([t for t in dp if t['k'] == k], h) for k in (5, 10, 20) for h in ('flip_1h', 'hold_24h')}
    R['basket'] = {}
    for hold, step in ((24, 24), (6, 6)):
        bt = basket_trades(P, hold_h=hold, step_h=step)
        res = {}
        for lab in ('train', 'oos1', 'oos2', 'all'):
            rows = [r for r in bt if (lab == 'all' or r['per'] == lab) and np.isfinite(r['straddle']) and np.isfinite(r['swing'])]
            if len(rows) < 3:
                continue
            x = np.array([r['swing'] for r in rows])
            o = {'n': len(rows)}
            for key in ('straddle', 'favs', 'tailD', 'tailR'):
                y = np.array([r[key] for r in rows])
                ok = np.isfinite(y)
                o[key] = dict(mean=float(y[ok].mean()), t=float(y[ok].mean() / (y[ok].std(ddof=1) / math.sqrt(ok.sum()))) if ok.sum() > 2 else np.nan)
                if ok.sum() > 4:
                    A = np.column_stack([np.ones(ok.sum()), x[ok], x[ok] ** 2])
                    b, res_, *_ = np.linalg.lstsq(A, y[ok], rcond=None)
                    e = y[ok] - A @ b
                    cov = np.linalg.inv(A.T @ A) * (e @ e) / max(ok.sum() - 3, 1)
                    o[key].update(a=float(b[0]), b=float(b[1]), c=float(b[2]), t_c=float(b[2] / math.sqrt(cov[2, 2])))
            o['swing_sd'] = float(x.std())
            res[lab] = o
        R['basket'][f'{hold}h'] = res
        R['basket_rows_%dh' % hold] = [{k: v for k, v in r.items() if not k.endswith('_legs')} for r in bt]
    json.dump(R, open(os.path.join(OUT, 'part1.json'), 'w'), indent=1, default=float)
    json.dump([{k: v for k, v in t.items()} for t in mo], open(os.path.join(OUT, 'momentum_trades.json'), 'w'), default=float)
    fmt = lambda d: '  '.join(f"{lab}: n{v['n']} {v['mean']:+.2f}t t{v['t']:+.1f} hit{v['hit']:.2f} p5 {v['p05']:+.1f} worst {v['worst']:+.1f} {v['per_day']:.1f}/d"
                              for lab, v in d.items())
    for h in (1, 4, 24):
        print(f'momentum next-bar {h}h  ', fmt(R['momentum'][h]))
    for h in (1, 4, 24):
        print(f'momentum same-bar {h}h  ', fmt(R['momentum_imm'][h]))
    for k, v in R['deep'].items():
        print(f'deep {k:<12}', fmt(v))
    for hk, res in R['basket'].items():
        for lab, o in res.items():
            print(f'basket {hk} {lab}: n{o["n"]} swing sd {o["swing_sd"]:.2f}t | ' + ' | '.join(
                f"{k} {o[k]['mean']:+.2f} (t{o[k]['t']:+.1f}) c={o[k].get('c', np.nan):+.3f} t_c {o[k].get('t_c', np.nan):+.1f}" for k in ('straddle', 'favs', 'tailD', 'tailR')))


# ----------------------------------------------------------------------------------------------------------- night
def night():
    """2024 election night (Polymarket/Kalshi): how much and how fast prices move after a state's polls close."""
    import csv
    st = json.load(open(D('election_night', 'study_2024.json')))
    rows = []
    for r in st:
        pc = r['p_close']
        if pc is None or any(r.get(k) is None for k in ('p30', 'p60', 'p120', 'p240')):
            continue
        fav = max(pc, 1 - pc)
        dec = [x for x in (r.get('t95'), r.get('loser95')) if x]
        t_dec = (min(dec) - r['close']) / 60 if dec else np.nan
        hr = (datetime.fromtimestamp(r['close'], timezone.utc).hour - 5) % 24      # EST on 5 Nov 2024
        rows.append(dict(key=r['key'], office=r['office'], fav=fav, hour=hr,
                         m30=abs(r['p30'] - pc), m60=abs(r['p60'] - pc), m120=abs(r['p120'] - pc), m240=abs(r['p240'] - pc),
                         t_dec=max(t_dec, 0) if np.isfinite(t_dec) else np.nan))
    out = {}
    def agg(sub):
        a = lambda k: float(np.mean([x[k] for x in sub]))
        td = [x['t_dec'] for x in sub]
        return dict(n=len(sub), m30=a('m30'), m60=a('m60'), m120=a('m120'), m240=a('m240'),
                    decided_4h=float(np.mean([np.isfinite(t) and t <= 240 for t in td])),
                    med_min_to_decided=float(np.nanmedian(td)) if np.isfinite(td).any() else np.nan)
    for lab, a, b in (('fav>=0.95', 0.95, 1.01), ('0.80-0.95', 0.80, 0.95), ('0.65-0.80', 0.65, 0.80), ('0.50-0.65', 0.5, 0.65)):
        out['fav ' + lab] = agg([x for x in rows if a <= x['fav'] < b])
    comp = [x for x in rows if x['fav'] < 0.80]
    for lab, hs in (('close 19-19:59 ET', (19,)), ('close 20 ET', (20,)), ('close 21 ET', (21,)), ('close 22+ ET', (22, 23, 0, 1))):
        sub = [x for x in comp if x['hour'] in hs]
        if sub:
            out['competitive ' + lab] = agg(sub)
    for off in ('senate', 'governor', 'house', 'president'):
        sub = [x for x in comp if x['office'] == off]
        if sub:
            out['competitive ' + off] = agg(sub)
    # election day before the first close (16:00-18:00 ET) vs after (18:00-24:00 ET), per leg, competitive legs only
    t16, t18, t24 = 1730840400, 1730847600, 1730869200
    legs = defaultdict(list)
    with open(D('election_night', 'paths_2024.csv')) as f:
        for r in csv.DictReader(f):
            if r['price']:
                legs[(r['venue'], r['slug_or_ticker'], r['party'])].append((int(r['ts']), float(r['price'])))
    pre, post = [], []
    for k, v in legs.items():
        v.sort(); ts = np.array([x[0] for x in v]); px = np.array([x[1] for x in v])
        def at(t):
            i = np.searchsorted(ts, t, 'right') - 1
            return px[i] if i >= 0 else np.nan
        p16, p18, p24 = at(t16), at(t18), at(t24)
        if np.isfinite(p16) and 0.15 <= p16 <= 0.85:
            pre.append(abs(p18 - p16)); post.append(abs(p24 - p18))
    out['day_pre_close_16_18ET'] = dict(n=len(pre), mean_abs=float(np.nanmean(pre)), p90=float(np.nanpercentile(pre, 90)))
    out['night_18_24ET'] = dict(n=len(post), mean_abs=float(np.nanmean(post)), p90=float(np.nanpercentile(post, 90)))
    json.dump(out, open(os.path.join(OUT, 'night.json'), 'w'), indent=1)
    for k, v in out.items():
        print(f'{k:<32}', {a: (round(b, 3) if isinstance(b, float) else b) for a, b in v.items()})


# ----------------------------------------------------------------------------------------------------------- figure
def predicted_quintile(P, h=24):
    """Cross-sectional quintile (1 = calmest) of the combined forecast fitted on training hours only."""
    g, F, fut, jump, cat, d = features(P)
    names = [k for k in F if k not in ('spread', 'touch_depth')]
    cats = sorted(set(cat))
    allsel = np.ones(len(g), bool)
    Xa = design(F, cat, allsel, names, cats)
    tr = (g >= TRAIN_START) & (g < TRAIN_END)
    Xt = design(F, cat, tr, names, cats)
    yt = np.log(0.5 + fut[h][:, tr].ravel())
    ok = np.isfinite(yt) & np.isfinite(Xt[:, :3]).all(1)
    med = np.nanmedian(Xt, 0)
    beta = np.linalg.lstsq(np.column_stack([np.ones(ok.sum()), np.where(np.isnan(Xt[ok]), med, Xt[ok])]), yt[ok], rcond=None)[0]
    okA = np.isfinite(Xa[:, :3]).all(1)
    pred = np.full(len(Xa), np.nan)
    pred[okA] = np.column_stack([np.ones(okA.sum()), np.where(np.isnan(Xa[okA]), med, Xa[okA])]) @ beta
    pred = pred.reshape(len(cat), len(g))
    q = np.full(pred.shape, np.nan)
    for n in range(len(g)):
        o = np.isfinite(pred[:, n])
        if o.sum() >= 20:
            r = stats.rankdata(pred[o, n]) / o.sum()
            q[o, n] = np.ceil(r * 5)
    return g, q, fut, jump


def fig():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    import textwrap
    P = load()
    g, q, fut, jump = predicted_quintile(P)
    race = np.array(P['race'])
    rng = np.random.default_rng(7)
    per = periods(P)
    xs = np.arange(1, 6)
    A = {}
    for lab, a, b in per:
        sel = (g >= a) & (g < b)
        y = fut[24][:, sel]; qq = q[:, sel]; jj = jump[:, sel]
        rows = []
        races = np.unique(race)
        for k in xs:
            mk = (qq == k) & np.isfinite(y)
            per_race = np.array([y[race == r_][mk[race == r_]].sum() for r_ in races]); cnt = np.array([mk[race == r_].sum() for r_ in races])
            boot = []
            for _ in range(300):
                ix = rng.integers(0, len(races), len(races))
                boot.append(per_race[ix].sum() / max(cnt[ix].sum(), 1))
            mj = (qq == k) & np.isfinite(jj)
            rows.append(dict(mean=float(y[mk].mean()), lo=float(np.percentile(boot, 2.5)), hi=float(np.percentile(boot, 97.5)),
                             jump=float(jj[mj].mean()) if mj.any() else np.nan, n=int(mk.sum())))
        A[lab] = rows
    # momentum trades by predicted quintile at the signal hour
    mo = momentum_trades(P)
    for t in mo:
        n = int((t['t'] - g[0]) // 3600)
        t['q'] = q[t['leg'], n] if 0 <= n < q.shape[1] else np.nan
    B = {}
    for grp, labs in (('train', ('train',)), ('test', ('oos1', 'oos2'))):
        for h in (4, 24):
            rows = []
            for k in xs:
                tr = [t for t in mo if t['per'] in labs and t['q'] == k and np.isfinite(t[f'pnl_{h}h'])]
                x = [t[f'pnl_{h}h'] for t in tr]
                mu, tt, nn = ctstat(x, [t['race'] for t in tr])
                se = abs(mu / tt) if tt and np.isfinite(tt) and tt != 0 else np.nan
                rows.append(dict(mean=mu, se=se, n=nn, p05=float(np.percentile(x, 5)) if len(x) >= 3 else np.nan,
                                 worst=float(np.min(x)) if x else np.nan))
            B[(grp, h)] = rows
    json.dump({'A': A, 'B': {f'{a}|{b}': v for (a, b), v in B.items()}}, open(os.path.join(OUT, 'figure_data.json'), 'w'), indent=1)
    for lab, rows in A.items():
        print('A', lab, [(round(r['mean'], 2), round(r['jump'], 3), r['n']) for r in rows])
    for k_, rows in B.items():
        print('B', k_, [(round(r['mean'], 2) if np.isfinite(r['mean']) else None, r['n'], r['p05'], r['worst']) for r in rows])

    plt.rcParams.update({'font.size': 10, 'axes.titlesize': 11, 'axes.labelsize': 10, 'xtick.labelsize': 9.5,
                         'ytick.labelsize': 9.5, 'legend.fontsize': 9.5})
    fig = plt.figure(figsize=(10, 17), facecolor='white', layout='constrained')
    gs = fig.add_gridspec(5, 1, height_ratios=[0.35, 4, 4, 4, 2.2])
    hax = fig.add_subplot(gs[0]); hax.axis('off')
    hax.text(0.5, 1.0, 'Can we profit from SIG volatility we can see coming?', ha='center', va='top', fontsize=15, fontweight='bold')
    hax.text(0.5, 0.25, 'Each hour, every SIG leg is ranked by forecast volatility for the next 24 h (model fitted on 8-16 Oct only)',
             ha='center', va='top', fontsize=10.5, color='0.25')
    axes = [fig.add_subplot(gs[i]) for i in (1, 2, 3)]
    cols = {'train': '0.35', 'oos1': 'tab:blue', 'oos2': 'tab:red'}
    names = {'train': 'Training days (8-16 Oct)', 'oos1': 'Test days 1 (17-21 Oct, prices from trades)',
             'oos2': 'Test days 2 (22-23 Oct, live quotes)'}
    for lab, rows in A.items():
        ls = ':' if lab == 'train' else '-'
        axes[0].plot(xs, [r['mean'] for r in rows], ls=ls, marker='^' if lab == 'train' else 'o', color=cols[lab], lw=2,
                     label=f"{names[lab]}: chance of a 5+ tick hourly jump {rows[0]['jump']:.0%} (calmest) to {rows[-1]['jump']:.0%} (most volatile)")
        axes[0].fill_between(xs, [r['lo'] for r in rows], [r['hi'] for r in rows], color=cols[lab], alpha=0.12)
    axes[0].set_title('A. Does the forecast pick the legs that move most in the next 24 hours?', loc='left', fontweight='bold')
    axes[0].set_ylabel('Realised volatility, next 24 h\n(ticks; root of summed squared hourly moves)')
    axes[0].set_ylim(bottom=0)
    axes[0].legend(loc='upper left', bbox_to_anchor=(0, -0.06), frameon=False)
    hc = {4: 'tab:green', 24: 'tab:purple'}
    for (grp, h), rows in B.items():
        ls = ':' if grp == 'train' else '-'
        axes[1].plot(xs, [r['mean'] for r in rows], ls=ls, marker='^' if grp == 'train' else 'o', color=hc[h], lw=2)
        if grp == 'test':
            axes[1].fill_between(xs, [r['mean'] - 1.96 * r['se'] if np.isfinite(r['se']) else r['mean'] for r in rows],
                                 [r['mean'] + 1.96 * r['se'] if np.isfinite(r['se']) else r['mean'] for r in rows], color=hc[h], alpha=0.12)
            for x_, r in zip(xs, rows):
                axes[1].annotate(f"n={r['n']}", (x_, r['mean']), textcoords='offset points', xytext=(6, -12 if h == 4 else 6), fontsize=9, color=hc[h])
        axes[2].plot(xs, [r['p05'] for r in rows], ls=ls, marker='^' if grp == 'train' else 'o', color=hc[h], lw=2)
        if grp == 'test':
            axes[2].scatter(xs, [r['worst'] for r in rows], marker='x', color=hc[h], s=40)
    axes[1].axhline(0, color='0.4', lw=1)
    axes[1].set_title('B. Profit per share of the best long-volatility trade: follow a 5+ tick move', loc='left', fontweight='bold')
    axes[1].set_ylabel('Profit per share after the spread\n(ticks)')
    hl = [Line2D([], [], color=hc[4], lw=2, label='Bought (or sold) with the move, sold 4 h later'),
          Line2D([], [], color=hc[24], lw=2, label='Bought (or sold) with the move, sold 24 h later'),
          Line2D([], [], color='0.3', ls=':', marker='^', label='dotted: Training days (8-16 Oct), average'),
          Line2D([], [], color='0.3', ls='-', marker='o', label='solid: Test days (17-23 Oct), average; band = 95% range')]
    axes[1].legend(handles=hl, loc='upper left', bbox_to_anchor=(0, -0.06), frameon=False)
    axes[2].axhline(0, color='0.4', lw=1)
    axes[2].set_title('C. How bad does it get? (worst 1 trade in 20, and the single worst)', loc='left', fontweight='bold')
    axes[2].set_ylabel('Profit per share, worst 5% of trades\n(ticks)')
    axes[2].legend(handles=hl[:3] + [Line2D([], [], color='0.3', ls='-', marker='o', label='solid: Test days (17-23 Oct), 5th percentile'),
                                     Line2D([], [], color='0.3', ls='', marker='x', label='x: Test days, single worst trade')],
                   loc='upper left', bbox_to_anchor=(0, -0.13), frameon=False)
    for ax in axes:
        ax.set_xticks(xs, ['1\ncalmest fifth', '2', '3', '4', '5\nmost volatile fifth'])
        ax.grid(alpha=0.3)
    axes[2].set_xlabel('Forecast volatility for the next 24 h (fifths of all legs, re-ranked every hour)')
    gax = fig.add_subplot(gs[4]); gax.axis('off')
    gax.add_patch(plt.Rectangle((0, 0), 1, 1, transform=gax.transAxes, facecolor='#f3f3ef', edgecolor='0.6'))
    gax.text(0.01, 0.97, 'Glossary', ha='left', va='top', fontsize=10, fontweight='bold', transform=gax.transAxes)
    gl = ['tick: 0.005, the smallest SIG price step (half a cent)',
          'leg: one YES contract, e.g. Wyoming Senate, Republican',
          'volatility: how much the mid price moves; here the root of the summed squared hourly moves',
          'forecast: a straight-line fit of next-day volatility on past 1/6/24 h volatility, volume, the gap to the Polymarket/Kalshi price, price level and race type',
          'long volatility: a position that gains from big moves either way and loses a little when prices sit still',
          'follow a move: after the mid moves 5+ ticks within an hour, buy at the ask (up move) or sell at the bid (down move) 5 min later',
          'spread: the gap between the best bid and ask; costs half of it on the way in and half on the way out (1 tick assumed on trade-price days)',
          'training / test days: the forecast was fitted on 8-16 Oct and checked on 17-23 Oct']
    wrapped = []
    for t_ in gl:
        w = textwrap.wrap(t_, 62)
        wrapped += [w[0]] + ['    ' + x for x in w[1:]]
    half = (len(wrapped) + 1) // 2
    for c in range(2):
        gax.text(0.01 + c / 2, 0.86, '\n'.join(wrapped[c * half:(c + 1) * half]), ha='left', va='top', fontsize=9,
                 transform=gax.transAxes, linespacing=1.4)
    os.makedirs(FIG, exist_ok=True)
    fig.savefig(os.path.join(FIG, 'long_vol_decision.png'), dpi=130)
    print('wrote', os.path.join(FIG, 'long_vol_decision.png'))


if __name__ == '__main__':
    what = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if what in ('build', 'all'):
        build()
    if what in ('part2', 'all'):
        part2()
    if what in ('part1', 'all'):
        part1()
    if what in ('night', 'all'):
        night()
    if what in ('fig', 'all'):
        fig()
