# How the leaders made +850k to +1M, and whether any of it is still available (24 Oct 2026, leader-study side session)

Question (Sizhe): how did a few accounts make so much with so few trades? Was it early mispricing against fair, e.g. right
after listing, before the market maker or the crowd priced the markets?

* Code: `research_notes/leader_study.py` (`analyse`, `plot`; ~20 s; needs numpy and matplotlib from a scratch virtualenv).
* Data: `data/research_leader/`, holding `lb_periods.json` (3 leaderboard reads), `opening_1m.json` (2 one-minute candle
  reads) and `results.json`.
* Figure: `research_notes/figures/leader_study.png`.
* 5 SIG reads in total, no orders, no bot code or config changed.
* Outside fair: the same leg's Polymarket price, or Kalshi's when Polymarket is stale. It comes from the cached
  `data/research_sig_convergence/raw/` histories (194 of 237 legs, from 1 Oct 00:00 UTC).
* Section 6 is an independent Fable re-derivation. Its corrections are applied throughout.

## Verdict

**Only part of the hypothesis holds.**

* **What holds.** The leaders did profit from prices far from fair in the first days.
* **What does not.** SIG did not list at wrong prices. On all 194 legs with a fair, the 1 Oct opening print was a median
  1.0c from fair. 98% were within 5c and none was 15c or more away.
* **Where the money came from.** Thin-book sweeps lasting minutes in a few busy legs on 1-4 Oct, above all Rhode Island
  Senate R and D on 1 Oct.
  * Rhode Island Senate R (fair 0.9%) traded 664k shares at an average 82.8c over 8 minutes.
  * With 100k of cash, an account could sell 609k of those shares: +448k at the next day's SIG price.
  * Two or three fills like that equal a leader's whole P&L.
* **The leaders' lead is banked.** 93-99.5% of the top 6's gains were in place before 18 Oct, and since then they have
  added 0-60k each.

"Few trades" does not describe #1 or #3. #1 has 4,961 trades on 17.9M of volume; #3 has 29,207 trades. The genuinely
few-trade big winners are further down the board:

* #8 calm-badger: 920 trades, +443k
* #12 tidy-dawn: 309 trades, +234k
* #18 ready-maple: 403 trades, +122k
* #19 quick-ember: 704 trades, +116k

**Is it still available?** The 1 Oct-size version is gone: no leg has had a 30c daily range since 3 Oct. A smaller
version did recur on 22-23 Oct.

* Single market orders of 100k-620k shares swept the market maker's ladder on longshots, alternating buys and sells.
* Example: Wyoming Senate D (fair 1.3%) traded 6.9M shares in 45 minutes on 23 Oct, between 8c and 13c.
* Takers gave away about 0.4M (22 Oct) and 0.8M (23 Oct) of edge against fair.
* The edge is 5-12c and can be turned into cash within minutes.
* Our 5,000 partisan cap allows only a few hundred SUSQies from it. Bigger size is a cap exception for Sizhe.

## Steps

### 1. Leaderboard: when did the leaders make it?

Three reads: all-time; this week (since Sunday 18 Oct 00:00 UTC); today.

| rank | account | gain | trades | volume | gain since 18 Oct | made before 18 Oct |
|---|---|---|---|---|---|---|
| 1 | kind-brook-6861 | +999.9k | 4,961 | 17.9M | +21.2k | 97.9% |
| 2 | bright-finch-0400 | +898.3k | 16,928 | 23.6M | < +4.2k (outside this week's top 100) | > 99.5% |
| 3 | steady-finch-4087 | +865.5k | 29,207 | 29.9M | +38.6k | 95.5% |
| 4 | nimble-beacon-4953 | +847.7k | 5,284 | 13.2M | +60.2k (this week's #1) | 92.9% |
| 5 | bright-badger-3145 | +591.0k | 2,543 | 6.2M | +18.2k | 96.9% |
| 6 | noble-birch-8618 | +554.5k | 40,208 | 18.5M | +11.0k | 98.0% |
| 8 | calm-badger-5206 | +443.3k | 920 | 5.0M | < +4.2k (lost ~126k last week on one position) | - |

* The leaderboard can only be split at 18 Oct. There is no reading between 1 and 18 Oct; steps 2 and 3 date the gains from
  market data.
* Cross-check: the top 6 gained +2k to +33k between the saved 22 Oct 08:43 snapshot and now.
* Lifetime return on volume is 3-6%, against 0.9-3% this week.

### 2. Test 1: were SIG's listing prices wrong?

Data: daily candles for every leg; 5-minute bars from 1 Oct for 74 legs.

* **First print.** On all 194 legs with a fair, the 1 Oct opening print was a median 1.0c from fair. 98% were within 5c
  and none was 15c or more away. Every market has `initialPrice` 0.50, yet the market maker opened at fair.
* **Closes.** On 1 Oct the median leg closed 1.5c from fair and no leg closed 10c or more away.
* **The gap now** is wider than on day 1: about 4c in the daily data, 2c at the mid now. It is the steady
  favourite/longshot offset described in `sig_convergence.md`.
* **Result: the hypothesis is rejected as stated.**

### 3. Test 2: where was the money, then?

Data: 5-minute bars compared with fair; one-minute candles for two legs.

**Swings within minutes.**

* Legs with a daily range of 30c or more: 90 on 1 Oct, 4 on 2 Oct, 1 on 3 Oct, none on any day from 4 to 21 Oct. The
  largest daily range in that later period is 13.5c.
* On 1 Oct, 23 legs printed both 0.5c and 99.5c (`high_upside.md`).

**Rhode Island Senate R, fair 0.9%, 1 Oct.**

* In 8 minutes between 16:13 and 17:44 UTC, 664k shares traded at an average 82.8c.
* Selling all of it took 115k of cash and was worth +483k at the next-day price of 10c.
* With 100k of cash an account could sell 609k shares for +448k.
* The single biggest minute was 16:42: 236k shares at 86c.
* Rhode Island Senate D (fair 99%) traded 383k shares at 35c in the same hour.

**US House D, fair 92.5%, 1-2 Oct.**

* In 9 minutes, 1.5M shares traded at an average 68.9c.
* Buying all of it needed about 1.0M of cash. A 100k account taking the cheapest minutes first (16:20 at 12c, 17:21 at
  38.5c) gets 261k shares, +131k at 88.5c.

**Venue-wide (figure, panel B).** The test: for every 5-minute bar that traded away from fair, value the side trading toward
fair at SIG's price one day later.

* In the covered bars on 1 Oct, that gain was **23% of the SUSQies traded**. These bars are 8.0M of the day's 23.8M and
  include the 10 wildest legs, so the venue-wide figure is lower.
* At daily resolution across all legs the gain was 10% of 1 Oct's notional.
* On 2 Oct it was negative: prices moved further from fair on 3 Oct. On 4 Oct it was large again, +2.1M at daily
  resolution, mostly VA-07 House R: 5.8M shares at 12c against a 3.7c fair.
* On every later day it was between -1% and +5%.
* So the window is 1-4 Oct. 1 Oct was the day one account could capture, because the edge sat in a few minutes of a few
  legs.

**A few-trade 100k account (panel D).**

* Rules: 10 trades; the best 5-minute bar in each 6 hours; at most 25% of that bar; gains re-used.
* Results by window, valued at next-day SIG prices:

  | window | gain |
  |---|---|
  | 1-4 Oct | +159k (+288k valued at fair) |
  | 6-9 Oct | +6k |
  | 14-17 Oct | +33k |
  | 18-21 Oct | +8k |

* The bar is chosen with hindsight, so these numbers are an upper bound for that rule. They are not a floor.
* The minute-level example above (+448k on one leg) shows the opening was big enough for a handful of trades to produce a
  leader-sized P&L.

**Who paid** (full leaderboard, 22 Oct).

* Field total over all 1,758 accounts: +1.86M net (gains 12.74M, losses 10.88M).
* Accounts with 10 or fewer trades (754): gross losses 5.81M, net -5.34M.
* Accounts with 11-100 trades: gross losses 3.64M.
* Accounts with 1,000+ trades (240): gains 9.6M.
* Nobody lost more than 99.5k. 12 accounts are at -90k or worse (-1.15M together), 9 of them with 10 or fewer trades.
* The RI Senate R sweep alone took 550k of buyer cash, i.e. at least six wiped-out starting balances.
* That is either naive accounts sweeping a thin opening book or feeder accounts passing their balance to a main account.
  Public data cannot tell these apart.
* The venue added phone verification on 6 Oct and bulk-banned accounts on 8 Oct. The rules let the sponsor replace an
  ineligible winner (`high_upside.md`).

### 4. Alternative explanations

| alternative | test | result |
|---|---|---|
| A big directional bet came right (a chamber or a race moved) | outside fair on 1 Oct vs 21 Oct, 194 legs | Only 6 legs moved 10c+ outside: Texas Governor, VA-05, Kansas Senate D, SC-01. The volume-weighted outside move is 2.8c, against a 10.8c volume-weighted gap at the day-1 average price. US House D's fair was 0.925 then and ~0.92 now. The best full-balance bet makes about +100k. **Not the source.** Calm-badger's −126k this week shows concentrated bets exist. |
| Leverage from longshots that rose | 69 legs with a day-1 average price of 20c or less; return on collateral | The best day-1 bets came from the longshot premium widening on 3-4 Oct: Illinois Senate R 1.9c -> 9c (4.7x), Colorado Senate R 2.2c -> 9c (4.1x), and Tennessee Senate R sold at 97.7c -> 88c (5.1x). The median premium over fair for legs with fair ≤ 3% went from 2.1c on 1 Oct to 7.1c on 3 Oct. Capacity is what fails: legs at 5c or less traded only 48k of notional on day 1. **Not the source.** |
| Venue quirks: early settlement, new listings, leverage | `markets.json`, the bot's universe log, the account endpoint | None found. All 237 markets settle 4 Nov 17:00 UTC; none has been created since 18 Sep; the bot saw 237 open markets at 13:27 UTC today; `availableAdvance` is 0. One quirk does matter: P&L is marked at SIG prices, so the 1 Oct fills were booked as soon as SIG returned near fair. |

### 5. Is any version available now, before the 3 Nov punt?

| version | now | usable for punt cash? |
|---|---|---|
| A new listing at a fresh book | none: 237 markets, the latest created 18 Sep | Only if a market is listed. The bot's cycle log already prints the market count, so an alert costs no reads. |
| 1 Oct-size swings | No 30c daily range since 3 Oct (data to 21 Oct). No print at or below 1c, or at or above 99c, in 103.6k tape prints since 15 Oct. | No. |
| **Block sweeps through the ladder (22-23 Oct)** | Prints of 100k+ shares: 19 on 22 Oct (6.0M shares) and 27 on 23 Oct (7.1M). Wyoming Senate D (fair 1.3%) on 23 Oct, 05:47-06:10 UTC: 567k at 13c, 500k at 9.5c, 500k at 11.5c, 350k at 8c, 350k at 11c, and so on, alternating sides. MN-05 House D swept to 87-95c against a 96.9% fair. Idaho Governor D and Virginia Senate R were similar. Aggressors gave away ~0.44M (22 Oct) and ~0.84M (23 Oct) against fair; the top 12 leg-hours were worth ~0.66M as round trips. | **Yes, in principle.** The sweeps go through the market maker's quotes (18-25k shares per half-cent), so resting orders 5-12c from fair fill, and the position can be closed minutes later for cash. In practice the 5,000 partisan cap allows a few hundred SUSQies per episode. Related: backlog 0b, "rival liquidation absorb". |
| Steady gaps against fair | 15 legs with the touch 5c or more through fair, e.g. RI Senate D at 89c vs 98.5c, Tennessee Senate D at 11.5c vs 3.8c | No. Only 8-10% of an 8+ tick gap closes in 24 h (`sig_convergence.md`), so the gain arrives at settlement, after the punt. |
| Following big outside moves ahead of the market maker | The market maker follows 0% of a move at 1-2 min and 53% at 60 min (`bot_exploits.md` 2.1) | Small: 100-250 a day, in the direction sleeve, which is not live. |

**How we would catch it, within our rules.**

* Trigger on any of:
  * the open-market count changes;
  * a print of 100k+ shares lands 5c or more from fair (a 15c threshold would have missed 23 Oct, where the gap was 12c);
  * a leg's market-maker spread opens to 10c or more.
* Response: trade toward fair, then close at SIG's price within the hour or by the next day, so the gain is cash before
  3 Nov.
* The 5,000 partisan cap binds. In the 1 Oct RI Senate case the cheap side of both legs pointed the same way (pro-D), so
  there was no race-neutral version. At 5k of net exposure the take is about +20k on a 1 Oct-size event and a few hundred
  on a 23 Oct-size one.
* Anything bigger is a cap exception that Sizhe approves in chat, like the punt.
* Resting orders 5-12c out on longshots would have filled on 22-23 Oct. Orders at 0.5c/99.5c would not: 0.7-0.85M shares
  already queue there per favourite leg.

## Decision

* **No change to the bot from this study.**
* The leaders' windfall came from the thin opening book on 1-4 Oct (mostly 1 Oct). It is banked, it has not recurred at
  that size, and it cannot be recreated before the punt. The punt plan's bar of roughly 0.9-1.0M stands.
* **Possible follow-up, for Sizhe to decide.** Catch the 22-23 Oct-style block sweeps. Two parts:
  * an alert on a 100k-share print 5c+ from fair, built from data the bot already has;
  * either a small cap-neutral resting ladder 5-12c out on longshot legs, or a one-off cap exception.

  It overlaps backlog 0b (rival liquidation absorb). At the current cap it is worth a few hundred SUSQies.

## Caveats

* 5-minute data covers 33-100% of each day's trading, and on 1-4 Oct only 8-19M of 24-55M a day. The 74 covered legs on
  1 Oct are the 10 busiest plus 64 quiet ones, so panel B is a rate over the covered bars, not a venue total. The
  all-leg daily check in step 3 gives the same ordering.
* "Valued at SIG next day" uses the first 5-minute close 24 h later (12-36 h window). On illiquid legs that close is a
  single print.
* Leaderboard P&L includes marks. No per-account trades are public, so linking a leader to a specific fill is an inference
  from timing, size and who lost. It is not an observation.
* The steady-gap counts in step 5 come from live `venue_fair` rows (24 Oct ~12:50 UTC); re-running moves them slightly.
* The figure's panel B and the panel D windows use the study script; the 22-23 Oct sweep numbers come from the tape (step
  5) and are not on the figure, whose candles end on 21 Oct.

## 6. Independent check (Fable, adversarial; own scripts, cached files only)

* Confirmed:
  * the leaderboard shares;
  * the RI Senate R minute numbers;
  * the 30c daily-range counts (90 / 4 / 1 / 0);
  * the 23 legs that printed both 0.5c and 99.5c;
  * no 1c/99c prints since 15 Oct;
  * the directional and venue-quirk rows.
* Corrected in this note:
  * First print: the original note used the first 5-minute close of 74 biased legs (median 1.2c, 3% at 15c+). It is now
    the opening print on all 194 legs (1.0c, 98%, 0%).
  * The RI Senate R window starts at 16:13, not 16:26.
  * The 23% rate is labelled as covered bars only.
  * US House D: about +131k for a 100k account, not +293k.
  * Losses of 10-or-fewer-trade accounts are gross 5.81M, net -5.34M.
  * The sweep needed at least six balances, not five.
  * The longshot row now ranks by leverage and gives capacity as the reason it fails.
  * Panel D is a hindsight upper bound, not a floor.
  * The window is "1-4 Oct" (4 Oct was large at daily resolution).
* The reviewer's main finding: the 22-23 Oct block sweeps, added to the verdict and step 5. "A repeat would need the
  market maker to pull its quotes" was wrong: the sweeps went through its quotes.
* Verdict: the core conclusion survives (listing prices were right; the windfall was a 1-4 Oct thin-book event, banked;
  nothing of that size is available). "No version is available" was too strong.
