# Directional alpha on SIG: 40 new hypotheses (31 Oct)

None of these repeats H01-H31 in `hypotheses.md`. Nothing here is a tested result. The only numbers are the "Quick look" notes,
which are raw, unclustered first looks made to confirm the data exists. They are not evidence of edge.

**Sort.** Score = prior x payoff, highest first. Prior 1-5 = how likely the effect is real. Payoff 1-5 = what it pays if real:
5 is 3c or more on 20k+ notional inside hours, 3 is about 1-2c on 10k+, 1 is about the spread.

**Shared definitions**
* mid = (`sig_bid` + `sig_ask`) / 2, gap = mid - `fair`, both from `venue_fair`. Keep polls where `pm` and `kalshi` agree within 4 pts unless a test says otherwise.
* Net = buy at `sig_ask` of the next poll, sell at `sig_bid` at the horizon (NO side mirrored), i.e. the full spread.
* Leg map: `venue_fair` gives `exchange_id` -> (`race`, `party`). Office comes from `data/race_map.json`, poll close from `data/election_night/poll_close_2026.json`.
* Signed flow = `tape.quantity` x +1 (`aggressor`='buy') or -1 ('sell'), NULLs dropped, our own fills removed via `fills`.
* ET = UTC-4 until 1 Nov 06:00 UTC, UTC-5 after that (clocks change on 1 Nov).
* Chamber legs: 840 House D, 841 House R, 842 Senate D, 843 Senate R.

**Data warnings found while checking**
* `shock_playbook.md` says SIG has no chamber market. It does, four legs, and they are among the busiest: over 1M shares a day in the Senate legs on 30 Oct.
* Tape signs look doubtful on chamber legs: House D shows +506k net buying on 30 Oct with the price flat at 0.855-0.86. `aggressor` is classified against our last quote snapshot, so check it there before relying on flow.
* Election-night ideas (A2, A3, A6, A20, A22, A28) can only be tested on 2022 and 2024 outside prices. They have no SIG history.
* `data/research_diralpha/r1.log` and `r3.log` show `lab.py` crashing at `P["fair"].between` (AttributeError: `fair` is a DataFrame, likely a duplicate column). This was the state at 01:00 UTC; I did not change it.

---

## Ranked list

**A1. Chamber pair: the two chamber gaps have split apart. Prior 3, payoff 5, score 15**
* Mechanism: chamber legs are a lottery for chasers and a hedge for leaders, so they clear on SIG's own flow; the House and Senate gaps cannot both be right, and a pair is close to neutral on party.
* Test: S = gap(840) - gap(842). Buy 840 and 843 at equal notional when S < -5c, and the reverse when S > +5c. Exit at half of S or at 6 / 24 / 72 h, and also mark at the first big outside move on 3 Nov.
* Quick look: S went from about -1c on 26 Oct to -14c on 31 Oct (House D 0.8575 vs 0.936, Senate D 0.7575 vs 0.698). It is one path, so the sample is one or two independent events.

**A2. Poll-close read-through to legs still voting. Prior 3, payoff 5, score 15**
* Mechanism: early closers (IN/KY 18:00, FL/GA/VA 19:00, NC/OH 19:30) reveal the national swing, and SIG's DMM walks only 0.2-0.5 pt/min on legs that close at 21:00-23:00 ET.
* Test: in `paths_2024.csv` / `paths_2022.csv`, regress the logit change of legs closing >= 21:00 ET over 19:30-21:00 on the party-signed move of legs closing <= 19:30 over 19:00-19:30. Trade: buy the side of the early move on late-closing SIG legs, exit at those legs' poll close, using the SIG copy kernel for the entry price.

**A3. First-called seats as a live swing estimate. Prior 3, payoff 5, score 15**
* Mechanism: the first called races against their pre-close prices give a surprise score that may predict all later races better than prices do in the first hour.
* Test: from `race_calls.csv` and `legs_2024.json` / `legs_2022.json`, surprise_k = mean(winner=D - pre-close D price) over the first k calls. Regress later legs' (outcome - price at the k-th call) on surprise_k. Direction: buy the surprise party in uncalled legs, hold to their close.

**A4. Stale clock: outside change accumulated since the last SIG step. Prior 4, payoff 3, score 12**
* Mechanism: `shock_playbook.md` found SIG copies changes, not levels; if the DMM steps once an accumulated change passes a threshold, the variable is the change since its last step, not a fixed lookback (H08).
* Test: for each leg, A = `fair` now - `fair` at the last poll where mid changed. Fit P(step in the next 1 h) on |A| and hours since the step, find any threshold, then enter at |A| just below it on A's side. Horizons 30 min, 2 h, 6 h, net.

**A5. Chamber legs copy big moves slowest. Prior 3, payoff 4, score 12**
* Mechanism: chamber legs are the only place 20-60k goes in within 3 ticks; if their DMM is slower than on seats, a national shock is best traded there.
* Test: for moves of 2+ pts in `fair` on 840-843 in 10 min, measure SIG's copy at 10 / 30 / 60 / 240 min against the same measure on seats, and net follow at 60 min and 4 h.
* Quick look: Senate D 1 h changes correlate -0.06 with `fair`, and the SIG change SD is 0.65 pts against 0.31 outside.

**A6. Count-order mirage, by state. Prior 3, payoff 4, score 12**
* Mechanism: each state's first batches lean a known way (PA and WI late mail D, AZ early D, FL fast); outside prices may overreact to the first batches, and the DMM copies them late.
* Test: in `paths_2024.csv` / `paths_2022.csv`, take the first 3+ pt move in the 60 min after each state's close; group by state count type (hand-coded before looking). Does it reverse by +3 h? Trade: fade once SIG has copied half of it, exit 2-3 h later.

**A7. Chaser bid: band-level momentum in the compression. Prior 3, payoff 4, score 12**
* Mechanism: chasers needing 8-10x buy 15-35c legs and fund that from favourites; unlike H22, which expects compression to unwind, this expects it to widen until the punt deadline.
* Test: every 6 h, band gap = mean gap for `fair` in .15-.35, and for > .85. Signal: 24 h change in band gap plus band signed notional. Long the 15-35c legs with the largest signed notional, 24 h and to 3 Nov 18:00 ET, reporting mid-minus-fair return as well as net.
* Quick look: .15-.35 gap +2.06c on 28 Oct, +3.39c on 30 Oct, +3.09c on 31 Oct; > .85 gap -3.05c, -3.62c, -3.25c.

**A8. Ladder depth imbalance. Prior 3, payoff 3, score 9**
* Mechanism: the DMM's depth within 5 ticks may show where it is willing to hold inventory, and so where its quote goes next.
* Test: from `data/books_history/`, I = (bid depth - ask depth) / sum, within 5 ticks of the touch. Long I > 0, short I < 0, measured to the next snapshot and to 6 / 24 h via `venue_fair`. Remove our own resting size first, and cluster by race and snapshot.
* Quick look: raw corr with the next snapshot's mid change +0.12 (n 5,382, legs .1-.9). This could be mechanical.

**A9. Heavy-level centre as the DMM's inner fair. Prior 3, payoff 3, score 9**
* Mechanism: the touch may lag while the big deep levels (5k-20k+) are placed around the DMM's real fair.
* Test: C = (highest bid level >= 20k + lowest ask level >= 20k) / 2 - mid, from `data/books_history/`. Trade the sign of C when |C| >= 1c, 6 / 24 h.
* Quick look: raw corr with the next snapshot's mid change +0.105, SD of C 0.53c. Overlaps A8.

**A10. Copy asymmetry by direction. Prior 3, payoff 3, score 9**
* Mechanism: persistent compression suggests the DMM copies moves toward 0.5 faster than moves away, and moves that widen a gap differently from those that close it.
* Test: for 2+ pt `fair` moves, copy at 30 / 60 / 240 min split by (a) toward or away from 0.5, (b) closing or widening the gap. Net follow only in the slow cell.

**A11. DMM attention by clock. Prior 3, payoff 3, score 9**
* Mechanism: if the DMM is supervised or its parameters are set in office hours, nights and weekends copy slower; 1 Nov is the last weekend and final polls land on it.
* Test: same copy measure by ET hour bucket and weekday/weekend (weekends 24-25 Oct, 31 Oct-1 Nov). Follow outside moves only in the slowest bucket, 1-4 h. H20 tests drift by hour, not the copy rate.

**A12. Skilled signature screen. Prior 3, payoff 3, score 9**
* Mechanism: bots are identifiable by size and second of minute (`bot_exploits.md`); one of 10-12 may be informed at hours horizons even if the average print is not.
* Test: cluster prints by (`quantity` bucket, second of `executed_at`). Rank clusters by 6-24 h mid markout on 21-27 Oct, then follow the top decile's 6 h net flow on 28 Oct onward. Permutation test on cluster labels.

**A13. Clean flow: directional pairs only. Prior 3, payoff 3, score 9**
* Mechanism: buy D + sell R in one action is conviction, while same-side pairs are set arbs, which pollute plain signed flow (H02).
* Test: match prints on the two legs of a race within 3 s and size within 10%. Signal: net 6 h directional-pair notional. Follow 6 / 24 h. Separately rerun H02 with set-like pairs removed.
* Quick look: 29 Oct has 1,317 opposite-side pairs against 1,873 same-side pairs, so the sample is real.

**A14. DMM reference model: offset plus slow average. Prior 3, payoff 3, score 9**
* Mechanism: the gap persists because the DMM may quote a fixed leg offset around a slow average of outside, in which case its next quote can be forecast.
* Test: per leg on a trailing 48 h, fit mid = a + EMA(`fair`, tau) with tau by grid search. Trade when predicted minus actual mid >= 2 ticks. Horizon about tau / 2, out of sample only.

**A15. Fund-the-punt selling in favourites. Prior 3, payoff 3, score 9**
* Mechanism: chasers must free cash from .85+ legs by 3 Nov, and that selling carries no information.
* Test: hourly signed notional in `fair` > .85 legs, excluding our own. When the 6 h sum is below -2 SD, buy the deepest such legs, sell 12 / 24 h later. Watch from 1 Nov.
* Quick look: band flow is -535k on 27 Oct and -55k to +273k on other days, so no trend yet.

**A16. Fast-count premium. Prior 3, payoff 3, score 9**
* Mechanism: chasers rolling cash on the night prefer legs that settle fast (`punt_strategy.md` screens the same way), so fast-count legs get richer against slow ones (AK, CA, ME, AZ, NV, OR, WA, UT).
* Test: within each price band, daily gap(first close <= 20:00 ET) - gap(slow states). Long fast, short slow, from 1 Nov to 3 Nov 18:00 ET, with 26-30 Oct as a placebo.

**A17. Chamber fade after a flow-only move. Prior 2, payoff 4, score 8**
* Mechanism: ev_hunt 1a found SIG-only moves stick, but those were DMM repricings across seats; chamber moves are print-driven (29 Oct 11:33: 107k sold in 44 prints walking House D .895 to .865), so they may behave differently.
* Test: on 840-843, |mid change| >= 1.5c in 2 h with |`fair` change| < 0.5c. Fade, hold 6 / 24 / 48 h. Expect few events; report each one.

**A18. Forecast anchor behind the DMM. Prior 2, payoff 4, score 8**
* Mechanism: the structural gap may come from a model (polls, ratings) blended into the DMM's fair; if so, new model inputs move SIG even when outside is still.
* Test: cross-section: regress gap on (poll-average implied probability - `fair`) from `data/fair/votehub_*.json` and on the wiki ratings. Time series: change in gap in the 24 h after a new poll's `created_at`. H29 tests polls against price; this tests whether the gap loads on them.

**A19. Party basket punts. Prior 2, payoff 4, score 8**
* Mechanism: a chaser wants one correlated outcome, so punts arrive as same-party underdog baskets.
* Test: 6 h signed notional in legs with mid < .40, by `party`, across at least 5 races. Follow with that party's underdog basket vs the other party's, 12 / 24 h. Also: 24 h change of (D gap - R gap) in .35-.65 legs to the next 24 h.
* Quick look: .35-.65 D gap +1.41c on 27 Oct to +0.17c on 31 Oct; R gap -0.53c to +0.55c. Size is limited by the 5,000 partisan cap.

**A20. Pre-close chaser run-up on 3 Nov. Prior 2, payoff 4, score 8**
* Mechanism: chasers delay punts to keep cash, so popular legs may rise into 19:00 ET with no outside move.
* Test: gap change by leg, 12:00-18:30 ET. Use 1-2 Nov as dry runs; the real sample is one day. Buy the most-traded 15-35c legs at 12:00 ET, sell by 18:30 ET. Forward test only.

**A21. Leaders' national exposure from their P&L. Prior 2, payoff 4, score 8**
* Mechanism: regressing a leader's balance changes on a national factor estimates their book's beta without seeing positions, which shows which way crowded unwinds would go.
* Test: leaderboard reads every 5-15 min from now on (done by the bot, not in this study), regress change in `pnl` on party-signed basket returns. Stored data has only a few reads (`data/research_more_trades/lb_*`, 23 Oct), so it cannot be tested yet.

**A22. Exit-poll rumour fade. Prior 2, payoff 3, score 6**
* Mechanism: 17:00-19:00 ET has rumours and no counts; moves in that window may reverse before results arrive.
* Test: in `paths_2024.csv` / `paths_2022.csv`, leg changes 17:00-19:00 ET against the next 1-2 h for legs not yet closed. Fade on SIG only where the DMM has copied.

**A23. Residual from the compression curve. Prior 3, payoff 2, score 6**
* Mechanism: gap has a stable shape in `fair`, so only the distance from that curve is mispricing; H07 uses each leg's own history, this uses the cross-section.
* Test: at each poll, fit gap = f(`fair`) by office across legs, residual R. Trade R >= 3 ticks toward the curve, 24 / 48 h.

**A24. Night outside moves are noise. Prior 3, payoff 2, score 6**
* Mechanism: thin overnight outside books give moves that revert, so copying them is worth less than copying day moves.
* Test: `fair` moves of 2+ pts with both venues, split 00:00-07:00 ET against 09:00-18:00 ET; retained share at 12 h, and SIG follow net. A filter rather than a trade.

**A25. Touch-size depletion. Prior 2, payoff 3, score 6**
* Mechanism: the DMM may shrink one side's touch as it nears an inventory limit, ahead of a step; H24 looks at one-step timing, this at a slow change in size.
* Test: `quotes.bid_qty` / `ask_qty` 6 h median over the leg's own 5-day median. Trade toward the thinner side, 6 / 24 h. `quotes` covers only our markets.

**A26. Ladder bots running out of cash. Prior 2, payoff 3, score 6**
* Mechanism: when a resting bot (100/300/600, 666) leaves one side as its capital goes into punts, that side thins and the price drifts through it.
* Test: per snapshot in `data/books_history/`, count known bot sizes per side. A one-sided drop > 50% between snapshots predicts mid toward the thinned side, 6 / 24 h.

**A27. Regional second factor. Prior 2, payoff 3, score 6**
* Mechanism: the DMM may track each leg but miss shared regional or demographic moves below the national factor (`data/correlation_matrix.csv`).
* Test: PCA on 1 h `fair` changes, drop PC1. For PC2-PC5, the leg's factor-implied move minus its own SIG move, follow 6 / 24 h.

**A28. Uncalled favourites before trading closes. Prior 2, payoff 3, score 6**
* Mechanism: 4 Nov 12:00 ET ends SIG trading; the DMM may widen on uncalled legs, so they trade below outside.
* Test: from `paths_2024.csv`, which uncalled legs at 06:00 ET were at .85+, and how did they settle? Live: gap on uncalled legs 06:00-12:00 ET on 4 Nov, held to settlement. No hours-horizon exit exists.

**A29. End of early voting. Prior 2, payoff 3, score 6**
* Mechanism: final early-vote reports (NV, NC, GA, AZ) come as dated narrative; markets may overreact on party-registration headlines.
* Test: `news_items` titles with "early vot" (100 found), matched to state. In the 12 h after each, fade the outside move on that state's legs, 24 h, against same-time legs elsewhere.

**A30. Attention premium. Prior 2, payoff 3, score 6**
* Mechanism: entrants crowd famous races, so high tape share goes with rich prices that fade once flows stop.
* Test: per race, 24 h share of tape notional vs its 5-day average. Top decile against the bottom: next 24 h change in gap.

**A31. Odd-size human flow. Prior 2, payoff 2, score 4**
* Mechanism: students trade non-bot sizes at evening hours and may chase news late.
* Test: prints with `quantity` outside the known bot sizes (`bot_exploits.md`) and at 18:00-01:00 ET. Sign of 6 h net flow, fade or follow by split, 24 h.

**A32. Independent legs carry a story premium. Prior 2, payoff 2, score 4**
* Mechanism: a named independent is a cheap, famous punt, so I legs stay rich.
* Test: party='I' legs (Nebraska Senate 0.37 vs 0.31-0.33, Montana Senate). Gap and signed flow over time; buy NO on the I leg when its gap is >= 3c, 24 / 72 h. Only two legs.

**A33. Races with no I leg. Prior 2, payoff 2, score 4**
* Mechanism: D + R should sum to 1 - P(I) (`contract_rules.md`), and the DMM has no I book, so a rising independent may not reach SIG's D and R legs.
* Test: from `venue_quotes` where possible, P(I) = 1 - PM D - PM R for ID Sen, SD Sen, ME Gov, TN Gov, AK Gov. Does a 1+ pt rise predict D and R mids falling over 6 / 24 h?

**A34. Same-state House clusters. Prior 2, payoff 2, score 4**
* Mechanism: seats in one state share news and rules; H16 covers Senate and Governor only.
* Test: for states with 3+ House legs, leg gap minus cluster mean gap. Trade toward the cluster, 24 h.

**A35. Points against logit. Prior 2, payoff 2, score 4**
* Mechanism: if the DMM moves legs by equal points on a national move, not equal logits, mid-priced legs undershoot and tail legs overshoot.
* Test: on hours when the party basket moves 1+ pt outside, SIG change against logit-implied change by price band; follow the undershoot, 6 / 24 h.

**A36. Triage and ad-cut news on House seats. Prior 2, payoff 2, score 4**
* Mechanism: outside House seat markets are thin, so committees pulling ads may not be priced at once.
* Test: `news_items` title regex (cancel, pull, ad spending, reservation: about 59 rows before dedupe), matched to House legs. Next 24 h `fair` and mid change in the implied direction. Separate from H28's general news labels.

**A37. Legs where the favourite differs by venue. Prior 2, payoff 2, score 4**
* Mechanism: entrants who watch outside may react most when SIG names a different favourite.
* Test: mid > .5 with `fair` < .5, or the reverse. Buy the outside favourite, 24 / 48 h, against same-size gaps without a flip.

**A38. Rebound after our own wind-down. Prior 2, payoff 2, score 4**
* Mechanism: our wind-down from 31 Oct is a known, uninformed flow, so legs we sold may recover when we stop.
* Test: from `fills`, legs with our sells in the top quartile over 6 h; gap change over the following 6 / 24 h, against unsold legs at the same price. If real, it affects the punt's choice of legs.

**A39. Round-number polling averages. Prior 1, payoff 3, score 3**
* Mechanism: students may react when a candidate's average crosses a lead of 0 in VoteHub, even with no real change in odds.
* Test: compute each race's average from `data/fair/votehub_*.json`, mark sign flips of the lead, change in gap over 24 h. Dates are daily only.

**A40. Final rating calls on toss-ups. Prior 1, payoff 3, score 3**
* Mechanism: forecasters must drop toss-ups in their last update, which entrants may treat as news.
* Test: `news_items` titles with "rating", "toss" or "moves to" (about 73 rows before dedupe) on 1-3 Nov against pre-window ones; follow the rating move on SIG, 12 h.
