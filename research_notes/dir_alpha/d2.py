"""Drill-down 2: map of the fresh-gap signal by threshold in cents, window, hold, office and price band. Fixed thresholds, so no look-ahead in the cut."""
import sys, numpy as np, pandas as pd, json
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, H
L = Lab()
mid, gap = L.mid, L.gap
rows = []
W = {"12h": 144, "24h": 288, "48h": 576, "72h": 864}
HOLD = {"2h": 24, "4h": 48, "8h": 96, "12h": 144, "24h": 288, "36h": 432, "48h": 576}
for wn, w in W.items():
    s = gap - gap.rolling(w, min_periods=w // 2).median()
    for off in (None, "senate", "governor", "house"):
        for band, lo, hi in (("all", 0, 1), ("15-85", .15, .85), ("25-75", .25, .75)):
            M = ((mid >= lo) & (mid <= hi)).values.copy()
            if off: M[:, L.office != off] = False
            for th in (0.01, 0.015, 0.02, 0.03, 0.04, 0.05):
                for hn, h in HOLD.items():
                    tr = L.trades(s, h, thr=th, mask=M)
                    if tr is None or len(tr) < 15: continue
                    ts = L.idx[tr[:, 1].astype(int)]
                    a, b = tr[ts < L.SPLIT], tr[ts >= L.SPLIT]
                    days = pd.Series(tr[:, 4]).groupby(L.day[tr[:, 1].astype(int)]).mean()
                    r = {"w": wn, "office": off or "all", "band": band, "th": th, "h": hn, "n": len(tr), "races": len(set(L.race[tr[:, 0].astype(int)])), "net": 100 * tr[:, 4].mean(), "gross": 100 * tr[:, 3].mean(), "win": (tr[:, 4] > 0).mean(), "days+": int((days > 0).sum()), "days": len(days)}
                    r["t_race"] = L._t(tr[:, 4], L.race[tr[:, 0].astype(int)])[0]
                    r["t_day"] = L._t(tr[:, 4], L.day[tr[:, 1].astype(int)])[0]
                    r["train"] = 100 * a[:, 4].mean() if len(a) >= 5 else np.nan
                    r["test"] = 100 * b[:, 4].mean() if len(b) >= 5 else np.nan
                    r["n_test"] = len(b)
                    rows.append(r)
    print(wn, len(rows), flush=True)
A = pd.DataFrame(rows); A.to_pickle("data/research_diralpha/d2.pkl")
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 30)
G = A[(A.train > 0) & (A.test > 0) & (A.n >= 25) & (A.n_test >= 10)].copy()
G["score"] = G.net * np.sqrt(G.n)
print(G.sort_values("score", ascending=False).head(30).round(2).to_string(index=False))
print(A[(A.w == "48h") & (A.band == "all")].pivot_table(index=["office", "th"], columns="h", values="net").round(2)[["2h", "4h", "8h", "12h", "24h", "36h", "48h"]].to_string())
print(A[(A.w == "48h") & (A.band == "all")].pivot_table(index=["office", "th"], columns="h", values="n").round(0)[["4h", "24h"]].to_string())
