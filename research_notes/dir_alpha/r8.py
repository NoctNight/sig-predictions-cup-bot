"""Round 8: polls (H29, A18) and our own model (H30). Fresh public VoteHub polls were saved under data/research_diralpha/polls/.
A poll is dated by created_at only, so entry is the next 00:00 UTC, which is late on purpose."""
import json, re, sys
import numpy as np, pandas as pd
from statistics import NormalDist
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, survivors, show
L = Lab()
mid, gap, fair = L.mid, L.gap, L.fair
meta = L.meta
rm = json.load(open("data/race_map.json"))["races"]
last = lambda s: re.sub(r"[^a-z]", "", re.sub(r"\(.*?\)", "", s).strip().split()[-1].lower()) if s.strip() else ""
names = {}
for r, e in rm.items():
    m = {}
    for p, ls in e.get("polymarket", {}).get("legs", {}).items():
        for l in ls:
            t = l.get("title") or ""
            if not re.match(r"^(Democrat|Republican|Independent|Other)", t): m[last(t)] = p
    for p, ls in e.get("kalshi", {}).get("legs", {}).items():
        for l in ls:
            c = l.get("candidate") or ""
            if c and not re.search("party", c, re.I): m.setdefault(last(c), p)
    names[r] = m
rows = []
for pt, suffix in (("governor", " Governor"), ("us-senator", " Senate"), ("us-representative", " House race")):
    d = json.load(open(f"data/research_diralpha/polls/{pt}.json"))
    d = d.get("data", d) if isinstance(d, dict) else d
    for p in d:
        subj = (p.get("subject") or "").replace("2026 ", "")
        race = subj + suffix
        if race not in names or p.get("internal"): continue
        by = {}
        for a in p.get("answers", []):
            pa = names[race].get(last(a.get("choice") or ""))
            if pa: by[pa] = max(by.get(pa, 0), float(a.get("pct") or 0))
        if "D" in by and "R" in by:
            rows.append(dict(race=race, created=p["created_at"], end=p["end_date"], m=by["D"] - by["R"], n=p.get("sample_size") or 500, partisan=p.get("partisan"), pollster=p.get("pollster")))
P = pd.DataFrame(rows).sort_values("created")
print("polls matched", len(P), "races", P.race.nunique(), "created 22-30 Oct", int((P.created >= "2026-10-22").sum()))
nd = NormalDist()
S1 = pd.DataFrame(np.nan, index=mid.index, columns=mid.columns); S2 = S1.copy(); S3 = S1.copy()
key = {(a, b): e for e, a, b in zip(meta.index, meta.race, meta.party)}
ix = pd.Series(L.idx, index=pd.to_datetime(L.idx, unit="s", utc=True))
n = 0
for r in P[P.created >= "2026-10-21"].itertuples():
    prev = P[(P.race == r.race) & (P.end < r.end)].tail(6)
    t = pd.Timestamp(r.created, tz="UTC") + pd.Timedelta(days=1)
    b = int(t.timestamp())
    if b not in S1.index: continue
    D = key.get((r.race, "D")); Rl = key.get((r.race, "R"))
    if D is None or Rl is None or not np.isfinite(mid.at[b, D]): continue
    n += 1
    x = mid.at[b, D]
    implied = 8 * nd.inv_cdf(min(max(x, .01), .99))
    if len(prev):
        sur = r.m - prev.m.mean()
        S1.at[b, D] = sur; S1.at[b, Rl] = -sur
    e = r.m - implied
    S2.at[b, D] = e; S2.at[b, Rl] = -e
    if r.partisan is None:
        S3.at[b, D] = e; S3.at[b, Rl] = -e
print("poll events", n)
fm = json.load(open("params/fair_model.json"))["races"]
Sm = pd.DataFrame(np.nan, index=mid.index, columns=mid.columns)
c = 0
for r, e_ in fm.items():
    for pa, l in e_.get("legs", {}).items():
        ex = key.get((r, pa))
        if ex and isinstance(l, dict) and l.get("fair") is not None and ex in Sm.columns:
            Sm[ex] = l["fair"] - mid[ex]; c += 1
print("model legs", c)
sig = {"H29surprise": S1, "H29edge": S2, "H29edgenonpartisan": S3, "H29fade": -S1}
A_ = [L.test(k, s, hs=("12h", "24h"), qs=(0.0, .5, .8), offices=(None, "senate", "governor", "house"), family=k) for k, s in sig.items()]
A_.append(L.test("H30model", Sm, hs=("12h", "24h"), qs=(.8, .95), family="H30model"))
A_.append(L.test("H30modelagree", Sm.where(np.sign(Sm) == np.sign(gap - gap.rolling(576, min_periods=288).median())), hs=("12h", "24h"), qs=(.8, .95), family="H30modelagree"))
A = pd.concat(A_, ignore_index=True)
print("tests", L.n_tests)
Sv = survivors(A, min_n=10); print("survivors", len(Sv)); show(Sv, 20)
print("best:"); show(A[A.get("test_n", 0) >= 10].sort_values("test_net_c", ascending=False), 14)
print(A.groupby(["family", "h"]).test_net_c.median().unstack().round(2).to_string())
# does the gap load on polls or the model (A18): cross-section on the last day
last_ = mid.iloc[-1]
y = gap.iloc[-1]; x = Sm.iloc[-1]
ok = y.notna() & x.notna() & (mid.iloc[-1].between(.1, .9))
print("A18 corr(gap, model minus SIG), last bin:", round(np.corrcoef(y[ok], x[ok])[0, 1], 3), "n", int(ok.sum()))
