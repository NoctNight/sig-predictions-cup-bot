# Does SIG move toward the Polymarket/Kalshi fair? (24 Oct 2026)

Hypothesis (Sizhe): the SIG market moves towards the Polymarket/Kalshi fair. Question: how much, how fast, and can we trade it?

Code `research_notes/sig_convergence_study.py` (`fetch`, `analyse`, `plot`). Data `data/research_sig_convergence/`
(`raw/` same-party outside history per leg, `panel.pkl`, `results.json`). Figure `research_notes/figures/sig_convergence.png`.
0 SIG venue calls. The study needs numpy and matplotlib; no system Python has them, so it was run from a throwaway virtualenv.

## Verdict

**The raw gap does not converge. It is a steady per-leg offset, mostly favourite–longshot: on SIG, favourites sit about 6 ticks
cheap and underdogs about 8 ticks rich.** At 24 h only 8–10% of an 8+ tick gap has closed, and gaps under 4 ticks *grow*
toward the leg's usual offset.

**The deviation from each leg's own trailing 24 h level does converge, slowly.** About 12% closes in 1 h, 19% in 4 h and
35–50% in 24 h, a half-life of ~1–1.7 days. Nearly all of it is SIG moving; the outside price barely moves.

**Trading it does not survive out of sample.** Raw gap: training +9.1 ticks/share at x ≥ 16, test −0.14 (t −0.3). Deviation:
training +3.8 at x ≥ 8, test +0.84 (t 1.5, n 25 trades, 18 races), after 1,680 group × rule tests.

Per category:
- **house_seat:** drop.
- **senate_seat:** drop (deviation rule +1.33 on 6 test trades: too few to judge).
- **governor:** drop.
- **chamber_control:** drop (nothing positive even in training).

Nothing earns live capital, especially under the cash objective: the trade needs a 24 h hold.

**Final (Sizhe, 24 Oct): drop everything, including paper trading.** A paper verdict would take ~6 of the remaining
~10 days to collect 40 trades. Its best-case upside is ~+100 in total, it needs 24 h holds, and it would need a new
`sigbot/` sleeve.

## Setup

- **Legs:** 194 legs from `tc_mapping.json` with terms "same" and no `*_late_settle` code (house 80, senate 57, governor 53,
  chamber 4). Each SIG leg is compared with the same party's own Polymarket token and Kalshi ticker (instruments from the
  bot's `venue_quotes`), never 1 minus the other party. `venue_fair.pm/kalshi` are overround-normalised, so they were not used.
- **Fair:** mean of the PM and Kalshi same-party mids, used only when both are fresh (PM ≤ 30 min, Kalshi ≤ 6 h
  forward-filled) and agree within 4 points. Five legs with no Kalshi data (the 2 chamber legs and 3 Senate legs) are PM only.
- **Sample A (1 Oct – 21 Oct 22:00 UTC):** SIG 5-min candle close, a trade price. PM prices-history at 5-min fidelity, and
  Kalshi 1-min bid/ask close, both fetched for this study. Grid points: 319k valid (27% of leg × time; SIG must have traded
  within 1 h). Training (IS) = before 17 Oct; OOS1 = from 17 Oct.
- **Sample B (21 Oct 23:49 – 23 Oct 22:46 UTC):** real SIG bid/ask and PM/Kalshi same-party mids per bot poll (~81 s);
  33k valid points. This is OOS2.
- **Gap:** g = SIG mid − fair, in ticks of 0.005. Price band is set by the fair: favourite > 0.7, underdog < 0.3.
- **Closure at h:** Σ sign·(g0 − gh) / Σ|g0| over all (leg, t0) with |g0| in the bucket. It splits exactly into a SIG part
  (−sign·ΔSIG) and an outside part (+sign·Δfair). SEs are clustered by race × day.

## 1. Raw gap: share closed (A + B pooled; SIG part / outside part)

| abs gap | n | 15 m | 1 h | 4 h | 24 h | 3 d | half-life |
|---|---|---|---|---|---|---|---|
| 2-4 | 60k | -0.00 | -0.01 | -0.06 | **-0.33** (SIG -0.30 / out -0.03) | -0.29 | never |
| 4-8 | 131k | 0.00 | 0.01 | 0.00 | -0.02 | -0.05 | never |
| 8-16 | 98k | 0.01 | 0.01 | 0.03 | 0.08 (0.07 / 0.01) | 0.10 | never |
| 16+ | 20k | 0.01 | 0.02 | 0.04 | 0.10 (0.11 / -0.01) | 0.17 | never |

Sample B alone (real touches), 8-16 ticks: 0.02 at 1 h, 0.07 at 4 h, 0.22 at 24 h. The direction is the same and the
magnitude is a little larger.

## 2. Is the gap a level? Yes

- Each leg's mean gap explains **86%** of the gap variance in A.
- Per-leg mean gap correlates across periods: IS vs OOS1 r = 0.91 (n 144 legs), IS vs B r = 0.89 (n 182), OOS1 vs B
  r = 0.91. The slope from IS to B is 0.77, so the offset shrinks by roughly a quarter over a week.
- Mean gap by band: favourites **−6.3** ticks (A) and −5.2 (B); middle +1.0 and +0.9; underdogs **+8.5** and +7.2. This is
  the favourite–longshot pattern already documented in `contract_rules.md`.

Deviation from the leg's trailing 24 h mean (frozen at t0), share closed (A + B):

| abs deviation | n | 15 m | 1 h | 4 h | 24 h | 3 d | half-life |
|---|---|---|---|---|---|---|---|
| 2-4 | 53k | 0.05 | 0.12 | 0.19 | 0.40 (SIG 0.37 / out 0.02) | 0.52 | 40 h |
| 4-8 | 17k | 0.05 | 0.08 | 0.13 | 0.34 (0.32 / 0.03) | 0.69 | 38 h |
| 8-16 | 1.7k | 0.05 | 0.10 | 0.24 | 0.50 (0.52 / -0.02) | 0.48 | 24 h |
| 16+ | 50 | 0.25 | 0.35 | 0.73 | 0.90 | – | 1.7 h (n tiny) |

Sample B alone, 2-8 ticks: 0.02–0.06 at 1 h, 0.10–0.15 at 4 h, 0.35–0.37 at 24 h. The outside part is ≤ 0 throughout. The
15-min closure in A (0.05) is partly bid-ask bounce in trade prices; B shows 0.01–0.02.

By category × band (deviation ≥ 2 ticks, share closed at 1 h / 4 h / 24 h, half-life):

- **House:**
  - favourite: 0.13 / 0.18 / 0.33, half-life 35 h
  - middle: 0.11 / 0.16 / 0.31
  - underdog: 0.14 / 0.21 / 0.38, half-life 39 h
- **Senate:**
  - favourite: 0.11 / 0.20 / 0.45, half-life 29 h
  - middle (8 legs): 0.10 / 0.26 / 0.62, half-life 10 h
  - underdog: 0.08 / 0.15 / 0.43, half-life 34 h
- **Governor:**
  - favourite: 0.09 / 0.16 / 0.38, half-life 37 h
  - underdog: 0.10 / 0.13 / 0.38, half-life 39 h
- **Chamber (2 legs):** middle 0.12 / 0.25 / 0.49.

Category barely matters. The raw gap ≥ 4 ticks closes ≤ 0.1 at 24 h in every cell except house-middle (0.18).

## 3. Trading test

**Rule.**
- **Entry:** at SIG's touch when |x| ≥ X ticks, where x is either the raw gap or the deviation from its level. Buy YES when
  SIG is cheap; sell YES when it is rich.
- **Exit:** at the opposite touch after h, or once Y of x has closed (deviation measured against the level frozen at entry).
- **Positions:** one at a time per leg.

**Costs.**
- **Always:** the spread plus 0.5 tick per side.
- **Sample A only:** another half spread per side, for bounce in trade prices (conservative).

**Grid:** X ∈ {2, 3, 4, 6, 8, 12, 16}, h ∈ {1 h, 4 h, 24 h}, Y ∈ {50%, hold}, mode ∈ {raw, deviation}.

**Selection:**
- **Choosing the rule:** for each group (all, category, band, category × band), the rule with the best IS clustered t,
  subject to n ≥ 15 trades and ≥ 5 clusters. That makes 84 rules × 20 groups = 1,680 tests.
- **Checking it:** OOS1 (17–21 Oct, 4.9 d) and OOS2 (live touches, 2.0 d).

Every chosen rule was the 24 h hold, sold at half closed.

| group / mode | chosen X | train ticks/share (t, n) | test ticks/share (t, n, races) | X positive in training (t > 2) | test S/day, cash/day |
|---|---|---|---|---|---|
| all, raw | 16 | +9.11 (3.9, 107) | **−0.14** (−0.3, 81, 32) | 8 | 8.2k, −5 |
| all, deviation | 8 | +3.76 (5.2, 94) | **+0.84** (1.5, 25, 18) | 6 | 5.6k, +14 |
| house, raw / dev | 16 / 8 | +17.9 / +6.3 | −0.31 (n 16) / 0.00 (n 8) | 8 / 6 | ≤ 1.6k |
| senate, raw / dev | 16 / 8 | +1.8 / +3.0 | −0.95 (t −1.5, n 42) / +1.33 (t 1.1, n 6) | none / 6 | 4.3k / 1.6k |
| governor, raw / dev | 12 / 6 | +6.2 / +1.2 | +0.62 (t 1.0, n 45) / −0.60 (n 15) | 12 / 6 | 4.5k / 4.0k |
| chamber | – | nothing positive | – | – | – |
| favourite, dev | 6 | +1.7 (3.2) | −0.95 (t −1.5, n 20) | 6 | |
| middle, dev | 6 | +2.0 (1.8) | +1.38 (t 2.1, n 13, 8 races) | 8 | 2.5k, +21 |
| underdog, dev | 12 | +9.7 (5.2, n 15) | no test trades | 6 | |
| senate underdog, dev | 6 | +1.5 (2.0) | +1.50 (t 2.6, n 12, 6 races) | 6 | 2.2k, +12 |

Pooled curve against X (Figure, panel C), test days:

- **Raw:** negative at every X; −1.5 at X = 2, rising to −0.14 at X = 16.
- **Deviation:**

  | X (ticks) | 2 | 3 | 4 | 6 | 8 | 12 |
  |---|---|---|---|---|---|---|
  | test ticks/share | −1.20 | −0.83 | −0.35 | +0.40 | +0.84 | +2.17 (n 6) |

  It crosses zero near X = 5, but the 95% band includes 0 at every X.

**Multiple testing.** With 1,680 tests and roughly 70 test-period checks, two test cells with t > 2 is about what chance
gives. Training edges are 3–10 times their test values; the early October market was more dislocated.

**Capacity.** Positions are 600 notional per leg (≤ 3k shares) with the 5,000 net partisan cap. The deviation rule at
X = 8 enters ~5.6k shares/day and earns ~+14/day in test. From 24 Oct to 18:00 ET 3 Nov (~10.9 days, last entry by
18:00 ET 2 Nov for the 24 h hold) that is at most ~60k shares and ~150 of expected profit, inside its own error band.
It is also a held position, which the cash objective (`sig-cash-objective`) rules out.

## Caveats

- Sample A uses SIG trade prices, not mids; the extra half-spread charge is a crude bounce correction. Sample B has real
  touches but covers only 2 days, so its 24 h horizon is short and there is no 3 d horizon.
- Grid points overlap heavily; the closure SEs are race × day clustered, and n counts grid points, not independent events.
- The bot's own MM quotes sit ≥ 2 ticks from mid and are rarely the touch, but in B some SIG moves toward fair may be our
  own re-quoting.
- The trailing-level definition (24 h mean, ≥ 6 h of data) was fixed before testing and not tuned.

## Decision

- **Raw-gap convergence trade:** drop.
- **Deviation trade (x ≥ 8, 24 h hold, sell at half closed):** drop. Paper trading was considered and rejected; see the
  Final note under Verdict.
- **For the MM and the fair model:** the outside fair predicts SIG's *level change* only through the deviation part. The
  steady favourite–longshot offset should be treated as part of SIG's own fair, not as edge.

---

# Follow-up: predict the expected gap from leg characteristics (24 Oct 2026)

Sizhe's idea: predict each leg's expected SIG-minus-outside gap from what kind of leg it is, then trade legs whose gap is
too big or too small for their kind. Code: `sig_convergence_study.py model` and `plot_model`. Results:
`data/research_sig_convergence/results_model.json`. Figure: `research_notes/figures/sig_convergence_model.png`.

## Verdict

**Leg characteristics predict the gap level well, but much worse than the leg's own last-24 h gap.** Out-of-sample R² is
0.69 / 0.57 for characteristics against 0.96 / 0.91 for the own gap. The leftover gap closes slowly: ~35% in 24 h, half-life
~2 days. It does not trade:

- **Characteristics model:** +0.12 ticks/share on test days (t 0.4, n 146).
- **Characteristics + own gap:** +0.58 (t 1.3, n 110).

**Decision: drop all of it, including paper trading (Sizhe, 24 Oct).** The one candidate rule (underdogs, below) is
not robust across the two test windows. It is also too slow to verify and too small to matter before 3 Nov.

## 1. Model

OLS of the gap (ticks) on these regressors, fitted on sample A before 17 Oct (206k grid points):

- **Price-level curve:** piecewise-linear spline in the outside fair, knots every 0.1.
- **Category:** dummies plus category × fair.
- **Party:** R dummy.
- **Spread and depth:** log median SIG spread and log median touch depth.
- **Volume:** log trailing-24 h SIG volume (candles to 21 Oct, then the bot's tape).
- **Venue disagreement:** |PM − Kalshi| and PM − Kalshi, in ticks.
- **Timing:** log days to the state's first poll close (`poll_close_2026.json`) and the close hour.
- **M3 only:** the leg's own trailing-24 h gap.

Spread and depth are each leg's medians from the live quotes (21–23 Oct), used as fixed traits. That is a mild look-ahead
for the training days, and it favours M2.

R² out of sample, on points where the trailing level exists:

| expected gap | train 1-16 Oct | test 17-21 Oct | test 22-23 Oct (live touches) | RMSE test (ticks) |
|---|---|---|---|---|
| M0: zero (outside fair is right) | -0.02 | -0.02 | -0.02 | 10.7 / 6.7 |
| M1: leg's own trailing 24 h gap | 0.93 | **0.96** | **0.92** | 2.2 / 1.9 |
| M2: characteristics only | 0.73 | 0.69 | 0.57 | 6.0 / 4.4 |
| M3: characteristics + own gap | 0.92 | 0.95 | 0.89 | 2.5 / 2.3 |

**Characteristics do not beat the own trailing gap, and adding them to it slightly hurts out of sample.** Most of M2's
power is the favourite–longshot curve.

Other fitted effects (M2, ticks):

| effect | size |
|---|---|
| R legs vs D legs | +1.3 |
| per log-unit of touch depth | +1.9 (deeper legs carry a larger gap) |
| per log-unit of days to poll close | −1.85 (the gap grows slightly as the election nears) |
| spread, volume, PM–Kalshi disagreement | ≤ 0.2 each |

## 2. Residual closure

The residual is the actual gap minus the predicted gap, with the prediction frozen at t0. Share closed (A + B pooled; SIG
part / outside part):

| model | abs residual | n | 15 m | 1 h | 4 h | 24 h | 3 d | half-life |
|---|---|---|---|---|---|---|---|---|
| M2 | 2-4 | 98k | 0.01 | 0.04 | 0.10 | 0.36 (0.33 / 0.03) | 0.51 | 41 h |
| M2 | 4-8 | 99k | 0.02 | 0.04 | 0.11 | 0.34 (0.34 / 0.00) | 0.50 | 47 h |
| M2 | 8-16 | 26k | 0.01 | 0.04 | 0.07 | 0.20 | 0.33 | never |
| M2 | 16+ | 1.5k | 0.07 | 0.09 | 0.13 | 0.29 | 0.31 | never |
| M3 | 2-4 | 90k | 0.04 | 0.11 | 0.19 | 0.53 (0.52 / 0.01) | 0.65 | 22 h |
| M3 | 4-8 | 34k | 0.04 | 0.08 | 0.16 | 0.38 | 0.60 | 50 h |
| M3 | 8-16 | 7.5k | 0.02 | 0.05 | 0.10 | 0.23 | 0.36 | never |

Live touches (B) only:

| model | abs residual | 1 h | 4 h | 24 h |
|---|---|---|---|---|
| M2 | 2-8 | 0.02 | 0.07–0.09 | 0.30–0.35 |
| M3 | 4-8 | 0.05 | 0.15 | 0.42 |

SIG does nearly all the moving. The outside venues contribute at most ±0.1 of the gap, and 0.00–0.03 in the pooled data.
Large M2 residuals close least, because they are mostly a leg's own persistent offset that the characteristics miss
(section 2 of the main study).

## 3. Trading test

Same engine as section 3 of the main study:

- **Entry:** at SIG's touch when |residual| ≥ X ticks.
- **Exit:** at the opposite touch after h, or once Y of the residual has closed.
- **Costs:** spread + 0.5 tick per side, plus another half spread per side on sample A.
- **Grid:** X ∈ {2, 3, 4, 6, 8, 12, 16}, h ∈ {1, 4, 24 h}, Y ∈ {50%, hold}, for M2 and M3.
- **Groups:** all, each category, each band. 672 tests; the rule with the best training t per group.
- **Test periods:** OOS1 = 17–21 Oct, OOS2 = 22–23 Oct live touches.

Every chosen rule was the 24 h hold, sold at half closed.

| group / model | X | train (t, n) | OOS1 | OOS2 | test pooled (t, n, races) | S/day, cash/day |
|---|---|---|---|---|---|---|
| all, M2 | 8 | +5.27 (5.5, 281) | -0.43 (t -1.2) | +1.50 (t 2.4) | **+0.12** (0.4, 146, 62) | 25.9k, +29 |
| all, M3 | 8 | +6.59 (4.5, 179) | -1.63 (t -3.0) | +2.17 (t 3.7) | **+0.58** (1.3, 110, 54) | 14.1k, +56 |
| house, M2 | 12 | +17.2 (5.4, 29) | +2.10 (t 2.0, n 10) | – | +1.58 (1.6, 12, 8) | 1.7k, +9 |
| senate, M2 / M3 | 8 | +3.2 / +2.6 | -1.03 / -2.69 | +0.23 / +0.90 | -0.66 / -0.21 | |
| governor, M2 / M3 | 8 / 6 | +6.3 / +4.5 | +0.07 / -1.31 | +2.25 / +1.76 | +0.57 (1.3) / -0.03 | |
| chamber | – | nothing positive in training | | | | |
| favourite, M2 | 8 | +5.4 (3.4) | -0.36 | +1.27 | -0.04 (n 56) | |
| middle, M3 | 8 | +7.4 (2.7, 24) | – | +5.36 (n 11) | +3.71 (1.7, 14, 7) | 2.6k, +47 |
| underdog, M2 | 6 | +2.65 (3.9, 260) | -0.23 | +0.07 | -0.12 (n 123) | |
| **underdog, M3** | 8 | +6.29 (3.3, 86) | +0.37 (t 0.4, n 16) | +1.62 (t 3.6, n 34) | **+1.22 (2.7, 50, 36)** | 6.4k, +43 |

Pooled test profit against X, all legs, 24 h hold, sold at half closed (figure, panel C):

| X (ticks) | 2 | 4 | 6 | 8 | 12 | 16 |
|---|---|---|---|---|---|---|
| M2 | −1.34 | −0.86 | −0.33 | +0.12 | +0.06 | −1.44 (n 9) |
| M3 | −1.12 | −0.35 | +0.23 | +0.58 | +0.14 | +0.88 (n 8) |

**Multiple testing.** 672 grid tests and 16 selected rules were checked out of sample. The best test result (underdog M3,
t 2.7, p ≈ 0.007) has a Bonferroni p of ≈ 0.11 across the 16, so it is not significant.

**The two test windows disagree in sign for almost every rule.** OOS1 is negative and OOS2 positive, so the 2-day
live-touch window is effectively one common draw.

**The training/test gap is huge.** Training gives 5–25 ticks/share, test 0–1.

## 4. Decision

- **M2, trading the gap against the characteristics prediction: drop**, in every category and band. If a gap baseline is
  needed elsewhere, the own trailing gap is the better expected gap.
- **M3, underdog rule: drop.** Paper trading was considered and rejected (see Verdict). For the record, the rule was:
  - **Legs:** matched-terms legs (`tc_mapping` "same", no late-settle code), underdog band (fair < 0.3).
  - **Prediction:** the M3 coefficients in `results_model.json`, refitted on all data up to the day before.
  - **Signal and entry:** residual = SIG mid − fair − prediction. When |residual| ≥ 8 ticks, take SIG's touch toward the
    prediction.
  - **Exit:** at the opposite touch once 50% of the residual has closed (prediction frozen at entry), or after 24 h.
  - **Size and limits:** 600 notional per leg, inside the 5,000 partisan cap. Last entry 18:00 ET 2 Nov; everything flat by
    18:00 ET 3 Nov.
  - **Upside:** ~6.4k shares/day and ~+43/day in test, too small to matter for the punt. It is also a held position, which
    the cash objective rules out.
- **Categories:**
  - **house_seat:** drop (M2 +1.58 on 12 trades, OOS2 none).
  - **senate_seat:** drop.
  - **governor:** drop.
  - **chamber_control:** drop.
  - **Bands:** favourite drop, middle drop (M3 +3.7 on 14 trades, a single window), underdog drop (M3 rule above).
