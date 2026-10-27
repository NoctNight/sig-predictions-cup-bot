# Strategy research backlog (daily research run, 23 Oct - 2 Nov 2026)

The 06:00 daily run takes the top open item(s), studies them, and updates this file: move finished items to "Done" with
a one-line verdict and a link to the note. Add new ideas at the bottom of "Open" with a one-line reason. Rank by
expected susqies gained before settlement (4 Nov 12:00 ET) per hour of work.

Bar for going live: out-of-sample positive with t > 2 (or a riskless mechanism), net of spread and our own market
impact, beats the ~3%-to-settlement return of capital left in arb sets, fits every hard limit, and passes the gate
(guard test + tests/sim_pipeline.py + precheck + Fable review + 10-min post-deploy watch).

## Open
0c. (added 24 Oct, kill-switch review O finding 1, pre-existing) zero-EV exits size on GROSS holdings: on 3 Nov they could
   sell the punt's shares or set legs. Before the punt executor (0000) goes live, exclude punt/election purposes and set
   legs from zero_ev_exits (and backfill fills.purpose, finding 5), under the usual gates.
0a. (added 24 Oct) WEEKEND LAG decision, Mon 26 Oct: re-run research_notes/overnight_followups.md section 2 on all of
   24-25 Oct (both-venue moves; SIG copy share at 10/30 min vs weekday 29% / 44%). Act only if the weekend copy at 10 min
   is < 15% with n >= 10 (then raise catch-up size on weekends via the usual gates).
0b. (added 24 Oct) RIVAL LIQUIDATION ABSORB (overnight_followups.md section 3): research_notes/liquidation_absorb_wiring.diff
   is built and reviewed SAFE (switched off). From 31 Oct the daily brief runs its detector; apply the diff (still off) in
   a gated deploy before 31 Oct, and enable it only if taker sell/buy > 60/40 for a day AND the favourite SIG-outside gap
   widens > 2 ticks.
0000. PUNT EXECUTOR (Sizhe 23 Oct: "let me verify the punts and you send the orders"). Build and fully test BEFORE 1 Nov:
   a plan-driven executor (sigbot/proposals/punt_executor.py + Bot wiring, OFF by default, enabled only by a session in
   which Sizhe approved the exact plan in chat). Plan = ordered steps, each with market (exchange id), side, max price,
   max size or cash, and a trigger: "buy now", "sell when bid >= X and Polymarket AND Kalshi >= Y", "buy toward target =
   max(T, k x best live rival balance)" (leaderboard read), "stop at time Z". Executes by walking the book in chunks never
   above the max price; logs every action; idempotent across restarts (state file); a dry-run mode that prints what it
   would do against live books. Needs: behaviour tests, a sim scenario of the night (stage 1 wins / loses / thin books /
   delayed calls / restart), Fable review, botctl deploy. Uses research_notes/punt_strategy.md section on the roll and
   the live books in data/books_latest.json. Never places anything without the approved plan.
   FABLE CHECK OF THE PUNT PLAN (24 Oct ~01:00): roll still the only option with real prize EV, but rewrite the plan
   before building: drop MN Sen R -> OR Gov R (stage 1 is a 13c leg; Oregon counts slowly); stage 1 at a 22-35c average
   fill in a state whose polls close by 20:00 ET; target max(1.3M, 1.12 x live leader) (rival punts are correlated, so
   the bar is bimodal ~1.2M / 1.3-1.5M); model the stage-1 sale at 0.85 with a netting fallback (buy the other leg's
   asks: a pair pays 1 at once); stage 2 is a fair-price bet with a hard price cap and abort-to-cash if the count has
   already moved it; stage 2 only in races called the same night (ranking is by SETTLED balance; avoid AZ CA NV OR WA,
   GA runoff, AK/ME ranked choice). Executor guards: capped limit orders only, verify fills from server records before
   stage 2, idempotency keys + retry-same-key on 502, cancel all MM orders first (self-trade), call rule = both outside
   venues >= 0.95 for several polls (single-venue 0.97 fallback), pre-approved ordered list of stage-2 candidates, ET =
   UTC-5 on 3 Nov, leg orientation checked at approval, dry-run default + kill switch. Still unknown: whether SIG halts
   or auto-settles a market at the media call (find out before 3 Nov: watch any market that resolves early, or ask the
   venue). Honest EV ~$300 (range $100-600), conditional on prize eligibility.
   ADDENDUM (timing): no market has ever settled in this tournament, so auto-settlement speed is unknown (feed checked
   every 4 h; no documented halt-on-call for race legs; chamber control and runoffs are manual and closed to trading
   until settled). Executor: treat auto-settlement as a bonus (detect via settlements[]/marketSettled, confirm with
   /portfolio/settlements, then use the cash); rest a 0.85 limit sell (or buy the other leg at <= 0.15, which nets pairs
   to 1 at once) from the outside call; stage-1 sale deadline tied to stage 2's poll close; if < ~70% sold by then, size
   stage 2 to the cash realised or abort to cash if the target band can't be reached; treat 403/409/503 on a sell as
   "re-read portfolio and market status". Only fast-count states in both stages: drop any OR, NV, AK, AZ, CA, WA, ME, UT
   or GA leg. Passing stage-1 legs: FL-13 / FL-16 D, VT Gov D, SC Sen D / SC-01 D, VA-05 D (avoid NH). Passing stage-2
   legs: NE Gov D, KS Gov D, MN-01 D, LA Sen D (re-verify Louisiana has no December runoff). Main risk: SIG's market
   maker lags a 20-70 point move by tens of minutes, so the >= 0.85 sale may come too late for stage 2. Night-time depth
   on normal days is fine (touch depth is higher at night than by day).
000. NEW OBJECTIVE (Sizhe, 23 Oct ~15:20 BST): maximise LIQUID CASH available at ~18:00 ET on 3 Nov for Sizhe's punt.
   Rank and settlement value no longer matter; a position's worth is its sale value on 3 Nov evening. Live since 23 Oct
   (main session): arb.new_sets=false (no new sets, no recycles), tilt off, direction.allow_live=false (polling stays on
   for the MM gate), offload off; free unwinds, zero-EV exits, residual hedges and the MM stay on. The research bar
   becomes "adds liquid cash by 3 Nov evening at t > 2"; hold-to-settlement edges no longer count.
   BUILD BEFORE 31 Oct (this is the capital-raise routine in 00b, now with a bigger job; off until its start time):
   a. Classify every held set by when its race is likely to be CALLED on election night (poll-closing time ET, plus how
      close the race is): early (called before ~22:00 ET 3 Nov) vs late (west coast / Hawaii / Alaska / close races).
   RAISE_TARGET = EVERYTHING (Sizhe, 23 Oct ~17:00 BST: "everything for the punt"). So: late-call sets, tilt and
   directional holdings and MM inventory are all turned into cash by 18:00 ET 3 Nov (patient, from 31 Oct, cheapest
   exit cost first); early-call sets are held and sold at their call on election night (approved by the same answer),
   which only adds cash if the punt is placed after those calls: the 1 Nov brief must ask Sizhe when he plans to place
   the punt, and if it is at or before ~19:00 ET, unwind the early-call sets before then too. Projected cash: ~95-100k.
   (The original rule below still applies to the mechanics: the routine raises up to the target and nothing more.)
   b. NO UNWIND WITHOUT SIZHE'S GO (Sizhe, 23 Oct ~16:20 BST: "only unwind if the money we unwind is able to make more
      money"). Every unwind loses susqies (exit cost 3.75-4.75% ~= the locked edge); it only pays if that cash goes into
      the punt. So the routine raises exactly the amount Sizhe sets (RAISE_TARGET, default 0 = hold everything to
      settlement) and nothing more. The 30 Oct brief must ask Sizhe for RAISE_TARGET (0 / 30k / 50k / 70k / all), with
      the cost of each and what it buys in punt odds. Once set: late-call sets first, cheapest exit cost per unit of cash
      first, patient over the remaining days (limit per hour so we do not walk the DMM books), cost cap starting at the
      set's locked return and loosening to ~4.5% by 2 Nov and 5.5% by 17:45 ET 3 Nov; stop as soon as cash >= target.
      Exception: the RI Governor trim (00a) at no realised loss, which is risk reduction, may run without a target.
   c. Early-call sets: hold; if (and only if) RAISE_TARGET is not yet met, on 3 Nov evening sell sets as their races are called (legs at >= 0.97 / <= 0.03)
      at near payout. This is a switch Sizhe turns on, or that turns itself on at 18:00 ET 3 Nov only if Sizhe has
      approved it by then; until then, report-only.
   d. MM: shrink inventory from 1 Nov and be flat by 12:00 ET 3 Nov only if RAISE_TARGET > 0; tilt/directional holdings: exit patiently from
      31 Oct when the exit costs <= ~1 tick against the outside fair.
   e. Daily brief: report projected liquid cash at 18:00 ET 3 Nov (cash + sale value of everything, by call time).
   f. Refresh proposals/punt_screener.py with the projected cash and the live third-place balance before 3 Nov.
   Never place the punt; Sizhe does that.
   g. EXECUTION FINDINGS 24 Oct (build these in; build handed to the ladder-exits session 24 Oct ~15:20 BST):
      * ladder_exits.md (Fable-checked): never sell a big set in one go; ~1,000-set clips about every 30 min, only when
        the clip's exit cost is at or below the race's trailing-24-h median ("level 0"). Holding a remainder for a
        better price (Sizhe's ladder) is not proven and costs time; no ladder.
      * ev_hunt.md 2c (Fable-checked GO with changes): time the sales the wind-down must make anyway by deviation
        (sell holdings when rich vs their usual level), ~+80 S (+50 to +110) on today's holdings.
      * ev_hunt.md part 3: Polymarket/Kalshi favourites are underpriced and longshots overpriced (the shrunk map beats
        raw prices in 4/4 held-out cycles): cheap favourites' edge is ~2x at settlement; the punt screener's map holds.
00. SIZHE'S DECISIONS 23 Oct ~11:45 BST (do these first, under the normal gates):
   a. Rhode Island Governor: CAP IT. Its three legs (D 1073, R 1074, I 880) are separate binary markets (the I market was
      created 24 Aug, D/R on 18 Sep); nothing guarantees exactly one resolves YES. Our sell-set (NO x3) pays 2 only if
      exactly one wins; two YES (fusion/independent-label ambiguity or a settlement error) pays 1: about -13k on the
      14,793 sets. Step 1: no new RI Governor sets (per-race cap at the current holding; the 3-leg / high-ret ceilings
      must not override it). Step 2: trim toward the normal 15% race ceiling, only by patient unwinds whose walked exit
      cost <= the set's locked return (no realised loss), via the capital-raise routine (b). Step 3: check NE Senate and
      MT Senate (the other 3-leg sets) for the same multi-YES / resolution-wording risk and report.
   b. Capital-raise routine: APPROVED to build. Patient arb-set unwinds with a per-set cost cap (default: walked exit
      cost <= locked return; election-night schedule 4.0% loosening to 5.5% by 17:45 ET on 3 Nov), a cross-sleeve cash
      floor, OFF by default. Allowed live use before 3 Nov: only the RI trim in (a), at no realised loss. Any raise for
      the election-night sleeve or the punt waits for Sizhe's go on 3 Nov.
   c. Election-night cash amount (30k/50k/70k): undecided, Sizhe decides on 3 Nov.
0. Partisan cap overshoot: the block is checked once per cycle against positions only. 23 Oct 07:21 BST: exposure dipped to
   4,826, the block lifted for two cycles, tilt posted a 6-hour ladder of longshot sells and exposure jumped to 5,723 in one
   cycle (5,630 at 12:00 after five of those orders filled). Make the check count resting orders that would add to the
   direction, and cancel resting adds when the block is on. The 5,000 figure is a hard limit and does not change; this
   only makes it bite earlier. Needs: guard test (block lifts for one cycle, ladder posted, exposure must stay <= cap),
   sim scenario, Fable review. Decision 5 in data/daily/2026-10-23.md (default: build unless Sizhe says no).
1. Recycle sizing fix (deployed 23 Oct): watch for a day that unwinds free capital the follow-through does not spend
   (data/live.log RECYCLE lines vs the ARB fills after them; arb_trades). If freed/deployed stays within 5%, close.
2. Settlement-basis value is not logged per cycle (only marked value in `cycles`), so the daily brief cannot compare
   settlement value with 24 h ago exactly. Write the kill switch's settlement-basis value into `cycles.notes` each cycle.
   Small, no trading change.
3. Allocator assumes a 1% unwind cost for arb sets; measured exit cost is 2.5-5% of payout (election_night_capital_plan.md).
   Its EXIT_DEADLINE 04:00Z is 23:00 ET on 2 Nov, not midnight as the comment says. Re-derive the offload/recycle gains with
   the measured cost; check whether any live offload rule uses the 1% figure.
4. Catch-up trade (volatile_moves.md (c)): +5.5 ticks on 10 trades from one night. Evaluate the paper log
   data/volatile_paper.jsonl once 30+ signals over 3+ nights exist; go live small only if it clears +2 ticks.
5. MM size: the 4x replay failed; re-test 2x vs 3x on the latest markouts (tracker, review --json) and the
   quote-sizing note (research_notes/Market maker quote sizing by depth). MM 30-min markout was -0.12 ticks (n 338) on
   23 Oct: do not size up until it is >= 0 over 48 h.
6. Free-unwind / recycle thresholds: are we leaving money in sets whose sale value is within 0.2% of payout?
   Measure how often books come close, and the value of a slightly lower threshold. (election_night_capital_plan.md
   says RI Governor never came within 2% of payout in 8 days; check the 2-leg sets.)
7. Favourite drift into resolution (favourite_drift.md): re-check with the latest week of data.
8. Late-punt screener refresh (proposals/punt_screener.py): update the candidate list for Sizhe's 3 Nov decision.
   Never place the punt.
9. Directional 6+ tick / 12 h rule: re-test again only if two more days of live quotes accumulate; as of 23 Oct it is
   negative out of sample (t -3.7 pooled). Low priority.

- 23 Oct lookback (research_notes/volatile_lookback_2026-10-23.md): 28 moves of 5+ ticks, all DMM re-pricing, none
  tradeable; fade/momentum/imbalance rules lose or are noise. Two follow-ups: (i) fold "follow a 3+ point Polymarket AND
  Kalshi move SIG has not followed" into the catch-up paper test (item 4): pooled +1.27 t (t 2.1, n 62) but +0.29 (t 0.9)
  without the top 3 races; trade only after 30+ new signals clear +1 t with t > 2 ex-top-3. (ii) Bot downtime: it was
  down 262 min of that day (incl. the 23:25-00:33 fix pause and the 10:08-11:03 network outage); find the rest and cut it
  (laptop power/network; a server would remove both). Fable double-check of the lookback not yet run (nothing acted on).

- 23 Oct "sweep SIG when Polymarket+Kalshi move" (research_notes/outside_stable_sweep.md, figure outside_sweep_x.png):
  optimal wait x = 0 (waiting to see the outside "stabilise" adds nothing; other bots rarely take SIG's stale side; the DMM
  leaves ~99% of the gap at 60 s). Fable double-check: PAPER-TRADE ONLY. The headline cell (+1.46 t 2.3) was picked on the
  out-of-sample data (best of ~28 cells, p ~0.07 by chance); over half the OOS edge is the outside continuing to move, not
  SIG catching up; pooled pure convergence net of cost +0.52 with 22 Oct, +0.17 without; honest edge +0.3 (-0.5..+1.2)
  tick/share, ~+1-3 S/day. Daily run: mark the outside_lead paper rows in data/volatile_paper.jsonl at 4 h / 12 h on PURE
  convergence (SIG move minus the outside's further move); go live only if 10+ post-23-Oct signals average >= +0.5 tick,
  then R-adding only, 300 notional, kill at -50, flat by 12:00 ET 3 Nov.

- 23 Oct HIGH-FREQUENCY ARB (Sizhe's name for "deviation churn"; code churn_*): Sizhe chose to run it live. Built
  (sigbot/proposals/dev_churn.py, Bot.churn_cycle), caps 12k / 3k floor / 4k per race / -600 stop, volatility pause
  (>= 1.5x normal gap movement, resume < 1.2x), no entries from 1 Nov 00:00 UTC, sell everything from 3 Nov 22:30 UTC.
  Launch waits on the Fable test-environment check; its recommended features follow. Watch daily: entries, exits,
  realised P&L, rejected baskets, pause state (grep "CHURN" in data/live.log).

- 23 Oct trading-firm tactics round 2 (research_notes/hft_tactics_2.md): DMM requote timing/batching/inventory skew,
  large-taker footprints, time of day, queue age, same-state cross-race moves: all DROP (best is pulling our quotes
  before a requote batch, < 10 S/day; following/fading big takers loses 1.2-1.3 ticks/share). Note: it says SIG has no
  chamber-control market; contract_rules.md says SIG has "U.S. House"/"U.S. Senate" — check before relying on either.
- 23 Oct contract rules (research_notes/contract_rules.md, data/research_contract_rules/tc_mapping.json): all venues pay
  on the winner's party incl. runoffs; 220/237 legs same terms, 194 safe to compare. Fixes for the bot's fair value:
  for Idaho Senate, South Dakota Senate and the Maine, Tennessee, Michigan and Alaska governor races (no SIG independent
  leg) compare each leg with the SAME party's outside price, never 1 - other party; check the Alaska Senate Polymarket
  R side (two Sullivan markets summed?); use Kalshi's direct CONTROLS-2026 / CONTROLH-2026 markets for chamber control.
  An uncalled race keeps SIG cash locked past 4 Nov (payoff unchanged) — the wind-down must treat slow-count races
  (GA runoff, AK/ME ranked choice, AZ CA NV OR WA) as late-call.

- 23 Oct punt strategy double-check (Fable): keep the decision (two-stage roll aimed at 1st, >= 1.45M, no single
  longshot before 18:00 ET, stage-2 target from the live leaderboard), but fix before 3 Nov: (1) punt_screener's
  roll_gap_h is 1 h — expose it and re-run with a 3-4 h minimum gap (stage 1 closing 19:00 ET in FL/VT/SC/VA/GA/KY/IN,
  stage 2 closing 23:00 ET or later, 21:00 at the earliest); (2) write the fallback for a stage 1 that wins but cannot
  be sold at >= 0.85 in time (sell down to ~0.8 only if a stage-2 leg still clears the live 1st-place bar, else hold for
  a 23:00+ fair-price leg); (3) do not apply the longshot haircut to 25-40c legs; (4) relabel the 2-15c statistics
  without presidential safe states (comparable races 0/32, P = 0.13, weak evidence). Realistic EV ~$300-600 (range
  $100-1,100), a ~2% shot at $30k. Build these into the punt executor's plan (item 0000).

- 24 Oct studies (side sessions; notes in research_notes/): skewed arb (skewed_arb.md) DROP; resting orders for
  high-frequency arb DROP; long volatility (long_vol.md) DROP before election night, keep long-vol for the punt; taker
  flow types (flow_patterns.md) DROP, quoting tighter or deeper does not pay; SIG convergence (sig_convergence.md) DROP
  as a trade (deviation half-life 1-1.7 days), gap-prediction follow-up running; more trades (more_trades.md): nothing
  new clears the bar. FOLLOW-UP for the daily run on 26 Oct: if high-frequency arb has >= 20 exits and earns >= +5 per
  1k of capital per day (and the Fable test-environment check passed), ask Sizhe whether to raise its pot to 24k funded
  by selling late-call sets at cheap moments.

## Done
- 27 Oct: GAP TRADES (rank legs by SIG vs outside gap, sell before the punt): DROP. Gap-closing edge +1c net in sample (t 4-5) flips to -0.3 to -0.9c out of sample (t down to -4.9). Held-through-call version untestable on SIG history; cheap favourites already covers it live. Note: gap-trades-handover.md.
- 23 Oct: Directional trades sized by market impact / 6+ tick gap held 12 h: NEGATIVE. New day out of sample -0.49
  ticks/share (t -1.1, n 100); pooled out of sample -0.48 (t -3.7, n 2,184); no variant positive with t > 2. The 21-22 Oct
  night was one good night. Not built. (research_notes/directional_impact.md, "Re-test 2026-10-23")
- 23 Oct: Election-night capital plan: written for Sizhe's return. Unwinding RI Governor costs 3.75-4.75% of payout
  against 4.04% locked, so freeing cash early is selling at cost; no unwinds before Sizhe decides; schedule for 3 Nov with
  cost caps loosening through the afternoon; 30k/50k/70k liquid cost about 0.4k/1.4k/3k. (election_night_capital_plan.md)
- 23 Oct: Tilt funding: does not clear the bar; marks are zero, markouts zero, thesis only testable at settlement; no
  capital moves. (tilt_funding.md)
- (see data/changes.md and research_notes/*.md for studies before 23 Oct)
