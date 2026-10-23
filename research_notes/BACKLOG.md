# Strategy research backlog (daily research run, 23 Oct - 2 Nov 2026)

The 06:00 daily run takes the top open item(s), studies them, and updates this file: move finished items to "Done" with
a one-line verdict and a link to the note. Add new ideas at the bottom of "Open" with a one-line reason. Rank by
expected susqies gained before settlement (4 Nov 12:00 ET) per hour of work.

Bar for going live: out-of-sample positive with t > 2 (or a riskless mechanism), net of spread and our own market
impact, beats the ~3%-to-settlement return of capital left in arb sets, fits every hard limit, and passes the gate
(guard test + tests/sim_pipeline.py + precheck + Fable review + 10-min post-deploy watch).

## Open
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

## Done
- 23 Oct: Directional trades sized by market impact / 6+ tick gap held 12 h: NEGATIVE. New day out of sample -0.49
  ticks/share (t -1.1, n 100); pooled out of sample -0.48 (t -3.7, n 2,184); no variant positive with t > 2. The 21-22 Oct
  night was one good night. Not built. (research_notes/directional_impact.md, "Re-test 2026-10-23")
- 23 Oct: Election-night capital plan: written for Sizhe's return. Unwinding RI Governor costs 3.75-4.75% of payout
  against 4.04% locked, so freeing cash early is selling at cost; no unwinds before Sizhe decides; schedule for 3 Nov with
  cost caps loosening through the afternoon; 30k/50k/70k liquid cost about 0.4k/1.4k/3k. (election_night_capital_plan.md)
- 23 Oct: Tilt funding: does not clear the bar; marks are zero, markouts zero, thesis only testable at settlement; no
  capital moves. (tilt_funding.md)
- (see data/changes.md and research_notes/*.md for studies before 23 Oct)
