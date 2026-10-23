# Outside move, then stable: sweep SIG's stale touch (23 Oct 2026)

Owner's idea: "implement a sweep for when Polymarket moves then stabilises". When Polymarket and Kalshi move a few points
and then hold steady, and SIG has not followed, take SIG's touch in the direction of the outside move. Exit when SIG
catches up, or at a time stop.

Refinement (owner, via coordinator):
* Sweep only when the outside move is big enough: scan the move threshold M.
* Minimise the wait. Find the optimal stabilisation wait of x seconds, trading noise filtering against the DMM
  catching up, with each move labelled REAL or NOISE.
* State the data resolution achieved and the live feed the bot would need.
* Plot three curves against x on the same events: (A) P(real | stable for x), (B) the share of SIG's stale gap others
  have already taken, (C) net EV. This is section 10.6 and `research_notes/figures/outside_sweep_x.png`.

Read-only study:
* Nothing under sigbot/, config.json, params/, tests/ or deploy/ was touched. No orders, and the bot was not touched.
* **Venue (SIG) calls: 0** of the 20 allowed.
* **Public outside calls: about 2,000.** These were Polymarket CLOB `prices-history` (fidelity 1), the gamma market
  lookup, data-api trades, and Kalshi 1-min candlesticks, for 114 races from 8 Oct to 23 Oct 21:00 UTC. They are cached
  in `data/research_outside_sweep/raw/`.
* **Reproduce:**
  * `python3 research_notes/outside_stable_sweep_study.py` runs the minute-scale grid (sections 1-9; about 2 min).
  * `... fetch` re-downloads the fine outside data.
  * `python3 -I ... xstudy` runs the seconds-scale x-study and the A/B/C curves (section 10; about 1 min).
  * The figure needs matplotlib, which the system Python lacks; it was drawn with matplotlib 3.11.2 in a scratch venv.
* **Outputs:** `results.json`, `out.md`, `xstudy.json` and `xstudy_out.md` in `data/research_outside_sweep/`.

Conventions:
* One tick is 0.005. P&L is in ticks per share, net of the touch spread and 0.05 tick of slippage per taker side.
  S means SUSQies.
* t-statistics are clustered by race.
* "ex-top-3" means the same sample without the 3 races that contribute the most P&L.

## Verdict

**1. The answer on x: wait zero seconds** (95% range 0-120 s, section 10.6). On out-of-sample data, every stability
wait lowers the expected value.
* **There is almost no noise to filter.** Requiring both Polymarket and Kalshi to move already does the filtering:
  83-86% of the moves are REAL at x = 0. Waiting raises P(real) only to 0.83-0.88 even at x = 600 s.
* **No other bot beats us to it.** On the SIG tape, other traders took the stale side in the direction of the outside
  move in only 1-3 of 22 covered events. Only the DMM's slow requotes erode the gap.
* **The DMM is slow, but waiting still costs.** At entry, 100% of SIG's gap is left for x up to 30 s, 98-99% at
  60-120 s, 88% at 300 s and 83% at 600 s.
* **Net P&L falls as x grows.** For M 2 pts, 12-h stop:

  | x | 0-30 s | 60 s | 120 s | 300 s | 600 s |
  |---|---|---|---|---|---|
  | ticks/share | +1.46 | +1.44 | +1.25 | +0.85 | +0.29 |

* **x picked on in-sample data:** 0 in 5 of the 6 (M, H) cells that have data. The other two cells pick 120 s and
  300 s on in-sample differences under 0.4 S/day, and those choices fail out of sample.
* **The minute-scale filter of the original brief** (stable 15-30 min, sections 1-9) also fails. No variant reaches
  t > 2, and it is worse than no filter in 33 of 36 pairs.

**2. The fast follow at x = 0 just meets the strict bar.** It needs fine outside data, which the minute-scale run on
5-min Polymarket bars did not have (sections 3-4).
* **Best cell:** M 2 pts, 12-h stop, entry within 2 s to 60 s.
  * Out of sample: **+1.46 tick/share (t 2.3, n 25)**, and **+0.46 without the top 3 races** (VA-05, FL-07, MN-01).
  * By period: OOS1 (17-21 Oct, candles) +1.22 (t 2.0); OOS2 (live quotes) +1.76 (t 1.3).
* **Neighbouring cells:**
  * M 1.5 and M 2 with 4-h or 12-h stops all make +0.69 to +1.46 (t 2.0-2.5), and +0.13 to +0.47 without the top 3.
  * M 3 makes +2.65 (t 2.5, n 8) and M 5 +4.9 (n 2), both too few to judge.
* **Evidence against:**
  * In-sample (8-16 Oct) it is flat: +0.03 (t 0.0).
  * Without the top 3 races, t is at most 1.4.
  * Over 120 variants were tried in this note.
* **Taken together, this is a marginal pass, not a robust edge.** Per the rule, section 11 gives an implementation
  spec. My recommendation is a capped trial at the existing 600-notional cap with a hard kill. That is a bounded bet of
  roughly ±100 S, worth +15-40 S/day if real. Of that, +16 S/day is on R-adding signals, the only ones the partisan cap
  allows while netD is at or above 4.8k.

**3. Production feed.** The current direction poll is enough.
* It runs every 60 s by day and 120 s by night (measured median 74 s).
* A 60-s detection delay costs about 0.02 tick and a 120-s delay about 0.2 tick, because the DMM does not move in the
  first 2 minutes.
* A Polymarket websocket would cut the minimum x to about 2-10 s, a gain of 0.05 tick at most. That is not worth
  building before 3 Nov.

## Minute-scale study (original brief): details

The stabilisation sweep fails. Do not take it live.
* **Out of sample, no stable variant meets the bar.** None of the 36 variants has t > 2 on the pooled out-of-sample set.
  The IS-chosen variant makes +0.69 tick/share (t 1.1, n 19), and -0.31 without its top 3 races.
* **Adding the stabilisation filter makes the rule worse.** Compare each stable variant with the same rule without
  the filter: it does worse out of sample in 33 of 36 pairs, by 0.86 tick/share on average.
* **Why it hurts:** SIG's partial catch-up happens in the first 15-30 minutes after the outside move. The filter waits
  out exactly that window.

The no-filter comparator on 5-min Polymarket bars meets the stated bar in 4 of 18 variants, but loses in-sample in all
18. Section 10 re-tests it on 1-min outside data. There it is in-sample flat, not negative, and positive in both
out-of-sample periods.

## 1. Signal definition

The rule is checked every grid step: 1 min on live quotes, 5 min on candles. It is evaluated on each race's D leg. In B,
the R leg is used where a race has no D leg, and section 6 replicates B on R legs.

| step | definition | values scanned |
|---|---|---|
| consensus | F = mean(Polymarket, Kalshi). Both must be present (no more than 7 min stale in B, 30 min in A) and agree within 4 pts | fixed |
| move | over the move window [t-S-W, t-S], abs(dF) >= M pts. **Polymarket and Kalshi each** moved at least 0.5 M in the same direction | M in {1.5, 2, 3}; W = 60 min fixed |
| stable | over [t-S, t], the range (max - min) of F is under R pts | S in {15, 30} min; R = 1 pt (0.5 as a check) |
| SIG lag | SIG's mid moved less than 1/3 of the outside move over [t-S-W, t]. The gap, (F - SIG mid) signed with the move, is at least G ticks | G in {2, 4} |
| entry | the first check at which all of the above hold. **Latency 1 min:** take the touch at t + 1 min (ask to buy, bid to sell). Spread at most 2 ticks; mid in [0.03, 0.97] | fixed (delays of 5, 15 and 30 min as checks) |
| exit | the first observation at which SIG's mid has closed 70% or more of the entry gap. Otherwise the time stop H. Taker at the opposite touch (T/T). If there is no quote, the first one after a data hole, at most 2 h late | H in {1, 4, 12} h |
| spacing | one position per race. The next signal must come after the exit and after the move window has rolled past the entry | fixed |
| no-filter comparator | the same rule with S = 0 and no range test, so it enters 1 min after the move is seen | M, G and H as above |

**Costs.**
* B pays the real touch.
* A pays the leg's median live spread (1 tick on liquid legs). Legs with a median spread over 2 ticks are excluded.
* Both pay an extra **0.05 tick per taker side**: realised slippage beyond the half spread on our own taker orders of up
  to 3k shares (`directional_impact.md` section 4).
* At about 900 shares (the 600-notional cap) nothing walks past the touch: the median touch is 5.8k shares.

**Fill assumption.** Entry is a taker order at the displayed touch with size up to the cap. It is assumed to fill in
full, which holds for these touch sizes. The T/mid column assumes a passive exit at the mid. That is an upper bound; the
far touch would fill rarely and is not modelled.

**Grid.**
* The main grid has 54 variants: 36 stable (3 M x 2 S x 2 G x 3 H, with R fixed) and 18 without the filter.
* Checks add 18 more: R = 0.5 (2), the lookback's 30-min window (1), entry delay (9) and the R-leg replication (6).
* **72 variants in total.**
* Selection rule, fixed before looking out of sample: the stable variant with the best in-sample clustered t,
  among those with n >= 15. A pre-registered default (M 2, S 30, G 4, H 4) is also reported.

## 2. Data and split

| set | window (UTC) | SIG price | outside | role |
|---|---|---|---|---|
| A | 8 Oct 00:00 - 21 Oct 21:30, 111 races | 5-min candle close, forward-filled, +- half the leg's median spread | Polymarket 5-min and Kalshi 1-3-min D-leg history (`model_history.json`) | **IS** = before 17 Oct 00:00 (9.0 d, 57% of the days). **OOS1** = 17-21 Oct (4.9 d) |
| B | 21 Oct 23:49 - 23 Oct 20:35, 117 races | live bid/ask (`venue_fair` + `quotes`) | Polymarket and Kalshi per poll (`venue_fair.pm`, `.kalshi`) | **OOS2** (1.9 d, all live quotes 21-23 Oct) |

* The OOS columns pool OOS1 and OOS2.
* B's 12-h outcomes exist only for entries before 23 Oct 08:35.
* The candle method in A was validated against true quotes in `directional_impact.md`.

## 3. Headline results (T/T, ticks/share)

| rule | IS n / mean (t) | OOS1 n / mean (t) | OOS2 n / mean (t) | **OOS pooled n / mean (t)** | **OOS ex-top-3 (t)** | OOS hit | signals/day (OOS) | S/day at the cap (OOS) | ... R-adding only |
|---|---|---|---|---|---|---|---|---|---|
| **stable, IS-best** (M 1.5, S 30, G 2, H 12) | 22 / -0.01 (-0.0) | 11 / -0.28 (-0.6) | 8 / +2.02 (+1.7) | **19 / +0.69 (+1.1)** | **-0.31 (-0.7)** | 0.47 | 3.3 | +12 | +6 (n 10) |
| stable, default (M 2, S 30, G 4, H 4) | 12 / -0.52 (-0.7) | 10 / +0.30 (+0.4) | 2 / +3.90 | 12 / +0.90 (+1.4) | -0.23 (-0.5) | 0.58 | 1.9 | +8 | +2 (n 7) |
| no filter, M 2, G 2, H 4 | 15 / -0.90 (-2.0) | 13 / +0.52 (+0.8) | 12 / +1.32 (+2.8) | 25 / +0.90 (+2.3) | +0.25 (+0.9) | 0.60 | 3.9 | +22 | +4 (n 9) |
| no filter, M 2, G 2, H 12 | 15 / -0.37 (-0.5) | 9 / +0.34 (+0.4) | 11 / +2.63 (+2.6) | 20 / +1.60 (+2.2) | +0.43 (+0.9) | 0.65 | 3.5 | +30 | +7 (n 8) |
| no filter, M 2, G 4, H 4 | 14 / -1.03 (-2.5) | 11 / +0.35 (+0.7) | 11 / +1.63 (+2.4) | 22 / +0.99 (+2.2) | +0.21 (+0.7) | 0.64 | 3.4 | +22 | +5 (n 8) |
| no filter, M 2, G 4, H 12 | 14 / -0.46 (-0.6) | 8 / +0.03 (+0.0) | 10 / +3.10 (+2.8) | 18 / +1.73 (+2.1) | +0.45 (+0.6) | 0.61 | 3.1 | +30 | +6 (n 7) |
| lookback-style (30-min window, M 2, no gap test, H 4) | 11 / -0.74 (-1.8) | 10 / +0.10 (+0.1) | 13 / +2.21 (+3.1) | - | - | - | - | - | - |

Grid totals (full table in section 9):

| set | stable variants with mean > 0 | stable with t > 2 | no-filter variants with mean > 0 | no-filter with t > 2 |
|---|---|---|---|---|
| IS | 2 / 36 | 0 | 0 / 18 | 0 |
| OOS1 | 10 / 36 | 0 | 11 / 18 | 0 |
| OOS2 | 18 / 36 | 4 | 18 / 18 | 9 |
| pooled OOS | 19 / 36 | **0** | 18 / 18 | 6 |

* **1-h holds lose everywhere** with the filter (-0.5 to -1.4). SIG rarely moves within an hour of a confirmed-stable
  outside move.
* **The 70% catch-up exit fires in 0-23% of trades in A and about 60% in B.** In A, "wait for SIG to catch up" mostly
  means waiting for the time stop.
* **Gross mid-to-mid** for the stable IS-best is +1.23 (IS), +0.82 (OOS1) and +3.31 (OOS2). Exiting passively at the
  mid would add about +0.6 tick, but the strict bar is not met even under that assumption. Pooled OOS T/mid is +1.27
  (t 2.0), and without the top 3 races it is +0.24 (t 0.5).

**By day** (stable IS-best, n / mean):

| | | | | | | | |
|---|---|---|---|---|---|---|---|
| IS | 9 Oct 4 / +2.15 | 10 Oct 3 / -0.77 | 11 Oct 2 / +0.90 | 12 Oct 3 / -2.43 | 13 Oct 2 / +0.90 | 14 Oct 5 / -0.70 | 15 Oct 3 / +0.23 |
| OOS1 | 17 Oct 1 / -2.1 | 18 Oct 1 / -0.1 | 19 Oct 4 / +0.15 | 20 Oct 4 / -0.6 | 21 Oct 1 / +0.9 | | |
| OOS2 | 22 Oct 7 / +1.76 | 23 Oct 1 / +3.9 | | | | | |

No filter (M 2, G 2, H 4):

| | | | | | | |
|---|---|---|---|---|---|---|
| IS | 9 Oct 5 / -0.9 | 10 Oct 1 / -1.1 | 11 Oct 3 / -0.1 | 12 Oct 1 / -2.1 | 14 Oct 4 / -1.35 | 15 Oct 1 / -0.1 |
| OOS1 | 17 Oct 2 / +1.9 | 18 Oct 2 / -1.1 | 19 Oct 3 / -0.43 | 20 Oct 2 / -0.1 | 21 Oct 4 / +1.65 | |
| OOS2 | 22 Oct 10 / +1.5 | 23 Oct 2 / +0.4 | | | | |

The positive out-of-sample result rests on 17, 21 and 22 Oct.

## 4. Does "then stabilises" add anything? No: it removes the edge

**Paired comparison** (same M, G and H; pooled OOS T/T mean, with n):

| M | G | H | no filter | S = 15 | S = 30 |
|---|---|---|---|---|---|
| 1.5 | 2 | 1 | +0.05 (47) | -0.78 (38) | -0.80 (30) |
| 1.5 | 2 | 4 | +0.47 (37) | -0.23 (32) | +0.25 (26) |
| 1.5 | 2 | 12 | +0.79 (28) | +0.06 (25) | +0.69 (19) |
| 1.5 | 4 | 4 | +0.53 (30) | +0.54 (25) | +0.30 (20) |
| 1.5 | 4 | 12 | +0.67 (22) | +0.85 (19) | +0.77 (15) |
| 2 | 2 | 4 | +0.90 (25) | +0.46 (18) | +0.63 (15) |
| 2 | 2 | 12 | +1.60 (20) | +0.83 (14) | +0.99 (11) |
| 2 | 4 | 4 | +0.99 (22) | +0.63 (15) | +0.90 (12) |
| 2 | 4 | 12 | +1.73 (18) | +1.23 (12) | +1.57 (9) |
| 3 | 2 | 4 | +1.46 (9) | -0.27 (6) | -0.43 (3) |
| 3 | 4 | 4 | +2.02 (8) | +0.30 (5) | -0.43 (3) |

* Over all 36 pairs, the filter is worse out of sample in 33, by -0.86 tick/share on average.
* In-sample it is +0.12 better on average, but both versions lose there.

**Where the catch-up happens.** The table follows SIG's mid after a no-filter signal (M 2, G 2), in ticks toward the
outside move, measured from the signal minute:

| set | n | +5 min | +15 min | +30 min | +1 h | +2 h | +4 h | outside's own further move by +4 h |
|---|---|---|---|---|---|---|---|---|
| IS | 15 | +0.20 | +0.20 | +0.20 | +0.73 | +0.60 | +1.00 | +0.70 |
| OOS1 | 13 | +0.23 | +0.69 | +0.92 | +1.15 | +1.46 | +2.23 | +1.61 |
| OOS2 | 8-12 | +1.00 | +2.17 | +2.10 | +2.82 | +3.89 | +4.31 | +0.06 |

* In B, SIG covers about half of its 4-h catch-up within 15-30 minutes. A 15-30-minute stability wait spends that
  window, and the remaining 2 ticks of drift less the 1.1-tick round trip is what the stable rule earns at best.
* In A, the whole 4-h catch-up (+1.0 to +2.2 ticks) is barely above the round trip, and part of it is the outside
  moving further.

**Entry-delay test** (pooled OOS mean (t) / ex-top-3; IS mean in brackets):

| rule | +1 min | +5 min | +15 min | +30 min |
|---|---|---|---|---|
| no filter M 2 G 2 H 4 | +0.90 (2.3) / +0.25 [IS -0.90] | +0.73 (1.9) / +0.14 | +0.51 (1.4) / -0.04 | +0.11 (0.4) / -0.25 |
| no filter M 2 G 2 H 12 | +1.60 (2.2) / +0.43 [IS -0.37] | +1.35 (1.9) / +0.36 | +1.12 (1.4) / -0.18 | +0.79 (1.0) / -0.30 |
| stable IS-best | +0.69 (1.1) / -0.31 [IS -0.01] | +0.64 (1.0) / -0.39 | +0.27 (0.4) / -0.89 | +0.37 (0.6) / -0.74 |

Every 15 minutes of waiting costs about 0.3-0.4 tick. Whatever edge exists belongs to speed, not to confirmation.

**Robustness of the stable rule.**
* R = 0.5 pt: the IS-best goes to IS +0.42 (n 21), OOS1 +0.10, OOS2 +1.61 (t 1.3), with ex-top-3 negative throughout.
* R-leg replication in B: +0.76 (t 0.4, n 7), ex-top-3 -3.1.
* No-filter M 2 on B's R legs: +1.84 to +3.30 (t 3.3-3.4, n 15-16), ex-top-3 +1.0 to +1.7 (t 2.2-2.3). This is the
  same events on the other leg, mostly 22 Oct, not independent evidence.

## 5. The owner's example: MN-01, 22 Oct (BST = UTC + 1)

**Outside.** The D-leg consensus was 0.317 until 10:15 UTC. It rose to 0.325 at 10:19 and 0.347 by 10:58, then held at
0.346-0.348 until 13:00. Polymarket went 0.325 -> 0.357 and Kalshi 0.309 -> 0.335. SIG's D leg was 0.310/0.315, and it
stepped once to 0.320/0.325 at about 11:03.

| rule | signal (UTC) | gap at entry | outcome |
|---|---|---|---|
| no filter | 10:49 | 6.0 ticks | caught the one-tick step: +0.9 tick at 1 h and 4 h, +1.9 at 12 h |
| stable (S 15) | 11:04, after the step | 4.9 ticks | SIG never moved again: -1.1 at 1 h and 4 h, -0.1 at 12 h |
| stable (S 30) | did not fire on the D leg | - | - |

The move the owner saw was on the **R leg**: 0.7025 -> 0.6700 over 125 min, so SIG's overround on this race widened
rather than the D leg catching up. A D-leg rule would not have captured it. The R-leg run in section 4 checks this in
general, and the stable rule does no better there.

## 6. Feasible size, partisan cap, S/day

* **Size at the cap.** At 600 notional per leg, a trade is a median of about 880-920 shares.
* **D-adding share.** Of the out-of-sample signals, 42-64% add D. In OOS2, 9 of the 12 no-filter signals added D.
* **Partisan cap.** netD has been 3.9-5.4k on 23 Oct (+3,919 at 21:31 BST). At 4.8k or more, every D-adding signal is
  blocked, which removes 47-64% of the signals and 50-85% of the S.
* **S/day.**

  | rule | all signals (S/day) | R-adding only (S/day) |
  |---|---|---|
  | stable IS-best | +12 | +6 |
  | no filter | +22-30 | +4-7 |

## 7. Sizing from the impact model

Size is set by q* = argmax over q of q·(m − w_in(q) − w_out(q))·tick − (q·σ·tick)²/(2·2000). The walk w comes from the
real liquid ladders (`books.json`, 21 Oct 22:03), with σ ≈ 2.5 ticks/share.

| m used | q* median (p25-p75) | notional at q* | E[S] per trade at q* | E[S] per trade at the cap (~750-900 sh) |
|---|---|---|---|---|
| stable IS-best, pooled OOS +0.69 | 3,250 (2,400-6,300) | ~1.6k | +10.7 | +2.6 |
| stable default +0.90 | 3,300 (2,400-6,300) | ~1.6k | +14.1 | +3.4 |
| no filter, H 12, +1.60 | 7,250 (4,100-11,900) | ~2.7k | +36.5 | +6.0 |
| **any rule ex-top-3 (stable -0.23 to -0.31)** | **0** | 0 | 0 | negative |

* While m is under 2 ticks, the optimum is "fill the smaller touch, never cross a second level", as in
  `directional_impact.md`.
* At the robust estimate (ex-top-3, negative) the optimal size is **zero**.
* At q* the trade would need 1.6-2.7k notional per leg, 3-5x the 600 cap.

## 8. Paper-trading parameters for the minute-scale rules (superseded for P1 by sections 10-11)

This was written before the fine-data x-study. P1 is superseded by the capped trial in section 11. P2 stays paper only.
The
`outside_lead` paper orders in `data/volatile_paper.jsonl` already approximate rule P1.

* **P1 (fast follow; the variant with a chance).**
  * Signal: on the D leg, F = mean(Polymarket, Kalshi), agreeing within 4 pts.
  * Move: F moved 2+ pts within 60 min, and Polymarket and Kalshi each moved 1+ pt the same way.
  * Lag: SIG's mid moved less than 1/3 of that.
  * Gap: 2+ ticks.
  * Entry: within 1 min, taker at the touch, spread at most 2 ticks.
  * Exit: when SIG has closed 70% of the gap, otherwise after 4 h and, separately, after 12 h. Taker exit.
  * Paper size: min(600 / price, 0.8 x the smaller touch).
  * Log both legs. Tag D-adding signals.
* **P2 (the owner's stable rule, for the record).** Same as P1, but M = 1.5, S = 30 min, R = 1 pt, G = 2, H = 12 h.
* **Pass test (both).**
  * 30+ new signals.
  * Mean T/T of +0.5 or better after the 0.05-tick slip.
  * t > 2 clustered by race.
  * Positive without the top 3 races.
  * Positive on R-adding signals alone. Those are the only ones the partisan cap allows.
* **Timing caveat.** At about 3.5 signals a day, 30 signals take about 9 days, which ends after the 31 Oct wind-down. In
  practice neither rule can earn its way to live trading in this competition. Under the cash objective, any live
  version would have to be flat by 12:00 ET on 3 Nov, so the last entry would be 3 Nov 00:00 ET with a 12-h stop.

Other notes:
* No change to the MM is suggested.
* The edge, if any, belongs to speed, but the current 60-120 s direction poll is fast enough (section 10.4).

## 9. Full grid

T/T in ticks/share, mean (t clustered by race). S/day is at the 600-notional cap, all signals. "adds-D share" is the share
of out-of-sample signals that add D.

| filter | M | S | G | H | IS n | IS mean (t) | IS ex-top-3 | IS S/day | OOS1 n | OOS1 mean (t) | OOS1 ex-top-3 | OOS1 S/day | OOS2 n | OOS2 mean (t) | OOS2 ex-top-3 | OOS2 S/day | OOS n | **OOS mean (t)** | **OOS ex-top-3 (t)** | OOS S/day | OOS hit | OOS /day | OOS adds-D share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| stable | 1.5 | 15 | 2 | 1 | 23 | -1.10 (-10.1) | -1.25 (-15.4) | -12 | 20 | -0.50 (-1.6) | -0.92 (-8.1) | -9 | 18 | -1.10 (-5.0) | -1.30 (-7.1) | -59 | 38 | -0.78 (-3.6) | -1.04 (-7.9) | -23 | 0.08 | 5.7 | 0.55 |
| stable | 1.5 | 15 | 2 | 4 | 23 | -0.67 (-2.1) | -1.00 (-3.9) | -6 | 17 | +0.14 (+0.3) | -0.48 (-2.8) | +3 | 15 | -0.63 (-0.8) | -1.46 (-1.8) | -26 | 32 | -0.23 (-0.5) | -0.66 (-1.6) | -5 | 0.41 | 5.0 | 0.53 |
| stable | 1.5 | 15 | 2 | 12 | 23 | -0.14 (-0.3) | -0.75 (-1.7) | -0 | 12 | -0.27 (-0.5) | -1.10 (-3.3) | -2 | 13 | +0.36 (+0.4) | -0.88 (-1.6) | +10 | 25 | +0.06 (+0.1) | -0.72 (-2.0) | +1 | 0.36 | 4.3 | 0.52 |
| stable | 1.5 | 15 | 4 | 1 | 22 | -1.10 (-9.6) | -1.26 (-14.7) | -10 | 19 | -0.47 (-1.4) | -0.91 (-7.6) | -8 | 11 | -0.83 (-2.1) | -1.23 (-4.8) | -30 | 30 | -0.60 (-2.2) | -0.91 (-6.0) | -14 | 0.1 | 4.5 | 0.5 |
| stable | 1.5 | 15 | 4 | 4 | 22 | -0.74 (-2.4) | -1.10 (-4.5) | -7 | 16 | +0.15 (+0.3) | -0.52 (-2.7) | +3 | 9 | +1.23 (+1.2) | -0.10 (-0.2) | +27 | 25 | +0.54 (+1.4) | -0.00 (-0.0) | +9 | 0.48 | 3.9 | 0.48 |
| stable | 1.5 | 15 | 4 | 12 | 22 | -0.19 (-0.3) | -0.84 (-1.9) | -1 | 12 | -0.27 (-0.5) | -1.10 (-3.3) | -2 | 7 | +2.76 (+3.0) | +0.57 (+0.8) | +57 | 19 | +0.85 (+1.6) | -0.03 (-0.1) | +12 | 0.42 | 3.3 | 0.47 |
| stable | 1.5 | 30 | 2 | 1 | 23 | -1.19 (-8.7) | -1.35 (-12.0) | -12 | 17 | -0.57 (-1.5) | -1.03 (-8.3) | -10 | 13 | -1.10 (-4.7) | -1.40 (-6.7) | -41 | 30 | -0.80 (-3.2) | -1.14 (-11.1) | -18 | 0.1 | 4.5 | 0.53 |
| stable | 1.5 | 30 | 2 | 4 | 23 | -0.67 (-2.2) | -1.00 (-3.7) | -6 | 16 | +0.02 (+0.1) | -0.56 (-2.3) | +1 | 10 | +0.60 (+0.8) | -0.39 (-0.9) | +22 | 26 | +0.25 (+0.7) | -0.28 (-1.4) | +7 | 0.42 | 4.0 | 0.54 |
| stable | 1.5 | 30 | 2 | 12 | 22 | -0.01 (-0.0) | -0.60 (-1.8) | +2 | 11 | -0.28 (-0.6) | -1.10 (-5.0) | -2 | 8 | +2.02 (+1.7) | -0.35 (-0.3) | +57 | 19 | +0.69 (+1.1) | -0.31 (-0.7) | +12 | 0.47 | 3.3 | 0.47 |
| stable | 1.5 | 30 | 4 | 1 | 21 | -1.10 (-7.1) | -1.27 (-8.8) | -10 | 16 | -0.54 (-1.3) | -1.02 (-7.6) | -9 | 7 | -0.81 (-2.1) | - | -16 | 23 | -0.62 (-2.0) | -1.05 (-11.8) | -11 | 0.13 | 3.4 | 0.43 |
| stable | 1.5 | 30 | 4 | 4 | 21 | -0.72 (-2.3) | -1.10 (-4.3) | -6 | 15 | +0.03 (+0.1) | -0.60 (-2.3) | +1 | 5 | +1.10 (+0.7) | - | +16 | 20 | +0.30 (+0.7) | -0.35 (-1.6) | +5 | 0.4 | 3.1 | 0.45 |
| stable | 1.5 | 30 | 4 | 12 | 20 | -0.05 (-0.1) | -0.72 (-2.0) | +0 | 11 | -0.28 (-0.6) | -1.10 (-5.0) | -2 | 4 | +3.65 (+2.6) | - | +44 | 15 | +0.77 (+1.1) | -0.60 (-1.9) | +9 | 0.4 | 2.6 | 0.4 |
| stable | 2.0 | 15 | 2 | 1 | 14 | -1.17 (-9.1) | -1.28 (-9.8) | -7 | 15 | -0.57 (-1.4) | -1.02 (-7.1) | -6 | 8 | -1.35 (-5.9) | -1.70 (-14.2) | -38 | 23 | -0.84 (-2.8) | -1.20 (-9.6) | -15 | 0.04 | 3.4 | 0.52 |
| stable | 2.0 | 15 | 2 | 4 | 14 | -1.10 (-2.7) | -1.65 (-4.9) | -7 | 12 | +0.07 (+0.1) | -0.98 (-4.4) | +2 | 6 | +1.23 (+1.1) | -0.77 (-0.9) | +16 | 18 | +0.46 (+0.9) | -0.31 (-0.9) | +6 | 0.44 | 2.8 | 0.5 |
| stable | 2.0 | 15 | 2 | 12 | 14 | -0.10 (-0.1) | -1.10 (-1.9) | +0 | 8 | -0.47 (-0.5) | -2.35 (-4.9) | -3 | 6 | +2.57 (+1.4) | -1.10 (-0.7) | +46 | 14 | +0.83 (+0.9) | -0.55 (-0.9) | +9 | 0.43 | 2.4 | 0.43 |
| stable | 2.0 | 15 | 4 | 1 | 14 | -1.17 (-9.1) | -1.28 (-9.8) | -7 | 14 | -0.53 (-1.2) | -1.01 (-6.4) | -6 | 5 | -1.10 (-3.4) | -1.60 | -22 | 19 | -0.68 (-2.0) | -1.10 (-8.4) | -10 | 0.05 | 2.8 | 0.47 |
| stable | 2.0 | 15 | 4 | 4 | 14 | -1.10 (-2.7) | -1.65 (-4.9) | -7 | 11 | +0.08 (+0.1) | -1.10 (-5.0) | +2 | 4 | +2.15 (+1.5) | -1.10 | +18 | 15 | +0.63 (+1.1) | -0.28 (-0.7) | +6 | 0.47 | 2.3 | 0.47 |
| stable | 2.0 | 15 | 4 | 12 | 14 | -0.10 (-0.1) | -1.10 (-1.9) | +0 | 8 | -0.47 (-0.5) | -2.35 (-4.9) | -3 | 4 | +4.65 (+2.5) | -0.10 | +54 | 12 | +1.23 (+1.3) | -0.32 (-0.5) | +10 | 0.42 | 2.1 | 0.42 |
| stable | 2.0 | 30 | 2 | 1 | 14 | -1.24 (-4.6) | -1.65 (-9.7) | -8 | 12 | -0.43 (-0.8) | -0.99 (-9.4) | -4 | 6 | -1.43 (-7.1) | -1.60 | -26 | 18 | -0.77 (-2.1) | -1.17 (-9.7) | -10 | 0.06 | 2.7 | 0.44 |
| stable | 2.0 | 30 | 2 | 4 | 14 | -0.53 (-0.8) | -1.37 (-3.6) | -3 | 11 | +0.26 (+0.4) | -0.81 (-2.5) | +4 | 4 | +1.65 (+1.0) | -2.10 | +19 | 15 | +0.63 (+1.1) | -0.28 (-0.7) | +8 | 0.53 | 2.3 | 0.47 |
| stable | 2.0 | 30 | 2 | 12 | 14 | +0.47 (+0.7) | -0.65 (-1.5) | +4 | 7 | -0.10 (-0.1) | -2.10 (-3.6) | +0 | 4 | +2.90 (+1.0) | -4.10 | +37 | 11 | +0.99 (+0.9) | -1.10 (-1.3) | +9 | 0.45 | 1.9 | 0.36 |
| stable | 2.0 | 30 | 4 | 1 | 12 | -1.18 (-3.8) | -1.66 (-8.6) | -6 | 11 | -0.37 (-0.7) | -0.98 (-8.2) | -3 | 3 | -1.10 | - | -11 | 14 | -0.53 (-1.2) | -1.01 (-11.3) | -5 | 0.07 | 2.1 | 0.36 |
| stable | 2.0 | 30 | 4 | 4 | 12 | -0.52 (-0.7) | -1.54 (-3.5) | -3 | 10 | +0.30 (+0.4) | -0.93 (-2.4) | +4 | 2 | +3.90 (+2.0) | - | +20 | 12 | +0.90 (+1.4) | -0.23 (-0.5) | +8 | 0.58 | 1.9 | 0.42 |
| stable | 2.0 | 30 | 4 | 12 | 12 | +0.23 (+0.3) | -1.10 (-3.8) | +2 | 7 | -0.10 (-0.1) | -2.10 (-3.6) | +0 | 2 | +7.40 (+4.9) | - | +45 | 9 | +1.57 (+1.2) | -0.90 (-1.1) | +11 | 0.44 | 1.6 | 0.33 |
| stable | 3.0 | 15 | 2 | 1 | 3 | -1.43 (-4.3) | - | -2 | 4 | -0.85 (-3.4) | -1.10 | -3 | 3 | -1.43 (-2.1) | - | -19 | 7 | -1.10 (-3.6) | -1.60 (-5.5) | -7 | 0.0 | 1.0 | 0.14 |
| stable | 3.0 | 15 | 2 | 4 | 3 | -0.43 (-0.5) | - | -0 | 4 | -0.35 (-0.5) | -1.10 | -1 | 2 | -0.10 (-0.0) | - | -6 | 6 | -0.27 (-0.3) | -1.77 (-2.6) | -2 | 0.33 | 0.9 | 0.17 |
| stable | 3.0 | 15 | 2 | 12 | 3 | -0.43 (-0.2) | - | -0 | 4 | -0.85 (-0.6) | -3.10 | -3 | 2 | +0.90 (+0.2) | - | -2 | 6 | -0.27 (-0.2) | -3.10 | -3 | 0.33 | 1.0 | 0.17 |
| stable | 3.0 | 15 | 4 | 1 | 3 | -1.43 (-4.3) | - | -2 | 4 | -0.85 (-3.4) | -1.10 | -3 | 2 | -1.10 (-1.1) | - | -12 | 6 | -0.93 (-3.0) | -1.43 (-4.3) | -5 | 0.0 | 0.9 | 0.0 |
| stable | 3.0 | 15 | 4 | 4 | 3 | -0.43 (-0.5) | - | -0 | 4 | -0.35 (-0.5) | -1.10 | -1 | 1 | +2.90 | - | +6 | 5 | +0.30 (+0.3) | -1.10 | +1 | 0.4 | 0.8 | 0.0 |
| stable | 3.0 | 15 | 4 | 12 | 3 | -0.43 (-0.2) | - | -0 | 4 | -0.85 (-0.6) | -3.10 | -3 | 1 | +4.90 | - | +12 | 5 | +0.30 (+0.2) | -3.10 | +0 | 0.4 | 0.9 | 0.0 |
| stable | 3.0 | 30 | 2 | 1 | 3 | -1.10 (-1.9) | - | -1 | 3 | -0.77 (-2.3) | - | -2 | 1 | -1.10 | - | -6 | 4 | -0.85 (-3.4) | -1.10 | -3 | 0.0 | 0.6 | 0.0 |
| stable | 3.0 | 30 | 2 | 4 | 3 | -0.10 (-0.1) | - | +0 | 3 | -0.43 (-0.7) | - | -1 | 0 | - | - | - | 3 | -0.43 (-0.7) | - | -1 | 0.33 | 0.5 | 0.0 |
| stable | 3.0 | 30 | 2 | 12 | 3 | -0.43 (-0.2) | - | -0 | 3 | +0.23 (+0.3) | - | +1 | 0 | - | - | - | 3 | +0.23 (+0.3) | - | +0 | 0.33 | 0.5 | 0.0 |
| stable | 3.0 | 30 | 4 | 1 | 3 | -1.10 (-1.9) | - | -1 | 3 | -0.77 (-2.3) | - | -2 | 1 | -1.10 | - | -6 | 4 | -0.85 (-3.4) | -1.10 | -3 | 0.0 | 0.6 | 0.0 |
| stable | 3.0 | 30 | 4 | 4 | 3 | -0.10 (-0.1) | - | +0 | 3 | -0.43 (-0.7) | - | -1 | 0 | - | - | - | 3 | -0.43 (-0.7) | - | -1 | 0.33 | 0.5 | 0.0 |
| stable | 3.0 | 30 | 4 | 12 | 3 | -0.43 (-0.2) | - | -0 | 3 | +0.23 (+0.3) | - | +1 | 0 | - | - | - | 3 | +0.23 (+0.3) | - | +0 | 0.33 | 0.5 | 0.0 |
| none | 1.5 | 0 | 2 | 1 | 24 | -0.98 (-6.8) | -1.10 (-7.9) | -10 | 22 | +0.04 (+0.1) | -0.51 (-2.8) | +1 | 25 | +0.06 (+0.2) | -0.35 (-1.2) | +27 | 47 | +0.05 (+0.2) | -0.28 (-1.2) | +8 | 0.38 | 7.0 | 0.62 |
| none | 1.5 | 0 | 2 | 4 | 23 | -0.67 (-2.0) | -1.10 (-4.1) | -7 | 18 | +0.79 (+1.8) | +0.21 (+0.9) | +14 | 19 | +0.16 (+0.2) | -0.77 (-1.1) | +28 | 37 | +0.47 (+1.0) | -0.10 (-0.3) | +18 | 0.59 | 5.8 | 0.59 |
| none | 1.5 | 0 | 2 | 12 | 23 | -0.36 (-0.7) | -0.99 (-2.1) | -3 | 13 | +0.67 (+1.2) | -0.21 (-0.5) | +12 | 15 | +0.90 (+1.2) | -0.32 (-0.5) | +67 | 28 | +0.79 (+1.6) | +0.16 (+0.4) | +25 | 0.68 | 4.9 | 0.57 |
| none | 1.5 | 0 | 4 | 1 | 23 | -1.01 (-9.1) | -1.15 (-13.4) | -10 | 20 | -0.10 (-0.3) | -0.54 (-2.9) | -2 | 20 | +0.50 (+0.9) | -0.17 (-0.4) | +53 | 40 | +0.20 (+0.7) | -0.19 (-0.9) | +13 | 0.4 | 6.0 | 0.57 |
| none | 1.5 | 0 | 4 | 4 | 22 | -0.69 (-2.2) | -1.05 (-4.0) | -7 | 16 | +0.59 (+1.5) | +0.07 (+0.2) | +9 | 14 | +0.47 (+0.4) | -0.83 (-0.9) | +36 | 30 | +0.53 (+1.0) | -0.02 (-0.0) | +16 | 0.63 | 4.7 | 0.53 |
| none | 1.5 | 0 | 4 | 12 | 22 | -0.42 (-0.8) | -1.10 (-2.6) | -4 | 12 | +0.32 (+0.5) | -0.73 (-2.6) | +7 | 10 | +1.10 (+1.0) | -0.60 (-0.6) | +56 | 22 | +0.67 (+1.1) | -0.16 (-0.3) | +18 | 0.64 | 3.8 | 0.5 |
| none | 2.0 | 0 | 2 | 1 | 16 | -1.04 (-5.3) | -1.35 (-12.1) | -7 | 17 | -0.10 (-0.2) | -0.93 (-5.9) | -0 | 16 | +0.40 (+1.0) | -0.10 (-0.3) | +34 | 33 | +0.14 (+0.5) | -0.29 (-1.3) | +9 | 0.39 | 4.9 | 0.67 |
| none | 2.0 | 0 | 2 | 4 | 15 | -0.90 (-2.0) | -1.55 (-4.3) | -6 | 13 | +0.52 (+0.8) | -0.32 (-1.4) | +7 | 12 | +1.32 (+2.8) | +0.57 (+1.1) | +62 | 25 | +0.90 (+2.3) | +0.25 (+0.9) | +22 | 0.6 | 3.9 | 0.64 |
| none | 2.0 | 0 | 2 | 12 | 15 | -0.37 (-0.5) | -1.37 (-2.5) | -1 | 9 | +0.34 (+0.4) | -0.93 (-1.5) | +4 | 11 | +2.63 (+2.6) | +0.65 (+0.5) | +113 | 20 | +1.60 (+2.2) | +0.43 (+0.9) | +30 | 0.65 | 3.5 | 0.6 |
| none | 2.0 | 0 | 4 | 1 | 15 | -1.17 (-7.8) | -1.35 (-12.1) | -8 | 15 | -0.23 (-0.6) | -0.92 (-5.4) | -3 | 13 | +0.90 (+1.2) | -0.10 (-0.2) | +55 | 28 | +0.29 (+0.8) | -0.29 (-1.2) | +13 | 0.43 | 4.2 | 0.64 |
| none | 2.0 | 0 | 4 | 4 | 14 | -1.03 (-2.5) | -1.55 (-4.3) | -6 | 11 | +0.35 (+0.7) | -0.35 (-1.3) | +4 | 11 | +1.63 (+2.4) | +0.40 (+0.9) | +72 | 22 | +0.99 (+2.2) | +0.21 (+0.7) | +22 | 0.64 | 3.4 | 0.64 |
| none | 2.0 | 0 | 4 | 12 | 14 | -0.46 (-0.6) | -1.37 (-2.5) | -2 | 8 | +0.03 (+0.0) | -1.60 (-3.2) | +0 | 10 | +3.10 (+2.8) | +0.90 (+0.7) | +125 | 18 | +1.73 (+2.1) | +0.45 (+0.6) | +30 | 0.61 | 3.1 | 0.61 |
| none | 3.0 | 0 | 2 | 1 | 4 | -1.10 (-2.7) | -2.10 | -2 | 5 | -0.10 (-0.2) | -1.10 | -0 | 5 | +1.70 (+2.7) | -0.10 | +36 | 10 | +0.80 (+1.5) | -0.30 (-0.8) | +10 | 0.5 | 1.5 | 0.4 |
| none | 3.0 | 0 | 2 | 4 | 3 | -0.43 (-0.5) | - | -0 | 5 | +0.90 (+0.8) | -1.10 | +5 | 4 | +2.15 (+7.5) | - | +37 | 9 | +1.46 (+2.3) | +0.15 (+0.2) | +14 | 0.67 | 1.4 | 0.44 |
| none | 3.0 | 0 | 2 | 12 | 3 | -0.77 (-0.5) | - | -1 | 4 | -0.60 (-0.5) | -3.10 | -2 | 4 | +2.15 (+7.5) | - | +46 | 8 | +0.77 (+1.0) | -0.60 (-0.5) | +9 | 0.5 | 1.4 | 0.38 |
| none | 3.0 | 0 | 4 | 1 | 4 | -1.10 (-2.7) | -2.10 | -2 | 5 | -0.10 (-0.2) | -1.10 | -0 | 4 | +2.65 (+1.7) | -0.10 | +43 | 9 | +1.12 (+1.4) | -0.30 (-0.8) | +12 | 0.56 | 1.3 | 0.33 |
| none | 3.0 | 0 | 4 | 4 | 3 | -0.43 (-0.5) | - | -0 | 5 | +0.90 (+0.8) | -1.10 | +5 | 3 | +3.90 (+2.6) | - | +49 | 8 | +2.02 (+2.1) | +0.15 (+0.2) | +17 | 0.75 | 1.2 | 0.38 |
| none | 3.0 | 0 | 4 | 12 | 3 | -0.77 (-0.5) | - | -1 | 4 | -0.60 (-0.5) | -3.10 | -2 | 3 | +3.90 (+2.6) | - | +61 | 7 | +1.33 (+1.1) | -0.60 (-0.5) | +13 | 0.57 | 1.2 | 0.29 |


## 10. Refinement: the optimal wait x (seconds), REAL vs NOISE moves, minimum x in production

### 10.1 Data resolution achieved

| source | what | resolution achieved |
|---|---|---|
| our `venue_quotes` (Polymarket, Kalshi, SIG) | the bot's stored outside polls | **too coarse:** median 601 s between stored rows per instrument (p10 60 s, p90 715 s) |
| our `venue_fair` | the direction poll | median 83 s per leg (p10 60 s, p90 129 s). The poll itself runs every 60 s by day and 120 s by night (`config.json` direction `poll_sec_day` / `poll_sec_night`; measured median 74 s in `sigbot.log`) |
| Polymarket CLOB `prices-history`, fidelity 1 | mid-price series per D token, 8-23 Oct | **median 60 s, p90 70 s** (114 races) |
| Polymarket data-api `/trades` | every print on the market, converted to D-YES terms | **tick level**, 54k prints, about 30 per race-day. Thin: in many minutes there is no print |
| Kalshi `candlesticks`, period 1 | 1-min bid/ask close | **1 min**. Candles are emitted only in minutes where the book or trades changed (median 120 s between candles), so they are forward-filled |
| SIG | A: 5-min candle close (to 21 Oct 21:30); B: `quotes` about 30 s on MM legs, `venue_fair` about 83 s on the others (from 21 Oct 22:43) | 30 s to 5 min |

**What that allows.**
* The outside move and its stability can be timed to about a minute.
* Within a minute, only the Polymarket prints add information. Using them as an extra reversal test (prints beyond a
  1c half-spread allowance) changed the results in only one cell: x = 60 s, one trade fewer.
* So x = 0, 15 and 30 s are measurably identical for noise filtering. They differ only in entry time, and SIG's gap
  does not change within 30 s either.
* Seconds-level resolution on both sides would need a live recording. The Polymarket websocket and SIG book pushes, as
  `hft_record.py` does, give that. A recording is not history: the 22 Oct 39-min recording held 52 Polymarket moves
  of 1c or more, and SIG followed 0 within 10 s (`hft_strategies.md`).

### 10.2 Method

**Events.** These are fixed and independent of x.
* Grid: 1 min.
* Trigger: the consensus F = mean(Polymarket mid, Kalshi mid) has moved at least M pts against its value 60 min
  earlier. Both venues agree within 4 pts at both ends, and each venue moved at least 0.5 M the same way.
* SIG side: SIG's mid moved less than 1/3 of the move, and the gap is at least 2 ticks.
* Detection time t_c is the timestamp of the newest Polymarket or Kalshi data point showing the move. That is when a
  streaming bot would know.
* At most one event per race per 2 h.
* Counts for M = 1.5 / 2 / 3 / 5 pts: 66 / 47 / 14 / 2 events, of which 28 / 18 / 4 / 0 are in-sample.

**Labels (evaluation only).**
* **REAL** if F still holds at least 70% of the move at both +1 h and +4 h, or if SIG's mid has followed by at least
  50% of the move by +4 h.
* **NOISE** otherwise.

**Wait x** in {0, 15, 30, 60, 120, 300, 600} s.
* Stable for x: no Polymarket mid and no Kalshi mid in (t_c, t_c + x + latency] gives back more than 30% of that
  venue's move.
* An unstable event is not traded.

**Entry, exit and costs.**
* Entry: SIG's touch at t_c + x + latency, with latency 2 s (streaming plus order). Spread at most 2 ticks. Skipped if
  less than 1 tick of the gap is left.
* Exit: when SIG's mid has closed 70% of the entry gap, otherwise at the time stop H (4 h or 12 h). Taker. Costs as
  above.
* "Gap left" is the gap at entry divided by the gap at detection.

**Split.** IS is 8-16 Oct. OOS1 is 17 Oct to 21 Oct 21:30 (SIG candles). OOS2 is 21 Oct 22:43 to 23 Oct (live SIG
quotes). x is picked by the in-sample S/day per (M, H) cell, then tested out of sample; the reverse fold (pick on OOS)
is also reported.

### 10.3 Results: x grid (latency 2 s, mids only)

| M | H | x (s) | IS P(real) -> P(real given stable) | IS n | IS mean (t) | OOS P(real) -> P(real given stable) | OOS stable pass | OOS gap left at entry | OOS n | **OOS mean (t)** | **OOS ex-top-3 (t)** | OOS1 mean (n) | OOS2 mean (n) | OOS S/day at cap |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1.5 | 4 | 0 | 0.82 -> 0.82 | 25 | -0.50 (-1.7) | 0.86 -> 0.86 | 1.0/1.0 | 1.0 | 35 | +0.69 (+2.0) | +0.13 (+0.7) | -0.04 (-0.2) (17) | +1.37 (+1.9) (18) | 27.5 |
| 1.5 | 4 | 15 | 0.82 -> 0.82 | 25 | -0.50 (-1.7) | 0.86 -> 0.86 | 1.0/1.0 | 1.0 | 35 | +0.69 (+2.0) | +0.13 (+0.7) | -0.04 (-0.2) (17) | +1.37 (+1.9) (18) | 27.5 |
| 1.5 | 4 | 30 | 0.82 -> 0.85 | 24 | -0.56 (-1.8) | 0.86 -> 0.86 | 1.0/1.0 | 0.99 | 35 | +0.69 (+2.0) | +0.13 (+0.7) | -0.04 (-0.2) (17) | +1.37 (+1.9) (18) | 27.5 |
| 1.5 | 4 | 60 | 0.82 -> 0.85 | 24 | -0.56 (-1.8) | 0.86 -> 0.86 | 1.0/1.0 | 0.99 | 35 | +0.63 (+2.0) | +0.13 (+0.7) | -0.04 (-0.2) (17) | +1.26 (+1.9) (18) | 24.4 |
| 1.5 | 4 | 120 | 0.82 -> 0.85 | 23 | -0.58 (-1.8) | 0.86 -> 0.86 | 0.94/1.0 | 0.98 | 33 | +0.67 (+2.0) | +0.15 (+0.7) | -0.04 (-0.1) (16) | +1.34 (+2.0) (17) | 24.9 |
| 1.5 | 4 | 300 | 0.82 -> 0.84 | 21 | -0.86 (-2.6) | 0.86 -> 0.86 | 0.94/1.0 | 0.9 | 31 | +0.43 (+1.2) | -0.12 (-0.5) | -0.04 (-0.1) (16) | +0.93 (+1.2) (15) | 16.2 |
| 1.5 | 4 | 600 | 0.82 -> 0.88 | 21 | -0.96 (-2.7) | 0.86 -> 0.85 | 0.94/0.94 | 0.89 | 26 | +0.19 (+0.6) | -0.17 (-0.6) | +0.03 (+0.1) (16) | +0.45 (+0.5) (10) | 4.7 |
| 1.5 | 12 | 0 | 0.82 -> 0.82 | 25 | -0.26 (-0.6) | 0.86 -> 0.86 | 1.0/1.0 | 1.0 | 34 | +1.21 (+2.5) | +0.47 (+1.4) | +1.19 (+2.2) (17) | +1.22 (+1.5) (17) | 42.1 |
| 1.5 | 12 | 15 | 0.82 -> 0.82 | 25 | -0.26 (-0.6) | 0.86 -> 0.86 | 1.0/1.0 | 1.0 | 34 | +1.21 (+2.5) | +0.47 (+1.4) | +1.19 (+2.2) (17) | +1.22 (+1.5) (17) | 42.1 |
| 1.5 | 12 | 30 | 0.82 -> 0.85 | 24 | -0.22 (-0.5) | 0.86 -> 0.86 | 1.0/1.0 | 1.0 | 34 | +1.21 (+2.5) | +0.47 (+1.4) | +1.19 (+2.2) (17) | +1.22 (+1.5) (17) | 42.1 |
| 1.5 | 12 | 60 | 0.82 -> 0.85 | 24 | -0.22 (-0.5) | 0.86 -> 0.86 | 1.0/1.0 | 0.99 | 34 | +1.18 (+2.5) | +0.51 (+1.5) | +1.25 (+2.3) (17) | +1.11 (+1.4) (17) | 39.2 |
| 1.5 | 12 | 120 | 0.82 -> 0.85 | 23 | -0.23 (-0.5) | 0.86 -> 0.85 | 0.94/1.0 | 0.98 | 33 | +1.04 (+2.4) | +0.50 (+1.4) | +0.96 (+2.4) (16) | +1.11 (+1.4) (17) | 34.9 |
| 1.5 | 12 | 300 | 0.82 -> 0.84 | 21 | -0.39 (-0.7) | 0.86 -> 0.85 | 0.94/1.0 | 0.91 | 31 | +0.72 (+1.5) | +0.10 (+0.2) | +0.77 (+2.0) (16) | +0.67 (+0.7) (15) | 22.3 |
| 1.5 | 12 | 600 | 0.82 -> 0.88 | 20 | -0.65 (-1.3) | 0.86 -> 0.85 | 0.94/0.94 | 0.9 | 27 | +0.55 (+1.2) | +0.05 (+0.1) | +0.90 (+2.3) (16) | +0.04 (+0.0) (11) | 11.2 |
| 2.0 | 4 | 0 | 0.83 -> 0.83 | 16 | -0.66 (-1.7) | 0.85 -> 0.85 | 1.0/1.0 | 1.0 | 25 | +0.98 (+2.2) | +0.27 (+0.9) | +0.01 (+0.0) (14) | +2.22 (+2.2) (11) | 28.1 |
| 2.0 | 4 | 15 | 0.83 -> 0.83 | 16 | -0.66 (-1.7) | 0.85 -> 0.85 | 1.0/1.0 | 1.0 | 25 | +0.98 (+2.2) | +0.27 (+0.9) | +0.01 (+0.0) (14) | +2.22 (+2.2) (11) | 28.1 |
| 2.0 | 4 | 30 | 0.83 -> 0.83 | 16 | -0.66 (-1.7) | 0.85 -> 0.85 | 1.0/1.0 | 0.99 | 25 | +0.98 (+2.2) | +0.27 (+0.9) | +0.01 (+0.0) (14) | +2.22 (+2.2) (11) | 28.0 |
| 2.0 | 4 | 60 | 0.83 -> 0.83 | 16 | -0.66 (-1.7) | 0.85 -> 0.85 | 0.93/1.0 | 0.98 | 25 | +0.90 (+2.2) | +0.27 (+0.9) | +0.01 (+0.0) (14) | +2.04 (+2.1) (11) | 24.9 |
| 2.0 | 4 | 120 | 0.83 -> 0.83 | 15 | -0.70 (-1.7) | 0.85 -> 0.84 | 0.87/1.0 | 0.97 | 24 | +0.94 (+2.1) | +0.29 (+0.9) | -0.06 (-0.2) (13) | +2.13 (+2.2) (11) | 24.9 |
| 2.0 | 4 | 300 | 0.83 -> 0.83 | 15 | -0.77 (-1.6) | 0.85 -> 0.84 | 0.87/1.0 | 0.87 | 23 | +0.60 (+1.2) | -0.10 (-0.3) | -0.14 (-0.4) (13) | +1.55 (+1.5) (10) | 16.4 |
| 2.0 | 4 | 600 | 0.83 -> 0.88 | 15 | -0.83 (-1.7) | 0.85 -> 0.83 | 0.87/0.92 | 0.83 | 20 | +0.05 (+0.1) | -0.47 (-1.6) | -0.29 (-0.8) (13) | +0.69 (+0.5) (7) | 0.5 |
| 2.0 | 12 | 0 | 0.83 -> 0.83 | 16 | +0.03 (+0.0) | 0.85 -> 0.85 | 1.0/1.0 | 1.0 | 25 | +1.46 (+2.3) | +0.46 (+1.0) | +1.22 (+2.0) (14) | +1.76 (+1.3) (11) | 37.7 |
| 2.0 | 12 | 15 | 0.83 -> 0.83 | 16 | +0.03 (+0.0) | 0.85 -> 0.85 | 1.0/1.0 | 1.0 | 25 | +1.46 (+2.3) | +0.46 (+1.0) | +1.22 (+2.0) (14) | +1.76 (+1.3) (11) | 37.7 |
| 2.0 | 12 | 30 | 0.83 -> 0.83 | 16 | +0.03 (+0.0) | 0.85 -> 0.85 | 1.0/1.0 | 1.0 | 25 | +1.46 (+2.3) | +0.46 (+1.0) | +1.22 (+2.0) (14) | +1.76 (+1.3) (11) | 37.7 |
| 2.0 | 12 | 60 | 0.83 -> 0.83 | 16 | +0.03 (+0.0) | 0.85 -> 0.84 | 0.93/1.0 | 0.99 | 24 | +1.44 (+2.3) | +0.49 (+1.0) | +1.25 (+1.9) (13) | +1.67 (+1.3) (11) | 34.8 |
| 2.0 | 12 | 120 | 0.83 -> 0.83 | 15 | +0.03 (+0.1) | 0.85 -> 0.83 | 0.87/1.0 | 0.98 | 23 | +1.25 (+2.1) | +0.46 (+1.0) | +0.77 (+1.8) (12) | +1.76 (+1.4) (11) | 30.1 |
| 2.0 | 12 | 300 | 0.83 -> 0.83 | 15 | +0.10 (+0.1) | 0.85 -> 0.83 | 0.87/1.0 | 0.88 | 22 | +0.85 (+1.3) | -0.07 (-0.1) | +0.69 (+1.5) (12) | +1.05 (+0.8) (10) | 20.4 |
| 2.0 | 12 | 600 | 0.83 -> 0.88 | 14 | -0.03 (-0.0) | 0.85 -> 0.83 | 0.87/0.91 | 0.83 | 18 | +0.29 (+0.5) | -0.56 (-1.3) | +0.52 (+1.2) (12) | -0.18 (-0.1) (6) | 1.7 |
| 3.0 | 4 | 0 | 0.75 -> 0.75 | 4 | -0.35 (-0.3) | 0.89 -> 0.89 | 1.0/1.0 | 1.0 | 9 | +1.68 (+1.6) | -0.60 (-1.0) | -0.35 (-0.4) (4) | +3.30 (+1.6) (5) | 18.8 |
| 3.0 | 4 | 15 | 0.75 -> 0.75 | 4 | -0.35 (-0.3) | 0.89 -> 0.89 | 1.0/1.0 | 1.0 | 9 | +1.68 (+1.6) | -0.60 (-1.0) | -0.35 (-0.4) (4) | +3.30 (+1.6) (5) | 18.8 |
| 3.0 | 4 | 30 | 0.75 -> 0.75 | 4 | -0.35 (-0.3) | 0.89 -> 0.89 | 1.0/1.0 | 1.0 | 9 | +1.68 (+1.6) | -0.60 (-1.0) | -0.35 (-0.4) (4) | +3.30 (+1.6) (5) | 18.8 |
| 3.0 | 4 | 60 | 0.75 -> 0.75 | 4 | -0.35 (-0.3) | 0.89 -> 0.89 | 1.0/1.0 | 0.94 | 9 | +1.34 (+1.4) | -0.85 (-1.4) | -0.60 (-0.7) (4) | +2.90 (+1.6) (5) | 15.0 |
| 3.0 | 4 | 120 | 0.75 -> 0.75 | 4 | -0.35 (-0.3) | 0.89 -> 0.89 | 1.0/1.0 | 0.93 | 9 | +1.34 (+1.4) | -0.85 (-1.4) | -0.60 (-0.7) (4) | +2.90 (+1.6) (5) | 15.0 |
| 3.0 | 4 | 300 | 0.75 -> 0.67 | 3 | -0.77 (-0.9) | 0.89 -> 1.0 | 1.0/0.8 | 0.82 | 7 | +1.76 (+1.6) | -1.60 (-3.2) | -0.60 (-0.7) (4) | +4.90 (3) | 14.7 |
| 3.0 | 4 | 600 | 0.75 -> 0.67 | 3 | -0.77 (-0.9) | 0.89 -> 1.0 | 1.0/0.8 | 0.8 | 5 | +0.90 (+0.7) | -2.10 | -0.60 (-0.7) (4) | +6.90 (1) | 3.4 |
| 3.0 | 12 | 0 | 0.75 -> 0.75 | 3 | -0.43 (-0.2) | 0.89 -> 0.89 | 1.0/1.0 | 1.0 | 8 | +2.65 (+2.5) | +0.23 (+0.5) | +1.57 (+2.3) (3) | +3.30 (+1.6) (5) | 25.6 |
| 3.0 | 12 | 15 | 0.75 -> 0.75 | 3 | -0.43 (-0.2) | 0.89 -> 0.89 | 1.0/1.0 | 1.0 | 8 | +2.65 (+2.5) | +0.23 (+0.5) | +1.57 (+2.3) (3) | +3.30 (+1.6) (5) | 25.6 |
| 3.0 | 12 | 30 | 0.75 -> 0.75 | 3 | -0.43 (-0.2) | 0.89 -> 0.89 | 1.0/1.0 | 1.0 | 8 | +2.65 (+2.5) | +0.23 (+0.5) | +1.57 (+2.3) (3) | +3.30 (+1.6) (5) | 25.6 |
| 3.0 | 12 | 60 | 0.75 -> 0.75 | 3 | -0.43 (-0.2) | 0.89 -> 0.89 | 1.0/1.0 | 0.94 | 8 | +2.27 (+2.3) | -0.10 | +1.23 (+1.4) (3) | +2.90 (+1.6) (5) | 21.4 |
| 3.0 | 12 | 120 | 0.75 -> 0.75 | 3 | -0.43 (-0.2) | 0.89 -> 0.89 | 1.0/1.0 | 0.92 | 8 | +2.27 (+2.3) | -0.10 | +1.23 (+1.4) (3) | +2.90 (+1.6) (5) | 21.4 |
| 3.0 | 12 | 300 | 0.75 -> 0.67 | 3 | -1.10 (-0.7) | 0.89 -> 1.0 | 1.0/0.8 | 0.8 | 6 | +3.07 (+4.0) | -0.10 | +1.23 (+1.4) (3) | +4.90 (3) | 21.0 |
| 3.0 | 12 | 600 | 0.75 -> 0.67 | 3 | -1.10 (-0.7) | 0.89 -> 1.0 | 1.0/0.8 | 0.76 | 4 | +2.65 (+2.3) | - | +1.23 (+1.4) (3) | +6.90 (1) | 8.4 |

Notes on the table:
* "OOS stable pass" is OOS1/OOS2.
* M 5 has only 2 events, both out of sample (+4.9 each).

**REAL vs NOISE, x = 0, M 2, H 12, all samples:**

| label | n | mean (ticks/share) |
|---|---|---|
| REAL | 34 | +1.40 |
| NOISE | 7 | -1.53 |

**Waiting does not separate the two.** Over 600 s, P(real) rises from 0.83 to 0.88 in-sample and from 0.85 to 0.83-0.85
out of sample. The NOISE moves are not the ones that revert within 10 minutes. They revert over hours, and no short
wait can see that.

**x selection.**

| M | H | x picked on IS S/day | OOS at that x: n, mean (t), ex-top-3 (t) | x picked on OOS S/day (reverse fold) | IS at that x |
|---|---|---|---|---|---|
| 1.5 | 4 | 0 | 35, +0.69 (2.0), +0.13 (0.7) | 0 | -0.50 (-1.7) |
| 1.5 | 12 | 120 | 33, +1.04 (2.4), +0.50 (1.4) | 0 | -0.26 (-0.6) |
| 2 | 4 | 0 | 25, +0.98 (2.2), +0.27 (0.9) | 0 | -0.66 (-1.7) |
| 2 | 12 | 300 (IS +1.3 vs +1.0 S/day at x 0) | 22, +0.85 (1.3), -0.07 (-0.1) | 0 | +0.03 (0.0) |
| 3 | 4 | 0 | 9, +1.68 (1.6), -0.60 | 0 | -0.35 |
| 3 | 12 | 0 | 8, +2.65 (2.5), +0.23 (0.5) | 0 | -0.43 |

* Out of sample, every (M, H) cell picks x = 0.
* In-sample, the S/day differences between x values are under 0.4 S/day, which is noise.
* The global in-sample best cell (M 2, H 12, x 300) fails out of sample.
* **Optimal x = 0, that is, as soon as the feed allows.**

**x = 0 fast follow, detail.**

| cell | out-of-sample by day (n, mean) | in-sample by day (n, mean) | adds D | R-adding only | median size |
|---|---|---|---|---|---|
| M 2, H 12 | 17 Oct 4, +0.65; 19 Oct 2, +3.4; 20 Oct 2, +1.9; 21 Oct 7, +0.76; 22 Oct 10, +1.80 | 9 Oct 3, +2.9; 10 Oct 1, +0.9; 11 Oct 3, +1.23; 12 Oct 1, -3.1; 13 Oct 1, +0.9; 14 Oct 4, -2.1; 15 Oct 2, -0.1; 16 Oct 1, -2.1 | 60% | n 10, +2.0, +16 S/day | 948 sh |
| M 1.5, H 12 | similar | similar | 59% | n 14, +1.54, +18.5 S/day | 926 sh |
| M 2, H 4 | 21 Oct 7, +0.33; 22 Oct 9, +2.23 | - | 64% | n 9, +0.73, +5 S/day | 948 sh |

Holding time averages 9.1 h with the 12-h stop, and 36% of trades exit on the 70% catch-up.

### 10.4 Feed and latency: the minimum achievable x

Out-of-sample mean (t) for M 2, H 12, by detection-plus-order latency:

| x | 2 s (streaming) | 60 s (current day poll) | 120 s (current night poll) |
|---|---|---|---|
| 0 | +1.46 (2.3), gap left 1.00 | +1.44 (2.3), 0.99 | +1.25 (2.1), 0.98 |
| 60 | +1.44 (2.3) | +1.25 (2.1) | +0.95 (1.5) |
| 300 | +0.85 (1.3) | +0.90 (1.3) | +0.85 (1.3) |

**Current bot.** The direction sleeve polls Polymarket and Kalshi every 60 s by day and 120 s by night. The SIG book is
already real-time through the feed (age about 1 s), and orders take about 1 s. The minimum x is therefore one poll
interval, 60-120 s. That gives up 0.02 tick by day and about 0.2 tick at night against streaming.

**Streaming.**
* Polymarket's public CLOB market websocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`, no key needed;
  `hft_record.py` already subscribes to 230 tokens) gives sub-second Polymarket.
* Kalshi's websocket needs an authenticated API key. Without one, a batched REST poll (`GET /markets?tickers=...`, up
  to about 100 tickers per call) every 5-10 s fits Kalshi's public rate limits.
* That gives x of about 5-10 s, worth at most +0.05 tick here.
* **Recommendation:** keep the current poll, but use the 60-s day cadence around the clock while the trial runs, which
  removes the 0.2-tick night penalty.

### 10.5 Sizing (impact model)

| m used | q* median (p25-p75) | notional at q* | E[S] per trade at q* | E[S] per trade at the 600 cap |
|---|---|---|---|---|
| m = +1.46 (σ 2.7) | 7,200 (4,100-11,900) | 2.7k | +30 | +5.5 |
| ex-top-3 m = +0.46 | 3,250 (2,400-6,200), touch-limited | 1.6k | +6.9 | +1.7 |

**Trial size:** stay at the 600 cap, about 950 shares. Raise toward the smaller touch (about 3k shares, about 1.6k
notional) only if the trial's first 20 closed trades average at least +0.5 tick.

### 10.6 Decision curves A, B, C against x (same events), and the optimal x

Figure: `research_notes/figures/outside_sweep_x.png`. The curves are in `data/research_outside_sweep/curves.json`,
including one row per event and x.

**Setup.**
* Every curve uses the same event set: M at least 1.5 pts (62 events) or at least 2 pts (44), with a 12-h stop and 2 s
  of latency.
* x runs from 0 to 3,600 s. Events whose 3,600-s wait plus 12-h hold would run past the data are dropped, so the x = 0
  numbers differ slightly from 10.3.
* **The samples are small.** The figure therefore shows the per-event P&L points (jittered) behind the C curve, not a
  smoothed fit.

**Curves for M at least 2 pts.**

| x (s) | A: P(real given stable for x) [95% Wilson], n | stable pass | B: SIG gap still available (ticks; share of gap at detection) | B: takes by others at the stale price (events with any / covered) | C: realised EV, OOS [95%], n | C: realised EV, IS, n | C: IS-calibrated model EV on OOS |
|---|---|---|---|---|---|---|---|
| 0 | 0.84 [0.71, 0.92], 44 | 1.00 | 7.18 (1.000) | 1 / 22 | **+1.46 [+0.19, +2.73]**, 25 | +0.03, 16 | +0.77 |
| 15 | 0.84 [0.71, 0.92], 44 | 1.00 | 7.18 (1.000) | 1 / 22 | +1.46 [+0.19, +2.73], 25 | +0.03 | +0.77 |
| 30 | 0.84 [0.71, 0.92], 44 | 1.00 | 7.18 (1.000) | 1 / 22 | +1.46 [+0.19, +2.73], 25 | +0.03 | +0.77 |
| 60 | 0.84 [0.70, 0.92], 43 | 0.98 | 7.12 (0.993) | 2 / 22 | +1.44 [+0.23, +2.66], 24 | +0.03 | +0.73 |
| 120 | 0.83 [0.69, 0.92], 42 | 0.95 | 7.04 (0.964) | 2 / 22 | +1.25 [+0.10, +2.39], 23 | +0.03 | +0.70 |
| 300 | 0.83 [0.69, 0.92], 42 | 0.95 | 6.82 (0.913) | 2 / 22 | +0.86 [-0.41, +2.12], 22 | +0.10 | +0.60 |
| 600 | 0.85 [0.71, 0.93], 40 | 0.91 | 6.55 (0.889) | 2 / 22 | +0.29 [-0.93, +1.51], 18 | -0.03 | +0.45 |
| 1,200 | 0.86 [0.72, 0.94], 37 | 0.84 | 6.29 (0.851) | 2 / 22 | +0.53 [-0.54, +1.59], 16 | -0.39 | +0.41 |
| 1,800 | 0.88 [0.72, 0.95], 32 | 0.73 | 6.25 (0.837) | 2 / 22 | +0.63 [-0.37, +1.63], 13 | +0.34 | +0.35 |
| 3,600 | 0.93 [0.77, 0.98], 27 | 0.61 | 5.90 (0.823) | 3 / 22 | +0.53 [-1.02, +2.07], 8 | +1.34 (n 9) | +0.43 |

M at least 1.5 pts gives the same shapes:
* A: 0.84 -> 0.85 up to 600 s, 0.93 at 3,600 s.
* B: 1.00 -> 0.94 at 300 s, 0.86 at 3,600 s.
* C: OOS +1.25 [+0.25, +2.25] at x 0-30 s, +1.10 at 120 s, +0.78 at 300 s, +0.61 at 600 s, +0.06 at 3,600 s.
* IS: -0.26 to -0.65.

**Reading the curves.**
* **A (signal success) is flat from 0 to 600 s.** Waiting does not raise it. It rises to 0.93 only by 1 h, and by then
  39% of events have been dropped as unstable and 18% of the gap has gone.
  * The NOISE moves (16%) are not the ones that wobble within minutes. They fade over hours.
  * Requiring Polymarket and Kalshi to agree is the real noise filter.
* **B (beaten by other bots): nobody else takes SIG's stale side.**
  * In only 1-3 of the 22 events covered by the SIG tape did any other trader take the stale touch in the direction of
    the outside move, from the move's start to x. One of them was Texas Senate: 30k shares before our detection. Our
    own fills are excluded.
  * Only 8 of the 22 event windows (±1 h) had any SIG print at all.
  * Touch depth at detection was recorded on just 3 MM-quoted legs: 1.1k-3.8k shares, and none of it was taken by x.
  * What erodes the gap is the DMM's requotes toward the fair: under 1% by 60 s, 4% by 120 s, 9% by 300 s, 11% by
    600 s and 18% by 1 h.
* **C (net EV) is flat from 0 to 60 s, then falls about 0.2 tick at 120 s and about 0.6 tick at 300 s.** It is no longer
  distinguishable from zero after 300 s.
  * The model EV = P(real | x) · κ · gap(x) - P(noise | x) · adverse - cost uses parameters calibrated in-sample at
    x = 0: κ = 0.33 of the available gap captured on REAL events, adverse about 0 for NOISE events (SIG simply does not
    move on them), cost 1.22 ticks.
  * Applied to the OOS P(real) and gap, it predicts the same shape at a lower level (+0.77 falling to +0.45 at 600 s).
  * The in-sample period earned about 0 at every x.

**Optimal x and its uncertainty.** A race-clustered bootstrap (2,000 draws) took the argmax of the OOS EV curve.

| M | x = 0 | x = 60 s | x = 120 s | other |
|---|---|---|---|---|
| 2 | 76% of draws | 18% | 6% | under 0.5% |
| 1.5 | 67% | 20% | 13% | - |

* **Optimal x = 0 s, with a 95% range of 0-120 s.** Every x up to 60 s is within 0.03 tick of the best.
* With the current 60-120 s direction poll the bot already sits inside that range. Building a websocket would gain at
  most 0.02-0.2 tick per share.

## 11. Implementation spec (capped trial of the x = 0 fast follow; not implemented here)

The rule clears the strict bar by a small margin. Under the double-check rule, this needs a failure-scenario test, the
botctl precheck and a Fable review before it is wired.

* **Purpose tag.** `catchup`, its own sleeve, so its P&L can be read separately.
* **Universe.**
  * D legs (the R leg where a race has none) with both Polymarket and Kalshi mapped.
  * Mid in [0.03, 0.97]. SIG spread at most 2 ticks at entry.
* **Signal**, evaluated on every direction poll.
  * F = mean(Polymarket mid, Kalshi mid), with both fresh (at most 2 poll intervals old) and agreeing within 4 pts, at
    both now and 60 min ago.
  * F(now) - F(60 min ago) is at least 2 pts in direction d.
  * Polymarket and Kalshi each moved at least 1 pt in direction d.
  * SIG mid now minus SIG mid 60 min ago is less than 1/3 of the outside move, signed.
  * Gap (F - SIG mid) · d is at least 2 ticks.
  * **No stabilisation wait** (x = 0).
  * At most one signal per race per 2 h, and one open `catchup` position per race.
* **Entry.**
  * Limit order at SIG's touch: buy YES at the ask for d > 0, sell YES (buy NO) at the bid for d < 0. Never walk past
    the touch.
  * Placed within 5 s of the poll that detected the signal.
* **Size.** q = min(600 / price paid, 0.8 x the touch, 2,000 shares). Skip if q < 200.
* **Partisan cap.**
  * Count the trade's effect on netD. While netD is at or above 4.8k, take only R-adding signals; that is about 40% of
    them.
  * Never let the trade push netD above 5,000.
* **Exit.**
  * Close when SIG's mid has moved 70% of the entry gap in direction d.
  * Otherwise close at the 12-h time stop.
  * Close at once if Polymarket and Kalshi disagree by more than 4 pts, or if F falls back below 30% of the move.
    These two exits were not backtested; they are safety exits.
  * All exits are taker at the touch.
* **Caps.**
  * At most 4 open `catchup` positions, so at most 2.4k cost.
  * The trial's notional comes from free cash above the 20k reserve and never from arb sets or the punt reserve.
* **Kill conditions.** Any one of these disables the sleeve.
  * Realised P&L since start at or below -100 S.
  * After 10 or more closed trades, mean T/T below 0.
  * Any fill priced beyond the touch.
  * Feed staleness: no new outside data for 5 min. This pauses new entries; existing positions keep their exits.
* **Review.** After 20 closed trades, or on 31 Oct, whichever comes first:
  * keep it if mean T/T is at least +0.5 tick and the R-adding subset is positive;
  * otherwise remove it.
* **End of competition (cash objective).**
  * No new entries after 2 Nov 23:30 ET, so 12-h stops land by 11:30 ET on 3 Nov.
  * At 11:30 ET on 3 Nov, flatten any open `catchup` position at the touch.
  * **All `catchup` positions closed by 12:00 ET on 3 Nov.**
  * The trial ends then and does not draw on punt cash.
* **Expected value.**
  * At the cap: +15-40 S/day, of which about +16 S/day survives the partisan cap.
  * About 9 days to 2 Nov gives roughly +100-300 S expected.
  * The kill switch bounds the loss at about -100 S plus open positions.
  * Robust case (ex-top-3, m +0.46): about +1.7 S per trade, so about +5-8 S/day.

## Caveats

* **Fine-data x-study (section 10).**
  * There are 44-66 events in total and 25-35 out-of-sample trades per cell, and the top 3 races (VA-05, FL-07,
    MN-01) carry 69-85% of the out-of-sample P&L.
  * The Kalshi 1-min candles are forward-filled across minutes with no book change.
  * Before 21 Oct 22:43, the SIG side in A is the 5-min candle close, not a live touch.
  * Touch depth was recorded for only 3 of the events.
  * Over 120 variants were tried across this note. Read the t-statistics against that number.

* **Small samples.** n is 2-25 per cell, and B is 1.9 days dominated by one day (22 Oct), so t-statistics on OOS2 are
  fragile. The pooled OOS is 6.8 days.
* **Set A's mid** is the last trade (bid/ask bounce of about ±0.5 tick). That adds noise, not bias (see
  `directional_impact.md`).
* **A's outside data.** A uses Polymarket 5-min bars, so stability tests in A see a smoother outside series than B's
  2-min polls.
* **Our own flow in B.** B contains our direction sleeve's flow (from 22 Oct 00:42 UTC), which pushes SIG toward the
  outside by 0.1-0.3 tick per take. OOS2's catch-up is therefore an upper bound.
* **Bot downtime.** About 4.4 h on 22-23 Oct leaves holes in B. Exits in a hole use the first quote after it (at most
  2 h late).
