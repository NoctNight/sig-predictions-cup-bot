# Directional alpha on SIG: findings (31 Oct overnight, updated as results arrive)

Status: research only, nothing traded. Scripts and logs: `research_notes/dir_alpha/`, `data/research_diralpha/`.
Independent audit: `audit_lag.md`. Other chat: `research_notes/offload_plan.md`. Plots: `figures/dir_alpha_exit.png`, `figures/dir_alpha_size.png`.

## Verdict
There is real directional alpha on SIG, but it is small, and nothing found supports 1 or 2 big trades for a quick exit. About 7,500 cells across about 230 signals were tested. One trade held on 3-20 Oct, days it was not found on. A second held but has faded and mostly repeats the first. A third is large but does not fit the account.

**1. The lag trade: Polymarket and Kalshi moved, SIG has not finished following.** Unseen days: 1.1-1.3c before spread, about +0.4c (slow) and +0.6c (fast) after it, about a third of the backtest. Use 0.5-1c a trade. Trades that start 2c+ on the right side of the market maker's line do better, so tilt size towards them. Worth to 3 Nov about +$0.5-1.5k, at about $2.5-5k a trade.

**2. The line trade: SIG's market maker prices about 0.93 of the way from 50c to outside; legs 3c+ off that line drift back.** Real but thin, and fading: +0.5-0.7c at the touch, +0.2-0.4c at 5,000 shares, about zero on entries since 25 Oct, and in late October mostly the same money as the lag trade. Not worth a sleeve of its own. Worth about +$160-320 on what fits the cap.

**3. The chamber markets: a large mispricing that pays mainly at the result.** SIG House D 86c against 94.5c, SIG Senate D 74c against 71c at 07:45 UTC (75.75c at 06:30, so the Senate leg has already closed about 2c). It scales, but it does not fit the account: the solid leg (House) needs a cap exception, the leg that fits the cap (Senate) is disputed by Silver Bulletin, it pays after the punt, and the MM would work the position away. A quick version earns 1-2c a pair at best, and size walks it away.

Neither reaches a prize. Only the 3 Nov punt can do that.

## The two rules
Fast: both venues moved 1.5c+ in 30 min, SIG copied under half, leg priced 25-75. Strict backtest: 30 trades, 90% moving our way at 4 h, +1.9c at 4 h (SE 0.4),
+2.1c at 8 h, +2.9c at 24 h. Training +1.97c, test +1.79c. House is the core (+2.2c, 23 trades). Governors are weak (+0.25c, 6 trades).
It decays fast: entry 15 min late +1.3c, 30 min late +0.9c, 60 min late +0.5c. So it needs the bot, not a person.

Slow: gap to outside 2c+ above its 48 h norm, both venues moved 1c+ in 24 h, SIG takers not 5,000 shares against, leg priced 15-85. Strict: 56 trades, +1.5c at 24 h,
+2.2c at 36 h, 86% right at 36 h, 7 of 7 days up. House +1.7c, Senate +1.4c, Governor +1.1c.

Selection haircut about 0.4c on both. Independent bets about 30 for the slow rule, fewer for the fast.

## Unseen days (`oos.py`-`oos7.py`, plots `figures/dir_alpha_oos.png`, `dir_alpha_level.png`): the best evidence here
Frozen rules, unchanged, on 3-20 Oct: SIG 5-minute candles, Polymarket 5-minute, Kalshi 1-minute. No taker flow filter (no tape then). No book, so last trades, entered at the next real trade after the signal, less an assumed 0.75c spread.
That method was checked on 21-30 Oct against the true book: it reads about 0.2c low (`oos5.py`). Entering on the signal bar's own trade reads about 0.2c high, so those first figures were dropped.
- Slow, 24 h: 84 trades, 43 races, +1.16c before spread, **+0.41c after (SE 0.22c)**, 69% moved our way, 12 of 18 days up, 1.05c above the rest of the market. Placebo on other legs +0.15c. D-side +0.46c, R-side +0.09c at 36 h.
- Fast, 8 h: 25 trades, 13 races, +1.32c before spread, **+0.57c after (SE 0.48c)**, 76% moved our way.
- By week, slow: -0.6c, +0.6c, +0.7c, then +1.1-1.3c on the true book for 21-30 Oct. Either the edge grew near the election, or it is noise. Use 0.5-1c.
- By office: no real difference (governor +0.46c, House +0.24c, Senate +0.28c), so the backtest's "Governors are weak" did not hold.
- Conviction points, slow: 0 +0.04c (14), 1 +0.25c (37), 2 +0.45c (23), 3+ +1.25c (3). Same order, about a third of the backtest size. Fast too thin to read.
- Own-side price, slow: under 70c +0.43c, 70c+ +0.03c. Filter confirmed, smaller. 2+ points and under 70c: 16 trades, +0.87c.
- Wave filter: 6 trades against a wave, about -0.7c. Right sign, too few.
- Exit rule confirmed. Slow: below water to 8 h, done by about 20 h. Fast: peaks at 8 h, then fades: -0.04c at 30 h, -1.08c at 48 h, so **sell fast trades by about 8 h**, not 24 h. Take-profit at +2.5c adds 0.3c on slow, +3.5c adds 0.1c, neither helps fast. Up 2.5c+ at 12 h: next 24 h -1.4c. Down 1c+ at 12 h: next 24 h +0.4c. Flat: +0.6c. Stop at -3c about neutral, -1.5c costs 0.1-0.2c.

**Starting point against the line (`oos9.py`, tested in `line_review.md`).** Signed distance: the leg is cheap against the line and the signal says buy, or rich and it says sell. Nothing fitted.
| | 2c+ on the right side | everything else |
|---|---|---|
| Slow, 3-20 Oct | +0.87c, 70% won (40) | about 0c (44) |
| Fast, 3-20 Oct | +1.37c, 85% won (13) | +0.07c (14) |
| Slow, 21-30 Oct strict | +1.71c (28) | +1.27c (28) |
| Fast, 21-30 Oct strict | +2.41c, 82% won (17) | +1.19c (13) |
The right-side trades hold up (two-way t 2.6-4.4). The filter does not: comparing within a day, the premium is +0.51c (SE 0.40c, t 1.3), and it depends on the 2c threshold. Use it as a size tilt: about 0.8-1c against 0.4-0.5c. My first reading, that nearly all the edge sits here, was wrong.

**The line trade (`oos8.py`, `oos10.py`, independent review `line_review.md`).** k is fitted across legs from past data only (0.87-0.97, about 0.92 now). Rule: 3c+ from the line, any price 10-90c, chamber out, one bet per leg per 72 h. Signal on the mid, not the last trade.
The review found a masking error in `oos8.py` that overstated the first figures (+1.33c on 30). Fair versions:
| set | trades | net at the touch | two-way t |
|---|---|---|---|
| 3-20 Oct, stated rule, last trades | 280 | +0.65c | 2.8 |
| 21-30 Oct, mid signal, true book | 144 | +1.08c | 2.4 |
For it: no look-ahead, last-print noise dilutes rather than creates it, 72-96% of the closure is SIG moving rather than outside, positive at every threshold, hold and fit tried, time-shift permutation p about 0.005, works buying and selling.
Against it, and these decide the verdict:
- Entries since 25 Oct made about zero on the true book. The first 3 days of each panel earned +1.4-2.1c and the rest -0.1c to +0.4c. A snapshot test says this is not a start-of-sample artefact, so the edge has shrunk.
- In 21-30 Oct, line trades with no lag signal within 72 h either side made +0.12c (108). Most of the late profit is the lag trade again. Do not add the two.
- Size: 5,000 shares costs 0.25-0.7c more than the touch.
- 70% of today's list was already 3c+ a day ago, the weaker group. Distances that opened in the last day do better (+1.38c against +0.53c early), which is close to the slow lag rule anyway.
- Wind-down would sell these positions, since only listed sleeves are exempt.
Estimate: +0.5-0.7c at the touch, +0.2-0.4c at 5,000 shares, +0.1-0.6c at the touch of which is new money. 16 bets fit the cap: about +$160-320. If used at all, run it through catch-up, not as a new sleeve.
Related: buying a side 6c+ under outside gave +0.62c (131) and +1.90c on the true book (36), mostly the same trades.
Not usable: gaps opened by SIG moving alone closed on 3-20 Oct (+1.4c at 48 h, 124 trades) and widened on 21-30 Oct (-1.1c, 32 trades).
Live list frozen at 05:23 UTC for scoring from 3 Nov: `data/research_diralpha/oos/line_now.pkl`. It wrongly includes 4 chamber sides; leave those out (29 sides, 24 races).

## Forward test so far (`paper.py`, read-only, 31 Oct 01:16-07:45 UTC, 6.5 hours)
Rules frozen before the data existed, entered at the touch, marked at the touch (so every open trade already carries its spread).
- Fast: 2 entries, mean +0.75c. FL-09 R NO +2.5c at 3 h, Michigan Senate D YES -1.0c at 8 minutes old.
- Slow: 14 entries, mean +0.29c, 36% ahead. By age: under 2 h -0.5c (1), 2-4 h +1.63c (4), 4-8 h -0.22c (9). Leaving out the 6 that were already live when it started: +0.69c (8).
- At 08:00 UTC: all 16 mean +0.22c. Fast +1.00c (2). Slow +0.11c (14), 29% ahead, or +0.63c (8) leaving out the 6 already live at the start. Michigan Senate was the newest fast signal: outside jumped 3c at 07:35 and SIG fell 1c.
- This does not confirm or reject the rule. No slow trade has reached the 8 h point where the backtest and unseen days start to pay, and 16 trades have a standard error of about 0.5c. It is consistent with both rules, no more.
- Extended, still read-only, to 1 Nov 08:00 UTC so the slow trades reach 24 h. Score it with `live_now.py`. It stops by itself.

## Evidence for and against
For: rebuilt from raw tables by the auditor (same trades); no look-ahead, no survivorship, prices real; other-leg placebo -0.3c against +2.0c (p 0.003);
time-shift placebo max +0.46c; walk-forward model +1.56c to +2.07c; walk-forward parameters +2.12c; 10 real live catch-up trades +0.70c, 60% winners.
Against: 9 days, one pre-election week, D drift of 5c on House seats (D-side trades +1.8c, R-side +0.9c); only about 0.9c is pure lag, the rest needs the outside to
keep going; the live number is half the backtest; strict train and test no longer match as well (1.9c and 1.3c on the slow rule).

## Exit rule
- Selling a winner: past +3.5c expected further gain is below zero, so sell. Under that, hold to 8 h (fast) or about 20 h (slow): nothing else adds.
- Selling a loser: rarely. A trade flat or down at 12 h still expects about +2c more (slow). Use a -3c stop for the disaster only: costs 0.1-0.2c. -1.5c costs 0.6c on the slow rule.
- Half the move comes in 3.8 h (fast), 12 h (slow). Three quarters in 8 h, 17.5 h. All of it in 15 h, 21 h. Flat after that.
- Resting exits at +2c to +5c: worse than crossing, because they cap the winners. Keep crossing.
- Swap rule: at 24 h held, remaining edge is about 1c (slow), so a fresh signal is worth more than an old position. Rotate when capital is tight.

## Size
Figures in this section use the backtest edge. On the unseen-day edge the best size is about $2-2.5k a trade, `figures/dir_alpha_size.png`.
Static ladder walking overstates cost; other traders' runs understate it (they pick their moment). Our own orders are the honest guide: 2-5k shares about 0, 10k shares about 0.25c each way,
37k-share burst about 0.75c. Used here: 0.02c per 1,000 shares each way. Edge covers cost to about 15k shares a trade. Best is $2.5-5k a trade.
Book to 3 Nov after haircut: $2.5k a trade +$1.8k, $5k +$2.1k, $10k -$1.4k, $40k heavily negative. Costs doubled: $2.5k still +$1.1k, $5k -$0.7k.
The 5,000 partisan cap costs about 20% at $5k a trade, so no exemption is needed. It would be needed for anything bigger.

## The live catch-up sleeve is already this signal
`sigbot/proposals/outside_sweep.py`, live since 24 Oct at $300 a trade. Replay of its exact code over all raw polls (27 signals, 23 entered):
- Live exit (70% converged, 12 h, outside reverts, venues disagree): +1.02c, 74% winners. Real fills so far: +0.70c on 10 trades.
- Drop the 70% exit, keep the revert exit, take profit at +3.5c, 24 h limit: +2.05c, 73% winners, 12.7 h held, worst -1.5c.
Why it is small today: $300 and 2,000 shares a trade, 4 open positions, 80% of touch, D leg only, and D-side adds blocked once net D is above 4,800 (it is about 4,900).
Biggest size signal is touch-limited: 0.8 of the touch at about 1,500 shares means one clip per order, so size needs repeat clips.

## Small upgrades tested
- Cheaper leg on entry: no gain once the exit stays on the same leg (1.49c stays 1.49c). A cross-leg exit makes a set and locks cash. Stay on the signalled leg.
- Signal size: the smallest third of fast triggers (under 2.3c) makes +0.45c, the rest +2 to +3c. Use a 2.3c minimum for full size.
- Size in shares, not dollars: about 10-15k shares a trade is best, to 3 Nov about +$3.3-3.7k after haircut with no capital limit.

## Conviction count (same order on unseen days, about a third of the size)
One point each: no public polls, toss-up rating, leg quote unchanged 2 h+, signal in the upper two thirds. No weights fitted. Strict trades:
Slow: 0-1 points +0.5c (29 trades), 2 points +2.2c (21), 3+ points +3.5c (6). Fast: 0-1 points +0.7c (18), 2+ points +3.7c (12, all winners). Train and test agree in every row.
Trading only 2+ points, 4.6 signals a day: +2.87c. It was found after looking, so a 0.8c haircut is used. At 20-30k shares a trade, about +$4.5-4.7k to 3 Nov before limits, but only +$2.3k with $60k of capital and +$0.4-0.7k inside the 5,000 cap (70-80% of these signals are D-side).
On unseen days the same count gave +0.45c at 2 points, so the sizes above are too big. Keep it as a size tilt, $2.5-5k rising to about $5-7k on 2+ points.

## The chamber markets (31 Oct, `d29`-`d43`, independent review `chamber_review.md`, plots `figures/dir_alpha_chamber.png`, `dir_alpha_chamber_size.png`)
Prices about 04:50 UTC 31 Oct: House D SIG 85.5/86, Polymarket 93/94, Kalshi direct 93.5/93.6. Senate D SIG 75/75.5, Polymarket 70/71, Kalshi direct 70/71. Long House D plus short Senate D pays "D House, R Senate" minus "R House, D Senate": about 23c on real-money prices, 11c on SIG. Kalshi's combo leg shows 26c but runs about 2c rich.
Same event on all three venues (`contract_rules.md`, review). The offer is real: 570-640k shares at 86c on four separate reads, not ours, 54,220 lifted at 04:45 UTC.
Against every other leg (compression about 0.93): House D sits 5c below the line, Senate D 7c above it, and Senate D is the only leg priced 62-78c more than 1.2c above outside. SIG's own state legs imply Senate D of about 65c (review), so SIG's 75c does not even match SIG.

**Corrections from the review.** (1) The gap is not new: House gaps of 9-10c on 3, 7 and 18 Oct, mean 5c. Chamber legs had been left out of the search, and my data started 21 Oct. (2) No single seller story can be shown. The 30 Oct 14:16 burst was a 491k buy, and the tape misses 757k House D shares from the outage (official low 72.5c). One actor long R House is the best fit, but that is inference. (3) Senate is disputed: Silver Bulletin 76.4% (22-23 Oct) sits next to SIG's price, Dave Manuel 70%. House is solid: Silver 95%, Dave Manuel 94%.

**Does it close before results?** Conflicting evidence, and neither is large enough to trade on.
- These legs, October: pair gaps of 7c+ narrowed about 5.9c in 87 h (median 4c), 89% of hours narrowed, 3 episodes (1.7c, 16.7c, 5.1c). Fitted: 13c now to about 8.7c by 3 Nov, mean 5c. House alone 7.75c to about 5.2c.
- All legs, 21-30 Oct: 49 cases of SIG moving 4c+ alone: about 14% closed in 48 h, net -0.8c. Gaps already in place: -0.5c. Offers of 50k+: no rebound. Following the move also loses.
- A quick round trip costs about 1.1-1.4c on House D and 1.3-3.2c on Senate D, 2.4-3.4c on a pair. So about 1-2c a pair at best.

**Held to the result**, chances from Polymarket, book of 03:56 UTC:
| trade | cash | expected | most likely result | cap |
|---|---|---|---|---|
| Long House D, 40k shares | 34.4k | +2.9k (8.5%) | 93% win 5.6k, 7% lose 34.4k | needs an exception |
| Short Senate D, 40k shares | 10.6k | +1.2k (11.6%); -1.1k at Silver's number | 70% lose 10.6k, 30% win 29.4k | fits, frees D room |
| Both, 20k pairs | 22.4k | +2.2k (10%) | 76% lose 2.4k, 23.5% win 17.6k, 0.6% lose 22.4k | needs an exception (cap counts +61c a pair, room allows about 500 pairs) |

**What gets in the way.** Net D is about +4.7k, so D-side room is about 300. The cap measure misreads a pair: on a national swing it is net short D. Changing that is a hard-limit decision. Any live trade needs an MM exclusion on these legs, because the MM quotes off SIG's price and would sell them back (from code, not run). Chamber legs settle by hand once control is called and may lock past 4 Nov. SIG can cancel trades if it finds collusion. The wind-down would not sell them while the gap stands.
Also: the bot's derived Kalshi chamber price reads about 2c off the direct market on Senate; `BACKLOG.md` already suggests the direct CONTROLS/CONTROLH markets.

**Plan for approval:** `chamber_plan.md` (31 Oct, plot `figures/dir_alpha_chamber_plan.png`).

**Not new: the favourite discount at large.** 57 legs and sides offer 5%+ at the touch, about $6m of capacity, both parties, all paying only at the result. Capital, not liquidity, is the limit. This is why it does not help cash at 18:00 ET.
If the punt goes on after about 21:00 ET, favourites in states closing by 20:00 ET could be carried to their call: on a $40k balanced book +$1.6-2.4k if SIG reprices after the call, nothing if the market maker keeps its discount until settlement. No evidence either way until 3 Nov.

**For the punt (`d42`).** The market maker's compression makes long shots dear. Expected value per dollar staked, at SIG's price against the outside chance:
| SIG price | 3-5c | 5-8c | 8-12c | 12-20c | 20-30c | 30-40c | 40-50c |
|---|---|---|---|---|---|---|---|
| value per $1 | 0.45 | 0.50 | 0.60 | 0.71 | 0.94 | 0.95 | 0.99 |
So a single 10x leg starts about 40% down, and two 3x legs in a row (the same 9x) about 10% down. This supports the two-stage roll in `punt_strategy.md` over a one-shot long shot.
Of legs with size under 40c, Senate D NO at 25c has the best value (1.21), then Senate R YES at 28c (1.09). House R YES at 14.5c is about the worst large leg (0.44). `punt_strategy.md` avoids chamber markets because rivals may copy them and they settle by hand; that reason still stands.

## Findings from the second idea round (`hypotheses_round2.md`, about 40 more cuts, small n)
- Wind-down would sell positions of any new sleeve, but catch-up legs are exempt (read in `wind_down_exempt`). Build on catch-up.
- The lag is shortening: SIG had copied 36% by trigger on 22-26 Oct, 48% on 27-30 Oct. Net at 4 h has held so far.
- Fast earns 11c per capital-day, slow 1.5c, so fast takes priority when capital is short.
- Stale legs (no quote change 2 h+) +2.62c against +1.04c on the slow rule.
- Real taker flow on Kalshi or Polymarket agreeing: +2.35c to +2.61c against +0.75c to +0.96c. Thin.
- FEC outside spending shifts: +0.70c on mid in 24 h, the one new outside source, too small alone. Overlay: agree +2.14c, disagree +0.81c.
- Trend risk: the fast rule holds against the national drift (+1.83c), the slow rule does not (+0.94c, -0.05c when the national move hurts during the hold). Stop new slow entries if the 24 h national move turns R by 1c+.
- Weekend: SIG copied 61% in 4 h against 76% on weekdays in the one weekend of the backtest, but three more weekends on unseen days show no penalty: slow +1.2c before spread by 8 h against +0.6c on weekdays (19 trades, `oos11.py`). No need to change anything for 31 Oct-1 Nov.
- Dead: DJT stock, PredictIt, posting passively first, legging sets, pulling MM quotes after a move. No break from final polls or the 1 Nov clock change.
- Check: `stop_quoting_at` 12:00Z is 07:00 ET on 3 Nov, earlier than the wind-down's 12:00 ET MM flat time.

## Paying for it (`research_notes/offload_plan.md`, the other chat)
Raising 20k in 5 minutes costs about 120, 40k about 690, 60k about 1,560. If those holdings would be sold by 18:00 ET 3 Nov anyway the true cost is about zero.
Only 2.9k of cash is spendable today (15k is the shock reserve). So a $2.5-5k book needs about 20k raised, for about 120. No executor exists for the offload yet.
The partisan cap limits a single trade to 9.8k Republican-side or 0.2k Democrat-side at net D +4.8k. D-side room can be bought by selling D legs: 5k costs about 142.

## By market type
Unseen days show no real difference by office, so treat what follows as weak. House seats: strongest and slowest to catch up, especially seats with no public polls (+3.3c, 24 trades) and toss-up ratings (+2.65c).
Senate: fast copy, smaller edge. Governor: weakest at short holds. Chamber control: SIG barely follows the outside at all (beta about 0), no signal in the 4 legs tested.

## Election night (`election_night_tests.md`, 2022 and 2024 outside prices, two nights only)
None of the four ideas should be traded on 3 Nov. Fading the first move after a state closes loses (it continues, -1.3 pts at +3 h across 36 state-years). Fading exit-poll rumours: the two nights disagree.
Read-through from early closers to late legs, and first-called seats, are one unproven bet: right both nights, but driven by a handful of legs, and the market maker's lag adds nothing.
Useful preparation: a read-only record from 18:00 ET on 3 Nov. The election sleeve stays off.

## Round 3 (`hypotheses_round3.md`, 40+ more cuts)
Two filters survive, both added after looking: skip slow trades where our side costs 70c+ (13 trades -0.1c, rest +2.0c, also true on 170 loose events it was not found on), and skip signals against a wave of 5+ legs (-0.6c to -1.4c).
Together on the slow rule: 31 trades, +2.0c, train 2.3c, test 1.9c.
Timing our offload with the signal: about $100, our holdings sit in legs that rarely signal. Basket of same-way legs: +1.4c, only +0.8c above the rest, placebo +0.5c. Long-short pairs: +1.37c, fits the cap, too few (18 in 9 days).
Delayed entry: waiting costs edge (2.24c to 1.38c at 6 h).

## Dead
Momentum and reversal; taker flow alone; big prints; market maker inventory; national factor lead-lag; same-state peers; chamber against seats; hour of day; favourite drift; round numbers;
quote steps; multi-leg sweep fades (the 30 Oct kind); single-leg sweeps; repeated clips; directional pairs; skilled trader signatures; news labels; topical news; polls; our own model;
regional factors; chaser bid; market maker reference model; stale clock; book depth imbalance; heavy-level centre; touch depletion; attention premium;
Wikipedia rating edits (210 events: -0.41c at 12 h, -0.03c at 24 h, `wiki_ratings.md`);
exit when the gap closes (0.5-1.0c against 1.9c on the clock); SIG-alone moves, fade or follow; flash catching with far resting orders (2-4c away loses, 6c away +2.4c but 9 fills in 9 days); walls at the touch; race sums above 1; overshoot fades; outside depth; which venue led; open seat, partisan lean, ratings disagreement; trigger hour; mirror leg; tape age; past leg slowness.
