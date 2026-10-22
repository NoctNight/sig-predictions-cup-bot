# Election night: the sleeve and the late punt (2026-10-22, end-game side session)

Code: `sigbot/proposals/election_night.py` (sleeve, poller, recorder, replay, `--simulate`) and `sigbot/proposals/punt_screener.py`.
Both are paper or read-only, and both have a `--selftest`. The measurement script is `research_notes/election_night_study.py`.

Data (`data/election_night/`): `paths_2024.csv` (1-minute Polymarket and Kalshi prices, 242 + 212 legs), `paths_2022.csv`
(Polymarket AMM trades from Polygon logs, 64 legs), `race_calls.csv` (231 AP, Fox, DDHQ and NBC call times),
`poll_close_2026.json` and `results_feeds.md`.

No SIG reads were made and no orders were placed. Fable checked the work independently (section 6).
## 1. What outside prices did after polls closed (2024, the winner's price, mean of the two venues)

| winner's price at the state's last poll close | races | at close | +30 min | +60 | +120 | +240 | reached 0.95 | median minutes to 0.95 |
|---|---|---|---|---|---|---|---|---|
| >= 0.95 (safe seat) | 61 | 0.974 | 0.971 | 0.972 | 0.974 | 0.974 | 61 | 0 |
| 0.80-0.95 | 24 | 0.905 | 0.912 | 0.908 | 0.911 | 0.911 | 23 | 95 |
| 0.50-0.80 | 31 | 0.665 | 0.681 | 0.686 | 0.700 | 0.772 | 23 | 463 |
| < 0.50 (eventual upset) | 10 | 0.350 | 0.360 | 0.370 | 0.373 | 0.534 | 6 | 436 |

Competitive races did not resolve in minutes. Prices drifted for hours as the count came in. Michigan Senate is the warning
case: around 03:00 ET Polymarket had the eventual winner (Slotkin) at 0.07 and Kalshi had her at 0.18, and she won.

## 2. How far outside venues led the calls

Lead is the call time minus the first minute at which every venue quoting the race had the winner at 0.95 or more.

| sample | n | median lead | p25 / p75 | markets first |
|---|---|---|---|---|
| 2024, AP, races below 0.95 at close | 39 | **+75 min** | -25 / +266 | 24 of 39 |
| 2024, first network or desk (Fox, DDHQ, NBC, ...) | 39 | +48 min | -67 / +258 | 22 of 39 |
| 2024, AP, races at 0.95 or more at close | 60 | +1 min | | called at the close |
| 2022, Polymarket AMM vs the first call (NBC or AP), thin data | 9 | +25 min | +1 / +82 | 7 of 9 |

* **Read the lead as about an hour, typically 45-80 min.** The median moves with the price cleaning (Fable's own parse gave
  +65 min). Among calls made within 24 hours the median is +46 min; the p75 comes from AZ, NV, ME and PA Senate, called days
  later.
* **The House is not measured.** The call file has no House calls, and the House is where thin books and false calls sit.
* **Neither venue leads.** Polymarket crossed first 19 times and Kalshi 17 (n 38), so the sleeve polls both and requires both.

**False calls by the price rule, 126 races after poll close.** The loser reached 0.95 on every venue once (CO-08, Kalshi
only, a thin book). It reached 0.90 on every venue 3 times (CA-27, MI Senate, CO-08), and 0.95 on any one venue 3 times.

The rule is both venues at 0.95, or 0.97 when only one venue quotes. It would have avoided every 2024 wrong call, but only
after stale Polymarket prints are dropped; without that, PA-08 is a false call too. The live rule therefore also requires two
consecutive polls (`call_polls 2`) and the poller's spread and depth filter.

## 3. This venue's lag, and the expected edge

* **Measured lag.** SIG's DMM follows a 1.5+ point outside move by 0% at 1-2 min, 9% at 3, 29% at 10, 44% at 30 and 53% at
  60 min (bot_exploits.md, 43 events). It refills a swept level in 3.7 s.
* **Today's compression.** SIG favourites sit 5-12 points under the outside blend (RI Senate D 0.86 vs 0.988).
* **Our side.** A full outside poll (260 Polymarket books plus 256 Kalshi quotes, measured today) takes p50 228 ms and
  p90 684 ms, so entry is bounded by the 4 s cadence.

**How the simulation works.** `--simulate` replays 2024 (127 races, 1-minute steps) against a synthetic SIG:

* Its mid follows `0.5 + kappa (fair - 0.5)` through `F(t) = a(1 - e^-(t-d)/T) + (1-a)(1 - e^-t/T2)`.
* `base` (a 0.45, d 2 min, T 10 min, kappa 0.8) fits the measured curve.
* The ladder is 2k, 2k, 3k, 5k and 5k shares per tick, then 10k.
* The sleeve funds the best expected return per SUSQie first, gives safe-seat calls at most 30% of the total, and sells a
  called hold once SIG's bid is within 1 point of the fair.

| scenario (DMM on election night) | 5k per race, 90k total | 10k / 90k | 10k / 90k, no move trades | 10k / 45k |
|---|---|---|---|---|
| base: today's lag and compression | **+21.3k** | +18.7k | +14.2k | +10.8k |
| kappa1: today's lag, no compression | +18.3k | +13.5k | +9.7k | +9.0k |
| fast_k1: 90% followed within 1-2 min, no compression (recycles cash) | +28.0k | +11.1k | +0.9k | +7.0k |
| pull: quotes pulled 10 min on a 3-point move | +10.2k | +9.6k | | |
| instant: no lag, no compression (sanity check) | 0 | 0 | | |

The 5k column uses the final rules; the others come from an earlier one-poll grid (under 0.4k different at 5k).

* **Cash binds, not the per-race cap.** With unlimited cash, base earns +189k. With 90k it earns about +20k, because called
  holds lock cash until settlement. A 5k per-race cap spreads the cash over more races.
* **Where the P&L comes from (base, 5k).** Races called after the close earned +15.8k (75%), safe seats +4.7k and uncalled
  races +0.7k; by entry kind, calls 78% and moves 22%.
* **What drives it.** Kappa drives only the safe-seat slice. The called-later bulk assumes the DMM still quotes the
  pre-result price 2-10 min after a 20-50 point outside move, extrapolated from 1.5-point pre-election moves.
* **Deadline haircut.** The simulation pays every hold at the true winner, but 18% of base P&L (+4.0k) is in races AP called
  after the noon analogue (MI president at 12:54 ET, ME president, WI and PA Senate).
* **Expected edge: about +17k central** (base after the haircut), ranging from +10k (`pull`) to +28k (`fast_k1`). That is
  10-30% on 90k. The worst simulated race was -1.9k, and no wrong call was held to settlement.

## 4. Caps I would ask Sizhe for (18:00 ET 3 Nov to 12:00 ET 4 Nov only)

| cap | ask | reason |
|---|---|---|
| per race | **5,000 notional** | best in every scenario |
| total | **90,000 notional**, or the cash left after the punt | cash binds; 45k earns about half |
| safe-seat calls (within 15 min of the close) | 30% of the total | keeps cash for competitive calls, which earn 15-40% each |
| partisan cap | exempt `purpose='election'` | calls are not a partisan view |
| entries | calls after the last poll close, moves after the first; no moves in AK, CA, ME, AZ, NV, OR, WA or UT | slow counts |

**Worst case.**

* A plausible bad night is 2 wrong calls held to settlement (-10k) plus move losses (-3k): about -13k.
* If the DMM does not lag, nothing fills and the night is 0.
* The hard maximum is the total cap (-90k), which needs every called race to reverse. In 2024, under the both-venue rule,
  0 of 94 did.
* **Settlement risk.** A race not AP-called by 4 Nov 17:00 UTC may settle late or by an unread rule. In 2024, 28 of 37
  later calls came by noon the next day.

## 5. The late punt (`punt_screener.py --report --cash C --target T`)

**The target.** C is 102k and T is 954.6k: 100k plus the third-place P&L on the 22 Oct snapshot. That needs 9.4x.

**The yardstick.** At fair prices no strategy beats P = C/T = 0.107, because wealth is a martingale. A single simultaneous
bet needs an average fill of 0.107 or less.

**Probability views.** Each option shows three: our fair, the outside blend, and calibrated (the outside blend with the
favourite-longshot correction, MAP a 0.14, b 1.37). Calibrated is the honest one. Each view uses 40k correlated draws, and
prices are SIG's 21 Oct books walked level by level.

| option | stake | P fair | P outside | P calibrated | expected loss |
|---|---|---|---|---|---|
| back Alabama Governor D (ask 0.06) | 97.6k | 0.062 | 0.057 | **0.018** | 80.5k |
| back Arkansas Senate D (0.055) | 96.9k | 0.025 | 0.053 | 0.016 | 81.4k |
| back South Dakota Senate D (0.065) | 100.2k | 0.052 | 0.052 | 0.016 | 85.3k |
| roll VT Governor D (19:00) -> MN-01 D (21:00); sell at 0.9, buy B after half the repricing | 102k | 0.148 | 0.119 | **0.066** | 36.9k |
| the same roll selling at 0.7 (best pair: VT Gov D -> FL-16 D) | 102k | 0.086 | 0.074 | 0.031 | 71.5k |
| 2 of 2: WA-03 R + South Dakota Governor D | 98.0k | 0.004 | 0.003 | 0.0003 | 74.5k |

* **Combinations do not help.** Simultaneous bets still need an average price of C/T or less, and requiring several wins
  only multiplies small probabilities.
* **The correlated, sequential roll beats a single bet only if the timing works.** It exploits P(B | A) > P(B) and SIG's lag.
  But the first upset must be called and sold within about 2 hours, and in 2024 upsets reached 0.95 a median 435 min after
  close. It also needs 300-900k to trade within one race's book. And its edge on B is the model's conditional, not an
  observed repricing.
* **The honest number is about 2% for a single bet (calibrated), and at most 3-7% for a roll that executes perfectly.** The
  expected loss is 37-85k of 102k, and the punt competes with the sleeve for the same cash.

## 6. Independent check (Fable)

* **No look-ahead.** Fable checked `_at`, the candle timestamps, the stale filter, `F(0) = 0`, the fills, settlement of NO
  on the other leg, and the cap accounting.
* **Results.** It reproduced every simulation figure (its own call-only replay: +17.8k vs +17.3k) and confirmed the lead
  at +65 to +78 min. Timezones are right on every row; NYT's accepted-at stamp adds a median of 1 min.
* **Fixed after the check:** the study's district keys and the two-poll call rule. **Caveats it added:** the deadline
  haircut, no House calls, the extrapolated lag kernel, the assumed depth and refill, and the roll's timing.

## Sources

* Prices: Polymarket CLOB `prices-history` (fidelity 1); Kalshi `historical/markets/{t}/candlesticks` (period 1); Polygon
  FPMM logs for 2022 (`research_notes/election_night_fetch*.py`).
* Calls: the NYT 2024 results JSON (`static01.nyt.com/elections-assets/pages/data/2024-11-05/results-*.json`), AP race-call
  articles, Fox call tables and the NBC 2022 live blog. Every row carries its URL in `race_calls.csv`.
* Poll closes: The Green Papers 2026, NYT 2024 `pollClosingTimes` and NPR 2024.
* Feeds: `results_feeds.md`. The NYT JSON was keyless in 2024; its 2026 path returns 404 today. AP and DDHQ need keys.
* Repo notes: bot_exploits.md (DMM lag), favourite_drift.md (calibration), correlation_arbs.md (factor model).
