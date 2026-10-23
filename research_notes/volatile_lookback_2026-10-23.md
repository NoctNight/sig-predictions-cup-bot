# Volatile moves, 1-day lookback (22 Oct 11:30 to 23 Oct 11:30 BST)

Read-only study. Nothing under sigbot/, config.json, params/, tests/ or deploy/ was touched, and no venue calls were made
(0 of the 20 allowed; the price-history endpoint needs the API key, which this study does not read). Reproduce with
`python3 research_notes/volatile_lookback_study.py`; it writes `data/research_volatile_lookback/results.json`.
One tick is 0.005. P&L is in ticks per share; S means SUSQies. t-statistics are clustered by race.

**Data.**
* **SIG mids:** `venue_fair` (all ~213 legs, ~85 s polls) merged with `quotes` (135 MM legs, ~35 s, with touch sizes).
  Where neither has a fresh quote, the tape fills in.
* **Outside fair:** `venue_fair.fair`, from Polymarket and Kalshi.
* **Tape:** 44k prints in the window, with the aggressor side.
* **Our records:** fills, orders, and netD from the HEALTH/cycle lines of sigbot.log.
* **Out-of-sample sets:**
  * B-pre: the 10.7 h of live quotes before the window (21 Oct 23:49 to 22 Oct 10:30 UTC).
  * A: 14 days of 5-minute candles plus Polymarket/Kalshi history (7-21 Oct), the same set as `directional_impact.md`.

**Coverage holes.**
* **The bot logged nothing for 262 min of the 24 h.** That covers 22 Oct 16:29-18:41, 23:21-00:33 and 23 Oct 10:08-11:06
  BST. Moves inside these gaps were found by comparing the mid before and after the gap. They have no fade or catch-up
  outcome.
* **Touch depth** exists only for MM-quoted legs.
* **The partisan cap** bound only from 23 Oct about 04:00 BST, when netD rose from +0.3k at the start of the window to
  +5.4k (`partisan cap hit` lines from 04:00 BST onward). Before that, D-adding trades were allowed.

## 1. The moves

**Detection rule.**
* A 5+ tick move of the SIG mid within 60 min, detected in flight on a 1-minute grid.
* The move extends while it makes new extremes. It ends when it gives back the larger of 2 ticks and half the move, or
  after 60 min without a new extreme.
* The price must be inside [0.03, 0.97]. Both legs of a race count once.
* A slower pass (10+ ticks within 4 h) found no move that the fast pass had missed.

**28 moves in 24 h, on 22 races.** In the 10.7 h before the window there were 16. Sizes ranged from 5 to 20 ticks
(median 6.5, total 235). All were two-sided: bid and ask both moved, so these were DMM requotes, not spread blow-outs.

| class | n | what it means |
|---|---|---|
| outside-led | 2 | Polymarket/Kalshi moved 1+ pt in the hour before: MN-01 R (lead about 120 min), VA-05 (lead about 2 min, outside +3.7 pts before) |
| concurrent | 4 | outside moved 1.5-7 pts during the move, not before: Idaho Senate, Georgia Governor, TX-23, WA-03 |
| SIG-only | 22 | outside flat (< 1 pt) before and during |

**Other features of the moves.**
* **Gaps to fair.** 17 of 28 moves went toward a pre-existing gap to the outside fair (gap before > 0). Examples: Texas
  Senate 9.5 -> 1.5 ticks, Alaska Governor 11.6 -> 2.8, CA-48 R 11 -> 3, Idaho Governor D 20 -> 9. The other 11 moved
  away from fair. MN-05, CA-48 D and WA-03 each opened 11-15-tick gaps, and the outside never followed.
* **Retrace (share of the move given back).**

  | after | median | mean |
  |---|---|---|
  | 15 min | 0.00 | 0.07 |
  | 1 h | 0.05 | 0.12 |
  | 4 h | 0.11 | 0.17 |

  Moves mostly stick. The exceptions are three tail spikes: Virginia Senate R 0.055 -> 0.1225 in one minute (52% back in
  15 min), Wyoming Senate D 0.0525 -> 0.115 (52% at 1 h) and Idaho Senate R (62% at 1 h).
* **News.** Web searches covered VA-05, Texas Senate, Georgia Governor, Idaho Senate, WA-03 and MN-01, all for
  22-23 Oct. They found no same-day trigger. The latest items predate the window: the UH Hobby Texas poll (released
  20-21 Oct, Talarico 48-46), the AJC/UGA and InsiderAdvantage Georgia polls (7-17 Oct), the OPB WA-03 poll (fielded
  Sept), the Post-Schar VA-05 poll (early Oct) and the Big Data MN-01 poll (29-30 Sept).
* **Simultaneous moves.** WA-03 D -15 and CA-48 D -14 started in the same minute (03:07 BST), with no outside move on
  CA-48. This points to a DMM batch requote.

**Hindsight bound.** With perfect foresight, taking all 235 ticks less a 1-tick spread each at the 2,000-share ceiling is
worth about 2,070 S. The two outside-led moves account for 26.5 ticks, so they bound anything predictable at about 265 S.

### Table: every move in the window (start in BST; outside in pts signed with the move; gap in ticks, + = fair on the move's side)

| # | start (BST) | dur min | race / leg | other leg | move (ticks) | p0 -> p1 | class | outside before / during / +4h (pts) | lead min | gap to fair before -> after | retrace 15m / 1h / 4h | touch bid/ask | tape sh (with-move share) | bot | our fills (race) | fade taker / passive (t/sh) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 22 11:45 | 125 | MN-01 R 1024 | R-6 | -6.5 | 0.7025 -> 0.6700 | outside-led | 1.03 / 2.25 / - | 120 | 6.4 -> 4.4 | 0.08 / 0.08 / - | 337/8205 | 3364 (0.58) | up; netD +267 | none | -2.0 / -1.0 |
| 2 | 22 11:53 | 43 | CA-48 R 893 | R-6,D+6 | -8.0 | 0.1200 -> 0.0800 | SIG-only | -0.0 / -0.0 / -0.0 | - | 11.0 -> 3.0 | 0.06 / 0.06 / - | 255/6860 | 5877 (0.02) | up; netD +208 | tilt 2, exit 1 | -4.0 / - |
| 3 | 22 12:35 | 118 | Idaho Senate R 952 | R-6 | -6.5 | 0.9100 -> 0.8775 | concurrent | 0.22 / 3.68 / - | -2 | -2.1 -> -1.2 | 0.15 / 0.62 / - | - | 27948 (0.07) | up; netD +315 | tilt 7 | 1.0 / - |
| 4 | 22 12:40 | 86 | Georgia Governor D 855 | R-7,R-7 | +8.0 | 0.5775 -> 0.6175 | concurrent | 0.95 / 2.01 / - | -56 | 3.1 -> -0.9 | - / 0.0 / - | 1404/250 | 13822 (1.0) | up; netD +575 | mm 5 | - / - |
| 5 | 22 13:05 | 182 | VA-05 R 1034 | D+16,R+6,D-5 | -20.0 | 0.6675 -> 0.5675 | outside-led | 3.7 / 7.43 / -6.45 | 2 | 7.9 -> 2.8 | 0.2 / - / 0.4 | - | 114378 (0.68) | down 38m; netD +710 | mm 9, inside 4, direction 3, exit 4 | - / - |
| 6 | 22 14:32 | 62 | Texas Senate D 982 | - | +8.0 | 0.6275 -> 0.6675 | SIG-only | - / 0.0 / -0.0 | - | 9.5 -> 1.5 | 0.0 / 0.0 / 0.0 | - | 42930 (0.87) | down 5m; netD +1392 | none | - / - |
| 7 | 22 14:46 | 35 | MI-10 R 1022 | - | -5.0 | 0.3025 -> 0.2775 | SIG-only | -0.55 / 0.39 / 0.3 | - | 0.4 -> -3.8 | -0.0 / -0.0 / 1.5 | - | 0 (-) | up; netD +1297 | mm 1 | 6.0 / - |
| 8 | 22 16:29 | 134 | MI-10 R 1022 | - | +6.5 | 0.2775 -> 0.3100 | SIG-only | -0.1 / -0.2 / 0.0 | - | 3.6 -> -3.3 | 0.0 / -0.08 / -0.54 | - | 27499 (0.61) | down (move inside gap); netD +1544 | arb 17, mm 1, inside 1 | - / - |
| 9 | 22 16:29 | 134 | WI-01 D 1038 | - | +6.0 | 0.4325 -> 0.4625 | SIG-only | -0.3 / 0.02 / 0.91 | - | -0.9 -> -6.9 | 0.0 / 0.0 / -0.17 | 6733/1065 | 20522 (0.76) | down (move inside gap); netD +1544 | arb 15, unknown 9 | - / - |
| 10 | 22 16:29 | 134 | Michigan Senate D 1047 | R-5 | +5.0 | 0.6775 -> 0.7025 | SIG-only | -0.32 / 0.14 / 0.17 | - | 4.3 -> -0.4 | 0.0 / 0.0 / 0.0 | - | 5166 (1.0) | down (move inside gap); netD +1544 | none | - / - |
| 11 | 22 19:11 | 46 | VA-07 R 1035 | - | +5.0 | 0.0425 -> 0.0675 | SIG-only | - / 0.06 / - | - | -3.5 -> -8.3 | 0.2 / 0.2 / 0.5 | - | 6593 (0.62) | up; netD +3130 | unknown 3 | - / - |
| 12 | 22 19:14 | 163 | Alaska Governor D 1057 | R-10,D+8 | +10.0 | 0.8125 -> 0.8625 | SIG-only | - / 0.6 / -0.03 | - | 11.6 -> 2.8 | 0.05 / 0.05 / 0.0 | 9054/892 | 33483 (0.91) | up; netD +3115 | arb 20, exit 1 | - / -3.0 |
| 13 | 22 19:16 | 7 | West Virginia Senate R 986 | R+5 | +5.0 | 0.9325 -> 0.9575 | SIG-only | - / -0.0 / -0.12 | - | 9.8 -> 4.8 | 0.0 / 0.0 / 0.0 | - | 0 (-) | up; netD +3016 | exit 3 | -1.0 / - |
| 14 | 22 19:18 | 52 | MI-10 D 1021 | - | -6.5 | 0.7150 -> 0.6825 | SIG-only | - / -0.0 / - | - | 1.7 -> -4.8 | -0.0 / -0.0 / - | 706/6950 | 915 (0.16) | up; netD +3025 | exit 1 | - / - |
| 15 | 22 21:06 | 48 | TX-23 R 935 | - | -6.0 | 0.6575 -> 0.6275 | concurrent | 0.67 / 3.66 / 1.03 | -18 | -1.3 -> -0.0 | -0.0 / -0.0 / -0.0 | - | 0 (-) | up; netD +3583 | exit 1 | -1.0 / - |
| 16 | 22 21:45 | 1 | Virginia Senate R 1053 | R+10,D-6 | +13.5 | 0.0550 -> 0.1225 | SIG-only | - / 0.0 / 0.0 | - | -7.0 -> -20.5 | 0.52 / 0.37 / 0.37 | - | 0 (-) | up; netD +3386 | none | 7.0 / - |
| 17 | 22 21:49 | 188 | Idaho Governor R 860 | - | -7.5 | 0.9550 -> 0.9175 | SIG-only | - / 0.0 / -0.05 | - | -4.5 -> -12.0 | -0.0 / 0.07 / 0.13 | - | 27958 (0.63) | down 72m; netD +3407 | tilt 11, arb 2 | -3.0 / -2.0 |
| 18 | 22 23:07 | 86 | MN-05 D 1027 | D-7 | -13.0 | 0.9525 -> 0.8875 | SIG-only | -0.01 / -0.35 / -0.17 | - | -1.7 -> -15.4 | -0.0 / -0.0 / -0.0 | - | 9704 (1.0) | down 72m; netD +2791 | arb 7 | -9.0 / -8.0 |
| 19 | 22 23:21 | 74 | NH-01 D 1064 | - | -6.0 | 0.9025 -> 0.8725 | SIG-only | - / 0.01 / -0.58 | - | -11.6 -> -17.6 | -0.0 / 0.17 / 0.17 | - | 33504 (0.33) | down (move inside gap); netD +2803 | mm 5 | - / - |
| 20 | 23 02:58 | 126 | MN-01 D 1023 | - | +9.5 | 0.3275 -> 0.3750 | SIG-only | -0.02 / 0.04 / 1.31 | - | 3.8 -> -5.6 | 0.05 / 0.05 / 0.11 | - | 13151 (0.63) | up; netD +2768 | none | -6.0 / -5.0 |
| 21 | 23 03:07 | 48 | WA-03 D 1036 | R+7,R+7 | -15.0 | 0.9075 -> 0.8325 | concurrent | -0.04 / 1.47 / -0.5 | -28 | -2.5 -> -14.6 | -0.0 / 0.2 / 0.13 | - | 27331 (0.01) | up; netD +3203 | none | -7.0 / -6.0 |
| 22 | 23 03:07 | 48 | CA-48 D 892 | R+8 | -14.0 | 0.9450 -> 0.8750 | SIG-only | 0.02 / -0.02 / 0.02 | - | 2.6 -> -11.4 | 0.04 / 0.04 / -0.11 | - | 12036 (0.0) | up; netD +3203 | none | -10.0 / -6.0 |
| 23 | 23 03:52 | 50 | WI-01 D 1038 | - | +5.0 | 0.4625 -> 0.4875 | SIG-only | 0.0 / -0.01 / 0.61 | - | -5.7 -> -10.7 | 0.2 / 0.2 / 0.2 | - | 12217 (0.76) | up; netD +4131 | none | 0.0 / - |
| 24 | 23 06:26 | 24 | Wyoming Senate D 1054 | D+11,D-6 | +12.5 | 0.0525 -> 0.1150 | SIG-only | -0.0 / 0.0 / - | - | -8.0 -> -20.5 | 0.12 / 0.52 / 0.56 | - | 742 (0.32) | up; netD +5411 | direction 2, arb 14 | 3.0 / 5.0 |
| 25 | 23 07:06 | 18 | Idaho Governor D 859 | R+7,R+7 | -11.0 | 0.1225 -> 0.0675 | SIG-only | -0.0 / -0.05 / - | - | 20.1 -> 9.0 | 0.18 / 0.23 / - | - | 13198 (0.61) | up; netD +5388 | tilt 8 | - / - |
| 26 | 23 07:21 | 59 | Wyoming Senate D 1054 | - | -5.0 | 0.1075 -> 0.0825 | SIG-only | -0.0 / 0.0 / - | - | 19.0 -> 14.0 | -0.0 / -0.0 / - | - | 38217 (0.76) | up; netD +5019 | tilt 5 | - / - |
| 27 | 23 07:51 | 36 | U.S. Senate R 843 | - | -6.0 | 0.3425 -> 0.3125 | SIG-only | 0.42 / -0.42 / - | - | 6.4 -> -0.4 | -0.0 / -0.0 / - | - | 2 (1.0) | up; netD +5682 | none | -3.0 / - |
| 28 | 23 09:21 | 17 | Wyoming Senate D 1054 | - | -5.0 | 0.0825 -> 0.0575 | SIG-only | -0.05 / -0.05 / - | - | 13.9 -> 8.8 | - / 0.5 / - | - | 0 (-) | down 30m; netD +5411 | none | 1.0 / - |

Columns: "fade taker / passive" is rule (b) below at the detection minute. Passive is shown only if the order was filled.
"our fills (race)" covers all legs of the race from 10 min before the move to 60 min after it.

**What our bot did.**
* **Fills.** 18 of the 28 moves had fills on the race, mostly arb, tilt and exit. The fills the moves ran over cost
  -0.2 S in total at the 1-hour mark.
* **MM.** It had 14 fills (945 shares) inside move windows, worth +3 S at 1 h and 0 at 4 h. The moves cost the MM
  nothing. Across the whole day the MM made +105 S at 1 h and -36 S at 4 h on 29.8k shares.
* **Vol gate.** I re-derived the live rule (|mid - mid 15 min earlier| >= 3 ticks) from the mids. It would have been hot
  on 21 of the 24 moves outside the gaps. For 15 of them it went hot within 0-10 min of the start. The slow ones took
  29-86 min (Alaska Governor, CA-48 R, WI-01 D, and Georgia Governor, which went hot only at the move's end), and VA-07,
  TX-23 and Idaho Governor D never triggered it.
* **The RETRACE PAUSE never fired** (no log line). The paper detector logged 51 events, but none was on a leg the MM was
  quoting at the time.
* **The direction sleeve** traded in two of the races: VA-05 (3 fills) and Wyoming Senate (2 fills).

## 2. What the rules would have made (window, then out of sample)

**Common rules.**
* Size is 600 of cost per leg, capped at 2,000 shares (about one DMM touch).
* Entry and exit are taker at the touch unless stated otherwise.
* A trade is "feasible" if it does not add D while netD is at or above 4.8k.
* S/day = sum over trades of ticks x 0.005 x qty, divided by days.

### (b) Fade the move

* **Taker:** hit the touch against the move at detection.
* **Passive:** rest at the far touch. It fills if the book or a print goes through the order within 60 min.
* **Exit:** taker at half retrace, or at 4 h.
* **Momentum:** follow the move at detection instead.

| rule | window n / hit / mean (t) / S/day | B-pre n / hit / mean (t) / S/day |
|---|---|---|
| fade taker | 16 / 0.31 / -1.75 (-1.3) / -108 | 16 / 0.06 / -3.38 (-3.1) / -883 |
| fade passive (fill rate 54% / 56%) | 8 / 0.12 / -3.25 (-2.3) / -114 | 9 / 0.11 / -3.56 (-2.2) / -489 |
| ... gap to fair widened | 6 / 0.17 / -3.67 (-1.9) | 1 / - / -2.0 |
| ... gap to fair closed | 2 / 0 / -2.0 | 8 / 0.12 / -3.75 (-2.0) |
| momentum taker, +1 h | 18 / 0.33 / 0.00 (0.0) / -13 | 16 / 0.25 / +0.19 (0.2) / -8 |
| momentum taker, +4 h | 13 / 0.31 / -0.08 (-0.1) / -86 | 16 / 0.44 / +1.06 (1.0) / +219 |
| deep tail ladder (+-6 ticks, legs <= 0.2 or >= 0.8, re-anchored hourly, +4 h mark) | 17 / 0.24 / -3.29 (-2.4) / -297 | 3 / - / -1.33 |
| ... +-8 ticks | 10 / 0.20 / -2.15 (-1.0) | 1 / - / -0.5 |
| ... +-12 ticks | 5 / 0.40 / +2.2 (0.6) / +44 | 0 |

A (6-20 Oct, `volatile_moves.md`): a passive fade of any 5+ tick move, marked at 60 min, made -0.95 (t -5.4).

**Verdict:** fading loses in all three samples, and the moves barely retrace. Momentum and the deep ladder are noise.

### (a) Catch-up: follow a Polymarket/Kalshi consensus move that SIG has not followed

**Signal.**
* The outside fair moves X pts or more within 30 min.
* Polymarket and Kalshi agree within 4 pts.
* The SIG mid moved less than 1/3 of the outside move.
* At most one signal per race per hour.
* Entry is taker at the touch. The exit is the opposite touch at +1 h or +4 h. A charges 1 tick for the round trip.

| sample | X | n | hit | +1 h | +4 h (t) | mid/mid +4 h | S/day |
|---|---|---|---|---|---|---|---|
| window | 1.5 | 30-37 | 0.27 | -0.95 (-4.0) | -0.67 (-1.5) | +0.47 | -82 |
| window | 2 | 14-16 | 0.57 | -0.50 | +0.86 (1.9) | +2.07 | +87 |
| window | 3 | 3-6 | 1.00 | -0.33 | +2.33 | +3.5 | +39 |
| B-pre | 2 | 10 | 0.60 | +1.9 | +4.2 (2.2) | +5.25 | +514 |
| A (14 d) | 1.5 | 82 | 0.62 | -0.40 (-2.2) | +0.16 (0.5) | +1.16 | +3 |
| A (14 d) | 2 | 38 | 0.63 | -0.03 | +0.66 (1.0) | +1.66 | +8 |
| A (14 d) | 3 | 7 | 0.71 | 0.0 | +1.0 (0.8) | +2.0 | +3 |
| **pooled A+B** | **2** | **62** | 0.61 | | **+1.27 (2.1)** | | **29** |
| pooled, without top 3 races (MI-07, MT-01, NV Gov) | 2 | 51 | 0.57 | | **+0.29 (0.9)** | | 7 |
| pooled A+B | 3 | 12 | 0.83 | | +2.92 (2.3) | | 14 |
| pooled, without top 3 races | 3 | 9 | 0.78 | | +0.67 (1.3) | | 3 |

**How to read it.**
* The by-day results on A swing around zero, except on 21 Oct (+3.2). Like the earlier tests, the live-quote set B
  (21-23 Oct) is the best period.
* Requiring Polymarket and Kalshi to agree is what lifts the rule above the earlier PM-only replay (-0.64, t -3.8).
* Only 1 of the 22 window signals at 2 pts was followed by a 5-tick SIG move within 60 min. That was VA-05, where the
  signal came at 13:06 BST, 45 min before the move: +4 ticks at 1 h on 1,690 shares, about 34 S.
* **Verdict:** a positive sign but not robust. Without its three best races it is t 0.9.

### (c) MM changes

| rule (window) | fills blocked | shares | saved at 1 h | saved at 4 h |
|---|---|---|---|---|
| oracle: no fill against the move from its start to its end | 14 | 945 | -3 S (the fills made money) | 0 |
| after detection: pull the side the move runs into until end + 60 min | 2 | 66 | 0 | -1 |
| pre-pull: after a 1.5+ pt outside move, pull that side for 60 min (45 signals) | 12 | 1,369 | -7 | +7 |

B-pre: zero, zero and 3 fills (+-2 S).

**Verdict:** the moves did not cost the MM anything, so a better rule cannot save anything. The MM's real loss is the
slow 3-6 h drift toward the outside fair (`mm_upgrade.md`), which the existing outside-fair gate handles.

### (d) Pre-positioning signals: firings, hits, false positives

A hit is a 5+ tick SIG move in the signal's direction within 60 min. Signals were checked on a 5-min grid, at most one
per race per hour. The base rate is the chance of a 5+ tick move in either direction within 60 min of a random grid point.

| signal | sample | fired/day | hit rate | base rate | false positives/day | median lead | taker +1 h (t) |
|---|---|---|---|---|---|---|---|
| touch imbalance >= 0.6 (MM legs) | window | 281 | 0.7% | 0.7% | 279 | 42 min | -0.97 (-13) |
| | B-pre | 143 | 0 | 0 | 143 | - | -0.93 (-11.5) |
| tape flow >= 80% one-sided and >= 10k shares in 15 min | window | 98 | 5.1% | 0.5% | 93 | 60 min | +0.16 (0.7) |
| | B-pre | 85 | 0 | 0.7% | 85 | - | -0.40 (-2.3) |
| outside drift >= 1 pt / 30 min, SIG < 1/3 | window | 94 | 1.1% | 0.5% | 93 | 50 min | -1.00 (-8.1) |
| | B-pre | 119 | 7.1% | 0.7% | 111 | 32 min | -0.31 (-0.9) |
| scheduled events | - | - | - | - | - | - | none in the window: no debates and no poll releases timed with a move; early-vote releases did not line up |

**Verdict.**
* Imbalance is pure noise.
* Flow and outside drift lift the hit rate 2-10x over the base rate, but they still miss more than 90% of the time.
* No pre-positioning trade pays the spread.

## 3. Recommendations (nothing implemented)

1. **Keep the catch-up rule in paper and re-test it; do not trade it yet.**
   * Rule: X >= 3 pts with Polymarket and Kalshi agreeing; SIG has moved < 1/3; taker at the touch; exit at the opposite
     touch at +4 h. Size 600 per leg, 2,000 shares at most.
   * While netD >= 4.8k, take only R-adding signals.
   * If it is real, it is worth +5-30 S/day, so about 50-300 S before settlement.
   * Evidence is weak: pooled t 2.1-2.3, but 0.9-1.3 without the top 3 races.
   * Confirming test: 30+ new signals over 3+ days, mean > +1 tick after the spread, t > 2 without the top 3 races. The
     `outside_lead` paper orders in `data/volatile_paper.jsonl` already approximate this; add the PM/Kalshi agreement
     filter.
2. **Do not build a fade, momentum or deep-ladder sleeve, and do not change the MM for moves.**
   * The fades lost -1.8 to -3.6 ticks per share in both samples (about -100 S/day in the window). The MM lost nothing to
     the moves.
   * This avoids about 100 S/day of losses compared with a naive fade.
   * Evidence is strong: it agrees with `volatile_moves.md` (t -5.4).
   * Confirming test: none needed. Re-run this script weekly.
3. **Reduce bot downtime (operational, not a trading rule).**
   * 4 of 28 moves, and 262 min of the day, fell in deploy or fix gaps. The VA-05 catch-up had no 4 h exit quote for
     that reason.
   * The value is small for moves themselves, under 20 S/day, but downtime also stops the arb and MM sleeves.
   * Test: the share of the day with cycles under 15 min apart. It was 82% in this window.

Noise, stated plainly: the fades of every kind; momentum after the move; the deep tail ladder; book imbalance; tape flow;
outside drift at 1-1.5 pts; and catch-up at 1.5 pts.
