# Independent review: the line trade and the right-side filter (31 Oct)

Read-only. Scripts are `lr_common.py` and `lr_01`-`lr_12` in this folder. `lr_05_perm.py` needs any argument to rerun the permutations. I placed no orders and did not touch the API or bot files. The trade lists are in `lr_sets.pkl` here.

## Verdicts

- **Claim A (line trade): real, smaller than reported, and in late October largely the same money as the lag trade.** The +1.33c on 30 trades is not the stated rule. Best estimate at the touch +0.5-0.7c per leg, about +0.2-0.4c at 5,000 shares. The part that adds to the lag trade is about +0.1-0.6c at the touch.
- **Claim B (right-side filter): not proven as a filter.** Right-side trades do make money. The claim that they earn "nearly all" of the lag edge fails: within a day the premium is +0.51c, se 0.40c, t 1.3. Use it as a small tilt.
- **Most damaging:** after 25 Oct the true-book profit is about zero, and in 21-30 Oct line trades with no lag signal within 72 h either side made +0.12c.

## 1. Reproduction and the rule mismatch

I reproduced `oos8.py` exactly: A 105 trades +0.77c, B 30 trades +1.33c.

**The reported numbers are not the stated rule.** `oos8.py` masks at 2c, takes the first bar past 2c, starts the 72 h lock there, and only afterwards cuts to 3c+. A leg that reaches 2c first and then 3c gets no 3c trade. So the "3c+" table holds only legs that were already 3c+ on their first usable bar. Running the rule as stated:

| set | trades | net | t race | t day | two-way t |
|---|---|---|---|---|---|
| A reported set (last trades) | 105 | +0.77c | 3.9 | 2.0 | 2.7 |
| A stated rule (mask 3c) | 280 | +0.65c | 4.3 | 2.9 | 2.8 |
| B reported set (book, signal on last trade) | 30 | +1.33c | 2.3 | 0.7 | 2.1 |
| B stated rule (book, signal on last trade) | 116 | +0.70c | 2.3 | 2.0 | 1.9 |
| B clean: signal on book mid, fresh poll, next poll entry, touch both ends | 144 | +1.08c | 2.3 | 1.2 | 2.4 |

B clean is the closest to what the live code would do. It uses no prints, so it has no print selection. I treat A stated and B clean as the fair versions. Two-way t uses the larger of race, day and two-way variance.

## 2. Look-ahead and leakage: none found

- Truncation test: residuals rebuilt on data cut at 40% and 70% of each panel match the full-sample residuals exactly (max difference 0, no NaN mismatch).
- Code reading: k is trailing and shifted one bin. Entry is the next real print strictly after the signal bin. Forward fills run forward only. The `strict_*` trade indices line up with the panel (sign agrees with S48 on 100%).
- Fills in late October: quotes at entry and exit are nearly all fresh. Stated rule: 3% not fresh at either end, worst 60 min at entry and 100 min at exit. Fresh both ends is +0.80c (110) against +0.70c (116), so filled-forward quotes hide nothing.
- Exits dropped for having no print in the 6 h window: A 15 at +1.05c, B 9 at -0.53c. For B that takes the stated rule from about +0.70c to about +0.61c. Small.
- Fill delay, B clean: 5 min +1.08c, 15 min +1.07c, 30 min +1.05c, 60 min +0.98c, 120 min +0.69c. No rush is needed.

## 3. Last-trade bias: it dilutes the result, it does not create it

- A, stated: mean distance 3.63c at the signal print, 3.19c on trailing 1 h VWAP, 3.11c at the entry print. So about 0.45c of the distance is print noise, and 44% of trades (124 of 278) are under 3c by the time you trade.
- But those trades lose little and the rest earn more. A stated: still 3c+ at the entry print +1.01c (154), under 3c +0.18c (124). A reported set: +0.97c (79) and +0.17c (26).
- A with SIG price replaced by 1 h VWAP in the signal: +0.85c (253), t day 3.6. VWAP-to-VWAP over the hour after entry and exit: +0.67c, against +0.65c last-to-last. No sign of false reversion.
- B, the same result on real quotes. Stated rule: mid distance 3c+ at signal +1.36c (67), under 3c -0.20c (49). So a last-print signal is wrong 42% of the time, and those trades earn nothing. Signal on the mid.
- Calibration of the method: on the same B trades, last-trade net is +0.51c and true book +0.70c (reported set +1.05c and +1.33c). True touch round trip on these legs was 0.60c against the assumed 0.75c. So the A numbers are, if anything, slightly low, provided early October spreads were like late October, which I could not check.

## 4. Is the outside the stale side? No

| set | start distance | closes | by SIG moving | by outside moving to SIG |
|---|---|---|---|---|
| A stated | 3.11c | 1.61c | 1.40c | 0.25c |
| A reported | 3.63c | 1.76c | 1.52c | 0.31c |
| B clean | 3.37c | 1.82c | 1.71c | 0.11c |
| B stated | 3.21c | 1.75c | 1.26c | 0.49c |
| B reported | 3.81c | 1.87c | 1.80c | 0.07c |

About 72-96% of the closure is SIG moving. On average the outside does not move our way (-0.02c to -0.47c), so this is not lag or drift. About half the distance is still open at 72 h.

Where it does go wrong: if the outside moves 1c+ against us over the hold, A stated is -0.62c (58) and B clean -1.29c (51).

Both venues fresh (under 1 h) and agreeing within 1.5c: A stated +0.66c (28), A reported +1.36c (9), B stated +0.20c (23), B reported +0.94c (8), B clean -0.29c (26). Too few trades to decide, and B clean fails it. In all five sets the trades earn more when venues differ by over 1c than when they agree within 1c: +0.78c/+0.50c, +1.18c/+0.33c, +0.79c/+0.56c, +1.82c/+0.91c, +1.56c/+0.38c. So "clean, both venues agree" does not raise the edge, which is odd, and I have no good explanation for it. Kalshi hourly against minute data shows the same (+0.74c hourly, +0.44c minute, A stated), but that is confounded with date.

## 5. Robustness

| setting | A n | A net | A t2 | B last n | B last net | B clean n | B clean net | t2 |
|---|---|---|---|---|---|---|---|---|
| 2.5c | 360 | 0.43 | 2.1 | 151 | 0.55 | 190 | 1.26 | 2.9 |
| 3.0c | 280 | 0.65 | 2.8 | 116 | 0.70 | 144 | 1.08 | 2.4 |
| 3.5c | 207 | 0.88 | 3.5 | 79 | 0.61 | 94 | 1.40 | 3.4 |
| 4.0c | 153 | 1.29 | 3.7 | 47 | 0.62 | 60 | 1.20 | 1.9 |
| hold 48 h | 309 | 0.54 | 2.4 | 151 | 0.58 | 167 | 0.85 | 2.6 |
| hold 87 h | 260 | 0.86 | 3.2 | 111 | 0.70 | 131 | 0.98 | 2.0 |
| price 15-85c | 223 | 0.92 | 3.2 | 86 | 0.62 | 107 | 1.20 | 2.2 |
| price 25-75c | 114 | 0.76 | 2.6 | 54 | 0.41 | 69 | 1.28 | 1.7 |
| k median | 282 | 0.76 | 3.1 | 114 | 0.92 | 144 | 1.32 | 3.3 |
| k weighted | 283 | 0.72 | 2.9 | 116 | 0.85 | 145 | 1.32 | 2.7 |
| k by office | 285 | 0.67 | 2.1 | 124 | 0.49 | 140 | 1.11 | 2.3 |

All positive. No setting breaks it, and there is no cliff at 3c. So the 3c line itself is not the problem. Size scales with distance in A, not clearly in B. Later exits are not worse: A keeps rising to 87 h, so "flat from 48 h" holds only in B.

| | net | one bet per race | drop best 3 races | drop best 2 days |
|---|---|---|---|---|
| A reported | 0.77 | 0.67 (86) | 0.48 | 0.41 |
| A stated | 0.65 | 0.56 (209) | 0.49 | 0.43 |
| B reported | 1.33 | 1.13 (26) | 0.88 | **-0.33** |
| B stated | 0.70 | 0.87 (85) | 0.46 | 0.07 |
| B clean | 1.08 | 1.05 (101) | 0.73 | 0.51 |

Time-shift permutation of the residual (200 runs, B clean 150): A stated p 0.005, A reported p 0.005, B clean p 0.007, both for a common shift and a per-leg shift. Those are the smallest p values the runs can show. Null means -1.07c, -0.95c, -0.45c. The reported B set was not permuted.

**Time pattern, the weakest point.** Net by 3-day block of entry date:

| | block 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|
| A stated net | 1.52 | 1.34 | -0.06 | 0.26 | 0.47 | -0.38 |
| A stated excess over matched legs | 2.24 | 1.79 | 0.71 | 1.60 | 1.15 | 0.28 |
| B clean net | 2.12 (77) | 0.06 (63) | -2.75 (4) | | | |
| B clean excess | 2.28 | 1.38 | 1.56 | | | |

First 3 days of a panel against after: A stated +1.52c/+0.38c, A reported +2.01c/+0.30c, B stated +1.39c/-0.06c, B clean +2.12c/-0.11c, B reported +1.93c/-0.31c. I tested whether that is a panel-start artefact by scoring a snapshot of all legs 3c+ every 6 h, which is what the live list is. It is not an artefact. A net is positive on 81% of days (mean +0.86c), and excess is positive every day. B snapshots by day: +1.69c, +0.95c, +1.24c, -0.10c, -0.16c, +0.20c. So 25-27 Oct was about zero net. Excess stayed positive (+1.24c, +1.34c, +0.60c), so part of that is the D move in late October hurting R-side trades, but the excess is also shrinking. Shorter holds on the latest entries: entries from 27 Oct, 24 h +0.44c, 36 h -0.17c, 48 h -0.29c.

## 6. Independence and the D drift

- D index moved +3.15c in A and +5.78c in B.
- Side split: A stated D-side +0.69c, R-side +0.56c. A reported +1.10c, +0.39c. B clean D-side +2.04c, R-side -0.02c. B stated +1.55c, -0.38c. B reported +1.23c, +1.55c.
- Against matched near-line legs (same time, same partisan direction, own price within 10c, under 1.5c from the line): gross 1.40c vs -0.05c (A stated, excess 1.42c), 1.71c vs -0.16c (B clean, excess 1.87c). Excess by side, B clean: D 1.45c, R 2.35c. A stated: D 1.24c, R 1.61c. So the drift explains the D/R gap, not the profit. Regression on the national move: alpha unchanged (1.40c, 1.69c gross).
- Market-neutral net (excess less cost): A stated +0.67c, B clean +1.24c, B stated +0.87c. Those need a balanced book, which the partisan cap partly forces.
- Single events: no one race or day carries it in A. B reported has it all in two days. Top days in B clean are 21-22 Oct.
- Independent bets: A stated about 125 of 280, A reported about 60 of 105, B clean about 80 of 144, B reported about 30. Those come from only 14, 14, 6 and 4 time-by-side blocks, so B's are rough. Treat B as about 6-8 real days of evidence.

## 7. Overlap

- **With lag, label from both sides of ±72 h, same race and direction:** A stated overlap +0.81c (63), none +0.60c (217). B clean overlap +3.96c (36), none **+0.12c (108)**. That label uses a future lag signal, so it flatters the overlap group. Past signals only (72 h): B clean with +3.28c (20), without +0.73c (124). B stated with +2.67c (15), without +0.41c (101). A stated with +0.46c (42), without +0.68c (238).
- Reading: in early October the line trade is separate from lag. In late October most of its profit sits with trades a lag rule also takes. 41-47% of lag trades are near a line trade. Do not add the two estimates. In B the reported 30 trades happen to avoid lag (3% overlap), which is a side effect of the first-touch mask.
- Claim B and lag: claim B is lag trades filtered by the line, so right-side lag trades are the shared money, not an extra sleeve.
- Cheap favourites: 36-49% of fair-version line trades are in that territory (own outside fair 80c+, 2c+ under): A stated +0.53c (137), rest +0.76c. B clean +0.96c (52), rest +1.15c. The reported B set is much more concentrated: 77% of trades, +1.72c (23) against +0.07c (7). Line trades mostly hold a real gap to outside (mean 4.3-5.1c, 95% positive), so this is partly the favourite discount with an extra filter.
- Old distance. Snapshots, net by where the leg stood a day earlier: A under 2c +1.38c, 3c+ +0.53c. B under 2c +0.69c, 3c+ +0.53c. On a race basis A +1.39c/+0.58c, B +0.88c/+0.52c. In trades, "already 2.5c+ a day ago" was +0.22c (A, 52) and -0.71c (B, 19). **Of the frozen live list, 70% were already 3c+ a day ago and 48% have been over 2c for 24 h+.** So the live list is mostly the weaker group: use about +0.5c at the touch for it.

## 8. Realism

**Cost.** 44 saved books, 646 leg-snapshots at 3c+. Static walk, round trip against mid:

| shares | mean | median | p90 |
|---|---|---|---|
| touch | 0.62c | | |
| 1,000 | 0.85c | 0.83c | 1.39c |
| 2,000 | 0.98c | 0.96c | 1.53c |
| 5,000 | 1.34c | 1.21c | 2.13c |
| 10,000 | 1.81c | 1.66c | 2.84c |
| 20,000 | 2.40c | 2.31c | 3.64c |

The current book on the frozen list: 5,000 shares costs 1.42c mean, 1.34c median. So one sweep of 5,000 costs about 0.7c more than the touch number. A static walk overstates this if the book refills, and five 1,000-share clips would cost about 0.25c more. I use 0.25-0.7c. That takes most of the +0.5-0.7c. `FINDINGS.md`'s "2-5k shares about 0" is from our own orders, which picked their moments, so I would not use it here.

**Own orders.** Bot fills start 21 Oct 22:42, so A has none. In B our fills are 0.6% of tape volume. During the hold our net flow in the trade's direction is median 0, with no clean trade above 2% of leg volume (1 of 30 in the reported set) and p95 0.6%. Own fills sit in 2-3% of entry bins, none at exit. An own order at the touch we would hit: 3% of entries, median 48 shares. No contamination.

**Partisan cap (`sigbot/risk.py`), 5,000 shares a bet, no add past ±5,000 unless it cuts |net|, 72 h hold, from net D +4,700:** 66% of A stated and 71% of B clean fit. With cash limits, 20% and 16% at $30k, 40% and 35% at $65k. Cash binds before the cap. Net of trades kept is unchanged (+0.5c to +1.1c). At B the blocked ones earned more (+1.8c), because they were D-side in a D move.

**Live list.** 33 sides, of which 4 are chamber legs, and they are the top of the list even though the rule says chamber out (the `line_now` loop filters k, not the list). Without them: 29 sides, 24 races, 19 D-side, 10 R-side, $93k of cash at 5,000 each. In distance order from +4,700, 16 fit (8 D, 8 R). Both sides of one race count once. `FINDINGS.md` says "about 20 races, 100k shares, $65k", and only 2.9k of cash is spendable without the offload.

**Wind-down (`wind_down.py`, read, not run).** Line positions would not be exempt, since only cheap-favourite, churn, shock, catch-up, punt and hold-file positions are. It sells single legs at up to 1 tick under outside fair until 2 Nov, 2 ticks until 12:00 ET 3 Nov, then up to 3 ticks or 5.5% of price. Line positions sit under fair by 4-5c on average, so it would mostly not sell until the last hours. Where the MM quotes the leg it sells above the MM inventory cap, $1,500 per race, shrinking to 0 at 12:00 ET 3 Nov. So a sleeve needs its own exit and an exemption, or it risks forced sales late or positions still held at 18:00 ET. `FINDINGS.md` already suggests building on catch-up. Also: the MM quotes off SIG's price and may trade against these legs (from reading, not run). A 72 h hold has to start by 23:00 UTC 31 Oct to end before 18:00 ET 3 Nov (23:00 UTC). A 48 h hold allows to 23:00 UTC 1 Nov.

## 9. Claim B detail

| set | right side (2c+) | rest | within-day permutation p |
|---|---|---|---|
| Slow 3-20 Oct | +0.87c (40) | -0.01c (44) | 0.22 (plain 0.023) |
| Fast 3-20 Oct | +1.37c (13) | +0.07c (14) | 0.17 |
| Slow 21-30 Oct | +1.71c (28) | +1.27c (28) | 0.26 |
| Fast 21-30 Oct | +2.41c (17) | +1.19c (13) | 0.37 |

- Numbers reproduce. No look-ahead.
- Pooled within sample and day, clustered by race: premium +0.51c, se 0.40c, t 1.3 (raw 1.45c against 0.52c). Slow +0.43c, t 0.9. Fast +0.77c, t 1.4.
- Most of the early gap comes from which days the trades fell on: p moves from 0.023 to 0.22 once you compare within a day.
- Threshold fragile. Slow 21-30 Oct at 3c: +0.71c (14) against +1.75c (42), the wrong way round. Slow 3-20 Oct at 3c: +0.70c against +0.31c.
- Fast 3-20 Oct right side is 7 races, and +0.42c after dropping the best 3.
- "Use 1-1.5c for these and about 0.3c for the rest" is not supported. Rest is 0c early and +1.2c late. About 0.8-1c against 0.4-0.5c is defensible, as a size tilt only.
- The right-side trades themselves hold up on all clusters (two-way t 2.6-4.4), so keep them.

## 10. Estimate

| step | per leg |
|---|---|
| Fair versions, A stated and B clean | +0.65c, +1.08c |
| Snapshot means, A and B | +0.86c, +0.70c |
| Haircut for the later period and selection | about -0.2c |
| **At the touch** | **+0.5-0.7c**, about +0.5c on today's mostly old list |
| Size, 5,000 shares | -0.25c to -0.7c |
| **At 5,000 shares** | **about +0.2-0.4c** on a good day for execution, near zero if swept |
| Not shared with lag | about +0.1-0.6c at the touch, 0-0.3c at size |

So 16 bets at 5,000 shares is about +$160-320, and the extra over lag is lower, against the reported +$0.3-0.8k. Clips near the touch keep the edge but carry far less money. This does not change the answer that the punt matters far more.

If it is used: signal on the mid, not last trade. Run it through catch-up or give it an exemption. Skip chamber legs. Count one bet per race. Prefer distances that opened in the last day. Size by clips.

## What I could not check

- SIG spreads or books before 21 Oct, so A's 0.75c cost is assumed. B suggests it is fair to slightly high.
- Real impact of 5,000 shares: a static book against real fills.
- Whether Polymarket prices-history is mid or trade based in A. I measured age of change instead.
- Any market like the last 72 h before an election. Nothing in the sample is like it, and the market makers may behave differently.
- The trades lost in the outage (the tape misses 757k House D shares, per `chamber_review`).
- Wind-down and MM interaction are from reading code, not a run.
- The frozen live list is not scored yet. It can be scored from 3 Nov.
