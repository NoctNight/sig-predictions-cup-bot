# How to reach 900k-1M by 4 Nov: every path, priced (2026-10-22, high-upside side session)

Question: we hold ~102k settlement value; third place is +853k. That is 9.4x in 13 days. Data: four leaderboard reads
(22 Oct 19:22 UTC, periods all/7d/1d and 7d by volume), `data/leaderboard_full.json` (11:07 UTC), 1-day and 5-minute candles
since 1 Oct (`data/candles_*.json`), the 32.5k-print tape 15-22 Oct (`tape` table), 21 Oct book snapshots (`data/books.json`),
one smart-score read, three live orderbook reads, and the sibling notes named below. Seven election-night simulations were
run with `python3 -m sigbot.proposals.election_night --simulate`. No orders were placed. Numbers were re-derived by a second
Fable pass (section 7).

## 1. How the leaders got there, and whether it can recur

| fact | number | source |
|---|---|---|
| Top-10 / top-20 / top-100 total P&L | 6.20-6.24M / 7.97M / 10.6M (11:07 and 19:22 UTC reads) | leaderboard all |
| Venue notional 1-4 Oct vs now | 23.8 / 54.5 / 53.9 / 52.7M per day vs 7-25M per day since 10 Oct (median ~12M) | candles_1d |
| Markets that printed both 0.005 and 0.995 on 1 Oct | 23 of 237 (none after 1 Oct); 42 spanned 0.10-0.90 on 1-2 Oct (every market opened at `initialPrice` 0.50) | candles_1d, markets.json |
| Favourites' day-1 VWAP (78 legs now >= 0.80) | 0.70-0.98, median 0.91, mean 0.89 (MN Senate D 0.70, US House D 0.72, IA-03 D 0.74, RI Senate D 0.79) | candles_1d |
| Opening-day drift, marked at today's prices | buyers' gross favourable drift 7.5M over 1-4 Oct (1.36 / 2.37 / 1.67 / 2.07M; sellers' mirror 8.0M, net -0.5M): a ceiling on what fast buyers could take, comparable in size to the top-20's P&L | candles_1d |
| Leaders this week (7d P&L as share of total; 7d ROI vs lifetime) | 0.3-12%; 0.2-3% vs 3-13% | leaderboard 7d |
| Best 7-day gain in the whole field | 66.9k (ready-dawn-8215, 15 trades); only 5 accounts above 20k; 345 accounts traded today, 554 this week | leaderboard 7d/1d |
| Standings | #1 989k, #2 901k, #3 853k, #4 827k, #5 585k, #10 269k; us 631st (P&L), smart score 14 (rank 733 of 868; elite needs 80) | leaderboard, smart-score |

The leaders are the accounts that bought favourites at 0.5-0.9 and sold longshots at 0.1-0.5 during the first four days,
when the designated market maker's fresh ladder was centred on 0.50 for every market. Since then they earn the same 0.2-3% of
volume as everyone else. Their holdings are mostly favourites (win rates 82-93%), so settlement plausibly lifts them further:
the third-place bar on 4 Nov is more like 0.9-1.0M than 853k (unverified: no holdings data; #4 at 827k also rises, and 7-18%
losing legs cut the other way).

Can it recur before 4 Nov? No evidence it can:

* No market has been created since 18 Sep (216 on 24 Aug, 16 on 10 Sep, 5 on 18 Sep); all 237 settle 4 Nov 17:00 UTC; none
  is composite or multi-outcome. The OpenAPI has no public upcoming-market or season endpoint; seeding a ladder is an admin call.
* Zero prints 15c or more from the day's median in 32,546 prints (15-22 Oct); zero daily 30c crashes or spikes since 6 Oct. The
  five-minute "flash" candles in `candles_5m.json` are all dated 1-3 Oct.
* The lottery for a repeat is already sold out: favourite legs carry ~720k shares bid at 0.005 (198k-740k) and a median 0.85M
  asked at 0.995 (40k-1.95M), in two standard size classes (720k and 220k) across markets; 4,917 book levels hold 100k+ shares. A new fat-finger sweep would fill them, not us.
* Chamber markets are within 1-4 points of outside: Senate D 0.675 vs 0.685 (Polymarket/Kalshi), House D 0.885 vs 0.923.
* The 3-leg races (RI Governor, Montana and Nebraska Senate) do have bids summing to 1.05-1.11 (RI Governor 1.075 live, 893
  sets at the touch); the bot already takes them (RI Governor 7,133 sets at 8.5% since 21 Oct). The per-race cap, not the edge,
  limits it: a few hundred SUSQies more, not a path.

## 2. Volatile moves per market (Sizhe's question): how often, where, and can they be predicted

Five-minute closes, 1-21 Oct, 237 markets (`python3 research_notes/high_upside_study.py` reproduces this and section 1 from the
cached data). Bars exist only where trades happened (median ~1,000 bars per market in 21 days, i.e. ~3.5 bar-days), so the rates
below are per 288 traded bars; per calendar day divide by about 6.

| group | markets | moves >= 3 ticks per 288 traded bars (median / mean) |
|---|---|---|
| price < 0.05 or > 0.95 | 11 | 2.0 / 2.3 |
| 0.05-0.15 (and mirror) | 119 | 0.3 / 0.9 |
| 0.15-0.30 | 51 | 0.3 / 0.9 |
| 0.30-0.50 (competitive races) | 56 | 0.0 / 0.4 |

* Venue-wide 503 moves of 3+ ticks in 21 days (24 a day; 8 a day in 14-21 Oct), 65% in legs below 0.15; the competitive races
  barely move intraday (Georgia Governor, the chamber markets, Arizona Governor: zero 3-tick moves in the week to 21 Oct).
* Most active per 288 traded bars: NY-13 D (6.5; 10 moves in 21 days), Wyoming Governor R (6.0), NY-13 R, AZ-01 D, Louisiana
  Senate R, New Jersey Senate D, MI-10 D, Kentucky Senate D, Oregon Governor R, Alabama Governor D (4-5). Daily-range persistence
  week to week: Spearman 0.52, so the volatile markets stay volatile, but 45% of markets had no 5c day in 16 days. Days with a 5c+
  range: RI Senate R 9 of 16, RI Governor I 6, US House D 5, RI Governor R / MA Senate D / TX Senate R / IL Senate R / CO-04 D /
  NY Governor D / US Senate D 4.
* Timing (UTC, 5-min candles): 16-18 run 1.2-2.3x the average rate, 10-12 run 0.1-0.3x. Median retrace at 30 minutes: 0.37.
* Cause and predictability (volatile_moves.md, confirmed here): the moves are the DMM shifting its ladder, two-sided flow, no
  headline behind any of the 40 largest, 5% information, 1% outside-led catch-up (the only predictable class, minutes ahead),
  the rest unpredictable and a passive fade loses 1 tick at +60 min (t -5.4). Tail-leg volatility is relative (3 ticks on a
  0.05 leg is 60%) but absolute edge per share is 1-2 ticks against a DMM that refills in 3.7 s. There is no volatility alpha
  here beyond the lag-follow already in the direction sleeve.

## 3. The paths, priced

C = 102k, T = 955k (third place + 100k). At fair prices no set of simultaneous bets beats P(reach T) = C/T = 0.107; the roll's
fair-view 0.148 exceeds it only through the model's conditional P(B|A) and SIG's lag, not by beating the martingale. Every edge
we have is small.

| path | P(>= 900k by settlement) | expected final (k) | worst case (k) | needs from the bot | source |
|---|---|---|---|---|---|
| (0) current grind: set arbs ~1%/day + MM/tilt/direction | 0 | 115-125 | 95 | nothing | volume_target.md |
| (a1) one longshot at 0.055-0.065 with all cash (AL Gov D, AR Sen D, SD Sen D) | 1.6-1.8% calibrated (5-6% at SIG's own price) | 20 | 5 | one order | punt_screener |
| (a2) correlated roll: VT Gov D (19:00) -> MN-01 D (21:00), sell at 0.9 | 3-6.6% calibrated (ceiling: needs the upset called and sold within ~2 h) | 35-65 | 5 | election-night code, 300-900k through one book | punt_screener |
| (a3) parlay of 2+ simultaneous longshots | < 0.1% | 25 | 5 | - | punt_screener |
| (b) election night, outside-venue leads, 100k cash | ~0 (13% per unit of turnover, cash turns ~3x) | 110-125 (sims +7k to +22k; best race +13k) | 89 (-13k plausible, -90k only if every call reverses) | sleeve built by the election-night session | section 4 |
| (c) compounding the grind at full size / leverage | 0 (cash accounts, `availableAdvance` 0; 3%/day for 13 days = 1.5x) | 130-150 | 90 | size gates | bot_exploits.md |
| (d) structural (new ladder, void, correction, chamber mispricing) | < 1% (no listing in 34 days; floors already queued) | 102 | 102 | a 1-read watcher | section 1 |

Prize view (only the top 3 pay; SUSQies have no other value): a 5% shot at third is ~$125 expected, a 0.5-1% shot at first
~$150-300, so the best lottery (a2) is worth roughly $200-450 in expectation, and it ends below 20k with probability ~94%.
The grind and (b) are worth $0 in prize money; they are worth a finish around rank 150-250 and a rising smart score, which is
what SIG's recruiting export ranks by (`/dmm/icims/export` orders candidates by Smart Score: P&L, ROI, win rate, Sharpe).

## 4. Election night at size (sim detail; 2024 replay, 127 races, 1-minute steps; `--scenario --cap --total`)

| scenario, per-race cap / cash | P&L | turnover | races traded | worst race |
|---|---|---|---|---|
| base, 10k / 100k | +20.3k to +21.5k (two runs today; the module is still being edited) | 197-207k | 22 | -0.6k (VA Senate) |
| base, 30k / 100k | +21.7k | 296k | 13 | -4.3k (VA Senate) |
| base, 100k / 100k | +7.0k | 302k | 9 | -6.9k |
| fast DMM (90% in 1-2 min), 30k or 100k / 100k | +9.0k / +7.9k | 262-291k | 29 | -3.1k |
| DMM pulls quotes 10 min on a 3-pt move, 100k / 100k | +6.3k | 178k | 5 | -5.0k |
| base, 30k / unlimited cash (what the edge is worth with no cash limit) | +500k on 2.7M peak held | 3.9M | 112 | -4.8k |

Reading: the edge is real in the 2024 replay (one election, zero wrong calls) but 13-18% per unit of turnover, and cash turns about three times in a night because called holds
sit until SIG converges. Bigger per-race caps lose: the DMM ladder is 2-5k per tick near the touch and 10k deeper, so a 100k
clip walks 10+ ticks into its own edge. Perfect execution with 100k of cash is +20k to +40k, never +800k.

## 5. Recommendation and timeline

Blunt version: there is no path with better than a ~7% chance of a top-3 finish (the roll's 6.6% is a perfect-execution
ceiling; 3-5% is the working number), and every path with any chance at all ends
with ~5-20k left about 94% of the time. The opening-day windfall that made the leaders is not coming back. Decide on the
objective, not the tactic:

* If the only thing that matters is prize dollars, the correlated roll (a2) on election night is the one ticket worth buying,
  because expected SUSQies are irrelevant and it has 3-6% versus 0%. Do nothing else with the cash that night.
* If finish rank, smart score or the bot itself matter, do not punt. Run the grind to 3 Nov, then the election-night sleeve
  at 10k per race and 90k total: expected +20k, a finish near rank 150-250, worst case about -13k.

| when (ET) | do | notes |
|---|---|---|
| now - 2 Nov | keep set arbs, MM, tilt, direction; let the size gates step as markouts allow | ~+1%/day; nothing else moves the needle |
| now | a 1-read-per-5-min watcher on `/markets` count and status, alerting on a new or voided market | the only way a fresh 0.50 ladder would be caught; expect nothing |
| now | raise the per-race cap for the three 3-leg sell-sets (RI Governor live at 7.5%) | riskless; a few hundred more |
| 3 Nov 17:00 | choose: roll or sleeve (they compete for the same cash) | the roll needs VT Gov D bought before 19:00 |
| 3 Nov 18:00 - 4 Nov 11:30 | election-night mode per election_night.md (both venues >= 0.97, no AK/CA/ME/AZ/NV/OR/WA/UT moves) | dry-run on IN/KY at 18:00 |
| 4 Nov 11:30 | flatten uncalled moves; hold called legs and sets to settlement | manual chamber settlement may lag |

## 6. What I did not find (checked)

Markets settling early (none); hidden leverage (none); 3-leg sets the bot misses (none); chamber mispricing (1-4 pts, within
the direction sleeve); flash crashes since 6 Oct (none); headline-driven moves (none in the 40 largest); a cheaper lottery than
the roll (none: combinations multiply small probabilities); leaders quitting or hedging (their 7d volume is 2-19% of lifetime,
all still active except calm-badger-5206).

## 7. Independent check (Fable)

A Fable subagent re-derived every number from the cached files (no API reads) and re-ran the base sim. Confirmed: leaderboard
totals, daily notional, createdAt/settlement dates, 0.005 floor sizes, 4,917 big levels, chamber prices, the 3-leg sums, the
volatility bucket table, Spearman 0.52, the 45% share, the 5c-day list, the prize arithmetic, and that P(900k) ~ 0 on election
night. Corrected in place: 0.005/0.995 spanning markets 42 -> 23 (all on 1 Oct); day-1 VWAP median 0.89 -> 0.91; the 7.5M
"transfer" relabelled as gross one-sided drift (net -0.5M), a ceiling not a measured capture; ask-floor sizes (two classes, 220k
and 720k); section 2's unit (per 288 traded bars, not per market-day: bars are sparse, so calendar-day rates are ~6x lower and
the venue total is 24 a day, not 330); the timing ratios; the base sim row (+20.3k on the checker's run); "~5%" -> "~7% ceiling";
the martingale bound applies to simultaneous bets; the settlement-uplift bar flagged as unverified.

## Sources

Leaderboard: `GET /tournaments/midterm-elections/leaderboard?period=all|7d|1d&sort=pnl|volume` (22 Oct 19:22 UTC; raw in the
session scratch `lb_periods.json`). Smart score: `GET /tournaments/midterm-elections/me/smart-score`. Books: three live
`/exchanges/{id}/orderbook` reads for RI Governor plus `data/books.json` (21 Oct 22:00 UTC). Sibling notes: election_night.md
(sleeve, punt screener, P = C/T bound), volatile_moves.md, favourite_drift.md (calibration a 0.14, b 1.37), bot_exploits.md (DMM
lag curve, ladder sizes), volume_target.md, past_winners_advice.md (rules, prizes, manual chamber settlement), tournament_research.md.
