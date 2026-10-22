# Strategy research backlog (daily research run, 23 Oct - 2 Nov 2026)

The 06:00 daily run takes the top open item(s), studies them, and updates this file: move finished items to "Done" with
a one-line verdict and a link to the note. Add new ideas at the bottom of "Open" with a one-line reason. Rank by
expected susqies gained before settlement (4 Nov 12:00 ET) per hour of work.

Bar for going live: out-of-sample positive with t > 2 (or a riskless mechanism), net of spread and our own market
impact, beats the ~3%-to-settlement return of capital left in arb sets, fits every hard limit, and passes the gate
(guard test + tests/sim_pipeline.py + precheck + Fable review + 10-min post-deploy watch).

## Open
1. Directional trades sized by market impact: Opus study done 23 Oct (research_notes/directional_impact.md). Verdict:
   do not scale; 6+ tick gaps held 12 h lose -0.62 t/share out of sample (t -4.9); only the single night of 21-22 Oct
   is positive (+1.65 t, t 2.9). Impact is small (0.046 t per 1,000 sh past the touch), the edge is the limit. TODO:
   Fable double-check of the analysis; re-test the 6+ tick / 12 h trade on each new day of live quotes (paper only)
   and go to the spec's gated trial only if it stays positive with t > 2 over 3+ more days.
2. Catch-up trade (volatile_moves.md (c)): +5.5 ticks on 10 trades from one night. Evaluate the paper log
   data/volatile_paper.jsonl once 30+ signals over 3+ nights exist; go live small only if it clears +2 ticks.
3. Election-night capital plan: election_night.md wants ~10k per race on 3 Nov evening. Model how much arb capital to
   free (and when, via free unwinds/recycles) before 3 Nov 18:00 ET versus holding sets to 4 Nov. Output a plan for
   Sizhe's return; the election sleeve itself stays OFF until Sizhe switches it on.
4. MM size: the 4x replay failed; re-test 2x vs 3x on the latest markouts (tracker, review --json) and the
   quote-sizing note (research_notes/Market maker quote sizing by depth).
5. Free-unwind / recycle thresholds: are we leaving money in sets whose sale value is within 0.2% of payout?
   Measure how often books come close, and the value of a slightly lower threshold.
6. Tilt (favourite/longshot) funding: tilt is capital-starved. Measure its realised edge per sleeve P&L and whether
   it beats arb sets per unit of capital per day.
7. Favourite drift into resolution (favourite_drift.md): re-check with the latest week of data.
8. Late-punt screener refresh (proposals/punt_screener.py): update the candidate list for Sizhe's 3 Nov decision.
   Never place the punt.

## Done
- (see data/changes.md and research_notes/*.md for studies before 23 Oct)
