"""Shared pieces for the others_* comparison-group fetch: Polymarket history with cache, T-10 price, JSON of selected legs."""
import os, sys, json, time, calendar
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import net
DAY = 86400
BASE = os.path.join(net.OUT, "others")
RAW = os.path.join(BASE, "raw")

def utc(y, mo, d, h=0, mi=0):
    return calendar.timegm((y, mo, d, h, mi, 0))

def pm_event(eid):
    return net.cached(os.path.join(RAW, f"ev_{eid}.json"), lambda: (time.sleep(.35), net.curl(net.GAMMA + "/events", {"id": eid})[1])[1])

def pm_hist(token, T):
    def go():
        time.sleep(.35)
        code, d = net.curl(net.CLOB + "/prices-history", {"market": token, "startTs": T - 14 * DAY, "endTs": T + DAY, "fidelity": 60})
        if code == 400:
            code, d = net.curl(net.CLOB + "/prices-history", {"market": token, "startTs": T - 14 * DAY, "endTs": T + DAY - 3600, "fidelity": 60})
        return d if code == 200 else None
    d = net.cached(os.path.join(RAW, f"h_{token}.json"), go)
    return (d or {}).get("history", [])

def price_at(hist, t):
    """last price at or before t, else None"""
    best = None
    for h in hist:
        if h["t"] <= t: best = h["p"]
        else: break
    return best

def yes_token_and_win(m):
    outs = json.loads(m["outcomes"]); toks = json.loads(m["clobTokenIds"]); pr = [float(x) for x in json.loads(m["outcomePrices"])]
    i = outs.index("Yes")
    return toks[i], (1 if pr[i] > .99 else 0 if pr[i] < .01 else None)
