"""Assemble legs.csv / hourly.csv from the cached Polymarket and Kalshi responses, and print the verification report."""
import os, sys, json, glob, csv, statistics, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from others_select import *
import others_fed_kalshi as fk
def g(d, k):
    v = (d or {}).get(k, (d or {}).get(k + "_dollars"))
    return None if v in (None, "") else float(v)
def kalshi_candles():
    out = {}
    for f in glob.glob(os.path.join(RAW, "k_historical_markets_*_candlesticks_*.json")):
        d = json.load(open(f))["data"]; out[d["ticker"]] = d["candlesticks"]
    for f in glob.glob(os.path.join(RAW, "k_markets_candlesticks_*.json")):
        for m in json.load(open(f))["data"].get("markets", []): out[m["market_ticker"]] = m["candlesticks"]
    return out
def kalshi_rows(ticker, cs, T):
    rows = []
    for c in cs:
        t = c["end_period_ts"]
        if not (T - 14 * DAY <= t <= T + DAY): continue
        bid, ask = g(c.get("yes_bid"), "close"), g(c.get("yes_ask"), "close")
        pc, pp = g(c.get("price"), "close"), g(c.get("price"), "previous")
        ok = bid is not None and ask is not None and bid > 0 and ask < 1 and ask - bid < .5
        p = (bid + ask) / 2 if ok else pc if pc is not None else pp
        if p is None: continue
        rows.append(("kalshi", ticker, t, round(p, 4), "" if bid is None else bid, "" if ask is None else ask))
    return rows
def jump(h, T):
    """hours after T at which a leg first covers half of its move from T-12h to the end of the window"""
    w = [x for x in h if T - 12 * 3600 <= x["t"]]
    if len(w) < 10: return None
    b, f = w[0]["p"], w[-1]["p"]
    if abs(f - b) < .3: return None
    for x in w:
        if abs(x["p"] - b) >= .5 * abs(f - b): return (x["t"] - T) / 3600
if __name__ == "__main__":
    pm = load_pm(); sel = select_pm(pm)
    legs, hourly = [], []
    for o in sel:
        legs.append(dict(category=o["category"], cluster=o["cluster"], contract=o["contract"], venue="polymarket", market_id=o["token"], t_settle=o["T"], outcome=o["outcome"], question=o["question"], volume=round(o["volume"])))
        for x in pm_hist(o["token"], o["T"]): hourly.append(("polymarket", o["token"], x["t"], x["p"], "", ""))
    # kalshi registry: fed legs (same contract as chosen on Polymarket) + oscars
    km, kt, cs = kalshi_markets(), fk.kalshi_tickers(), kalshi_candles()
    reg = {}
    for o in sel:
        if o["category"] != "fed": continue
        y, m = int(o["cluster"][4:8]), int(o["cluster"][9:]); suf = fk.MAP.get(o["contract"].split("-", 3)[-1])
        if suf and suf in kt.get((y, m), {}): reg[kt[(y, m)][suf]] = dict(contract=o["contract"], cluster=o["cluster"], category="fed", T=o["T"])
    rp = os.path.join(RAW, "kalshi_registry_oscars.json")
    if os.path.exists(rp): reg.update(json.load(open(rp)))
    for p in glob.glob(os.path.join(RAW, "kalshi_registry_extra*.json")): reg.update(json.load(open(p)))
    nok = 0
    for t, r in reg.items():
        if t not in cs: continue
        rows = kalshi_rows(t, cs[t], r["T"])
        m = km.get(t, {})
        res = m.get("result")
        if not rows or res not in ("yes", "no"): continue
        nok += 1
        legs.append(dict(category=r["category"], cluster=r["cluster"], contract=r["contract"], venue="kalshi", market_id=t, t_settle=r["T"], outcome=1 if res == "yes" else 0,
                         question=f'{m.get("title","")} | {m.get("yes_sub_title","")}'.strip(" |"), volume=round(float(m.get("volume_fp") or 0))))
        hourly += rows
    L = pd_legs = None
    os.makedirs(BASE, exist_ok=True)
    with open(os.path.join(BASE, "legs.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["category", "cluster", "contract", "venue", "market_id", "t_settle", "outcome", "question", "volume"]); w.writeheader(); w.writerows(legs)
    seen = set(); H = []
    for r in hourly:
        if (r[1], r[2]) in seen: continue
        seen.add((r[1], r[2])); H.append(r)
    H.sort(key=lambda r: (r[0], r[1], r[2]))
    with open(os.path.join(BASE, "hourly.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["venue", "market_id", "ts", "price", "bid", "ask"]); w.writerows(H)
    print("legs", len(legs), "hourly", len(H), "kalshi legs", nok)
    # report
    import pandas as pd
    L = pd.DataFrame(legs); Hd = pd.DataFrame(H, columns=["venue", "market_id", "ts", "price", "bid", "ask"])
    print(L.groupby(["category", "venue"]).size().unstack(fill_value=0))
    print("clusters", L.groupby("category").cluster.nunique().to_dict())
    both = L.groupby("contract").venue.nunique(); print("contracts on both venues", int((both == 2).sum()), "of", len(both))
    last = Hd.sort_values("ts").groupby(["venue", "market_id"]).price.last().rename("last")
    L = L.merge(last, left_on=["venue", "market_id"], right_index=True, how="left")
    bad = L[((L.outcome == 1) & (L["last"] < .9)) | ((L.outcome == 0) & (L["last"] > .1))]
    print("outcome vs final price mismatches:"); print(bad[["contract", "venue", "outcome", "last"]].to_string())
    J = collections.defaultdict(list)
    for o in sel:
        j = jump(pm_hist(o["token"], o["T"]), o["T"])
        if j is not None: J[o["category"] if o["category"] != "awards" else o["cluster"]].append(round(j, 1))
    for k, v in J.items(): print("PM half-move offset vs t_settle (h)", k, "n", len(v), "median", statistics.median(v), "min", min(v), "max", max(v))
    n_by = Hd.groupby(["venue", "market_id"]).size(); print("rows per leg: min", n_by.min(), "median", n_by.median(), "max", n_by.max(), "| legs with <200 rows:", int((n_by < 200).sum()))
