# Directional alpha on SIG: round 2 ideas (31 Oct, about 02:00 UTC)

Twenty new ideas in three groups, on top of `FINDINGS.md`, `audit_lag.md` and the first 40. Nothing here is a tradable result. Nothing was traded and no SIG API was called.

**Base for every number.** Strict trades from `d15` (`strict_fast.pkl` 30 trades at +1.88c, `strict_slow.pkl` 56 at +1.49c), 22-30 Oct, touch both ways. Splits have small n. These cuts add about 40 tests that are not in `ledger.jsonl`, so read any single split with the 0.4c haircut in mind.

**Scripts.** `h2_internal.py`, `h2_internal2.py`, `h2_internal3.py`, `h2_fetch.py`, `h2_fec_load.py`, `h2_fec2.py`, `h2_external.py`, `h2_2024.py`. Raw public pulls are in the session scratchpad, not the repo.

**Definitions.** mid = (`sig_bid` + `sig_ask`) / 2. gap = `fair` - mid. Copy = SIG mid change as a share of the `fair` change. "Both-venue move" = `pm` and `kalshi` each moved 1.5c+ the same way in 30 min, mid 15-85, no chamber legs, 119 events.

## What changed from round 1

- **Best news:** one non-venue source shows a real link. FEC outside-spending shifts lead both `fair` and SIG (A1), but too weakly to pay a spread alone.
- **Best filters:** a stale leg and real outside taker flow each about double the slow rule (B1, B2). Both need out-of-sample confirmation.
- **Biggest practical issues:** the lag is shortening (C1), capital binds before size does (B3), and the wind-down would sell lag positions unless they are exempted (C2).
- **Dead tonight:** DJT, PredictIt daily closes, passive entry, set legging.

## Summary

| ID | Idea | Prior | Measured tonight | Status |
|---|---|---|---|---|
| A1 | FEC outside-spending share shift | 2 alone | +0.70c on mid in 24 h after removing the day mean, p 0.001 | Real link, no standalone trade |
| A2 | Early-vote daily reports | 2 | Fetch works, no history | Forward only |
| A3 | Wikipedia rating edits | 2 | 44 tagged edits | Not event-studied |
| A4 | End-of-tournament selling in SIG sets | 2 | Overround rising, 0.35c to 0.72c | No selling yet |
| A5 | PredictIt as a third venue | 1 | corr -0.005 with next-day `fair` | Dead at daily data |
| A6 | DJT stock leads the national factor | 1 | +0.12c in top-decile moves | Dead |
| B1 | Stale leg filter on the slow rule | 3 | +2.62c (16) vs +1.04c (40) | Test next |
| B2 | Real outside taker flow filter, slow rule | 3 | Kalshi +2.35c vs +0.75c, Polymarket +2.61c vs +0.96c | Test next |
| B3 | Rank by return on capital | 4 | Fast 11.0c vs slow 1.48c per capital-day | Decide now |
| B4 | FEC agreement as an overlay | 3 | Agree +2.14c (18), disagree +0.81c (18) | Test next |
| B5 | Mirror leg as second tranche | 3 | Mirror +1.23c fast, +1.02c slow | Only above 10-16k shares |
| B6 | Pull MM quotes after a both-venue move | 3 | -2.09c on 5,041 shares, -$105 in 9 days | Small |
| B7 | Wind-down timing and set legging | 2 | $147 across all history | Too small |
| B8 | Post passively, then cross | 1 | Best case +0.09c | Dead |
| C1 | Lag shortening | 4 | Copied at trigger 36% to 48% | Live warning |
| C2 | Wind-down sells lag positions | 5 | Purpose not exempt; 64% of trades in sell-list races | Must fix before trading |
| C3 | Trend reversal | 3 | Slow +0.94c against drift, -0.05c when national move hurt | Live warning |
| C4 | Weekend liquidity | 3 | 4 h copy 61% vs 76% | Hold longer, expect fewer |
| C5 | Final polls | 2 | No damage found | No break seen |
| C6 | Clock change | 1 | No break in the rule | Two labels to note |

## (a) Different sources of direction

**A1. FEC outside-spending shift. Prior 2 alone**
- Mechanism: spending groups move money on private polling, and FEC 24-hour reports publish that move. This could matter most in House seats with no public polls.
- Data: `https://www.fec.gov/files/bulk-downloads/2026/independent_expenditure_2026.csv`. Keyless, 12 MB, refreshed about 10:15 UTC. **Fetched tonight.** It holds junk filings (one filer at $12bn), so rows above $5M are dropped.
- Test: per race, D share of pro-D plus pro-R dollars by `receipt_dat`, last 3 days minus the prior 14. Needs $50k+ and $100k+. Enter 12:00 UTC the next day, on the D leg, and compare the change in `fair` and mid over 24 h.
- Measured, |shift| >= 0.3: 89 race-days, +0.70c on mid after removing each day's mean, permutation p 0.001.
- Regression with past 3-day `fair` move and gap as controls: t 3.0 on mid, t 3.5 on `fair`. It leads the outside as well as SIG.
- Against it: first event per race +0.25c, net -0.41c. Moves against the past `fair` move +0.06c. Party and leadership committees alone show nothing (t -0.4 on mid, only 5 large shifts), which undercuts the private-polling story.
- Verdict: does not pay the spread alone. Use as an overlay (B4).

**A2. Early-vote daily reports. Prior 2**
- Mechanism: final party turnout in states with registration data may be read late on thin legs.
- Data: `https://election.lab.ufl.edu/early-vote/2026-early-voting/`. Keyless, **fetch works**, last updated 30 Oct 21:39 ET. It shows only the current state, so there is no back-test.
- Test: snapshot twice a day from now. For NV, NC, PA, GA and AZ Senate, Governor and House legs, compare the change in party share to the change in `fair` and mid over the next 24 h. Forward only, a few dozen points at best.

**A3. Wikipedia rating edits. Prior 2**
- Mechanism: an edit timestamp dates a rating change better than a news title does.
- Data: MediaWiki revisions API on the three 2026 overview pages. Keyless, **fetch works**. Since 15 Oct: 26 rating-tagged Senate edits, 17 Governor, 1 House.
- Test: use `action=compare` on those revisions to pull race, old rating and new rating. Then measure `fair` and mid change over 12 / 24 h in the rating's direction.
- Round 1's news-title version lost -0.25c to -0.50c, so expect little. Not event-studied tonight, because the diff parsing would not be quick.

**A4. End-of-tournament selling in SIG sets. Prior 2**
- Mechanism: other entrants need cash for final punts too. Selling sets pushes both legs down at once, so the sum of mids falls under 1 with no news.
- Test: hourly sum of mids minus 1 on two-leg races from `sig_bid` and `sig_ask`. A fall of 0.3c+ in 6 h with `fair` flat means buying the cheaper leg, 12 / 24 h. Watch from 1 Nov.
- Measured: overround rose from 0.35c on 28 Oct to 0.72c on 30 Oct. Gap on 15-35c legs went from -1.73c on 26 Oct to -3.05c on 30 Oct. So buying pressure is growing and forced selling has not started.

**A5. PredictIt as a third venue. Prior 1**
- Mechanism: a separate retail venue may move before Polymarket and Kalshi on some races.
- Data: `https://www.predictit.org/api/marketdata/all/` and `/api/Public/GetMarketChartData/{id}?timespan=7d`. Keyless, **fetched**. 76 races map to SIG. History is daily and only 7 days long.
- Test: PredictIt daily change minus `fair` change, against next-day `fair` and mid.
- Measured: 185 race-days, corr -0.005 with next `fair`, +0.066 with next mid. At |residual| >= 2c, net -0.48c.
- Verdict: dead at daily data. Minute polling is the only version left, and it is forward only.

**A6. DJT stock leads the national factor. Prior 1**
- Mechanism: an equity priced by a deeper market may react first to national news.
- Data: Yahoo `v8/finance/chart/DJT`, 5-minute bars. Keyless, **fetched**.
- Test: DJT 30-minute return against the next 1 h and 4 h of the D-signed national `fair`, regular hours, 7 trading days.
- Measured: corr with next 1 h +0.055 (the wrong sign: DJT up should mean D down), by-day mean +0.045 (SE 0.038). Top decile moves: +0.12c at 4 h in the expected direction.
- Verdict: dead. Only two more sessions remain anyway.

*Not fetchable under tonight's rules:* the venue's own per-market news digest may lead the market maker, but reading it is a SIG API call.

## (b) More edge or capacity from the lag trade

`d17` and `d18` already cover cheaper-side entry and signal-size scaling, so they are not repeated.

**B1. Stale leg filter on the slow rule. Prior 3**
- Mechanism: a leg nobody has touched for hours has not started catching up, while one that just stepped has used part of the move.
- Test: minutes since the mid last changed at the signal. Slow rule only, with the 120 min cut fixed before any forward test.
- Measured: over 120 min +2.62c (16, 81% win), else +1.04c (40). Training +2.75c vs +1.33c, test +2.50c vs +0.91c. Other cuts agree: 30 min +2.25c vs +1.00c, 60 min +2.36c vs +1.08c.
- Against it: 16 trades in 13 races. The fast rule tilts the other way (under 5 min +2.23c, n 15).

**B2. Real outside taker flow, slow rule. Prior 3**
- Mechanism: a price move backed by takers is information. A quote drift may be one maker adjusting.
- Data: Kalshi `markets/trades` and Polymarket `data-api.polymarket.com/trades`. Keyless, **fetched** for all 86 trades.
- Test: dollars of taker flow in the signal's direction over the 30 min before the signal, as a share of volume. Split at 0.3.
- Measured, slow: Kalshi +2.35c (26) vs +0.75c (30). Polymarket +2.61c (18) vs +0.96c (38). Two venues agree.
- Fast rule: little change, +1.94c vs +1.82c and +2.13c vs +1.72c.
- Against it: flow is thin, with a median of $66 on Kalshi and $6 on Polymarket per 30 min, and 28 and 41 events had no trades. Wallet count shows no pattern.

**B3. Rank by return on capital. Prior 4**
- Mechanism: the fast rule earns more per trade and frees cash about six times sooner. Cash, not market depth, limits the book.
- Test: replay the strict trades with the real cash path from `params/wind_down_report.json`, fast rule first, slow only with spare cash. Compare with first come, first served.
- Measured: fast 11.0c per capital-day, slow 1.48c. Open positions at 8 h and 36 h holds: mean 11, p90 17, max 22. At 4 h and 24 h: mean 7.4, p90 12.
- At $2.5k a trade, p90 needs $29-42k against $18k cash today. The d17 size table assumes no capital limit.
- Last entries for a flat book by 18:00 ET 3 Nov: slow 06:00 ET 2 Nov, fast 10:00 ET 3 Nov at an 8 h hold.

**B4. FEC agreement as an overlay. Prior 3**
- Mechanism: a weak independent signal can sort trades even when it cannot pay a spread alone.
- Test: for each strict trade, the latest A1 shift in that race within 3 days, |shift| >= 0.1, against the trade's party direction.
- Measured: agree +2.14c (18), disagree +0.81c (18), no data +1.74c (50). Fast agree +3.42c (6). Slow is flatter: +1.50c vs +0.97c.
- Only 2 fast trades disagree, so the spread is mostly slow-rule noise plus a few fast winners. Covers about 40% of trades.

**B5. Mirror leg as second tranche. Prior 3**
- Mechanism: in a two-leg race, buying NO on the other leg is nearly the same bet on a different book, so it splits impact.
- Test: same signal, entered and exited on the mirror leg, using the 0.02c per 1,000 shares cost.
- Measured: mirror +1.23c fast, +1.02c slow. Half on each +1.56c, +1.22c. Corr of the two nets 0.88 to 0.89.
- Break-even size is about 16k shares (fast) and 10k (slow). Choosing the leg by remaining lag adds only 0.0-0.1c.
- Worth it only for large signals, which B3 says cash will rarely allow.

**B6. Pull MM quotes after a both-venue move. Prior 3**
- Mechanism: the same lag that pays us as a taker costs us as a maker. The current gate needs 3 ticks against the mid, so smaller moves still get through.
- Test: `fills` with purpose `mm`, by whether the fill was against a both-venue move in the prior 2 h. Quantity-weighted markout from fill price.
- Measured: against 5,041 shares, -2.09c at 4 h, -$105 in 9 days. No signal: 85,735 shares, +0.05c, +$39.
- Real, but worth perhaps $35 before the MM winds down.

**B7. Wind-down timing and set legging. Prior 2**
- Mechanism: a sale already pays the spread, so delaying a leg whose signal points up costs nothing extra. Selling a set's losing leg first is free lag exposure.
- Measured: mid moves +1.28c in 8 h after a 2c signal, active 3.4% of leg-time, on only $2.3k of legs. Legging every held set once on all 44 matching trades: $147 for the whole history.
- Too small, and it adds set-leg risk.

**B8. Post passively, then cross. Prior 1**
- Mechanism: resting at our touch saves the half spread if the market comes to us.
- Test: rest from the fill bin. Filled on a print through our price (strict) or at it (loose), else cross at the end of the wait.
- Measured, 5 min wait: loose fills 30% fast and 23% slow, +0.09c and +0.08c. Strict -0.03c and -0.02c. Waiting 60 min costs up to 0.8c. 83% of entries are already one tick wide.
- Dead.

## (c) What can break it in the last four days

**C1. The lag is shortening. Prior 4**
- Mechanism: the market maker or other entrants may be reacting sooner as attention rises.
- Test: both-venue moves, share copied by the trigger and share of the remainder copied in 60 min, by day. Rerun daily.
- Measured, 22-26 Oct vs 27-30 Oct: copied by trigger 36% to 48% (day means, SE 3 points). Remaining move 1.82c to 1.44c. Share of remainder copied in 60 min 0.45 to 0.87 (SE 0.18).
- 4 h net from the next poll is unchanged, +0.85c vs +0.88c, so the edge has not gone yet.
- Fast rule hold: early +2.06c at 4 h and +2.63c at 8 h. Late +1.79c at 4 h and +1.57c at 8 h (n 14). Cut the fast hold to 4 h if this repeats on 31 Oct and 1 Nov. The slow rule is unchanged at 24 h (+1.54c, +1.56c).
- Kill test: 4 h net on both-venue moves below +0.3c across 31 Oct to 1 Nov, with 15+ events.

**C2. The wind-down would sell lag positions. Prior 5**
- Mechanism: the wind-down starts at 04:00 UTC today and owns every race it does not exempt. Its `HOLD_PURPOSES` covers only punt and election orders. The catch-up sweep's legs are exempt through `wind_down_exempt`.
- A lag trade under a new purpose would be treated as inventory to sell, and in a set race it could net against held sets.
- Test: a dry-run sim of a lag order in a sell-list race, before any live trade, under the usual precheck gate.
- Measured: 64% of strict trades fall in sell-list races, net +1.32c vs +2.18c elsewhere. None of the 3 live paper signals is in one right now.
- This is a code path read, not an observed failure. Check it in the sim.

**C3. Trend reversal. Prior 3**
- Mechanism: D fair rose about 3.2c on the national factor from 27 to 30 Oct. A late R swing tests whether the rule depends on that.
- Test: net by trade direction against the prior 24 h national drift, and by the national move over the hold.
- Measured: fast against drift +1.83c (12), with drift +2.30c (15). Slow against drift +0.94c (9), with +1.59c (34). Slow with the national move against it over the hold: -0.05c (10).
- Net of the national part: fast +1.73c, slow +1.11c. The fast rule looks robust. The slow rule is more exposed, so stop new slow entries if the 24 h national move turns R by 1c+.

**C4. Weekend liquidity. Prior 3**
- Mechanism: most catch-up comes with prints. If fewer people trade, SIG follows more slowly.
- Measured: `d6` 4 h copy 61% at weekends vs 76% on weekdays. Tonight's events: 2.14c vs 3.10c (9 weekend events). Notional 4-5M on 24-25 Oct vs 5-11M on weekdays.
- Fast rule: 1.65c of the 2.47c catch-up comes in bins with prints, and those are 25% of bins. Almost all of that is bins where flow agrees.
- Test: rerun C1 on 31 Oct and 1 Nov. Keep holds at their full length over the weekend, and expect fewer signals. Only 2 fast trades happened on the last weekend.

**C5. Final polls. Prior 2**
- Mechanism: poll-driven moves may reverse, which would hurt the part of the P&L that needs the outside to keep going.
- Measured: moves with a race poll in the prior 24 h keep +1.04c of outside move at 4 h (27) vs +0.98c without (92). Strict fast +2.57c (7) vs +1.67c. Slow +1.23c (11) vs +1.56c.
- 2024 Polymarket Senate, Governor and House, moves of 1.5c+ in 30 min, 4 h later: T-1 to T-4 -0.84c (SE 0.41), T-9 to T-18 -0.84c (SE 0.25). That is single-venue and thin, so the level means little, but it shows no change into the election.
- No break found.

**C6. Clock change on 1 Nov. Prior 1**
- Mechanism: anything keyed to a fixed UTC hour moves by an hour against ET.
- Checked: both rules use rolling bins, so they do not depend on the clock. Round 1 found no hour-of-day effect. `wind_down.py` uses `America/New_York`, so 18:00 ET 3 Nov is correctly 23:00 UTC.
- Two literals to note. `EXIT_DEADLINE` 04:00Z is labelled 00:00 ET but will be 23:00 ET on 2 Nov (one hour early, harmless). `stop_quoting_at` 12:00Z is 07:00 EST, and it takes precedence over the wind-down's 12:00 ET MM flat time, so check that 07:00 is intended.

## Order to test

1. C2 before any lag trade goes live.
2. C1 and C3 daily from 31 Oct, using the kill rules above.
3. B3 replay, then freeze B1 and B2 as paper-only filters beside `paper.py`. There are no new paper entries yet (6 rows, all already open at the start).
4. Refresh A1 and B4 after each 10:15 UTC FEC update.
