# Fill probability and adverse selection vs distance from the touch: arXiv sweep

Source: WebFetch of arxiv.org/html/<id> for each paper (full text; each page was truncated at ~100k chars, tails read for 2403.02572 only). Figures are as reported by the fetch summaries; none were cross-checked against PDFs.

## 2403.02572 - Fill Probabilities in a Limit Order Book with State-Dependent Stochastic Order Flows
- Data: LMAX EUR/USD spot FX, 2 Nov 2020 - 29 Oct 2021; four representative weeks 7 Jun - 2 Jul 2021; evaluation on five days 29 Jun - 5 Jul 2021. Spread usually 1-5 ticks (3-4 ticks ~80% of time).
- Model: Poisson order flow with state-dependent rates (distance to opposite best, spread, queue size); each level a birth-death queue; own order is a pure-death process. Fill/mid-move probabilities via Laplace transforms of first-passage times (continued fractions), numerically inverted (Euler/COS).
- Key results:
  - More than 90% of limit-order executions occur at the best quotes; ~85% of executed limit orders were submitted within one tick of the best.
  - Cancelled orders are 99.87-99.93% of all limit orders; filled only 0.07-0.13%.
  - Mid-price-move probability MAPE: 9.7% (rolling 5-day calibration, spread-dependent rates) vs 10.6% spread-independent; same-weekday calibration 9.9% / 11.3%. Best at S=4 (4.7%), worst at S=3 (13.3-14.7%).
  - Fill prob at best bid (5 Jul 2021): S=1, qA=1, qB=1: empirical 2.0% vs model 3.0%; qB=2: empirical 0.6% vs model 1.9%. Fill probability rises with opposite-queue size and falls with own-side queue size. Model overestimates when own queue is large (empirical often < 0.5%).
  - One tick deeper: fills only after the best quote shifts toward the order; model values ~0.75-1.21% vs empirical 0.19-2.16% in the same cell (noisy, model slightly overestimates). Fill probabilities beyond one tick are "typically negligible".
  - Average queue sizes smallest at the touch, peak 4-6 ticks deep.
  - Market-to-limit size ratio 0.38-0.47 by week; cancel-to-limit size ratio 1.00.
- Actionable rule: queue position (own-side queue ahead, opposite-side queue) is the dominant fill driver; quote at or one tick from the touch, treat deeper as near-zero fill; use spread-dependent calibration on a rolling window.
- Caveats: unit-size orders; Poisson/independent flows; accuracy best for small queues and 1-2 tick spreads; model biased high on fills when queue is large; single venue/pair; no adverse-selection modeling.

## 2607.28323 - Optimal Execution with Passive Market Impact
- Data: NASDAQ via LOBSTER, six stocks (AMZN, TSLA, NFLX, ORCL, CSCO, MU), 252 trading days of 2016, 10:00-15:30; FX from LSEG public L2 + trades, 2026, ~100 business days, 06:00-18:00 UTC, 10 levels, 55 ms conflation, 10 s buckets; FX passive fills proxied by next-bucket aggressor trades.
- Model: fill intensity lambda*exp(-k*delta) (Avellaneda-Stoikov); passive price impact via MLOFI coefficient xi*exp(-l*delta), so impact rate eta*exp(-m*delta), m = k + l. Optimal quote (m=k): delta* = 1/k + (1/k)log(w(t,q)/w(t,q-1)) + (eta/lambda)*q; Lambert-W solution if m != k. Baseline: T=300 s, q0=20k shares, lambda=1.4, k=48 per $, eta=0.0028, sigma=0.003, tick $0.01.
- Key results (decay of fill intensity k, per $ for equities / per spread-unit as reported):
  - Equities k-hat: AMZN 0.481 (spread 36.7 ticks), TSLA 0.566 (19.2), NFLX 0.858 (4.0), ORCL 1.578 (1.2), CSCO 3.725 (1.1), MU 1.686 (1.1). Small-tick/wide-spread names have slow decay; large-tick names decay fast.
  - Equity l-hat is small (0.015-0.021 for wide-spread; slightly negative for large-tick) so m ~ k.
  - FX k-hat 0.49-0.62 (USDMXN 0.52, GBPUSD 0.49, AUDUSD 0.54, USDTHB 0.62, USDSGD 0.61); l-hat 0.13-0.33 so m = 0.67-0.95, m != k in FX.
  - Higher impact eta pushes quotes deeper; eta 0 / 0.005 / 0.01 gives final inventory 1.0k / 1.9k / 6.4k shares and P&L 0.32k / 0.07k / -0.01k $.
  - Faster impact decay m (40/50/60) gives P&L 0.47k / 0.77k / 0.94k $ with leftover inventory 0.04-0.08k shares.
- Actionable rule: model fill rate as exponential in distance and re-fit k per instrument; quote closer to mid when inventory is large, deeper when your own flow moves price; shorten the lookback if k is unstable.
- Caveats: estimates do not causally measure one trader's passive impact; FX fills are proxied; parameters likely depend on spread, queue position, volatility (omitted); exponential form may fit poorly at extremes; paper is dated 2026 (ID 2607) and the fetched numbers were not independently verified.

## 2306.05479 - Deep Attentive Survival Analysis in Limit Order Books: Estimating Fill Probabilities with Convolutional-Transformers
- Data: LOBSTER Nasdaq, nine stocks (AAPL, AMZN, BIDU, COST, CSCO, DELL, GOOG, INTC, MSFT). Train 1 Sep - 26 Dec 2022; fill stats 1 Oct - 27 Dec 2022. 100 orders per ticker per day (real tracked orders or hypothetical orders pegged to best level) at random times.
- Model: Conv-Transformer encoder (3 dilated causal convs, 4 heads, kernel 3) + monotonic NN decoder; trained with right-censored log-likelihood (RCLL); lookbacks T = 50/500/1000 trades; baselines DeepSurv, DeepHit, MN-MLP/CNN/LSTM.
- Key results (negative RCLL, lower better):
  - Tracked orders, T=50: MN-Conv-Trans 0.13-0.41 vs MN-MLP 1.27-1.99 (AAPL 0.142 vs 1.465); average improvement 86.0% (T=50), 85.7% (T=500), 85.2% (T=1000).
  - Hypothetical pegged orders: MN-Conv-Trans 3.0-5.96 vs MN-MLP 10.6-13.6 (AAPL 3.201 vs 10.554); average improvement ~62.6% / 62.2% / 62.7%.
  - Large-tick stocks: orders inside the spread fill ~3-6x more often than orders at the best level. Example AAPL best-level fill probability 0.0539, avg fill time 1.32 s.
  - Inside-spread activity vs large-tick is orders of magnitude different (MSFT 1 tick inside: 9,654,578 events vs CSCO 466,645).
- Actionable rule: fill probability must be modeled as a survival curve with cancellation censoring, conditioned on tick regime; large-tick books reward joining inside the spread, small-tick books do not.
- Caveats: only RCLL reported (no concordance/Brier); wide std devs (e.g. +/-0.185 AAPL T=50); hypothetical orders assume no impact; single ~4-month window; large-tick classification inconsistent (AAPL 1.44 ticks).

## 2307.04863 - Interpretable ML for High-Frequency Execution
- Data: Coinbase L3 BTC-USD and ETH-USD, 2022-11-05 to 2022-12-05 (7-10 Nov dropped as extreme volatility); Euronext Paris BNPP and LVMH, 2017 (BEDOFIH). Median spread: BTC 163.4 ticks, ETH 15.6, BNPP 2.0, LVMH 1.0. Median time-to-fill: BTC 0.125 s, ETH 0.205 s, BNPP 4.07 s, LVMH 8.38 s; median time-to-cancel 0.059 s / 0.065 s / 1.19 s / 3.98 s.
- Model: feed-forward net (3x32, ReLU, 25% dropout) on fixed-horizon fill probability (1 s crypto, 10 s equities); cancellation treated as competing risk (Aalen-Johansen), IPCW censoring correction; second network estimates clean-up cost V (adverse move of best ask after non-execution; smooth in realized vol). Execution rate within horizon ~2% (crypto), ~4% (equities).
- Key results:
  - Shapley: passive orders (delta>0): distance and order size top-3; priority volume top for BTC. At-touch: spread and order size. Fill falls slowly (power-law-like) with priority volume; higher aggressiveness index = higher fill, lower cancel; limit order flow imbalance (50 events) monotone for crypto, not found for equities.
  - Toy model F = A*exp(-k*delta^a): optimal distance delta* = 1/k - V; saved cost (A/k)exp(kV-1) - V.
  - Place-or-take decision F-score: BTC-USD 0.34 (toy) / 0.38 (NN fill, constant V) / 0.51 (NN fill + NN V); BNPP 0.07 / 0.14 / 0.30. State-dependent clean-up cost drives the gain.
  - Fees: taker 5 bps, maker 0; higher taker/maker gap pushes optimal distance toward the spread edge; at zero latency 1 tick below the ask in a wide spread can be optimal. Optimal distance looks linear or sub-linear in spread.
- Actionable rule: choose distance by trading fill probability against adverse-move cost V (delta* = 1/k - V), with V conditioned on volatility; condition fill on priority volume, order flow imbalance, spread.
- Caveats: one month of crypto; no fill-model AUC or P&L reported; equity precision/recall low (BNPP model III precision 0.20, recall 0.54); partial fills excluded; latency assumed negligible; inference would need < 1 microsecond; optimal distance may not be unique.

## 2512.05734 - KANFormer for Predicting Fill Probabilities via Survival Analysis in Limit Order Books
- Data: front-month CAC 40 index futures LOB, Euronext, 300 trading days 6 Jan 2016 - 7 Mar 2017; orders 9:05-17:00; 100 valid orders/day sampled at random times, only those resting at best bid/ask kept. Train 6 Jan - 12 Dec 2016, validation 13 Dec 2016 - 24 Jan 2017, test 25 Jan - 7 Mar 2017. >10% of orders partially fill (counted as fill).
- Model: dilated-causal-conv LOB branch + Action-type branch, both KAN-Transformers; fused with queue position; Weibull (scale, shape) output; 50-event lookback; Adam lr 1e-3; 30 repeated runs.
- Key results (test, mean +/- sd): KANFormer RCLL 0.53 +/- 0.03, IBS 0.027, IAUC 0.76 +/- 0.02, C-index 0.72 +/- 0.05. Best baselines: DeepHit RCLL 0.56, IAUC 0.56; Cox IAUC 0.68, C-index 0.67; ConvTrans RCLL 1.18, IAUC 0.47. Evaluation horizons up to 0.627 s.
  - Ablation: removing queue position drops IAUC 0.76 -> 0.57 (C-index 0.72 -> 0.55); removing agent features -> 0.68; removing action type -> 0.66; no inputs beyond LOB -> 0.53.
  - SHAP: queue position and volume imbalance dominate at ~1.3 ms; action type and cancel ratio gain importance by 0.627 s.
- Actionable rule: queue position is the single most valuable fill feature at short horizons; add recent order-flow action/cancel behavior for longer horizons; judge models on calibration (RCLL/IBS) and discrimination (IAUC/C-index) together.
- Caveats: per-agent ratios not observable to an individual participant; Weibull and independent-censoring assumptions; single market (index futures), single year; baseline inputs not matched (only ConvTrans uses LOB-only); horizon <= 0.63 s only; IBS differences between models are negligible (0.027-0.029).

## What this means for the bot
Caveat: all five papers use equity/FX/crypto/futures books, not prediction-market (binary contract, bounded price in [0,1]) books. Transfer is by mechanism, not numbers.
1. Fill probability is concentrated at the touch: ~85-90% of executions at or within one tick, deeper fills negligible [2403.02572]. Default quote at the touch or one tick back; do not expect fills at 3+ ticks.
2. Model fill as P(fill|delta) = A*exp(-k*delta) and re-fit k per market on a rolling window; k varies by 8x across instruments (0.48 to 3.7) so no universal constant [2607.28323].
3. Queue position is the dominant short-horizon feature: removing it cut IAUC 0.76 -> 0.57 [2512.05734]; own-queue size dominates fill probability [2403.02572]. Track queue ahead of each resting order and cancel/reprice when it grows.
4. Pick distance by balancing fill vs adverse move: delta* = 1/k - V, with V (clean-up cost after non-fill) estimated as a function of volatility [2307.04863]. Wider quotes when expected adverse move is large; also quote deeper when your own fills move price (passive impact) [2607.28323].
5. In large-tick books (prediction markets are effectively 1c-tick, tight spreads), joining or improving inside the spread is worth ~3-6x the fill rate of the best level [2306.05479], at the price of adverse selection; use survival (time-to-fill with cancel censoring) models, not a single-horizon label.
6. Cancellations dominate (99.9% of limit orders in LMAX) [2403.02572]; plan for high cancel/replace rates and cancel-as-competing-risk modeling [2307.04863].
7. Short horizons need queue + imbalance features; longer horizons add order-flow behavior [2512.05734, 2307.04863]. Spread, order size, and imbalance are the next features after distance.
8. Validate with calibration (RCLL/IBS) as well as discrimination; fits degrade at large queue sizes and in noisy deep cells [2403.02572, 2512.05734].
