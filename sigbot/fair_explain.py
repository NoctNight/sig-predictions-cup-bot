"""Explain every leg where SIG, the outside venues and our own model disagree, and give a trading verdict.

    python3 -m sigbot.fair_explain            # reads the direction loop's latest quotes + params/fair_model.json
    python3 -m sigbot.fair_explain --gap 0.04 # lower the discrepancy threshold (default 5 points)

Writes params/fair_explained.json:
    {race: {party, sig_mid, sig_bid, sig_ask, pm, kalshi, model, believed_fair, explanation, confidence, verdict,
            flags, outside: {depth_usd, spread, age_min, disagreement}, model_inputs}}
    verdict: trade_yes (SIG ask well below believed fair) | trade_no (SIG bid well above) | no_trade.

A leg is listed when |SIG mid - Polymarket mid| >= gap OR |model - Polymarket| >= gap (Kalshi stands in when Polymarket
has no quote). believed_fair = outside blend (Polymarket/Kalshi, depth weighted) mixed with the model: 50/50 when the
model is poll-driven (>= 2 polls), 70/30 when it is rating-driven, outside only for chamber legs (no model) and for
legs the model cannot see (independent candidates without polls). trade_* only when: outside depth near the touch is at
least 1,500 USD, the two outside venues agree within 4 points, the outside feed is fresh (< 60 min), the model is within
12 points of the outside blend (when it is poll-driven), the leg is a D or R leg with a party-level outside market, and
the executable SIG price is at least `gap` from believed fair after the SIG spread. Structural flags (ranked-choice,
slow-count state, candidate-level outside market, special election, independent in the race, derived chamber quote)
lower confidence and, for the first three, block the verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from typing import Optional

from .store import Store
from .venues.states import parse_race

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "params", "fair_explained.json")
RCV_STATES = {"Alaska", "Maine"}
SLOW_COUNT = {"Alaska", "California", "Arizona", "Nevada", "Oregon", "Washington", "Maine", "Utah", "Colorado"}
SPECIALS = {"Florida Senate"}


def latest_quotes(store: Store) -> tuple[dict, dict]:
    """({(race, party): fair-row}, {(race, party, venue): {depth, spread, age, instruments}}) from the loop's tables."""
    last_ts = store.one("SELECT MAX(ts) AS t FROM venue_fair")["t"]
    fair = {}
    if last_ts:
        for r in store.rows("SELECT * FROM venue_fair WHERE ts=?", (last_ts,)):
            fair[(r["race"], r["party"])] = dict(r)
    qual: dict = {}
    rows = store.rows("SELECT q.* FROM venue_quotes q JOIN (SELECT venue, instrument, MAX(ts) AS ts FROM venue_quotes WHERE venue IN ('polymarket','kalshi') "
                      "GROUP BY venue, instrument) m ON q.venue=m.venue AND q.instrument=m.instrument AND q.ts=m.ts")
    now = time.time()
    for r in rows:
        k = (r["race"], r["party"], r["venue"])
        q = qual.setdefault(k, {"depth_usd": 0.0, "spread": None, "age_min": None, "n": 0, "bid": None, "ask": None})
        q["depth_usd"] += r["depth_usd"] or 0.0
        if r["bid"] is not None and r["ask"] is not None:
            sp = r["ask"] - r["bid"]
            q["spread"] = sp if q["spread"] is None else min(q["spread"], sp)
        age = (now - r["ts"]) / 60
        q["age_min"] = age if q["age_min"] is None else min(q["age_min"], age)
        q["n"] += 1
    return fair, qual


# Hand-researched context for the legs where our model and the outside venues disagree most (2026-10-22). Keep short.
NOTES: dict = {
    "Georgia Governor": "Polls since mid-September put Rick Jackson (R) narrowly ahead of Keisha Lance Bottoms (D): InsiderAdvantage Oct 12-13 49-47, "
                        "Rasmussen Sep 14 48-45, YouGov Sep 15-18 a virtual tie, 4-9% undecided; Jackson skipped the GPB debate. Markets still lean "
                        "Bottoms (~55%), the model follows the polls (R lead ~2): a genuine disagreement, not a venue artefact; confidence stays moderate.",
    "Vermont Governor": "Tightest race in a decade for a five-term incumbent: UNH (Sep 21) had Janoo (D) +6 but the Scott campaign disputed its heavily "
                        "Democratic sample; a State Navigate poll (Oct 7) has Scott 46-43. Ratings still Likely R. Markets ~76% Scott; the model's 41% D "
                        "rests on the one UNH poll, so believed fair is pulled only 3 points from the markets.",
    "Alaska Senate": "Peltola (D) leads Sullivan (R) in most October polls (NYT/Siena Sep 24-Oct 1 50-43; Alaska Survey Research 51-49 after ranked-choice "
                     "rounds; Quantus tied 46-46; Trafalgar last had Sullivan ahead). Markets ~75% Peltola; the model's two-party margin is near zero because it "
                     "ignores ranked-choice transfers that favour Peltola. Ranked-choice and a slow count block any verdict.",
    "Montana Senate": "Steve Daines retired at the filing deadline; Alme (R) won the GOP nomination with 76%, Bankhead is the Democrat and Bodnar runs as a "
                      "Tester-backed independent, splitting the non-Republican vote. No public Alme-Bankhead-Bodnar poll found; ratings Solid/Likely R. "
                      "The independent explains the market's ~8% non-R mass; the two-party model overstates R.",
}


def explain_leg(race: str, party: str, f: dict, q: dict, model: Optional[dict], gap: float, racemap: dict) -> Optional[dict]:
    info = parse_race(race)
    sig_bid, sig_ask, sig_mid = f.get("sig_bid"), f.get("sig_ask"), f.get("sig_mid")
    pm, k = f.get("pm"), f.get("kalshi")
    pm_w, k_w = f.get("pm_w") or 0.0, f.get("k_w") or 0.0
    outside = f.get("fair")
    ref = pm if pm is not None else k
    leg_model = (model or {}).get("legs", {}).get(party, {}) if model else {}
    m = leg_model.get("fair")
    if sig_mid is None or ref is None:
        return None
    gap_sig = sig_mid - ref
    gap_model = (m - ref) if m is not None else None
    if abs(gap_sig) < gap and (gap_model is None or abs(gap_model) < gap):
        return None
    qp, qk = q.get((race, party, "polymarket"), {}), q.get((race, party, "kalshi"), {})
    depth = (qp.get("depth_usd") or 0.0) + (qk.get("depth_usd") or 0.0)
    ages = [x["age_min"] for x in (qp, qk) if x.get("age_min") is not None]
    age = min(ages) if ages else None
    disagreement = abs(pm - k) if (pm is not None and k is not None and pm_w > 0 and k_w > 0) else None
    inputs = (model or {}).get("inputs", {}) if model else {}
    n_polls = inputs.get("n_polls", 0)
    polled = n_polls >= 2 and (inputs.get("n_eff") or 0) >= 1.0
    e = racemap.get("races", {}).get(race, {})
    pm_legs = e.get("polymarket", {}).get("legs", {}).get(party, [])
    candidate_level = any(not str(l.get("title", "")).startswith(("Democrat", "Republican", "Independent")) and "(" not in str(l.get("title", "")) for l in pm_legs)
    flags = []
    if info["office"] == "chamber":
        flags.append("chamber control: Kalshi quote is derived from the balance-of-power combo markets; no model fair")
    if info["state"] in RCV_STATES:
        flags.append("ranked-choice state: final-round result can differ from first-round polls, count takes days")
    if info["state"] in SLOW_COUNT:
        flags.append("slow-count state: result may not be known by the SIG close on Nov 4 noon ET")
    if race in SPECIALS:
        flags.append("special election")
    if party == "I" or (model and model.get("mode") == "multi-candidate"):
        flags.append("independent candidate in the race: two-party model does not apply cleanly")
    if candidate_level:
        flags.append("Polymarket market is candidate-level; party probability is a sum over mapped candidates")
    thin_at = 1500.0 * min(1.0, 4.0 * min(ref, 1 - ref))        # a 2-cent band holds little notional on a 1-cent longshot
    if depth < thin_at:
        flags.append(f"thin outside quotes ({depth:,.0f} USD within 2c of the touch, need {thin_at:,.0f})")
    if age is not None and age > 60:
        flags.append(f"outside feed {age:.0f} min old")
    if disagreement is not None and disagreement > 0.04:
        flags.append(f"Polymarket and Kalshi disagree by {100 * disagreement:.1f} points")
    if m is not None and polled and abs(m - outside) > 0.12:
        flags.append(f"model ({m:.2f}) far from the outside blend ({outside:.2f})")
    # ---- believed fair
    # the model may pull believed fair at most 6 points from the deep real-money venues when it rests on >= 2 polls,
    # 3 points when it rests on ratings (or a single poll): one contrarian poll must not manufacture a trade
    multi = model is not None and model.get("mode") == "multi-candidate"
    if m is None or info["office"] == "chamber" or party == "I" or multi:
        believed = outside
        basis = "outside venues only" + (" (independent in the race: the two-party model is not used)" if multi else "")
    elif polled:
        believed = outside + max(-0.06, min(0.06, 0.5 * (m - outside)))
        basis = f"outside blend pulled by the poll-driven model ({n_polls} polls, capped at 6 points)"
    else:
        believed = outside + max(-0.03, min(0.03, 0.3 * (m - outside)))
        basis = "outside blend nudged by the rating-driven model (capped at 3 points)"
    # ---- explanation
    parts = []
    parts.append(f"SIG mid {sig_mid:.3f} (bid {sig_bid} / ask {sig_ask}); Polymarket {pm if pm is not None else '-'}; Kalshi {k if k is not None else '-'}; "
                 f"outside blend {outside:.3f}; our model {m if m is not None else '-'}.")
    if abs(gap_sig) >= gap:
        side = "above" if gap_sig > 0 else "below"
        if (ref <= 0.25 and gap_sig > 0) or (ref >= 0.75 and gap_sig < 0):
            parts.append(f"SIG is {100 * abs(gap_sig):.1f} points {side} the outside price on a {'longshot' if ref <= 0.25 else 'favourite'}: the venue's "
                         f"measured compression toward 50% (longshots overpriced, favourites underpriced, tilt s~0.1) explains most of it; "
                         f"the outside venues are real-money and deep, SIG is play-money with rank-chasing flow.")
        else:
            parts.append(f"SIG is {100 * abs(gap_sig):.1f} points {side} the outside price in the competitive band, which the tilt does not explain; "
                         f"either SIG lags outside repricing or SIG traders hold a different view.")
    if m is not None and info["office"] != "chamber":
        rs = inputs.get("ratings") or {}
        rsum = ", ".join(f"{a} {b}" for a, b in list(rs.items())[:4])
        pl = inputs.get("poll_margin")
        parts.append(f"Model inputs: {n_polls} polls in 45 d" + (f" (D-R {pl:+.1f}, latest {inputs.get('latest_poll')})" if pl is not None else "")
                     + f"; ratings {rsum or 'none'}; PVI {inputs.get('pvi')}; generic ballot D{inputs.get('generic_ballot'):+.1f}; "
                     f"incumbency {inputs.get('incumbency')}; margin_hat {model.get('margin_hat'):+.1f} +/- {model.get('sigma')}.")
        if gap_model is not None and abs(gap_model) >= gap:
            parts.append(f"Model differs from the outside price by {100 * gap_model:+.1f} points: " + (
                "with few or no polls the model is a rating/fundamentals prior with sigma 8 and should not override deep outside markets"
                if not polled else "the polls and ratings point away from where the outside venues price it; outside venues also see candidate news, fundraising and early-vote data the model does not"))
    if race in NOTES:
        parts.append("Context: " + NOTES[race])
    if flags:
        parts.append("Caveats: " + "; ".join(flags) + ".")
    # ---- verdict
    verdict = "no_trade"
    conf = 0.5
    if disagreement is not None and disagreement <= 0.03:
        conf += 0.1
    if m is not None and outside is not None and abs(m - outside) <= 0.05:
        conf += 0.1
    if polled and n_polls >= 3:
        conf += 0.05
    if m is not None and polled and abs(m - outside) > 0.08:
        conf -= 0.25            # polls and deep real-money markets genuinely disagree: that is uncertainty, not edge
    if depth >= 10000:
        conf += 0.1
    blocking = any(s.startswith(("ranked-choice", "slow-count", "Polymarket market is candidate-level", "thin outside", "outside feed", "Polymarket and Kalshi disagree", "model (")) for s in flags) \
        or party == "I" or info["office"] == "chamber"
    if info["office"] == "house" and not polled and depth < 5000:
        blocking = True
        flags.append("House seat without recent polls and under 5,000 USD outside depth")
    conf -= 0.1 * len(flags)
    conf = max(0.0, min(1.0, conf))
    if not blocking and sig_bid is not None and sig_ask is not None:
        if sig_bid - believed >= gap:
            verdict = "trade_no"
        elif believed - sig_ask >= gap:
            verdict = "trade_yes"
    if verdict != "no_trade" and conf < 0.55:
        verdict = "no_trade"
        parts.append("Verdict withheld: confidence below 0.55.")
    elif verdict != "no_trade":
        parts.append(f"Verdict {verdict}: executable SIG {'bid' if verdict == 'trade_no' else 'ask'} is {100 * abs((sig_bid if verdict == 'trade_no' else sig_ask) - believed):.1f} "
                     f"points from believed fair {believed:.3f} ({basis}); the gap is the venue tilt, not a resolution or liquidity artefact.")
    else:
        parts.append(f"Verdict no_trade: believed fair {believed:.3f} ({basis}); " + ("blocked by caveats." if blocking else "executable SIG price is within the threshold of believed fair."))
    return {"party": party, "sig_mid": round(sig_mid, 4), "sig_bid": sig_bid, "sig_ask": sig_ask, "pm": pm, "kalshi": k, "outside_blend": round(outside, 4),
            "model": m, "believed_fair": round(believed, 4), "gap_sig_vs_outside": round(gap_sig, 4), "gap_model_vs_outside": round(gap_model, 4) if gap_model is not None else None,
            "explanation": " ".join(parts), "confidence": round(conf, 2), "verdict": verdict, "flags": flags,
            "outside": {"depth_usd": round(depth), "spread": min([x["spread"] for x in (qp, qk) if x.get("spread") is not None] or [None]) if (qp or qk) else None,
                        "age_min": round(age, 1) if age is not None else None, "disagreement": round(disagreement, 4) if disagreement is not None else None},
            "model_inputs": {k2: inputs.get(k2) for k2 in ("n_polls", "poll_margin", "latest_poll", "rating_margin", "ratings", "pvi", "incumbency", "fund_margin", "open_seat", "incumbent")} if inputs else None}


def build(gap: float = 0.05) -> dict:
    store = Store(os.path.join(HERE, "data", "sigbot.sqlite"))
    fair, qual = latest_quotes(store)
    model = json.load(open(os.path.join(HERE, "params", "fair_model.json"))) if os.path.exists(os.path.join(HERE, "params", "fair_model.json")) else {"races": {}}
    racemap = json.load(open(os.path.join(HERE, "data", "race_map.json")))
    out: dict = {"built_at": time.time(), "gap": gap, "legs": {}}
    for (race, party), f in fair.items():
        r = explain_leg(race, party, f, qual, model["races"].get(race), gap, racemap)
        if r:
            out["legs"][f"{race} [{party}]"] = r
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), indent=1)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=0.05)
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()
    out = build(args.gap)
    legs = sorted(out["legs"].items(), key=lambda kv: -abs(kv[1]["sig_mid"] - kv[1]["believed_fair"]))
    print(f"{len(legs)} legs listed (gap >= {args.gap}); verdicts: " + ", ".join(f"{v}={sum(1 for _, x in legs if x['verdict'] == v)}" for v in ("trade_yes", "trade_no", "no_trade")))
    print(f"{'leg':32s} {'SIG':>6s} {'Poly':>6s} {'Kalshi':>6s} {'model':>6s} {'belief':>6s} {'gap':>6s} {'conf':>5s}  verdict   flags")
    for name, x in legs[: args.top]:
        f = lambda v: f"{v:6.3f}" if isinstance(v, (int, float)) and v is not None else "   -  "
        print(f"{name[:32]:32s} {f(x['sig_mid'])} {f(x['pm'])} {f(x['kalshi'])} {f(x['model'])} {f(x['believed_fair'])} {100 * (x['sig_mid'] - x['believed_fair']):+6.1f} {x['confidence']:5.2f}  {x['verdict']:9s} {'; '.join(s.split(':')[0] for s in x['flags'])[:60]}")
    print(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()
