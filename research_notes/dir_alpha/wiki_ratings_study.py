"""Do forecaster rating changes on Wikipedia move SIG prices after the edit time? Public MediaWiki API via curl, no SIG API, no orders.
usage: python wiki_ratings_study.py CACHE_DIR      (CACHE_DIR holds raw API json; never inside the repo)
Part 1: pull every revision since 2026-10-20 of the three ratings pages, parse each revision's rating tables, diff consecutive revisions
        -> data/research_diralpha/wiki_ratings.csv   (race, rater, old, new, scores, edit time, user, comment, reverted flag)
        (full wikitext of each revision is diffed instead of action=compare HTML: a compare diff has no race name on changed rows.)
Part 2: event study on the 5-minute panel (lab.py).
Definitions fixed before looking at prices:
  score: Solid/Safe R -3, Likely R -2, Lean R -1, Tilt R -.5, Tossup 0, Tilt D .5, Lean D 1, Likely D 2, Solid/Safe D 3
  usable event: one revision x one race, summed score change over raters != 0, not reverted (same cell back to its old value within 6 h)
  entry: first 5-min bin at/after the edit time; direction = sign of summed change; race move = mean over the race's legs of
         (+1 for D leg, -1 for R leg) x direction x change; net at the touch: long leg buys ask / exits bid, short leg sells bid / buys back at ask.
  prior: signed 24 h change of outside fair before entry (same orientation); 'aligned' if >= +1c.
"""
import json, os, re, subprocess, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CACHE = sys.argv[1]
UA = "sigbot-research/1.0 (research study; sizhe.pan@gmail.com)"
PAGES = ["2026 United States Senate elections", "2026 United States gubernatorial elections", "2026 United States House of Representatives election ratings"]
SINCE = "2026-10-20T00:00:00Z"
WORD = {"solid": 3, "safe": 3, "likely": 2, "lean": 1, "tilt": .5}
NAMES = {"Cook": "Cook", "IE": "IE", "Sabato": "Sabato", "Econ.": "Economist", "ST": "SplitTicket", "DDHQ": "DDHQ", "FPO": "FPO", "Fox": "Fox", "Silver": "Silver", "RCP": "RCP", "NBC": "NBC", "SB": "Silver", "Economist": "Economist"}


def api(**p):
    args = ["curl", "-s", "-A", UA, "-G", "https://en.wikipedia.org/w/api.php"]
    for k, v in p.items():
        args += ["--data-urlencode", f"{k}={v}"]
    for _ in range(4):
        out = subprocess.run(args, capture_output=True, text=True).stdout
        try:
            return json.loads(out)
        except Exception:
            time.sleep(5)
    raise RuntimeError("api fail")


def fetch(title):
    f = os.path.join(CACHE, title.replace(" ", "_") + ".json")
    if os.path.exists(f):
        return json.load(open(f))
    revs = []
    d = api(action="query", prop="revisions", titles=title, rvlimit=500, rvprop="ids|timestamp|comment|user", rvend=SINCE, format="json", formatversion=2)
    revs = d["query"]["pages"][0]["revisions"][::-1]
    txt = {}
    for i in range(0, len(revs), 20):
        ids = [str(r["revid"]) for r in revs[i:i + 20]]
        d = api(action="query", prop="revisions", revids="|".join(ids), rvprop="ids|content", rvslots="main", format="json", formatversion=2)
        for pg in d["query"]["pages"]:
            for r in pg["revisions"]:
                txt[str(r["revid"])] = r["slots"]["main"]["content"]
        time.sleep(1)
    for r in revs:
        r["text"] = txt.get(str(r["revid"]), "")
    json.dump(revs, open(f, "w"))
    return revs


def score(cell):
    m = re.search(r"USRaceRating\|([A-Za-z]+)(?:\|([DR]))?", cell)
    if not m:
        return None
    w = m.group(1).lower()
    if w.startswith("toss"):
        return 0.0
    if w not in WORD or not m.group(2):
        return None
    return WORD[w] * (1 if m.group(2) == "D" else -1)


STATES = None


def parse(title, text):
    """-> {(race_key, rater): (label, score)} using the comment-labelled rows (Senate, governors) or the header order (House)."""
    out = {}
    if "House" in title:
        hdr = []
        for l in text.split("\n"):
            m = re.match(r"!\[\[[^\]|]*\|([^\]]+)\]\]<br", l)
            if m and m.group(1) in NAMES:
                hdr.append(NAMES[m.group(1)])
            if l.startswith("|-") and len(hdr) >= 6:
                break
        for row in text.split("\n|-")[1:]:
            m = re.search(r"\{\{ushr\|([A-Z]{2})\|([A-Z0-9]+)", row)
            if not m:
                continue
            st, dist = m.group(1), m.group(2)
            key = f"{st}-{int(dist):02d}" if dist.isdigit() else f"{st}-{dist}"
            cells = [score(c) for c in re.findall(r"^\|\s*\{\{USRaceRating[^\n]*", row, flags=re.M)]
            cells = re.findall(r"^\|\s*(\{\{USRaceRating[^\n]*)", row, flags=re.M)
            for h, c in zip(hdr, cells):
                s = score(c)
                if s is not None:
                    out[(key + " House race", h)] = (c, s)
        return out
    kind = "Senate" if "Senate" in title else "Governor"
    for row in text.split("\n|-"):
        m = re.search(r"^\s*!\s*\[\[2026[^|\]]*\|([^\]]+)\]\]", row, flags=re.M)
        if not m or "<!--" not in row:
            continue
        name = m.group(1).strip()
        for lab, c in re.findall(r"<!--\s*([A-Za-z.]+)\s*-->\s*\|\s*(\{\{USRaceRating[^\n]*)", row):
            s = score(c)
            if s is not None and lab in NAMES:
                out[(f"{name} {kind}", NAMES[lab])] = (c, s)
    return out


def build():
    rows = []
    for title in PAGES:
        revs = fetch(title)
        prev = None
        for r in revs:
            cur = parse(title, r["text"])
            if prev is not None:
                for k in cur:
                    if k in prev and cur[k][1] != prev[k][1]:
                        rows.append(dict(page=title, revid=r["revid"], ts=r["timestamp"], user=r.get("user", ""), comment=r.get("comment", "")[:120],
                                         race=k[0], rater=k[1], old=prev[k][1], new=cur[k][1]))
            prev = cur
    E = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    E["t"] = [int(pd.Timestamp(x).timestamp()) for x in E.ts]
    # reverted: same (race, rater) goes back to the old value within 6h
    E["reverted"] = False
    for i, r in E.iterrows():
        later = E[(E.race == r.race) & (E.rater == r.rater) & (E.t > r.t) & (E.t <= r.t + 6 * 3600) & (E.new == r.old)]
        if len(later):
            E.loc[i, "reverted"] = True
            E.loc[later.index, "reverted"] = True
    E.to_csv("data/research_diralpha/wiki_ratings.csv", index=False)
    return E


def study(E):
    from lab import Lab
    L = Lab()
    # tolerate the lab's file layout: lab reads data/ relative to cwd
    idx, cols = L.idx, L.cols
    legs = {}
    for j, (r, p) in enumerate(zip(L.race, L.party)):
        if p in ("D", "R"):
            legs.setdefault(r, []).append((j, 1 if p == "D" else -1))
    U = E[~E.reverted].copy()
    U["d"] = U.new - U.old
    ev = U.groupby(["race", "revid"]).agg(t=("t", "first"), d=("d", "sum"), raters=("rater", lambda x: ",".join(sorted(set(x)))), user=("user", "first")).reset_index()
    ev = ev[ev.d != 0]
    print("parsed rating cell changes", len(E), "reverted", int(E.reverted.sum()), "| race x revision events", len(ev))
    hs = {"1h": 12, "4h": 48, "12h": 144, "24h": 288}
    rows = []
    for e in ev.itertuples():
        if e.race not in legs:
            continue
        i = int(np.searchsorted(idx, e.t))
        if i >= len(idx) or idx[i] - e.t > 1800 or e.t < idx[0]:
            continue
        s = 1.0 if e.d > 0 else -1.0
        rec = dict(race=e.race, t=e.t, day=L.day[i], d=e.d, raters=e.raters, i=i)
        pr = []
        for j, sg in legs[e.race]:
            if i >= 288 and np.isfinite(L.fair.values[i, j]) and np.isfinite(L.fair.values[i - 288, j]):
                pr.append(sg * s * (L.fair.values[i, j] - L.fair.values[i - 288, j]))
        rec["prior"] = np.mean(pr) if pr else np.nan
        for hn, h in hs.items():
            mv, fv, nt = [], [], []
            for j, sg in legs[e.race]:
                if i + h >= len(idx):
                    continue
                m0, m1 = L.mid.values[i, j], L.mid.values[i + h, j]
                f0, f1 = L.fair.values[i, j], L.fair.values[i + h, j]
                if np.isfinite(m0) and np.isfinite(m1):
                    mv.append(sg * s * (m1 - m0))
                if np.isfinite(f0) and np.isfinite(f1):
                    fv.append(sg * s * (f1 - f0))
                b0, a0, b1, a1 = L.B[i, j], L.A[i, j], L.B[i + h, j], L.A[i + h, j]
                if all(np.isfinite([b0, a0, b1, a1])):
                    nt.append((b1 - a0) if sg * s > 0 else (b0 - a1))
            rec[f"mid{hn}"] = np.mean(mv) if mv else np.nan
            rec[f"fair{hn}"] = np.mean(fv) if fv else np.nan
            rec[f"net{hn}"] = np.mean(nt) if nt else np.nan
        rows.append(rec)
    R = pd.DataFrame(rows)
    R.to_csv(os.path.join(CACHE, "event_returns.csv"), index=False)
    print("usable events (priced race x revision):", len(R), "| distinct race-days:", R.groupby(["race", "day"]).ngroups, "| days:", R.day.nunique(), "| races:", R.race.nunique())

    def rep(D, name):
        print(f"\n== {name}: n={len(D)}")
        for hn in hs:
            line = f"  {hn:>3}: mid {100*D[f'mid{hn}'].mean():+.2f}c (n={D[f'mid{hn}'].notna().sum()}, win {np.mean(D[f'mid{hn}'].dropna()>0):.0%})  fair {100*D[f'fair{hn}'].mean():+.2f}c"
            if hn != "1h":
                x = D[f"net{hn}"].dropna()
                line += f"  NET {100*x.mean():+.2f}c (n={len(x)}, win {np.mean(x>0):.0%}, t(race-day)={tstat(D, f'net{hn}'):+.2f})"
            print(line)
    rep(R, "all")
    rep(R[R.prior >= .01], "fair already moved that way in prior 24h (>= +1c)")
    rep(R[R.prior < .01], "fair had not moved that way (< +1c)")
    for k in ("Senate", "Governor", "House"):
        rep(R[R.race.str.contains(k)], k)
    # one-per-race-day version (first event of the race that day)
    rep(R.sort_values("t").groupby(["race", "day"]).head(1), "first event per race-day")
    return R


def tstat(D, c):
    g = D.dropna(subset=[c]).groupby(["race", "day"])[c].mean()
    return g.mean() / (g.std() / np.sqrt(len(g))) if len(g) > 2 and g.std() > 0 else np.nan


if __name__ == "__main__":
    os.makedirs(CACHE, exist_ok=True)
    E = build()
    print(E.groupby("page").size(), E.rater.value_counts().to_dict())
    study(E)
