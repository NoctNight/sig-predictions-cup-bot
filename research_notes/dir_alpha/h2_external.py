"""Round-2 checks on public data pulled by h2_fetch.py into argv[1]: FEC independent expenditures, DJT stock, PredictIt, Kalshi and Polymarket trades before the strict signals."""
import json, os, re, sys, warnings
import numpy as np, pandas as pd
warnings.filterwarnings("ignore")
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
OUT = sys.argv[1]; WHAT = sys.argv[2:] or ["fec", "djt", "pi", "quality"]
L = Lab()
mid, fair = L.mid, L.fair
pd.set_option("display.width", 200, "display.max_rows", 100, "display.max_columns", 30)
c = lambda v: round(100 * float(np.nanmean(v)), 2)
key = {(r, p): j for j, (r, p) in enumerate(zip(L.race, L.party))}
ix = L.idx
bin_at = lambda ts: int(np.searchsorted(ix, ts))
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}

# national factor, D-signed
legs = np.flatnonzero((L.dsign.values != 0) & (L.office != "chamber"))
ff = fair.iloc[:, legs].where((mid.iloc[:, legs] >= .1) & (mid.iloc[:, legs] <= .9)) * L.dsign.values[legs]
natl = ff.sub(ff.mean(axis=0), axis=1).mean(axis=1)
mm = mid.iloc[:, legs].where((mid.iloc[:, legs] >= .1) & (mid.iloc[:, legs] <= .9)) * L.dsign.values[legs]
natm = mm.sub(mm.mean(axis=0), axis=1).mean(axis=1)


def trade_net(j, t, h, s):
    b0, a0, b1, a1 = L.B[t, j], L.A[t, j], L.B[t + h, j], L.A[t + h, j]
    return (b1 - a0) if s > 0 else (b0 - a1)


if "fec" in WHAT:
    print("=== FEC independent expenditures by race. Public date = receipt date, entry 12:00 UTC the next day (file refreshes about 10:15 UTC)")
    F = pd.read_csv(os.path.join(OUT, "fec_ie.csv"), low_memory=False, dtype=str)
    F = F[F.ele_type.fillna("").str.startswith("G") & F.can_office.isin(["H", "S"])].copy()
    F["amt"] = pd.to_numeric(F.exp_amo, errors="coerce").fillna(0); F = F[F.amt <= 5e6]
    F = F.drop_duplicates(["spe_id", "tran_id"], keep="last")
    F["rc"] = pd.to_datetime(F.receipt_dat, format="%d-%b-%y", errors="coerce")
    pty = F.cand_pty_aff.fillna("").str.upper()
    dem, rep = pty.str.contains("DEMOCRAT"), pty.str.contains("REPUBLICAN")
    sup, opp = F.sup_opp == "S", F.sup_opp == "O"
    F["side"] = np.where((dem & sup) | (rep & opp), "D", np.where((rep & sup) | (dem & opp), "R", ""))
    F = F[F.side != ""]
    sn = F.can_office_state.map(STATES)
    dis = pd.to_numeric(F.can_office_dis, errors="coerce").fillna(0).astype(int)
    F["race"] = np.where(F.can_office == "H", F.can_office_state + "-" + dis.map("{:02d}".format) + " House race", sn + " Senate")
    print("rows", len(F), "races", F.race.nunique(), "in SIG", F.race[F.race.isin(set(L.race))].nunique(), "$M since 15 Oct", round(F[F.rc >= "2026-10-15"].amt.sum() / 1e6, 1), "last receipt", F.rc.max())
    F = F[F.race.isin(set(L.race))]
    dly = F.pivot_table(index=["race", "rc"], columns="side", values="amt", aggfunc="sum").fillna(0).reset_index()
    rows = []
    days = pd.date_range("2026-10-22", "2026-10-29")
    for race, g in dly.groupby("race"):
        g = g.set_index("rc")[["D", "R"]].reindex(pd.date_range("2026-08-01", "2026-10-30")).fillna(0)
        if (race, "D") not in key: continue
        j = key[(race, "D")]
        for d in days:
            r3 = g.loc[d - pd.Timedelta(days=2):d].sum(); r14 = g.loc[d - pd.Timedelta(days=16):d - pd.Timedelta(days=3)].sum()
            if r3.sum() < 50000 or r14.sum() < 100000: continue
            ts = (d + pd.Timedelta(days=1, hours=12)).tz_localize("UTC").timestamp()
            t = bin_at(ts)
            if t + 96 >= len(ix): continue
            sh3, sh14 = r3.D / r3.sum(), r14.D / r14.sum()
            burst = (r3.sum() / 3) / max(r14.sum() / 14, 1)
            row = dict(race=race, d=d, office=L.office[j], sh3=sh3, sh14=sh14, ds=sh3 - sh14, burst=burst, usd3=r3.sum())
            m, f = mid.values, fair.values
            for lab, h in (("24", 288), ("48", 576)):
                if t + h < len(ix):
                    row["dm" + lab], row["df" + lab] = m[t + h, j] - m[t, j], f[t + h, j] - f[t, j]
                    row["fm" + lab] = (fair.iloc[t + h, j] - mid.iloc[t + h, j]) - (fair.iloc[t, j] - mid.iloc[t, j])
            row["mid"] = m[t, j]
            row["n24"] = trade_net(j, t, 288, np.sign(sh3 - sh14) or 1.0) if t + 288 < len(ix) else np.nan
            rows.append(row)
    R = pd.DataFrame(rows)
    R = R[(R.mid > .1) & (R.mid < .9)]
    print("race-days", len(R), "races", R.race.nunique())
    for th in (0, .1, .2, .3):
        g = R[R.ds.abs() >= th]
        sg = np.sign(g.ds)
        print(f"|share shift| >= {th:.1f}: n {len(g)} races {g.race.nunique()}  fair 24h {c(sg*g.df24)}  mid 24h {c(sg*g.dm24)}  fair 48h {c(sg*g.df48)}  mid 48h {c(sg*g.dm48)}  net 24h {c(g.n24)}  by-race SE mid24 {round(100*(sg*g.dm24).groupby(g.race).mean().std()/np.sqrt(g.race.nunique()),2)}")
    # money share level against price: does the side outspending more than the price implies gain?
    R["lvl"] = R.sh3 - R.mid
    print("corr(share shift, mid 24h)", round(R.ds.corr(R.dm24), 3), "corr(share shift, fair 24h)", round(R.ds.corr(R.df24), 3), "corr(level sh3-mid, mid 24h)", round(R.lvl.corr(R.dm24), 3))
    print("demeaned by day (national move out): corr", round((R.ds).corr(R.dm24 - R.groupby("d").dm24.transform("mean")), 3))
    fh = R[R.office == "house"]; print("house only: n", len(fh), "shift>=.2 mid24", c(np.sign(fh.ds[fh.ds.abs() >= .2]) * fh.dm24[fh.ds.abs() >= .2]))
    # one-sided surge: 3-day spend rate at least 3x the prior two weeks
    g = R[R.burst >= 3]; sg = np.sign(g.ds)
    print("burst >=3x: n", len(g), "follow the side that gained share, mid 24h", c(sg * g.dm24), "mid 48h", c(sg * g.dm48), "net 24h", c(g.n24))

if "djt" in WHAT:
    print("\n=== DJT 5-minute bars against the national factor (D-signed), regular hours, 22-30 Oct")
    d = json.load(open(os.path.join(OUT, "djt.json")))["chart"]["result"][0]
    s = pd.Series(d["indicators"]["quote"][0]["close"], index=d["timestamp"]).dropna()
    s.index = (s.index.values // 300) * 300
    s = s[~s.index.duplicated(keep="last")].astype(float)
    s = s[(s.index >= ix[0]) & (s.index <= ix[-1])]
    print("bars", len(s), "days", pd.to_datetime(s.index, unit="s").strftime("%m-%d").nunique())
    r30 = np.log(s).diff(6).dropna()
    na = natl.reindex(r30.index); nm = natm.reindex(r30.index)
    rows = []
    for lab, h in (("30m", 6), ("1h", 12), ("4h", 48)):
        fwd = (natl.shift(-h) - natl).reindex(r30.index); fwdm = (natm.shift(-h) - natm).reindex(r30.index)
        past = (natl - natl.shift(h)).reindex(r30.index)
        ok = fwd.notna() & past.notna()
        days = pd.to_datetime(r30.index, unit="s").strftime("%m-%d")
        byday = pd.DataFrame(dict(x=r30[ok], y=fwd[ok]), index=r30.index[ok]).groupby(days[ok]).apply(lambda g: g.x.corr(g.y))
        rows.append(dict(h=lab, corr_next_fair=r30[ok].corr(fwd[ok]), corr_next_mid=r30[ok].corr(fwdm[ok]), corr_past_fair=r30[ok].corr(past[ok]), byday_mean=byday.mean(), byday_se=byday.std() / np.sqrt(len(byday)), n=int(ok.sum())))
    print(pd.DataFrame(rows).round(3).to_string(index=False))
    big = r30[r30.abs() >= r30.abs().quantile(.9)]
    fwd = (natl.shift(-48) - natl).reindex(big.index); fwdm = (natm.shift(-48) - natm).reindex(big.index)
    print("top decile DJT 30m moves: n", len(big), "next 4 h national fair, signed so that DJT up = R up, c", c(-np.sign(big) * fwd), "SIG basket", c(-np.sign(big) * fwdm))

if "pi" in WHAT:
    print("\n=== PredictIt daily closes against the blended outside fair")
    a = json.load(open(os.path.join(OUT, "pi_all.json")))["markets"]
    ch = json.load(open(os.path.join(OUT, "pi_charts.json")))
    STN = set(STATES.values())
    ord_re = re.compile(r"(\d+)(?:st|nd|rd|th)")
    mp = {}
    for x in a:
        nm = x["name"]
        race = None
        mm_ = re.search(r"US Senate (?:special )?election in ([A-Za-z ]+)\?", nm)
        if mm_: race = mm_.group(1).strip() + " Senate"
        mm_ = re.search(r"US House (?:election|race) in ([A-Za-z ]+)'s (\d+)", nm)
        if mm_:
            ab = {v: k for k, v in STATES.items()}.get(mm_.group(1).strip())
            if ab: race = f"{ab}-{int(mm_.group(2)):02d} House race"
        mm_ = re.search(r"(?:governor(?:ship)?|gubernatorial).*? (?:of|in) ([A-Z][a-z]+(?: [A-Z][a-z]+)?)", nm, re.I)
        if race is None and mm_ and mm_.group(1) in STN: race = mm_.group(1) + " Governor"
        if race and (race, "D") in key: mp[str(x["id"])] = race
    print("PI markets total", len(a), "mapped to a SIG race", len(mp), "with charts", sum(1 for k in mp if k in ch), "offices", pd.Series(list(mp.values())).str.extract(r"(Senate|Governor|House)")[0].value_counts().to_dict())
    rows = []
    for mid_, race in mp.items():
        for p in ch.get(mid_, []):
            nm = p["contractName"].lower()
            pa = "D" if re.search(r"dem", nm) else ("R" if re.search(r"gop|rep", nm) else None)
            if pa == "D":
                rows.append(dict(race=race, date=p["date"][:10], px=p["closeSharePrice"], vol=p["tradeVolume"]))
    D = pd.DataFrame(rows).drop_duplicates(["race", "date"])
    print("rows", len(D), "dates", sorted(D.date.unique()))
    out = []
    for race, g in D.sort_values("date").groupby("race"):
        j = key[(race, "D")]
        g = g.reset_index(drop=True)
        for i in range(1, len(g)):
            d = pd.Timestamp(g.date[i], tz="UTC") + pd.Timedelta(hours=28)       # 04:00 UTC next day, after the close on any clock
            d0 = d - pd.Timedelta(days=1)
            t, t0 = bin_at(d.timestamp()), bin_at(d0.timestamp())
            if t + 288 >= len(ix): continue
            dpi = g.px[i] - g.px[i - 1]
            dfa = fair.values[t, j] - fair.values[t0, j]
            out.append(dict(race=race, date=g.date[i], dpi=dpi, dfair=dfa, resid=dpi - dfa, lvl=g.px[i] - fair.values[t, j], vol=g.vol[i], mid=mid.values[t, j], nf=fair.values[t + 288, j] - fair.values[t, j], nm=mid.values[t + 288, j] - mid.values[t, j], net=trade_net(j, t, 288, np.sign(dpi - dfa) or 1.0)))
    R = pd.DataFrame(out).dropna(subset=["resid", "nf", "nm"])
    R = R[(R.mid > .1) & (R.mid < .9)]
    print("race-days", len(R), "races", R.race.nunique(), "median PI volume", R.vol.median())
    print("corr(PI residual change, next-day fair)", round(R.resid.corr(R.nf), 3), "next-day SIG mid", round(R.resid.corr(R.nm), 3), "| corr(PI minus fair level, next fair)", round(R.lvl.corr(R.nf), 3), "next mid", round(R.lvl.corr(R.nm), 3))
    for th in (.01, .02, .03):
        g = R[R.resid.abs() >= th]; sg = np.sign(g.resid)
        print(f"|residual| >= {th}: n {len(g)} races {g.race.nunique()} next fair {c(sg*g.nf)} next mid {c(sg*g.nm)} net {c(g.net)}")
    g = R[(R.resid.abs() >= .02) & (R.vol >= R.vol.median())]; sg = np.sign(g.resid)
    print("same, volume above median, >=.02: n", len(g), "next fair", c(sg * g.nf), "next mid", c(sg * g.nm), "net", c(g.net))

if "quality" in WHAT:
    print("\n=== Outside trades in the 30 min before the strict signal: did real flow back the move?")
    Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl"); Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl")
    Ef["rule"] = "fast"; Es["rule"] = "slow"
    E = pd.concat([Ef, Es], ignore_index=True)
    E["j"] = E.j.astype(int); E["t"] = E.t.astype(int)
    E = E.set_index(["j", "t", "rule"])
    for f in ("kalshi_trades.json", "pm_trades.json"):
        fp = os.path.join(OUT, f)
        if not os.path.exists(fp):
            print("missing", f); continue
        res = json.load(open(fp))
        rows = {}
        for r in res:
            k = (r["j"], r["t"], r["rule"])
            if k not in E.index: continue
            ts0 = ix[r["t"]]
            s = E.loc[k, "s"]; s = s.iloc[0] if hasattr(s, "iloc") else s
            a = rows.setdefault(k, dict(usd=0.0, sflow=0.0, n=0, big=0.0, w=set(), tmin=None))
            for t in r["trades"]:
                if "kalshi" in f:
                    tt = pd.Timestamp(t["created_time"]).timestamp()
                    q = float(t.get("count_fp") or t.get("count") or 0); px = float(t.get("yes_price_dollars") or (t.get("yes_price") or 0) / 100)
                    sg = 1 if t.get("taker_side") == "yes" else -1
                    usd = q * px; wallet = None
                else:
                    tt = t["timestamp"]; q = float(t["size"]); px = float(t["price"]); usd = q * px
                    sg = (1 if t["side"] == "BUY" else -1) * (-1 if t.get("outcomeIndex") == 1 else 1); wallet = t.get("proxyWallet")
                if not (ts0 - 1800 <= tt <= ts0): continue
                a["usd"] += usd; a["sflow"] += sg * s * usd; a["n"] += 1; a["big"] = max(a["big"], usd)
                if wallet: a["w"].add(wallet)
        T = pd.DataFrame.from_dict(rows, orient="index")
        T["nw"] = T.w.map(len)
        T["net"] = [float(np.mean(E.loc[k, "net"])) for k in T.index]
        T["rule"] = [k[2] for k in T.index]
        T["share_with"] = T.sflow / T.usd.replace(0, np.nan)
        print(f, "events", len(T), "zero-trade", int((T.n == 0).sum()), "median $ in 30 min", round(T.usd.median()), "median trades", T.n.median(), "median share with move", round(T.share_with.median(), 2))
        for rule, g in T.groupby("rule"):
            hi = g.usd >= g.usd.median()
            print(" ", rule, "n", len(g), "| $ low", c(g.net[~hi]), "$ high", c(g.net[hi]), "| flow with<0.3", c(g.net[g.share_with.fillna(0) < .3]), f"({(g.share_with.fillna(0) < .3).sum()})", "flow with>=0.3", c(g.net[g.share_with.fillna(0) >= .3]), f"({(g.share_with.fillna(0) >= .3).sum()})", "| zero trades", c(g.net[g.n == 0]) if (g.n == 0).any() else "-", f"({(g.n == 0).sum()})")
            if "pm" in f:
                nz = g[g.n > 0]
                print("    wallets: one", c(nz.net[nz.nw <= 1]), f"({(nz.nw <= 1).sum()})", "2-3", c(nz.net[(nz.nw > 1) & (nz.nw <= 3)]), f"({((nz.nw > 1) & (nz.nw <= 3)).sum()})", "4+", c(nz.net[nz.nw > 3]), f"({(nz.nw > 3).sum()})")
