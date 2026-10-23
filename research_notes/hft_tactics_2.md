# HFT tactics, round 2: five new candidates (23 Oct 2026)

Read-only study. Nothing under sigbot/, config.json, params/, tests/ or deploy/ was touched. No orders were placed and no
venue calls were made (0 of the 30 allowed reads). Reproduce with `<venv with matplotlib>/bin/python
research_notes/hft_tactics_2_study.py`. It writes `data/research_hft2/results.json` and
`research_notes/figures/hft2_decision.png`. One tick is 0.005, and S means SUSQies.

**Data.**
* **Tape:** 79.4k public prints with the aggressor side, 21 Oct 23:49 to 23 Oct 22:38 UTC. 2,401 of our own fills were
  removed.
* **Mids:** the `quotes` table (136 MM legs, ~35 s) merged with `venue_fair` (228 legs, ~85 s, plus the outside fair).
* **Our fills:** 753 mm, tilt and inside fills, joined to `orders` to get each order's age at fill.
* **Recordings:** two full-feed recordings, 64 min in all (`feed_capture_20261022.jsonl` and `rec_20261022_1941.jsonl`).
* **Candles:** 5-minute candles, 6-21 Oct, for the cross-market test.

**Split and statistics.**
* Training is before 23 Oct 00:00 UTC (1.0 day) and test is after (0.94 day). The candle test trains on 6-14 Oct and tests
  on 15-21 Oct.
* t-statistics are clustered by market (by race pair for E).
* About 30 statistics were run, so treat |t| < 3.1 as noise (Bonferroni at 5%).

## Verdicts, ranked

| # | candidate | hypothesis | key result (training / test) | net of spread | feasible S/day | verdict |
|---|---|---|---|---|---|---|
| A | DMM requote clock and inventory skew | the DMM requotes on a timer, after N shares, or skews after it trades | **Timer:** none. Second-of-minute Rayleigh p 0.29; chi-square on six 10-s bins 11.9 (5 df, p 0.04, n 21, not significant after correction). **Batches:** 24% of requotes have another market requoting within 2 s, vs 0.2% when shuffled (permutation p < 0.005). **Volume:** the requote hazard rises 10x after traded volume (0.06% to 0.65% per minute). **Direction:** toward the outside fair 55% (z 1.5) / 66% (z 3.2); with recent flow (inventory skew) 52% / 54% (z < 0.7); repeats the last requote 36% / 47% | 344 requotes over 2 days; 19 of the 21 in the recordings were 1 tick. A taker who calls a 1-tick requote right nets 1 - spread <= 0 | taker 0. Pulling MM quotes ahead of a batch: < 10 (our MM trades ~20k shares/day). The direction signal is already in the outside-fair gate | **drop** |
| B | Large-taker footprints | after 5k+ shares on one side in 60 s, the price continues or reverts | **Impact:** +0.52 / +0.83 tick. **Continuation at 30 min:** +0.30 (t 3.6) / +0.06 (t 0.9) | **Follow, 30 min:** -0.95 / -1.18 tick per share. **Fade, 30 min:** -1.55 / -1.31. All horizons (5, 30, 120 min) have t < -4, n 221 / 291 | about **-550** if traded (300 shares x ~270 events/day) | **drop** (confirms bot_exploits 2.6) |
| C | Time of day | quoting pays more in some 3-hour ET slots | The resting side of public prints earns +0.08 to +0.53 tick at 30 min in every slot. **Slot ranking:** not stable (Spearman training vs test -0.14, permutation p 0.65). **Rule test:** the training-best 4 slots minus the other 4, on test data: -0.003 tick (t -0.04) | 0 | 0 | **drop**. Weekends are untested (the tape is Wed-Fri) |
| D | Queue age of our resting orders | old orders get picked off (stale), so refresh them | **30-min markout, old (2+ min) minus young:** -0.01 (permutation p 0.96) / +0.06 (p 0.70). Orders resting 10+ min do best in test (+0.37, t 2.8). Median age at fill: 202 s | 0 | 0, and a refresh rule would spend writes and lose queue place | **drop**: keep old orders |
| E | Cross-market consistency | **Pairs:** after one race in a state moves 2+ ticks in 30 min, the other race in that state follows. **National factor:** the other legs' average move predicts a leg's next move | **Pairs, candles:** +0.02 (t 0.4, n 266) / 0.00 (n 462). **Pairs, live quotes:** n 27, no effect. **National factor:** -0.07 (t -3.0) / -0.01 (t -0.2), the opposite sign and not repeated. SIG has no chamber-control market to price against the seats | 0 | 0 | **drop** |
| - | Latency arbitrage, REST vs feed | the feed or REST view is stale long enough to pick off | Not re-tested. The DMM's staleness lasts minutes and nobody else races for it (hft_strategies row 8; outside_stable_sweep: 1-3 of 22 events taken). Up to ~1 s of venue-side latency changes nothing | - | 0 | **drop** |

## What this adds

* **No new HFT edge clears its costs.** Each candidate either fails out of sample (C, D, E) or cannot pay a 1-tick
  spread on 1-tick moves (A, B). The resting side earns about +0.3 tick a share at 30 min in every hour (C). That is the
  live MM ladder, and its limit is queue share, not timing.
* **The DMM is event-driven, not clock-driven.**
  * It requotes in cross-market batches.
  * It requotes more after volume.
  * It leans toward the outside fair (66% in test).
  * There is no timer, and no requote is predictable before it happens.
  * A batch-start alert could pull our quotes, but it is worth < 10 S/day at today's MM volume. Not worth building
    before 3 Nov.
* **Big takers are slightly informed in training** (+0.30 tick of continuation at 30 min), and not in test. Neither
  side of their trade is worth taking as a taker.

## Caveats

* **About two days of quotes.** The time-of-day test has no weekend. The direction hit rate in A rose from training to
  test, and may simply be the regime shift noted in `directional_impact.md` (SIG converging to the outside fair).
* **Requotes in the quotes table** are detected at ~35 s resolution, so 1-tick requotes that reverse within 35 s are
  missed. The burst test uses the 64 minutes of full-feed recordings (21 requotes only).
* **Mids are forward-filled** with a staleness of at most 300 s. Candle closes are last trades, which adds noise (bid-ask
  bounce) to E but no bias.
