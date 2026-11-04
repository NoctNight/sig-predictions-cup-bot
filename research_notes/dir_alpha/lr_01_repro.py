from lr_common import *
L, D, tp = load()
for nm in "AB":
    d = D[nm]; Res, ok = residual(d)
    E = engine(d, Res, ok); q = E[E.res >= .03]
    print(nm, "repro oos8 (mask 2c, cut res>=3c after):", st(q), "rich", st(q[q.rich]), "cheap", st(q[~q.rich]))
    if nm == "A": pass
    sv = pd.read_pickle(O + f"line_{nm.lower()}.pkl"); print("   saved 3c+", len(sv[sv.res >= .03]), round(100 * sv[sv.res >= .03].n.mean(), 2))
    # stated rule: trigger at 3c, not first touch of 2c
    E3 = engine(d, Res, ok, trig=.03)
    print("  stated rule (mask 3c):", st(E3), "rich", st(E3[E3.rich]), "cheap", st(E3[~E3.rich]))
    if nm == "B": print("  stated rule last-trade net:", st(E3, "lt"), " oos8 set last-trade:", st(q, "lt"))
    # how many bars before: distance at t-1 for the oos8 3c+ set
    Rs = pd.DataFrame(Res); prev = np.array([abs(Res[t - 1, j]) if t > 0 else np.nan for t, j in zip(q.t, q.j)])
    print("  oos8 3c+ trades: |res| at the bar before: median %.2fc, share <2c %.2f, jump at signal bar median %.2fc" % (100 * np.nanmedian(prev), np.nanmean(prev < .02), 100 * np.nanmedian(q.res.values - prev)))
