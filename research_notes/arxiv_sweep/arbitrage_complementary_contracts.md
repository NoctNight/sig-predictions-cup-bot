# Arbitrage on complementary contracts: how often YES prices mis-sum and how to size

Sources: HTML versions of the four arXiv papers (first ~100k chars read for 2508.03474 and 2610.23517; full text for the others).

## 2508.03474 - Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets
- Data: Polymarket markets resolved 1 Apr 2024 - 1 Apr 2025; 8,659 single-condition and 1,578 NegRisk markets (17,218 conditions). About 86M on-chain bids (Polygon), per-block VWAP prices carried forward up to 5,000 blocks (~2.5 h). LLM (DeepSeek-R1-Distill-Qwen-32B) used for dependency detection.
- Key results:
  - 7,051 of 17,218 conditions (~41%) had at least one mis-sum: 2,628 NegRisk, 4,423 single. All single-condition cases were long (YES+NO < $1).
  - 662 of 1,578 NegRisk markets (~42%) had at least one opportunity, ~100 per market on average. Average max profit about $0.40 per dollar.
  - Realized profit estimate ~$39.59M total ($1 profit per trade assumed). Single-condition: $5.90M buying below $1, $4.68M selling above $1. Multi-outcome: buy YES $11.09M, buy NO $17.31M, sell YES $0.61M.
  - Top account: $2.01M over 4,049 transactions. About 1% of estimated US-election opportunities exploited.
  - Combinatorial: 1,576 of 46,360 election pairs flagged dependent; only 13 of 374 manually checked met the strict definition. Avg max profit per opportunity ~$100.
- Actionable rule: if YES sum < $1, buy YES on every outcome; if > $1, buy NO on all (or split and sell YES). Mis-sums are common in NegRisk markets on VWAP data.
- Caveats: VWAP averaging overstates persistence and tradable edge; non-atomic legs; no fees; max-profit assumes taking all liquidity; thresholds inconsistent (5c text vs 2c figures); LLM false positives; one year dominated by US election; $1/trade profit accounting is generous. Treat the 41% as an upper bound on tradable frequency.

## 2605.00864 - Arbitrage Analysis in Polymarket NBA Markets
- Data: 173 NBA games, 4 Feb - 4 Mar 2026; 75,088,497 top-of-book (L1) snapshots over 3,042 markets; 8.59M combined Moneyline/Spread states. CLOB GET /book polling, sweep 3.6-5.5 s, RTT ~171 ms.
- Key results:
  - Single-market: 37 raw detections, 30 (81.1%) post-game and discarded, 7 valid, all in-game, 0 pre-game. Time in arb 0.0001% of in-game time. Median duration 3.6 s. $210.19 capped at $100/episode ($4,418 uncapped, outlier-driven). Median yield 11.0%. 85.7% of episodes had depth for $100.
  - Combinatorial (ML ask + Spread-complement ask < 1): 523 candidates, 233 post-game artifacts, 290 executable (~2 per game). In-game 279 episodes (0.18% of time, median 16 s), pre-game 11 (median 11 s). Median yield 101 bps in-game, 309 bps pre-game. 17.2% lasted <= 4 s.
  - $559.59 capped total ($2,032.75 uncapped). 76.9% of episodes could not deploy $100; mean executable size ~14.8 synthetic shares. Zero "middle" double-payouts.
  - Median bid-ask spread 392 bps pre-game, 1,031 bps in-game.
- Actionable rule: single-market mis-sums are rare and brief, so size to displayed depth at the thinnest leg (min $10), expect ~15 shares, act within seconds (faster than a 3.6-5.5 s poll).
- Caveats: lower bound on frequency (misses sub-poll episodes); one month, one sport; only 7 single-market episodes; zero fees, gas and slippage unmodelled; L1 only; one-shot fill model.

## 2608.00666 - Executable Arbitrage and Market Efficiency in Prediction Markets
- Data: FPMM panel (51 outcome sets, 235 binary markets, ~53k trades); CLOB actor panel (32,702 events, 259M trades through 31 Dec 2025); Level-2 CLOB market-state panel 14 Apr - 19 May 2026 (hourly windows); 308M websocket messages over 7 days used for validation.
- Key results:
  - Mechanism-linked profit ~$1.118M, of which ~97% (~$1.086M) is converter-enabled (NegRisk NO-to-YES conversion); full-set NO-to-collateral conversions $205,531 (~18.9%).
  - Basket-level settlement profit: FPMM $3,639 (67 baskets, 19 events, 35 actors); CLOB $28,644 over 5,923 baskets (YES $14,199 / 4,014; NO $14,445 / 1,909). FPMM market-state benchmark $5,185.
  - Same Apr 2024 - Apr 2025 window under executable criteria: $291,424, versus $10.6M + $29M cited from the earlier paper (note: the prior estimates are far larger).
  - CLOB: 2,098 positive YES-side episodes vs 36 NO-side. 624 episodes observed; 366 (58.7%) touch window edges; of the remaining 258, YES-side median duration 16.15 s (253), NO-side 7.99 s (5 only). FPMM: ~40 episodes persist > 50 min, some > 1 day.
  - Protocol: NegRisk adapter converts only NO to YES before settlement; NO inputs must be acquired within 5 blocks (~10 s) of conversion; YES-side violations generally require holding the full basket to resolution. Gas for conversion grows from ~0.70M to 25.32M across 5-196 outcomes.
- Actionable rule: only count a mis-sum as exploitable if a realization path exists; prefer NO-side baskets via the adapter (short capital lock-up); budget for YES baskets held to resolution.
- Caveats: descriptive not causal; panels cannot be matched at opportunity level; all fills assumed taker; gas omitted; 381,748 USDC 75-conversion cluster (12 Dec 2025) excluded; mark-to-market included; minimum order size and fill probabilities not reported.

## 2610.23517 - Combinatorial Arbitrage in Prediction Markets
- Data: Polymarket, 23 Jun - 3 Aug 2026 (42 days); ~745k binary contracts; 73.22B order-book messages (3.6B valid two-sided quote states); 229.9M on-chain fills ($5.23B, 679k addresses). This is real data with a simulated arbitrageur (not purely a listing-summary claim).
- Key results:
  - 1,942,133 relations recovered, reduced to 573,024 constraints over 255k+ contracts. Layers: venue-asserted (NegRisk groups: 20,118 partition, 748 mutex), venue-derived (sports, 1,358,323), semantic-inferred (LLM + two cross-vendor auditors, 18.8-64.6% confirmation).
  - 491,827 arbitrage windows at best quotes after fees; 125,095 had at least one simulated trade against full-depth books.
  - ~$2.50M accumulated guaranteed profit; first execution per window $1.10M. 3.5% weekly risk-free return on $11.1M peak capital.
  - Profit by layer: venue-asserted 34%, venue-derived 59%, semantic 7%.
  - With 1 s order delay filled orders keep 79% of profit.
  - Median window duration (>= 0.3 s): ~2.9 s asserted, 6.3 s derived, ~31 s semantic. Other traders took every leg in 44% / 15% / 9% of windows, median lag ~3 s; top 10 of 854 addresses account for 78% of such windows.
- Actionable rule: simple partition/mutex mis-sums close fastest (~3 s); relations needing inference persist longer and are less contested, but need verified logic. Size from displayed depth after taker fees, cap legs at ~5.
- Caveats: simulated idealized arbitrageur; no price impact of own orders (first take = lower bound, accumulated = upper bound); only settled portfolios; 6 invalid relations found ex post; Polymarket only. Text beyond ~100k chars not read.

## What this means for the bot
- Frequency: tradable mis-sums are far rarer than headline numbers. The 41% (2508.03474) is VWAP-based over a year; L1 book data in NBA markets shows single-market mis-sums at 0.0001% of in-game time (2605.00864); executable profit is ~$1.1M vs tens of millions naive (2608.00666).
- Persistence: median windows 3-16 s (3.6 s single-market, 16 s combinatorial, 16 s CLOB YES-side, 2.9 s venue-asserted). Quote refresh and legging must be sub-second.
- Sizing: size by thinnest-leg displayed depth, not by theoretical edge. Typical executable size ~15 shares in NBA; 77% of combinatorial episodes cannot fill $100. Use a $10 min-size filter, per-basket caps ($100-scale in thin books), and hold-to-resolution capital for YES baskets.
- MM use: treat sum(YES asks) < 1 or sum(YES bids) > 1 net of fees as a quoting skew/inventory signal; widen or pull quotes on outcome legs when the complement sum is stale. Do not rely on arb as the main PnL source.
- Execution: legs are non-atomic; model 1 s delay (79% profit retained); prefer NegRisk adapter paths for NO-side baskets.
- Data quality: exclude post-resolution/near-settlement snapshots (81% of single-market raw detections were post-game artifacts).
