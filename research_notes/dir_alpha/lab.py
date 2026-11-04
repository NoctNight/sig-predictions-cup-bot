"""Hypothesis-test harness for the overnight directional alpha study (31 Oct). One function per job:

    L = Lab()                       loads the 5-minute panel
    r = L.test(name, S)             S = DataFrame (bins x legs), sign = predicted direction of the SIG YES price, size = strength

test() enters at the touch whenever |S| is in the top quantile, at most once per leg per horizon, and exits at the touch h later,
so every number is net of the full spread (buy at the ask, sell at the bid). Results go to a ledger (every test ever run), so
the multiple-testing count is honest. Training days = before SPLIT, test days = after it; the last LAST days are also shown
because that is the regime we would actually trade in. t statistics: by race (legs of one race count once) and by day
(a national move hits every leg at once, so the race t alone overstates).
A signal survives only if train and test agree in sign and the test net is positive on both t statistics (see survivors()).
"""
import json, os, pickle, time
import numpy as np, pandas as pd

D = "data/research_diralpha/"
BIN = 300
H = {"15m": 3, "1h": 12, "4h": 48, "12h": 144, "24h": 288, "48h": 576}
TICK = 0.005


class Lab:
    def __init__(self, ledger="ledger.jsonl"):
        P = pickle.load(open(D + "panel.pkl", "rb"))
        self.meta = pd.read_pickle(D + "meta.pkl")
        self.cols = list(P["sig_bid"].columns)
        self.meta = self.meta.reindex(self.cols)
        self.P = P
        self.idx = P["sig_bid"].index.values
        b, a = P["sig_bid"], P["sig_ask"]
        ok = (a > b) & (b > 0) & (a < 1)
        self.bid, self.ask = b.where(ok), a.where(ok)
        self.mid = (self.bid + self.ask) / 2
        self.spr = self.ask - self.bid
        self.fair = P["fair"].where((P["fair"] >= 0.005) & (P["fair"] <= 0.995))
        self.pm, self.k = P["pm"], P["kalshi"]
        self.gap = self.fair - self.mid
        self.race = self.meta.race.values
        self.office = self.meta.office.values
        self.party = self.meta.party.values
        self.dsign = pd.Series(np.where(self.party == "D", 1.0, np.where(self.party == "R", -1.0, 0.0)), index=self.cols)
        self.t = pd.to_datetime(self.idx, unit="s", utc=True)
        self.day = np.asarray(self.t.strftime("%m-%d"))
        self.hour = self.t.hour.values
        self.SPLIT = self.idx[0] + 0.5 * (self.idx[-1] - self.idx[0])
        self.LAST = self.idx[-1] - 3 * 86400
        self.B, self.A = self.bid.values, self.ask.values
        self.ledger_path = D + ledger
        self.n_tests = 0

    # ---- generic helpers
    def fw(self, h):
        return self.mid.shift(-h) - self.mid

    def lag(self, df, h):
        return df - df.shift(h)

    def rsum(self, df, h):
        return df.rolling(h, min_periods=1).sum()

    def legs(self, office=None, band=None):
        m = np.ones(len(self.cols), bool)
        if office:
            m &= self.office == office
        return m

    def trades(self, S, h, q=0.9, mask=None, thr=None, cool=None, exec_="touch"):
        """Greedy entries: top-quantile |S|, at most one open per leg. Returns arrays leg, entry bin, sign, gross, net."""
        v = S.reindex(index=self.idx, columns=self.cols).values.astype(float)
        if mask is not None:
            v = np.where(mask, v, np.nan)
        a = np.abs(v)
        if thr is None:
            f = a[np.isfinite(a) & (a > 0)]
            if len(f) < 30:
                return None
            thr = np.quantile(f, q)
        cool = cool or h
        n, m = v.shape
        out = []
        for j in range(m):
            ix = np.flatnonzero(a[:, j] >= max(thr, 1e-12))
            last = -10**9
            for i in ix:
                if i - last < cool or i + h >= n:
                    continue
                s = 1.0 if v[i, j] > 0 else -1.0
                b0, a0, b1, a1 = self.B[i, j], self.A[i, j], self.B[i + h, j], self.A[i + h, j]
                if not (np.isfinite(b0) and np.isfinite(a0) and np.isfinite(b1) and np.isfinite(a1)):
                    continue
                gross = s * ((b1 + a1) / 2 - (b0 + a0) / 2)
                net = (b1 - a0) if s > 0 else (b0 - a1)
                out.append((j, i, s, gross, net))
                last = i
        if not out:
            return None
        return np.array(out)

    @staticmethod
    def _t(values, groups):
        if len(values) < 3:
            return np.nan, 0
        s = pd.Series(values).groupby(np.asarray(groups)).mean()
        n = len(s)
        if n < 3 or s.std() == 0:
            return np.nan, n
        return float(s.mean() / (s.std() / np.sqrt(n))), n

    def score(self, tr):
        j = tr[:, 0].astype(int); i = tr[:, 1].astype(int); net = tr[:, 4]; gross = tr[:, 3]
        races, days = self.race[j], self.day[i]
        res = {"n": len(tr), "races": len(set(races)), "gross_c": 100 * gross.mean(), "net_c": 100 * net.mean(), "win": float((net > 0).mean())}
        res["t_race"], _ = self._t(net, races)
        res["t_day"], _ = self._t(net, days)
        return res

    def test(self, name, S, hs=("1h", "4h", "12h", "24h"), qs=(0.8, 0.95), offices=(None, "senate", "governor", "house"), mask=None, log=True, family=None):
        rows = []
        ts = self.idx
        for hn in hs:
            h = H[hn]
            for q in qs:
                for off in offices:
                    M = np.ones((len(ts), len(self.cols)), bool)
                    if off:
                        M[:, self.office != off] = False
                    if mask is not None:
                        M &= (mask.values if hasattr(mask, "values") else mask)
                    tr = self.trades(S, h, q=q, mask=M)
                    self.n_tests += 1
                    if tr is None:
                        continue
                    tsel = ts[tr[:, 1].astype(int)]
                    a, b, c = tr[tsel < self.SPLIT], tr[tsel >= self.SPLIT], tr[tsel >= self.LAST]
                    r = {"name": name, "family": family or name.split(":")[0], "h": hn, "q": q, "office": off or "all"}
                    for lab, x in (("all", tr), ("train", a), ("test", b), ("last3", c)):
                        if len(x) >= 5:
                            for k, v in self.score(x).items():
                                r[f"{lab}_{k}"] = v
                    rows.append(r)
        if log and rows:
            with open(self.ledger_path, "a") as f:
                for r in rows:
                    f.write(json.dumps(r, default=float) + "\n")
        return pd.DataFrame(rows)

    # ---- permutation null: circular time shift of the signal per leg keeps its autocorrelation, breaks timing
    def perm_p(self, S, h, q, office=None, mask=None, n=200, seed=1, part="all"):
        rng = np.random.default_rng(seed)
        M = np.ones((len(self.idx), len(self.cols)), bool)
        if office:
            M[:, self.office != office] = False
        if mask is not None:
            M &= (mask.values if hasattr(mask, "values") else mask)
        real = self.trades(S, H[h], q=q, mask=M)
        if real is None:
            return None
        r0 = real[:, 4].mean()
        vals = S.reindex(index=self.idx, columns=self.cols).values
        c = 0
        nul = []
        for _ in range(n):
            sh = rng.integers(288, len(self.idx) - 288)
            Sx = pd.DataFrame(np.roll(vals, sh, axis=0), index=self.idx, columns=self.cols)
            tr = self.trades(Sx, H[h], q=q, mask=M)
            x = tr[:, 4].mean() if tr is not None else -9
            nul.append(x)
            c += x >= r0
        return {"net_c": 100 * r0, "p": (c + 1) / (n + 1), "null_mean_c": 100 * float(np.mean(nul)), "null_p95_c": 100 * float(np.quantile(nul, 0.95))}


def survivors(df, min_n=30, t=2.0):
    """Train and test agree, test net > 0, t on races and days both past t, last 3 days not negative."""
    if df is None or df.empty or "test_net_c" not in df:
        return df.iloc[0:0] if df is not None else None
    d = df.copy()
    k = (d.get("train_net_c", 0) > 0) & (d.test_net_c > 0) & (d.get("test_n", 0) >= min_n) & (d.test_t_race >= t) & (d.test_t_day >= t) & (d.get("last3_net_c", -1) > 0)
    return d[k].sort_values("test_net_c", ascending=False)


def show(df, n=12):
    cols = ["name", "h", "q", "office", "train_n", "train_net_c", "test_n", "test_net_c", "test_win", "test_t_race", "test_t_day", "last3_n", "last3_net_c"]
    cols = [c for c in cols if c in df]
    pd.set_option("display.width", 220, "display.max_columns", 30, "display.max_rows", 200)
    print(df[cols].round(2).head(n).to_string(index=False))
