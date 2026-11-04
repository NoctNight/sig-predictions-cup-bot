# Gap trades (27 Oct): DROP, nothing built

Script `research_notes/gap_trades_study.py`, numbers `data/research_gap/study.json`. Source: `venue_fair` (975k rows, 21 Oct 23:49 -
27 Oct), read-only. Rule: enter at the SIG touch when the outside fair is >= 3c better than the touch (buy YES at ask, or NO at
1 - bid), PM/Kalshi within 4 pts, one entry per leg per 24 h; exit at the SIG touch h hours later (net = after the full spread).
IS = first half of the data, OOS = second half (from ~24 Oct 12:00 UTC). t clustered by race (race means). No plot yet (no
plotting stack on this machine's Python); numbers below are the whole result.

| group | half | h | net mean | t | gross (mid to mid) | gap closed | races |
|---|---|---|---|---|---|---|---|
| all, YES side | IS | 24 / 48 / 96 | +0.9 / +1.0 / +1.0c | 4.6 / 4.5 / 3.0 | +1.5 / +1.6 / +1.6c | 36 / 38 / 34% | 74 |
| all, NO side | IS | 24 / 48 / 96 | +0.8 / +1.0 / +1.1c | 5.4 / 5.2 / 4.2 | +1.4 / +1.6 / +1.8c | 34 / 38 / 37% | 83 |
| all, YES side | **OOS** | 24 / 48 | **-0.6 / -0.9c** | -4.9 / -3.6 | +0.1 / -0.3c | 2% / -10% | 53 / 44 |
| all, NO side | **OOS** | 24 / 48 | **-0.3 / -0.4c** | -2.5 / -1.8 | +0.3 / +0.2c | 9% / 5% | 65 / 57 |
| early-call states (polls close <= 21:00 ET; same legs as the fast-count set) | OOS | 24 / 48 | -0.6 / -1.0c YES, -0.2 / -0.5c NO | -4.5 / -2.9, -1.7 / -1.7 | ~0 | ~0 | 32-48 |

(96 h has no OOS sample: the data ends too soon.)

## Verdicts
1. **Gap closing before the punt: DROP.** In-sample about a third of the gap closes and nets +1c (t 4-5); out of sample none of
   it does (gross ~0, net -0.3 to -0.9c, t down to -4.9, the sign flips). Same picture as follow-the-gap in high_variance.md.
   The earlier "closing" was a regime of the first days; the SIG DMM now holds its price. The 5-8 day horizon is further out
   than anything testable here and would need the in-sample effect to come back.
2. **Early-call version: not a new build.** Held through the call, the payoff is settlement, which this data cannot test (nothing has
   settled). For favourites >= 0.80 that is exactly the live cheap-favourites sleeve (calibration 52/52, ev_hunt part 3, already
   sold/rolled at the call). What the extension would add (legs under 0.80, NO on rich longshots) rests on part 3's thinner
   longshot sample, with no out-of-sample t > 2 I can show. It fails the bar as stated.
3. Funding rule (exit cost < edge - 0.5%): moot. Selling holdings to buy gap trades would pay up to the gap's edge to earn
   an edge that out of sample is negative after costs.

## If revisited
Settled-leg calibration (731 legs, `data/research_ev_hunt/calib/`) for sub-0.80 legs in early-call states, held to the call,
with a t-stat clustered by race. That is the only test that could clear the bar for (2). Cheaper alternative: just raise the cheap-favourites
budget and lower its min_fav a notch, since that path is already live and gated.

## Tiny live pilot (28 Oct): built, reviewed, NOT precheck-clean, not deployed

Sizhe asked for "a tiny live pilot" after the DROP (no clean out-of-sample edge; the pilot buys live evidence, it is not a proven edge).
Deliverable: `research_notes/gap_trades_wiring.diff` (787 lines, `git apply --check` clean on HEAD a0298d2): new
`sigbot/proposals/gap_pilot.py`, `gap_cycle` / `_gap_trade` / `_gap_blind` in `run.py`, `gap_*` config fields, the launch keys in
`config.json`, 70+ guard tests and the `gappilot` sim scenario. Built in a git worktree; the shared tree, the live config and the bot were not touched.

**What it does.** Buys the side SIG prices >= 3c under the Polymarket/Kalshi fair (YES at the ask, NO at 1 - bid; +1c if one venue)
on any same-terms two-party leg, walking the book while each marginal share keeps the edge; one route per race outcome; excludes the
House and Senate chamber markets, punt races, other sleeves' and wind-down races, legs held > 4,000 shares, races where another
sleeve holds > 100 shares. 900 total / 300 per leg / 150 per order inside the shared cheapfav 15,000; own net D within +-500; book
net D keeps the 1,000 headroom. Exits at the touch: gap closed or reversed, stop (4c on the mid of a normal book), 72 h, hard exit
31 Oct 00:00 UTC (before the wind-down); last entry 29 Oct 20:00 UTC. Kill switch: `params/strategy_kill.json` names `gap` / `exit:gap`
(aliased to cheapfav). No funding swaps. Trades through the cheapfav ledger (purposes cheapfav / cheapfav_exit), legs labelled in param `gap_legs`.

**Launch config** (in the diff's config.json): `gap_enabled true, gap_budget 900, gap_min_edge 0.03, gap_leg_cap 300,
gap_order_notional 150, gap_net_max 500, gap_hold_hours 72, gap_stop 0.04, gap_take_edge 0.005`.

**What it would buy now** (27 Oct 23:41 UTC poll, empty positions, touch size assumed 800, book net D +4.2k): 27 candidates (12 YES-D, 7 NO-D, 5 YES-R,
3 NO-R). First 6 orders to the 900 cap (150 each): NH Governor 1062 YES (R wins, 4.9c), Tennessee Senate 1051 NO (R, 4.9c), Oklahoma Governor 1063 YES
(R, 4.5c), Rhode Island Senate 1076 YES (D, 8.4c), Mississippi Senate 961 NO (R, 4.4c), New York Governor 874 NO (D, 6.1c); pilot net D -299, book net D +4.2k -> +3.9k.
Live positions would remove some (the clean-race rule: RI Senate holds sets). Next by edge: Michigan Senate 1048, NH Senate 1070, MN-02, NE-02. All candidates at
150 each = 4,050 notional; edge averages 4.4c.

**Reviews.** Four Opus 5.5 high-effort adversarial reviews. Findings fixed: cheapfav pause aliases (else the strategy-stops bypass rule pauses all entries bot-wide),
stop-loss churn, exit double-send, orphaned labels, one-sided pending, chamber markets, wind-down locks, unknown-outcome orders (timeouts escape ApiError), own-quote netting,
exits with unknown outcome never auto-resent. The last review of the exit change said SAFE TO DEPLOY. After it, two small tightenings were added without re-review (fresh
60 s expiry per order; cash floor = max(cheapfav, churn floor)). Known nits left: gap-closed exit does not check venue agreement, exits ignore the partisan cap, blind notional not in own net,
an unknown-outcome exit leaves the leg held to the call (ERROR logged), tests are mostly source-level (no injected-timeout test).

**Gates: the precheck sim line does NOT pass with the live config + pilot enabled.** Clean HEAD passes these; with the pilot (budget 900) they fail:
* `churn_many_lowcash`: churn_none (no entry). With the pilot's budget 0 it passes, so its orders matter: its legs sit in cheapfav-ledger races, which the set paths
  (churn, recycler) must leave alone, and the tiny sim universe has few races.
* `slow_writes`: expired_on_arrival x4 on the shock sleeve's orders (12 s writes). Passes at budget 0.
* `day` (24 h): mm_filled_on_gated_side x1-2 (also with the code before the last two tightenings). With the pilot's budget 0 the 24 h day passes (0 violations), so the pilot's orders cause it.
* `shock_reserve` fails at pilot budgets >= ~1,200 (passes at 900); `churn_blowout` fails on clean HEAD too.
Passing: guard tests, `gappilot`, `cheapfav`, `cash_raise_cheapfav`, `shock_reserve` (at 900) and the other 33 scenarios.
A patch to make the pilot lighter (fewer book reads, 2 orders a cycle, stand down in a shock) made the cheapfav scenario fail instead and was reverted.

**Recommendation.** Do not deploy as is: botctl's precheck gate would block it anyway. Options: (a) pilot only where no other sleeve can want the race (needs the churn
race list in the pilot), (b) run it with `gap_budget` 150-600, which passes the sims I tried, (c) drop the pilot (the study says there is no clean out-of-sample edge;
the 30 Oct rerun is scheduled).

### Budget retest (28 Oct): no pilot budget from 150 to 600 passes the whole precheck
Final code, live config with `gap_enabled` true, full sim line (all scenarios but `day`) and the 24 h `day`, each in its own copy:

| gap_budget | sim line | day |
|---|---|---|
| 150 | fails `slow_writes` (expired_on_arrival x4) + `churn_blowout` (fails on clean HEAD too) | passes |
| 300 | same two | fails `mm_filled_on_gated_side` x1 (MM bid on leg 872, placed 24 Oct 10:05 sim, filled 20 h later against the xv gate) |
| 600 | same two | same day failure |
`churn_many_lowcash` and `shock_reserve` pass at all three. Zero budget passes everything, so the pilot's orders cause the failures.
`slow_writes`: the failing orders are the shock sleeve's (12 s writes, 60 s expiry); its batch has almost no slack, so one extra 12 s write by the pilot pushes its last order past the expiry in the sim. That looks like a timing coincidence of the scenario rather than a live risk (the shock computes its expiry inside the trade lock), but the precheck gate does not care.
`day`: the MM resting quote on 872 not being pulled when the gate flips is outside the pilot's code; the pilot changes the path enough to expose it.
Neither was patched: forcing a pass (e.g. a start-up delay for the pilot) would be tuning to the sim, not a fix.

### 29 Oct: the chase of the two sim failures, and what the diff now contains

**`slow_writes`: root cause fixed, in six sleeves, none of them the pilot.** The scenario makes every write take 12 s. Any sleeve that
computed ONE 60 s expiry before a loop of several orders had every order after the first few arrive at the venue already expired.
Shock mode was the one the sim caught; an audit of all 16 `place_order(expiration=...)` sites found the same pattern in cash raise,
catch-up, cheap favourites (its expiry was even computed before waiting for the trade lock), lag follow and election night (the worst:
a 15 s TTL). All six now set each order's expiry right before its own write (`expiry_in(seconds)`, same seconds as before). Left
alone: sites that already were per-order or send one order (zero-EV exit, wind-down, offload, punt adapter whose payload must stay
identical for the idempotent resend, direction.py which is capped at 2 writes a poll, news.py). The shock taker/maker exit stamps
(`shock_exit_sent`, `shock_hold`, `shock_ingest_ts`) now share one wall clock with the per-order expiry; the Opus review found the
first version of that fix left the maker-path stamps on the tick clock.

**`day` (mm_filled_on_gated_side): a pre-existing market-maker hole; the pilot only changes the path that exposes it.** Leg 872
(Nevada Governor): the partisan block leaves only the R side quotable, then the outside fair drifts so the xv gate blocks that side too.
`quote_cycle` then builds no quotes and, at `mm.py:485` (`if not quotes: continue`), returns before `cancel_all`, so the resting
0.235 bid stays on the book with its 1,800 s TTL and is hit twice (10:23 and 10:24 sim time). The same hole is in `fast_requote_pass`.
Evidence: the pilot never traded legs 871/872 (no pilot order in the pilot-300 sim DB; its edge on that race was at most 2.2c),
the clean-HEAD control day passes (0 violations) and a probe with the real `MarketMaker` reproduces the no-cancel path. Whether the
checker catches it depends on inventory and bid placement along the run, which the pilot's orders perturb: budgets 150 and 0 pass,
300 and 600 fail. NOT fixed here: it is live market-maker code and wants its own change, test and review (proposed as a separate task).

**Three real pilot bugs, found by new behavioural tests (`tests/test_gap_failure_paths.py`, 81 checks, stub-driven: a fake venue
that raises TimeoutError / URLError / ApiError 4xx/5xx or returns no orderId, an in-memory store, the real `gap_cycle` code).**
B1 a stale `gap_exit_unknown` mark was never cleared from the stored param (it blocked the exit of the same leg bought again later):
now written back once the ledger shows the leg closed, and such legs stay out of re-entry. B2 blind entries (outcome unknown) counted
in the budget and leg caps but not in the pilot's own +-500 net-D band: records now carry their direction. B3 an exit could be re-sent
after the 900 s spacing while the fills ingest lagged, flipping the leg through zero: an exit now waits (at most 45 min) while the
latest exit order on the leg has no ingested fill and the venue no longer shows our side, and a partial fill caps the next exit at the
venue's quantity. The first version of B3 waited 6 h on the venue sign alone, which the review showed would stall any leg another
sleeve (the market maker) was short on. The new test file is in `deploy/precheck.sh`.

**Reviews (29 Oct):** two independent Opus 5.5 high-effort reviews, one of the non-pilot changes (expiry, shock stamps, cheapfav skipping
the pilot's legs), one of the pilot fixes and tests. Each found one SHOULD FIX (the shock maker-path clock; the 6 h B3 wait), both fixed
and re-checked: both say SAFE TO DEPLOY. Known, accepted: an unknown-outcome exit that did fill keeps its ledger quantity (logged as
"needs a look"); `_blind_rec` raises on a malformed param (only the bot writes it); time/hard exits sell at the touch with no spread gate.

**Diff:** `research_notes/gap_trades_wiring.diff`, 1,801 lines, applies cleanly to the live tree at 97b841d (8 files: run.py,
config.py, gap_pilot.py, config.json with the launch keys, sim scenario, guard tests, failure-path tests, precheck). Launch config
unchanged except `gap_budget` 150 (900 fails `shock_reserve`; 300+ exposes the market-maker hole in `day`). Sims on the final code:
see the next entry.

**Final sims (29 Oct 01:20 UTC, live config + the diff, `gap_budget` 150):** the 24 h `day` passes (0 violations); the scenario line
passes 39 of 40, the one failure `churn_blowout` (churn_over_pot, 12,164 > 12,000 at cycle 27) being byte-identical on a clean HEAD, so
it is pre-existing and blocks the precheck gate for ANY change until someone fixes or re-baselines it. `slow_writes` passes on the
final code. Status: reviewed SAFE twice, tests and sims clean apart from the pre-existing churn_blowout; nothing deployed, the deploy
decision is the main session's (and Sizhe's: the pilot at 150 is one ~150 order; the expiry fixes are the valuable part).
