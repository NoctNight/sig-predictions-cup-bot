# New strategy ideas, overnight 24 Oct 2026 (brief)

I checked each idea against the Done list in BACKLOG.md and the linked notes, and dropped anything already tested
(directional, time of day, footprints, cross-race pairs, long vol, maker arb, cheap favourites, flow patterns,
convergence). Only data already on disk was used. No SIG calls were made.

| # | idea | verdict |
|---|---|---|
| 1 | Shock mode: scale the catch-up sweep by the size of the outside move | **build** (spec in shock_playbook.md section 5) |
| 2 | Sell at the refilling touch instead of walking the book for the capital raise | **paper, test in the first live unwind** |
| 3 | Absorb rivals' pre-punt liquidation, 31 Oct-3 Nov | **paper** (watch from 31 Oct) |
| 4 | Weekend lag: does the DMM copy outside moves more slowly on weekends? | **paper** (measure this weekend, 24-25 Oct) |

**1. Shock mode.**
* Outside shocks of 10-20 pts are worth +0.5k to +4.3k per leg at central calibration (+3k to +9k uncalibrated),
  because SIG's DMM copies only 29% of an outside move in 10 min and refills swept levels at the same price.
* The live catch-up sweep fires on such moves, but at about 1% of the max-EV size: 300 notional, the touch only, and
  the partisan cap applies.
* Tested here on four 2024 shock paths × the measured lag kernel, calibrated on SIG's own 3+ and 5+ pt moves. Profit
  was positive on every path, entries from 5 min to 4 h.
* EV before 3 Nov is about +0.5-1.5k, with a +5-20k right tail. Building it costs one gated deploy.
* **Build**, if Sizhe exempts `purpose='shock'` from the partisan cap.

**2. Sell at the refilling touch, not by walking the book (capital raise, cash objective).**
* The capital-raise plan prices unwinds at a 3.75-4.75% walked exit cost.
* But the DMM refills a swept level at the same price in 3.7 s and does not re-centre on prints (bot_exploits.md).
* If that also holds for sells at the bid, selling 2k shares at the touch every ~5 s would unwind about 1.4M shares an
  hour at a 1-tick cost instead of walking 4-6 ticks.
* On ~95-100k of positions to liquidate, the saving is roughly 1.5-3k S. That is more than any other idea here, and
  it is nearly riskless.
* Not testable offline: the recordings show refills after others' buys, not after large sells of ours.
* **Paper → test in the first live unwind.** Sell one 2k clip at the touch and record whether the level comes back at
  the same price within 10 s. If it does, cap the unwind's per-clip size at the touch quantity and drop the walk. This
  should go to whoever builds the capital-raise routine.

**3. Absorb rivals' pre-punt liquidation (31 Oct-3 Nov).**
* Every serious rival is probably raising cash for an election-night punt, as we are.
* That means one-sided selling into SIG's bids in the final days. SIG prices of held favourites would sag below the
  outside fair, and taker sell flow would dominate the tape.
* Our MM's bid side would then earn more per fill. The markouts stay positive if the sag is flow, not news.
* This conflicts with the cash objective: buying adds positions. It only works as a round trip (buy the sag, sell back
  within hours as the DMM re-centres) and must be flat by 12:00 ET 3 Nov.
* No data exists yet; the tape so far is two-sided (flow_patterns.md).
* **Paper:** from 31 Oct, the daily brief reports taker sell/buy imbalance and the SIG-minus-outside gap on
  favourites. Act only if the imbalance exceeds 60/40 for a day and the gap widens by more than 2 ticks.

**4. Weekend lag.**
* hft_tactics_2 left weekends untested, and every lag measurement (bot_exploits, outside_stable_sweep) is weekday-heavy.
* If the DMM is slower on weekends, the catch-up sweep's edge per share is larger then, and its cap could be raised
  on Saturdays and Sundays.
* Data: the catch-up sweep's own fills over 24-25 Oct, plus `venue_quotes`. Compare the share of each outside move
  SIG has copied at 10 and 30 min against the weekday kernel (29% / 44%).
* **Paper:** a one-line query on Monday 26 Oct. Act only if the weekend copy at 10 min is under 15% with n >= 10.

Not listed because they are already covered:
* the election-night lag (election_night.md);
* two-leg set arbs (arb.py; new sets are off under the cash objective);
* news-feed front-running (news_history study);
* Polymarket-only follow (hft_strategies: reverts 50-76%).

## Round 2 (24 Oct 00:30-01:10 UTC): Fable (high) generated 8 ideas, Opus tested them on data already on disk

| # | idea (Fable) | test | result | verdict |
|---|---|---|---|---|
| F1 | Lag-informed stage-1 pick: buy the punt leg whose outside price jumps soon after polls close | study_2024.json, legs 0.15-0.45 at close, jump of 5-15 pts by 30/60 min | Only 1-5 qualifiers per night; win rate 0-50% vs 21% base (n ≤ 5). Side note: 2024 underdogs priced 0.20-0.40 at close won 21% vs a 31% price (n 33, not significant) | **drop** |
| F2 | Sweep the refilling touch every ~4 s instead of walking (punt and unwinds) | feed_capture_20261022.jsonl, 139 full touch sweeps | Same price back within 20 s only 24% of the time; 73% the touch moves a tick away. For 1,000+ share sweeps, 5 came back and 8 were gone | **drop** (also kills my item 2 above) |
| F3 | Unwind only the longshot legs of sell-sets | RI Governor live books | The overround is paid on every leg closed: 1.84 per set now plus the residual NO-D at 0.085 later = 1.925, the same as a full unwind | **drop** |
| F4 | Route lag trades to the cheap instrument (min(p, 1-p)) | reasoning | A short costs 1 - p and a long costs p, so this only helps when the rising leg is the cheap one. Before the election, cash does not bind; walking cost does | tie-breaker only |
| F5 | After the punt, top up called winners still quoted ≤ 0.98 | n/a | Already the election-night sleeve's safe-seat slice (+4.7k in its simulation) | covered |
| F6 | Rest just inside the DMM's stale quote to catch mechanical takers | tape, 58 lag events of 2+ pts | Against-move takers traded 66k shares vs 161k with the move, about 1.1k shares per event-hour. Worth ~100-200 S/day; the MM's outside-fair gate partly captures it | **paper** |
| F7 | Fade rivals' punt flow, 15:00-19:00 ET 3 Nov | untestable before then | | **paper** (watch 3 Nov) |
| F8 | Poke the DMM with small prints to speed its requote | hft_tactics_2 A: requotes go toward fair only 55-66% of the time | | **drop** |

**New from this round (Opus): a fast shock detector + shock mode → build.** On SIG's own 5+ pt outside moves, each
minute of entry delay costs about 0.5-1 pt/share (shock_playbook.md section 0). A 5-10 s batched Polymarket
`/midpoints` poll costs about 0.1 s per call. It is worth about +1.5-3.5k S (up to +3-7k with both legs) to 3 Nov together with shock-mode sizing.

## Round 3 (24 Oct ~04:20 UTC): Fable (high) ideas for pre-election round trips, tested by Opus

| # | idea | test | result | verdict |
|---|---|---|---|---|
| R1 | Fire shock mode on single-venue jumps (Polymarket-only or Kalshi-only) | venue_fair 22-24 Oct, 3+ pt moves by venue (`overnight_study.py venues`) | Polymarket-only (n 11): SIG copied 8% by 60 min, P&L +0.06 pt (t 0.2). Kalshi-only: n 0. Both venues (n 6): copied 43-58%, +2.3 pts at 60 min (t 2.0). SIG follows the blend | **drop**; keep both-venue confirmation |
| R2 | Cap-neutral leg pair: short the lagging leg, hedge with the walked one | D+R sum around 7 events of 3+ pts | D+R moved only 0.2-2.5 pts from baseline and did not close back within 60 min; the legs move together | **drop** |
| R3 | Exit by resting asks 1-2 ticks ahead of SIG's walking bid | needs our fills inside walk windows (none yet) | | **paper**: an exit option for shock mode, with a 60-min taker stop |
| R4 | Unwind held inventory only after a favourable 3+ pt move in that race | | Events per held race are rare (~0.45/day across all races for 5+ pt) | **paper** |
| R5 | Size by whether the move widens or narrows SIG's static gap | | n too small | **drop** |
