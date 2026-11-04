# Being long volatility on SIG, and predicting volatility (2026-10-24, long-vol side session)

This study was read-only. Nothing under `sigbot/`, `config.json`, `params/`, `tests/` or `deploy/` was changed. No orders
were placed, and there were **0 SIG venue reads** (the cached candles and the bot's own tables were enough). To reproduce:

    <venv with numpy+scipy+matplotlib>/bin/python research_notes/long_vol_study.py all     (about 3 min)

Outputs:
* `data/research_long_vol/` holds `panel.npz`, `part1.json`, `part2.json`, `night.json`, `figure_data.json`, `out_*.txt`
  and `momentum_trades.json`.
* The figure is `research_notes/figures/long_vol_decision.png`.

Units: one tick is 0.005, and P&L is in ticks per share net of the spread. "S" means SUSQies.

## Data and splits

| set | window | SIG mid | use |
|---|---|---|---|
| TRAIN | 8-16 Oct (9 d) | last candle close on a 5-min grid (5m inside the 5m span, else 1h; daily-only points masked) | fit everything |
| OOS1 | 17 Oct - 21 Oct 21:30 (4.9 d) | 5m candle close (a trade price, so bid-ask bounce included) | test 1 |
| OOS2 | 21 Oct 22:43 - 23 Oct 22:48 (2.0 d) | live mid from `venue_fair` (~85 s) and `quotes` (~35 s, MM legs) | test 2, clean mids |

**Other inputs.**
* **Outside fair:** the Polymarket and Kalshi 1-min D-leg cache (`data/research_outside_sweep/raw`, 114 races, 8-23 Oct).
  The R leg is 1 - D. After 23 Oct 21:00 it is `venue_fair.fair`.
* **Model fair:** `params/fair_explained.json` (`model`, 124 legs, one snapshot from 22 Oct).
* **Volume:** candle volume, then the tape. **Spread:** the live touch. **Depth:** the smaller touch size, MM legs only.
* **Panel:** 228 legs x 4,883 five-minute steps.

**Costs.**
* Taker in and taker out costs half the spread each way: the actual spread live, and 1 tick (the median) on candle days.
* At 1k shares a trade sits far inside the ~2-6k touch, so impact is zero (`directional_impact.md`).

## Part 1. Ways to be long volatility, and whether they pay here

**Where convexity really comes from.** A YES contract pays 1 or 0, so its price is linear in probability: holding it is
never long volatility in probability. Convexity enters in four ways.

1. **The S-curve in the hidden margin.** Probability is about Phi(margin / sigma). Legs below 0.5 are convex in the vote
   margin and legs above 0.5 are concave. So a basket of cheap legs on both sides gains on a big swing either way. That
   is a true straddle on the national environment. But each price is still a martingale, so the convexity is pre-paid:
   if nothing happens, the tails bleed as sigma shrinks toward election day. That is theta, plus any longshot premium.
2. **Settlement, the 0/1 bound.** Holding to resolution turns the price into a 0/1 payoff. That is maximal convexity, but
   it is only available to the 3 Nov punt, because everything else must be cash by 18:00 ET.
3. **Dynamic trading.** A long straddle replicated with a linear contract means buying after up-moves and selling after
   down-moves (momentum). It pays only if moves trend rather than revert.
4. **Resting orders.** A resting limit order gives the market a free option, so it is **short** volatility. A stop or
   breakout order is the long side, and that is idea 3.

Results on SIG data, net of the spread (race-clustered t):

| idea | payoff in plain words | really long vol? | train | OOS1 | OOS2 | verdict |
|---|---|---|---|---|---|---|
| Follow a 5+ tick / 60 min move, taker, exit 4 h | win if the move keeps going | yes (synthetic straddle) | -0.11 (t -0.3, n 169) | -0.03 (t -0.1, n 138) | +0.84 (t 1.3, n 38) | **paper** (see the calm-leg variant) |
| ... exit 1 h / 24 h | | | -0.36 / -1.23 (t -3.7) | -0.52 (t -2.3) / -0.62 | +0.02 / +0.16 | drop |
| ... only in legs outside the top predicted-vol fifth (rule set on train), 4 h | moves in calm legs persist | yes | +0.69 (t 2.0, n 67) | +0.32 (t 0.9, n 84) | +1.15 (t 1.4, n 13) | **paper**; test pooled +0.43 (t 1.2) |
| ... calmest 3 fifths (chosen after seeing the test, descriptive only), 4 h | | | +0.51 (t 1.4) | +1.03 (t 2.1) | +2.25 (t 2.0) | pooled test +1.19 (t 2.5, n 60): does not clear multiple testing |
| Deep resting buy/sell 5 ticks from the mid, renewed hourly, flip at +1 h | catch spikes, sell the bounce | **no: short vol** | -1.08 (t -2.7) | -1.23 (t -2.6) | -4.33 (t -7.8) | drop |
| ... same, hold 24 h ("held for the move") | | no | +0.10 | -0.83 | -3.94 (t -2.3) | drop |
| ... 10 ticks away (flip / hold) | | no | -0.21 / +0.88 (n 24) | -0.25 / -0.55 | -1.86 / -4.00 | drop; 20 ticks: 2 fills in 17 days |
| Cheap legs both sides (0.05-0.30, D and R), equal shares, 24 h | gains on a big swing either way | yes (S-curve) | -0.95/day (t -3.5) | -1.17 (t -1.7) | n/a | **drop** before 3 Nov |
| ... 6 h holds | | | -1.02 (t -15.9) | -1.05 (t -7.6) | -1.47 (t -4.8) | gross is about 0; the loss is the spread |
| Correlated races that pay on a common shock | same basket: D underdogs + R underdogs | yes | curvature c not significant (t -0.9 to +1.7, sign flips) | | | **drop**: there is no common shock to harvest |
| Buy the side the DMM is slow to reprice before scheduled events | catch-up after an outside move | no: a directional information bet | not retested; this is the sig-convergence session's question. The base rate is -0.64 (t -3.8, `volatile_moves.md`), and no debates or dated catalysts remain | | | leave to sig-convergence |
| 0/1 bound and election-night timing | hold through poll close | yes: the only large convexity | see the next table | | | **use for the punt** |

**Why the straddle basket cannot work before 3 Nov.**
* **There is almost no national shock.** The average D-leg move across races has a standard deviation of 0.4 ticks per
  day (0.2 per 6 h).
* **So the convexity payoff is tiny.** For a 15c leg with a 6-pt margin sigma, the price slope is 0.04 per margin point
  and the curvature is about 0.007 per pt². A 0.2-tick daily price move is a 0.05-pt margin move, which earns about
  0.002 ticks from convexity, against a 1-tick round trip. This agrees with `sig-correlation-model`: the national swing
  matters for risk, not for alpha.
* **Holding to election day pays nothing either.** In 2024, competitive legs moved only **1.6 pts** on average between
  16:00 and 18:00 ET on the day, against **30 pts** between 18:00 and 24:00 ET (n 18).

**Election night, for the punt (2024 Polymarket/Kalshi, `data/election_night/study_2024.json`).** "Decided" means
someone reached 0.95.

| race at its state's last poll close | n | mean abs move +60 min | +240 min | decided within 4 h | median min to decided |
|---|---|---|---|---|---|
| favourite >= 0.95 | 59 | 1.5 pts | 1.7 | 98% | 0 |
| favourite 0.80-0.95 | 23 | 3.7 | 7.8 | 70% | 89 |
| favourite 0.65-0.80 | 21 | 5.8 | 19.7 | 33% | 436 |
| favourite 0.50-0.65 | 16 | 4.2 | 18.2 | 0% | 465 |
| competitive (< 0.80), polls close 19:00-19:59 ET | 5 | 8.3 | 22.3 | **80%** | 204 |
| competitive, 20:00 ET | 8 | 5.2 | 20.7 | 13% | 447 |
| competitive, 21:00 ET | 15 | 4.7 | 21.9 | 7% | 464 |
| competitive, 22:00+ ET | 9 | 3.8 | 11.2 | 11% | 1,169 |
| competitive Senate / governor / House / president | 7 / 2 / 21 / 7 | 6.6 / 3.1 / 2.8 / 11.0 | 29.3 / 18.7 / 11.8 / 31.0 | 43% / 50% / 0% / 43% | 447 / 260 / 1,169 / 248 |

**What this means for the punt's two-stage roll.**
* **Pick a 19:00 ET Senate or governor race for stage 1** (GA or VA closing at 19:00, NC or OH at 19:30). Only those were
  usually resolved within 4 h, which leaves time to roll.
* **Avoid House races for stage 1.** None of 21 was decided within 4 h; the median was 19.5 h.
* **Expect slow drift, not jumps.** The first hour after close moves only 4-8 pts on average, even in competitive races.
* **Caution:** the 19:00 bucket is n = 5.

## Part 2. Which SIG legs move, and can we forecast it?

**Most volatile since 7 Oct.**

**By hourly volatility** (sd of hourly mid changes, ticks):

| leg | price | sd per hour | jumps of 5+ ticks in an hour |
|---|---|---|---|
| Wyoming Senate R | 0.94 | 1.60 | 5 |
| Massachusetts Senate D | 0.95 | 1.55 | 5 |
| Rhode Island Senate R | 0.14 | 1.49 | 4 |
| New York Governor D | 0.90 | 1.43 | 5 |
| MN-05 D / R | 0.94 / 0.08 | 1.40 / 1.38 | 4 / 3 |
| Illinois Senate R | 0.06 | 1.36 | 4 |

The candle-period ranking of illiquid tails is partly bid-ask bounce in trade prices.

**By daily volatility** (sd of daily changes, ticks): MA Senate D 7.6, WY Senate R 6.8, ID Governor R 6.7, MI-07 D 6.6,
MN-05 R 6.3.

**Live 5-min volatility, clean mids (22-23 Oct):** ID Governor D 0.66, WY Senate D 0.64, MN-05 D 0.61, WA-03 D 0.57,
HI Governor D, CA-48 D, MN-01 R, VA-05 R, MT-01 (both legs).

**By category** (median hourly sd): chamber 1.08, Senate 0.92, governor 0.79, House 0.75. Kruskal p is 3e-6 on train,
0.01 on OOS1 and 0.009 on OOS2, but the order of governor and House flips in OOS2.

**Over time:** market-wide hourly volatility shows no trend toward the election yet (daily Spearman +0.35, p 0.18).

**Predictors of future volatility.** The table shows the mean cross-sectional Spearman rho per hour, with t computed
across non-overlapping 6 h blocks (24 h blocks for the 24 h horizon).

| predictor | next 6 h: train | OOS1 | OOS2 | next 24 h: train | OOS1 |
|---|---|---|---|---|---|
| past 24 h volatility | +0.40 (t 42) | +0.31 (t 19) | +0.23 (t 15) | +0.43 | +0.29 (t 12) |
| past 6 h volatility | +0.38 | +0.32 (t 25) | +0.24 (t 12) | +0.37 | +0.27 |
| 5-min volatility, past 1 h | +0.32 | +0.25 (t 26) | +0.21 (t 7) | +0.32 | +0.21 |
| trade volume, past 24 h | +0.40 | +0.30 (t 18) | +0.23 (t 6) | +0.43 | +0.28 |
| gap to the Polymarket/Kalshi fair | +0.24 | +0.26 (t 18) | +0.18 (t 7) | +0.29 | +0.29 (t 8) |
| spread (live only) | | | +0.09 (t 10); next 1 h +0.15 (t 18) | | |
| touch depth (live, MM legs) | | | -0.16 (t -2.8) | | |
| price level p(1-p) | +0.09 | +0.06 (t 3) | +0.09 (t 5) | +0.08 | +0.03 (t 0.9) |
| gap to the bot's model fair | +0.06 | -0.01 (t -0.6) | -0.03 | +0.08 | -0.06 |
| outside-venue volatility, past 24 h | +0.03 | -0.01 (t -0.5) | +0.08 (t 3.8) | +0.01 | -0.02 |

OOS2 at 24 h has only one block, so it cannot be tested.

**Combined forecast.** This is an OLS fit of log next-period volatility on all of the above plus category, fitted on train
only.

| | OOS R², next 6 h | OOS R², next 24 h | past-vol-only model, 24 h | rank correlation, 24 h |
|---|---|---|---|---|
| OOS1 | 0.18 | 0.18 | 0.13 | 0.37 |
| OOS2 | 0.05 | 0.01 | 0.01 | 0.25 |

Ranking by the forecast still sorts the test legs (figure, panel A).
* **Next-24 h volatility,** calmest fifth to most volatile fifth: 2.9 to 4.6 ticks (OOS1) and 3.4 to 4.4 (OOS2).
* **Chance of a 5+ tick hourly jump in the next 24 h:** 6.5% to 9.7% (OOS1) and 7.7% to 12.7% (OOS2). That is a lift of
  about 1.5-1.6x.
* **OOS2 R² is low** because the live mids are less noisy than the candle closes the model was fitted on, so its level is
  biased. The ranking survives.

**Answer: a useful volatility forecast exists, but only as a ranking.** It rests on clustering, volume and the gap to the
outside fair. Outside volatility, the model gap and time to the election add nothing.

**Multiple testing.**
* **Part 2** ran about 90 cells (10 predictors x 3 horizons x 3 periods). Bonferroni at 5% needs |t| > 3.4. Past
  volatility, volume, the outside gap and spread clear it in both test sets.
* **Part 1** ran about 40 tests, so it needs |t| > 3.2. **No positive long-vol result clears it.** The best, momentum in
  calm legs (pooled test t 2.5), was chosen after looking.

## Decision

* **Long-vol trades before 3 Nov: drop.**
  * Every pre-election version either loses or does not clear multiple testing: momentum, deep orders, the tail straddle
    and the correlated-race straddle.
  * SIG moves revert (the short-vol edge the bot already harvests). The national common shock is about 0.
  * Resting deep orders are short volatility, not long.
* **Paper: follow 5+ tick moves only in legs outside the top predicted-vol fifth, exit at 4 h.**
  * Train +0.69 (t 2.0); test +0.43 (t 1.2); calmest 3 fifths +1.19 (t 2.5, post hoc).
  * At about 9 trades a day and 1k shares, that is about 50 S/day if real. It is not worth punt cash before it clears
    30+ out-of-sample trades at t > 2.
* **Build (via the main session's gates): the forecast as a risk input for the existing short-vol sleeves.**
  * Use it to widen or shrink MM and churn size in the top predicted-vol fifth, where jumps are about 1.5x as likely.
  * This needs its own markout check before wiring.
* **Use for the punt:** the convexity is real only on election night. Choose stage 1 from competitive Senate or governor
  races whose polls close at 19:00-19:30 ET. House races resolve too slowly for a same-night roll.

Figure: `research_notes/figures/long_vol_decision.png`.
* **A:** realised next-24 h volatility by forecast fifth.
* **B:** net P&L of following a move, 4 h and 24 h.
* **C:** the 5th-percentile and worst trade.

The worst single test trade was -24 ticks at a 24 h hold and -9 ticks at 4 h.
