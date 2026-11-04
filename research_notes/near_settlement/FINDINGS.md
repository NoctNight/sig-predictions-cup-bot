# Do the 90-92c favourites catch up with fair before the result?

Built 1 Nov 2026 (T-3). Plot: `research_notes/figures/near_settlement.png`. Tables from the last full run: `data/research_near_settlement/final_run/`.
T is 18:00 ET on election day, before any result, the same clock as the punt. T-3 is now. A sale at 18:00 ET on 2 Nov is T-1, noon ET on 3 Nov is T-6h.

## Bottom line

**The prior is half right, and the half that is right does not help this trade.**

- Volatility does rise, but only modestly and mainly in the last day: favourites swing 1.2x in the last 2 days, 1.5x in the last day alone. Result night is far bigger than either.
- Convergence on SIG does not follow. SIG's gap to fair has not closed faster as 3 Nov approaches, and on SIG volatility and closure are unrelated (correlation 0.015).
- The prior describes Fed and award markets well. In US elections, favourites that went on to win had not moved towards 1.00 by T-1.

| Exit | Expected per share, central | Whole range | On about 23,200 shares ($21k budget) |
|---|---|---|---|
| Sold 2 Nov (18:00 ET) | -0.2c to +0.6c | -0.5c to +1.5c | -$50 to +$135 (range -$115 to +$360) |
| Sold 3 Nov noon ET | -0.1c to +0.9c | -0.4c to +2.0c | -$35 to +$205 (range -$105 to +$465) |
| Held to the result | +6.6c | +4.6c to +8.9c | +$1,520 (range +$1,060 to +$2,060), no cash for the punt |

What SIG actually paid on gaps of 6c+ since 24 Oct: **+0.15c at 48 h, +0.13c at 66 h**, which sits inside the central range.

So money that must be in the punt earns about nothing from this trade. The whole 6.6c arrives at the result, after the punt.

## What you would need to believe for 2 Nov to beat holding

A sale happens at SIG's bid, below fair, so faster closure alone can never do it: even a gap that closed completely leaves the sale half a spread under fair. One of these has to be true:

1. **The outside fair is badly wrong.** The true win rate on these legs would have to be under about 91%, against 97% outside. Three days out, 128 past US races with favourites at 80-98c produced no losers.
2. **Punt cash is worth more.** A dollar at the 18:00 ET punt would have to be worth at least **1.07 dollars after settlement**. This is the real question and it is Sizhe's call, because it turns on the odds available and the prize, not on this study.

Between the two sale dates there is little to choose: 3 Nov noon is 0.1-0.3c better on the sums, and level on what SIG actually paid. Both are inside the noise. How the sale is done matters more: the speed-up adds about 0.3c, while resting the exit instead of hitting the bid would save about 0.4c.

## Q1. Volatility by days to settlement

**Hypothesis.** News keeps arriving, so prices swing more as settlement nears. If news arrived steadily, volatility on the probit scale would grow like one over the square root of days left, a slope of -0.5. If it all came on the night, the slope would be 0.

**Test.** Hourly prices T-14 to T+1 on 136 races, 212 legs, one leg per race per venue. Realised volatility from 4-hour moves, also checked at 1 hour and with jumps removed. 855 bad prints of 213,979 were removed (a 1-3 hour excursion that comes straight back, for example 2c to 51c and back). Errors are bootstrapped over races.

| Days to T | 10 | 9 | 8 | 7 | 6 | 5 | 4 | 3 | 2 | 1 | Result night (12 h) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Favourites 80-95c, cents per day | 1.6 | 1.6 | 1.6 | 4.0 | 1.9 | 1.8 | 1.9 | 3.1 | 2.0 | 3.4 | 8.9 |

| Last 2 days against T-10..T-6 | Ratio | Band |
|---|---|---|
| US elections, favourites 80-95c | 1.20 (last day alone 1.46) | 0.98-1.73 |
| US elections, close races 50-65c | 1.52 | 1.28-1.87 |
| Foreign elections, favourites | 1.92 | 1.63-2.21 |
| Fed decisions, favourites | 0.62 | 0.55-0.70 |
| Award shows, favourites | 0.59 | 0.50-0.70 |

Slope for election favourites: -0.31 (-0.39 to -0.25), between the two theories. Night by night on daily closes the ratio is 1.14 in 2022, 1.17-1.23 in 2024, 1.43-1.55 in 2025.

The T-7 reading is a few legs (the median is 1.1c), and the T-3 bump is the Selzer poll weekend in 2024. Neither is a rule.

**Decision.** Volatility rises in US elections, by about 20%. It is not a law of settlement: Fed and awards go the other way.

## Q2. Do favourites drift towards their outcome faster?

**Test.** Favourites at the band on T-10, followed to the result. Share of the distance to 1.00 covered:

| | n (races) | T-5 | T-3 | T-2 | T-1 | T-6h |
|---|---|---|---|---|---|---|
| US elections, 88-93c | 15 (12) | +2% | +14% | +3% | +6% | +17% |
| US elections, 85-95c | 45 (34) | +3% | +6% | -5% | -5% | +3% |
| Other events, 85-95c | 60 (44) | +19% | +25% | +26% | +40% | +35% |

In other events by T-1: Fed +50%, awards +31%, foreign elections +21%.

**Winners against losers: there are no losers to split.** No favourite at 85-95c in any group lost, so every row above is winners only. I checked this because it looks like a bug. It is not: an independent count from the calibration file gives 0 lost in 128 races at 80-98.5c at T-3, and upsets start below 80c.

**Against what the price implied.** If 90c were honest, winners alone would rise about 0.2c by T-1 just from the spread of outcomes. Election winners moved -0.4c on the wider band (-1.1 to +0.2) and +0.6c on the narrow one (-0.2 to +1.3). So the drift is no faster than the price implied.

**Not a rule either.** The election-day rise is one night: T-1 to T-6h was +1.0c in 2024, -0.5c in 2022, -0.5c in 2025. The move at T-3 to T-2 is -1.7c a day, the Selzer weekend.

**Decision.** In US elections, even eventual winners do not climb before the result. Do not plan on a rise in fair.

## Q3. When two venues disagree, does the gap close faster?

**Test.** The closest analogue: Polymarket against Kalshi on the same contract, 28-29 skewed pairs a day, 6-hour average gaps, so a noisy print cannot pose as closure. The cheap venue plays SIG.

| 24-hour closure | T-14 to T-3 | T-3 to T-1 | Speed-up |
|---|---|---|---|
| Gaps 2c+ | 24% | 34% | 1.44x (band 1.05-1.77, 95% 0.77-2.26) |
| Gaps 1c+ | 26% | 28% | 1.09x |
| Without presidential states, 2c+ | 17% | 28% | 1.6x, all from the dear venue falling |
| Kalshi the cheap venue, 2c+ | 21% | 24% | 1.15x |
| Kalshi the cheap venue, 1c+ | 22% | 15% | 0.67x |
| Gaps already open 48 h+ (closest to SIG) | 19% | 17% | 0.89x, only 8 trades in 4 races |
| Other events, 1c+ | 28% | 45% | 1.6x |

Three things cut against the headline:

- **The gap itself widens into the result.** With nothing selected, the average gap in election contracts goes 1.45c (T-14 to T-11), 1.81c (T-10 to T-4), 2.27c (T-3 to T-1). New disagreement opens faster than the old closes. In other events it falls: 1.25c, 1.21c, 0.97c.
- **Most late closure is the dear venue falling.** In T-3 to T-1 the cheap venue rose 0.43c a day and the dear one fell 0.81c. On SIG the outside fair will not fall to meet us, so only the first part pays.
- **Gaps open at T-3 in 2024 were news shocks** (Selzer), and those close fast. SIG's gaps are weeks old. That is why the 48 h+ row matters most, and it is the thinnest.

**Volatility against convergence, tested directly.** Gaps sorted by how much the market moved over the same 24 hours:

| 24-hour closure | Quiet third | Middle | Busy third |
|---|---|---|---|
| Polymarket-Kalshi | 11% | 34% | 35% |
| SIG, sorted on the outside fair's swing | 8% | 7% | 10% |
| SIG, sorted on SIG's own swing | 7% | 8% | 10% |

On real-money venues movement and closure go together. My reading, not something this study proves: arbitrage money acts when prices move, and its cost falls near settlement. On SIG, capital near the end goes to the punt, so that force may run the other way.

**Decision.** Use the 1.44x as the speed-up, as asked, and treat it as generous for this trade.

## Q4. Is this specific to elections?

Yes, in the direction that hurts: elections are the case where the prior fails. Fed and awards show falling volatility, early drift and narrowing gaps, because the answer leaks out beforehand. US elections show rising volatility, no drift and widening gaps, because the answer arrives on the night.

Election markets all settle together, so race-level bands are too narrow for anything national. I show nights apart wherever the data allows. 2024 dominates hourly data, and nearly all Polymarket-Kalshi evidence is one night, so read the speed-up as one observation with many legs, not as 23 independent ones.

## Q5. SIG itself

**There is no history for the last days.** `venue_fair` runs 21 Oct to 31 Oct, which is T-13 to T-3. Nothing here says what SIG does inside T-3, and I do not extrapolate past it.

**Test.** Same entries as `favourite_gap_exit_study.py`: sides at 88-93c, 4c+ under fair, entered one bar after the signal, gap measured on the mid.

| Entries | 21-23 Oct | 24-25 | 26-27 | 28-29 | 30-31 |
|---|---|---|---|---|---|
| Gap closed in 24 h | 23% | 11% | 14% | 2% | 21% |
| n (races) | 88 (42) | 50 (23) | 54 (25) | 68 (31) | 32 (25) |

- **Trend by entry day:** -1.4 points a day ± 1.5 overall, +0.4 ± 2.2 from 24 Oct. So flat, with no sign of a speed-up. A race-clustered version looks more negative (t -3.5), but it treats same-day entries as independent, so I do not use it.
- **The 21% at the end is one day of entries (30 Oct).** The 12-hour figure for the same window is 6%.
- **With nothing selected, it looks no better.** All favourites with fair 93-98.5c: average gap 3.9c on 22 Oct, 3.0c on 26-27 Oct, 3.6-3.7c on 30-31 Oct. Fixed legs show the same. SIG's spread widened from 0.6c to 0.9c. The line (SIG's distance from 50c as a share of fair's) stays near 0.93.
- **Our own buying does not explain any of it:** the sleeve's fills are about 5,100 shares in total.

**Decision.** No speed-up on SIG through T-3. A rate that rises inside the last 3 days cannot be ruled out, because it has not happened yet.

## The exit sums

Inputs from the night of 31 Oct: 10 live sides in 9 races, ask 90.6c, mid 90.1c, fair 97.2c, gap to mid 7.1c. Cost to round trip 0.93c (half today's 1.1c spread, half the 48-hour average of 0.76c, because picking wide gaps also picks wide spreads at that moment), plus 0.19c to sell 1,200 shares a leg into the book. A sale breaks even if **16% of the gap closes**. Fair is assumed not to drift (Q2).

| Scenario | Gap closed by 2 Nov | by 3 Nov noon | Sold 2 Nov | Sold 3 Nov noon |
|---|---|---|---|---|
| SIG's rate since 24 Oct, no speed-up | 9% | 10% | -0.50c | -0.44c |
| The same with history's 1.44x | 13% | 14% | -0.22c | -0.14c |
| SIG's rate since 21 Oct with 1.44x | 24% | 28% | +0.58c | +0.88c |
| Same with the top of the speed-up, 2.26x | 37% | 44% | +1.54c | +2.02c |
| SIG closes like real-money venues, 30% a day | 51% | 63% | +2.51c | +3.33c |

The last row is the prior taken fully at its word. Nothing in SIG's data supports it, so I leave it out of the range, but it still falls far short of holding.

**Holding:** +6.6c if the outside fair is right, +4.6c if it is 2c too high, +8.9c if favourites keep winning at the past rate (99.5c). Win or lose, this is lumpy: expect 0.25 upsets across the 9 races, about a 77% chance of none if races were independent, and they are not. One upset costs about $2,000 at the leg cap. Uncalled races also keep cash locked past the close.

Funding cost is left out. The Rhode Island sets were going to cash by 3 Nov anyway, so selling them now costs little unless they would otherwise be held to settlement, when it is up to 9c a set.

## What this means for the trade

These are decisions for Sizhe; this study changed nothing in the bot.

- **Buying with money meant for the punt earns about nothing.** Buying only pays on money that can stay in until the result.
- **If the money must reach the punt,** do not rush to sell on 2 Nov. Work the exit passively from the evening of 2 Nov into 3 Nov, and cross the spread only near the deadline.
- **The decision that matters is the 1.07 in the section above,** not the speed of convergence.

## How sure I am

- **High:** SIG had not sped up through T-3, and on expected value holding beats any sale. Both hold under every cut I tried.
- **Medium:** volatility rises about 20% in elections and result night dwarfs it. All nights agree on the sign, but the size is uncertain.
- **Medium-low:** the 1.44x speed-up, and anything about SIG inside T-3. It is mostly one night, and SIG has no history there.

Limits I could not remove:

- **2024 dominates.** Kalshi's state markets only opened in October 2024, so some early closure is young markets maturing. Kalshi 2022 does not exist. 2022 Polymarket is daily AMM trade prices.
- **Prices.** Polymarket hourly history has no bid or ask. Thin Kalshi legs have a fresh print in about 47% of hours.
- **The 100% win rate.** 128 races sounds like a lot, but they fall on a few correlated nights. A 95% limit on the loss rate is about 4% if they were independent, and well above that in truth. In the comparison set, four contests were dropped because their final price contradicted the resolved outcome, so its win rate is not clean evidence.
- **SIG bands** run over races, so a shock shared across a day is not in them. The spread across entry windows (2% to 23%) is the better guide.
- **Comparison set details.** Awards jump 2-4 hours after the ceremony starts, and Kalshi's Fed series stop at the statement. Inside T-3 it has few 2c+ gaps, which is why I compare it at 1c+.

## Files

Scripts in `research_notes/near_settlement/`: `net.py`, `fetch_elections.py`, `others_*.py`, `build_panel.py`, `study_q12.py`, `study_q3.py`, `q1_daily.py`, `q2_nights.py`, `sig_closure.py`, `sig_panel.py`, `vol_link.py`, `live_book.py`, `exit_ev.py`, `plot.py`.
Data in `data/research_near_settlement/`; comparison markets in `others/` with their own README. Read-only throughout: no calls to SIG, no bot or config changes, no commits.
