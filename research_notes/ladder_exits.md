# Ladder exits: sell arb sets in steps at better prices, or all at once? (research, 24 Oct 2026)

Read-only study for Sizhe's idea: exit a set in increments, selling part at each better price level and holding the rest in
case the price moves strongly in our favour. Nothing under `sigbot/`, `config.json`, `params/`, `tests/` or `deploy/` was
touched. The bot was not stopped, no orders were sent and there were **0 venue API reads**: everything uses local data.

* Scripts:
  - `research_notes/ladder_exits_study.py`: about 20 min at nice 5.
  - `--pool-only` re-runs section 3.
  - `--figures` redraws the figure, from `research_notes/ladder_exits_figures.py`.
  - `research_notes/ladder_exits_hf.py`: the high-frequency arb part, about 1 min.
* Outputs: `data/research_ladder_exits/results.json` and `hf.json`, about 0.1 MB.
* Figure: `research_notes/figures/ladder_exits_decision.png`.

**Status: final. The Fable double-check (section 7) says the conclusion holds with changes, which are applied here.**

## Bottom line

* **Hypothesis.** A set's exit price can revert against us on every leg at once (Sizhe's RI Governor worry). Selling part
  at each better level and keeping a remainder for a big favourable move should then beat one clip.
* **What holds up on real quotes: never sell a big set in one go. Sell it in clips, and only when the price is at least
  as good as the race's usual level.**
  * **Clips save money.** One market order for the whole position walks deep into the book. 1,000-set clips every
    30 min instead save, per set (training-day / test-day swings):
    * RI Governor (12,304 NO sets): 2.7 / 3.8 ticks.
    * MN-05 (21,162 YES sets): 2.6 / 4.7 ticks.
    * Hawaii Governor (7,658 YES sets): 2.0 / 2.1 ticks.
    * That is about $0.5-0.8k on the three, against a one-shot sale. The whole sale takes 4-11 h.
    * On 2-leg races, the 0.43 tick per set lost by a one-shot 2,000-set sale is simply the extra book walk of 2,000
      sets over 500 (from the snapshots). It is a mechanical cost, not a statistical finding.
    * Tier 2 of the cash raise already clips at ≤ 80% of the thinnest touch, so the saving against the live rule is much
      smaller. The point is to never exit in one shot anywhere else: a manual sale or the wind-down.
  * **Selling a clip only when its exit cost is at or below the race's median of the last 24 h** ("level 0") adds a
    little more:
    * +0.09 to +0.16 tick per set on 2-leg races on the test day. It was also the best level on the training day, but
      quote outages leave only one 5-hour evening (22 Oct 18:00-22:00 UTC) of usable training start times.
    * The t of 4.6-6.3 measures how consistent the gain is across races within one day. Every start hour is positive,
      but there are only 2 independent days. It is cheap to adopt, but it is not a proven edge across days.
    * It is positive for all three of our sets in every simulated case. It is small: about $0.2k across the three on
      test-day swings, more if a race swings the way MN-05 did on 22-23 Oct.
* **Sizhe's ladder (hold part for 1-4 ticks better, sell the rest at the deadline): not proven, so no ladder
  parameters.**
  * **Real quotes, 6-12 h horizons.** Levels 1-4 ticks better than usual lose against level 0 (t -1 to -10). The best
    ladder (half at level 0, half at 2 better) only ties (-0.00, t -0.1) and takes twice as long (6.0 h vs 2.8 h).
  * **Simulated 10 days to the deadline.** Waiting does pay a little, but slowly:
    * Holding out for 2 ticks better earns +0.6 tick per set over level 0 on 2-leg races (test-day swings).
    * The typical wait is about 18 h for 2 better, but the **average set** comes out after 95 h, against 16 h at level
      0. The 21% of paths that never reach the level are sold at the deadline. Its worst 5% is -4.4 ticks.
    * Ladders land between level 0 and the single better level on mean, tail and time. None beats the best single
      level.
    * These t-stats are simulated: there is no real-quote test beyond 25 h.
    * The simulation counts level drift twice (once in the swings, measured against a lagging median, and once in the
      added daily drift). It is therefore more volatile than the data, which flatters ambitious levels.
    * It also assumes the level drifts with no bias. That is optimistic for races we hold *because* they were cheap:
      both of our buy-set races have already drifted 3-7 ticks against us since 22 Oct.
  * **Cash timing outweighs the gain.** Take r = 0.5% a day for freed cash.
    * The source is `arb_convergence.md`: deviation churn beat holding by +5 to +11 per 1k of capital a day on test
      quotes, while capital was short.
    * At that rate, 3-4 extra days cost about 3-4 ticks per YES set, five times the +0.6 tick gained.
    * When no sleeve needs the cash, r is about 0 and the trade-off is only the tail risk below.
  * **Election eve (2-3 Nov) is not in the data.** Wider spreads or faster moves then argue for finishing sales early,
    not holding a remainder to the deadline.
  * **In dollars on our three sets** (simulated, 2,000-set clips, test-day swings): 2 better vs level 0 is RI +$43,
    MN-05 +$63 and Hawaii -$27. The worst 5% of the ambitious levels are -9 to -13.5 ticks, which is $0.5k on RI
    and up to $1.4k on MN-05.
* **Sizhe's adverse case is real, and it is a level move, not a fast spike.** How far the exit cost rose while we waited
  (95th percentile):
  * 3-leg races (RI): 7.8 ticks within 24 h on real quotes, and up to 20 ticks over 2-10 simulated days.
  * The 3-leg simulation has only 3 level paths, one of them RI's own 14-16 Oct move from 30 to 15 ticks, applied
    with a random sign. The RI tails are therefore "what if RI's jump recurs", not an estimate.
  * For 12,304 RI sets, 8 ticks is about **$0.5k** and 16 ticks about **$1k**.
  * Our two buy-set races already moved 3-7 ticks against us between 22 and 24 Oct.
  * Holding a remainder of RI for a better price carries this risk for a gain worth about $40.
* **Resting orders (passive exit): no.** Public flow on the side that would fill us is too thin for our sizes:
  * About 57k shares a day on RI's Independent leg (880), 138k on MN-05 R (1028) and 43k on Hawaii R (991). That
    includes our own prints, and we would queue behind the touch and our own market maker.
  * `maker_arb.md` measured resting exits at +0.04 tick (t 0.5). A 1,000-share joined order fills fully only 11% of the
    time within 1 h.
  * A set needs every leg filled. One leg filling alone leaves us with a directional position.
* **High-frequency arb: keep the live rule (all at +2 ticks, 6-tick stop, 24 h).**
  * Splitting the exit (part at +1, the rest at +2 or +3) changes P&L by -0.18 to +0.18 tick per set (paired t ≤ 1.5,
    22 training lots).
  * The tiers do not fill together. Of the 22 training lots: +1 then +2 in the same step 2 times, +1 before +2 7 times,
    +1 only 6, neither 7, stop first 4. The early and late exits roughly offset each other.
  * The test column has no power: all 12 lots run past the end of the data, so every policy is marked at the same
    last quote.
  * The training sample can only detect differences of 0.3-0.4 tick or more. So the evidence is underpowered rather
    than a measured zero. Keeping the live rule is the default because nothing beats it.

## Recommended parameters (for the cash-raise routine and the 31 Oct-3 Nov wind-down)

These follow from the clip-and-gate result, which is what clearly wins on real quotes. They are not a multi-level ladder.

| | value | why |
|---|---|---|
| clip size | RI ≤ 1,000 sets, MN-05 ≤ 3,000 (walk ≤ 1 tick); Hawaii 500 (walk about 1.6 ticks; its 991 bid touch is often under 100 shares, so there is no clip below 1 tick); always also ≤ 80% of the thinnest touch | depth snapshots (section 2); RI at 2,000 walks 1.3-2.6 ticks in 3 of 8 snapshots |
| gap between clips in a race | ≥ 30 min (a judgment) | no lasting touch move after our ≥ 2,000-set trades at this resolution (n = 40, SE 0.2-0.3 tick); depth refill is not measured, so the ≤ 80%-of-touch rule is the binding protection |
| gate | sell a clip only if the touch exit cost ≤ the race's trailing 24-h median | level 0 wins on both days; better levels lose on real quotes |
| urgent cash need | skip the gate for the shortfall; still clip | the wait costs about as much as the gate earns |
| wind-down 31 Oct-3 Nov | gate on from 31 Oct; per race, turn the gate off at 18:00 ET 3 Nov - (clips left x 30 min) - 2 h, then clip at the touch every 30 min regardless | with today's holdings that is about 10:00 ET (RI, 13 clips), 08:30 (Hawaii, 16 clips) and 12:30 (MN-05, 8 clips) on 3 Nov; level 0 is hit within 24 h for one clip 96% of the time, but each race needs 8-16 qualifying points 30 min apart |
| remainder held for a big move | **none** | not shown on real quotes; days of waiting; worst 5% -4 to -13 ticks |
| high-frequency arb exits | unchanged | ladder no better (t ≤ 1.5) |

## Data

| item | source | notes |
|---|---|---|
| touch prices | `venue_quotes` (SIG), 21 Oct 23:50 - 24 Oct 13:10 UTC, forward-filled on a 2-min grid | a leg > 30 min stale or a poll > 10 min old = missing (11% of points) |
| depth | `data/books_history/*.json` (8 full books, 23 Oct 23:17 - 24 Oct 13:05; two near-duplicate pairs) | walk cost beyond the touch, I(k), median over snapshots; assumed constant over time |
| touch sizes over time | not available | `quotes` covers only the 136 legs the market maker quotes, and none of our three races fully |
| our impact | `arb_trades` (349 of our taker set trades since 22 Oct) | how the traded side moves 2-120 min after |
| daily level drift | `data/research_arb_convergence/race_day_overround.csv` (14-23 Oct daily medians; 14-21 from trade prices) | lag-k change of a race's median exit cost |
| tape | `tape` 22-24 Oct | aggressor volume per leg, for resting exits |

**Split.**
* Warm-up: 21 Oct 23:50 - 22 Oct 12:00, used for the trailing medians only.
* Training (IS): 22 Oct 12:00 - 23 Oct 12:00 UTC. Quote outages (only 31% of points usable at 14:50-17:50 and 54% at
  20:50-23:50) leave 12-h start times only at 22 Oct 18:00-22:00, so for long horizons it is in effect one evening.
* Test (OOS): 23 Oct 12:00 - 24 Oct 13:10 UTC.
* Levels are always relative to the race's trailing 24-h median at the decision time, so no level is fitted on test data.

**Exit cost.** In ticks of 0.005, at the touch:
* sell-set (NO on all legs, pays legs - 1): sum of YES asks - 1.
* buy-set (YES on all legs, pays 1): 1 - sum of YES bids.

A clip of k sets costs that plus I(k).

## 1. How the exit cost moves (figure panels A-B)

| | 2-leg races | 3-leg races |
|---|---|---|
| usual exit cost, sell-set | 3 ticks | 14 ticks (RI 16-17) |
| within-day sd around the race's own level | 1.1 ticks | 2.2 ticks |
| change of the daily median after 1 / 3 / 7 days (sd) | 2.1 / 3.0 / 3.2 ticks | 4.0 / 5.7 / 6.1 ticks |
| level change between the two halves, median abs / 90th pct | 1 / 3 ticks | 1 / 1.8 ticks |

* Our buy-set races moved against us by 3-7 ticks between the halves:
  * MN-05: from -2 to +5 ticks.
  * Hawaii: from 6 to 9 ticks.
* That is the same "both legs revert against us" effect Sizhe described for RI: it is a level move, and it lasts a day
  or more.

**Chance to reach a level within h** (share of start times; 2-leg races; test half; levels relative to the trailing
median):

| level | 1 h | 6 h | 24 h | 3 d (sim) | 10 d (sim) |
|---|---|---|---|---|---|
| usual (0) | 0.78 | 0.87 | 0.96 | 0.95 | 0.98 |
| 1 better | 0.25 | 0.40 | 0.67 | 0.80 | 0.90 |
| 2 better | 0.11 | 0.23 | 0.48 | 0.62 | 0.79 |
| 4 better | 0.01 | 0.03 | 0.11 | 0.27 | 0.49 |

* The training half gives the same picture: 0.75 / 0.83 / - / 0.95 / 0.98 at the usual level.
* For 3-leg races, 2 better is reached 49% of the time within 6 h and 83% within 24 h (6 race-sides, so noisy).
* Median wait (test half, 10-day simulation), counting only paths that get there: 6 h for 1 better, 18 h for 2 better,
  64 h for 4 better.

**Adverse moves: how far the exit cost rises while waiting (95th percentile), and the change by the end (5th-95th pct).**

| horizon | 2-leg: max rise | 2-leg: end change | 3-leg: max rise | 3-leg: end change |
|---|---|---|---|---|
| 1 h (real) | 1 | -1 / +1 | 2 | -2 / +2 |
| 6 h (real) | 3 | -2 / +2 | 4 | -3 / +3 |
| 24 h (real) | 4 | -3 / +3 | 7.8 | -7.5 / +6.8 |
| 3 d (sim) | 7.8 | -5 / +5 | 19 | -16 / +16 |
| 10 d (sim) | 10.1 | -5.5 / +5 | 20 | -13.5 / +13.5 |

## 2. Walking cost by clip size (ticks per set beyond the touch, median of 6 snapshots)

| clip | RI Governor (NO) | MN-05 (YES) | Hawaii (YES) |
|---|---|---|---|
| 500 | 0.0 | 0.0 | 1.6 |
| 1,000 | 0.0 | 0.0 | 2.1 |
| 2,000 | 0.9 | 0.5 | 2.7 |
| 5,000 | 2.1 | 1.5 | 3.6 |
| whole position | 3.2 (12,304) | 5.2 (21,162) | 3.7 (7,658) |

* Hawaii's R leg (991) bid side, where a YES-set exit sells, is thin. The bid touch over the 8 snapshots was 8,452 /
  8,452 / 1,560 / 1 / 98 / 98 / 98 / 98 shares, then 2 ticks down to the next level, so even 250 sets walk 1.2 ticks.
* Our own past trades show no lasting impact on the touch:
  * Our 40 taker set trades of ≥ 2,000 sets moved the traded side by -0.25 / +0.21 / -0.20 / +0.38 / -0.30 tick at
    2 / 10 / 30 / 60 / 120 min.
  * All 349 trades: +0.07 to +0.67, which is partly reversion after entries we made on a cheap price.
  * So a 30-min gap between clips is enough at the touch.
  * Whether the deeper book refills in 30 min could not be measured: there are only 6 snapshots.

## 3. Exit policies, all 2-leg races (2,000 sets, 500-set clips, ≥ 30 min apart; panel C)

The gain is per set, in ticks, against selling a clip every 30 min from now. It is averaged per race and then over 228
race-sides; t is across race-sides. h is when anything left is sold at once.

| policy | IS h=12 | OOS h=12 (t) | OOS worst 5% | OOS mean time to cash | OOS vs level 0 (t) | OOS h=6 (t) |
|---|---|---|---|---|---|---|
| all at once now | -0.44 | **-0.43 (-16.7)** | -1.42 | 0 h | -0.59 (-17.0) | -0.43 (-17.2) |
| clips from now | 0 | 0 | 0 | 0.8 h | -0.16 (-6.3) | 0 |
| level +1 worse than usual | +0.20 | +0.11 (5.4) | -0.17 | 2.1 h | -0.05 (-3.2) | +0.07 (4.5) |
| **level 0 (usual)** | **+0.26** | **+0.16 (6.3)** | -0.73 | 2.8 h | 0 | **+0.09 (4.6)** |
| level 1 better | +0.16 | +0.10 (1.7) | -2.00 | 8.2 h | -0.06 (-1.2) | -0.10 (-2.6) |
| level 2 better | +0.02 | -0.05 (-0.8) | -2.23 | 10.0 h | -0.21 (-3.3) | -0.23 (-5.8) |
| level 4 better | -0.28 | -0.36 (-5.3) | -2.72 | 11.7 h | -0.52 (-8.2) | -0.40 (-10.4) |
| 50% now, rest at level 0 | +0.15 | +0.10 (8.3) | -0.25 | 1.7 h | -0.06 (-4.1) | +0.07 (8.2) |
| 50% now, rest at 2 better | +0.12 | +0.09 (2.9) | -1.00 | 5.0 h | -0.07 (-2.1) | +0.01 (0.3) |
| ladder: half at 0, half at 2 better | +0.22 | +0.16 (4.1) | -1.20 | 6.0 h | -0.00 (-0.1) | +0.04 (1.6) |
| ladder: thirds at 0 / 2 / 4 better | +0.05 | -0.03 (-0.6) | -1.72 | 8.6 h | -0.19 (-4.3) | -0.12 (-4.4) |
| ladder: quarters now / 0 / 2 / 4 better | +0.14 | +0.08 (2.6) | -1.09 | 5.9 h | -0.07 (-2.6) | +0.01 (0.4) |

* The full table, with 30 policies, is in `results.json` → pool.
* Race-sides share the same hours, so the t-stats overstate independence. The test half is about 25 h, so about 5-10
  reversion half-lives.
* A 24-h horizon does not fit in a 24-h training half, so h = 12 is the longest compared.

## 3b. The same 2-leg races, simulated 3 and 10 days ahead (panel C, brown)

* Each race-side gets 20 simulated paths, built as in section 4 (2-leg level drift). 228 race-sides.
* t is across race-sides of a simulation, so it measures the model, not the market.

| policy | 10 d, training swings | 10 d, test swings (worst 5%) | mean time to cash | vs level 0 (test swings) |
|---|---|---|---|---|
| all at once now | -0.43 | -0.43 (-1.5) | 0 h | -0.91 |
| level +1 worse | +0.45 | +0.24 (+0.0) | 2.5 h | -0.24 |
| **level 0** | +0.72 | **+0.48 (+0.0)** | 16 h | 0 |
| 1 better | +1.07 | +0.86 (-3.3) | 54 h | +0.38 |
| 2 better | +1.30 | +1.10 (-4.4) | 95 h | +0.62 |
| 4 better | +1.17 | +0.97 (-5.2) | 168 h | +0.49 |
| ladder: half at 0, half at 2 better | +1.04 | +0.83 (-2.1) | 51 h | +0.35 |
| ladder: thirds at 0 / 2 / 4 better | +1.18 | +0.97 (-3.3) | 106 h | +0.49 |
| ladder: thirds at 1 worse / 1 better / 3 better | +1.11 | +0.91 (-2.8) | 78 h | +0.42 |
| 50% now, rest at 2 better | +0.70 | +0.59 (-2.0) | 45 h | +0.11 |

* At 3 days the gains shrink. 1 better gives +0.13 over level 0 on test swings, 2 better +0.03, and 4 better -0.38.
* So waiting pays only with several days of runway and idle cash.
* Every ladder is a mix of level 0 and a single better level, and its results fall between theirs.

## 4. Our three sets, simulated to the 3 Nov deadline (panel D)

Simulated 10 days per race:
* Price swings: 6-h blocks of the race's own deviation from its trailing median, taken from the training or the test
  half.
* Level drift: a randomly chosen same-leg-count race's daily median path, 14-23 Oct, with a random sign.
* 120 paths per case. Clips are 2,000 sets ≥ 30 min apart; anything left is sold at once at the deadline.

Gain is in ticks per set vs clips from now (worst 5% in brackets):

| | all at once now | level 0 | 1 better | 2 better | 4 better | mean time to cash, level 0 |
|---|---|---|---|---|---|---|
| RI, training swings | -2.1 | +1.6 (+0.0) | +2.4 (+0.6) | +2.9 (+0.9) | +1.5 (-14.6) | 5 h |
| RI, test swings | -2.6 | +0.4 (+0.0) | +0.6 (+0.0) | +1.1 (+0.0) | +1.6 (-11.9) | 2 h |
| MN-05, training swings | -2.3 | +8.0 (-3.4) | +7.2 (-11.1) | +6.1 (-13.5) | +2.2 (-15.5) | 66 h |
| MN-05, test swings | -4.3 | +0.2 (+0.0) | +0.6 (+0.0) | +0.8 (+0.0) | +1.4 (-0.2) | 3 h |
| Hawaii, training swings | -1.2 | +0.1 (+0.0) | +0.8 (+0.0) | +1.7 (+0.0) | +3.4 (+1.1) | 1 h |
| Hawaii, test swings | -1.2 | +4.2 (+0.2) | +3.4 (-4.0) | +3.5 (-9.0) | +1.3 (-9.5) | 28 h |

* The best level per race flips between the two halves (MN-05: level 0 on training swings, 4 better on test swings;
  Hawaii the reverse), so a per-race level cannot be fitted from 2.5 days of quotes.
* Level 0 is ≥ 0 in all six cases.
* MN-05 (training) and Hawaii (test) show large level-0 gains because those races swung 6 ticks within that half.
* `results.json` → targets has every policy, clip size (1,000 / 2,000 / 4,000) and horizon (1 / 3 / 10 days).

## 5. High-frequency arb exits (`ladder_exits_hf.py`)

* Live entry rule: 2-leg races, mid overround ≥ 4 ticks above its trailing 24-h median, sum of bids - 1 ≥ 1 tick, one
  2,000-set lot per race, 30-min re-entry gap, 0.1 tick slippage per leg each way.
* Exits: each tier sells at its round-trip target, a 6-tick loss sells everything, and the rest goes at 24 h.
* Test-half lots are cut off by the end of the data and marked at the last touch.

| exit | IS n | IS ticks/set | OOS n | OOS ticks/set | paired vs live, IS (t) | paired vs live, OOS (t) |
|---|---|---|---|---|---|---|
| live: all at +2 | 22 | +0.16 | 12 | -0.87 | - | - |
| all at +1 | 22 | -0.02 | 12 | -0.87 | -0.18 (-1.2) | 0.00 |
| half +1, half +2 | 22 | +0.07 | 12 | -0.87 | -0.09 (-1.2) | 0.00 |
| third +1, two thirds +2 | 22 | +0.10 | 12 | -0.87 | -0.06 (-1.2) | 0.00 |
| half +2, half +3 | 21 | +0.18 | 11 | -0.93 | 0.00 | +0.09 (1.5) |
| thirds +1 / +2 / +3 | 21 | +0.12 | 11 | -0.99 | -0.06 (-1.0) | +0.03 (0.6) |

No exit rule differs from the live one by more than 0.2 tick per set, at any t.

## 6. Caveats

* Only about 2.5 days of real quotes. Everything beyond 24 h is simulated and assumes the swings of 22-24 Oct carry on.
  * The level drift is borrowed from 14-23 Oct daily medians, which before 22 Oct come from trade prices and are
    noisier, so drift is overstated if anything.
  * Election-eve behaviour on 2-3 Nov (wider spreads, faster moves) is not in the data. That favours finishing the
    sales earlier rather than holding a remainder to the deadline.
* Depth is a constant from 6 snapshots over 13 hours. The deeper book's refill after our clips is assumed, not
  measured.
* Only the cost of exiting is modelled. The sets' own settlement value and the cash's use elsewhere are not, except as
  the time-to-cash column.
* The 2-leg pool's t-stats treat race-sides as independent. They share the same hours, so they measure consistency
  within a day; the independent unit is the day, and there are 2 (one of them 5 usable hours).
* The own-impact control is an absolute move while the treatment is signed, and neither has an SE. All 349 trades
  show +0.67 tick at 60 min, more than the control's 0.44, which may be post-trade adverse drift rather than reversion
  of cheap entries (untested).

## 7. Fable double-check

The review is at `side-session-notes/.../ladder-exits-fable-review.md`. **Verdict: conclusion holds with changes.**

* **What Fable re-ran, read-only.**
  * The real-quote 2-leg pool (h = 12, both halves), clustered by start hour.
  * A 10-day simulated pool with its own code.
  * A gate that re-reads the moving median at each clip.
  * The high-frequency first-hit order.
  * Book ordering and touch sizes.
* **What it reproduced and confirmed.**
  * The numbers, to ±0.02-0.07 tick.
  * The sign conventions and the depth walk side.
  * run_policy: a tightest-tier-first tie-break changes ladders by ≤ 0.02 tick.
  * No leak between training and test.
  * The recommended moving-median gate is slightly better than the fixed level simulated (+0.02 tick, 0.6 h faster).
* **Applied from its should-fix findings.**
  * The wait times were shifted one level; fixed.
  * The high-frequency "tiers fill together" explanation was false; corrected, and the test column's zero power is
    now stated.
  * The training half is in effect one 5-hour evening; now stated.
  * The t-stats measure within-day consistency, and the one-shot loss is mechanical; reworded.
  * The wind-down fallback started too late for 18:00 ET; now computed per race.
  * Hawaii's thin side is the bid, not the ask; clip sizes are now consistent with section 2.
  * The one-shot saving is now quoted as a range from both halves and labelled as against a one-shot sale.
* **Applied from its notes.**
  * The simulation counts level drift twice, which flatters ambitious levels, so it is conservative for the conclusion.
  * The 3-leg tails are illustrative.
  * The own-impact SE.
  * 8 snapshots, not 6.
  * The source of r.
  * Selection bias and election eve are now in the bottom line.
* **None of its findings reverses a recommendation.**
