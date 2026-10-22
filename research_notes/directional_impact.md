# Directional trades toward the outside consensus, sized by market impact (23 Oct 2026)

Question (Sizhe): "make directional trades on liquid markets, modelling market impact to decide sizing, so that we can buy
low, wait for it to rise and then sell." Read-only study: no bot, config or param changes, no venue calls.
Reproduce: `python3 research_notes/directional_impact_study.py [all|robust|sizing]`. Outputs (events, taker orders, raw
tables) are in `data/research_directional/` (`out_v1.md`, `out_robust.md`, `out_sizing.md`, `events.json`).
One tick = 0.005. P&L is in ticks per share unless stated otherwise.

## Answer

1. **Size is not the problem. The edge is.** On liquid legs, the touch on each side holds a median of 5.8k shares
   (p25 2.9k). A market order up to the touch walks nothing, so a round trip costs exactly the 1-tick spread. Our own
   taker orders confirm this up to 3k shares: they paid 0.50-0.58 tick against the mid and slipped zero beyond the
   touch. Impact only starts to matter above roughly 5k shares per leg, far above the current caps (600 notional is
   about 700 shares of a 0.85 favourite).
2. **On the 14-day history (7-21 Oct), SIG does not move toward the outside fair fast enough to pay a 1-tick round
   trip.** Mid-to-mid convergence on legs at least 4 ticks off the consensus is +0.1 to +0.4 tick at 4-12 h and at most
   +1 tick at 24 h. After the spread, taker-in/taker-out loses in every bucket and every horizon from 1 h to 24 h. Out of
   sample (17-21 Oct, OOS1), gaps of 6+ ticks held up to 12 h lose **-0.62 tick per share (t -4.9)**. Gaps almost never
   close (1-2% of trades exit on the close rule): SIG keeps the gap.
3. **The only positive result is 21-22 Oct.** On the live-quote set (OOS2, 21 Oct 23:49 to 22 Oct 22:21 UTC), gaps of
   6+ ticks held up to 12 h make **+1.65 tick per share (t 2.9 clustered by race)**. Without the top 3 races this is
   +0.68 (t 2.3), and +1.36 after removing a market-wide drift. The 4 h version is still negative (-0.33). All of these
   12 h entries fall in one night (22 Oct 00:00-10:00 UTC). In the candle data, only 3 of 14 days are positive at 12 h,
   and two of them are 21 Oct and 22 Oct. This could be a new regime: SIG has started converging, perhaps because the
   direction sleeve and other traders now push toward Polymarket/Kalshi. It could also be one trending night, which
   volatile_moves.md already flagged. Two days cannot tell these apart.
4. **Verdict: no robust positive bucket.** A bucket clears (out of sample, t > 2) only in OOS2 at the 12 h hold, and the
   same bucket fails OOS1 at t -4.9. At the base rate, the impact-optimal size is **0**. If the 21-22 Oct regime holds,
   the optimal size is the smaller of the two touch sizes, about 3,200 shares (about 2.7k notional at 0.85). That would
   make about 50-120 S/day on the 6k the sleeve may use (0.8-1.9% per day). Arb sets make 0.23% per day. At the base
   rate the same sleeve loses about 45 S/day. Recommendation: do not scale up. If Sizhe wants to test the regime
   question, run the gated trial in section 6 at the current caps. That is a bounded bet of roughly ±50 S/day.

## 1. Data and method

| set | window | SIG price | outside fair | use |
|---|---|---|---|---|
| A | 7 Oct 12:00 - 21 Oct 22:00 UTC (14 d) | 5-min candle close (1-h close before the first 5-min bar), forward-filled, as the mid | Polymarket 5-min + Kalshi 1-min D-leg history (`data/model_history.json`), averaged | IS = before 17 Oct 00:00 (9.0 d); OOS1 = 17-21 Oct (5.0 d) |
| B | 21 Oct 23:49 - 22 Oct 22:21 UTC (0.94 d) | real bid/ask/mid from `venue_fair` (the direction sleeve's ~2-min poll) | `venue_fair.fair` (depth-weighted PM/Kalshi) | OOS2 (never used for any choice) |

* **Liquid legs:** 66 of 237. A leg qualifies if its median spread over B is 1 tick or less, its touch is at least 2k
  shares on both sides, and it shows at least 20k shares within 5 ticks of the touch on both sides
  (`data/books.json`, 21 Oct 22:03). Its mid must also be in [0.03, 0.97]. At entry, the spread must be 2 ticks or less.
* **Consensus:** both venues price the leg and agree within 4 points, the same rule as `ExplainedDirection`.
* **Event:** the gap is g = (fair - mid)/tick on the race's D leg. The R leg is the same bet, so it is not counted
  again. At most one event per race per hour. Buckets of |g|: 1-2, 2-4, 4-6, 6-10 and 10+ ticks. When g > 0 we buy YES
  at the ask; when g < 0 we buy NO, which is selling YES at the bid.
* **Exit rule:** exit at the first poll where the signed gap is 0 or less (mid reaches fair), or after H = 1, 4, 12 or
  24 h. The exit is a taker trade at the touch (T/T). Mid/mid (gross) and "taker in, passive out" are reported as upper
  bounds.
* **Spread in A:** set A has no bid/ask, so each leg is charged its median B spread (1 tick on liquid legs). **Method
  check:** I rebuilt B using the tape's last trade as the mid (method A) and compared it with the true quotes on the same
  window. For gaps of 6+ ticks at 12 h, mid/mid is +2.58 with tape closes and +2.83 with quotes; T/T is +1.58 and
  +1.65. Method A therefore measures the same thing, so the gap between A and B comes from the period, not the method.
* **t-statistics** are cluster-robust by race. Dropping the top 3 races removes the three with the largest positive
  P&L contribution.

## 2. Event study: SIG mid move toward the outside fair (liquid legs, ticks signed toward fair, t in brackets)

| split | bucket | n | races | mean gap | +15 min | +1 h | +4 h | +24 h | fair's own move +4 h |
|---|---|---|---|---|---|---|---|---|---|
| IS | 2-4 | 920 | 20 | 2.8 | -0.01 (-1.0) | -0.06 (-2.2) | -0.17 (-2.9) | -0.82 (-3.6) | -0.01 |
| IS | 4-6 | 941 | 22 | 4.8 | -0.02 (-1.3) | -0.01 (-0.5) | -0.05 (-0.7) | +0.06 (+0.2) | -0.02 |
| IS | 6-10 | 1141 | 26 | 7.7 | +0.03 (+1.4) | +0.06 (+2.0) | +0.17 (+2.0) | +0.11 (+0.3) | +0.17 |
| IS | 10+ | 732 | 18 | 14.2 | +0.01 (+0.4) | +0.09 (+3.5) | +0.19 (+3.0) | +1.04 (+5.4) | -0.04 |
| OOS1 | 2-4 | 265 | 17 | 3.0 | +0.01 | -0.04 | -0.46 (-1.8) | -2.18 (-1.6) | -0.09 |
| OOS1 | 4-6 | 353 | 18 | 5.0 | -0.01 | -0.09 | -0.08 (-0.6) | -0.16 (-0.2) | -0.01 |
| OOS1 | 6-10 | 1025 | 25 | 7.7 | +0.03 | +0.02 | +0.04 (+0.4) | +0.19 (+0.5) | -0.02 |
| OOS1 | 10+ | 1089 | 21 | 14.6 | -0.04 | +0.07 | +0.27 (+4.0) | +0.81 (+3.3) | +0.05 |
| OOS2 | 2-4 | 179 | 20 | 3.0 | +0.02 | +0.08 (+2.1) | +0.18 (+1.2) | - | -0.26 |
| OOS2 | 4-6 | 125 | 16 | 4.9 | +0.02 | +0.11 (+2.0) | +0.41 (+2.1) | - | -0.36 |
| OOS2 | 6-10 | 82 | 12 | 8.2 | +0.10 | +0.31 (+3.3) | +1.30 (+4.0) | - | -0.05 |
| OOS2 | 10+ | 151 | 11 | 15.3 | +0.02 | +0.14 (+5.3) | +0.70 (+6.6) | - | -0.18 |

The 1-2 bucket is in `out_v1.md` and is flat or negative everywhere. Gaps of 2-4 ticks drift slightly *away* from fair
(IS t -3.6 at 24 h). Large gaps converge by about 0.3% of the gap per hour in A and 1-4% per hour in B. After 4 h the gap left is still
about 98% of the starting gap in A and 85-95% in B. The +24 h column could not be measured in B, which covers less than a day.

## 3. The trade: buy the touch, sell the touch when the gap closes or after H hours (ticks/share)

| split | bucket | H | n | T/T | t | mid/mid | T/T without top-3 races (t) | mean hold h |
|---|---|---|---|---|---|---|---|---|
| IS | 4-6 | 12 | 941 | -1.05 | -6.3 | -0.05 | -1.15 (-7.0) | 11.8 |
| IS | 6-10 | 12 | 1141 | -0.72 | -3.3 | +0.28 | -0.98 (-5.6) | 12.0 |
| IS | 10+ | 12 | 732 | -0.48 | -5.0 | +0.52 | -0.53 (-5.7) | 12.0 |
| IS | 10+ | 24 | 732 | +0.04 | +0.2 | +1.04 | -0.21 (-1.2) | 24.0 |
| OOS1 | 4-6 | 12 | 291 | -0.65 | -2.1 | +0.35 | -1.09 (-3.9) | 11.5 |
| OOS1 | 6-10 | 12 | 965 | -0.89 | -4.2 | +0.11 | -1.08 (-5.7) | 11.9 |
| OOS1 | 10+ | 12 | 986 | -0.36 | -2.7 | +0.64 | -0.52 (-5.2) | 12.0 |
| OOS1 | 10+ | 24 | 864 | -0.19 | -0.8 | +0.81 | -0.45 (-2.3) | 24.0 |
| OOS2 | 4-6 | 4 | 109 | -0.70 | -3.6 | +0.40 | -0.94 (-7.2) | 3.8 |
| OOS2 | 6-10 | 4 | 63 | +0.21 | +0.4 | +1.37 | -0.70 (-1.9) | 3.9 |
| OOS2 | 10+ | 4 | 129 | -0.60 | -5.3 | +0.59 | -0.68 (-6.3) | 3.8 |
| OOS2 | 4-6 | 12 | 74 | +0.04 | +0.1 | +1.11 | -0.72 (-3.4) | 10.6 |
| OOS2 | 6-10 | 12 | 43 | **+2.98** | **+3.0** | +4.15 | +0.60 (+1.1) | 11.7 |
| OOS2 | 10+ | 12 | 90 | **+1.01** | **+2.9** | +2.20 | +0.52 (+1.9) | 11.8 |

Pooled results (liquid legs; the all-legs set has the same signs):

| split | gap | H | n | races | T/T (t) | mid/mid (t) | T/T without top 3 |
|---|---|---|---|---|---|---|---|
| IS | 6+ | 12 | 1873 | 28 | -0.63 (-4.6) | +0.37 (+2.7) | -0.77 (-7.3) |
| OOS1 | 6+ | 12 | 1951 | 29 | -0.62 (-4.9) | +0.38 (+3.0) | -0.76 (-7.1) |
| OOS2 | 6+ | 4 | 192 | 17 | -0.33 (-1.6) | +0.85 (+4.8) | -0.61 (-4.4) |
| OOS2 | 6+ | 12 | 133 | 15 | **+1.65 (+2.9)** | +2.83 (+5.3) | +0.68 (+2.3) |
| OOS2 | 4+ | 12 | 207 | 24 | +1.07 (+2.5) | +2.22 (+5.4) | +0.41 (+1.5) |

Every 1 h hold loses about one spread in every split (-0.9 to -1.2). The close rule almost never fires: 0-2% of exits in
A and 14-22% in B. "Wait for it to rise" therefore usually means waiting for the time stop.

**OOS2 robustness** (`out_robust.md`):

| gap, H | T/T | t by race | t by 2-h entry block (blocks) | market factor | T/T minus factor | entries |
|---|---|---|---|---|---|---|
| 6+, 12 h | +1.65 | +2.9 | +14.1 (7) | +0.29 | +1.36 | 21 Oct 23:49 - 22 Oct 10:03 |
| 6+, 4 h | -0.33 | -1.6 | -1.5 (11) | +0.08 | -0.42 | 21 Oct 23:49 - 22 Oct 18:06 |
| 10+, 12 h | +1.01 | +2.9 | +4.9 (7) | +0.27 | +0.74 | same night |

* The market factor is the other liquid races' D-leg move over the same window, signed with the trade. It explains
  only 0.3 tick.
* 97% of OOS2 events with gaps of 4+ ticks are on the favourite's side (200 favourite vs 7 longshot at 12 h). The
  sleeve's favourite-only filter therefore changes nothing here.
* The 7-block t of +14 should not be read as strong evidence: 7 consecutive 2-hour blocks of one night are not 7
  independent draws.
* Set A by day, gaps of 6+ ticks, 12 h, T/T: 8 Oct +0.08, 10 Oct +1.09, 21 Oct +1.03, and -0.16 to -1.37 on the other
  11 days. On 21 Oct the convergence was already about 2 ticks mid/mid in 12 h, before the direction sleeve's first
  trade (22 Oct 00:42 UTC).

## 4. Market impact model

**Static walk** from the 21 Oct 22:03 ladders (liquid legs, both sides). The table shows the extra ticks per share
beyond the touch for a q-share market order.

| q | 1,000 | 2,500 | 5,000 | 10,000 | 25,000 | 50,000 |
|---|---|---|---|---|---|---|
| median | 0.00 | 0.00 | 0.00 | 0.52 | 1.22 | 1.87 |
| p75 | 0.00 | 0.00 | 0.43 | 0.87 | 1.68 | 2.52 |
| p90 | 0.00 | 0.09 | 0.57 | 1.21 | 2.12 | 3.08 |

**Fitted model:** one-way cost per share against the mid is c(q) = s/2 + w(q), with s = 1 tick. The walk w(q) is 0 for
q up to the touch (median 5.8k, p25 2.9k). Above the touch, w(q) ≈ k·q/1000 with k = 0.046 tick per 1,000 shares
(median; p25 0.019, p75 0.071), or equivalently w(q) ≈ 0.21·q/touch. The round trip costs
**RT(q) = 1 tick + w_in(q) + w_out(q)**, where w_out is walked on the opposite side's ladder. The real step function
matters more than the linear fit. Each share beyond the touch costs one more tick on entry and one more on exit.
Whenever the expected gain is below 2 ticks, the optimal size is therefore the smaller of the two touch sizes.

**Realised taker orders** (our arb, direction and exit orders, priced against the poll nearest before placement;
`taker_orders.json`):

| purpose | size | n | slip beyond touch | cost vs mid | fills over 2+ levels | mid drift with our trade +5 min | +30 min | +2 h |
|---|---|---|---|---|---|---|---|---|
| arb | <250 | 117 | -0.03 | +0.58 | 0% | +0.08 | +0.25 | +0.34 |
| arb | 250-1k | 135 | -0.06 | +0.53 | 1% | +0.10 | +0.33 | +0.32 |
| arb | 1k-3k | 73 | -0.05 | +0.57 | 7% | +0.35 | +0.21 | +0.14 |
| arb | 3k+ | 5 | +0.20 | +0.80 | 0% | +0.90 | - | - |
| direction | <1k | 44 | 0.00 | +0.55 | 0% | +0.10 | +0.22 | +0.32 |
| exit | <1k | 53 | 0.00 | +0.54 | 2% | +0.07 | -0.19 | -0.21 |

* Realised cost matches the model: half a spread, with no walk up to 3k shares.
* After our takes, the mid drifts 0.1-0.3 tick *in our direction* over 5 min to 2 h. Arb sets carry no information, so
  this is the DMM stepping toward our flow, the same effect hft_strategies.md found for sweeps. For a round trip it
  lifts the mark between entry and exit, so it is not a cost. I did not credit it, and B's convergence may contain a
  little of our own sleeve's impact.

## 5. Optimal size per trade, S/day, and comparison with arbs

The size q maximises q·(m - w_in(q) - w_out(q))·tick - (q·σ·tick)²/(2T) on each real liquid ladder. Here m is the
small-size T/T gain per share after the spread, σ is the per-share standard deviation of T/T, and T is a risk tolerance
in S (`out_sizing.md`).

| m (ticks/share) | source | q* median (p25-p75) | E[S]/trade at q* | E[S]/trade at the cap (750 sh) |
|---|---|---|---|---|
| -0.62 | OOS1 base rate, 6+ ticks, 12 h | **0** | 0 | -2.3 |
| +0.68 | OOS2 without top 3 | 3,200 (2,400-6,300) | 10.7 | +2.6 |
| +1.00 | middle case | 3,850 (2,500-6,600) | 15.9 | +3.8 |
| +1.65 | OOS2, all races | 7,700 (4,100-11,900) | 41.2 | +6.2 |

* The risk penalty barely matters. At T = 2,000 S, q* changes by under 15%; at T = 500 S, by under 25%. One trade's
  standard deviation at 3k shares is about 45 S.
* Correlation does matter: all OOS2 profit came from one night, so the real risk is a national, regime-wide factor, not
  per-trade noise.

**Against the existing caps** (direction.per_market_cap 600 notional per leg, 5,000 net-D partisan cap,
overall_cash_frac 0.6 of about 10k free cash = 6k):

* The cap binds well before impact does. At a bought-side price of about 0.85, 600 notional is about 700 shares, a
  quarter of q*.
* q* of 3.2k shares is about 2.7k notional per leg. Buying it on the 11-18 races that show a 6+ tick gap at any hour
  would need 30-50k, but only 6k is available.
* The partisan cap is not the binding limit: about 64% of these trades are D-long, so the 6k sleeve nets about 1.8k D.

| scenario | trades/day (12 h holds, cash-limited) | S/day | return on deployed capital per day |
|---|---|---|---|
| base rate (m = -0.62), current caps | ~20 | **about -45** | -0.7% |
| m = +0.68, current caps (10 x 600) | ~20 | about +48 | +0.8% |
| m = +0.68, q* sizing (3.2k sh, about 2.7k notional), all 10k free cash | ~7 | about +80 | +0.8% |
| m = +1.65, current caps | ~20 | about +116 | +1.9% |
| **arb sets** (~3% to settlement over ~13 d) | - | about +14 on 6k, about +23 on 10k | **+0.23%, riskless** |

If the 21-22 Oct regime is real, the directional round trip beats arbs on capital by 3.5-8x. Without any walk, the
return per unit of capital does not depend on size: q* only puts more capital into each race. If the 14-day base rate
holds, it loses about 2 S per trade. The evidence currently favours the base rate: 12 of 14 days, OOS1 at t -4.9, and
the 4 h hold negative even in OOS2.

## 6. Spec for a gated trial (only if Sizhe wants to test the regime; not implemented)

This is a variant of `ExplainedDirection` value signals, run as its own purpose (`dir_rt`) so its P&L can be read
separately.

* **Universe.** Liquid legs only: median spread of 1 tick, touch of at least 2k on both sides, at least 20k within 5
  ticks, and mid in [0.05, 0.95]. Both Polymarket and Kalshi must price the leg, agree within 4 points, and have been
  fresh within 5 min. Trade the favourite's side only. One leg per race.
* **Entry.** Enter when the gap g = (fair - SIG mid)/tick is 6 or more on the favourite's side and the SIG spread is 1
  tick. Take the touch with a limit order at the touch, never walking. Allow at most one open dir_rt position per race.
* **Size.** q = min(per_market_cap / price, 0.8 × entry-side touch, 0.8 × exit-side touch), and 0 if q < 200.
  - Do not raise per_market_cap for the trial; at 600 this is about 700 shares.
  - Only if the regime gate has passed for 3 days, q may rise toward min(touch_in, touch_out). That is the impact-optimal
    size while m < 2 ticks. Never cross a second level.
* **Exit.** Exit when the signed gap reaches 0 or less, or after 12 h, whichever comes first.
  - First rest at the far touch (sell at the ask) for 15 min.
  - Then take the near touch.
  - Exit at once if Polymarket and Kalshi move to disagree by more than 4 points, or if the fair goes stale for over
    15 min.
* **Caps.** Keep everything that exists: direction.per_market_cap 600, the 5,000 partisan cap, and overall_cash_frac
  0.6. On top of that, cap dir_rt cost at 3,000 notional, so it stays at or below half the sleeve and never draws cash
  from arb sets or the late punt.
* **Regime gate (kill switch).** Compute this rule's paper T/T from `venue_fair` every hour, using 12 h holds that
  closed in the trailing 48 h. Trade only while all three hold:
  - the mean is at least +0.5 tick per share;
  - at least 4 of the last 6 4-hour entry blocks are positive;
  - the trial's own realised P&L is above -100 S.

  Otherwise stay in paper. The gate keeps the trial from scaling on one good night.
* **Review.** After 3 days and 60 or more closed trades, keep the trial if the realised T/T is at least +0.5 (t > 2,
  clustered by 4-hour block). Otherwise remove it.

## 7. Caveats

* B is under one day long, and every 12 h entry in it comes from a single night. A has 14 days but no bid/ask. The
  method check shows A's close-as-mid matches the true quotes, though bid/ask bounce adds up to about 0.25 tick of
  spurious convergence in A. Without that bounce, A's result would be even more negative.
* Events overlap within a race, because positions are held 12 h with hourly entries. Race clustering handles that, but
  not a factor shared across races on the same night (see the 2-hour-block test).
* The ladder is one snapshot (21 Oct 22:03) and includes the bot's own resting orders, which we cannot trade against.
  The DMM's refill after a take (34 of 80 refilled within 120 s, per hft_strategies.md) means a second take a few
  seconds later may find a thinner touch.
* B contains our own direction and tilt flow, which pushes toward fair by 0.1-0.3 tick per take (section 4). B's
  convergence is therefore an upper bound for a sleeve that adds more of the same flow.
* Holding a gap of 10+ ticks to settlement is a different trade, covered by favourite_drift.md and the existing sleeve.
  Its expected value is the whole gap if the outside venues are right, which is much larger than the 1-2 ticks a 12 h
  round trip captures. This study only says the gap does not close on SIG within a day; it says nothing about who is
  right at settlement.
