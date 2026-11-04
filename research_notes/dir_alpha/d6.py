"""Drill-down 6: copy speed. For every 2+ point outside move in 1 h, how much has SIG copied after 1, 4, 12, 24, 48 h, and how much of the
outside move stayed? Split by office, direction, clock. This explains where the lag lives. A10, A11, A24."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab()
mid, gap, fair = L.mid, L.gap, L.fair
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 40)
d = L.lag(fair, 12)
dp, dk = L.lag(L.pm, 12), L.lag(L.k, 12)
v = d.values; m = mid.values; f = fair.values
rows = []
for j in range(v.shape[1]):
    last = -10**9
    for i in np.flatnonzero(np.abs(np.nan_to_num(v[:, j])) >= .02):
        if i - last < 72 or i + 12 >= v.shape[0] or i < 12: continue
        last = i
        s = np.sign(v[i, j])
        r = dict(j=j, i=i, s=s, mv=abs(v[i, j]), both=(dp.values[i, j] * s >= .01) and (dk.values[i, j] * s >= .01), f0=f[i, j], m0=m[i, j], dm0=s * (m[i, j] - m[i - 12, j]), gap0=s * (f[i, j] - m[i - 12, j]))
        for h, n in ((12, "1h"), (48, "4h"), (144, "12h"), (288, "24h"), (576, "48h")):
            if i + h < v.shape[0]:
                r["sig" + n] = s * (m[i + h, j] - m[i - 12, j])
                r["out" + n] = s * (f[i + h, j] - f[i - 12, j])
        rows.append(r)
E = pd.DataFrame(rows)
E["office"] = L.office[E.j]; E["party"] = L.party[E.j]; E["hour"] = L.hour[E.i]; E["day"] = L.day[E.i]
et = (E.hour - 4) % 24
E["clock"] = pd.cut(et, [-1, 6, 9, 17, 23], labels=["night 0-6 ET", "morning 7-9 ET", "day 10-17 ET", "evening 18-23 ET"])
wk = pd.to_datetime(L.idx[E.i], unit="s", utc=True).dayofweek
E["weekend"] = wk >= 5
E["toward50"] = (E.s * (.5 - E.f0)) > 0
E["closing"] = E.gap0 > E.mv                      # outside moved toward SIG's level or away: gap before the move in the move's direction
E["pre_gap_same"] = (E.s * (E.f0 - E.m0)) > 0
print("events", len(E), "both venues", int(E.both.sum()))


def tab(by, sub=None):
    x = E if sub is None else E[sub]
    g = x.groupby(by, observed=True)
    t = g.agg(n=("mv", "size"), races=("j", "nunique"), move=("mv", lambda v_: 100 * v_.mean()), c1=("sig1h", lambda v_: 100 * v_.mean()), c4=("sig4h", lambda v_: 100 * v_.mean()), c12=("sig12h", lambda v_: 100 * v_.mean()), c24=("sig24h", lambda v_: 100 * v_.mean()), c48=("sig48h", lambda v_: 100 * v_.mean()), o24=("out24h", lambda v_: 100 * v_.mean()), o48=("out48h", lambda v_: 100 * v_.mean()))
    print("\n--", by); print(t.round(2).to_string())


for b in ("office", "both", "clock", "weekend", "toward50", "pre_gap_same"):
    tab(b)
tab(["office", "both"]); tab(["clock", "both"]); tab(["office", "clock"], E.both)
tab(pd.cut(E.mv, [.02, .03, .05, .08, .5]), E.both)
print("\nBy day, both venues"); tab("day", E.both)
# does the outside move stick? fade noise at night
E["kept24"] = E.out24h / E.mv
print(E.groupby(["clock", "both"], observed=True).kept24.median().round(2).to_string())
E.to_pickle("data/research_diralpha/copy_events.pkl")
