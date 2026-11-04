"""Round-2 public data pulls, keyless, saved to the directory given as argv[1] (never into the repo). Nothing here touches SIG.
    fec   : FEC independent-expenditure bulk file
    pi    : PredictIt current prices and 7-day daily charts for 2026 race markets (0.7 s between calls)
    djt   : Yahoo 5-minute bars for DJT
    kalshi: Kalshi public trades in the 60 min before each strict signal (3 calls per 30 s)
    pm    : Polymarket public trades in the 60 min before each strict signal, by condition id
"""
import json, os, re, sqlite3, ssl, sys, time, urllib.parse, urllib.request
import numpy as np, pandas as pd
OUT = sys.argv[1]; WHAT = sys.argv[2:]
UA = {"User-Agent": "Mozilla/5.0"}
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")


def get(u, tries=3):
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30, context=CTX).read()
        except Exception as e:
            err = e; time.sleep(5 * (i + 1))
    raise err


if "fec" in WHAT:
    open(os.path.join(OUT, "fec_ie.csv"), "wb").write(get("https://www.fec.gov/files/bulk-downloads/2026/independent_expenditure_2026.csv"))
if "djt" in WHAT:
    open(os.path.join(OUT, "djt.json"), "wb").write(get("https://query1.finance.yahoo.com/v8/finance/chart/DJT?range=1mo&interval=5m"))
if "pi" in WHAT:
    d = json.loads(get("https://www.predictit.org/api/marketdata/all/"))
    json.dump(d, open(os.path.join(OUT, "pi_all.json"), "w"))
    ms = [x for x in d["markets"] if "2026" in x["name"] and len(x["contracts"]) <= 3]
    ch = {}
    for x in ms:
        try:
            ch[x["id"]] = json.loads(get(f"https://www.predictit.org/api/Public/GetMarketChartData/{x['id']}?timespan=7d"))
        except Exception as e:
            print("fail", x["id"], e)
        time.sleep(.7)
    json.dump(ch, open(os.path.join(OUT, "pi_charts.json"), "w"))
    print("pi charts", len(ch))
if "kalshi" in WHAT or "pm" in WHAT:
    Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl"); Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl")
    Ef["rule"] = "fast"; Es["rule"] = "slow"
    E = pd.concat([Ef, Es], ignore_index=True)
    panel_idx = pd.read_pickle("data/research_diralpha/panel.pkl")["sig_bid"]
    cols = list(panel_idx.columns); idx = panel_idx.index.values
    meta = pd.read_pickle("data/research_diralpha/meta.pkl").reindex(cols)
    db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
    t0 = time.time() - 40 * 3600
    ins = pd.read_sql_query("select distinct venue,race,party,instrument from venue_quotes where ts>? and venue in ('kalshi','polymarket')", db, params=(t0,))
    rows = []
    for r in E.itertuples():
        j = int(r.j); rows.append((j, int(r.t), r.rule, meta.race.values[j], meta.party.values[j], float(idx[int(r.t)]), float(r.s), float(r.net)))
    Ev = pd.DataFrame(rows, columns=["j", "t", "rule", "race", "party", "ts", "s", "net"])
    Ev.to_pickle(os.path.join(OUT, "strict_events.pkl"))
    if "kalshi" in WHAT:
        res = []
        for r in Ev.itertuples():
            tk = ins[(ins.venue == "kalshi") & (ins.race == r.race) & (ins.party == r.party)].instrument.tolist()
            for k in tk[:2]:
                u = f"https://api.elections.kalshi.com/trade-api/v2/markets/trades?ticker={k}&min_ts={int(r.ts-3600)}&max_ts={int(r.ts+900)}&limit=1000"
                try:
                    tr = json.loads(get(u)).get("trades", [])
                except Exception as e:
                    print("k fail", k, e); tr = []
                res.append(dict(j=r.j, t=r.t, rule=r.rule, ticker=k, trades=tr))
                time.sleep(4)
        json.dump(res, open(os.path.join(OUT, "kalshi_trades.json"), "w"))
        print("kalshi calls", len(res))
    if "pm" in WHAT:
        cond = {}
        res = []
        for r in Ev.itertuples():
            tk = ins[(ins.venue == "polymarket") & (ins.race == r.race) & (ins.party == r.party)].instrument.tolist()
            for k in tk[:2]:
                if k not in cond:
                    try:
                        m = json.loads(get("https://gamma-api.polymarket.com/markets?clob_token_ids=" + k))
                        cond[k] = m[0]["conditionId"] if m else None
                    except Exception as e:
                        print("g fail", e); cond[k] = None
                    time.sleep(.3)
                c = cond[k]
                if not c: continue
                out, off = [], 0
                while off < 3000:
                    try:
                        b = json.loads(get(f"https://data-api.polymarket.com/trades?market={c}&limit=500&offset={off}&takerOnly=true"))
                    except Exception as e:
                        print("p fail", e); break
                    out += b
                    if len(b) < 500 or (b and b[-1]["timestamp"] < r.ts - 3600): break
                    off += 500; time.sleep(.3)
                res.append(dict(j=r.j, t=r.t, rule=r.rule, token=k, trades=[x for x in out if r.ts - 3600 <= x["timestamp"] <= r.ts + 900]))
                time.sleep(.3)
        json.dump(res, open(os.path.join(OUT, "pm_trades.json"), "w"))
        print("pm calls", len(res))
