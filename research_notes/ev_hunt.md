# EV hunt: positive-EV trades we are not taking yet (24 Oct 2026)

Read-only study. No code, config or params changed, nothing committed or deployed, **0 SIG API reads**, `.sig_api_key`
never read. `data/sigbot.sqlite` opened read-only (`mode=ro`, no copy). Public Polymarket/Kalshi APIs only, for part 3.

* Script: `research_notes/ev_hunt_study.py` (modes `part1`, `part2`, `part2_real`, `part3`, `part3_apply`, `figs`). Run it
  through `deploy/simq.sh` with a Python that has numpy/pandas/scipy/matplotlib (no system Python has them; a throwaway
  venv was used).
* Calibration data fetch: `research_notes/ev_hunt_calib_fetch.py` -> `data/research_ev_hunt/calib/`.
* Numbers: `data/research_ev_hunt/part*.json`.
* Figures:
  * `research_notes/figures/ev_hunt_reversion.png` (part 1)
  * `ev_hunt_sweep.png` (part 2)
  * `ev_hunt_calibration.png` (part 3)
* One tick = 0.005. S = SUSQies. Bar: out of sample, positive, t > 2, net of spread and our impact, inside the hard limits,
  adds liquid cash by 3 Nov evening (riskless mechanisms qualify without the t-stat).

## Verdicts

**One small GO, and it is execution, not a new trade:** time the sales the wind-down must make anyway (2c). It adds
**about +80 S (+50 to +110)** on today's holdings and passed the Fable adversarial check with changes. Everything else is
a DROP or confirms what the bot already does.

| # | candidate | result (test half) | decision |
|---|---|---|---|
| 1a | fade SIG-only moves (DMM overshoot) | -1.1 to -2.7 ticks/share, t -3 to -12, every horizon | DROP (moves stick) |
| 1b | follow SIG moves toward the outside price | +0.4 at 12 h (t 0.5) | DROP |
| 1c | cross-race basket reversion | basket SD 0.16 tick | DROP |
| 1d | buy-set side of the HF arb | 5 test trades, +1.6 (t 1.6) | DROP (too few) |
| 1e | reversion after our own fills | taker impact persistent; MM decay not significant | DROP (patience buys depth, not the touch) |
| 2a | hold raw gaps 3-10 days to 3 Nov | -0.25 / +0.6 on one price source; the apparent +3 to +6 is a candle-to-quote artefact | DROP; re-test from ~27 Oct on real quotes |
| 2b | only stable, fast-reverting legs | 12-17 trades, no separation | DROP |
| **2c** | **timed selling of holdings (netting)** | **sets +0.40 tick/set (t 5.9), legs +0.13 (t 3.0), every 12-h block positive; placebos ~0** | **GO with changes (Fable)** |
| 3 | Polymarket/Kalshi recalibration | favourites underpriced and longshots overpriced; the shrunk map beats raw prices in 4/4 held-out cycles | no new trade; keep the MM gate on raw prices; the punt-screener map is confirmed; cheap-favourite edge ~2x at settlement |

Figures: `ev_hunt_reversion.png` (part 1), `ev_hunt_sweep.png` (part 2), `ev_hunt_calibration.png` (part 3).

## Part 1. More mean reversion

**Already covered elsewhere, not redone.**
* Large-taker follow/fade: hft_tactics_2 B, both lose 1-1.5 ticks.
* Time of day: hft_tactics_2 C and hf_mean_reversion section 2, no stable slot.
* Same-state race pairs: hft_tactics_2 E.
* Sweeps on the tape: hf_mean_reversion section 5.
* Gap deviation from the leg's own 24-h level: sig_convergence (half-life 1-1.7 days, test t 1.5).

**Data.** `venue_fair`: real SIG bid/ask plus the outside fair, every leg, every bot poll (~85 s), 21 Oct 23:49 - 24 Oct
13:11 UTC. Training (IS) is before 23 Oct 12:00 UTC; test (OOS) is after. t-statistics are clustered by race. Every rule
trades one poll after its signal and pays the touch.

### 1a. Fade a SIG-only move (the DMM "overshoot")
* **Hypothesis.** When SIG's mid moves 2+ ticks in 10 min while the outside fair is still (< 0.3 points), the move is
  liquidity or overshoot, and it reverses.
* **Test.** Fade at the touch, hold 30 min / 2 h / 6 h / 12 h, close at the touch.
* **Result: the opposite.** Fading loses at every horizon in both halves.

| SIG-only move | 30 m IS / OOS | 2 h | 6 h | 12 h | n IS / OOS |
|---|---|---|---|---|---|
| all, 2+ ticks | -1.40 / -1.25 (t -12 / -9) | -1.70 / -1.49 | -1.79 / -1.68 | -1.52 / -1.69 (t -5.7 / -2.9) | 243 / 93 |
| away from the outside price | -1.14 / -1.13 | -1.14 / -1.18 | -0.81 / -0.73 | -0.30 / -0.39 | 105 / 40 |
| toward the outside price | -1.59 / -1.34 | -2.14 / -1.71 | -2.62 / -2.36 | -2.58 / -2.72 | 138 / 53 |

SIG-only moves are DMM re-pricings that stick. The ones toward the outside price keep going.

### 1b. Follow a SIG move toward the outside price (the one lead from 1a)
* **Result.** 2+ ticks: 12 h IS +0.19 (t 0.5), OOS +0.41 (t 0.5); 30 min to 2 h is negative.
* 3+ ticks looked good in training (12 h +3.1, n 14) and lost in the test (-1.0, n 6).
* **Decision: DROP.** Noise. It also overlaps the catch-up trial.

### 1c. Cross-race baskets
* **Hypothesis.** The party-signed average deviation of all legs (a "national" SIG-vs-outside factor) wanders and reverts.
* **Result.** It barely moves: SD 0.16 tick (95th percentile 0.28) against 2.1 ticks for single legs, with reversion
  slopes -0.15 (1 h), -0.63 (4 h), -0.87 (12 h).
* **Decision: DROP.** Nothing to trade after a 1-tick spread per leg. Leg deviations are idiosyncratic; there is no
  common factor to fade.

### 1d. The buy-set side of the high-frequency arb
* **Hypothesis.** Live churn trades sell-sets only. Buy-sets (YES on every leg) could be churned when the mid overround is
  >= 4 ticks *below* its trailing 24-h median, exiting at +2 ticks or after 24 h.
* **Result.** 12 IS trades (-0.08 tick/set) and 5 OOS (+1.6, t 1.6; 4 of 5 hit the +2 take). Only 4 were below payout.
* **Decision: DROP.** Too few signals: ~3 a day across all races. Unlike sell-sets, a buy-set is not riskless (the
  third-party risk x).

### 1e. Reversion after our own fills
* **Hypothesis.** After our own fills the price comes back, so there is something to harvest (re-quote, or wait before
  exiting).

| fills | 1 m | 5 m | 30 m | 1 h | 3 h | 6 h | 24 h |
|---|---|---|---|---|---|---|---|
| MM fills: mid minus fill price, IS (n 310) | +0.62 | +0.17 | -0.23 | -0.41 | -0.61 | -0.43 | -1.36 |
| MM fills, OOS (n 87) | +0.53 | +0.30 | +0.04 | -0.01 | -0.06 | -0.36 | -4.3 (n small) |
| our taking fills: SIG mid move in our direction, IS (n 1,273) | +0.21 | +0.56 | +0.66 | +0.87 | +1.09 | +1.20 | +1.02 |
| our taking fills, OOS (n 100) | +0.03 | +0.18 | +0.76 | +0.65 | +0.27 | +0.94 | -1.30 |

* Our taking trades move SIG's mid our way by 0.5-1 tick, and the move **does not come back** within 24 h. Our impact is
  permanent (the DMM re-prices after volume; hft_tactics_2 A). Splitting the 3 Nov unwinds into slower clips therefore
  does not recover the touch: it only saves walking the book.
* MM inventory decays slowly after the first minutes: IS 3-24 h markouts are -0.4 to -1.4 tick (t -1 to -1.5). That is
  consistent with the param audit (median hold 3.7 h), not significant, and nothing to harvest.
* **Decision: DROP** as a trade. Useful fact for the cash raise: patience buys depth, not a better touch.

## Part 2. Sweeping toward the Polymarket/Kalshi fair

**Already covered, not redone.**
* Directional gaps: directional_impact, negative OOS.
* The cross-sectional gap model: sig_convergence part 2, dropped.
* Raw-gap and deviation trades at 1-24 h: sig_convergence, dropped. The raw gap is a per-leg offset; the deviation
  half-life is 1-1.7 days.
* Sweeping after an outside move: outside_stable_sweep; the live catch-up trial.

**Data.**
* The sig_convergence panel: SIG 5-min trade closes 1-21 Oct plus Polymarket/Kalshi same-party history, smoothed with a
  30-min median against bid-ask bounce, ± a fixed half spread per leg.
* Stitched to the bot's real quotes from 21 Oct 23:49.
* Long-horizon IS = entries 2-9 Oct; OOS = entries from 10 Oct.

### 2a. The cash objective: hold H days, sell before 3 Nov
* **Hypothesis.** The raw gap does not close in a day, but a big one may close over 3-10 days, which is all the cash
  objective needs.
* **Test.** Daily entries at 18:00 UTC: buy legs SIG prices >= 8 ticks under the fair (or sell legs >= 8 over). Sell H
  days later at the touch.
* **First pass looked like a GO:** +2.6 to +3.4 ticks/share net at 3-7 days, t 2.6-4.6 in both halves.
* **It does not survive the price-source check.** Split the OOS trades by where the exit price comes from:

| gap >= 8 ticks | 3 days | 5 days | 7 days |
|---|---|---|---|
| IS, entries 2-9 Oct (trade prices both ends) | +2.86 (t 3.0, n 33) | +2.83 (t 3.8) | +2.64 (t 2.6) |
| **OOS, both ends on trade prices (exit before 21 Oct 22:00)** | **-0.25 (t -0.4, n 134)** | **+0.58 (t 0.5)** | **+0.57 (t 0.3)** |
| OOS, exit on real quotes, entry on trade prices | +6.38 (t 7.3, n 101) | +4.90 | +4.77 |

* The OOS "edge" comes entirely from trades that enter on candle prices and exit on real quotes. A large candle gap is
  partly a stale or bounced trade price, and the real quote does not have it. That jump is an artefact, not convergence.
* On one consistent price source, mid-October entries earn about zero. Gaps >= 12 ticks: OOS +0.8 / +2.5 / +5.2 at
  3/5/7 days with t 1.4-1.8 (n 16-56). Not enough.
* **Decision: DROP.** Cheap favourites already holds the best of the favourite side through the call. Re-test only once
  real quotes span 5+ days (from ~27 Oct): use `venue_fair` alone, entries 22-24 Oct, exits 27-29 Oct.

### 2b. Only legs whose gap is stable and whose deviation reverts fast
* **Test.** In IS (1-9 Oct), legs with a below-median gap SD AND a faster-than-median 24-h deviation reversion: 13 of 69
  scored legs. Trade the deviation (>= 4 ticks from the leg's trailing 24-h median), 1 and 3 days.
* **Result.** Fast legs OOS: 17 trades at 1 day (-0.41, t -0.6) and 12 at 3 days (+1.92, t 1.6). All legs: 3 days +1.17
  (t 2.5), with the same stitch exposure as 2a.
* **Decision: DROP.** The filter leaves too few trades and does not separate from the rest. The 3-day number carries the
  2a artefact.

### 2c. Netting: time the sales we have to make anyway (candidate GO, execution only)
* **Hypothesis.** Under the cash objective nearly all of the ~101k positions are sold by 3 Nov 18:00 ET. The spread is
  paid either way. Selling a holding when it is rich against its own usual level, rather than evenly, captures part of
  the deviation without a round trip or any new position.
  * This is the "sell holdings rich vs fair instead of buying new" angle.
  * The cash raise (cash-raise-handover.md) sells "cheapest exit first" and has no timing rule.
* **Data.** Real quotes only (`venue_fair`, 21-24 Oct). No candles and no stitch.
* **Rule.**
  * In a 12-h or 24-h selling window, sell the holding at the first poll (+1 poll latency) where it is at least X ticks
    better than its trailing 12-h median. Else sell at the window's end.
  * Single legs: the gap to the fair; a YES holding sells at the bid, a NO holding pays the ask.
  * 2-leg sell-sets: the sum of mids; closing buys YES on both legs at the asks.
  * Benchmark: equal clips every hour at the touch over the same window.

| holding (12-h window) | X | training | test | every 12-h block |
|---|---|---|---|---|
| 2-leg sets | 1 tick | +0.35 tick/set (t 6.4, n 989) | **+0.40 (t 5.9, n 369)** | +0.31, +0.33, +0.41, +0.40 |
| 2-leg sets | 2 ticks | +0.31 (t 4.9) | +0.34 (t 4.4) | +0.26 to +0.34 |
| single legs | 2 ticks | +0.23 tick/share (t 5.2) | +0.13 (t 3.0) | +0.22, +0.24, +0.24, +0.13 |
| single legs | 3 ticks | +0.21 (t 5.2) | +0.13 (t 3.1) | |

* 24-h windows (training only, the data are too short for a test): sets +0.47 to +0.57, legs +0.28 to +0.42.
* Windows overlap (3-h step), so the honest count of independent periods is 4-5. Every one is positive.
* **Size.** Holdings in the 24 Oct snapshot: ~32k 2-leg sets, 12k 3-leg sets (not tested) and ~36k single-leg shares.
  * 32k sets x 0.4 tick ≈ +65 S.
  * 36k shares x 0.2 tick ≈ +35 S.
  * In all, **about +100 S (range +50-150)**. Small but riskless: no new position, the same sales at better moments.
* **Caveats.**
  * The test sells the whole holding at one poll; real sales go in clips at touch depth.
  * Our clips move the price about 0.5 tick for good (1e), which eats into the deviation.
  * 2.5 days of quotes may not represent election week.
**Independent adversarial check (Fable, 24 Oct): GO WITH CHANGES** (sets yes, legs marginal). Its scripts are
`adv_timed.py`, `adv_exec.py` and `adv_clips.py` in this session's scratchpad.

* **No look-ahead.** An independent re-implementation reproduces the result: sets +0.37 (X=1) / +0.32 (X=2), legs
  +0.21.
* **Placebos are clean.**
  * A random trigger with the same hit share gives +0.01 / -0.04 (sets) and +0.005 (legs).
  * But the sum of asks tightened from 3.5 to 2.5 ticks over the sample, so ~0.1 of the 24-h figure is trend, not
    timing. The 12-h figure is barely affected (+0.03).
* **Use the sum of ASKS as the set signal**, not mids: +0.40. It is what we pay; a bids-only signal gives +0.22.
* **Legs need the outside fair.** A SIG-mid-only signal gives -0.08. The leg edge is the known SIG-lags-Polymarket
  effect, and it halved out of sample (0.23 -> 0.13).
* **Independence.**
  * About 4 independent 12-h blocks, all positive (0.31-0.41).
  * 79% of 95 races are positive; the median race is +0.33; removing the top 5 races leaves +0.31.
  * The edge is in the races the MM quotes (+0.39); the other 15 races give +0.17 (t 0.9).
* **Execution: the key change.**
  * Robust to latency: +0.36 -> +0.30 at 14 min late -> +0.21 at 1 h late.
  * Touch depth at signal polls: median ~2,100 sets (25th percentile ~900). 25-40% of races need 2+ polls.
  * **An hourly-clip version earns only +0.05-0.08 per set**, because the signal fires within the hour only 30% of the
    time. The value comes from deferring clips for hours: carry-to-deadline earns +0.42-0.46 (t 9).
* The reviewer found ~0 mid impact after 254 of our taker fills (-0.02 to +0.06 tick at 1 poll to 30 min). Part 1e
  reports +0.5-1 tick on 1,373 fills (mostly arb sets, share-weighted). Either way, waiting does not get the touch back.
* **Honest edge:** sets +0.35/set (95% CI +0.25 to +0.45); legs +0.15/share (+0.05 to +0.25).
* **Value:**
  * On the 24 Oct holdings: 32k 2-leg sets ≈ +55 S, 36k leg shares ≈ +25 S, so **≈ +80 S (+50 to +110)**.
  * If the 12k 3-leg sets behave the same, about +20 more.
  * The reviewer's 110-200 assumed ~90k sets, which is too many.

**Decision: GO (execution change, small), for the main session to wire into the cash-raise / wind-down routine.**
* **Signal.**
  * Sets: sell a set's clip only while its **sum of asks** is >= 1 tick below its trailing 12-h median.
  * Single legs: only while the leg's SIG-minus-fair gap is >= 2 ticks richer than its 12-h median.
* **Sizing.** Each clip is capped at touch size, and the signal stays armed for the remainder.
* **Deferral.** Clips may be held back for hours, up to a hard deadline (e.g. 3 Nov 15:00 ET). After it, the current
  cheapest-first rule sells the rest.
* **Kill.** Switch the timing off if its hit share falls below ~50% per 12 h.
* **Scope.** It changes only *when* already-planned sales happen. It adds no position and no hard-limit risk.
* **Not ready to deploy.** This is a research result. Wiring it needs the usual gate: guard test, sim, precheck and a
  Fable code review.

## Part 3. Calibration of Polymarket/Kalshi against what happened

**Data** (`data/research_ev_hunt/calib/README.md`): 731 resolved legs in 314 races, extending the favourite_drift dataset.
* Polymarket:
  * 2020: 6 state markets created 28-29 Oct 2020, rebuilt from Polygon logs.
  * 2022: 70 legs from on-chain AMM trades.
  * 2024: 362 CLOB legs.
  * 2021, 2023, 2025, 2026: specials and governors.
* Kalshi: 2024 (opened October, so thin before 14 days), 2025-26.
* Not available:
  * Polymarket 2018: it did not exist.
  * Polymarket 2020 Senate: not in the index.
  * Kalshi 2022: no election markets.
* Main sample: SIG-like legs only. D or R legs of state/district presidential, Senate, governor and House races. Third-party
  legs, national-winner, control and mayoral fields are excluded. One venue per race (Polymarket preferred); off-years are
  pooled as one "2025" cycle.
* Horizons: 60, 30, 14, 7, 3, 1 days, eve (00:00 UTC on election day) and day (12:00 UTC).
* Per horizon: 139-187 races (276-366 legs); 2024 is ~60-70% of each.
* CIs resample whole races.
* Same race, Polymarket vs Kalshi at 7 / 1 days: mean absolute difference 2.0 points, mean -0.2 (no level gap); n 350.

### 3a. Reliability and the favourite-longshot bias
* **Compressed toward 50% at every horizon.** Logit-linear slope b (b = 1 means calibrated):

| horizon | 60 d | 30 d | 14 d | 7 d | 3 d | 1 d | eve | day |
|---|---|---|---|---|---|---|---|---|
| b, maximum likelihood | 2.04 | 2.16 | 2.24 | 2.54 | 1.99 | 1.60 | 1.67 | 1.83 |
| 95% CI (races resampled) | 1.4-4.1 | 1.6-3.9 | 1.7-3.7 | 1.9-4.9 | 1.5-3.1 | 1.25-2.3 | 1.3-2.4 | 1.4-2.5 |
| b, shrunk (prior a ~ N(0, 0.25), b ~ N(1, 0.25)) | 1.50 | 1.52 | 1.57 | 1.62 | 1.54 | 1.39 | 1.42 | 1.48 |
| a 0.80 favourite really wins (shrunk) | | | 0.89 | 0.89 | | 0.86 | 0.87 | |

* **Reliability (7 days / 1 day):**
  * Legs priced 0.05-0.15 won 0/31 and 0/27 (expected ~3 and ~2.5).
  * 0.85-0.95 won 32/32 and 26/26.
  * 0.95-1.00 won 76/76 and 81/81.
  * The middle differs by horizon:
    * at 7 days, 0.30-0.50 won 21% (priced 40%; CI of the gap -33 to -5 points) and 0.70-0.85 won 91% (priced 78%);
    * at 1 day, the middle buckets are on the diagonal (0.50-0.70 won 59% at 59%).
* **Sizhe's prior holds, one way round: longshots are overpriced and favourites underpriced.**
  * Buying 0.70-0.97 favourites and holding to resolution: +108 per 1,000 at 7 days (70/72 won vs 63.4 expected) and +83
    at 1 day.
  * Buying 0.03-0.30 longshots: -929 and -673.
  * By cycle at 7 days: favourites +106 (2022), +137 (2024), -94 (off-years, n 6).

### 3b. Out of sample by election cycle (leave one cycle out)
* **Test.** Fit on the other cycles, score the held-out one.
* **The shrunk map beats raw prices in every held-out cycle at every horizon:** 28 of 28 horizon x cycle cells have a lower log loss.
  Per race x 1,000:

| held out | 30 d | 14 d | 7 d | 3 d | 1 d | eve | day |
|---|---|---|---|---|---|---|---|
| 2024 (~120-133 races) | +38 (t 4.3) | +40 (t 8.7) | +47 (t 9.9) | +39 (t 5.0) | +25 (t 2.6) | +28 (t 2.9) | +34 (t 4.1) |
| 2022 (21-34 races) | +98 (t 4.1) | +54 (t 1.2) | +54 (t 1.0) | +37 (t 0.8) | +32 (t 1.0) | +36 (t 1.0) | +35 (t 0.9) |
| off-years (13-14 races) | +41 (t 0.8) | +4 | +24 | +69 (t 3.3) | +48 (t 3.5) | +57 (t 3.9) | +62 (t 3.9) |
| 2020 (6 races) | | | | +104 (t 2.2) | +57 | +63 | +87 |

* The raw (unshrunk) fit wins on 2024 and the off-years but loses on 2022 at 7-14 days. At 60 days, with 2024 held out, it
  blows up: the training cycles separate perfectly. **Use the shrunk map.**
* Brier scores move the same way. 2024 at 7 days: 0.044 -> 0.039.

### 3c. Caveats: the important one is party
* **Within a cycle the bias is partly one national polling miss.** Favourites priced 0.60-0.97, won / expected:

| | 2022 D | 2022 R | 2024 D | 2024 R |
|---|---|---|---|---|
| 7 days | 11 / 9.2 | 11 / 11.0 | 26 / 23.1 | 30 / 24.3 |
| 1 day | 11 / 8.8 | 10 / 11.6 | 27 / 28.2 | 29 / 23.6 |

* A week out, **both parties'** favourites beat their prices in both cycles: a genuine favourite-longshot bias.
* The day before, the excess is mostly the party the polls underrated (D in 2022, R in 2024). That direction is not
  predictable in advance. The eve map for 0.6-0.85 legs is therefore partly a bet on a polling miss of unknown sign.
* The tails (< 0.15 and > 0.85) hold in both parties and cycles.
* **Presidential states** (one national shock in 2024) dominate. Without them b is 2.15 (7 d) / 1.44 (1 d); shrunk 1.48 /
  1.28.
* **Effective sample is small.** Outcomes within a cycle share a shock (favourite_drift: effective n about 27-37 of 47).
  The race-clustered t-statistics in 3b are optimistic within 2024. The leave-one-cycle-out sign agreement (4 of 4
  cycles) is the robust evidence.
* **Regime.**
  * 2024 was a high-liquidity Polymarket year.
  * 2022 is thin AMM data (16 of 70 legs under $5k volume).
  * Kalshi 2024 opened in October.
  * 2026 venues are deeper, and arbitrageurs have read this literature, so the bias may be smaller now.
* **Same-terms gap.** Polymarket and Kalshi differ by 2 points per leg on average, so fair-blend noise is about the size of
  the shrunk adjustment for 0.90+ legs.

### 3d. What a recalibrated fair would do to the bot (latest poll, 211 legs)

| map | mean shift | 0.05-0.15 legs | 0.15-0.30 | 0.70-0.85 | 0.85-0.95 | MM gate flips | cheap-fav routes (YES+NO) | cheap-fav edge/share |
|---|---|---|---|---|---|---|---|---|
| none (today) | 0 | | | | | | 73 | 0.037 |
| 7-day shrunk (a -0.02, b 1.62) | 12 ticks | -14 | -21 | +21 | +13 | 73 legs | 159 | 0.064 |
| eve shrunk (a 0.00, b 1.42) | 9 ticks | -11 | -15 | +15 | +11 | 72 legs | 149 | 0.059 |

* **MM gate: do not recalibrate.**
  * The gate protects 30-min to 6-h marks.
  * SIG follows the Polymarket/Kalshi *price* (29% copy in 10 min), not the true probability.
  * Under the cash objective the MM inventory is sold before the result.
  * A recalibrated fair would flip the gate on 73 legs in the direction SIG does not move. Keep the raw blend.
* **Cheap favourites: the edge roughly doubles at settlement (+3.7c -> +6.4c per share on its current routes). That
  supports the sleeve, but there is no new trade without cash.**
  * The sleeve holds through the call, so the calibrated value is the right one.
  * Its study already scored a 2022/2024 calibrated case (+1,166 vs +840 at 15k); this wider fit agrees.
  * Widening it to legs whose *recalibrated* fair is >= 0.80 (raw 0.72-0.80) would double the routes. But the 1-day
    evidence for 0.70-0.85 legs is the party-shock pattern above, and cash is ~1k free (positions 101k).
  * **Not a GO:** a wider basket needs the cash raise to sell sets at ~4% to fund +2.7c a share of settlement EV on legs
    that lose the whole stake 15-25% of the time. At most, rank existing routes by recalibrated edge.
* **Punt screener.** Its single map (a 0.14, b 1.37) matches the eve shrunk fit: 0.80 -> 0.885 vs 0.877, 0.90 -> 0.959 vs
  0.958. **Keep it.** The data back two of its rules:
  * no single longshot: legs under 0.15 won 0 of 106 the day before;
  * no haircut for 22-35c stage-1 legs: 0.15-0.30 legs won 17% at 22%, CI -17 to +11 points.
* **Decision: no new trade from part 3.** The bias pays only at settlement, and the cash objective sells before it. It
  helps only sleeves that hold through the call (cheap favourites, the punt), and both already use an equivalent map.
