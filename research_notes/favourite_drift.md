# Favourite drift into election day: Polymarket / Kalshi history and what it means for SIG's 80/20 and 90/10 legs

Built 2026-10-22, 13 days before the 4 Nov close. Rebuild every table: `python3 research_notes/favourite_drift_study.py`
(reads the cache only; full output in `data/polymarket_history/tables.md`, numbers in `study_summary.json`).

## Answer

1. **No drift.** Over 20->13, 13->7, 7->1 and 13->1 days the favourite's price (0.75-0.95 at the window start) moves
   by -0.019 to +0.008 on average. No window survives a correction for the ~20 window x bucket cells tested, and the
   signs flip between adjacent windows. Kalshi 2024 shows the same. A martingale is not rejected, so the price path gives
   no timing edge: nothing is gained by buying favourites now rather than later, or by selling them now and buying back.
2. **Favourites are underpriced, not overpriced.** 47 of 47 favourites priced 0.75-0.95 at 13 days won, against 41.0
   expected (exact one-sided Poisson-binomial p = 0.002). At 20 days 50 of 52 won (45.3 expected). The logistic calibration slope is b = 1.8-2.6
   (b > 1 means prices are compressed toward 50%), which is the favourite-longshot bias of the literature. It holds in
   2022 (13/13 vs 11.0) and 2024 (29/29 vs 25.7), cycles whose national polling errors ran in opposite directions.
   Buying the favourite and holding to resolution returned +150 per 1,000 at 13 days. Selling it lost -1,000 per 1,000.
3. **On SIG the edge is in specific legs, not in the 80/20 or 90/10 bands as such.** Bought at SIG's ask and held to 4 Nov,
   the average 75-95 leg earns +2 per 1,000 if Polymarket/Kalshi are right, and +58 with the bias fitted under a prior shrinking it toward zero (MAP).
   The 75-85 band is priced *above* the outside blend (-33 per 1,000 at outside prices; +59 only if the bias is real). Fifteen legs, mostly 85-92 favourites, sit 5-12 points
   under the outside blend and earn +60 to +140 per 1,000. That beats arbs (+20 to +30 per 1,000 to settlement) per unit
   of capital, at a risk of losing the whole stake on an upset.
4. **Recommendation: no new drift sleeve.** Keep the current strategy. Within the existing directional limits (300 per
   leg, 3,000 total), fill the five +60 to +75 legs that the direction sleeve does not hold yet. Raising the total cap to
   4,500, so all 15 legs carry 300, adds roughly +100 to +250 of expected value. That is Sizhe's call, not a research result.
   Never take the other side: the data say selling 75-95 favourites (buying their 5-25% longshots) is the losing trade.
## Data (sources at the end)

| source | races | legs | prices |
|---|---|---|---|
| Polymarket 2024: Senate 33, governor 11, House 19, presidential states and districts 56 | 119 | 242 | CLOB daily history |
| Polymarket 2022 midterms: Senate 14, governor 14, House 1 | 29 | 64 | rebuilt from on-chain AMM trades (FPMMBuy/Sell logs); noisy in thin markets |
| Polymarket 2023-2026 two-way races: 7 national runoffs, 4 US specials, WI Supreme Court 2025, NJ/VA governor 2025 | 14 | 30 | CLOB daily history |
| Kalshi 2024 (replication only: same races, markets opened 7-31 Oct 2024) | 107 | 212 | daily candles, bid/ask mid |

Filters: lifetime volume >= 2,000 USD and >= 5 price points in the last 30 days. Unit: one favourite per race per horizon,
the higher-priced leg, kept when its price is 0.75-0.95. Prices: last trade/mid at or before election-day 12:00 UTC minus
d days. 162 Polymarket races pass the filters. Favourites in the band: 52 at 20 days, 47 at 13 and 7 days, 45 at 1 day.
Dataset: `data/polymarket_history/legs.csv`, `extra_legs.csv`, `kalshi_legs.csv`, `daily_paths.csv`.

## Drift of the favourite's price (Polymarket, all cycles)

| window | bucket | n | mean chg | median | rose | sign p | E[rose] martingale | t-test p | bootstrap 95% CI | cluster CI |
|---|---|---|---|---|---|---|---|---|---|---|
| 20d->13d | all | 52 | -0.019 | +0.000 | 24/49 | 1.00 | 0.55 | 0.045 | [-0.039, -0.002] | [-0.044, -0.004] |
| 13d->7d | all | 47 | +0.006 | +0.007 | 28/42 | 0.044 | 0.55 | 0.19 | [-0.003, +0.016] | [-0.005, +0.022] |
| 7d->1d | all | 47 | -0.010 | -0.005 | 21/47 | 0.56 | 0.55 | 0.27 | [-0.030, +0.006] | [-0.036, -0.000] |
| **13d->1d** | 75-85 | 18 | +0.010 | +0.010 | 11/18 | 0.48 | 0.55 | 0.32 | [-0.008, +0.030] | [-0.014, +0.039] |
| **13d->1d** | 85-92 | 16 | +0.019 | +0.037 | 12/16 | 0.08 | 0.57 | 0.09 | [-0.003, +0.038] | [+0.006, +0.034] |
| **13d->1d** | all | 47 | +0.008 | +0.015 | 29/46 | 0.10 | 0.57 | 0.20 | [-0.005, +0.020] | [-0.003, +0.027] |
| 1d->resolution | all | 45 | +0.101 | +0.135 | 43/45 | <0.001 | 0.85 | 0.002 | [+0.035, +0.149] | [+0.070, +0.141] |

Kalshi 2024: 20->13 -0.009 (p 0.35), 13->7 -0.007 (p 0.22), 7->1 -0.008 (p 0.50), 13->1 -0.011 (p 0.41), n 18-20.Reading it. "Rose more often than not" is **not** evidence of upward drift. Under a martingale a favourite usually creeps
up and occasionally collapses, so the expected share of rises is about 55%, not 50%. That share comes from a probit-Brownian
model of the latent margin, fitted to the observed probit variance: one election-night polling error is worth about 130
days of pre-election news. Against that null no window's rise count is unusual (13->7 p 0.12). The only nominal t-test
hit (20->13, p 0.045) comes from three legs (Slovakia and Poland runoffs, AZ-06), without which it is -0.007. With ~20 cells tested it is the expected false positive.
The cluster bootstrap resamples whole cycle x office groups, so one national polling miss cannot count as many races.
Legs inside 75-95 at 20, 13 *and* 7 days, as briefed: +0.000, +0.007, -0.004 (path-conditioned; `tables.md`).

## Calibration: is 0.90 really 0.90?

| horizon | bucket | n | mean price | won | P(wins >= obs), exact | buy fav per 1,000 [price-only CI] | sell fav per 1,000 |
|---|---|---|---|---|---|---|---|
| 20d | 75-85 | 14 | 0.790 | 12 (0.857) | 0.41 | +87 [-183, +277] | -294 |
| 20d | 85-92 | 26 | 0.883 | 26 (1.000) | 0.039 | +134 [+123, +144] | -1,000 |
| 13d | 75-85 | 18 | 0.810 | 18 (1.000) | 0.023 | +235 [+221, +249] | -1,000 |
| 13d | 85-92 | 16 | 0.893 | 16 (1.000) | 0.41 | +121 [+109, +133] | -1,000 |
| 13d | all | 47 | 0.873 | 47 (1.000) | 0.002 | +150 (upset rate <= 6.4%) | -1,000 |
| 1d | all | 45 | 0.855 | 43 (0.956) | 0.030 | +119 [+32, +181] | -797 |

Hosmer-Lemeshow over the three buckets (df 3): 20d p 0.20, 13d p 0.07, 7d p 0.17, 1d p 0.26. Each bucket is too small to
reject on its own. The pooled and slope tests carry the result:

| horizon | races with a 50-99 favourite | a (se) | b (se) | b = 1 p | implied P(win) at 0.80 / 0.90 / 0.95 |
|---|---|---|---|---|---|
| 20d | 150 | +0.08 (0.44) | 1.80 (0.52) | 0.12 | 0.929 / 0.983 / 0.995 |
| 13d | 156 | -0.43 (0.51) | 2.59 (0.77) | 0.04 | 0.959 / 0.995 / 0.999 |
| 7d | 159 | -0.02 (0.49) | 2.30 (0.74) | 0.08 | 0.959 / 0.993 / 0.999 |
| 1d | 157 | -0.28 (0.43) | 1.95 (0.53) | 0.07 | 0.919 / 0.982 / 0.996 |

By cycle, at 13 days: 2022 13/13 vs 11.0 expected (P(all win) 0.12), 2024 29/29 vs 25.7 (0.03), 2025 4/4, 2026 1/1.
Caveats (independent Fable review): outcomes share a cycle shock, so the 47/47 p rises to 0.007 at within-cycle correlation
0.5 and 0.026 at 0.7 (effective n about 27-37). The CIs on buy P&L reflect price dispersion only, because there were no
upsets. The sign of the bias is solid; its size is an upper bound. Calibration is symmetric by party (2024 D 15/15, R 13/13),
and dropping the search-picked runoffs leaves 43/43. The SIG mapping uses a MAP fit with priors a ~ N(0, 0.25^2) and
b ~ N(1, 0.25^2): the 13d raw fit (a -0.43, b 2.59) becomes a +0.14, b 1.37. A fair edge from the bias alone is +20 to +60 per 1,000.

Literature agrees in sign: Page and Clemen (2013) find prices biased toward 50%, more so far from expiry; Snowberg and
Wolfers (2010) trace the favourite-longshot bias to misperceived probabilities; Bürgi, Deng and Whelan find Kalshi
favourites earn small positive returns and longshots lose; Polymarket calibration studies find prices compressed toward 0.50.

## SIG mapping (snapshot of the bot's venue_quotes, 2026-10-22 12:37 UTC; 70 legs with ask 0.75-0.95)

EV per 1,000 bought at SIG's ask and held to 4 Nov, net of the venue's own compression, because the price paid *is* the
venue price:

| SIG bucket | legs | mean ask | outside blend | EV/1k, outside is right | EV/1k, MAP bias | EV/1k, raw fit (upper) | legs >= +50 |
|---|---|---|---|---|---|---|---|
| 75-85 ("80/20") | 20 | 0.805 | 0.779 | -33 | +59 | +137 | 3 |
| 85-92 ("90/10") | 30 | 0.893 | 0.915 | +25 | +77 | +103 | 9 |
| 92-95 | 20 | 0.939 | 0.941 | +3 | +30 | +31 | 3 |

Drift adds nothing: the 13->1 day change is +0.008 (CI -0.005 to +0.020), about +9 per 1,000 in marks and zero at settlement.
Legs >= +50 per 1,000 at the outside price (`study_summary.json` has all 70). Held now: the bot's direction sleeve.

| leg | SIG ask | outside | EV/1k | EV/1k MAP | held now |
|---|---|---|---|---|---|
| Rhode Island Senate D (and R NO) | 0.870 | 0.984 | +131 | +146 | yes, both legs |
| Mississippi Senate R (via D NO) | 0.800 | 0.893 | +117 | +193 | yes, D NO 375 |
| Georgia Senate D (via R NO) | 0.880 | 0.958 | +89 | +123 | yes, R NO 172 |
| NH-01 House D | 0.890 | 0.957 | +75 | +110 | **no** |
| Tennessee Senate R (via D NO) | 0.895 | 0.961 | +74 | +105 | yes, D NO 170 |
| Rhode Island Governor D | 0.895 | 0.959 | +72 | +104 | **no** |
| New Hampshire Senate D | 0.840 | 0.900 | +71 | +141 | yes, 368 |
| Tennessee Governor R | 0.875 | 0.936 | +69 | +118 | **no** |
| Massachusetts Senate D / Governor D | 0.920 / 0.905 | 0.982 / 0.964 | +68 / +65 | +83 / +94 | yes, 329 / 276 |
| Florida Senate R | 0.845 | 0.900 | +65 | +134 | **no** |
| Maine Governor D | 0.895 | 0.948 | +60 | +99 | **no** |

## Against the position-neutral alternatives (per 1,000 of capital, 13 days)

| use of 1,000 | expected | risk | binding constraint |
|---|---|---|---|
| set arbs held to settlement | +20 to +30 (2-3%) | none | opportunities (~2.5k total profit capacity), not cash |
| market making, +0.1 to +0.2 tick per fill | about +10 per day for the whole sleeve (~7k/day volume) | inventory | size and markouts, not cash (resting orders do not escrow) |
| favourite in the 15 gap legs | +60 to +140 | SD ~250 per leg; an upset loses the stake | directional caps |
| average 80/20 leg on SIG | -33 to -11 | same | none: do not buy |

Cash is not the binding constraint: about 45k is free against 100k. So the directional legs add to the arbs and market
making rather than displace them. Exposure: 15 legs x 300 = 4,500 notional, 10 D and 5 R, so net-D is about 1,500 under the
5,000 partisan cap. Expected value is +335 at outside prices and +500 with the MAP bias, with SD about 280 if the races were
independent. Worst case is -4,500, far inside the 50k limit. The sleeve already holds about 2,000 of the 3,000 cap. Filling
the five unheld legs at about 200 each fits inside 3,000. All 15 at 300 needs the total cap raised to 4,500.

In prize terms all of this is noise: +500 against a +850k to +980k leaderboard. It is worth doing only because it is
positive expected value at low risk. The planned late longshot bet is the trade this study scores worst. Historically,
selling a 75-95 favourite lost 800-1,000 per 1,000, and SIG longshots trade 3-7 points above the outside price. Treat that
bet as a lottery ticket for rank, not as edge.
## Sources

- Polymarket Gamma `https://gamma-api.polymarket.com/events?id=<id>`; discovery via `/public-search` (queries in `data/polymarket_history/sources.json`).
- Polymarket CLOB `https://clob.polymarket.com/prices-history?market=<token>&interval=max&fidelity=1440`.
- 2022 AMM trades: Polygon `eth_getLogs` FPMMBuy/FPMMSell via public RPC (polygon.gateway.tenderly.co); see `research_notes/favourite_drift_extra.py`.
- Kalshi `https://api.elections.kalshi.com/trade-api/v2/historical/markets` and `/historical/markets/{ticker}/candlesticks`; see `research_notes/favourite_drift_kalshi.py`.
- Page, L. and Clemen, R. (2013), Do prediction markets produce well-calibrated probability forecasts? Economic Journal 123(568), https://people.duke.edu/~clemen/bio/Published%20Papers/45.PredictionMarkets-Page&Clemen-EJ-2013.pdf
- Snowberg, E. and Wolfers, J. (2010), Explaining the favorite-longshot bias, JPE 118(4), https://www.nber.org/papers/w15923
- Bürgi, C., Deng, W. and Whelan, K., Makers and Takers: the economics of the Kalshi prediction market, https://www.karlwhelan.com/Papers/Kalshi.pdf
- Polymarket calibration: https://www.lesswrong.com/posts/Hruc6Gwo3vBZFGb6v/empirical-calibration-of-polymarket-analysis-of-7-661 ; https://theses-dissertations.princeton.edu/entities/publication/d5708372-5ea3-4252-b669-7f9cee07387c
