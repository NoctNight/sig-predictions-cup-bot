# Missed because of holding capital? 24 Oct 00:00 UTC to 29 Oct 11:00 UTC

Question (Sizhe, 29 Oct): "we've quite literally stayed at the same pnl for 5 days which is pretty bad. look for
opportunities that we clearly missed due to holding capital".

**Answer: little was missed. The plateau comes from having no edge, not from capital.**

Over the window, holding the 24 Oct book earned **-0.5k at the touch (-0.4k walking the book)** on 84.9k of holdings.
Every trade that cash blocked or shrank was worth **-0.6k to +0.1k** at the sleeves' measured live edges. The trades
that cash blocked in the hindsight scan would have **lost** 0.5k. Freeing capital earlier would not have moved the
balance.

Script: `research_notes/missed_capital_study.py` (`run`, then `plot`; read-only, no SIG calls). Outputs are in
`data/research_missed_capital/`. Figure: `research_notes/figures/missed_capital_decision.png`.

## 0. What actually bound: the shock reserve, not the holdings

* Free cash was never short. It stayed at 15-21k all window (median 17.6k).
* From 24 Oct 19:00 the 15k shock reserve came off the top. Every other sleeve plans with free cash - 15k, so the
  spendable median was **2.6k**.
* No shock fired in 4.7 days, so the reserve sat idle. That costs nothing under the cash objective, because it is
  still liquid on 3 Nov.
* The cash raise tried to refill the reserve and the sleeves' shortfalls:
  * 1,361 cycles found no clip under 1% of mid;
  * 120 clips raised 22.0k, costing 92.

## 1. Hold side: what the capital earned

The 24 Oct 00:00 book is frozen, and each component is valued by selling it at the touch, then by walking the whole
position through the book (snapshots 24 Oct 00:01 and 29 Oct 10:04).

| bucket | tied up 24 Oct, touch / dumped | value 29 Oct, touch | earned by holding, touch / walked | settlement or fair value |
|---|---|---|---|---|
| RI Governor sets (12,304) | 23.7k / 23.4k | 23.9k | **+185 / +16** (exit cost 15 -> 12 ticks a set) | 24.6k |
| MN-05 sets (21,162) | 20.7k / 20.2k | 20.3k | **-423 / -173** (exit cost 4 -> 8 ticks a set) | 21.2k |
| arb sets, 14 races | 10.4k / 10.4k | 10.2k | **-158 / -147** | 10.8k |
| of which Hawaii Governor (7,401 buy-sets) | 7.1k | 6.9k | **-185** (R bid fell from 0.055 to 0.025) | 7.4k |
| leftover legs, 71 races | 30.1k / 30.0k | 29.9k | **-125 / -136** | 31.5k (outside fair) |
| **total** | **84.9k / 84.0k** | **84.4k** | **-523 / -440** | |
| cheap favourites (38 legs, bought 24-26 Oct) | cost 15.0k | 14.8k | **-194 at the touch, +742 at the outside fair** | |

* The account at mid went from 102,037 to 102,433 (+396, at 29 Oct 11:26; it drifts with each poll).
* Set exit costs barely moved in five days, apart from MN-05 and Hawaii widening. Holding mostly waited for settlement.
* Cheap favourites are worth +742 at the outside fair, but SIG's touch has not converged to it (-194). Under the cash
  objective only the touch counts.

## 2. Missed side: trades skipped or shrunk for cash, by sleeve

| sleeve | evidence in live.log / DB | capital wanted | value at measured live edge | optimistic |
|---|---|---|---|---|
| MM size | budget = min(20k, 0.9 x spendable); order notional fell from ~85 (24 Oct morning) to 6-35 | full budget | **-34** (full-size markout -0.13 ticks/share, 22-24 Oct) | +74 (window markout +0.29) |
| high-frequency arb | 205 "skipped/cut for cash" lines, 17 episodes, 25 Oct 12:20 to 27 Oct 03:56; more went unlogged (run.py:2174 logs nothing while spendable < 0, 266 cycles) | 12.3k sets within the 4k pot | **-10** (live -0.16 ticks/set, n=5) | +52 (quote replay: +2 ticks or 24 h; ignores fills) |
| cheap favourites | 287 shortfall lines, 24-26 Oct | 0: the whole 15k budget got spent (14,999), cash only delayed it | **0** | 0 |
| shock | reserve idle, 0 shocks | — | 0 | 0 |
| catch-up | 0 cash lines | — | 0 | 0 |
| **total because of cash** | | | **-44** | **+126** |
| *not cash:* **pause** of set arbs, recycles and high-frequency arb | pingpong stop 27 Oct 03:57 UTC (`params/strategy_kill.json`; Montana Senate / CO-04 recycling into each other); since then 0 set-arb or recycle entries (3 unwinds only) and 0 high-frequency arb entries, against 13-19 set arbs a day before | — | -33 (arb_trades edges before the pause) | **+86** at the stops' P&L rate (arb +49, high-frequency arb +52 over ~2.7 days = 37/day, × 2.3 days); +139 at the monitor's +0.93% |

From 27 Oct 03:57 the pause, not cash, blocked those entries. The high-frequency arb was also cash-blocked from 25 Oct
12:20 until the pause. Its skips are counted under cash above and not counted again here.

## 3. Hindsight scan: large SIG mispricings against the outside fair

The scan covers every leg where the SIG touch sat **≥ 3c through the Polymarket/Kalshi fair**, with PM and Kalshi
within 4 pts, for ≥ 10 minutes:

* 1,261 episodes;
* enter at the second poll at the touch;
* exit at the touch when SIG reaches the fair, otherwise after 72 h or now;
* a 500 S ticket per episode.

| what actually blocked it | n | P&L (S, per 500 ticket, summed) | mean (ticks/share) |
|---|---|---|---|
| **cash** (a sleeve's rule covered it, spendable < ticket) | 49 | **-521** | -3.6 |
| partisan cap | 540 | -645 | -0.6 |
| sleeve rule (cash was free) | 151 | -1,164 | -2.8 |
| no sleeve (cash was free) | 206 | -1,559 | -2.0 |
| no sleeve (cash also short) | 63 | -58 | -0.5 |
| different contract terms | 201 | -969 | -1.7 |
| taken | 55 | -18 | -0.2 |

* At 2c and at 5c the "cash" row is also negative (-219 and -184).
* The reviewer tried other exit rules for the 3c "cash" row and it stayed negative under all of them: hold to now at
  the touch -939, now at mid -820, 72 h at mid -513, the study's exit time at mid -423.
* Adding "no sleeve (cash also short)" to the cash row: -579.
* It turns positive only when marked at the outside fair (+737). That is settlement value, which SIG's touch never
  reached.
* Only 12% of episodes converged within 72 h. The converged "cash" ones still lost (-91), because the fair moved to
  SIG rather than SIG moving to the fair.
* SIG gaps against the outside fair do not close net of the spread over days. This agrees with the earlier
  no-directional-edge finding.

## 4. Verdict

* **Realised value missed because of capital: -0.6k to +0.1k.** That is -44 at the measured edges, plus -521 from the
  hindsight cash-blocked trades; or +126 if the optimistic marks are used and the hindsight row is ignored.
* **Missed because of the 27 Oct pause (not cash): about +0.1k** (-33 to +139; +86 at the stops' P&L rate).
* **Value of holding: -0.5k at the touch / -0.4k walked** on 84.9k.
  * The losers were MN-05 (-423) and Hawaii Governor (-185); RI Governor made +185.
  * Holding cost more than any missed trade, but it is still 0.6% of the book, and the exits cost about as much.
* Freeing capital would not have helped. The sleeves that cash cut (MM, high-frequency arb) have about zero realised
  edge. Cheap favourites got its full budget. The cash-blocked mispricings lost money.
* **What we should have sold, and when:**
  * **MN-05 on 24 Oct.** Its exit cost doubled from 4 to 8 ticks a set; selling then would have saved 0.2-0.4k. It is
    an early-call set, held to its call by design (arb_ideas idea 3), so today's 8 ticks is what the wind-down faces.
  * **Hawaii Governor buy-sets on 24 Oct** (-185). This is a late race that the wind-down sells anyway.
  * Everything else was flat or up.
* **Rules for 29 Oct to 3 Nov:**
  * Do **not** add a rule that sells holdings to fund sleeves. None has a realised edge that beats a 1% exit cost.
  * Do **not** lift the MM budget by shrinking the reserve. The full-size era's markouts were negative.
  * Keep the wind-down as built: sell from 31 Oct against the exit-cost medians.
  * The only open lever is not about capital. The high-frequency arb and the set arbs/recycles are still off from the
    27 Oct pingpong stop, and only Sizhe can re-enable them. The pause has cost about +86 so far (+37/day at the
    stops' rate). Over the 2-3 days left before the wind-down, that is worth roughly +0.1k at most.

## Caveats

* Touch prices come from quotes polled about every 2 min, and hindsight entries assume a 500 S ticket fills at the
  touch.
* The hold side freezes the 24 Oct book, so trims, raises and recycles made in the window are not credited. Account
  value at mid, +396, is the all-in figure.
* About 20 h of bot downtime fell in the window: 842 "cycle failed" DNS errors, and cycle gaps of up to 229 min on
  28 Oct. That is not capital, and it is small at the MM's ~+15/day.
* "Cash" labels use a spendable figure interpolated from the cycles table and a simplified cheap-favourites/gap
  eligibility test (same terms, fast-count state, favourite ≥ 0.80, not a punt race).
* The partisan-cap test uses net D read from the log, cap 5,000 minus 1,000 headroom.

## Independent check

An independent Opus 5.5 reviewer recomputed the claims and tried to refute them.

* It found three bugs, all fixed and rerun:
  * part 1 dropped 4 of 110 T0 positions whose venue_fair rows stopped before 23 Oct, among them Hawaii Governor R
    7,401 shares. Holding moved from -300 to -523;
  * two cheap-favourite legs had no fair. At the outside fair the bucket moved from +425 to +742;
  * the high-frequency arb's logged 'unmet' is notional, not sets.
* It confirmed:
  * 110 positions at T0 (= cycles.n_positions);
  * NO-leg liquidation;
  * the MM budget rule (mm.py:565) and the markout sign and eras (-0.135 / +0.338 / +0.265);
  * the high-frequency arb counts and values;
  * the cheap-favourites budget filling;
  * recycle and shock numbers;
  * the hindsight sign under every exit rule.
* Verdict unchanged: little was missed, and the cause is no edge, not capital.
