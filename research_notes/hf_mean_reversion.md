# High-frequency mean reversion on the Predictions Cup tape

Generated 2026-10-21 23:42 UTC by `python3 -m sigbot.hfstats`. Prices are YES prices; one tick = 0.005. Candles exist only for intervals that traded, so bar clocks are event clocks; calendar clocks forward-fill.

Data: candles_1m.json: 60 markets, median 1000 bars, 2026-10-01 16:13 to 2026-10-21 23:35 UTC; candles_5m.json: 237 markets, median 1000 bars, 2026-10-01 16:00 to 2026-10-21 22:25 UTC; hist.json: 237 markets, median 200 bars, 2026-10-01 16:00 to 2026-10-21 22:00 UTC.

## 1. Autocorrelation of price changes by horizon (pooled, mid in [0.05, 0.95])

Two clocks. *Calendar*: forward-filled close on a fixed grid, non-overlapping changes (an interval without trades is a zero change). *Bar*: consecutive traded bars (the clock the bot's earlier -0.45 figure used). AR(1) is the OLS slope of the next change on the last change; t is Newey-West (Bartlett, 5 lags, autocovariances summed within market). The HAC t-statistics are small relative to the sample because the changes have a kurtosis in the thousands (a handful of fat-finger prints that reverse dominate the covariance), so the *wins* columns repeat the regression with changes winsorized at 4 ticks; that is the estimate that describes the typical bar. VR(k) is the Lo-MacKinlay variance ratio of k-horizon to 1-horizon changes on the calendar clock with the heteroskedasticity-robust z; VR < 1 means reversion.

| horizon | markets | n cal | AR(1) cal | t | AR(1) cal wins | t | n bar | AR(1) bar | t | AR(1) bar wins | t | VR(2) z | VR(4) z | VR(8) z | VR(16) z |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1m | 60 | 735046 | -0.122 | -2.6 | -0.180 | -34.5 | 56490 | -0.692 | -4.2 | -0.416 | -68.8 | 0.76 (-55.5) | 0.62 (-54.1) | 0.39 (-2.9) | 0.26 (-1.9) |
| 5m | 237 | 1030840 | -0.310 | -4.4 | -0.186 | -69.7 | 208397 | -0.441 | -6.6 | -0.363 | -120.3 | 0.64 (-3.4) | 0.46 (-3.4) | 0.33 (-3.6) | 0.24 (-3.8) |
| 15m | 237 | 343201 | -0.258 | -4.4 | -0.206 | -59.0 | 69205 | -0.251 | -6.5 | -0.264 | -50.8 | 0.69 (-2.9) | 0.51 (-3.1) | 0.38 (-3.4) | 0.31 (-3.5) |
| 30m | 237 | 171319 | -0.191 | -6.0 | -0.206 | -48.2 | 34377 | -0.162 | -4.7 | -0.171 | -24.0 | 0.76 (-7.4) | 0.60 (-8.1) | 0.49 (-8.8) | 0.42 (-9.2) |
| 1h | 237 | 75396 | -0.152 | -20.3 | -0.170 | -30.0 | 44029 | -0.205 | -25.2 | -0.232 | -37.4 | 0.83 (-20.6) | 0.72 (-19.6) | 0.63 (-18.7) | 0.55 (-17.5) |
| 4h | 237 | 18533 | -0.100 | -8.7 | -0.119 | -12.5 | 10669 | -0.061 | -5.3 | -0.087 | -7.9 | 0.88 (-10.5) | 0.76 (-11.6) | 0.62 (-12.7) | 0.48 (-13.1) |

## 2. Reversion by US Eastern time of day and weekday/weekend

Calendar clock, non-overlapping changes; the bucket is the ET time at which the lagged change ends (the moment a fade would be placed). AR(1) and t are on raw changes (HAC); *wins* winsorizes changes at 4 ticks. diff z compares the bucket's winsorized slope with the winsorized slope on all other observations (independent HAC standard errors); the Wald chi-square (3 df) tests equality of the winsorized slopes across the four buckets. VR(2) and VR(4) are on raw changes within the bucket (windows never cross a bucket boundary) with their z-statistics.

### 1m

| bucket | n | AR(1) | t | AR(1) wins | t | diff vs rest z | VR(2) z | VR(4) z |
|---|---|---|---|---|---|---|---|---|
| overnight 00-07 | 211458 | -0.173 | -12.9 | -0.180 | -18.9 | -0.1 | 0.78 (-19.1) | 0.61 (-18.5) |
| morning 07-12 | 154382 | -0.169 | -6.2 | -0.151 | -12.8 | 2.7 | 0.80 (-6.1) | 0.63 (-7.1) |
| afternoon 12-17 | 157815 | -0.055 | -1.4 | -0.192 | -18.6 | -1.4 | 0.75 (-69.1) | 0.62 (-66.2) |
| evening 17-24 | 211386 | -0.232 | -5.3 | -0.187 | -18.6 | -0.9 | 0.77 (-15.2) | 0.63 (-14.8) |
| Wald equality (df=3) | | | | | | chi2=8.0 | | |
| weekday | 517169 | -0.113 | -2.2 | -0.193 | -33.3 | -4.1 | 0.76 (-81.3) | 0.62 (-73.4) |
| weekend | 217877 | -0.172 | -7.1 | -0.140 | -12.1 | 4.1 | 0.82 (-6.1) | 0.67 (-7.2) |

### 5m

| bucket | n | AR(1) | t | AR(1) wins | t | diff vs rest z | VR(2) z | VR(4) z |
|---|---|---|---|---|---|---|---|---|
| overnight 00-07 | 298288 | -0.181 | -28.0 | -0.186 | -37.5 | -0.1 | 0.80 (-28.0) | 0.63 (-30.0) |
| morning 07-12 | 215258 | -0.180 | -21.4 | -0.179 | -31.4 | 1.4 | 0.80 (-21.4) | 0.64 (-23.6) |
| afternoon 12-17 | 220290 | -0.412 | -3.6 | -0.192 | -33.7 | -1.4 | 0.53 (-2.4) | 0.33 (-2.3) |
| evening 17-24 | 296959 | -0.208 | -12.9 | -0.185 | -37.0 | 0.2 | 0.77 (-11.0) | 0.58 (-12.9) |
| Wald equality (df=3) | | | | | | chi2=2.9 | | |
| weekday | 734726 | -0.330 | -4.1 | -0.182 | -59.3 | 2.0 | 0.62 (-3.1) | 0.43 (-3.0) |
| weekend | 296111 | -0.202 | -27.2 | -0.195 | -36.8 | -2.0 | 0.79 (-23.8) | 0.60 (-26.9) |

### 15m

| bucket | n | AR(1) | t | AR(1) wins | t | diff vs rest z | VR(2) z | VR(4) z |
|---|---|---|---|---|---|---|---|---|
| overnight 00-07 | 99357 | -0.206 | -20.2 | -0.198 | -29.6 | 1.3 | 0.78 (-18.1) | 0.61 (-19.4) |
| morning 07-12 | 71656 | -0.202 | -22.2 | -0.206 | -27.6 | -0.0 | 0.79 (-20.4) | 0.63 (-21.4) |
| afternoon 12-17 | 73320 | -0.302 | -2.8 | -0.208 | -28.9 | -0.4 | 0.61 (-1.6) | 0.41 (-1.6) |
| evening 17-24 | 98766 | -0.232 | -14.2 | -0.210 | -32.3 | -0.9 | 0.75 (-11.2) | 0.57 (-12.1) |
| Wald equality (df=3) | | | | | | chi2=1.9 | | |
| weekday | 244549 | -0.266 | -3.9 | -0.201 | -48.8 | 2.2 | 0.68 (-2.5) | 0.49 (-2.6) |
| weekend | 98642 | -0.222 | -27.0 | -0.218 | -33.6 | -2.2 | 0.77 (-23.6) | 0.60 (-24.1) |

### 30m

| bucket | n | AR(1) | t | AR(1) wins | t | diff vs rest z | VR(2) z | VR(4) z |
|---|---|---|---|---|---|---|---|---|
| overnight 00-07 | 49633 | -0.208 | -17.4 | -0.208 | -25.3 | -0.3 | 0.77 (-16.1) | 0.60 (-16.5) |
| morning 07-12 | 35761 | -0.194 | -18.1 | -0.202 | -23.0 | 0.5 | 0.80 (-16.0) | 0.64 (-17.1) |
| afternoon 12-17 | 36560 | -0.162 | -2.1 | -0.212 | -23.4 | -0.7 | 0.73 (-2.4) | 0.58 (-2.5) |
| evening 17-24 | 49202 | -0.217 | -12.7 | -0.202 | -25.7 | 0.5 | 0.77 (-8.8) | 0.60 (-9.7) |
| Wald equality (df=3) | | | | | | chi2=0.9 | | |
| weekday | 122008 | -0.184 | -4.7 | -0.202 | -40.6 | 1.4 | 0.76 (-5.8) | 0.59 (-6.4) |
| weekend | 49294 | -0.216 | -23.0 | -0.215 | -26.1 | -1.4 | 0.77 (-21.3) | 0.63 (-19.8) |

### 1h

| bucket | n | AR(1) | t | AR(1) wins | t | diff vs rest z | VR(2) z | VR(4) z |
|---|---|---|---|---|---|---|---|---|
| overnight 00-07 | 21847 | -0.156 | -11.7 | -0.174 | -15.9 | -0.4 | 0.81 (-16.6) | 0.65 (-17.9) |
| morning 07-12 | 15799 | -0.148 | -9.0 | -0.179 | -14.6 | -0.8 | 0.87 (-10.0) | 0.74 (-12.3) |
| afternoon 12-17 | 16080 | -0.158 | -10.3 | -0.160 | -12.8 | 0.9 | 0.82 (-12.4) | 0.73 (-11.0) |
| evening 17-24 | 21490 | -0.153 | -10.3 | -0.172 | -16.3 | -0.2 | 0.82 (-9.3) | 0.69 (-9.7) |
| Wald equality (df=3) | | | | | | chi2=1.3 | | |
| weekday | 53972 | -0.145 | -16.9 | -0.167 | -26.3 | 0.9 | 0.84 (-17.2) | 0.74 (-15.7) |
| weekend | 21408 | -0.174 | -11.6 | -0.179 | -14.5 | -0.9 | 0.81 (-11.8) | 0.69 (-11.6) |

## 3. Bid-ask bounce versus real reversion

Roll (1984): a price that alternates between bid and ask with a fixed mid has cov(dp_t, dp_t-1) = -s^2/4. Trade-to-trade changes isolate that mechanics; 5-minute vwap-to-vwap changes average the bounce out (at the cost of a Working-type positive bias from averaging); collapsing consecutive bars at the same close removes the zero changes created by sparse trading (which *strengthens* the negative autocorrelation of a pure bounce process, so it is a discreteness adjustment, not a bounce removal).

| series | n | AR(1) slope | t | rho | note |
|---|---|---|---|---|---|
| trade-to-trade, all trades | 45751 | -0.347 | -42.4 | -0.348 | tapes.json, 237 markets |
| trade-to-trade, price-changing trades only | 13341 | -0.566 | -57.9 | -0.569 | zero changes dropped |
| Roll implied spread from trades | 45740 | | | | s = 2 sqrt(-cov1) = 0.0041 (0.82 ticks); var(dp) = 1.24e-05 |
| same-aggressor trades only (buy) | 3509 | -0.134 | -4.7 | -0.135 | sigbot.sqlite, bot-classified aggressor; quote-to-quote, no bounce |
| same-aggressor trades only (sell) | 3985 | -0.103 | -4.2 | -0.106 | sigbot.sqlite, bot-classified aggressor; quote-to-quote, no bounce |
| 5m close-to-close (bar clock) | 208397 | -0.441 | -6.6 | -0.439 | |
| 5m vwap-to-vwap (bar clock) | 207202 | -0.353 | -7.3 | -0.375 | |
| 5m close-to-close, changes winsorized at 4 ticks | 208397 | -0.363 | -120.3 | -0.364 | robust to fat-finger prints |
| 5m vwap-to-vwap, winsorized at 4 ticks | 207202 | -0.334 | -101.4 | -0.335 | |
| 5m close, contiguous bars only | 68735 | -0.438 | -7.9 | -0.486 | both neighbours exactly 5 min away |
| 5m close, consecutive same-close bars collapsed | 78070 | -0.520 | -8.6 | -0.518 | every change non-zero |
| 5m vwap, bars within half a tick of the previous vwap collapsed | 75262 | -0.431 | -8.8 | -0.458 | |

Variance of a 5-minute close change (mid in [0.05, 0.95]) is 3.88e-05 (sd 1.25 ticks). A pure bounce across a one-tick spread contributes an autocovariance of -s^2/4 = -6.25e-06, i.e. rho = -0.161 of the 5-minute AR(1); with the Roll spread estimated from the tape (0.82 ticks) it is -0.109. 
The observed 5-minute close-to-close rho is -0.439; the vwap-to-vwap rho is -0.375. Bounce therefore explains about 25% of the close-to-close coefficient by the Roll decomposition, and the close-minus-vwap gap (-0.064) says 14% of it disappears when the bounce is averaged out. The remainder, rho of roughly -0.375 on vwap (and the same-aggressor quote-to-quote estimates above), is reversion of the quote itself: the order flow is mean-reverting, not just the trade print.

## 4. Event study: large 5m moves (sigma from the trailing 48 bar changes, bar clock)

R_k = cumulative change over the next k bars signed against the move (positive = reversion), in ticks. Passive fade P&L per share = R_k + half a tick (resting at the post-move close improved by one half tick, exit at the close k bars later); taker/passive = R_k - half a tick (cross the spread to enter, leave passively); taker fade P&L = R_k - one tick (crossing a 0.005 spread to enter, exiting at the close, i.e. paying the spread once in full). The post-move close is itself a print at the far quote, so about half the Roll spread (0.4 ticks) of R_k on close is bounce; the vwap tables are the cleaner measure. Events can overlap (one shock produces several bars), so the t-statistics are optimistic.

### close, |move| >= 2 sigma (14000 events, mean move 2.08 ticks)

| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |
|---|---|---|---|---|---|---|---|---|---|
| all | 14000 | 1 | 0.76 | 71.1 | 1.00 | 0.52 | 1.26 | 0.26 | -0.24 |
| all | 13964 | 3 | 0.79 | 66.9 | 1.00 | 0.55 | 1.29 | 0.29 | -0.21 |
| all | 13906 | 6 | 0.76 | 56.5 | 1.00 | 0.56 | 1.26 | 0.26 | -0.24 |
| all | 13793 | 12 | 0.76 | 45.8 | 1.00 | 0.56 | 1.26 | 0.26 | -0.24 |
| overnight 00-07 | 3646 | 1 | 0.80 | 37.7 | 1.00 | 0.53 | 1.30 | 0.30 | -0.20 |
| overnight 00-07 | 3645 | 3 | 0.84 | 36.3 | 1.00 | 0.57 | 1.34 | 0.34 | -0.16 |
| overnight 00-07 | 3639 | 6 | 0.81 | 30.8 | 1.00 | 0.57 | 1.31 | 0.31 | -0.19 |
| overnight 00-07 | 3626 | 12 | 0.80 | 25.4 | 1.00 | 0.57 | 1.30 | 0.30 | -0.20 |
| morning 07-12 | 3057 | 1 | 0.74 | 35.1 | 1.00 | 0.53 | 1.24 | 0.24 | -0.26 |
| morning 07-12 | 3056 | 3 | 0.75 | 31.6 | 1.00 | 0.55 | 1.25 | 0.25 | -0.25 |
| morning 07-12 | 3050 | 6 | 0.76 | 28.0 | 1.00 | 0.56 | 1.26 | 0.26 | -0.24 |
| morning 07-12 | 3033 | 12 | 0.76 | 22.9 | 1.00 | 0.56 | 1.26 | 0.26 | -0.24 |
| afternoon 12-17 | 3256 | 1 | 0.76 | 33.3 | 1.00 | 0.52 | 1.26 | 0.26 | -0.24 |
| afternoon 12-17 | 3244 | 3 | 0.80 | 33.4 | 1.00 | 0.56 | 1.30 | 0.30 | -0.20 |
| afternoon 12-17 | 3214 | 6 | 0.79 | 28.5 | 1.00 | 0.56 | 1.29 | 0.29 | -0.21 |
| afternoon 12-17 | 3142 | 12 | 0.79 | 22.5 | 1.00 | 0.56 | 1.29 | 0.29 | -0.21 |
| evening 17-24 | 4041 | 1 | 0.75 | 36.5 | 1.00 | 0.52 | 1.25 | 0.25 | -0.25 |
| evening 17-24 | 4019 | 3 | 0.75 | 32.9 | 1.00 | 0.54 | 1.25 | 0.25 | -0.25 |
| evening 17-24 | 4003 | 6 | 0.70 | 26.5 | 1.00 | 0.54 | 1.20 | 0.20 | -0.30 |
| evening 17-24 | 3992 | 12 | 0.71 | 21.5 | 1.00 | 0.55 | 1.21 | 0.21 | -0.29 |
| weekday | 10637 | 1 | 0.72 | 62.3 | 1.00 | 0.52 | 1.22 | 0.22 | -0.28 |
| weekday | 10601 | 3 | 0.75 | 57.5 | 1.00 | 0.55 | 1.25 | 0.25 | -0.25 |
| weekday | 10543 | 6 | 0.72 | 47.8 | 1.00 | 0.55 | 1.22 | 0.22 | -0.28 |
| weekday | 10430 | 12 | 0.71 | 37.9 | 1.00 | 0.56 | 1.21 | 0.21 | -0.29 |
| weekend | 3363 | 1 | 0.89 | 35.1 | 1.00 | 0.55 | 1.39 | 0.39 | -0.11 |
| weekend | 3363 | 3 | 0.90 | 34.5 | 1.00 | 0.58 | 1.40 | 0.40 | -0.10 |
| weekend | 3363 | 6 | 0.90 | 30.3 | 1.00 | 0.57 | 1.40 | 0.40 | -0.10 |
| weekend | 3363 | 12 | 0.92 | 25.9 | 1.00 | 0.58 | 1.42 | 0.42 | -0.08 |

### close, |move| >= 3 sigma (4499 events, mean move 2.50 ticks)

| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |
|---|---|---|---|---|---|---|---|---|---|
| all | 4499 | 1 | 0.85 | 38.7 | 1.00 | 0.54 | 1.35 | 0.35 | -0.15 |
| all | 4488 | 3 | 0.87 | 37.2 | 1.00 | 0.57 | 1.37 | 0.37 | -0.13 |
| all | 4475 | 6 | 0.80 | 29.5 | 1.00 | 0.55 | 1.30 | 0.30 | -0.20 |
| all | 4434 | 12 | 0.79 | 24.5 | 1.00 | 0.55 | 1.29 | 0.29 | -0.21 |
| overnight 00-07 | 1208 | 1 | 0.95 | 21.6 | 1.00 | 0.55 | 1.45 | 0.45 | -0.05 |
| overnight 00-07 | 1208 | 3 | 1.00 | 22.0 | 1.00 | 0.60 | 1.50 | 0.50 | -0.00 |
| overnight 00-07 | 1208 | 6 | 0.94 | 18.1 | 1.00 | 0.58 | 1.44 | 0.44 | -0.06 |
| overnight 00-07 | 1206 | 12 | 0.97 | 16.4 | 1.00 | 0.58 | 1.47 | 0.47 | -0.03 |
| morning 07-12 | 1011 | 1 | 0.80 | 18.9 | 1.00 | 0.54 | 1.30 | 0.30 | -0.20 |
| morning 07-12 | 1010 | 3 | 0.78 | 17.1 | 1.00 | 0.56 | 1.28 | 0.28 | -0.22 |
| morning 07-12 | 1008 | 6 | 0.79 | 15.1 | 1.00 | 0.56 | 1.29 | 0.29 | -0.21 |
| morning 07-12 | 1006 | 12 | 0.71 | 11.7 | 1.00 | 0.54 | 1.21 | 0.21 | -0.29 |
| afternoon 12-17 | 1035 | 1 | 0.80 | 18.2 | 1.00 | 0.53 | 1.30 | 0.30 | -0.20 |
| afternoon 12-17 | 1030 | 3 | 0.86 | 18.7 | 1.00 | 0.57 | 1.36 | 0.36 | -0.14 |
| afternoon 12-17 | 1024 | 6 | 0.74 | 14.1 | 1.00 | 0.54 | 1.24 | 0.24 | -0.26 |
| afternoon 12-17 | 990 | 12 | 0.75 | 11.4 | 1.00 | 0.54 | 1.25 | 0.25 | -0.25 |
| evening 17-24 | 1245 | 1 | 0.83 | 18.9 | 1.00 | 0.53 | 1.33 | 0.33 | -0.17 |
| evening 17-24 | 1240 | 3 | 0.82 | 16.9 | 1.00 | 0.55 | 1.32 | 0.32 | -0.18 |
| evening 17-24 | 1235 | 6 | 0.71 | 12.5 | 1.00 | 0.53 | 1.21 | 0.21 | -0.29 |
| evening 17-24 | 1232 | 12 | 0.70 | 10.2 | 1.00 | 0.55 | 1.20 | 0.20 | -0.30 |
| weekday | 3410 | 1 | 0.77 | 33.0 | 1.00 | 0.52 | 1.27 | 0.27 | -0.23 |
| weekday | 3399 | 3 | 0.77 | 29.9 | 1.00 | 0.55 | 1.27 | 0.27 | -0.23 |
| weekday | 3386 | 6 | 0.70 | 23.1 | 1.00 | 0.54 | 1.20 | 0.20 | -0.30 |
| weekday | 3345 | 12 | 0.69 | 18.8 | 1.00 | 0.54 | 1.19 | 0.19 | -0.31 |
| weekend | 1089 | 1 | 1.10 | 20.7 | 1.00 | 0.60 | 1.60 | 0.60 | 0.10 |
| weekend | 1089 | 3 | 1.17 | 22.7 | 1.00 | 0.64 | 1.67 | 0.67 | 0.17 |
| weekend | 1089 | 6 | 1.10 | 19.1 | 1.00 | 0.60 | 1.60 | 0.60 | 0.10 |
| weekend | 1089 | 12 | 1.09 | 16.6 | 1.00 | 0.59 | 1.59 | 0.59 | 0.09 |

### vwap, |move| >= 2 sigma (11903 events, mean move 1.92 ticks)

| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |
|---|---|---|---|---|---|---|---|---|---|
| all | 11903 | 1 | 0.61 | 53.0 | 0.47 | 0.61 | 1.11 | 0.11 | -0.39 |
| all | 11865 | 3 | 0.63 | 48.8 | 0.62 | 0.64 | 1.13 | 0.13 | -0.37 |
| all | 11801 | 6 | 0.59 | 38.6 | 0.61 | 0.62 | 1.09 | 0.09 | -0.41 |
| all | 11672 | 12 | 0.57 | 30.0 | 0.63 | 0.60 | 1.07 | 0.07 | -0.43 |
| overnight 00-07 | 3087 | 1 | 0.64 | 33.3 | 0.50 | 0.62 | 1.14 | 0.14 | -0.36 |
| overnight 00-07 | 3086 | 3 | 0.66 | 29.4 | 0.68 | 0.64 | 1.16 | 0.16 | -0.34 |
| overnight 00-07 | 3081 | 6 | 0.63 | 23.4 | 0.62 | 0.63 | 1.13 | 0.13 | -0.37 |
| overnight 00-07 | 3066 | 12 | 0.60 | 18.2 | 0.65 | 0.61 | 1.10 | 0.10 | -0.40 |
| morning 07-12 | 2511 | 1 | 0.56 | 23.2 | 0.47 | 0.62 | 1.06 | 0.06 | -0.44 |
| morning 07-12 | 2510 | 3 | 0.60 | 22.9 | 0.58 | 0.64 | 1.10 | 0.10 | -0.40 |
| morning 07-12 | 2507 | 6 | 0.60 | 19.1 | 0.68 | 0.64 | 1.10 | 0.10 | -0.40 |
| morning 07-12 | 2490 | 12 | 0.58 | 14.6 | 0.68 | 0.61 | 1.08 | 0.08 | -0.42 |
| afternoon 12-17 | 2813 | 1 | 0.64 | 28.4 | 0.50 | 0.62 | 1.14 | 0.14 | -0.36 |
| afternoon 12-17 | 2800 | 3 | 0.65 | 26.6 | 0.66 | 0.65 | 1.15 | 0.15 | -0.35 |
| afternoon 12-17 | 2765 | 6 | 0.61 | 20.5 | 0.56 | 0.62 | 1.11 | 0.11 | -0.39 |
| afternoon 12-17 | 2681 | 12 | 0.59 | 16.1 | 0.67 | 0.61 | 1.09 | 0.09 | -0.41 |
| evening 17-24 | 3492 | 1 | 0.59 | 23.9 | 0.38 | 0.59 | 1.09 | 0.09 | -0.41 |
| evening 17-24 | 3469 | 3 | 0.60 | 21.4 | 0.55 | 0.62 | 1.10 | 0.10 | -0.40 |
| evening 17-24 | 3448 | 6 | 0.55 | 16.4 | 0.55 | 0.60 | 1.05 | 0.05 | -0.45 |
| evening 17-24 | 3435 | 12 | 0.52 | 12.7 | 0.55 | 0.59 | 1.02 | 0.02 | -0.48 |
| weekday | 8991 | 1 | 0.59 | 44.7 | 0.45 | 0.61 | 1.09 | 0.09 | -0.41 |
| weekday | 8953 | 3 | 0.60 | 40.6 | 0.59 | 0.63 | 1.10 | 0.10 | -0.40 |
| weekday | 8889 | 6 | 0.56 | 31.3 | 0.58 | 0.62 | 1.06 | 0.06 | -0.44 |
| weekday | 8760 | 12 | 0.54 | 24.2 | 0.61 | 0.61 | 1.04 | 0.04 | -0.46 |
| weekend | 2912 | 1 | 0.67 | 28.7 | 0.53 | 0.61 | 1.17 | 0.17 | -0.33 |
| weekend | 2912 | 3 | 0.72 | 27.3 | 0.74 | 0.65 | 1.22 | 0.22 | -0.28 |
| weekend | 2912 | 6 | 0.71 | 22.9 | 0.72 | 0.62 | 1.21 | 0.21 | -0.29 |
| weekend | 2912 | 12 | 0.66 | 18.3 | 0.70 | 0.60 | 1.16 | 0.16 | -0.34 |

### vwap, |move| >= 3 sigma (3662 events, mean move 2.41 ticks)

| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |
|---|---|---|---|---|---|---|---|---|---|
| all | 3662 | 1 | 0.71 | 25.5 | 0.58 | 0.62 | 1.21 | 0.21 | -0.29 |
| all | 3650 | 3 | 0.71 | 24.3 | 0.71 | 0.64 | 1.21 | 0.21 | -0.29 |
| all | 3629 | 6 | 0.63 | 18.6 | 0.65 | 0.62 | 1.13 | 0.13 | -0.37 |
| all | 3594 | 12 | 0.59 | 15.2 | 0.62 | 0.60 | 1.09 | 0.09 | -0.41 |
| overnight 00-07 | 876 | 1 | 0.74 | 17.9 | 0.67 | 0.63 | 1.24 | 0.24 | -0.26 |
| overnight 00-07 | 875 | 3 | 0.79 | 16.7 | 0.80 | 0.65 | 1.29 | 0.29 | -0.21 |
| overnight 00-07 | 874 | 6 | 0.70 | 12.4 | 0.72 | 0.64 | 1.20 | 0.20 | -0.30 |
| overnight 00-07 | 869 | 12 | 0.71 | 11.3 | 0.81 | 0.63 | 1.21 | 0.21 | -0.29 |
| morning 07-12 | 876 | 1 | 0.69 | 12.9 | 0.66 | 0.65 | 1.19 | 0.19 | -0.31 |
| morning 07-12 | 876 | 3 | 0.65 | 12.7 | 0.67 | 0.66 | 1.15 | 0.15 | -0.35 |
| morning 07-12 | 874 | 6 | 0.64 | 10.7 | 0.73 | 0.64 | 1.14 | 0.14 | -0.36 |
| morning 07-12 | 873 | 12 | 0.60 | 8.4 | 0.71 | 0.61 | 1.10 | 0.10 | -0.40 |
| afternoon 12-17 | 874 | 1 | 0.70 | 13.4 | 0.50 | 0.61 | 1.20 | 0.20 | -0.30 |
| afternoon 12-17 | 869 | 3 | 0.72 | 12.9 | 0.69 | 0.64 | 1.22 | 0.22 | -0.28 |
| afternoon 12-17 | 856 | 6 | 0.63 | 10.1 | 0.59 | 0.62 | 1.13 | 0.13 | -0.37 |
| afternoon 12-17 | 829 | 12 | 0.58 | 7.9 | 0.55 | 0.59 | 1.08 | 0.08 | -0.42 |
| evening 17-24 | 1036 | 1 | 0.70 | 10.6 | 0.53 | 0.61 | 1.20 | 0.20 | -0.30 |
| evening 17-24 | 1030 | 3 | 0.70 | 9.8 | 0.72 | 0.63 | 1.20 | 0.20 | -0.30 |
| evening 17-24 | 1025 | 6 | 0.57 | 6.9 | 0.55 | 0.60 | 1.07 | 0.07 | -0.43 |
| evening 17-24 | 1023 | 12 | 0.51 | 5.4 | 0.50 | 0.59 | 1.01 | 0.01 | -0.49 |
| weekday | 2794 | 1 | 0.67 | 21.0 | 0.53 | 0.62 | 1.17 | 0.17 | -0.33 |
| weekday | 2782 | 3 | 0.66 | 19.6 | 0.68 | 0.64 | 1.16 | 0.16 | -0.34 |
| weekday | 2761 | 6 | 0.56 | 14.2 | 0.61 | 0.63 | 1.06 | 0.06 | -0.44 |
| weekday | 2726 | 12 | 0.54 | 11.7 | 0.57 | 0.60 | 1.04 | 0.04 | -0.46 |
| weekend | 868 | 1 | 0.82 | 14.9 | 0.76 | 0.62 | 1.32 | 0.32 | -0.18 |
| weekend | 868 | 3 | 0.87 | 14.9 | 0.88 | 0.65 | 1.37 | 0.37 | -0.13 |
| weekend | 868 | 6 | 0.85 | 13.0 | 0.78 | 0.62 | 1.35 | 0.35 | -0.15 |
| weekend | 868 | 12 | 0.75 | 10.8 | 0.76 | 0.62 | 1.25 | 0.25 | -0.25 |

## 4b. Event study: large 1m moves (sigma from the trailing 48 bar changes, bar clock)

R_k = cumulative change over the next k bars signed against the move (positive = reversion), in ticks. Passive fade P&L per share = R_k + half a tick (resting at the post-move close improved by one half tick, exit at the close k bars later); taker/passive = R_k - half a tick (cross the spread to enter, leave passively); taker fade P&L = R_k - one tick (crossing a 0.005 spread to enter, exiting at the close, i.e. paying the spread once in full). The post-move close is itself a print at the far quote, so about half the Roll spread (0.4 ticks) of R_k on close is bounce; the vwap tables are the cleaner measure. Events can overlap (one shock produces several bars), so the t-statistics are optimistic.

### close, |move| >= 2 sigma (3977 events, mean move 1.94 ticks)

| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |
|---|---|---|---|---|---|---|---|---|---|
| all | 3977 | 1 | 0.80 | 38.7 | 1.00 | 0.53 | 1.30 | 0.30 | -0.20 |
| all | 3969 | 3 | 0.78 | 35.1 | 1.00 | 0.56 | 1.28 | 0.28 | -0.22 |
| all | 3952 | 6 | 0.80 | 33.2 | 1.00 | 0.56 | 1.30 | 0.30 | -0.20 |
| all | 3918 | 12 | 0.79 | 29.2 | 1.00 | 0.57 | 1.29 | 0.29 | -0.21 |
| overnight 00-07 | 1137 | 1 | 0.81 | 23.8 | 1.00 | 0.54 | 1.31 | 0.31 | -0.19 |
| overnight 00-07 | 1137 | 3 | 0.75 | 21.3 | 1.00 | 0.55 | 1.25 | 0.25 | -0.25 |
| overnight 00-07 | 1137 | 6 | 0.79 | 21.0 | 1.00 | 0.57 | 1.29 | 0.29 | -0.21 |
| overnight 00-07 | 1134 | 12 | 0.76 | 18.0 | 1.00 | 0.58 | 1.26 | 0.26 | -0.24 |
| morning 07-12 | 770 | 1 | 0.72 | 16.0 | -0.00 | 0.50 | 1.22 | 0.22 | -0.28 |
| morning 07-12 | 770 | 3 | 0.72 | 14.1 | 1.00 | 0.55 | 1.22 | 0.22 | -0.28 |
| morning 07-12 | 769 | 6 | 0.79 | 14.1 | 1.00 | 0.56 | 1.29 | 0.29 | -0.21 |
| morning 07-12 | 766 | 12 | 0.81 | 13.2 | 1.00 | 0.58 | 1.31 | 0.31 | -0.19 |
| afternoon 12-17 | 862 | 1 | 0.82 | 18.0 | 1.00 | 0.53 | 1.32 | 0.32 | -0.18 |
| afternoon 12-17 | 861 | 3 | 0.81 | 16.6 | 1.00 | 0.56 | 1.31 | 0.31 | -0.19 |
| afternoon 12-17 | 860 | 6 | 0.79 | 15.8 | 1.00 | 0.54 | 1.29 | 0.29 | -0.21 |
| afternoon 12-17 | 856 | 12 | 0.79 | 14.2 | 1.00 | 0.57 | 1.29 | 0.29 | -0.21 |
| evening 17-24 | 1208 | 1 | 0.82 | 19.9 | 1.00 | 0.54 | 1.32 | 0.32 | -0.18 |
| evening 17-24 | 1201 | 3 | 0.83 | 18.5 | 1.00 | 0.56 | 1.33 | 0.33 | -0.17 |
| evening 17-24 | 1186 | 6 | 0.83 | 16.5 | 1.00 | 0.57 | 1.33 | 0.33 | -0.17 |
| evening 17-24 | 1162 | 12 | 0.79 | 13.9 | 1.00 | 0.56 | 1.29 | 0.29 | -0.21 |
| weekday | 3157 | 1 | 0.77 | 35.6 | 1.00 | 0.53 | 1.27 | 0.27 | -0.23 |
| weekday | 3149 | 3 | 0.75 | 30.9 | 1.00 | 0.56 | 1.25 | 0.25 | -0.25 |
| weekday | 3132 | 6 | 0.77 | 28.9 | 1.00 | 0.56 | 1.27 | 0.27 | -0.23 |
| weekday | 3098 | 12 | 0.74 | 25.3 | 1.00 | 0.57 | 1.24 | 0.24 | -0.26 |
| weekend | 820 | 1 | 0.92 | 16.5 | 1.00 | 0.52 | 1.42 | 0.42 | -0.08 |
| weekend | 820 | 3 | 0.91 | 16.9 | 1.00 | 0.55 | 1.41 | 0.41 | -0.09 |
| weekend | 820 | 6 | 0.93 | 16.5 | 1.00 | 0.57 | 1.43 | 0.43 | -0.07 |
| weekend | 820 | 12 | 0.96 | 14.6 | 1.00 | 0.58 | 1.46 | 0.46 | -0.04 |

### close, |move| >= 3 sigma (1388 events, mean move 2.39 ticks)

| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |
|---|---|---|---|---|---|---|---|---|---|
| all | 1388 | 1 | 0.98 | 23.1 | 1.00 | 0.57 | 1.48 | 0.48 | -0.02 |
| all | 1386 | 3 | 0.97 | 21.0 | 1.00 | 0.61 | 1.47 | 0.47 | -0.03 |
| all | 1380 | 6 | 0.91 | 18.7 | 1.00 | 0.60 | 1.41 | 0.41 | -0.09 |
| all | 1365 | 12 | 0.86 | 16.4 | 1.00 | 0.59 | 1.36 | 0.36 | -0.14 |
| overnight 00-07 | 391 | 1 | 0.95 | 14.7 | 1.00 | 0.57 | 1.45 | 0.45 | -0.05 |
| overnight 00-07 | 391 | 3 | 0.94 | 13.9 | 1.00 | 0.59 | 1.44 | 0.44 | -0.06 |
| overnight 00-07 | 391 | 6 | 0.86 | 12.0 | 1.00 | 0.57 | 1.36 | 0.36 | -0.14 |
| overnight 00-07 | 391 | 12 | 0.79 | 10.6 | 1.00 | 0.58 | 1.29 | 0.29 | -0.21 |
| morning 07-12 | 295 | 1 | 0.88 | 9.6 | 1.00 | 0.53 | 1.38 | 0.38 | -0.12 |
| morning 07-12 | 295 | 3 | 0.87 | 8.7 | 1.00 | 0.60 | 1.37 | 0.37 | -0.13 |
| morning 07-12 | 295 | 6 | 0.85 | 8.1 | 1.00 | 0.60 | 1.35 | 0.35 | -0.15 |
| morning 07-12 | 293 | 12 | 0.84 | 7.5 | 1.00 | 0.59 | 1.34 | 0.34 | -0.16 |
| afternoon 12-17 | 262 | 1 | 0.98 | 10.8 | 1.00 | 0.59 | 1.48 | 0.48 | -0.02 |
| afternoon 12-17 | 261 | 3 | 0.97 | 8.6 | 1.00 | 0.59 | 1.47 | 0.47 | -0.03 |
| afternoon 12-17 | 260 | 6 | 0.91 | 8.1 | 1.00 | 0.58 | 1.41 | 0.41 | -0.09 |
| afternoon 12-17 | 259 | 12 | 0.93 | 7.7 | 1.00 | 0.60 | 1.43 | 0.43 | -0.07 |
| evening 17-24 | 440 | 1 | 1.07 | 12.0 | 1.00 | 0.58 | 1.57 | 0.57 | 0.07 |
| evening 17-24 | 439 | 3 | 1.06 | 11.5 | 1.00 | 0.63 | 1.56 | 0.56 | 0.06 |
| evening 17-24 | 434 | 6 | 0.99 | 9.9 | 1.00 | 0.62 | 1.49 | 0.49 | -0.01 |
| evening 17-24 | 422 | 12 | 0.88 | 8.0 | 1.00 | 0.59 | 1.38 | 0.38 | -0.12 |
| weekday | 1110 | 1 | 0.92 | 20.6 | 1.00 | 0.56 | 1.42 | 0.42 | -0.08 |
| weekday | 1108 | 3 | 0.89 | 17.7 | 1.00 | 0.59 | 1.39 | 0.39 | -0.11 |
| weekday | 1102 | 6 | 0.85 | 15.8 | 1.00 | 0.60 | 1.35 | 0.35 | -0.15 |
| weekday | 1087 | 12 | 0.78 | 13.6 | 1.00 | 0.59 | 1.28 | 0.28 | -0.22 |
| weekend | 278 | 1 | 1.22 | 10.7 | 1.00 | 0.60 | 1.72 | 0.72 | 0.22 |
| weekend | 278 | 3 | 1.29 | 11.6 | 1.00 | 0.66 | 1.79 | 0.79 | 0.29 |
| weekend | 278 | 6 | 1.13 | 10.3 | 1.00 | 0.59 | 1.63 | 0.63 | 0.13 |
| weekend | 278 | 12 | 1.14 | 9.5 | 1.00 | 0.60 | 1.64 | 0.64 | 0.14 |

### vwap, |move| >= 2 sigma (3542 events, mean move 1.82 ticks)

| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |
|---|---|---|---|---|---|---|---|---|---|
| all | 3542 | 1 | 0.71 | 36.7 | 0.62 | 0.63 | 1.21 | 0.21 | -0.29 |
| all | 3536 | 3 | 0.69 | 32.9 | 0.75 | 0.63 | 1.19 | 0.19 | -0.31 |
| all | 3519 | 6 | 0.68 | 28.5 | 0.83 | 0.64 | 1.18 | 0.18 | -0.32 |
| all | 3492 | 12 | 0.68 | 24.7 | 0.90 | 0.63 | 1.18 | 0.18 | -0.32 |
| overnight 00-07 | 951 | 1 | 0.78 | 21.9 | 0.69 | 0.65 | 1.28 | 0.28 | -0.22 |
| overnight 00-07 | 951 | 3 | 0.72 | 19.5 | 0.85 | 0.64 | 1.22 | 0.22 | -0.28 |
| overnight 00-07 | 951 | 6 | 0.74 | 18.4 | 0.87 | 0.66 | 1.24 | 0.24 | -0.26 |
| overnight 00-07 | 948 | 12 | 0.71 | 15.5 | 1.00 | 0.65 | 1.21 | 0.21 | -0.29 |
| morning 07-12 | 714 | 1 | 0.58 | 14.8 | 0.50 | 0.60 | 1.08 | 0.08 | -0.42 |
| morning 07-12 | 714 | 3 | 0.64 | 13.1 | 0.66 | 0.63 | 1.14 | 0.14 | -0.36 |
| morning 07-12 | 713 | 6 | 0.61 | 10.7 | 0.67 | 0.62 | 1.11 | 0.11 | -0.39 |
| morning 07-12 | 710 | 12 | 0.60 | 9.5 | 0.66 | 0.62 | 1.10 | 0.10 | -0.40 |
| afternoon 12-17 | 815 | 1 | 0.72 | 17.6 | 0.67 | 0.63 | 1.22 | 0.22 | -0.28 |
| afternoon 12-17 | 814 | 3 | 0.68 | 14.9 | 0.74 | 0.63 | 1.18 | 0.18 | -0.32 |
| afternoon 12-17 | 813 | 6 | 0.67 | 13.9 | 0.77 | 0.64 | 1.17 | 0.17 | -0.33 |
| afternoon 12-17 | 810 | 12 | 0.66 | 12.0 | 0.83 | 0.62 | 1.16 | 0.16 | -0.34 |
| evening 17-24 | 1062 | 1 | 0.71 | 19.0 | 0.53 | 0.62 | 1.21 | 0.21 | -0.29 |
| evening 17-24 | 1057 | 3 | 0.69 | 18.3 | 0.78 | 0.63 | 1.19 | 0.19 | -0.31 |
| evening 17-24 | 1042 | 6 | 0.69 | 14.5 | 0.92 | 0.63 | 1.19 | 0.19 | -0.31 |
| evening 17-24 | 1024 | 12 | 0.71 | 12.7 | 0.98 | 0.63 | 1.21 | 0.21 | -0.29 |
| weekday | 2776 | 1 | 0.69 | 34.7 | 0.62 | 0.63 | 1.19 | 0.19 | -0.31 |
| weekday | 2770 | 3 | 0.67 | 30.9 | 0.75 | 0.64 | 1.17 | 0.17 | -0.33 |
| weekday | 2753 | 6 | 0.66 | 25.7 | 0.81 | 0.64 | 1.16 | 0.16 | -0.34 |
| weekday | 2726 | 12 | 0.65 | 22.1 | 0.88 | 0.63 | 1.15 | 0.15 | -0.35 |
| weekend | 766 | 1 | 0.77 | 14.7 | 0.62 | 0.60 | 1.27 | 0.27 | -0.23 |
| weekend | 766 | 3 | 0.74 | 13.3 | 0.76 | 0.60 | 1.24 | 0.24 | -0.26 |
| weekend | 766 | 6 | 0.76 | 12.8 | 0.90 | 0.63 | 1.26 | 0.26 | -0.24 |
| weekend | 766 | 12 | 0.76 | 11.2 | 0.98 | 0.63 | 1.26 | 0.26 | -0.24 |

### vwap, |move| >= 3 sigma (1163 events, mean move 2.26 ticks)

| bucket | n | k | mean R_k (ticks) | t | median | hit rate | passive P&L (ticks) | taker in / passive out | taker P&L (ticks) |
|---|---|---|---|---|---|---|---|---|---|
| all | 1163 | 1 | 0.78 | 18.4 | 0.60 | 0.64 | 1.28 | 0.28 | -0.22 |
| all | 1163 | 3 | 0.79 | 18.1 | 0.85 | 0.66 | 1.29 | 0.29 | -0.21 |
| all | 1158 | 6 | 0.75 | 15.5 | 0.88 | 0.65 | 1.25 | 0.25 | -0.25 |
| all | 1148 | 12 | 0.70 | 13.3 | 0.87 | 0.64 | 1.20 | 0.20 | -0.30 |
| overnight 00-07 | 341 | 1 | 0.88 | 11.8 | 0.63 | 0.65 | 1.38 | 0.38 | -0.12 |
| overnight 00-07 | 341 | 3 | 0.84 | 12.1 | 0.90 | 0.66 | 1.34 | 0.34 | -0.16 |
| overnight 00-07 | 341 | 6 | 0.79 | 10.5 | 0.78 | 0.66 | 1.29 | 0.29 | -0.21 |
| overnight 00-07 | 341 | 12 | 0.72 | 9.0 | 1.00 | 0.66 | 1.22 | 0.22 | -0.28 |
| morning 07-12 | 239 | 1 | 0.58 | 7.6 | 0.49 | 0.59 | 1.08 | 0.08 | -0.42 |
| morning 07-12 | 239 | 3 | 0.67 | 7.1 | 0.65 | 0.65 | 1.17 | 0.17 | -0.33 |
| morning 07-12 | 239 | 6 | 0.60 | 5.7 | 0.75 | 0.62 | 1.10 | 0.10 | -0.40 |
| morning 07-12 | 237 | 12 | 0.51 | 4.6 | 0.47 | 0.61 | 1.01 | 0.01 | -0.49 |
| afternoon 12-17 | 232 | 1 | 0.88 | 8.1 | 0.79 | 0.69 | 1.38 | 0.38 | -0.12 |
| afternoon 12-17 | 232 | 3 | 0.83 | 6.7 | 0.85 | 0.68 | 1.33 | 0.33 | -0.17 |
| afternoon 12-17 | 231 | 6 | 0.77 | 6.2 | 0.80 | 0.69 | 1.27 | 0.27 | -0.23 |
| afternoon 12-17 | 230 | 12 | 0.78 | 6.1 | 0.88 | 0.65 | 1.28 | 0.28 | -0.22 |
| evening 17-24 | 351 | 1 | 0.74 | 9.2 | 0.51 | 0.64 | 1.24 | 0.24 | -0.26 |
| evening 17-24 | 351 | 3 | 0.80 | 10.8 | 0.88 | 0.64 | 1.30 | 0.30 | -0.20 |
| evening 17-24 | 347 | 6 | 0.81 | 8.7 | 1.00 | 0.65 | 1.31 | 0.31 | -0.19 |
| evening 17-24 | 340 | 12 | 0.76 | 7.0 | 0.95 | 0.64 | 1.26 | 0.26 | -0.24 |
| weekday | 910 | 1 | 0.73 | 17.7 | 0.58 | 0.65 | 1.23 | 0.23 | -0.27 |
| weekday | 910 | 3 | 0.73 | 17.1 | 0.84 | 0.66 | 1.23 | 0.23 | -0.27 |
| weekday | 905 | 6 | 0.72 | 14.0 | 0.88 | 0.66 | 1.22 | 0.22 | -0.28 |
| weekday | 895 | 12 | 0.65 | 11.5 | 0.81 | 0.64 | 1.15 | 0.15 | -0.35 |
| weekend | 253 | 1 | 0.94 | 7.5 | 0.83 | 0.62 | 1.44 | 0.44 | -0.06 |
| weekend | 253 | 3 | 0.99 | 7.8 | 0.98 | 0.65 | 1.49 | 0.49 | -0.01 |
| weekend | 253 | 6 | 0.88 | 6.9 | 0.86 | 0.64 | 1.38 | 0.38 | -0.12 |
| weekend | 253 | 12 | 0.88 | 6.7 | 1.00 | 0.64 | 1.38 | 0.38 | -0.12 |

## 5. Sweeps on the tape

A sweep is a run of consecutive trades, each within 60 s of the previous one, whose prices never move against the run and which carries the price by at least 2 ticks from the first to the last trade. The post-sweep price at +5, +15 and +60 min is the last known price at or before that time, from the tape where it extends that far and otherwise from 5-minute candle closes (stamped at bar end). Reversion is signed against the sweep (positive = price came back), in ticks. The sweep's last print is at the quote the sweep was hitting and the later print is on average near the mid, so about half the Roll spread (~0.4 ticks, section 3) of the measured reversion is bounce; a passive fader who posts at the sweep's end price earns the measured reversion, a taker crossing the spread earns it minus one tick.

995 sweeps in 237 markets; mean size 2.35 ticks, mean 5.6 trades, median shares 1156.

| bucket | n | +5 min (ticks) | t | +15 min | t | +60 min | t | median +15 | hit +15 |
|---|---|---|---|---|---|---|---|---|---|
| all | 995 | 0.95 | 23.3 | 1.00 | 22.5 | 1.01 | 17.9 | 1.00 | 0.57 |
| overnight 00-07 | 296 | 0.77 | 10.6 | 0.77 | 9.6 | 0.89 | 8.0 | -0.00 | 0.45 |
| morning 07-12 | 198 | 0.95 | 12.3 | 1.00 | 12.4 | 1.02 | 12.0 | 1.00 | 0.60 |
| afternoon 12-17 | 321 | 1.01 | 14.8 | 1.02 | 13.7 | 1.13 | 12.5 | 1.00 | 0.61 |
| evening 17-24 | 180 | 1.13 | 9.4 | 1.36 | 10.3 | 1.01 | 4.9 | 1.00 | 0.66 |
| weekday | 974 | 0.94 | 22.9 | 0.99 | 22.0 | 1.01 | 17.5 | 1.00 | 0.57 |
| weekend | 21 | 1.14 | 4.0 | 1.43 | 4.7 | 1.29 | 4.6 | 2.00 | 0.62 |

By ET hour (reversion at +15 min):

| ET hour | n | mean sweep (ticks) | +15 min reversion (ticks) | t | hit |
|---|---|---|---|---|---|
| 00 | 21 | 2.33 | 0.71 | 2.0 | 0.48 |
| 01 | 29 | 2.90 | 0.79 | 3.7 | 0.45 |
| 02 | 15 | 2.20 | 1.13 | 3.1 | 0.60 |
| 03 | 29 | 2.03 | 0.38 | 2.0 | 0.28 |
| 04 | 51 | 2.25 | 0.59 | 3.3 | 0.33 |
| 05 | 59 | 2.14 | 0.51 | 3.4 | 0.37 |
| 06 | 92 | 2.25 | 1.10 | 6.5 | 0.59 |
| 07 | 104 | 2.13 | 0.90 | 8.4 | 0.54 |
| 08 | 17 | 2.18 | 1.35 | 5.6 | 0.76 |
| 09 | 20 | 2.40 | 1.05 | 3.4 | 0.70 |
| 10 | 22 | 2.41 | 1.23 | 4.0 | 0.73 |
| 11 | 34 | 2.18 | 0.94 | 6.2 | 0.59 |
| 12 | 62 | 2.47 | 1.11 | 5.5 | 0.61 |
| 13 | 41 | 2.22 | 1.22 | 7.9 | 0.73 |
| 14 | 64 | 2.27 | 0.75 | 5.1 | 0.58 |
| 15 | 56 | 2.30 | 0.84 | 4.9 | 0.54 |
| 16 | 98 | 2.68 | 1.15 | 8.4 | 0.63 |
| 17 | 79 | 2.44 | 1.39 | 8.4 | 0.70 |
| 18 | 11 | 2.55 | 0.73 | 1.6 | 0.36 |
| 19 | 18 | 2.83 | 2.22 | 5.9 | 0.83 |
| 20 | 7 | 2.14 | 1.14 | 3.4 | 0.71 |
| 21 | 18 | 2.89 | 1.89 | 3.7 | 0.78 |
| 22 | 14 | 2.21 | 0.71 | 1.2 | 0.50 |
| 23 | 19 | 2.16 | 0.79 | 2.0 | 0.53 |

## 6. Opportunities

**Horizon.** Reversion is present at every horizon from one minute to four hours and is strongest in the bar clock at 1-5 minutes (winsorized AR(1): 1m -0.416, 5m -0.363, 15m -0.264, 30m -0.171, 1h -0.232, 4h -0.087); on the calendar clock the slope is about -0.2 from 1 minute to 30 minutes and -0.17 at 1 hour, with variance ratios falling monotonically with k (VR(16) of 0.24 at 5 minutes, 0.55 at 1 hour), so the reversion is not a one-bar artefact: a good part of any 5-minute move is given back over the next hour or two.

**Fading large 5-minute moves (section 4), what survives the spread:**

| sample | n | R_3 close (ticks) | passive | taker in / passive out | taker round trip | R_3 vwap | verdict |
|---|---|---|---|---|---|---|---|
| 2 sigma, all | 13964 | 0.79 (t 66.9) | 1.29 | 0.29 | -0.21 | 0.63 | pays only if one side is passive |
| 2 sigma, overnight 00-07 | 3645 | 0.84 (t 36.3) | 1.34 | 0.34 | -0.16 | 0.66 | pays only if one side is passive |
| 2 sigma, morning 07-12 | 3056 | 0.75 (t 31.6) | 1.25 | 0.25 | -0.25 | 0.60 | pays only if one side is passive |
| 2 sigma, afternoon 12-17 | 3244 | 0.80 (t 33.4) | 1.30 | 0.30 | -0.20 | 0.65 | pays only if one side is passive |
| 2 sigma, evening 17-24 | 4019 | 0.75 (t 32.9) | 1.25 | 0.25 | -0.25 | 0.60 | pays only if one side is passive |
| 2 sigma, weekday | 10601 | 0.75 (t 57.5) | 1.25 | 0.25 | -0.25 | 0.60 | pays only if one side is passive |
| 2 sigma, weekend | 3363 | 0.90 (t 34.5) | 1.40 | 0.40 | -0.10 | 0.72 | pays only if one side is passive |
| 3 sigma, all | 4488 | 0.87 (t 37.2) | 1.37 | 0.37 | -0.13 | 0.71 | pays only if one side is passive |
| 3 sigma, overnight 00-07 | 1208 | 1.00 (t 22.0) | 1.50 | 0.50 | -0.00 | 0.79 | pays only if one side is passive |
| 3 sigma, morning 07-12 | 1010 | 0.78 (t 17.1) | 1.28 | 0.28 | -0.22 | 0.65 | pays only if one side is passive |
| 3 sigma, afternoon 12-17 | 1030 | 0.86 (t 18.7) | 1.36 | 0.36 | -0.14 | 0.72 | pays only if one side is passive |
| 3 sigma, evening 17-24 | 1240 | 0.82 (t 16.9) | 1.32 | 0.32 | -0.18 | 0.70 | pays only if one side is passive |
| 3 sigma, weekday | 3399 | 0.77 (t 29.9) | 1.27 | 0.27 | -0.23 | 0.66 | pays only if one side is passive |
| 3 sigma, weekend | 1089 | 1.17 (t 22.7) | 1.67 | 0.67 | 0.17 | 0.87 | taker round trip positive |

Fading a 2-3 sigma 5-minute move as a round-trip taker loses in every bucket (about -0.1 to -0.3 ticks per share); crossing once and leaving passively nets +0.3 to +0.7 ticks, and that is the bot's earlier "+0.33 cents" number (it charged the spread once). The clean gain is passive: a resting quote filled by the move earns 1.2-1.7 ticks per share gross on these events, of which roughly 0.4 ticks is the bounce of the print back to the mid and 0.6-1.0 ticks is reversion of the quote itself (vwap column). Fill probability is not modelled: a resting order is filled only when the move continues through it, so this is an upper bound on the ladder's edge per fill, not a strategy return.

**Hours.** The AR(1) slopes barely differ between the four ET buckets (winsorized Wald chi-square on 3 df: 1m 8.0, 5m 2.9, 15m 1.9, 30m 0.9, 1h 1.3; 5% critical value 7.8; only 1m crosses it, and there the spread between buckets is small (overnight -0.180, morning -0.151, afternoon -0.192, evening -0.187); see section 2), and the event-study reversion is 0.7-1.0 ticks in every bucket. The differences that do show up are in the sweeps (section 5):

- all: n 980, +15 min reversion 1.00 ticks (t 22.5, median 1.0, hit 0.57); passive at the sweep's end price 1.00, taker -0.00.
- overnight 00-07: n 296, +15 min reversion 0.77 ticks (t 9.6, median -0.0, hit 0.45); passive at the sweep's end price 0.77, taker -0.23.
- morning 07-12: n 197, +15 min reversion 1.00 ticks (t 12.4, median 1.0, hit 0.60); passive at the sweep's end price 1.00, taker 0.00.
- afternoon 12-17: n 321, +15 min reversion 1.02 ticks (t 13.7, median 1.0, hit 0.61); passive at the sweep's end price 1.02, taker 0.02.
- evening 17-24: n 166, +15 min reversion 1.36 ticks (t 10.3, median 1.0, hit 0.66); passive at the sweep's end price 1.36, taker 0.36.
- weekday: n 959, +15 min reversion 0.99 ticks (t 22.0, median 1.0, hit 0.57); passive at the sweep's end price 0.99, taker -0.01.
- weekend: n 21, +15 min reversion 1.43 ticks (t 4.7, median 2.0, hit 0.62); passive at the sweep's end price 1.43, taker 0.43.

Overnight (00-07 ET) sweeps revert least (median zero, hit rate below one half, weakest at 03-05 ET); evening (17-24 ET) and the 19-21 ET hours revert most, 1.4-2 ticks, but on a few dozen sweeps. Even the evening figure only just survives a round-trip taker fade (about +0.4 ticks before any adverse selection), so sweeps are a signal for where to rest quotes and when to pull the adding side, not a taker trade. Weekend events revert somewhat more than weekday ones (3-sigma 5-minute moves: 1.17 vs 0.77 ticks at k = 3), consistent with thinner books and the DMM ladder being the only liquidity.

**Does not survive:** any taker fade of a single-bar move at any hour at 2 sigma; taker fades of 3-sigma moves on weekdays; taker fades of overnight sweeps. **Survives:** passive fills on the far side of 2-3 sigma moves and sweeps at any hour (1.2-1.5 ticks gross per share before fill probability), cross-once-leave-passive on 3-sigma moves (+0.4 ticks), and evening sweeps if the fill is near the sweep's end price.

**Caveats.**

- Seven to twenty days of 5-minute candles per market and 1,000 one-minute bars (one to twenty days, depending on activity) on the 60 most traded markets; every estimate is in-sample on one campaign stretch, and the election-week regime will differ.
- Event windows overlap (one shock creates several qualifying bars and several sweeps), so the event-study and sweep t-statistics overstate independence; the HAC regressions handle it, the event tables do not.
- Changes have a kurtosis in the thousands: a handful of fat-finger prints that reverse (5-minute changes up to 0.8) contribute almost half of the raw covariance, which is why raw HAC t-statistics are around 5-7 despite 200k-1M observations; the winsorized columns describe the typical bar.
- Candles exist only for intervals that traded; the bar clock stretches overnight (a 5-minute bar can span an hour), and the overnight bucket has fewer traded bars and sweeps, so its estimates are noisier and partly measure the morning's first trades.
- The post-move close and the sweep's last print sit at the far quote; about 0.4 ticks (half the Roll spread) of the measured reversion on close is bounce, and the collapse of same-price bars strengthens rather than removes that effect.
- Passive P&L assumes a fill at the post-move price and an exit at a later print; it ignores queue position, the chance of being filled only when the move continues (adverse selection), and the 15-minute order expiry.
