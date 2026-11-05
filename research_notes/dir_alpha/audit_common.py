"""Audit of the S48 lag rule: shared loaders and an independent rebuild of the signal. Does NOT import lab.py or extract.py.
Reads data/sigbot.sqlite read-only; caches raw tables in AUDIT_CACHE (default: scratchpad given by env)."""
import os, sqlite3
import numpy as np, pandas as pd
CACHE = os.environ.get("AUDIT_CACHE", "/tmp/audit_cache")
BIN = 300
TH, HOLD, WIN, MINP = 0.02, 288, 576, 288


def dump_raw():
    os.makedirs(CACHE, exist_ok=True)
    db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True, timeout=60)
    q = {"vf": "select ts,race,party,pm,kalshi,fair,pm_w,k_w,sig_bid,sig_ask,sig_mid,exchange_id from venue_fair where exchange_id is not null",
         "tape": "select trade_id,exchange_id,price,quantity,executed_at,sequence,aggressor,ingested_at from tape",
         "fills": "select id,order_id,exchange_id,side,price,quantity,filled_at,purpose,ingested_at from fills",
         "orders": "select order_id,exchange_id,side,action,price,quantity,purpose,placed_at,status,expires_at from orders",
         "quotes": "select ts,exchange_id,best_bid,best_ask,bid_qty,ask_qty,mid,fair,inventory from quotes",
         "sigq": "select ts,instrument as exchange_id,bid,ask from venue_quotes where venue='sig'",
         "cycles": "select cycle_id,ts,notes from cycles"}
    for k, s in q.items():
        d = pd.read_sql_query(s, db)
        d.to_pickle(f"{CACHE}/{k}.pkl")
        print(k, len(d), flush=True)


def raw(k):
    return pd.read_pickle(f"{CACHE}/{k}.pkl")


class Grid:
    """5-minute grid rebuilt from raw venue_fair. Unlike extract.py it takes whole rows (no column-wise groupby.last) and keeps
    the poll timestamp of every cell, so staleness is known exactly."""

    def __init__(self, ffill=36):
        vf = raw("vf")
        vf["b"] = (vf.ts // BIN).astype("int64") * BIN
        g = vf.sort_values("ts").drop_duplicates(["b", "exchange_id"], keep="last")
        self.meta = vf.sort_values("ts").groupby("exchange_id")[["race", "party"]].last()
        self.idx = np.arange(g.b.min(), g.b.max() + BIN, BIN)
        self.cols = sorted(g.exchange_id.unique(), key=str)
        F = {}
        for c in ["pm", "kalshi", "fair", "sig_bid", "sig_ask", "ts", "pm_w", "k_w"]:
            F[c] = g.pivot(index="b", columns="exchange_id", values=c).reindex(index=self.idx, columns=self.cols)
        self.fresh = F["ts"].notna()
        self.P = {c: (v.ffill(limit=ffill) if ffill else v) for c, v in F.items()}
        b, a = self.P["sig_bid"], self.P["sig_ask"]
        ok = (a > b) & (b > 0) & (a < 1)
        self.bid, self.ask = b.where(ok), a.where(ok)
        self.mid = (self.bid + self.ask) / 2
        f = self.P["fair"]
        self.fair = f.where((f >= .005) & (f <= .995))
        self.gap = self.fair - self.mid
        self.S48 = self.gap - self.gap.rolling(WIN, min_periods=MINP).median()
        self.t = pd.to_datetime(self.idx, unit="s", utc=True)
        self.age = pd.DataFrame(self.idx[:, None] + BIN - self.P["ts"].values, index=self.idx, columns=self.cols)   # seconds from poll to bin end
        n = self.meta.reindex(self.cols).race.values
        self.race = n
        self.party = self.meta.reindex(self.cols).party.values
        self.chamber = np.isin(n, ["U.S. House", "U.S. Senate"])

    def flow(self, tape=None, cutoff_col="ts"):
        tp = raw("tape") if tape is None else tape
        tp["ts"] = (pd.to_datetime(tp.executed_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
        tp["b"] = (tp.ts // BIN).astype("int64") * BIN
        tp["q"] = tp.quantity.abs()
        tp["sf"] = tp.aggressor.map({"buy": 1.0, "sell": -1.0}).fillna(0.0) * tp.q
        self.tape = tp
        return tp.groupby(["b", "exchange_id"]).sf.sum().unstack().reindex(index=self.idx, columns=self.cols).fillna(0.0)

    def masks(self, sflow):
        S = self.S48; s = np.sign(S)
        dp, dk = self.P["pm"] - self.P["pm"].shift(HOLD), self.P["kalshi"] - self.P["kalshi"].shift(HOLD)
        both = (dp * s >= .01) & (dk * s >= .01)
        fl = sflow.rolling(HOLD, min_periods=1).sum()
        flowok = fl * s >= -5000
        band = (self.mid >= .15) & (self.mid <= .85)
        M = band.values & ~self.chamber[None, :]
        return dict(both=both.values, flowok=flowok.values, base=M, full=M & both.values & flowok.values)

    def trades(self, mask, S=None, th=TH, hold=HOLD, delay=0, need_exit=True):
        """One open trade per leg, in the time order of the entry signal. delay = bins between signal and fill. Returns a DataFrame,
        including rows for signals dropped because the exit quote is missing (dropped=True)."""
        S = self.S48 if S is None else S
        v = S.values; B, A = self.bid.values, self.ask.values
        n, m = v.shape; out = []
        for j in range(m):
            nxt = 0
            for i in np.flatnonzero((np.abs(v[:, j]) >= th) & mask[:, j]):
                if i < nxt: continue
                x = i + delay; y = x + hold
                if y >= n:
                    continue
                s = 1.0 if v[i, j] > 0 else -1.0
                q = (B[x, j], A[x, j], B[y, j], A[y, j])
                drop = not np.isfinite(q).all()
                if drop and need_exit:
                    continue                     # same as lab: not consumed, next bin may enter
                net = (q[2] - q[1]) if s > 0 else (q[0] - q[3])
                gross = s * ((q[2] + q[3]) / 2 - (q[0] + q[1]) / 2)
                out.append((j, i, x, y, s, gross, net, drop))
                nxt = i + hold
        d = pd.DataFrame(out, columns=["j", "i", "x", "y", "s", "gross", "net", "dropped"])
        d["ex"] = np.array(self.cols, dtype=object)[d.j]; d["race"] = self.race[d.j]; d["party"] = self.party[d.j]
        d["t"] = self.t[d.x]; d["day"] = self.t[d.x].strftime("%m-%d"); d["ts"] = self.idx[d.x]
        return d
