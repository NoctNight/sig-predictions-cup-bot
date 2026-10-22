# Large volatile moves on SIG: what they are and how to trade them

2026-10-22, volatile-moves side session. Data: 5-minute candles for all 237 markets (1-21 Oct), the tape and
`venue_quotes` (21-22 Oct), 15 days of Polymarket/Kalshi D-leg history, the 22 Oct realtime capture, the news tables.

Code is in `sigbot/proposals/volatile_moves.py` (paper only; CLI flags `--backtest` and `--calendar`). One tick is 0.005,
and "S" means SUSQies. A second, independent model run (Fable) re-derived the key numbers. It found four bugs in the
first version, and they are fixed. It also refuted two of my first conclusions. All numbers below are after the fixes.

## 1. The moves

Definition: the 5-minute close moves 5+ ticks within 60 minutes, extended while it keeps going (up to 3 h). Both legs of
a race within 30 minutes count once, and closes must sit inside [0.03, 0.97]. Retrace is the share of the move given
back, where 1 means all of it.

| period | moves | per day | note |
|---|---|---|---|
| 1-4 Oct (launch) | 658 | 165 | 1 Oct: summed daily ranges 84 pts vs 4-8 from 6 Oct; 19 of 49 markets printed 0.995/0.005 in 1 Oct 12-13 ET |
| 6-13 Oct | 87 | 11 | quiet |
| 14-16 Oct | 120 | 40 | debate week (GA, ME, FL, MI debated 13-15 Oct), but the movers were MA/WY Senate, MN-05, NH-01 |
| 17-18 Oct | 24 | 12 | weekend |
| 19-21 Oct | 105 | 35 | rising |
| **6-21 Oct** | **336** | **21** | 36 of 10+ ticks (2.2/day); median 6 ticks, 55 min, 62k shares; 62% start in a tail (p <= 0.15 or >= 0.85) |

| ET time of day | 00-07 | 07-12 | 12-17 | 17-24 |
|---|---|---|---|---|
| moves 6-21 Oct | 81 | 70 | 89 | 96 |
Busiest: 11:00, 16:00, 17:00 ET. Only 9% take 15 minutes or less; 32% take over an hour.

**What the DMM does.** These moves are the designated market maker (DMM) shifting its whole ladder, not takers sweeping
it level by level. In the 22 Oct capture, the four largest moves (1.5-2.5 pts) traded 3.6k-55k shares. Meanwhile
75k-230k deep DMM orders were pulled and re-posted 1-2 ticks away. Median volume inside a move is 62k shares, below one
deep DMM level. Where the tape is tagged, flow in the move windows is two-sided: 444k shares with the move, 469k
against. The 1-2 Oct launch is the only true level-by-level sweep, and it has not recurred.

Across all 336 moves, 21% was retraced at 15 min, 25% at 1 h, 21% at 4 h, 36% at 24 h and 38% at 48 h.

## 2. Classification and base rates

| class (6-21 Oct, 336 moves) | share | identified by | retrace 24 h | predictable in advance? |
|---|---|---|---|---|
| (a) information: outside venues moved with or after SIG | 5% | Polymarket/Kalshi +1 pt over the move or +1.5 by 4 h | 19% | no headline matched any |
| (c) catch-up: outside moved first, SIG followed < 1/3 | 1% | outside 1.5+ pts in the prior hour | 13% | yes, minutes ahead |
| (b) liquidity: outside flat, 50%+ retraced in 4 h | 25% | | 71% | only in flight, and only in hindsight |
| outside flat, persisted | 56% | | 25% | no |
| no outside quote (I legs, unmapped races) | 14% | | 22% | no |

Classes (b) and the persistent group cannot be told apart in flight. At detection, 45% of moves widen the gap to
Polymarket and 38% close it, and neither group fades profitably (section 3).

**Headlines.** None of the 40 largest moves since 1 Oct matched a headline (`params/move_explanations.json`). Web
searches found no news for the three biggest since 6 Oct: MA Senate (14 Oct, -22 ticks), WY Senate (14 Oct, -19) and
MS Senate (19 Oct, +18).

The sweeper's 271 classified items (22 Oct, about 15 h) include none above 3 pts at 0.6 confidence. Polymarket Michigan
Senate stayed at 0.695 through the El-Sayed story.

**Outside leads.** Polymarket makes a 2-25 pt move in 30 minutes about 7 times a day. Polymarket moves persist: 90% keep
half or more at 4 h. SIG is slow to follow. On logged quotes (21-22 Oct, 118 moves of 1.5+ pts), SIG had followed:

| after | 10 min | 30 min | 1 h | 2 h | 4 h |
|---|---|---|---|---|---|
| SIG follow | 8% | 15% | 17% | 23% | 42% |

## 3. Backtest

Command: `python3 -m sigbot.proposals.volatile_moves --backtest --md`, covering 6-20 Oct (14.9 days) at 1,000 shares.
Decisions use only bars up to detection. The passive fade fills only if a later bar trades one tick through it within
60 minutes, and the exit is searched from the fill bar.

| trade | n | hit | mean t/share | t-stat | worst | S/day at 1,000 sh |
|---|---|---|---|---|---|---|
| (b) fade gap-widening move, passive; exit at half retrace or 24 h | 83 | 0.64 | +0.25 | 0.5 | -12 t | +7 |
| ... same without its 3 best races (IL, MA, WY Senate) | 66 | 0.59 | -0.38 | -0.7 | -12 t | -8 |
| ... same marked at +24 h | 83 | 0.53 | +1.45 | 2.0 | -11 t | +40 |
| fade gap-closing move | 74 | 0.42 | -0.84 | -1.6 | -18 t | -21 |
| any 5+ tick move, passive, marked at +60 min | 189 | 0.21 | -0.95 | **-5.4** | -10 t | -60 |
| (c) catch-up, 15-day candle replay, take the touch, +4 h | 77 | 0.16 | -0.64 | -3.8 | -4 t | -16 |
| (c) catch-up on logged quotes, take the touch, opposite touch at +4 h | 10 | 0.70 | +5.50 | 3.1 | -1 t | one night only |
| ... control: Polymarket moves SIG had already followed | 3 | 1.00 | +5.00 | | +3 t | |
| (a) headline trigger | 0 | | | | | no qualifying event yet |

How to read it:
* **No class has a robust edge before election night.** The fade's positive 24 h mark comes from one day: 14 Oct
  contributed +51.5 ticks, and 16-20 Oct summed to -50.5.
* The fill rule is generous on size: 9 of 83 fill bars traded under 1,000 shares while the DMM rested 75k+ at that level.
* The quote-replay catch-up is one trending night (22 Oct 00:27-08:48 UTC, nearly all D-leg up moves). The control
  earned as much, so the night's drift paid, not SIG's lag.
* The 15-day candle replay is the base rate. The checker tested my explanation that stale SIG closes and noisy
  Polymarket history caused the flat result, and refuted it: SIG closes are not materially stale (next open within
  0.3-0.6 ticks), and Polymarket moves persist. The base rate shows SIG simply does not converge to Polymarket within
  4 h on average.
* **The robust number is a warning for market making.** A quote resting at the post-move price is down about 1 tick at
  +60 minutes (t -5.4). Retrace-side quotes left in place right after a 5+ tick move are adversely selected for about
  an hour.

## 4. Exposure versus the hard limits

| trade | notional per 1,000 sh | need vs caps (300/leg, 3,000 total) | ask |
|---|---|---|---|
| fade | about 820 (fades buy the favourite side) | 6 concurrent, peak 27.8k over 24 h holds | none: do not trade |
| catch-up | 300-700 | fits at about 600 sh per trade | none: paper only until 30+ signals over 3+ nights clear +2 t |
| election night (section 5) | 10k per race | blocked by every cap | **the one ask** |

## 5. Scheduled events (`--calendar`) and election night

Few catalysts remain. Debates are over: GA, ME, AK, IA, MI, KS and MN held theirs by 21 Oct. No dates were set by 20-22
Oct for the NC, OH, TX, NH or NE Senate debates. Final polls are expected 27 Oct-2 Nov with no announced dates. Expect
about 21 moves a day until 2 Nov.

| when (ET) | event | expected volatility | action |
|---|---|---|---|
| Oct 23-30 | early-vote releases (TX to Oct 30; NC, GA, NV, AZ, FL) | low-medium | none |
| Oct 23, 27 | Trump rallies: Fort Myers FL; Lubbock and Waco TX | low | none |
| Oct 27-Nov 2 (expected) | final Marist, Emerson, Quinnipiac and NYT/Siena polls | medium; 1-3 pts on Polymarket | none |
| Nov 1 02:00 | clocks fall back: ET = UTC-5 | - | time gates shift |
| Nov 3 18:00-23:00 | polls close: IN/KY 18:00, GA/VA/FL 19:00, NC/OH 19:30, most states 20:00, AZ/MI/WI/TX 21:00, IA/NV 22:00 | **very high** | election-night mode |
| Nov 4 01:00 | Alaska closes (ranked choice, unresolved by close) | high | no position |
| Nov 4 12:00 | SIG trading ends; settlement 17:00 UTC | - | flatten uncalled races |

**Election night is the only remaining analogue of the opening sweep.** After an AP call, Kalshi and Polymarket go to
0.97-0.99 within seconds. On normal days the DMM requotes 3-20 minutes after them. Its ladder shows 0.9-1.6M shares
within 10 ticks of today's touch in 25 competitive races (`--calendar`, 21 Oct snapshot). A called leg bought within 5
ticks of today's price returns **3,600-7,400 S per 10k of cash**.

Unknowns:
* whether the DMM pulls its quotes that night (untested);
* rare AP call reversals;
* slow-count states (AK, CA, ME, AZ, NV, OR, WA) that may not settle by Nov 4 noon;
* competition for cash with the planned late longshot punt.

Proposed caps for Sizhe's decision:
* trade only legs where Kalshi and Polymarket are both at 0.97 or better;
* cap each race at 10k notional, and the total at the cash left after the punt;
* exempt called legs from the 5,000 partisan cap;
* place nothing before a call.

## 6. What the main session should wire

1. **No new trading sleeve before Nov 3.** The fade and catch-up have no robust edge, so keep them in paper.
2. **MM check (cheap).** After a 5+ tick move, retrace-side quotes lose about 1 tick in the next hour (t -5.4).
   Compare the vol gate's reducing-side anchor at the pre-move mid with that figure. Pull or widen the retrace side for
   about an hour if the gate's own fill markouts agree. `MoveDetector.on_books(now, feed.books())` flags the moves.
3. **Paper logging.** Each direction poll, call `MoveDetector.on_outside(now, direction.rows)` (add `pm_spread` to the
   rows) and `VolatileMoves.cycle(...)`, then log the paper orders as `purpose='volatile'`. Rerun `--backtest`
   after 3+ nights; the quote replay grows with `venue_quotes`. `on_news(conn)` is ready for the sweeper.
4. **Election-night mode,** pending the caps decision. Build a separate sleeve that polls Kalshi and Polymarket every
   3-5 s from 18:00 ET on Nov 3 (an outside poll takes about 1 s, per direction-handover.md). Dry-run it on any race
   called early, such as Kentucky and Indiana at 18:00 ET.
