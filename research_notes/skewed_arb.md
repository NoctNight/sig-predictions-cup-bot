# Skewed arb: does one leg do the reverting, and should the high-frequency arb lean on it? (research, 23 Oct 2026)

Read-only study. Nothing under `sigbot/`, `config.json`, `params/`, `tests/` or `deploy/` was touched; no orders; **0 venue
reads** (local data only). Script `research_notes/skewed_arb_study.py` (about 50 s; `--figures` redraws); tables in
`data/research_skewed_arb/tables.md`; figure `research_notes/figures/skewed_arb_decision.png`.

**Hypothesis (Sizhe).** In a two-leg race priced x / 1-x, when the overround (sum of YES prices - 1) moves back toward its
normal level, the move is not split equally. For example, the favourite rises more than the underdog falls. If the
asymmetry is reliable, skew the high-frequency arb set toward it and repeat often.

## Bottom line

* **No reliable asymmetry. Verdict: drop.** Not even paper-trading is worth it.
* **Training days and test days disagree on which leg moves.** After the gap widens by 4+ ticks, the favourite does 44%
  of the 6-h fall on training days (proxy prices) and 86% on test days (real quotes). The directional part dq, defined
  below, is +0.10 tick (t +0.5) on training and -0.85 tick (t -2.1, n 26, 18 races) on test. The sign flips, and no
  primary test survives a multiple-testing correction.
* **The best skew picked on training does not hold on test.** It is 1.5 NO on the leg that did *not* jump and 0.5 NO on
  the other. It added +0.52 tick per set on training (t 2.1). On test it added **+0.32 (t 0.5, 20 trades)**. Rebuilt on
  raw trade prices instead of the 30-min smoothed proxy, the training edge halves (t 1.3). The smoothing manufactures
  "the jumped leg keeps rising".
* **The skew costs real risk.** It raises the standard deviation per set from 2.0 to 3.2 ticks. It adds 1,600 of
  one-party exposure per 4,000-set race. The peak net D exposure would have been 11.2k on training days and 4.8k on
  test days, against the 5,000 partisan cap that is shared with the direction sleeve and the market maker.
* **"Doing it often" would diversify, but there is nothing to diversify.** The cross-race correlation of 6-h D-leg
  changes on SIG is about 0.005, and concurrent skews in different races correlate at 0.01 (training) and -0.08
  (test). National swing is not what kills this idea; the missing edge is.
* **One consistent cell, but it is not a skew signal.** When the favourite is above 0.95, it drifts down against the
  underdog, by about 0.4 tick per 6 h in both panels. Almost all of these favourites sit at 0.95-0.97, which is SIG's
  practical ceiling (max seen 0.978), and only 4 of the 20 test-day trades fall in this band. On test days, three races carry it
  (FL-20, Hawaii Governor, Wyoming Senate); without them it is -0.13 (t -1.9). It is a slow unconditional drift near
  the ceiling, not a split of the reversion, and there is no capacity to exploit it. It is the opposite side of
  favourite_drift.md's settlement edge (SIG favourites are underpriced to settlement), so it should not become a
  standalone trade either.

## Definitions

* Tick = 0.005. Mid of a leg = (bid + ask) / 2.
* **Gap** (mid overround) o = mid_fav + mid_dog - 1.
* **Deviation** = o minus the race's trailing 24-h median, which needs at least 12 h of samples (as in
  arb_convergence.md).
* Over a horizon h:
  * Δfav = Δsum/2 + dq and Δdog = Δsum/2 - dq, so **dq = (Δfav - Δdog)/2** is the directional part of the move.
  * A positive dq means the favourite gained against the underdog.
  * "Favourite's share" = mean Δfav / mean Δsum.
* **Skewed set:** 1+s NO on leg L and 1-s NO on the other leg R.
  * It trades the same number of contracts as a plain set, so it pays the same spread.
  * Its P&L is the plain set's P&L + s·[(bid_L,0 - ask_L,1) - (bid_R,0 - ask_R,1)], which is about -2s·dq_L, where
    dq_L is dq taken from leg L's side.
  * The extra one-party notional is s per set. The payout swing between outcomes is 2s per set.
* **Jump leg:** the leg whose mid is furthest above its own 24-h median at entry.

## Data

| panel | role | period (UTC) | prices |
|---|---|---|---|
| A | training | 14 Oct 00:00 - 21 Oct 22:15 | 5-min trade close per leg, 30-min trailing median, ± the leg's median half-spread (arb_convergence proxy) |
| B | test | 21 Oct 23:50 - 23 Oct 22:50 | real touch from `venue_quotes` (venue='sig'), points more than 10 min after the last poll dropped |

114 two-leg races. Episodes start at the first 5-min point where the deviation crosses the threshold, with at most one
new episode per race per hour. Clustering is by race (t race) and by sign-flip permutation over races (perm p). Day
clusters are reported only on A, since B has 2-3 days.

## 1. Who does the reverting

Selected rows (the full table, including the buy-side deviations ≤ -2 / -4, is in `tables.md`):

| panel | episodes | h | n | mean Δsum | fav share | mean dq | t race | perm p |
|---|---|---|---|---|---|---|---|---|
| A | dev ≥ +2 | 1 h | 1,340 | -0.40 | 35% | +0.06 | +2.5 | 0.013 |
| A | dev ≥ +2 | 6 h | 1,300 | -1.03 | 44% | +0.07 | +1.1 | 0.29 |
| A | dev ≥ +2 | 24 h | 1,145 | -1.47 | 45% | +0.07 | +0.4 | 0.67 |
| A | dev ≥ +4 | 1 h | 300 | -0.62 | 47% | +0.02 | +0.3 | 0.78 |
| A | dev ≥ +4 | 6 h | 277 | -1.64 | 44% | +0.10 | +0.5 | 0.60 |
| A | dev ≥ +4 | 24 h | 231 | -2.37 | 34% | +0.38 | +1.2 | 0.24 |
| B | dev ≥ +2 | 1 h | 126 | -0.55 | 66% | -0.09 | -1.0 | 0.33 |
| B | dev ≥ +2 | 6 h | 124 | -1.11 | 59% | -0.10 | -0.6 | 0.55 |
| B | dev ≥ +4 | 1 h | 30 | -0.85 | 102% | -0.44 | -2.3 | 0.040 |
| B | dev ≥ +4 | 6 h | 26 | -2.35 | 86% | -0.85 | -2.1 | 0.043 |
| B | dev ≥ +4 | 24 h | 10 | -3.75 | 75% | -0.92 | -0.8 | 0.46 |

**By favourite band** (dev ≥ +2, 6 h, dq in ticks):

| panel | 0.50-0.70 | 0.70-0.85 | 0.85-0.95 | > 0.95 |
|---|---|---|---|---|
| A | +0.06 (t +0.6) | +0.29 (t +2.4) | +0.07 (t +0.6) | -0.81 (t -2.4) |
| B | -0.51 (t -1.2) | -0.15 (t -0.3) | +0.17 (t +0.6) | -0.60 (t -0.9) |

Unconditionally (every race, every hour), favourites at 0.50-0.85 gained +0.3 to +0.5 tick per day against their
underdog on A (t about 2-2.7). On B the sign is mixed. Favourites above 0.95 lost -0.6 tick per 6 h on A (t -4.7) and
-0.46 on B (t -2.3).

**Multiple testing.**

* Stage 1 has about 190 conditional cells and 24 unconditional cells. The primary family is panel A, deviation ≥ +2 or
  ≥ +4, 3 horizons: 6 tests. Its best p is 0.013, which is 0.08 after Holm correction. Nothing in the primary family is
  significant.
* The one B cell with p < 0.05 (dev ≥ +4) has the opposite sign to A, so it cannot confirm a training choice.

## 2. Mechanics

* **Underdog tick floor: not a factor.** No underdog bid in the > 0.95 cells is below 0.01. Excluding underdogs at 4
  ticks or less changes nothing (table column "dog > 4 ticks").
* **Favourite ceiling: this is the > 0.95 effect.** 88-98% of those points are at 0.95-0.97 and the highest favourite
  mid seen is 0.978. A favourite in the band has 1-4 ticks of room up and many ticks down, and band membership is chosen
  on a noisy mid.
  * On A, all 19 races drift down.
  * On B, 13 of 17 races drift down, and without FL-20, Hawaii Governor and Wyoming Senate the mean is -0.13 (t -1.9).
* **Last-trade bounce and smoothing in the proxy.** Bounce would make legs revert, but what A shows is momentum: the
  jumped leg keeps rising (jump = favourite, dev ≥ +4, 6 h: dq +0.82, t 3.7). That comes from the proxy's 30-min
  trailing median, which is still catching up with a step when the episode starts.
  * Rebuilt on raw 5-min closes, the jump-leg effect at 1 h is +0.00 / +0.12, and at 6 h it is +0.44 (t 2.5) for a
    favourite jump and -0.22 (t -0.9) for an underdog jump.
  * On the real-quote panel B, the jump effect has no consistent sign: favourite jumps revert (-0.83 at 6 h) and
    underdog jumps continue (-0.86).

## 3. Backtest: plain vs skewed high-frequency arb, same entries

* **Rule (arb_convergence.md section 7):** buy the sell-set when the bot's thresholds hold and the deviation is ≥ +4.
  Exit at a +2-tick round trip, a free unwind, or after 24 h. One set per race at a time, with 30 min between trades in
  the same race.
* **Costs:** spread both ways at the touch, slippage of 0.1 or 0.25 tick per contract (4 contract-trades per set), and
  20% of entries dropped at random (basket rejections). 90% ranges come from a race-cluster bootstrap.
* **Trades:** 155 on A (94 reached +2 ticks, 39 hit the 24-h limit, 22 were still open at the panel's end) and 20 on B.
* **Candidate skew rules, from the stage-1 signs:** more NO on the favourite; more NO on the underdog; the band rule
  (underdog unless the favourite is > 0.95); the non-jump leg. Each with s ∈ {0.1, 0.25, 0.5, 1}.
* **How s was picked:** on A, by profit per unit of risk. The mean alone always picks the largest s, because it is
  linear in s.

Ticks per set, slippage 0.1:

| rule | s | A plain | A skewed | A extra (t) | B plain | B skewed | B extra (t) | B skewed, slippage 0.25 |
|---|---|---|---|---|---|---|---|---|
| more NO on favourite | 0.5 | +0.25 | -0.09 | -0.34 (-1.5) | +0.15 | +0.87 | +0.72 (+1.2) | +0.27 |
| more NO on underdog | 0.5 | +0.25 | +0.59 | +0.34 (+1.5) | +0.15 | -0.58 | -0.72 (-1.2) | -1.18 |
| band rule | 0.5 | +0.25 | +0.58 | +0.33 (+1.4) | +0.15 | +0.37 | +0.22 (+0.4) | -0.23 |
| **non-jump leg (chosen)** | **0.5** | +0.25 | **+0.77** | **+0.52 (+2.1)** | +0.15 | **+0.47** | **+0.32 (+0.5)** | -0.13 |
| non-jump leg, raw-close training | 0.5 | +1.05 | | +0.20 (+1.3) | | | | |

* **The training choice does not survive.** Its t of 2.1 is about p 0.14 after correcting for 4 rule families, and the
  test gain has t 0.5 on 20 trades. The favourite/underdog rules flip sign between panels. At 0.25 slippage every
  variant on B is about zero or negative except "more NO on the favourite", which had lost on training.
* **Risk** (s 0.5, chosen rule):
  * Standard deviation per set rises from 1.99 to 3.18 ticks (A).
  * Extra one-party exposure is s × 4,000 × 0.8 = 1,600 per race. The peak net D notional of the overlays was 11.2k on
    A and 4.8k on B, against the shared 5,000 cap.
  * Worst day at 4,000 sets per trade: the overlay lost -252 on 14 Oct (A); the plain sleeve's worst day was -667 on
    21 Oct (A).
  * On B the overlay made +48 and +56 per day, which is about +0.5k to 1 Nov if it were real. It is well inside the
    noise.
* **Diversification:** the mean pairwise correlation of 6-h D-leg changes across races is 0.006 (A) and 0.005 (B).
  Overlay P&L between concurrent trades correlates at +0.01 (A, 1,664 pairs) and -0.08 (B, 117 pairs). Repetition would
  average the noise away quickly, if there were a mean to find.

## 4. Verdict

**Drop. Do not skew the high-frequency arb.** No skew rule chosen on training holds on real quotes:

* the favourite/underdog split flips sign between panels;
* the jump-leg effect is mostly a smoothing artefact of the proxy;
* the one consistent cell, favourites above 0.95, is a ceiling drift with almost no capacity in the sleeve.

The skew would add roughly 60% more per-set standard deviation and up to about 11k of partisan exposure for an expected gain of
0 to +0.3 tick per set. Keep the plain 1:1 sell-set.

**Revisit only if** a week of real quotes (from about 30 Oct, with at least 60 sleeve trades) shows the non-jump skew at
t > 2 on data it was not chosen on. That week would end too close to the 1 Nov entry cut-off to matter, so in practice
this is closed.

## Limitations

* The test panel is short: 2 days and 20 sleeve trades. Its 24-h horizon has only 10 episodes.
* The training panel is a proxy, with level noise of ±1.1 ticks of the sum and 1-h change correlation of 0.05 with real
  quotes (arb_convergence.md). 1-h training results are unreliable.
* The backtest is per set. It has no capital ranking or per-race cap interaction, fills at the logged touch, and
  assumes no partial basket fills.
