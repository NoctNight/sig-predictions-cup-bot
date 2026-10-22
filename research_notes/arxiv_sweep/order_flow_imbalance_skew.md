# Order-flow imbalance, skew and adverse selection: arXiv sweep notes

Goal: use buy/sell pressure from fills and tape to skew prices and sizes in a prediction-market market-making bot.
Source: arXiv HTML versions fetched via WebFetch (summaries produced by a small model; numbers below are as extracted, a few flagged as unverified).

---

## 2608.07690 On a Simple Relationship Between Order Imbalance, Skew and Width in Over-The-Counter Trading

Author: Peter Cotton (preprint, not peer reviewed).

**Data used:** None. Purely theoretical; proposes testing on RFQ archives but does not.

**Model:** Sealed-bid enquiries arrive as a Poisson process; each is a seller with probability q, buyer with 1-q. Best competing response is exponential with hazard h, so market width w = 1/h. Each win concedes adverse selection eps. Steady state.

**Key results:**
- Theorem 1: imbalanced problem equals the balanced (q = 1/2) problem with carry cost multiplied by M(q) = 1 / (2 sqrt(q(1-q))) >= 1. Skew shifts by delta, width widens by gamma = (1/h) log M(q).
- Skew at zero inventory: S(0) = delta = (w/2) log(q/(1-q)) ~ 2w(q - 1/2) for mild imbalance. First order in imbalance.
- Width widening: gamma ~ 2w(q - 1/2)^2. Second order. M(q) ~ 1 + 2(q - 1/2)^2.
- One-sided flow (q -> 0 or 1) acts as an unbounded carry-cost multiplier.
- Fill probability at markup m is exp(-m/w); a zero-adverse-selection, zero-inventory-cost dealer quoting optimally wins about 1/e ~ 37% of enquiries.
- Worked intuition: if q = 0.6 (sellers dominate), delta ~ 0.2w, gamma ~ 0.02w. If q = 0.7, delta = (w/2) ln(7/3) ~ 0.42w, gamma = w ln M, M = 1.091, ~ 0.087w.

**Actionable rule:** Even with zero inventory, shift the mid against the dominant flow direction by about (w/2) ln(q/(1-q)), where q is the recent fraction of seller-initiated flow. Widen the spread only slightly (quadratic in imbalance). Do not use spread widening as the main response to imbalance; skew is the first-order tool.

**Caveats:** Closed form is for the balanced/compressed solution; the full problem needs numerics. Assumes constant eps, constant q, steady state, Poisson (non-clustered) arrivals. Exponential win curve only needed locally. No empirical test. OTC RFQ setting, not a continuous order book.

---

## 2508.20225 Optimal Quoting under Adverse Selection and Price Reading

Authors: Barzykin, Bergault, Gueant, Lemmel (v6, shortened version for Risk).

**Data used:** Simulation only, illustrative parameters. Size ladder 1,2,5,10,20,50 (millions notional); summed intensities (2000,800,600,400,120,80) per day; kappa = 3 per bp; daily vol 100 bp; risk aversion gamma = 1e-4; 1e5 Monte Carlo paths, T = 1e4 s; adverse-selection tier alpha = 0.05, beta = 2; safe volume share 25% and 75%. (Last ~6.5k chars of the page were not read.)

**Model:** Avellaneda-Stoikov-style infinite-horizon with client tiers and trade sizes. Two informational effects treated as first-order perturbations (eps): adverse selection (trades cause jumps in reference price) and price reading (clients infer inventory from the quote skew; skew causes drift).

**Key results:**
- Optimal quote = baseline quote + eps * (global part, identical across tiers, from finite differences of the first-order value correction f + tier-specific part).
- Symmetric case: f(q) ~ q^2. Global component proportional to (+/- q + Delta/2): widens spread at zero inventory and increases skew with inventory.
- Price-reading tier-specific term is proportional to -q * w (loading), vanishes at zero inventory: reduce skew toward sniffer tiers.
- Adverse selection effect depends on sign of beta - kappa: slow signals (beta < kappa) mean the informed tier's flow is a usable signal (tighten the other tiers at zero inventory, widen informed tier); sharp footprints (beta > kappa) reverse the effect.
- Simulations: "Optimal" beats "No Action" on both PnL and risk, most effective when sniffers or informed tiers are a modest share of flow. "No Skew" (drop skew entirely) recovers PnL but raises risk substantially. No headline numeric PnL figures extracted.

**Actionable rule:** Track flow by counterparty class. For counterparties who look informed or who read your inventory off your skew, quote them less skew-sensitive and wider; for uninformed flow keep full inventory skew. Do not respond to price reading by removing skew altogether (cuts access to risk-reducing flow and raises risk). Informed flow that arrives slowly is a signal, not just a cost.

**Caveats:** First-order perturbation, only reliable when informational effects are small; interactions are second order and ignored. Parameters are inputs, not estimated. Linear J and exponential zeta are local specifications. Results are simulated with assumed parameters; PnL gain partly reflects a controlled price drift. FX-style dealer setting with client tiers.

---

## 2409.12721 Market Simulation under Adverse Selection

Authors: Lalor and Swishchuk (v3).

**Data used:** CME futures ES, NQ, CL, ZN (near-term), Futures First LOB data, up to 5 levels, resampled to 1 s with forward fill, one trading day 8:30-16:00 EST (paper's dates inconsistent: April 2024 vs April 2025). 330 simulated 120-second paths per asset. Mean ES trade size 3.47, median 1.

**Fill model:** Non-adverse fills scaled by constant probability rho (rho = 1 benchmark, rho = 0.2 improved environment; heuristic, not calibrated). Fill is adverse if next price move goes against the position (ask fill then higher ask; bid fill then lower bid). Tracked separately as AFA/AFB vs NFA/NFB.

**Key numbers:**
- Adverse share of live-sim limit-order fills: ES 941 fills ~81.5% adverse; NQ 1,929 ~65.8%; CL 625 ~82.9%; ZN 224 ~88.8%.
- CL strategy runs (330 runs): AFA 3,950, NFA 825, AFB 3,943, NFB 813, about 83% adverse.
- Appendix 120 s runs: ES ~73%, NQ ~85%, ZN ~17% (conflicts with the earlier table; treat as unreliable).
- Improved (rho = 0.2, adverse fills tracked) environment performs "much worse" than the benchmark; no PnL or Sharpe numbers reported (qualitative histograms only).

**Actionable rule:** In backtests and fill simulators, do not assume every touched passive quote fills benignly. Tag each fill adverse/non-adverse by next-tick mid move and set non-adverse fill probability well below 1 (the paper's heuristic is 0.2). Expect most passive fills to be adverse, and do not trust backtest PnL that assumes rho = 1.

**Caveats:** One day, one strategy, heuristic rho, no out-of-sample test. Inconsistent dates and table contradictions (ZN). Simple alpha predictor with acknowledged decay. Futures LOB, not a binary prediction market.

---

## 2004.08290 Empirical Study of Market Impact Conditional on Order-Flow Imbalance

Author: Anastasia Bugaenko (MSc dissertation, University of Glasgow).

**Data used:** LOBSTER (NASDAQ ITCH reconstruction), not TAQ as the task brief stated. Stocks SIRI and EBAY (large tick), TSLA and PCLN (small tick). 2 Jan - 30 Jun 2015 (124 trading days). Average daily market orders: SIRI 624, EBAY 3,540, TSLA 3,924, PCLN 1,333. ~1e5 relevant market-order events. Level 1 only, mid price.

**Key numbers:**
- Impact is approximately linear in OFI for small |delta V| and becomes concave as |delta V| grows.
- TSLA, OFI over T = 10 market orders: Kyle's lambda (slope in the linear region) = 0.011; overall OFI-impact correlation 0.8538.
- Linear regression MSE 3.86 (single split) vs decision tree 2.5 (dollar cents); 10-fold CV: linear 4.42 (SD 0.42) vs tree 4.11 (SD 1.07). Linear-region fit: tree better by ~0.50 cents MSE.
- Aggregation horizons examined: T = 5, 10, 20, 50 market orders; lag-1 response also studied.
- Regression only reported for TSLA, not the other three stocks. R-squared values not reported numerically. Author warns R-squared is a poor metric for time-series prediction.

**Actionable rule:** Model expected price move as lambda * signed flow over the last ~10 trades (window in trade count, not clock time) and cap or compress it for large imbalance because impact is concave. Fit lambda per market; do not port 0.011. Do not expect a tree/ML model to reliably beat a linear one; the CV evidence is mixed.

**Caveats:** Four stocks, one half-year, dissertation-level work. ML gain is not robust under cross-validation (high tree SD). Level-1 only. Outliers removed; market orders reconstructed from executions.

---

## 2112.13213 Cross-Impact of Order Flow Imbalance in Equity Markets

Authors: Cont, Cucuringu, Zhang (v4, May 2023).

**Data used:** LOBSTER/Nasdaq ITCH, top 100 S&P 500 stocks by market cap, 2017-01-01 to 2019-12-31, 10:00-15:30 only, 1-minute returns and OFI, 30-minute non-overlapping estimation windows.

**OFI:** Best-level OFI per Cont et al. (2014); multi-level OFI over top 10 levels scaled by average depth; integrated OFI = first principal component of the 10 level-OFIs, normalized to sum to 1, explaining ~89% of variance across levels.

**Contemporaneous impact R-squared (% in-sample / out-of-sample):**
- Best-level own OFI: 71.16 / 64.64
- Best-level plus cross-impact: 73.87 / 66.03
- Integrated own OFI: 87.14 / 83.83
- Integrated plus cross-impact: 87.85 / 83.62

**Other numbers:**
- Cross-impact beats own-stock model for ~91% of stocks (1% level) with best-level OFI, only ~28% with integrated OFI.
- 1-minute-ahead forecast: out-of-sample R-squared near zero for all models, but cross-impact models (FCI, FCII, CAR) statistically beat own-stock ones at the 1% level. Forecast-implied strategy (no costs): about 0.43 (FCI[1]) vs 0.21 (FPI[1]); units unclear.
- Cross-asset predictability strongest within several minutes, decays quickly; cross-asset PnL falls faster than own-stock up to 30 minutes.

**Actionable rule:** Aggregate OFI across several book levels (not just touch) into one signal; this lifts contemporaneous explanation from about 65% to 84% out-of-sample R-squared. For prediction-market bots, lagged order flow from correlated contracts (same event family, related outcomes) is a candidate short-horizon (minutes) skew input; its edge decays within minutes.

**Caveats:** US large caps, 2017-2019, no trading costs in strategy results, near-zero forecast R-squared (contemporaneous fit is not predictive power). Mechanisms for cross-asset effect are proposed, not tested. Equities with deep books, unlike thin prediction markets.

---

## What this means for the bot

1. **Skew first, widen second.** Order imbalance moves the quote mid at first order and the width only at second order (2608.07690). With q = share of recent flow in one direction, shift mid by about (w/2) ln(q/(1-q)) away from the dominant side even at zero inventory; add only a small width bump of about 2w(q-1/2)^2.
2. **Separate three skew terms:** inventory skew (A-S style, grows with q), flow/imbalance skew (above), and an adverse-selection term. Keep them additive and tune separately.
3. **Segment counterparties/fills.** Informed or inventory-sniffing flow deserves less inventory skew and wider quotes, while uninformed flow gets full skew (2508.20225). Never drop skew entirely: it raises risk.
4. **Impact estimate:** expected short-term price move = lambda * signed volume over the last ~10 trades, linear for small imbalance, compressed beyond (2004.08290). Estimate lambda per market online; the TSLA 0.011 is not transferable.
5. **Multi-level aggregation and cross-asset terms:** use depth-weighted multi-level OFI instead of touch only (2112.13213); add lagged OFI from related contracts as a minute-scale input, expecting fast decay.
6. **Backtest honesty:** mark each fill adverse or not by the next mid move; assume most passive fills (65-89% in the futures sample) are adverse and discount non-adverse fill probability (paper heuristic 0.2). Treat any backtest assuming 100% benign fills as overstated (2409.12721).
7. **Caution on evidence quality:** two papers are theory/simulation only (2608.07690, 2508.20225); the empirical ones are equities/futures with deep books, one day (2409.12721) or four stocks (2004.08290). All thresholds must be re-fit on prediction-market data, where books are thin, prices are bounded in [0,1], and resolution risk adds a jump component none of these models include.
