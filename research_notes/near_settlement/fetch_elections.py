"""Hourly prices T-14..T+1 for the resolved election legs of data/research_ev_hunt/calib, both venues.

T = 23:00 UTC on election day (18:00 ET, first polls close, same clock as the 3 Nov punt). One call per leg on Polymarket
(15-day window); mirror legs are skipped. Kalshi runs through the shared limit, skewed races first, legs above 97c at T-10 last.
Run: python -I research_notes/near_settlement/fetch_elections.py pm|kalshi
"""
import calendar, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import net
DAY = 86400
L = pd.read_csv(os.path.join(net.ROOT, "data/research_ev_hunt/calib/legs_all.csv"), dtype={"market_id": str})
P = pd.read_csv(os.path.join(net.ROOT, "data/research_ev_hunt/calib/paths.csv"), dtype={"market_id": str})
L = L[L.us == 1]
L["T"] = [calendar.timegm(time.strptime(d, "%Y-%m-%d")) + 23 * 3600 for d in L.election_date]


def pm():
    legs = L[(L.venue == "polymarket") & (L.source == "clob") & ~L.market_id.str.contains("#")].copy()
    ids = sorted(set(legs.market_id))
    tok = {}
    for i in range(0, len(ids), 50):
        path = os.path.join(net.OUT, "raw", "pm", f"gamma_{i}.json")
        d = net.cached(path, lambda: net.curl(net.GAMMA + "/markets", {"id": ids[i:i + 50], "limit": 50, "closed": "true"})[1])
        for m in d or []:
            tok[str(m["id"])] = json.loads(m.get("clobTokenIds") or "[]")
        time.sleep(.3)
    legs["token"] = [(tok.get(i) or [None])[0] for i in legs.market_id]
    print("pm legs", len(legs), "with token", legs.token.notna().sum(), flush=True)
    legs.to_csv(os.path.join(net.OUT, "pm_legs.csv"), index=False)
    n = 0
    for _, r in legs.dropna(subset=["token"]).iterrows():
        p = os.path.join(net.OUT, "raw", "pm", f"h_{r.token}.json")
        if os.path.exists(p):
            continue
        code, d = net.curl(net.CLOB + "/prices-history", {"market": r.token, "startTs": r["T"] - 14 * DAY, "endTs": r["T"] + DAY, "fidelity": 60})
        if d is not None:
            json.dump(d, open(p, "w"))
        n += 1
        if n % 50 == 0:
            print("pm", n, flush=True)
        time.sleep(.35)
    print("pm done", n, flush=True)


def kalshi():
    legs = L[L.venue == "kalshi"].copy()
    pr = P[(P.venue == "kalshi") & P.days_before.isin([10, 7, 5, 3, 1])].pivot_table(index="market_id", columns="days_before", values="price")
    s = pr.reindex(legs.market_id)
    legs["skewed"] = ((s >= .78) & (s <= .96) | (s >= .04) & (s <= .22)).any(axis=1).values
    legs["mid"] = ((s >= .35) & (s <= .65)).any(axis=1).values
    legs["dead"] = ((s > .97) | (s < .03)).all(axis=1).values
    # a two-party race needs one leg only: the other is its mirror
    legs = legs.sort_values(["market_id"]).drop_duplicates("market_id")
    keep = legs[~legs.market_id.str.endswith("-R") | ~legs.market_id.str[:-2].add("-D").isin(legs.market_id)]
    keep = keep.assign(rank=(~keep.skewed).astype(int) * 2 + (~keep.mid).astype(int) + keep.dead.astype(int) * 4).sort_values(["rank", "volume_usd"], ascending=[True, False])
    print("kalshi legs", len(keep), keep["rank"].value_counts().to_dict(), flush=True)
    keep.to_csv(os.path.join(net.OUT, "kalshi_legs.csv"), index=False)
    for i, (_, r) in enumerate(keep.iterrows()):
        p = os.path.join(net.OUT, "raw", "kalshi", r.market_id + ".json")
        if os.path.exists(p):
            continue
        q = {"start_ts": r["T"] - 14 * DAY, "end_ts": r["T"] + DAY, "period_interval": 60}
        code, d = net.kalshi(f"/historical/markets/{r.market_id}/candlesticks", q)
        if code == 404 or d is None:
            code, d = net.kalshi(f"/series/{r.market_id.split('-')[0]}/markets/{r.market_id}/candlesticks", q)
        cs = (d or {}).get("candlesticks") or []
        json.dump([[c["end_period_ts"], (c.get("yes_bid") or {}).get("close"), (c.get("yes_ask") or {}).get("close"), (c.get("price") or {}).get("close"), c.get("volume")] for c in cs], open(p, "w"))
        print(i, r.market_id, code, len(cs), flush=True)


{"pm": pm, "kalshi": kalshi}[sys.argv[1]]()
