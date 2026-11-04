from lr_common import *
L, D, tp = load(); Z = pd.read_pickle("research_notes/dir_alpha/lr_sets.pkl"); b0, a0 = D["B"]["book"]; mB = (b0 + a0) / 2; mA = D["A"]["S"].values
def cap(E, Mx, start, cash=None, q=5000, hold=864):
    E = E.sort_values("e"); opn = []; net = start; tk = []; csh = 0
    for r in E.itertuples():
        for o in list(opn):
            if o[0] <= r.e: net -= o[1]; csh -= o[2]; opn.remove(o)
        p = Mx[r.e, r.j]; ds = (1 if r.party == "D" else -1 if r.party == "R" else 0) * r.s; c = (p if r.s > 0 else 1 - p) * q; n = c * ds
        ok = abs(net + n) <= 5000 or abs(net + n) < abs(net)
        if cash and csh + c > cash: ok = False
        tk.append(ok)
        if ok: net += n; csh += c; opn.append((r.e + hold, n, c))
    tk = np.array(tk); return round(tk.mean(), 2), round(100 * E.n.values[tk].mean(), 2), round(100 * E.n.values[~tk].mean(), 2) if (~tk).any() else None, int(tk.sum())
for lab, E, Mx in (("A stated", Z["A2"], mA), ("B clean", Z["C"], mB)):
    for st_ in (0, 4700):
        for cash in (None, 30000, 65000): print(lab, "start", st_, "cash", cash, "(taken share, net taken, net blocked, n)", cap(E, Mx, st_, cash))
Zn = pd.read_pickle(O + "line_now.pkl"); Zn = Zn[~Zn.race.isin(["U.S. House", "U.S. Senate"])]
for order, q in (("distance", Zn), ("R-side first", Zn.sort_values("side", ascending=False, kind="stable"))):
    net = 4700; tk = []
    for r in q.itertuples():
        ds = 1 if r.side == "D" else -1; c = r.price * 5000
        if abs(net + ds * c) <= 5000 or abs(net + ds * c) < abs(net): net += ds * c; tk.append(r.side)
    print("live list", order, "taken", len(tk), "D", tk.count("D"), "R", tk.count("R"), "end net", round(net))
