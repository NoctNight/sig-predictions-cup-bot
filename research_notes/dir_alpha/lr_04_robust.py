from lr_common import *
L, D, tp = load()
dA = D["A"]; dB = D["B"]; qa = book_ctx(L, dB)
Ra, oka = residual(dA); nxa = next_real(dA["R"].values); raa = dA["R"].rolling(12, min_periods=1).max().fillna(0).values > 0
Rb, okb = residual(dB); nxb = next_real(dB["R"].values); rab = dB["R"].rolling(12, min_periods=1).max().fillna(0).values > 0
rows = []
def add(lab, Ea, Eb, Ec):
    rows.append(dict(cfg=lab, A_n=len(Ea), A_net=round(100 * Ea.n.mean(), 2), A_t2=round(twoway_t(Ea)[0], 1) if len(Ea) > 5 else None, Blast_n=len(Eb), Blast_net=round(100 * Eb.n.mean(), 2), Bmid_n=len(Ec), Bmid_net=round(100 * Ec.n.mean(), 2), Bmid_t2=round(twoway_t(Ec)[0], 1) if len(Ec) > 5 else None))
for thr in (.025, .03, .035, .04):
    add(f"thr {thr*100:.1f}c", engine(dA, Ra, oka, trig=thr, nx=nxa, ra=raa), engine(dB, Rb, okb, trig=thr, nx=nxb, ra=rab), clean(L, dB, qa, trig=thr)[0])
for h in (576, 864, 1044):
    add(f"hold {h//12}h, thr 3", engine(dA, Ra, oka, trig=.03, hold=h, nx=nxa, ra=raa), engine(dB, Rb, okb, trig=.03, hold=h, nx=nxb, ra=rab), clean(L, dB, qa, trig=.03, hold=h)[0])
add("band 15-85", engine(dA, Ra, oka, trig=.03, lo=.15, hi=.85, nx=nxa, ra=raa), engine(dB, Rb, okb, trig=.03, lo=.15, hi=.85, nx=nxb, ra=rab), clean(L, dB, qa, trig=.03, lo=.15, hi=.85)[0])
add("band 25-75", engine(dA, Ra, oka, trig=.03, lo=.25, hi=.75, nx=nxa, ra=raa), engine(dB, Rb, okb, trig=.03, lo=.25, hi=.75, nx=nxb, ra=rab), clean(L, dB, qa, trig=.03, lo=.25, hi=.75)[0])
for mode in ("median", "wfit"):
    r1, o1 = residual(dA, mode=mode); r2, o2 = residual(dB, mode=mode)
    add(f"k {mode}", engine(dA, r1, o1, trig=.03, nx=nxa, ra=raa), engine(dB, r2, o2, trig=.03, nx=nxb, ra=rab), clean(L, dB, qa, trig=.03, mode=mode)[0])
r1, o1 = residual(dA, by_office=True); r2, o2 = residual(dB, by_office=True)
add("k by office", engine(dA, r1, o1, trig=.03, nx=nxa, ra=raa), engine(dB, r2, o2, trig=.03, nx=nxb, ra=rab), clean(L, dB, qa, trig=.03, by_office=True)[0])
print(pd.DataFrame(rows).to_string(index=False))
A1 = engine(dA, Ra, oka, nx=nxa, ra=raa).query("res>=.03"); A2 = engine(dA, Ra, oka, trig=.03, nx=nxa, ra=raa); B1 = engine(dB, Rb, okb, nx=nxb, ra=rab).query("res>=.03"); B2 = engine(dB, Rb, okb, trig=.03, nx=nxb, ra=rab); C, R2, ok2 = clean(L, dB, qa)
C1 = clean(L, dB, qa, trig=.02)[0].query("res>=.03")
for lab, E in (("A oos8 set", A1), ("A stated rule", A2), ("B oos8 set book", B1), ("B stated rule book", B2), ("B clean mid", C), ("B clean first-touch", C1)):
    print(lab, "legs:", summary(E)); RB = race_bets(E); print("    races: ", summary(RB)); 
    ds = dside(E); print("    D-side", st(E[ds > 0]), "R-side", st(E[ds < 0]))
    if E is C: E = E
for lab, E in (("A stated", A2), ("B clean", C)):
    E = E.copy(); E["wk"] = (E.t // (288 * 4)); print(lab, "by 4 days", E.groupby("wk").n.agg(n="size", net=lambda v: round(100 * v.mean(), 2)).T.to_string())
    print("   by distance", E.groupby(pd.cut(E.res, [0, .035, .045, .07, 1]), observed=True).n.agg(n="size", net=lambda v: round(100 * v.mean(), 2)).T.to_string())
    print("   top races:", E.groupby("race").n.sum().sort_values(ascending=False).head(5).round(3).to_dict())
    print("   top days:", E.groupby("day").n.sum().sort_values(ascending=False).head(4).round(3).to_dict(), "trades by day", E.groupby("day").size().to_dict())
pd.to_pickle(dict(A1=A1, A2=A2, B1=B1, B2=B2, C=C, C1=C1), "research_notes/dir_alpha/lr_sets.pkl")
