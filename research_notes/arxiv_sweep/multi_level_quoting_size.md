# Multi-level quoting and size allocation: arXiv sweep notes

Goal: how a prediction-market market-making bot should allocate size across several levels away from mid.
Source: HTML versions fetched via WebFetch (all five were available). Where a paper's text was truncated by the fetcher, the remainder was fetched with an offset. Numbers below are as reported by the fetch summaries, not independently checked against the PDFs.

---

## 1. 2608.18195 - Multi-Level Market Making with Reinforcement Learning
Authors: Cheridito and Weiss (ETH Zurich).

**Data used**
- No historical data. Simulated LOB with multi-agent Poisson order flow, three environments: (1) noise traders only, (2) noise + tactical (react to instantaneous imbalance, cause jumps), (3) noise + tactical + strategic (trade on smoothed imbalance, cause drifts).
- Horizon T = 600 s, decisions every 30 s (N = 20 steps). Lot sizes M = 2 and M = 20. 10,000 out-of-sample test episodes with separate seeds.
- Simulator: start bid 1000 / ask 1001, market order intensity 0.1237, order sizes half-normal (sd 2). Limit-order intensity by level k=1..5: 0.2842, 0.5255, 0.2971, 0.2307, 0.0826 (peaks at level 2, then falls roughly 10x by level 5; ~0 beyond level 12).

**Method**
- Action = point on a simplex over 2(K+1) outputs (no order, market buy/sell, limit buy/sell at each of K = 3 levels), via a logistic-normal policy; fractional lots rounded by Hamilton apportionment. Actor-critic, 800 gradient steps x 1,280 trajectories, lr 0.0005, inventory penalty gamma = 0.01. Terminal inventory liquidated by market order.
- Benchmarks: TOP1 (M/2 at best bid and ask), TOP2 (M/2 one tick deeper), INV (best prices, size skewed by inventory: bid M/2(1 - Qbar), ask M/2(1 + Qbar), alpha = 1).

**Key results (normalized cash flow, mean (std))**

| Market | Lots | TOP1 | TOP2 | INV | LN (RL) |
|---|---|---|---|---|---|
| Noise | 2 | 4.40 (3.87) | 2.91 (3.69) | 4.83 (2.40) | 6.03 (2.81) |
| Noise | 20 | 3.84 (2.62) | 1.48 (2.82) | 4.26 (1.59) | 4.66 (1.41) |
| Noise+Tactical | 2 | 5.47 (2.38) | 4.31 (2.42) | 5.11 (2.20) | 9.11 (2.20) |
| Noise+Tactical | 20 | 2.88 (2.21) | 2.55 (1.82) | 1.22 (1.92) | 5.68 (1.03) |
| All three | 2 | 3.95 (2.76) | 3.83 (2.35) | 3.24 (2.32) | 8.55 (2.40) |
| All three | 20 | 0.73 (2.73) | 2.05 (1.95) | -1.44 (2.12) | 4.99 (1.07) |

- Largest edge: all-trader market, 20 lots: 4.99 vs 0.73 (TOP1), 2.05 (TOP2), -1.44 (INV).
- Logistic-normal beat Dirichlet in all six cells; worst gap all-trader M=20: LN 4.99 vs DR -6.18.
- Inventory penalty ablation (gamma 0.01 vs 0): penalty costs a little mean but cuts std, e.g. Noise M=2: 6.03 (2.81) vs 7.95 (8.20).
- Allowing terminal inventory (nu = 0.5) adds roughly 0.3-0.6 mean.
- Markout at 30 s (10,000 fills): TOP1 20 lots all-trader mean markout -0.06 ticks; TOP2 has better markouts but fills less.
- Fill rate falls as informed traders enter: 74.65% -> 59.16% (M=2), 59.65% -> 43.99% (M=20).
- LN policy: market orders only ~1.9-2.7% of volume (mostly terminal liquidation); mean inventory near zero (within ~1 lot for M=2, bands to ~6 lots for M=20).

**Actionable rule (paper's own simulation observations)**
- Small size, little adverse selection: concentrate at the best bid/ask.
- Larger size: split across level 1 and level 2.
- Informed flow present: push volume to levels 2-3 and cancel/reposition more often.
- Long inventory: tilt toward the sell side.
- Naive inventory skew (INV) works with noise-only flow but fails (even negative) once tactical/strategic traders appear.
- Parametrize allocations as a simplex (softmax / logistic-normal incl. a "no order" weight) rather than independent per-level sizes.

**Caveats**
- Simulation only; results depend on simulator intensities. K = 3, one horizon, one decision frequency. Benchmarks are simple heuristics (no optimal-control comparison). No Sharpe or drawdown. Heavy compute (128 CPUs + GPU). Std is across episodes. Not a prediction market; ticks are discrete and the book is deep relative to a typical Polymarket book.

---

## 2. 2610.26789 - Cooperative Multi-Agent RL for Market Making on Prediction-Market Order Books
Author: Skander Lejmi (LSE MSc thesis). Page title: "Cooperative Multi-Agent Market Making on Prediction-Market Order Books".

**Data used**
- Polymarket L2 updates reconstructed into LOBSTER format; abstract cites 417 real markets. The actual experiments use BTC up/down five-minute binary markets, only the final five minutes before resolution, collected 12:00-20:00 UTC.
- Train: 4 days (22 and 28 Apr, 14 and 28 May 2026) = 640 streams (2 tokens per market). Held-out: 1 day (11 Jun 2026) = 192 streams. Window counts are inconsistent across the text (64 / 1,152 / 48 windows).
- Environment: JaxMARL-HFT / JAX-LOB, PPO with GRU, 5 observed book levels per side (10 loaded), prices 0.01-0.99, 250 steps x 400 messages per episode.

**Key results (held-out portfolio value in ticks; PV std; volume units/step; no-trade %; mean |inventory|)**

| Config | PV | Std | Vol | No-trade | |Inv| |
|---|---|---|---|---|---|
| PAPER_BASE (single agent) | -46.1 | 19.4 | 0.056 | 19.2% | 1.108 |
| WIDE_SPOONER (17-row quote table out to 13 ticks) | -9.6 | 3.1 | 0.032 | 4.4% | 0.366 |
| D_SPLIT (desk: quoter + risk manager) | -72.3 | 17.6 | 0.112 | 1.6% | 0.241 |
| D_BLEND | -41.0 | 12.3 | 0.068 | 4.7% | 0.172 |
| D_MARK (markout reward) | -5.9 | 3.5 | 0.032 | 20.4% | 0.082 |
| D_BL_MARK_CURR | -4.3 | 1.7 | 0.022 | 19.7% | 0.060 |

- Every configuration has negative mean PV under periodic held-out evaluation (headline negative result). One seed per config.
- Window-level: D_SPLIT loses in 45/48 windows, D_BLEND 39/48, D_MARK 6/48, D_BL_MARK_CURR 8/48; the last two sum to +133 and +67 ticks across windows, which the authors say does not establish profitability.
- Baseline no-trade collapse: chose no-trade 61.1%-97.7% of the time across three seeds. Removing the no-trade row just pushed quotes several ticks outside a ~2-tick book where nothing fills.
- Markout reward did most of the repair work; curriculum added stability.
- Inventory cap: 4 units (shared stack), 20 units (PAPER_BASE).

**Actionable rule**
- Do not assume a learned multi-level policy is profitable on Polymarket; the only real-book RL evidence here is negative.
- Fills on real flow are disproportionately informed, so reward or score fill quality (post-fill markout), not spread captured.
- Control inventory with a hard cap and loss stop outside the learned objective, not a reward penalty (penalty minimum at zero inventory encourages no-trade).
- Keep a no-trade / one-sided option, but quoting far outside a ~2-tick book earns nothing, so cap quote distance to where fills occur.
- Account for time to resolution; value decays and informed flow rises near expiry.

**Caveats**
- Thesis, not peer reviewed. Single seed per config. Inconsistent window counts. Abstract says 417 markets but the detailed experiment covers 5-minute BTC markets over 5 days. No explicit quote-distance or per-level size numbers; adverse selection not quantified. Held-out is one day.

---

## 3. 1903.07222 - Market Making under a Weakly Consistent Limit Order Book Model
Authors: Baron Law (Agam Capital), Frederi Viens (Michigan State).

**Data used**
- QQQ (Nasdaq-only LOB) on 8 May 2014, 12:00-15:00. Nasdaq ~20.8% of QQQ volume, so aggressive orders are likely overstated; figures are illustrative.
- Order flow: 12 order types; types 8-12 are 98.83% of events; total market-order intensity 0.3371/s (about one trade every 3 s).
- Base case: T = 300 s, participation rho = 0.1, risk aversion theta = 1e-7, tick 0.01, rebate 0.002, fee 0.003. Volume lognormal(6.5, 1.35^2) for types 1-2.

**Model**: level-one (best bid/ask only) reduced-form LOB, direction- and timing-consistent but not volume-consistent; optimal switching + impulse control solved by an HJB quasi-variational inequality (finite-difference penalty scheme; existence/uniqueness of viscosity solution conjectured, not proven).

**Key results (backtest, N = 1e6 sessions, 6.5 h each, Table 6/8)**

| LOB | Unconstrained mean / IR | Optimal-control mean / IR | Overstatement vs consistent (uncon / opt) |
|---|---|---|---|
| Consistent | 8,224 / 0.80 | 6,471 / 12.55 | NA |
| Inconsistent 1 | 12,286 / 0.54 | 10,533 / 11.19 | 49% / 63% |
| Inconsistent 2 | 12,304 / 0.63 | 10,534 / 12.65 | 50% / 63% |
| Inconsistent 3 (price right direction 80%) | 9,853 / 0.60 | 8,095 / 11.30 | 20% / 25% |

- Optimal control cuts std from 10,258 to 516 (mean 8,224 -> 6,471), IR about 16x higher (0.80 -> 12.55); kurtosis 7.81 -> 3.19.
- Bid-off threshold goes to infinity 26 s before the end; impulse (market-order) size threshold reaches equilibrium 750 shares at 108 s remaining; impulse starts at 230 when inventory hits 750; anchor 520.
- Probability of 4+ consecutive sell market orders is about 6%.
- Under a two-tick spread profit is 7/5 tick vs 2/5 tick for one-tick spread (3.5x).
- Under heavy sell pressure (lambda2 = 0.08) inventory held below 70 and bid side effectively not quoted.
- Switching (turning quoting off) more attractive when discount alpha <= 0.05.

**Actionable rule**
- Backtests on simplified books that ignore price/volume consistency overstate profit by roughly 20-63%; haircut simulated or naive-replay PnL by about half before sizing risk.
- Inventory bands plus market-order impulse trades beat unconstrained quoting on risk-adjusted terms (IR 12.55 vs 0.80) at a cost of about 21% of mean PnL.
- Switch quoting off on the side facing sustained one-way flow, and near the end of the horizon (for a prediction market, near resolution).

**Caveats**
- The paper does NOT address multi-level quoting: it is a single-level model and the fetch found no multi-level or multi-size results. The "motivates multi-level quoting" framing in the task brief is not supported by the text read. The "single-size" point is about volume inconsistency in a level-one book, not about size across levels.
- One ETF, one afternoon for calibration, single venue; execution at best bid/ask understates profit; cost formula heuristic; PDE convergence unproven.

---

## 4. 1806.05101 - Order-book modelling and market making strategies
Authors: Xiaofei Lu and Frederic Abergel (CentraleSupelec).

**Data used**
- Eurostoxx 50 futures (large tick, average spread ~1 tick). Calibration June-July 2016; out-of-sample backtest July to mid-November 2016. Lot size 10; max queue 500; only best bid/ask followed.

**Model**: non-Markovian extension of the queue-reactive model. Order sizes non-unit (truncated geometric; market orders = geometric + Dirac masses at multiples of 5 and full queue). Memory: type of last removal event matters.
- P(new limit is a "follow" | removal by market order) = 84.3% vs 27.0% after a cancellation; for market orders of size >= 30 follow probability ~1 (>20% of market orders are >30).
- Mean size of re-established limit: 7.46 (removal <10), 10.18 (10-19), 27.93 (>=20).
- Latency peak ~200 microseconds after market-order removal.
- Calibration (queues 21-30): limit geometric p ~0.64; cancel p ~0.65-0.66; market p falls 0.349 -> 0.305.

**Key results**
- Simulation (Model II, daily avg): locally optimal P&L 1.35 (sd 0.47), mean |inventory| 3.33, turnover 19.01 contracts (Z = 2.87); naive P&L -7.12 (0.71), |inventory| 10.47, turnover 113.46.
- Backtest (daily avg): locally optimal +0.54 kEUR, turnover 45.20 MEUR, 0.12 bp; naive no threshold -26.89 kEUR, turnover 3430.42 MEUR, -0.08 bp; naive q_min = 250: -3.43 kEUR (-0.29 bp); naive q_min = 400: -2.13 kEUR (-0.59 bp).
- Inventory cap 80 contracts (other caps did not change conclusions).

**Actionable rule**
- Default is to NOT quote: in most states the optimal policy is cancel both sides; quote only when queue position is good and queues are balanced and long.
- Queue priority is valuable; avoid joining short or back-of-queue positions. Do not stay in if the opposite queue is short (e.g. ask queue <= 220 with bid priority 300 / ask 200 case).
- After one side fills, keep the passive side if the opposite queue >= 140; otherwise close with a market order.
- Condition on the last removal type and size: after a large market-order sweep the level is very likely to be refilled (84.3%, ~100% for size >= 30), and refills scale with sweep size (mean 7.5 -> 27.9).
- A queue-size threshold alone is not enough (naive q_min variants still lose).

**Caveats**
- One large-tick futures contract, ~4 months. Only best bid/ask (no multi-level). Fixed latency. Fill assumption (market order clearing a limit counted as a fill even from back of queue) can affect up to 20% of turnover and inflates results. No confidence intervals in backtest; no Sharpe. Benchmark is naive strategy only, not a Markovian optimal one. Prediction-market tick (1c on 0-100c) is coarse like a large-tick contract, so the large-tick findings are probably the more relevant ones.

---

## 5. 2106.11691 - Two Price Regimes in Limit Order Books: Liquidity Cushion and Fragmented Distant Field
Authors: Krause, Jungblut, Guhr (Duisburg-Essen).

**Data used**
- NASDAQ TotalView-ITCH, 96 of the NASDAQ-100 stocks, one week (7-11 Mar 2016); detailed figures for Ebay and LVNTA on 10 Mar 2016. Full day 4:00-20:00 NY, focus on 9:30-16:00. Cushion width averaged over 5 days.

**Key results**
- Lifetime split: 76% of orders live < 10 s; 24% live >= 10 s; 0.9% live >= 10 min; 0.7 per mille survive the full day. Orders of 10 s-10 min supply almost all near-quote volume; orders >= 10 min (< 1% of orders) supply almost all deep volume.
- Insertions farther than ~1% of mid (rel. > 1e-2) are 3 per mille of orders but ~45% of the lifetime-weighted filling. About 30% of insertions hit or undercut the midpoint.
- Cushion width (full width at 2/3 of max occupation): Ebay 49 cents (relative half-width ~1% of mid); LVNTA 36 cents; Google 5.43 USD. Width grows with mid price, quote-change count and traded value; NOT correlated with volatility. Rises with spread for small-tick stocks (up to >10x).
- Ebay regime comparison: distant field 0.16% of orders (12% for LVNTA); avg lifetime 32 s (cushion) vs 2488 s (distant); avg volume 205 vs 436 shares.
- Timing: distant field fills ~1 min after open; cushion needs ~30 min to reach a stable form.
- Zero-intelligence model (Ebay): N = 273,835 orders, T = 23,400 s, half-width L = 25 levels, P_market = 0.0147, level decay l0 = 3.045 (insertion probability falls ~4 orders of magnitude), lifetime scale 13.24 s, avg cushion lifetime 30.0 s. Needs level-dependent insertion and lifetime to reproduce the average book; uniform versions fail.

**Actionable rule (inferred; the paper gives no sizing guidance)**
- Near mid, depth is dense but short-lived (lifetimes seconds to minutes), so own quotes there face heavy competition and fast churn: size should be modest per level and refreshed often.
- Far levels are sparse and long-lived: a resting quote deep in the book is rarely competed against and rarely touched. Size there is cheap to hold but fills only on large moves (these are the fills with the most adverse selection).
- Define the quoting band relative to mid (about 1% of mid is the cushion half-width in equities) and re-estimate per instrument; width scales with spread/tick and activity, not volatility.
- Expect the book to need roughly 30 minutes after open to stabilize; avoid sizing up in that window.

**Caveats**
- Equities, one week in 2016, deep analysis on 1-2 days; visible orders only; cushion width is a heuristic; zero-intelligence model, cushion only; far-field explanations are speculative. Not binary-contract specific: prices bounded 0-1 and resolution deadlines likely compress both regimes.

---

## What this means for the bot

1. **Evidence is thin where it matters.** Only 2610.26789 uses a prediction-market book, and it is negative: no configuration had positive average held-out PV (best -4.3 ticks, one seed, one held-out day). 2608.18195 is simulation-only; 1903.07222 and 1806.05101 are single-level; 2106.11691 is descriptive. None gives a validated multi-level sizing formula. Treat everything below as hypotheses to test in paper-trading.
2. **Sizing shape by level.** Use a simplex allocation (weights sum to <= 1 of the quote budget, including an explicit "no quote" weight) over 3 levels (2608.18195). Small budget: put most at level 1. Larger budget: split level 1 and level 2. When fills look informed (falling fill rate, negative markout): shift to levels 2-3 and shorten requote interval. Sim fill rates fell from ~75% to ~59% (M=2) as informed flow entered.
3. **Why not just sit at the touch.** Near-touch liquidity churns fast (76% of orders live < 10 s, 2106.11691) and the touch is where informed fills hit (markout -0.06 ticks at TOP1 in 2608.18195; fills in real Polymarket data skew informed, 2610.26789). Deeper levels fill less but with better markout (TOP2 > TOP1 on markout, 2608.18195).
4. **Do not quote when queue position is bad.** 1806.05101: default is cancel both sides; quote only with good priority and balanced, long queues; leave when the opposite queue is short. Prediction books are thin, so check queue ahead of you before posting a level.
5. **Inventory.** Use a hard cap + side-off (stop quoting the side that adds to a one-way position) + occasional market-order impulse, not only a reward/skew penalty (1903.07222: IR 12.55 vs 0.80 at ~21% lower mean; 2610.26789: penalty pushes toward no-trade; 2608.18195: simple size skew INV fails once informed flow appears, -1.44 vs +4.99).
6. **Time to resolution.** Switch quoting off or widen as resolution nears (1903.07222 bid-off at 26 s before end of a 300 s horizon; 2610.26789 notes value/time decay). Unconfirmed that these thresholds transfer; calibrate against own data.
7. **Haircut backtests.** Naive replay fills overstate profit: 20-63% in 1903.07222, up to 20% of turnover from optimistic back-of-queue fills in 1806.05101. Require markout-adjusted PnL, not spread capture.
8. **Score fills by post-fill markout** and use it as the primary signal for shifting size between levels (2610.26789 found markout reward did most of the repair work).

Gaps to close with own data: per-level fill probability and markout curves on the target Polymarket/Kalshi books; how the cushion/distant-field split looks in a 0-100c book; whether the 2608.18195 level-1/level-2 split holds on real queues.
