# Synthesis: prediction-market market-making + set-arbitrage bot

Evidence base: six sweep notes (about 30 papers). Only 2610.26789, 2602.19520, 2603.03152, 2609.28965 and the three arbitrage papers use prediction-market data. The rest are equity, FX or futures papers, or simulations, and transfer by mechanism only. Numbers come from fetch summaries and were not checked against PDFs.

## 1. Mean reversion / momentum

**Evidence.** No paper shows an exploitable hourly or daily mean-reversion edge.
- After the 2024 debate shock the Trump YES gain retraced about half in 3 h (+4c to +2c). After the assassination attempt it extended (+5c to +10c) [2603.03152]. The sign depends on the shock type.
- Order flow persists at 6 h (flow-history ROC-AUC 0.78) [2609.28965].
- The only fast reversion is in the sum of complementary legs: half-life about 4-5 five-minute intervals, roughly 20 min (my inference), on Betfair [2609.22639]. This is likely microstructure, which a maker already harvests.
- A static calibration bias exists. Politics and long-dated markets are compressed toward 50% (logit slope 1.2-1.8 on Kalshi, 1.2-1.5 on Polymarket). Near expiry the slope is about 1 [2602.19520].
- Markets near even odds with recent movement carry 5-7x lift for leader flips [2609.28965].

**Backtestable rule (hypotheses to test, not validated).**
1. *Leg-sum reversion.* Every 5 min compute S = mid(YES) + mid(NO). Keep an EWMA mean and sd. If z > 2, lean the reservation price down on both legs by 0.25 x (S - mean). If z < -2, lean up. Exit at z = 0 or after 30 min. Measure 5-step markout net of spread and fees.
2. *Shock fade, gated.* After a move of at least 3c in 30 min with no confirming move in linked markets, fade 50% of the move over 3 h. If linked markets confirm, do not fade and widen instead [2603.03152].
3. *Calibration skew.* Set p* = sigmoid(a + b logit p) per domain and horizon bucket, with b shrunk halfway toward 1. Apply it to the reservation price only, never as standalone alpha [2602.19520].

## 2. Quote size vs distance from mid

**What the evidence says.** There is no validated formula for a prediction market.
- The only real-book RL study had negative held-out value for every configuration (best -4.3 ticks, one seed). The best configurations won by trading rarely [2610.26789].
- In simulation, small size works best at the touch. Larger size is better split across levels 1 and 2. With informed flow, shift to levels 2-3. Simple inventory size-skew failed (-1.44 vs +4.99 for the learned policy) once informed traders appeared [2608.18195].
- Near-touch liquidity churns fast: 76% of orders live under 10 s [2106.11691].
- On a large-tick futures contract, the optimal policy defaults to not quoting. Quote only with good queue priority and balanced, long queues [1806.05101].
- Quoting far outside a roughly 2-tick book earns nothing [2610.26789].

**Recommended ladder.** Use three levels at 1, 2 and 3 ticks from the touch, plus an explicit no-quote weight.
- Calm regime: 50% / 30% / 20% of the quote budget.
- Informed or post-shock regime: 15% / 35% / 35%, with a faster requote.
- Leave 10-20% of the budget unallocated.
- Cap total inventory hard and switch off the side that adds to the position [1903.07222].
- Shrink size into expiry near p = 0.5 using the term gamma_T q^2 p(1-p) [2607.17991].

## 3. Fill probability and adverse selection (priors for A and k)

Model: P(fill | delta) = A exp(-k delta) [2607.28323, 2307.04863].

- **Concentration at the touch.** Over 85-90% of executions happen at or within one tick of the best quote. Deeper fills are negligible [2403.02572].
- **One tick deeper.** About 0.2-2% vs 2-3% at the touch in FX, a fall of roughly 2-4x [2403.02572].
- **Inside the spread.** Inside-spread orders fill 3-6x more often than orders at the best level in large-tick stocks [2306.05479].
- **Fill rate per horizon.** About 2% (crypto, 1 s) to 4% (equities, 10 s) [2307.04863].
- **k varies 8x across instruments** (0.48 to 3.7 per $), so fit it per market on a rolling window [2607.28323].
- **Starting priors (my choice, not from the papers).** A = 0.03 per 10 s at the touch, k = 0.7-1.0 per tick. Overwrite with your own fills within days.
- **Adverse share of passive fills.** 65-89% in the futures sample [2409.12721]. In backtests use non-adverse fill probability rho of about 0.2 and discard any backtest that assumes rho = 1.
- **Markout.** -0.06 ticks at the touch (TOP1) in simulation, better one tick deeper [2608.18195].
- **Distance rule.** delta* = 1/k - V, where V is the post-non-fill adverse move [2307.04863].
- **Backtest haircuts.** Cut naive-replay PnL by 20-63% [1903.07222], and discount back-of-queue fills [1806.05101].
- **Fill probability depends on queue position.** Removing it dropped IAUC from 0.76 to 0.57 [2512.05734].

## 4. Flow-pressure skew rules

1. Skew first, widen second. With q the recent fraction of one-sided flow, shift the mid by (w/2) ln(q/(1-q)) away from the dominant side. Add a width bump of only about 2w(q - 0.5)^2 [2608.07690]. At q = 0.7 this is about 0.42w.
2. Estimate the expected move as lambda x signed volume over the last 10 trades, and compress it for large imbalance because impact is concave. Fit lambda per market [2004.08290].
3. Aggregate OFI over several book levels. Out-of-sample R-squared rose from 65% to 84% [2112.13213]. Add lagged OFI from linked contracts, which decays within minutes.
4. In the first 30 min after a shock, flow is one-sided (94% at the withdrawal) [2603.03152]. Withdraw or widen the exposed side, but discount volume that was already priced in.
5. Keep inventory skew, flow skew and the adverse-selection term separate and additive. Never drop skew entirely [2508.20225].
6. Use a per-fill 5-step markout kill switch per market [2610.26789].

## 5. Complementary-contract arbitrage sizing

- **Frequency.** Headline frequencies shrink under executable criteria.
  - VWAP data showed 41% of conditions with a mis-sum [2508.03474].
  - L1 NBA data showed single-market arbitrage in 0.0001% of in-game time [2605.00864].
  - Executable profit was about $291k vs tens of millions naive [2608.00666].
- **Persistence.** Median windows last 2.9 s (venue-asserted partition/mutex), 3.6 s (single market), 6.3 s (venue-derived), 16 s (combinatorial or CLOB YES-side) and about 31 s (semantic). Legs are non-atomic. A 1 s delay retains 79% of profit [2610.23517].
- **Sizing.** Size to the thinnest leg's displayed depth after taker fees.
  - Typical executable size is about 15 shares, and 77% of combinatorial episodes cannot fill $100 [2605.00864].
  - Use a $10 minimum, a roughly $100 cap per basket, and at most 5 legs.
  - Exclude post-resolution snapshots: 81% of raw single-market detections were artifacts.
- **Capital and execution.** Prefer NegRisk NO-to-YES conversion baskets, which have short lock-up. YES baskets need capital held to resolution [2608.00666].
- **Role in the bot.** Treat sum(YES asks) < 1 net of fees as a quote-skew signal, not the main PnL source.

## 6. Open questions

- Is there hourly or daily drift conditional on shock type? The evidence is three events [2603.03152].
- What is the leg-sum half-life on the Polymarket CLOB (YES+NO) net of fees?
- What are the per-level fill and markout curves on 0-100c books?
- Does the calibration slope (a politics edge) survive spread and fees?
- Does the 50/30/20 ladder beat touch-only on real queues?
- How should the quoting band be defined near 0 and 1, where sigma_p = p(1-p) sigma_L shrinks [1105.3115, 2607.17991]?
- Which papers' figures survive a PDF check? Several summaries contained inconsistencies (2603.03152, 2409.12721).
