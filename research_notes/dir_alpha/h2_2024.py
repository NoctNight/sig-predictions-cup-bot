"""Round-2 check: do outside moves keep going less in the last days before an election? Polymarket 2024 Senate, Governor and House markets, 30-minute prices,
one market per race (the first market of the event), 18 Oct to 5 Nov 2024. Keyless; no SIG calls. argv[1] = scratch directory for the raw pull."""
import json, os, ssl, sys, time, urllib.request
import numpy as np, pandas as pd
OUT = sys.argv[1]
CTX = ssl.create_default_context(cafile="/etc/ssl/cert.pem")
get = lambda u: json.loads(urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=30, context=CTX).read())
legs = json.load(open("data/election_night/legs_2024.json"))
slugs = sorted({(x["office"], x["slug_or_ticker"]) for x in legs if x["venue"] == "polymarket" and x["office"] != "president"})
raw = os.path.join(OUT, "pm2024.json")
if os.path.exists(raw):
    H = json.load(open(raw))
else:
    H = {}
    T0, T1 = 1729209600, 1730851200   # 18 Oct 2024 00:00 UTC to 6 Nov 2024 00:00 UTC
    for off, sl in slugs:
        try:
            ev = get("https://gamma-api.polymarket.com/events?slug=" + sl)
            tok = json.loads(ev[0]["markets"][0]["clobTokenIds"])[0]
            pts = []
            t = T0
            while t < T1:
                pts += get(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={t}&endTs={min(t + 5 * 86400, T1)}&fidelity=30").get("history", [])
                t += 5 * 86400; time.sleep(.25)
            H[sl] = dict(office=off, pts=pts)
        except Exception as e:
            print("fail", sl, e)
    json.dump(H, open(raw, "w"))
print("markets", len(H), "with data", sum(1 for v in H.values() if len(v["pts"]) > 100))
rows = []
B = 1800
for sl, v in H.items():
    if len(v["pts"]) < 100: continue
    s = pd.DataFrame(v["pts"]).drop_duplicates("t").sort_values("t")
    s["b"] = (s.t // B) * B
    p = s.groupby("b").p.last().reindex(np.arange(s.b.min(), s.b.max() + B, B)).ffill(limit=4)
    d = p.diff()
    last = -99
    vals, idx = p.values, p.index.values
    for i in range(1, len(p) - 16):
        if i - last < 8 or not (.25 <= vals[i - 1] <= .75) or not np.isfinite(d.values[i]) or abs(d.values[i]) < .015: continue
        last = i
        sg = np.sign(d.values[i])
        rows.append(dict(office=v["office"], sl=sl, t=idx[i], mv=abs(d.values[i]), f2=sg * (vals[i + 4] - vals[i]), f4=sg * (vals[i + 8] - vals[i]), f8=sg * (vals[i + 16] - vals[i])))
R = pd.DataFrame(rows)
E24 = 1730851200
R["per"] = pd.cut((E24 - R.t) / 86400, [-1, 1, 5, 9, 19], labels=["T-0 (5 Nov)", "T-1 to T-4", "T-5 to T-8", "T-9 to T-18"])
pd.set_option("display.width", 200)
tb = R.groupby("per", observed=True).agg(n=("mv", "size"), mkts=("sl", "nunique"), mv=("mv", lambda v: 100 * v.mean()), f2=("f2", lambda v: 100 * v.mean()), f4=("f4", lambda v: 100 * v.mean()), f8=("f8", lambda v: 100 * v.mean()), se4=("f4", lambda v: 100 * v.std() / np.sqrt(len(v))), up4=("f4", lambda v: (v > 0).mean()))
print(tb.round(2).to_string())
R["d"] = pd.to_datetime(R.t, unit="s").dt.strftime("%m-%d")
print(R.groupby("d").agg(n=("mv", "size"), f4=("f4", lambda v: round(100 * v.mean(), 2))).T.to_string())
