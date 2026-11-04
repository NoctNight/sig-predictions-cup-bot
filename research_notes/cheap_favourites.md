# Cheap safe favourites on SIG: buy them in fast-count states and hold to the call (24 Oct 2026)

Read-only study. No orders were placed and the bot was not touched. `.sig_api_key` was never read or printed. There was
**1 SIG read**: `GET /portfolio/positions` through `BotConfig`, saved to `data/research_cheapfav/positions.json` and
logged in `reads.log`. Rebuild:

    <venv with numpy+matplotlib>/bin/python research_notes/cheap_favourites_study.py      (about 70 s, no venue calls)

* Figure: `research_notes/figures/cheap_favourites.png`.
* Numbers: `data/research_cheapfav/results.json`. Console output: `run_output.txt`.
* The basket reads the newest `venue_fair` poll. A re-run therefore moves the figures by a few percent: three runs within
  10 minutes gave EV +828 / +835 / +840 at 15k.

## Verdict: GO, at 15k, inside the partisan cap, with the sleeve off until Sizhe enables it

| 15k basket, 32 legs, edge/risk weights, at most 2,000 per leg | outside prices right | calibrated (2022/2024 fit) | stress: polling miss 1.5x |
|---|---|---|---|
| expected profit | **+840** (+5.6%) | +1,166 | +513 |
| P(loss) | 10.6% | 2.5% | 21% |
| 5th percentile | -664 | +382 | -1,791 |
| average of the worst 5% | -1,356 | -239 | -2,790 |
| 1st percentile / worst of 200k nights | -1,820 / -6,799 | -679 / -4,537 | -3,440 / -7,502 |
| P(at least one favourite loses) | 77% | 49% | 82% |

* The profit comes back on election night. Every leg is in a state whose last polls close by 21:00 ET. Safe seats are
  called at the close (election_night.md), and SIG settles 0-4 h after the call.
* 10k and 20k scale almost linearly:
  * 10k: +559, P(loss) 10.6%, 5th percentile -438.
  * 20k: +1,115, P(loss) 11.7%, 5th percentile -830.
* Profit per unit of bad-case loss is flat across budgets: 0.60 at 10k, 0.62 at 15k, 0.62 at 20k. So **cash, not the
  ratio, sets the size.** Free cash is ~15k today (cycle 3888: cash 15.0k, positions 87.0k).
* Why GO:
  * The edge is real against the outside venues and does not depend on the favourite-longshot bias. The bias only adds to it.
  * A ~5.6% return over 10 days beats arbs (+2-3% to settlement).
  * The basket is roughly party-balanced: 6.8k on D favourites, 8.2k on R. A national polling miss therefore hits one half
    and helps the other. In the worst 5% of national swings for the basket, the mean P&L is only -215.

**The cost to weigh (Sizhe's call, already accepted in the plan):** the 15k is locked through the punt's stage 1, which is
bought before 19:00-20:00 ET. It returns, with profit, only for stage 2. If the punt is sized from all free cash at
stage 1, this sleeve shrinks stage 1 by 15k.

## A1. Are outside prices right for favourites?

* Sample: one favourite per race per horizon, 2022 and 2024, Senate, governor and House. Presidential, chamber and other
  markets are excluded.
* Sources: Polymarket daily prices (`data/polymarket_history`), Kalshi daily prices (2024 markets opened 7-31 Oct, so the
  sample is thin), and Kalshi hourly prices 24 h out (`data/research_punt/hist`).
* P(>=) is the exact Poisson-binomial chance of at least that many wins if the prices were right. The "with cycle swing"
  column lets each cycle share a latent swing with correlation 0.3, signed by the favourite's party.

| source | days out | n | won | expected | win rate vs price | P(>=) | with cycle swing |
|---|---|---|---|---|---|---|---|
| Polymarket | 13 | 52 | 52 | 48.2 | 1.000 vs 0.927 | 0.017 | 0.029 |
| Polymarket | 7 | 55 | 55 | 51.3 | 1.000 vs 0.932 | 0.019 | 0.033 |
| Polymarket | 3 | 54 | 54 | 50.3 | 1.000 vs 0.931 | 0.019 | 0.032 |
| Polymarket | 1 | 51 | 51 | 47.8 | 1.000 vs 0.937 | 0.034 | 0.051 |
| Kalshi daily | 3 / 1 | 10 / 9 | 10 / 9 | 9.3 / 8.4 | 1.000 vs 0.93 | 0.47 / 0.52 | |
| Kalshi hourly | 1 | 16 | 15 | 14.6 | 0.938 vs 0.910 | 0.57 | |
| all, each race once (Polymarket, mean 1-14 d price) | | 53 | 53 | 49.4 | | 0.022 | 0.040 |

* **By band (Polymarket, 13 days out).** The full per-band table is in `results.json`. No band rejects on its own.
  * 0.80-0.85: 9/9 won, 7.3 expected.
  * 0.85-0.90: 6/6 won, 5.3 expected.
  * 0.90-0.95: 9/9 won, 8.3 expected.
  * 0.95-0.99: 28/28 won, 27.3 expected.
* **Reading.** The outside price is **too low for favourites**, not too high: ~4 more wins than priced in ~52 races, at
  p 0.02-0.05. This matches favourite_drift.md and punt_strategy.md (85-98c favourites lost 0 of 78 races). The only
  favourite in the band that lost was on Kalshi, priced 0.82 at 24 h out.
* **Calibration map.** A MAP logistic fit with the favourite_drift priors, on 89-90 Polymarket favourites at 0.50-0.995
  (7 and 13 days out), gives a = +0.15 and b = 1.27. Prices map as 0.85 -> 0.914, 0.90 -> 0.950, 0.95 -> 0.980,
  0.98 -> 0.994.

**EV per 1,000 spent on SIG at g below the outside fair, held to settlement:**

| fair | g = 2c | 5c | 10c |
|---|---|---|---|
| 0.85 | +24 / +101 | +63 / +142 | +133 / +218 |
| 0.90 | +23 / +80 | +59 / +118 | +125 / +188 |
| 0.95 | +22 / +54 | +56 / +89 | +118 / +153 |
| 0.98 | +21 / +35 | +54 / +69 | +114 / +130 |

The first figure in each cell assumes the outside price is right; the second uses the calibrated probability. The
calibration only adds to the edge, so the trade does not depend on it. An upset still loses the whole stake on that leg.

## A2. Today's basket (venue_fair 24 Oct 00:05 UTC, books 23 Oct 23:17 UTC)

**Funnel.** 101 leg-sides have an edge of at least 2c (buy YES: fair - ask; buy NO: bid - fair).
1. 78 have the same contract terms and no late-result flag.
2. 73 are in fast-count states: last polls close by 21:00 ET, and the state is not AK, AZ, CA, ME, NV, OR, UT, WA or GA.
3. 56 are favourites (P(win) >= 0.80).
4. 55 remain after dropping races with an I leg and the punt screener's candidate races (FL-13, FL-16, VT Gov, MN Sen,
   NE Gov, OR Gov, LA Sen, ID Gov, WY Gov, SD Gov).
5. 52 remain after dropping races where we hold more than 4,000 shares on a leg.
6. These collapse to **34 race outcomes**: D-YES and R-NO are one bet, so the better route is kept and both routes' depth
   is pooled.

The figure's panel B lists every leg. The top ten by edge at the touch:

| leg (route) | close ET | fair | SIG | edge | depth at >= 2c edge |
|---|---|---|---|---|---|
| Rhode Island Senate D (R NO) | 20:00 | 0.984 | 0.875 | 10.9c | 5.1M |
| NH-01 D (YES) | 20:00 | 0.961 | 0.890 | 7.1c | 0.95M |
| Tennessee Senate R (D NO) | 20:00 | 0.961 | 0.895 | 6.6c | 0.92M |
| Mississippi Senate R (D NO) | 20:00 | 0.876 | 0.815 | 6.1c | 1.07M |
| Delaware Senate D (R NO) | 20:00 | 0.974 | 0.915 | 5.9c | 0.46M |
| New Hampshire Senate D (YES) | 20:00 | 0.903 | 0.845 | 5.8c | 1.0M |
| Massachusetts Senate D (YES) | 20:00 | 0.982 | 0.925 | 5.7c | 0.37M |
| Florida Senate R (D NO) | 20:00 | 0.911 | 0.855 | 5.6c | 0.70M |
| Wyoming Senate R (D NO) | 21:00 | 0.987 | 0.935 | 5.2c | 0.24M |
| Virginia Senate D (YES) | 19:00 | 0.980 | 0.935 | 4.5c | 58k |

* **Depth is not the constraint.** Every top leg has 50k-5M of notional at an edge of at least 2c, and a 2,000 order
  fills at the touch. Two legs (Colorado Senate D, New Jersey Senate D) had no touch in the snapshot and get nothing.
* **The partisan cap is the constraint.** Our book is already **net D +6.3k** (long 17.0k, short 10.4k, measured with
  `risk.exposure` at SIG mids), which is over the 5,000 cap. The allocator therefore splits the budget into an R pool and
  a D pool so the basket ends net R by the excess. The 15k basket is net D -1.3k, which brings the book to +5,000.
  The live sleeve must do the same: buy R favourites first, and add D favourites only while the book stays at or under
  the cap.

## A3. Monte Carlo (200,000 election nights)

* **Outcomes.** They come from `sigbot/correlation_model.py`:
  * a national swing (sd 3.7 points x office beta), shared by every race;
  * a division shock and a state shock, shared by races in the same region or state;
  * idiosyncratic noise, integrated out.
* **Win chances.** Each race's D marginal is pinned to the outside fair ("outside") or to the calibration map
  ("calibrated"). The stress case scales the national swing by 1.5x.
* **Fills.** Fills walk the snapshot book from the best price.
* **Allocations tried.** Budgets of 10k, 15k and 20k; three weighting schemes (equal, edge, edge / (1 - P(win))); per-leg
  caps of 500-3,000. The winner at every budget is **edge / (1 - P(win)) with a 1,500-2,000 cap per leg** (figure
  panel D).
* **Why that scheme wins.** It puts more into the safest legs, which have the smallest chance of a total loss: Wyoming
  Senate R 2.0k, Rhode Island Senate D 1.5k, Oklahoma Senate R 1.2k, Tennessee Senate R 1.1k, West Virginia Senate R 0.9k.
* **Comparison.** Equal or edge weights at 15k give a similar EV (+712 / +823) with a worse tail: an average worst-5% loss
  of -1,908 / -2,020, against -1,356.

## Part B: the sleeve (drafted, off, wiring not applied)

The sleeve is `sigbot/proposals/cheap_favourites.py`. The Bot wiring, config fields, guard tests and sim scenario are in
`research_notes/cheap_favourites_wiring.diff`, a git diff rebased onto e9eef45, the recycler fix after the high-frequency arb fixes. It applies cleanly there. The main session applies it after the high-frequency arb deploy. Apply it with `git apply research_notes/cheap_favourites_wiring.diff`, then re-run both test
suites. Once applied, `Bot.cheapfav_cycle` runs after the directional poll and the MM's fair refresh. Orders use purpose `cheapfav`, and exits use `cheapfav_exit`. Its config is `cheapfav_*` and it is off by
default.

**Where the rules come from.** They are the study's filters:
* same-terms legs (read from `tc_mapping.json` at start-up; if the file is missing, the sleeve stays off);
* fast-count states;
* P(win) >= 0.80 and edge >= 2c, plus 1c when only one venue prices the leg;
* Polymarket and Kalshi within 4 points;
* no race with an Independent leg, and none of the punt's candidate races.

**Sizing.**
* The budget is spread by edge / (1 - p), capped per race outcome.
* Each order is at most 500 notional, at the touch, and at most 80% of the touch quantity.
* At most 3 orders per poll, and one per leg in flight (60 s TTL).
* The cash floor is 1,000.

**Holdings and exposure.**
* Partisan cap. This rule was rewritten after the 24 Oct precheck failure (see below).
  * Never add to a direction the bot blocked this cycle.
  * After the add, the book must stay within ±4,000 net D in the direction of the add, which keeps 1,000 under the
    5,000 cap for the other sleeves. The book is measured from a positions read made under the lock, plus unfilled
    orders from this sleeve and the directional sleeves, counted one-sided.
  * The sleeve's own net D stays within ±2,000, whatever the book is doing.
  * At today's book (+6.3k net D, so D is blocked) this lets about 2k of R favourites in, worth roughly +100 to +150
    expected. That is well short of the unconstrained 15k basket's +840. Raising `cheapfav_net_max` lets in more R while
    the book is long D; it is a trade-off against partisan room for the catch-up sweep.
* 4,000 shares: no race where any leg holds more than 4,000, and no order that would take a leg past 4,000.
* The sleeve never buys a route that trades against a holding on that leg. SIG nets YES against NO, so such an order
  would close an arb set.
* Its positions are excluded from MM inventory, the same treatment as the catch-up sweep's.

**Exits.** The sleeve exits only when SIG prices the held side 2c or more above the fair, both venues agree, and the
state's polls have not closed. Selling then beats holding in expectation and frees the cash before stage 1. It never
exits on a loss in marks.

**Restarts and timing.** The ledger is rebuilt from fills, so a restart is safe. The last entry is 3 Nov 17:30 ET.

**Fable review (24 Oct) and the fixes now in the patch.** The review found nothing that loses money in normal
operation. These holes are fixed, each with a guard test:
1. A failed positions read (empty map) now blocks entries. Before, every holding guard would have been blind.
2. Entry orders from the last 10 minutes that are not yet in the fills table count as held. A lagging fill ingest
   therefore cannot double-buy a leg.
3. Single-venue legs keep the 3c bar at the fresh-book recheck.
4. The soft partisan stop follows the cap (cap - 200).
5. The 4,000-share race skip checks every leg of the race, including legs without a directional row.
6. New in the rebase: the sleeve skips races where the high-frequency arb (churn) holds sets. Churn books its sets from
   what the venue shows, so the two ledgers must not net against each other.

**Second Fable review (rebased patch, 24 Oct): SHIP WITH FIXES. All fixes are in the patch and tested.**

SIG reports any race that is NO on every leg as an arb set. A cheap-favourite R-NO next to another sleeve's D-NO
therefore looked like a set to the recycler, the free unwinds and the high-frequency arb's sell-all, and any of them
could close it at the ask. The fixes:
* Cheap-favourite races are added to `arb.protected`.
* Both free-unwind passes, the churn sell-all and churn entries skip these races.
* The sleeve never buys a NO next to a NO on another leg of the race.
* Exits and entries run under `trade_lock` on the fresh `positions_snapshot`.
* Unfilled entry orders from the last 3 minutes count toward net D.
* A response without an `orderId` blocks that leg for an hour.

**Rebase onto 34d0d44.** An exit now closes exactly the sleeve's own ledger quantity, the same as the catch-up sweep.
It is not clamped to the venue's net position.
* Why: other sleeves trade the same legs, every sleeve keeps its own fills ledger, and `mm_positions` subtracts
  this sleeve's holdings.
* What broke: clamping exits to the venue's net position, as churn does after 34d0d44, blocked a valid exit in the
  sim. The cheapfav ledger held +174 YES while the venue showed -1,426 after a lag-follow short. Churn needs that clamp
  because its sets must exist on the venue; this sleeve does not.
* Added guard: at most one exit per leg per 5 minutes, filled or not, so a failed fill ingest cannot trigger a second
  exit.
* Fable checks of the rebase: SHIP for the merge, then SHIP WITH FIXES for the exit sizing. The 5-minute guard is that
  fix.

Left as it is: the MM may quote on a cheap-favourite leg and round-trip part of it. This corrects itself, the same as
with the catch-up sweep.

Two points are left as they are, the same as in the catch-up sweep:
* Two sleeves entering in one cycle can overshoot the partisan cap by one small order.
* A crash between sending an order and recording it leaves an unattributed fill.

Enable: the patch now carries `config.json` with `"cheapfav_enabled": true, "cheapfav_budget": 15000.0,
"cheapfav_min_edge": 0.02, "cheapfav_leg_cap": 2000.0, "cheapfav_order_notional": 500.0, "cheapfav_cash_floor": 1000.0,
"cheapfav_net_max": 2000.0, "cheapfav_headroom": 1000.0`.

**Precheck failure, 24 Oct, and the fix.** With the sleeve on under the live config, two scenarios failed:
* `inside_on`: a cheap-favourite D entry took net D to +5,010 while D was blocked.
* `catchup`: the sleeve had filled the partisan room to 5,358, so the catch-up sweep's entries were starved and an MM
  fill landed on a gated side.

The fix is the partisan rule above, plus two more changes. Races the catch-up sweep is in are skipped. Other sleeves'
unfilled orders count: those placed in the last 330 s, or still resting at the venue.

Reviews of the fix:
* Opus adversarial review, first pass: FIX FIRST. It found phantom pending orders, stale same-cycle exposure and an
  unbounded narrowing branch.
* All three were fixed and tested. The re-check said SAFE TO DEPLOY.
* Its last point, about the order-age window, is fixed too.

Left as is: the sleeve sends its orders while holding `trade_lock`. Churn and the recycler do the same, and it is not
deploy-blocking. The follow-up is to send orders after releasing the lock.

## Caveats

* **The outside fair is a single snapshot.** The gaps are persistent (correlation_arbs.md: half-life ~3 weeks) and
  favourites do not drift (favourite_drift.md). So buying any time before 3 Nov 18:00 ET should get similar edges. The
  sleeve re-checks the edge on every order.
* **The sample has no upsets, so the size of the calibration bias is an upper bound.** The GO does not need the bias.
* **Single-venue legs.** Kentucky Senate has no Kalshi market, and its fair is Polymarket only.
* **Settlement timing.** A call that slips past midnight (a close race we mis-rated as safe) keeps that leg's cash
  locked longer. It does not change the payoff.
