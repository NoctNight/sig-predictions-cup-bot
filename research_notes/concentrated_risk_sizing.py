#!/usr/bin/env python3
"""Read-only sizing study for a 'concentrated risk' directional sleeve. Stdlib only. Places no orders.
Run from the project root:  python3 research_notes/concentrated_risk_sizing.py
Inputs: params/fair_explained.json, data/markets.json, data/sigbot.sqlite (opened mode=ro), data/leaderboard_snapshot.json.
"""
import json, re, sqlite3, random, math, time, sys
from statistics import NormalDist

TICK = 0.005
ND = NormalDist()
N_SIM, SEED = 20000, 20261104
SIGMA_MARGIN, SIGMA_SWING = 5.3, 3.0
PROPOSAL = dict(total=40000.0, leg=6000.0, netd=15000.0)
CURRENT = dict(total=3000.0, leg=300.0, netd=5000.0)

# ---------- load legs ----------
fe = json.load(open('params/fair_explained.json'))
mk = json.load(open('data/markets.json'))
exid = {}
for m in mk:
    g = re.match(r"Will the (Democratic|Republican|Independent) Party win the (.*)\?", m['title'])
    if g:
        exid[(g.group(2), g.group(1)[0])] = m['exchanges'][0]['id']
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
now = time.time()

def latest_quote(ex):
    """latest SIG touch from `quotes`; if that table has no (or an older) row, use the sig rows of venue_quotes
    (same bot log, same SIG touch, written for every leg each poll)."""
    best = None
    r = db.execute("select ts,best_bid,best_ask from quotes where exchange_id=? order by ts desc limit 1", (ex,)).fetchone()
    if r and r[1] is not None and r[2] is not None:
        best = (r[0], r[1], r[2], 'quotes')
    r = db.execute("select ts,bid,ask from venue_quotes where venue='sig' and instrument=? order by ts desc limit 1", (ex,)).fetchone()
    if r and r[1] is not None and r[2] is not None and (best is None or r[0] > best[0]):
        best = (r[0], r[1], r[2], 'venue_quotes')
    return best

legs = []
for k, v in fe['legs'].items():
    if v.get('verdict') not in ('trade_yes', 'trade_no'):
        continue
    g = re.match(r"(.*) \[(.)\]$", k)
    race, party = g.group(1), g.group(2)
    q = latest_quote(exid[(race, party)])
    legs.append(dict(name=k, race=race, party=party, verdict=v['verdict'], ex=exid[(race, party)],
                     bid=q[1], ask=q[2], qage_min=(now - q[0]) / 60, qsrc=q[3],
                     fair=v['believed_fair'], model=v['model'], outside=v['outside_blend'],
                     file_bid=v['sig_bid'], file_ask=v['sig_ask'], conf=v['confidence'], flags=v['flags']))

def entry_price(l, mode):
    """price per share actually paid for the verdict side. YES: bid. NO: 1 - ask (i.e. sell YES at the ask).
    modes: passive | tick (1 tick worse than passive) | take (cross the spread)"""
    if l['verdict'] == 'trade_yes':
        p = {'passive': l['bid'], 'tick': l['bid'] + TICK, 'take': l['ask']}[mode]
    else:
        p = {'passive': 1 - l['ask'], 'tick': 1 - l['ask'] + TICK, 'take': 1 - l['bid']}[mode]
    return round(min(0.995, p), 4)

def win_prob(l, fair_key='fair', shrink=0.0):
    """probability our side pays 1. fair_key picks the belief; shrink moves it toward the venue mid (0=none, 1=full)."""
    pf = l[fair_key]
    mid = (l['bid'] + l['ask']) / 2
    pf = pf + shrink * (mid - pf)
    return pf if l['verdict'] == 'trade_yes' else 1 - pf

def netd_sign(l):  # +1 if the side we hold is long-D
    d = l['party'] == 'D'
    return 1 if (d == (l['verdict'] == 'trade_yes')) else -1

def size_book(caps, mode='passive', dedup=False):
    """greedy by edge/share (passive-entry edge on the base fair); returns {leg_name: notional}."""
    rows = []
    for l in legs:
        p = entry_price(l, mode)
        edge = win_prob(l) - p
        rows.append((edge, l))
    rows.sort(key=lambda t: -t[0])
    out, used, net, races = {}, 0.0, 0.0, set()
    for edge, l in rows:
        if edge <= 0 or (dedup and l['race'] in races):
            continue
        s = netd_sign(l)
        room = min(caps['leg'], caps['total'] - used, caps['netd'] - s * net)
        if room <= 1:
            continue
        out[l['name']] = room; used += room; net += s * room; races.add(l['race'])
    return out

# ---------- race-level simulation ----------
races = sorted({l['race'] for l in legs})
rng = random.Random(SEED)
S = [rng.gauss(0, 1) for _ in range(N_SIM)]
E = [[rng.gauss(0, 1) for _ in races] for _ in range(N_SIM)]

def race_pD(fair_key='fair', shrink=0.0):
    out = {}
    for r in races:
        ps = []
        for l in legs:
            if l['race'] != r: continue
            pf = l[fair_key]; mid = (l['bid'] + l['ask']) / 2
            pf = pf + shrink * (mid - pf)
            ps.append(pf if l['party'] == 'D' else 1 - pf)
        out[r] = min(max(sum(ps) / len(ps), 1e-4), 1 - 1e-4)
    return out

def simulate(book, mode='passive', fair_key='fair', shrink=0.0, idio_sd=None, return_legs=False):
    """P&L of the book over N_SIM elections. Common D-margin shift s~N(0,3); race margin = 5.3*z(pD) + s + e,
    e~N(0, sqrt(5.3^2-3^2)) so each race's marginal P(D wins) equals its pD; idio_sd overrides e's sd (literal variant)."""
    if idio_sd is None:
        idio_sd = math.sqrt(SIGMA_MARGIN ** 2 - SIGMA_SWING ** 2)
    pd = race_pD(fair_key, shrink)
    mu = {r: SIGMA_MARGIN * ND.inv_cdf(pd[r]) for r in races}
    pos = []
    for l in legs:
        n = book.get(l['name'])
        if not n: continue
        p = entry_price(l, mode)
        pos.append((l, races.index(l['race']), n / p, n, l['party'] == 'D' if l['verdict'] == 'trade_yes' else l['party'] != 'D'))
    pnl = []
    legloss = {l['name']: 0 for l, *_ in pos}
    legpnl = {l['name']: 0.0 for l, *_ in pos}
    for i in range(N_SIM):
        s = SIGMA_SWING * S[i]; tot = 0.0
        for l, ri, sh, n, wants_dwin in pos:
            dwin = (mu[races[ri]] + s + idio_sd * E[i][ri]) > 0
            if dwin == wants_dwin:
                g = sh - n
            else:
                g = -n; legloss[l['name']] += 1
            tot += g; legpnl[l['name']] += g
        pnl.append(tot)
    pnl.sort()
    q = lambda f: pnl[min(N_SIM - 1, int(f * N_SIM))]
    res = dict(mean=sum(pnl) / N_SIM, p5=q(.05), p50=q(.5), p95=q(.95), p_gt10=sum(x < -10000 for x in pnl) / N_SIM,
               p_gt20=sum(x < -20000 for x in pnl) / N_SIM, p_neg=sum(x < 0 for x in pnl) / N_SIM,
               worst=pnl[0], best=pnl[-1], sd=(sum((x - sum(pnl) / N_SIM) ** 2 for x in pnl) / N_SIM) ** .5)
    if return_legs:
        res['legs'] = {k: (legpnl[k] / N_SIM, legloss[k] / N_SIM) for k in legloss}
    return res

def f0(x): return f"{x:,.0f}"
def pc(x): return f"{100*x:.1f}%"

if __name__ == '__main__':
    print(f"# legs={len(legs)}  quotes: " + ", ".join(f"{l['name']}={l['qsrc']}/{l['qage_min']:.0f}m" for l in legs))
    book = size_book(PROPOSAL); cur = size_book(CURRENT); bookD = size_book(PROPOSAL, dedup=True)
    print("\n## 1. per-leg table (all verdict legs; passive entry; base fair=believed_fair)")
    print("leg|verdict|bid/ask(file bid/ask)|entry|model|outside|fair|edge/sh|notional|shares|exp.profit|win+|loss-")
    totE = totN = 0
    for l in sorted(legs, key=lambda l: -(win_prob(l) - entry_price(l, 'passive'))):
        p = entry_price(l, 'passive'); w = win_prob(l); e = w - p
        n = book.get(l['name'], 0); sh = n / p if n else 0
        side_model = l['model'] if l['verdict'] == 'trade_yes' else 1 - l['model']
        side_out = l['outside'] if l['verdict'] == 'trade_yes' else 1 - l['outside']
        print(f"{l['name']}|{l['verdict']}|{l['bid']}/{l['ask']} ({l['file_bid']}/{l['file_ask']})|{p:.3f}|{side_model:.3f}|{side_out:.3f}|{w:.3f}|{e:+.4f}|{f0(n)}|{f0(sh)}|{e*sh:,.0f}|{sh-n:,.0f}|{-n:,.0f}")
        totE += e * sh; totN += n
    net = sum(netd_sign(l) * book.get(l['name'], 0) for l in legs)
    print(f"TOTAL notional {f0(totN)} exp profit {f0(totE)} netD {f0(net)}  (cur: notional {f0(sum(cur.values()))}, netD {f0(sum(netd_sign(l)*cur.get(l['name'],0) for l in legs))})")
    print("dedup book:", {k: round(v) for k, v in bookD.items()})
    print("race pairs in book:", sorted(r for r in races if sum(1 for l in legs if l['race'] == r and l['name'] in book) > 1))
    print("\n## 2/3. portfolio scenarios")
    scen = [("base: fair=believed, passive", dict()),
            ("fair shrunk 50% to venue mid", dict(shrink=.5)),
            ("1 tick worse entry", dict(mode='tick')),
            ("shrink 50% + 1 tick worse", dict(shrink=.5, mode='tick')),
            ("take the touch (cross spread)", dict(mode='take')),
            ("truth = outside blend (not believed)", dict(fair_key='outside')),
            ("truth = our model alone", dict(fair_key='model')),
            ("truth = venue mid (full shrink)", dict(shrink=1.0)),
            ("literal noise: e sd 5.3 on top of swing", dict(idio_sd=SIGMA_MARGIN))]
    print("scenario|book|mean|sd|p5|p50|p95|P(<-10k)|P(<-20k)|P(<0)|worst|best")
    for name, kw in scen:
        for bname, b in (("PROPOSAL", book), ("CURRENT", cur), ("PROPOSAL dedup", bookD)):
            r = simulate(b, **kw)
            print(f"{name}|{bname}|{f0(r['mean'])}|{f0(r['sd'])}|{f0(r['p5'])}|{f0(r['p50'])}|{f0(r['p95'])}|{pc(r['p_gt10'])}|{pc(r['p_gt20'])}|{pc(r['p_neg'])}|{f0(r['worst'])}|{f0(r['best'])}")
    print("\n## per-leg contribution (base, PROPOSAL)")
    r = simulate(book, return_legs=True)
    for k, (m, pl) in sorted(r['legs'].items(), key=lambda t: -abs(t[1][0])):
        print(f"{k}|mean pnl {m:,.0f}|P(lose) {pc(pl)}|notional {f0(book[k])}")
    # swing-conditional: what does a 2-sigma adverse national swing do? (deterministic race outcomes via mu+s only)
    print("\n## national swing stress (no idio noise), base fair, PROPOSAL")
    pd = race_pD(); mu = {r_: SIGMA_MARGIN * ND.inv_cdf(pd[r_]) for r_ in races}
    for s in (-12, -9, -6, -3, 0, 3, 6, 9, 12):
        tot = 0
        for l in legs:
            n = book.get(l['name'])
            if not n: continue
            p = entry_price(l, 'passive'); dwin = mu[l['race']] + s > 0
            wants = (l['party'] == 'D') if l['verdict'] == 'trade_yes' else (l['party'] != 'D')
            tot += (n / p - n) if dwin == wants else -n
        print(f"s={s:+d}: pnl {f0(tot)}")
    # upside vs leaderboard
    lb = json.load(open('data/leaderboard_snapshot.json'))
    def rank(pnl): return 1 + sum(1 for x in lb if x['pnl'] > pnl)
    p3 = lb[2]['pnl']; p1 = lb[0]['pnl']
    maxwin = sum(book[k] / entry_price(next(l for l in legs if l['name'] == k), 'passive') - book[k] for k in book)
    print(f"\n## leaderboard: n={len(lb)}; #1 {p1:,.0f}  #3 {p3:,.0f}; rank at +270: {rank(270)}; at +270+EV({totE:,.0f}): {rank(270+totE)}; at +270+max sleeve win({maxwin:,.0f}): {rank(270+maxwin)}; at 270-20000: {rank(270-20000)}")
    print("pnl pctiles of field: ", {q: round(sorted(x['pnl'] for x in lb)[int(q * len(lb))]) for q in (.1, .25, .5, .75, .9, .95)})
    print("gap to #3 / max sleeve win:", round(p3 - 270), round(maxwin), "x:", round((p3 - 270) / maxwin))

    print("\n## sleeve-size scaling (per-leg cap 6,000; base fair; passive)")
    for tot in (5000, 10000, 20000, 40000):
        for dd in (False, True):
            b = size_book(dict(total=float(tot), leg=6000.0, netd=15000.0), dedup=dd)
            r = simulate(b)
            print(f"total {tot:,} dedup={dd}: legs {len(b)} mean {f0(r['mean'])} p5 {f0(r['p5'])} P(<-10k) {pc(r['p_gt10'])} P(<-20k) {pc(r['p_gt20'])} worst {f0(r['worst'])}")
    # mark-to-market path of the proposed book on logged hourly candles (hist.json, ~8 days): would a 2,000 / 400 stop trip?
    hist = json.load(open('data/hist.json'))
    print("\n## hindsight MTM of the proposal book on hist.json hourly closes (book held since window start, entry at first close)")
    ser = {}
    for l in legs:
        n = book.get(l['name'])
        if not n: continue
        c = hist[l['ex']]['candles']
        px = [(x['time'], x['close']) for x in c if x.get('close') is not None]
        # value of our side per share: YES -> close ; NO -> 1-close ; shares fixed at today's passive entry
        sh = n / entry_price(l, 'passive')
        ser[l['name']] = {t: sh * (p if l['verdict'] == 'trade_yes' else 1 - p) for t, p in px}
    times = sorted(set.intersection(*[set(v) for v in ser.values()]))
    val = [sum(v[t] for v in ser.values()) for t in times]
    print(f"hours {len(times)} from {times[0]} to {times[-1]}")
    worst1h = max(max(val[max(0, i - 1):i + 1]) - val[i] for i in range(len(val)))   # peak within trailing ~1-2 candles
    worst24 = max(max(val[max(0, i - 24):i + 1]) - val[i] for i in range(len(val)))
    ch1 = [val[i] - val[i - 1] for i in range(1, len(val))]
    print(f"worst hourly drop {-min(ch1):,.0f}; worst peak-to-now within 24h {worst24:,.0f}; worst 1h peak-to-now {worst1h:,.0f}; start {val[0]:,.0f} end {val[-1]:,.0f}")
    print("stdev of hourly MTM change", round((sum(x*x for x in ch1)/len(ch1))**.5), "; a uniform 1-point move in all 7 prices = ", round(sum(book[k]/entry_price(next(l for l in legs if l['name']==k),'passive') for k in book)*0.01))
    # same on the current-caps book
    curval = {}

    # attribution of the left tail (base fair): which races lose in elections where the book is down > 10k / > 0
    pd = race_pD(); mu = {r_: SIGMA_MARGIN * ND.inv_cdf(pd[r_]) for r_ in races}
    idio = math.sqrt(SIGMA_MARGIN ** 2 - SIGMA_SWING ** 2)
    from collections import Counter
    c10, c0, nn10, nn0, allwin = Counter(), Counter(), 0, 0, 0
    for i in range(N_SIM):
        s = SIGMA_SWING * S[i]; tot = 0.0; lost = set()
        for l in legs:
            n = book.get(l['name'])
            if not n: continue
            p = entry_price(l, 'passive'); ri = races.index(l['race'])
            dwin = mu[l['race']] + s + idio * E[i][ri] > 0
            wants = (l['party'] == 'D') if l['verdict'] == 'trade_yes' else (l['party'] != 'D')
            if dwin == wants: tot += n / p - n
            else: tot -= n; lost.add(l['race'])
        allwin += (not lost)
        if tot < 0: nn0 += 1; c0.update(lost)
        if tot < -10000: nn10 += 1; c10.update(lost)
    print(f"\n## tail attribution (PROPOSAL, base): P(all legs win)={pc(allwin/N_SIM)}; of {nn0} losing elections, share where race lost: " + ", ".join(f"{k} {pc(v/nn0)}" for k, v in c0.most_common()))
    print(f"of {nn10} elections with loss > 10k: " + ", ".join(f"{k} {pc(v/nn10)}" for k, v in c10.most_common()))
