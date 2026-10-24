"""Projected liquid cash at 18:00 ET 3 Nov by holding (backlog 000e), offline, read-only.

    python3 research_notes/wind_down_projection.py [--md out.md]

Positions = the fills ledger netted per leg (data/sigbot.sqlite, read-only; it matches the venue's sets to the share on
24 Oct), depth = data/books_latest.json (a leg missing there: the newest data/books_history snapshot that has it), favourite = the latest Polymarket/Kalshi fair (venue_fair), else the SIG mid,
cash = the last cycle's cash. The same WD.project the bot writes hourly to params/wind_down_report.json. No API calls.
"""
from __future__ import annotations

import collections
import glob
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from sigbot.proposals import wind_down as WD  # noqa: E402
from sigbot.universe import Universe  # noqa: E402


def main(argv):
    con = sqlite3.connect("file:" + os.path.join(HERE, "data", "sigbot.sqlite") + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pos = collections.defaultdict(float)
    cheap = collections.defaultdict(float)
    catch = collections.defaultdict(float)
    for r in con.execute("SELECT exchange_id, purpose, SUM(quantity) q FROM fills GROUP BY 1, 2"):
        pos[str(r["exchange_id"])] += r["q"]
        if r["purpose"] in ("cheapfav", "cheapfav_exit"):
            cheap[str(r["exchange_id"])] += r["q"]
        if r["purpose"] in ("catchup", "catchup_exit"):
            catch[str(r["exchange_id"])] += r["q"]
    cash = con.execute("SELECT cash FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()["cash"]
    fair = {}
    for r in con.execute("SELECT exchange_id, fair FROM venue_fair v WHERE ts = (SELECT MAX(ts) FROM venue_fair w WHERE w.race = v.race "
                         "AND w.party = v.party) AND fair IS NOT NULL"):
        fair[str(r["exchange_id"])] = float(r["fair"])
    con.close()
    u = Universe.from_markets(json.load(open(os.path.join(HERE, "data", "markets.json"))))
    books = json.load(open(os.path.join(HERE, "data", "books_latest.json")))
    hist = sorted(glob.glob(os.path.join(HERE, "data", "books_history", "books_*.json")), reverse=True)
    for path in hist:                        # a leg missing from the latest snapshot: the newest older book of it
        missing = [e for e in u.by_exchange if e not in books]
        if not missing:
            break
        old = json.load(open(path))
        books.update({e: old[e] for e in missing if e in old})
    for e, b in books.items():
        if e not in fair and b.get("bids") and b.get("asks"):
            fair[e] = 0.5 * (b["bids"][0]["price"] + b["asks"][0]["price"])

    def fav_of(name):
        xs = [fair[e] for e in u.races[name].exchange_ids if e in fair]
        return max(xs) if xs else None

    exempt_races = {}
    for name, r in u.races.items():
        if any(abs(cheap.get(e, 0.0)) >= 1 for e in r.exchange_ids):
            exempt_races[name] = "cheap favourites"
    try:
        for l in json.load(open(os.path.join(HERE, "params", "churn_state.json"))).get("lots", []):
            exempt_races[l["race"]] = "high-frequency arb"
    except (OSError, ValueError):
        pass
    exempt_legs = {e: "catch-up sweep" for e, q in catch.items() if abs(q) >= 1}
    rep = WD.project({n: r.exchange_ids for n, r in u.races.items()}, dict(pos), books, fav_of, cash,
                     exempt_races=exempt_races, exempt_legs=exempt_legs)
    out = render(rep)
    if "--md" in argv:
        open(argv[argv.index("--md") + 1], "w").write(out)
    print(out)


def render(rep) -> str:
    rows, t = rep["rows"], rep["totals"]
    L = []
    sets = sorted([x for x in rows if x["kind"] in ("sell_set", "buy_set")], key=lambda x: (x["call"], -(x["sale_value"] or 0)))
    L.append("| holding | sets | call | why | payout | exit cost now (ticks, touch) | clip | sale per set | sale value | cost vs payout | min hours | when |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for x in sets:
        L.append(f"| {x['holding']} ({'NO' if x['kind'] == 'sell_set' else 'YES'}) | {x['qty']:,} | {x['call']} | {x['why']} | {x['payout']:,.0f} | "
                 f"{x['touch_cost_ticks']} | {x['clip']} | {x['sale_per_set']} | {x['sale_value'] or 0:,.0f} | {x['exit_cost'] or 0:,.0f} | "
                 f"{x['hours_min']} | {x['when']} |")
    legs = sorted([x for x in rows if x["kind"] in ("leg", "mm_leg")], key=lambda x: -(x["sale_value"] or 0))
    L.append("")
    L.append("| leg | shares | side | sale per share (touch) | sale value | when |")
    L.append("|---|---|---|---|---|---|")
    for x in legs:
        if (x["sale_value"] or 0) < 25:
            continue
        L.append(f"| {x['holding']} | {x['qty']:,} | {x['side']} | {x['sale_per_share']} | {x['sale_value'] or 0:,.0f} | {x['when']} |")
    small = [x for x in legs if (x["sale_value"] or 0) < 25]
    L.append(f"| {len(small)} smaller legs | | | | {sum(x['sale_value'] or 0 for x in small):,.0f} | |")
    ex = [x for x in rows if x["kind"] == "exempt"]
    L.append("")
    L.append("| exempt holding | sleeve | legs | when |")
    L.append("|---|---|---|---|")
    for x in ex:
        L.append(f"| {x['holding']} | {x['sleeve']} | {x['legs']} | {x['when']} |")
    L.append("")
    L.append("| total | S |")
    L.append("|---|---|")
    for k in ("cash_now", "late_sets_sale", "legs_sale", "projected_cash_1800_et", "early_sets_sale_today", "early_sets_payout",
              "projected_cash_if_early_sold_too"):
        L.append(f"| {k.replace('_', ' ')} | {t[k]:,.0f} |")
    return "\n".join(L)


if __name__ == "__main__":
    main(sys.argv[1:])
