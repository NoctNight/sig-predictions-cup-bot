# Flow patterns: is some taker flow easy money for our market maker? (24 Oct 2026)

**Verdict: drop.** No taker flow type on SIG loses to the passive side by more than the spread, in either half of the data.
The price moves *toward* the taker after every type of trade. The passive side earns +0.2 to +0.7 tick/share after 30 min,
but that is spread capture we already get at the touch. The only way to get more of it is to step ahead of SIG's market maker
(one tick inside), and that loses about −0.4 tick/share. No flow type arrives predictably enough to pre-place a quote for it.
No change to the bot.

Script `research_notes/flow_patterns_study.py` (read-only; run with `uv run --with matplotlib python ...`), outputs
`data/research_flow_patterns/{results.json,rows.json,run.log}`, figure `research_notes/figures/flow_patterns.png`.

## Data and method
* `tape`: 83,975 prints (substantively 21 Oct 20:00 to 23 Oct 22:50 UTC; a sample of venue trades, some hours thin). Our own
  fills were matched by exchange, price, |qty| and ±3 s, and 2,402 were removed. That leaves 46.9k public prints that have a book
  snapshot ≤ 90 s before the trade. Snapshots come from `quotes` (30 s, 136 markets), SIG rows of `venue_quotes` and `venue_fair`.
* Taker side = `aggressor` (classified against our last snapshot); when that is missing, price vs mid.
* **Half-spread paid** = side × (price − pre-trade mid). **Taker's gain** at h = side × (mid(t+h) − pre-trade mid).
  **Passive keep** = half-spread − gain = side × (price − mid(t+h)). The keep does not depend on how stale the pre-trade
  snapshot is. A fresh-mid (≤ 35 s) subset gives the same answer (keep +0.28 / +0.33).
* Split at the median print: training = before 23 Oct 02:08 UTC, test = after (0.86 days). T-stats are clustered by market
  and by hour, and the smaller |t| is kept. Roughly 31 types × 4 horizons × 2 measures ≈ 250 tests, so |t| > 3 is required
  in both halves.
* Types: size (1-2, 3-99, 100/120/150/200/300/1000 exactly, other 100-1499, 1500+), bursts (same size in ≥ 3 markets within
  5 s), time of day (6-h ET slots), market type (House seat / Senate / Governor / chamber), price band (cheaper side < 15c,
  15-40c, 40-60c), buys underdog vs favourite.

## Results (ticks per share; train → test)
| flow | n | half-spread | taker gain 30 min | passive keep 30 min (t) | keep if 1 tick inside (test) |
|---|---|---|---|---|---|
| all public | 46.9k | 0.78 → 0.78 | +0.52 → +0.40 | +0.23 (3.8) → +0.38 (7.8) | −0.38 |
| 1-2 sh | 4.5k | 0.57 → 0.51 | +0.20 → +0.24 | +0.34 (6.8) → +0.27 (2.7) | −0.67 |
| 1-2 sh bursts | 1.7k | 0.55 → 0.54 | +0.13 → +0.14 | +0.40 (9.1) → +0.40 (9.3) | −0.51 |
| 1000 sh | 2.1k | 1.10 → 1.07 | +0.87 → +0.60 | +0.21 (1.6) → +0.47 (4.0) | −0.41 |
| 1500+ sh | 2.8k | 1.70 → 1.65 | +1.18 → +0.93 | +0.33 (1.0) → +0.72 (3.4) | −0.25 |
| longshot < 15c | 22.9k | 0.86 → 0.85 | +0.52 → +0.32 | +0.28 (2.0) → +0.54 (10.9) | −0.32 |
| chamber | 2.3k | 0.66 → 0.62 | +0.36 → +0.03 | +0.33 (3.3) → +0.59 (3.5) | −0.29 |
| 06-12 ET | 13.1k | 0.73 → 0.86 | +0.58 → +0.29 | +0.16 (1.8) → +0.56 (8.7) | −0.21 |
| **our MM fills** | 1.25k | | | +0.28 (3.3) → +0.36 (2.9) | |

* **No type has a negative taker gain at any horizon in either half** (1 min to 2 h; the smallest is chamber test, +0.03).
  So the flow is not "inefficient beyond the spread". Takers pay ~0.8 tick and win back ~0.4 of it from the move.
* **Differences between types don't replicate.** Excess keep over all flow reaches |t| > 3 in only one half for any type:
  longshot is +3.1 in test vs +0.3 in training; 1-2 sh bursts +3.9 in training vs +0.5 in test; 150 sh −4.6 in test only.
  Rank correlation of keep, training vs test, is 0.33 across all 31 types. For the types the bot can see before a trade
  (market type, band, time of day) it is 0.12.
* Our MM fills keep as much as public flow does (+0.36 test). We are not being picked off relative to the DMM; we just
  get few fills, because the DMM is first in the queue with 2,000-2,500 shares.

## Predictable in advance?
* Inter-arrival times of a type in one market have a coefficient of variation of 1.3-3.8. That means bursty, not periodic
  (a clock would be ≪ 1). The share landing on the same second of the minute as the previous print is 4-12%, against ~8% by
  chance. Exceptions: 200 sh 19% and chamber 23%, which are weak hints of a round-robin sweeper.
* The next same-type print in a market is on the same side 88% of the time for 1-2 sh, 74% for 3-99, 70% for 1000, 54% for 100.
* After a requote (22 Oct full-feed capture, 25 min): 20% of 1-2 sh prints come within 2 s after a top-of-book change,
  against a 0.9% base. The rates are 7.5% for 3-99, 3-4% for 100+, and 0% for 1500+. A one-share pinger follows requotes, but at
  300-share caps it is worth ~3,400 shares/day × 0.27 tick ≈ 5 S/day.

## How much more could the bot earn? (panel C, test half, recorded tape)
* Ceiling, if we were first in the queue for up to 300 shares of every public print: 4.26M shares/day × 0.38 tick ≈ 8.1k S/day.
  This is the DMM's income, not ours. Joining behind it at the same price adds fills only from trades larger than its queue
  (1500+ sh), and those we already get.
* Feasible, quoting one tick inside the DMM wherever the spread is 2+ ticks: 0.84M shares/day × −0.38 tick ≈ **−1.6k S/day**.
  It is negative for every type except 1000-sh bursts (+0.18, n 358, t < 2, not timeable). This matches
  `bot_exploits.md` (our improving level-1 fills earned −0.01 tick) and `hft_tactics_2.md`.
* Cash objective: nothing to add. The current MM keeps +0.3-0.4 tick on its own fills and stays flat by 12:00 ET 3 Nov
  as planned.

## Verdicts
| flow type | verdict | why |
|---|---|---|
| all size classes (1-2 … 1500+), fixed-size bots, bursts | drop | taker gain > 0, keep < half-spread, inside quoting −0.2 to −0.7 tick |
| 1-2 sh requote pinger | drop | predictable (20% within 2 s of a requote, 88% same side) but ~5 S/day |
| longshot / overnight (00-12 ET) / chamber markets | paper | test keep +0.51-0.59 but training +0.16-0.33 and rank stability 0.12. Re-check our own MM fills' keep by band and slot on 26 Oct; act only if \|t\| > 3 in a fresh half (and then only as a size tilt at the touch, not price improvement) |
| 1000-sh bursts inside the DMM | drop | +0.18 tick on n 358, t < 2, unpredictable timing |

## Caveats
Mids come from 30-s to 90-s snapshots, so the 1-min markouts are noisy. The data covers only ~2.5 days, and the all-flow
keep rose from 0.23 to 0.38 between halves (a market-wide shift, not a type effect). `aggressor` is classified against our
last snapshot and could be wrong on stale books. The tape is a sample, so S/day totals are lower bounds (both the
ceiling and the loss scale together).

## Follow-up: tighter quotes, or more depth? (24 Oct)
Passive keep by the spread at the time of the trade, training → test, with t clustered by market. "Inside" assumes the same
taker would have hit us one tick better, which is an upper bound.
| spread | share of trades | keep at the touch, 30 min | keep one tick inside, 30 min |
|---|---|---|---|
| 1 tick | 82% | +0.18 (3.2) → +0.33 (7.6) | no room |
| 2 ticks | 16% | +0.46 → +0.56 | −0.54 (−7.6) → −0.44 (−11.1): we would sit at the mid |
| 3 ticks | 1.2% | +0.54 → +0.88 | −0.46 → −0.12 (t < 2) |
| 4+ ticks | 0.5% | +1.62 → +1.70 | +0.62 (1.5) → +0.70 (2.4); ~140 trades/day in the recorded tape |
Getting swept is not the danger. Keep at 30 min by taker size: < 100 sh +0.27 → +0.35, 100-299 +0.21 → +0.34,
300-1499 +0.20 → +0.41, 1500+ +0.33 → +0.72 per trade (share-weighted +1.08 → +1.70). Big sweeps walk the book about
1.7 ticks and only ~0.9 of that holds after 30 min, so liquidity one tick behind the touch keeps +1.7 tick (t 8.1, test).
Conclusion: do not tighten at 1-3 tick spreads. Inside quoting at 4+ tick spreads is marginal (= `inside_quoter` proposal).
The better lever is depth behind the touch for sweeps; that needs a queue-aware paper test before any size change.

## Paper test: a deeper sweep-catching order (24 Oct) — drop
Script `research_notes/flow_patterns_deep_study.py`; figure `research_notes/figures/flow_patterns_deep.png`; output
`data/research_flow_patterns/deep.json`. Setup: a virtual 300-share order rests d = 1, 2, 3 ticks behind the touch, on both
sides, in the 136 markets the bot quotes. It is repriced from the bot's own 30-s snapshots, and every reprice sends it to the
back of the queue. It fills when a print lands beyond its price (queue-free), or when the volume printed at its price exceeds
the depth ahead. Depth ahead is calibrated from the two 22 Oct full-book recordings, assuming no cancels ahead.
* Depth already waiting ahead (median): **5.5k / 14k / 20k shares** at d = 1 / 2 / 3. Walls from the resting-ladder bots and
  SIG's market maker absorb almost every sweep.
* Fills: d = 1: 30/day in training, 9/day in test (8.7k shares/day pooled); d = 2: ~3.9/day (1.2k shares); d = 3: ~2.5/day
  (0.7k shares). Test fills are too few to judge profit per half, so profit uses both halves pooled.
* Profit per share at 30 min / 2 h, marked at mid: d = 1 +0.17 (t 0.8) / +0.59 (t 3.6), and −0.37 at 30 min after paying the
  spread to get flat; d = 2 +0.90 / +1.30 (n 5, 3 markets); d = 3 +1.17 / +1.83 (n 3).
* Worth: d = 1 −10 S/day after the exit spread; d = 2 and d = 3 +1.5 to +3 S/day (maybe ×2 because the tape is a sample).
  The deep edge per share is real, but the queue belongs to the walls that were there first. Our level 2 (about d = 1) is
  roughly break-even, which matches its real fills: 9.6/day, +0.59 tick at 30 min, t 1.9.
* Verdict: drop. No ladder change. A sticky deep order that keeps its queue place for hours can't be tested without
  longer depth history, and even at full priority the volume is ~1k shares/day.
