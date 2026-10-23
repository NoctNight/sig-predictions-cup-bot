# Strategy research backlog (daily research run, 23 Oct - 2 Nov 2026)

The 06:00 daily run takes the top open item(s), studies them, and updates this file: move finished items to "Done" with
a one-line verdict and a link to the note. Add new ideas at the bottom of "Open" with a one-line reason. Rank by
expected susqies gained before settlement (4 Nov 12:00 ET) per hour of work.

Bar for going live: out-of-sample positive with t > 2 (or a riskless mechanism), net of spread and our own market
impact, beats the ~3%-to-settlement return of capital left in arb sets, fits every hard limit, and passes the gate
(guard test + tests/sim_pipeline.py + precheck + Fable review + 10-min post-deploy watch).

## Open
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
