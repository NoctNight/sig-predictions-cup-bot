# The 3 Nov punt: what to buy, how big a target, and when (2026-10-23 study)

Read-only research. No orders were placed, the bot was not touched, and `.sig_api_key` was never read or printed (the
venue reads went through `sigbot.config.BotConfig` inside `data/research_punt/sig_read.py`). There were **18 venue
reads** (13 leaderboard pages, 1 tournament, 1 trade-tape sample and 3 probes; log in `data/research_punt/sig_reads/reads.log`).
Rebuild everything with:

    <venv with numpy+matplotlib>/bin/python research_notes/punt_strategy_study.py     (about 5 min; no venue calls)

The figures are `research_notes/figures/punt_ev.png`, `punt_bar.png`, `punt_cash.png`, `punt_calibration.png` and
`punt_night.png`. The numbers are in `data/research_punt/results.json` and the history pull is in `data/research_punt/hist/`.
The roll candidates come from the existing `sigbot/proposals/punt_screener.py`, which was imported read-only and not edited.

## 0. Answer in brief

* **What a punt is worth.** The best punt is worth about **$300-560 in expected prize money** in the base case (range $100-1,400).
  Doing nothing is worth about $0. The best single pre-election longshot is worth $5-35. A ~2.4% chance at 1st place is the
  realistic ceiling.
* **The plan.** Put the cash into a **two-stage roll targeting a 1.4-1.6M payout**, not 1.0M. Stage 1 is a 25-30c leg in a
  race whose polls close at 19:00-20:00 ET, **bought before its polls close**. If it wins, sell it, read the leaderboard, and
  put everything on a later-closing leg that brings the balance to at least the live 1st-place bar.
* **Do not buy a single longshot before 18:00 ET.**

| corrected owner point | what the data say |
|---|---|
| (a) "Punt with enough upside for a top 3; size it to what we need" | Size it to **1st place**, not 3rd. 1st place carries 80% of the prize money ($30k of $37.5k), and the 3rd-place bar is not today's 967k. With carry, settlement, and the chasers ranked 5-30 who will punt too, the simulated 3rd-place bar on 4 Nov has a median of **1.16M** (10-90%: 1.05-1.30M) and 1st has a median of **1.37M** (1.19-1.59M). The prize EV per ticket peaks at a **1.45-1.6M payout**. |
| (a) implicit: one cheap longshot can get us there | **No.** SIG's books cap a ~97.5k single-outcome ticket at **~1.0-1.17M payout**, even when both books of a race are walked: 4c touch prices fill at a 9-10c average. Below ~1.1M the prize value is near zero in most scenarios. |
| (b) "Make different punts from richer rivals; track them and trade against them" | Half right. What matters is the joint distribution of our payout and rivals' final balances. But (i) no positions are visible: the leaderboard gives only P&L, volume, trades, win rate and ROI; the tape carries no user ids; profile endpoints are 404. (ii) Exposure is **not identifiable** from P&L snapshots: 5 intervals, 2 degrees of freedom, and the coefficients flip sign. (iii) Trading against a 600k rival with 97.5k does nothing to their balance. Do three things instead: avoid bets the chasers are likely to share (national-wave or chamber outcomes), prefer state-specific upsets, and **set stage 2's target from the live leaderboard** on the night. |
| (c) implicit: cheap longshots at least win at their price | **They do not.** At 07:00 ET on election day, 2-15c outcomes won **0 of 80 races** (105 legs). About 4.6 wins were expected at the quoted prices, so P(0 wins) = 0.008. Overnight, 2-10c legs won 1 of 265. 85-98c favourites lost **0 of 78** races before election night. |

## 1. Objective

EV = sum_k P(finish k) x prize_k, with **prize_1 = $30,000, prize_2 = $5,000 and prize_3 = $2,500**. The rules say "up to" for
each amount; ties split the pooled prizes; non-US winners have 30% withheld; an ineligible winner is replaced by the next
rank (https://predictionscup.com/rules/, fetched 23 Oct). The score is the verified balance after **all** markets resolve,
so the final balance is what counts, not 4 Nov marks.

SUSQies have no other value, so the objective is risk-seeking. A ticket that loses everything 97% of the time is fine if
it raises P(1st). Because 1st place is 80% of the money, **P(1st) dominates**. A ticket that lands exactly on the 3rd-place
bar is worth at most $2.5k x P(win).

## 2. The bar: where #1-#3 are likely to finish (figure `punt_bar.png`)

**Standings at 23 Oct 21:58 UTC.** Balance = 100k + P&L. 1,764 ranked accounts; 4,910 members.

| rank | account | balance | 7-day P&L (since Sun 00:00 UTC) | per day |
|---|---|---|---|---|
| 1 | kind-brook-6861 | 1,099k | +20.5k | +3.5k |
| 2 | bright-finch-0400 | 1,001k | +3.0k | +0.5k |
| 3 | steady-finch-4087 | 967k | +40.3k | +6.8k |
| 4 | nimble-beacon-4953 | 943k | +55.7k | +9.4k |
| 5-7 | bright-badger-3145 / noble-birch-8618 / merry-coral-9766 | 689k / 651k / 607k | +16.6k / +7.4k / +23.3k | +1-4k |
| 8 | calm-badger-5206 | 532k | -126k | (one concentrated loss) |
| 9-12 | | 432k-334k | +9k to +51k | +1.5-8.6k |
| us | sunny-beacon-7397 | ~100k (rank 709 on 22 Oct) | | |

**Model** (`project_field`, top 60 rivals, 20,000 simulations):

* **Carry to 3 Nov.** Each rival adds its own 7-day rate x 11 days x U(0.3, 1.0). Leaders are still earning 3-9k a day.
* **Holding rivals.** Settlement moves the balance by h x r:
  * h ~ U(0.3, 0.9) is the unknown share of the account in open positions.
  * r = +4% (SIG favourites sit under the outside blend, favourite_drift.md) + 5% x (unknown partisan tilt) x the national
    shock + N(0, 8%) per account (calm-badger lost 26% of its P&L on one position this week).
* **Punting rivals.** Ranks 1-4 punt with probability 5%, ranks 5-30 with q, ranks 31-60 with q/2.
  * A punter aims for a lognormal target around **1.25M** (sd 15%), at least 1.05M and at most 10x its balance.
  * It wins with the same longshot-corrected probability we would get at that price, x 0.97.
  * On a loss it keeps 7.5%.
* **Scenarios** (all plain words in the figures):

| scenario | q (ranks 5-30 who punt) | rival punts correlated with ours | leaders' books when our ticket wins | bans |
|---|---|---|---|---|
| base case | 35% | 0.15 | unchanged | none |
| few rivals punt | 10% | 0 | unchanged | none |
| most rivals punt | 70% (top 4: 10%) | 0.15 | unchanged | none |
| rivals copy our bet | 35% | **0.60** | unchanged | none |
| our win hurts leaders | 35% | 0.15 | **-6% x h** | none |
| our win lifts leaders | 35% | 0.15 | +6% x h | none |
| some leaders banned | 35% | 0.15 | unchanged | each top-8 account 15% |

**Bar distribution (10th / 50th / 90th percentile, k SUSQies):**

| scenario | best rival (the 1st-place bar) | 3rd-best rival (the 3rd-place bar) |
|---|---|---|
| base case | 1,186 / **1,370** / 1,594 | 1,054 / **1,155** / 1,302 |
| few rivals punt | 1,102 / 1,186 / 1,382 | 998 / 1,050 / 1,122 |
| most rivals punt | 1,300 / 1,463 / 1,666 | 1,143 / 1,268 / 1,398 |
| rivals copy our bet | 1,344 / 1,501 / 1,697 | 1,195 / 1,322 / 1,443 |

**Prize given our final balance W.** Each cell is P(1st) / P(top 3) / expected prize in dollars.

| W | base case | few rivals punt | most rivals punt | rivals copy our bet | our win hurts leaders |
|---|---|---|---|---|---|
| 1.0M | 0 / 0.01 / $22 | 0 / 0.11 / $306 | 0 / 0 / $1 | 0 / 0 / $0 | 0 / 0.04 / $121 |
| 1.1M | 0.01 / 0.26 / $1.1k | 0.09 / 0.83 / $5.7k | 0 / 0.04 / $125 | 0 / 0.01 / $38 | 0.03 / 0.37 / $2.1k |
| 1.2M | 0.12 / 0.67 / $5.7k | 0.56 / 0.99 / $18.8k | 0.02 / 0.26 / $1.3k | 0.01 / 0.11 / $0.5k | 0.15 / 0.72 / $6.6k |
| 1.3M | 0.32 / 0.90 / $12.0k | 0.81 / 1.0 / $25.3k | 0.10 / 0.62 / $4.9k | 0.05 / 0.41 / $2.7k | 0.33 / 0.90 / $12.3k |
| 1.5M | 0.78 / 1.0 / $24.4k | 0.96 / 1.0 / $29.0k | 0.60 / 0.99 / $19.8k | 0.50 / 0.97 / $16.9k | 0.78 / 1.0 / $24.4k |

**Reading.**

* The bar is driven by **which chasers punt**, and that is the biggest unknown (q, from 10% to 70%). It is not driven by the
  leaders' books.
* The leaders' book effect matters only near 1.0-1.2M. At the 1.4M+ we need, we must clear the leaders outright, so the
  effect is small (12.0k -> 12.3k at 1.3M).
* Correlation with the chasers' punts is large. "Rivals copy our bet" cuts the prize value at 1.3M from $12.0k to $2.7k.
* Even with no chaser punting, today's #1 plus carry and settlement sits at ~1.15-1.2M.

## 3. What one ticket can pay on SIG (the binding constraint)

The candidate tickets come from `punt_screener.outcomes`. Each ticket is the YES asks plus NO via the other leg's bids in
two-leg races, walked level by level through the 21 Oct books (`data/books.json`). They are priced against the 23 Oct
outside blend (`venue_fair`).

| ticket (97.5k, pre-election) | touch | average fill | payout if it wins | P(win): outside / longshot-corrected | P(1st \| win) base | EV base (range over scenarios) |
|---|---|---|---|---|---|---|
| Idaho Governor D | 0.04 | 0.083 | 1,174k | 3.2% / 0.81% | 0.08 | **$24-35** ($2-127) |
| Wyoming Governor D | 0.045 | 0.087 | 1,120k | 2.7% / 0.64% | 0.02 | $11 ($0-51) |
| South Dakota Governor D | 0.035 | 0.092 | 1,061k | 4.1% / 1.15% | 0.00 | $5 ($0-29) |
| any other single (88 checked) | 0.035-0.30 | | 0.38-1.03M | | 0 | about $0-1 |

**Why singles fail.**

* The ladder gets deep only from about 0.10. For example, RI Senate R holds 432k at 0.135 and 581k at 0.125. So 97.5k at a
  4c touch fills at an average of 8-10c, and **no single ticket pays more than ~1.17M** (`punt_cash.png` panel A).
* More cash helps singles only slowly: the best single pays 1.26-1.38M at 120k and ~1.37M at 150k.
* A split (2 x 48.75k) pays 0.5-0.67M per ticket, so it needs **both** to win. Its P is below 0.03%, and it is dominated.
* The fair-price ceiling (cash / payout x prize given W, panel C of `punt_ev.png`) peaks at **$1.7-1.9k near 1.5M**.
  Every real ticket sits below that ceiling because of the longshot haircut, the book walk and the sale costs.

## 4. Candidates priced (figures `punt_ev.png`, `punt_cash.png`)

**The rolls** are `punt_screener` two-stage rolls with our cash 97.5k:

* Stage 1 is bought at today's book.
* It is sold at 0.9 when called (`roll_recovery`).
* Stage 2 is bought after SIG has followed half of the outside repricing P(B|A) - P(B) (`roll_follow` 0.5).
* The ranking uses the base-case prize value.

| option | payout if it wins | P(win): outside / longshot-corrected | P(1st \| win) base | expected prize, base (range over scenarios) | with a 50% execution haircut |
|---|---|---|---|---|---|
| **R1** roll FL-13 House D (20h ET, ~27c) -> Nebraska Governor D (21h) | 1.46-1.48M | 5.8-6.0% / **2.3-2.4%** | 0.70-0.74 | **$520-565** ($320-700) | ~$260-280 |
| **R2** roll Minnesota Senate R (21h) -> Oregon Governor R (23h) | 1.50-1.57M | 5.3-5.6% / 1.9% | 0.77-0.87 | $456-511 ($314-562) | ~$230-255 |
| **R3** roll FL-16 House D (20h) -> Nebraska Governor D (21h) | 1.61-1.64M | 4.4-4.6% / 1.5-1.6% | 0.92-0.94 | $424-460 ($371-481) | ~$215-230 |
| roll Vermont Governor D (19h, ~26c) -> Nebraska Governor D (21h) | 1.27M | 7.7% / 3.3% | 0.26 | $338 ($57-804) | ~$170 |
| best single (Idaho Governor D, bought before 18:00 ET) | 1.17M | 3.2% / 0.8% | 0.08 | $24-35 ($2-127) | n/a |
| night "lag ticket" (buy at <= 6c while the outside jumped to >= 12c) | ~1.0M | ~12% (7 of 60 such jumps in 2024 won) | 0 | ~$3 base, ~$37 if few rivals punt | n/a |
| hold / election-night sleeve | ~0.1M | | 0 | $0 (a top-3 needs about 1,700 accounts to vanish) | n/a |

The screener's own Monte Carlo (n 10,000) moves these rows by up to ~10% between runs, so each row shows the range over the last two runs. A Vermont Governor D -> Louisiana Senate D roll (1.82-1.84M payout, 1.3-1.6%, $400-467) appears in some runs: same tier.

**Reading.**

* The rolls are worth **10-15x the best single**. They reach 1.3-1.8M, where P(1st | win) is 0.5-1.0, and their stages are
  bought at 20-30c, where past markets were roughly fair (section 6). Single longshots are bought at 2-6c, where they were not.
* Higher payouts trade P(win) against P(1st | win). The EV is flat from about 1.45M to 1.65M ($460-565 base), and the
  1.45-1.65M rolls are the most robust across scenarios (worst case $360-420).
* The "rolls" are all D-upset chains (FL-13 D, NE Gov D) or R-upset chains (MN Sen R, OR Gov R), and their edge comes from
  P(B|A) > P(B) in the factor model plus SIG's lag in repricing B. That is the screener's model, not an observed repricing
  (see election_night.md section 5).
* **The roll's real risk is timing.** In 2024, eventual winners that trailed at 19:00 ET took a median **151 min** to cross 0.5
  (n 21; range 25-668 min). Races at 0.5-0.8 at the close took a median 463 min to reach 0.95 (election_night.md). Stage 1
  has to become sellable (SIG bid >= ~0.85) before stage 2's polls close and its count moves the price. In a slow count,
  stage 2 is just a fair-price bet with no correlated edge, which is the 50% haircut column.
* **How the cash changes it** (`punt_cash.png`). The best roll is worth about $190-210 at 60k, $380-410 at 80k, $520-560 at 97.5k,
  $860 at 120k and $1,460 at 150k (base case). Each extra 10k is worth ~$40-60 of prize EV near 100k. The best single only
  becomes non-trivial above ~120k ($150), because the book cap stops binding.

## 5. Rivals: what the venue exposes, and whether their books can be inferred

| source | exists? | what it gives | read |
|---|---|---|---|
| `GET /tournaments/{slug}/leaderboard?period=1d\|7d\|30d\|all&sort=pnl\|roi\|winRate\|volume\|trades` | yes | rank, profileId, username, pnl, tradesCount, volume, winRate, roi; periods are calendar-aligned (1d = since 00:00 UTC, 7d = since Sunday 00:00 UTC) | 13 pages |
| `GET /leaderboards`, realtime `leaderboard:all` | yes (OpenAPI, realtime docs) | the same fields plus a "standings recomputed" tick, a few seconds after the trades | not read |
| `GET /exchanges/{id}/trades` | yes | id, createdAt, price, size, side (YES/NO space), volume: **no user id and no aggressor** | 1 |
| `GET /profiles/{id}`, `/users/{id}`, `/tournaments/{slug}/members` | **404** | none | 3 |
| `GET /tournaments/{slug}/seasons` | 404 ("only on global tournaments") | none | 1 |
| website profile pages | private or P&L only (past_winners_advice.md) | P&L | not read |
| `/dmm/tournaments/{slug}/trades`, `/orders`, `/inventory`, `/dmm/trader-analytics/users/{profileId}` | in the OpenAPI, but **sponsor/admin endpoints** | per-user trades and positions | **not called** (not public, and outside "public data only") |

**Exposure regression** (`results.json` -> `exposure_regression`):

* Snapshots of P&L: 18 Oct 00:00 (all minus 7d), 22 Oct 07:44, 11:08 and 19:30, 23 Oct 00:00 (all minus 1d), and 23 Oct 21:58 UTC.
* Regressors: a favourite index (the sum of the favourite-side SIG mids, 237 markets), a D index (the sum of D mids minus R
  mids) and time (carry).
* The fit has 5 intervals, 3 unknowns and **2 degrees of freedom**. Coefficient SEs are as large as the estimates, and the
  signs are implausible (kind-brook: -34k +/- 13k per index point on favourites).
* The first interval (4.3 days) holds most of the index movement and the carry, so they are collinear.
* **Conclusion: not identifiable.** A longer daily history would need one read of `period=1d` every day at 23:59 UTC from
  now to 3 Nov (11 reads). That would give 10 more intervals, still a weak fit, and it would identify at most a net partisan
  tilt, not race-level holdings.
* **What is identifiable:** leaders are not hugging one race. Their largest single-interval moves were +-1-3% of the balance
  (kind-brook +13.4k on 23 Oct; steady-finch +27.7k over 18-22 Oct). Calm-badger-5206 (-126k this week) is the exception.

**How correlation changes the best punt** (section 2 table):

* **Against chasers.** A punt that the 5-30 tier is likely to copy (a national wave, chamber control or the most popular
  longshot) loses up to **80%** of its prize value. Prefer state-specific upsets (the R2 and R3 chains are less obvious than
  "D wave") and avoid the chamber markets. Those also settle manually and late (past_winners_advice.md).
* **Against leaders.** "A longshot that hits when leaders' favourite-heavy books lose" helps only below ~1.2M (+$1k at 1.1M)
  and is irrelevant at 1.4M+. It is a tie-breaker, not a selection criterion.
* **The usable form of "track the rivals" is sequential.** `period=1d` P&L during the night shows which chasers' punts have
  landed (big jumps or collapses since 00:00 UTC). Stage 2 is sized after stage 1 is sold, so set stage 2's target to clear
  the **live** 1st-place balance by 10-15%. That costs 1-2 reads per check.

## 6. History: last-day longshots, losing favourites, election-night speed (`punt_calibration.png`, `punt_night.png`)

**Data** (`data/research_punt/hist/`, pulled by a Sonnet subagent from public APIs):

* 714 legs in 181 races: Polymarket 2024 (397 legs, CLOB `prices-history` hourly), Kalshi 2024 (239 legs, hourly
  candlesticks, bid/ask mid) and Polymarket 2022 (78 legs, rebuilt from on-chain AMM trades, daily points only).
* Coverage is the 2022/2024 general elections: Senate, governor, the House races that were listed (22 Polymarket and ~25
  Kalshi House races in 2024, which is all that exist), presidential states and chamber control.
* Excluded:
  * 2025-26 legs. At least one outcome is mislabelled (2025 NJ governor D marked lost), and specials are not comparable.
  * Kalshi hours with a bid/ask spread above 0.15. Thin House books print single 2-7c trades with a 3c bid and an 86c ask.
* Kalshi has no 2022 election markets, so none could be fetched.

**Longshots before election night.** "Expected" uses the race-level prices.

| when | 2-15c legs (races) | won | expected at price | P(0 wins) if prices are fair | P(0 wins) if longshot-corrected | upper 95% on the win rate |
|---|---|---|---|---|---|---|
| Monday 07:00 ET (24 h before) | 113 (85) | **0** | 4.8 | 0.007 | 0.18 | 3.5% per race |
| election day 07:00 ET | 105 (80) | **0** | 4.6 | 0.008 | 0.19 | 3.7% |
| election day 17:00 ET (hourly legs only) | 100 (72) | **0** | 3.5 | 0.03 | 0.31 | 4.1% |

**Favourites at 85-98c:**

* 0 of 80 races lost on Monday and 0 of 78 at 07:00 ET (expected at price: 4.3-4.4).
* 0 of 63 lost at 17:00 ET.
* By bucket, winners at 07:00 ET were 15/15 at 85-90c, 29/29 at 90-95c and 63/63 at 95-98c.

**Reading:**

* Pre-election election markets are compressed: longshots are overpriced and favourites underpriced. This matches
  favourite_drift.md's fit (b = 1.8-2.6) and the literature (Page & Clemen 2013; Bürgi-Deng-Whelan on Kalshi).
* The MAP correction used here (a 0.14, b 1.37) is, if anything, generous to longshots. The data cannot reject it
  (P = 0.18-0.31), but they reject fair pricing (P = 0.007-0.03).
* Upsets happened at 20-35c (PA Senate R at 0.23-0.27; PA-07, IA-01, NE-02, PA-08 R at 0.29-0.34), never at 2-15c.
* Effective n is small (one cycle-wide shock per election), so treat the size as uncertain and the sign as solid.

**Overnight (19:00-07:00 ET, hourly, 2024).** Each cell is wins / legs at each leg's average overnight price.

| price | 2-5c | 5-10c | 10-15c | 15-25c | 25-40c | 40-60c | 60-75c | 75-85c | 85-95c |
|---|---|---|---|---|---|---|---|---|---|
| won / legs | 0/158 | 1/107 | 5/72 | 8/73 | 21/82 | 47/103 | 52/69 | 55/66 | 118/122 |
| won share vs price | 0 vs 0.031 | 0.009 vs 0.067 | 0.069 vs 0.123 | 0.11 vs 0.19 | 0.26 vs 0.33 | 0.46 vs 0.49 | 0.75 vs 0.67 | 0.83 vs 0.80 | 0.97 vs 0.91 |

**Reading:**

* The compression persists overnight but is milder.
* The comebacks are real, but they are the minority:
  * Six eventual winners touched 2-15c overnight (6 of 80 legs that touched): WI Senate D at 0.075 (Polymarket) around
    03:00 ET, MI Senate D, PA Senate R at 0.115, PA-07 R, CA-27 D and CO-08 R.
  * 2-15c **at first touch** wins about at its price (6/80 = 7.5%).
  * Legs that **stay** cheap for hours are worse (1 of 265 at 2-10c).

**Speed (1-minute paths, 2024, `punt_night.png`):**

* Eventual winners below 0.5 at 19:00 ET crossed 0.5 after a median **151 min** (25-668). Their largest 10-minute rise has a
  median of **0.26**.
* Sudden jumps from 6c or less to 12c or more within 10 minutes happened **60 times** overnight; **7** of those legs won (12%).
  Of the 24 that were still at 12c or more an hour later, 6 won (25%).
* SIG's DMM follows a 1.5-point outside move 0% at 1-2 min, 9% at 3 min and 29% at 10 min (bot_exploits.md). So for 1-3
  minutes after such a jump SIG may still sell at 6c or less.
* That is a positive-edge ticket (~12% to win at ~6-10c average fill), but it pays only ~1.0M. Its value is as a **stage 1**,
  not as the whole punt.

## 7. Decision procedure for 3 Nov (the owner places every order; nothing here is automated)

**Inputs to collect at 16:30-17:00 ET.** These cost about 5 reads.

1. **Our cash C.** Do it after the wind-down; every 10k adds ~$50 of prize EV.
2. **The live bar.** Read `leaderboard?period=all` (top 100) and `period=1d`. Recompute the 1st/3rd-place bars with
   `punt_strategy_study.py`, after a fresh leaderboard read and the 3 Nov rate.
3. **Live SIG books and outside prices.** Use a fresh `feed.books()` dump, then run
   `python3 -m sigbot.proposals.punt_screener --report --cash C --target T --books <dump>` for T = 1.3M, 1.5M and 1.8M.
4. **The rival-copy check.** Ask which outcomes are "obvious" punts (chamber control, a national-wave parlay, the 2-3 most
   talked-about upsets). Drop rolls built on them.

**Choose** (in this order):

* **(A) Default: the two-stage roll** with the highest P(win) x prize-given-W, using the screener rows at T = 1.45-1.65M.
  Today these are FL-13 D -> NE Gov D, MN Sen R -> OR Gov R and FL-16 D -> NE Gov D. Prefer a stage 1 that:
  1. is a **20-35c** leg (past markets fair there; the book takes 97.5k with ~8% walk),
  2. closes at **19:00-20:00 ET** in a **fast-counting state** (FL, IN, KY, GA, VT, NH, SC, VA) so it can be sold early,
  3. is not a chamber market or a state that counts slowly (AK, CA, ME, AZ, NV, OR, WA, UT for stage 1).
* **(B) Upgrade (opportunistic, during stage 1 or instead of stage 2).** A leg that was at 6c or less jumps to 12c or more on
  both Polymarket and Kalshi while SIG's ask is still 6c or less. Buy it with the stage cash only if the fill keeps the
  payout on the path to the target. History says 12% win, so it is a stage-1 quality bet, not a finisher.
* **(C) Fallback** (stage 1 lost; or no roll reaches 1.3M in the screener; or the cash is below ~60k): there is no top-3
  path. Keep the balance and run the election-night sleeve.

**Timing:**

* **Stage 1** is bought **before its polls close** (17:00-18:55 ET for a 19:00 close). After the close, SIG's price moves
  with the count and the lag works against a buyer of the trailing side.
* **Stage 1 is sold** when Polymarket and Kalshi both show >= 0.95 (election_night.md rule), at SIG's bid. Accept >= 0.85
  rather than wait: the stage-2 window matters more than the last 10%.
* **Stage 2:**
  1. Read `period=1d` and `all` once (2 reads).
  2. Set T2 = max(1.4M, 1.12 x the best live rival balance, or 1.12 x the best rival plus its open punt if a chaser just
     jumped).
  3. Buy the leg whose walked fill brings our balance to T2 or more at the best corrected P. Prefer 20-40c legs, legs whose
     outside price is above SIG's (lag in our favour), and races still uncalled but counting fast.
* **Never** make the pre-18:00 single longshot: worth $5-35, about one-tenth to one-twentieth of the roll.

**Expected prize EV of the top 3 options (97.5k, base case, honest ranges):**

| option | P(win) | payout | EV base | range (scenarios x execution) |
|---|---|---|---|---|
| R1 roll FL-13 D -> NE Gov D | 2.3-2.4% (1.2-6.0%) | 1.46-1.48M | **$520-565** | $160-1,350 |
| R2 roll MN Sen R -> OR Gov R | 1.9% (0.9-5.6%) | 1.50-1.57M | $456-511 | $160-1,400 |
| R3 roll FL-16 D -> NE Gov D | 1.5-1.6% (0.8-4.6%) | 1.61-1.64M | $424-460 | $185-1,290 |
| (for scale) best single, Idaho Gov D before 18:00 | 0.8% (0.02-3.2%) | 1.17M | $24-35 | $0-140 |

**How to read the ranges:**

* The low end assumes the first sale misses its window half the time and the scenario "most rivals punt".
* The high end assumes Polymarket/Kalshi prices are right (no longshot haircut) and "few rivals punt".
* About 97% of the time any of these ends with <15k. That is the cost of a ~1.5-2% chance at $30k.

## 8. Assumptions and limits (for the double-check)

1. **The bar model is assumption-heavy.**
   * q (the share of chasers who punt), their targets (~1.25M) and their success odds are guesses.
   * The settlement uplift (+4% x h) and the noise (8%) are anchored loosely: favourite_drift.md for the SIG-vs-outside gap,
     and the calm-badger loss.
   * The scenarios bracket it. The ranking of options (roll >> single > night-only) is stable across all seven scenarios;
     the dollar values are not.
2. **Book depth is from 21 Oct** (`data/books.json`). The DMM ladder shape has been stable, but 3 Nov books must be re-walked.
   The ~1.0-1.17M single-ticket cap is the most decision-relevant number and must be re-checked on the day.
3. **Roll numbers come from punt_screener's model.**
   * The factor-model correlation, `roll_recovery` 0.9 and `roll_follow` 0.5 are all model inputs.
   * The sale of 300-400k stage-1 shares into SIG's bid on a called race is unmeasured. Recovery may be below 0.9 if the DMM
     pulls its bids.
   * The 50% execution haircut is a judgement, not a measurement.
4. **The calibration evidence comes from two cycles** with a shared national shock (effective n much smaller than 80 races).
   * The House sample is small: 22 Polymarket and ~25 Kalshi races in 2024.
   * Kalshi hourly prices are bid/ask mids filtered at a 15c spread.
   * Polymarket prices are trade-based and can be stale by an hour (`p12_age_h` in the index).
   * 2022 Polymarket comes from on-chain AMM trades and has only daily points.
5. **Rival exposure is not identifiable** from public data (section 5). The "leaders hurt / lifted" scenarios are symmetric
   +-6% guesses.
6. **Prize amounts are "up to".** A disqualification of a top-8 account helps only via the "bans" scenario (+$0-25 at 1.0M,
   ~+$600 at 1.3M).
7. **Settlement.** The final standings use the balance after all markets resolve. Chamber markets settle manually. A slow
   count (AK/CA/AZ/NV) delays the score but does not change it. The rules allow settled-market corrections.
8. **Not modelled:**
   * Rivals reacting to our punt (we are invisible at rank ~700).
   * The sleeve and the punt competing for cash. They are exclusive: the roll uses all of it.
   * Venue outages or 502 `ORDER_STATUS_UNKNOWN` on election night (changelog 17 Oct). Retry with the same idempotency key.

## Sources

* Venue (18 GETs, 23 Oct 21:58-21:59 UTC):
  * `/tournaments/midterm-elections/leaderboard` period all (offsets 0 and 100), 1d (0-300) and 7d (0-500)
  * `/tournaments/midterm-elections`
  * `/exchanges/1047/trades?limit=5`
  * 404 probes as listed
  * Cached: `data/leaderboard_snapshot.json`, `data/leaderboard_full.json`, `data/leaderboard_all_20261022.json`,
    `data/openapi.json`, `data/realtime_docs.md`, `data/books.json`, `data/candles_1d.json`, the `venue_quotes` and
    `venue_fair` tables.
* Rules and prizes: https://predictionscup.com/rules/
* Polymarket:
  * Gamma `https://gamma-api.polymarket.com/events` and `/public-search`
  * CLOB `https://clob.polymarket.com/prices-history?market=<token>&fidelity=60`
  * 2022 AMM trades from Polygon FPMM logs (favourite_drift_extra.py)
* Kalshi:
  * `https://api.elections.kalshi.com/trade-api/v2/historical/markets`
  * `/historical/markets/{ticker}/candlesticks?period_interval=60` (fetch code: `data/research_punt/hist/fetch_hist.py`)
  * 1-minute 2024 night paths: `data/election_night/paths_2024.csv` (election_night.md)
* Literature:
  * Page & Clemen (2013), EJ 123(568)
  * Snowberg & Wolfers (2010), JPE 118(4)
  * Bürgi, Deng & Whelan, "Makers and Takers" (Kalshi)
  * All as cited in favourite_drift.md.
* Sibling notes: high_upside.md, election_night.md, favourite_drift.md, past_winners_advice.md, bot_exploits.md (DMM lag).
