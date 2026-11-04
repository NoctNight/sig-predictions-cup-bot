# Overnight ideas, tested (24 Oct 2026): F6 stale-quote inside resting, idea 4 weekend lag, idea 3 rival liquidation

Task (Sizhe, via the main session): "implement the ideas from overnight new strategies after you have tested them". This
note covers F6, idea 4 and idea 3 from `new_strategies_overnight.md`. Shock mode and R3 are built by another session.

* **Code:**
  * `research_notes/overnight_followups_study.py`, with subcommands `f6`, `weekend` and `liq`. It needs numpy, pandas and
    matplotlib.
  * `research_notes/liquidation_absorb_wiring.diff`, the detector and the switched-off sleeve.
* **Data:** `data/research_overnight_followups/`.
* **Figures:** `figures/f6_stale_inside.png`, `figures/weekend_lag.png`, `figures/liquidation_watch.png`.
* **Read-only:** `data/sigbot.sqlite` was opened with mode=ro. 0 venue calls, no orders. The shared tree, config, deploy
  and the bot were not touched.

## Verdicts

| idea | verdict | why |
|---|---|---|
| F6: rest one tick inside the DMM's stale quote during lag events | **NO-GO (drop)** | 340 of 395 against-move prints came at a 1-tick DMM spread, where there is no inside. The real order would have had 16 fills in 56 h. Even the upper bound (join the stale touch and take every against-move print, ignoring the DMM's queue) is −0.31 ticks/share in training (t −1.9) and +0.22 in test (t 0.9) after exit costs. |
| Idea 4: weekend lag | **interim, no decision** | Weekdays: SIG copied a median 30% of a 1.5+ point both-venue jump by 10 min (n 25), matching the 29% kernel. Saturday (ET) so far: n 1. Rerun on Monday 26 Oct. |
| Idea 3: absorb rivals' pre-punt liquidation | **built, OFF; trigger not met** | Detector and sleeve are in the diff, with guard tests and a sim scenario at 0 violations. Live readings 22–24 Oct: sell share 56–65%, favourite gap 0.6–1.4 ticks *narrower* than baseline. The trigger is off. |

## 1. F6: rest one tick inside the DMM's stale quote

**Hypothesis.**
* Polymarket/Kalshi move 2+ points and SIG's DMM has not followed.
* Some takers still trade against the move at the DMM's stale price. They sell into the old bid after an outside rise.
* A bid one tick above the DMM's bid would catch them, and SIG's catch-up would pay us.

**Data.**
* `venue_fair` gives Polymarket, Kalshi and SIG's bid/ask every ~107 s, from 21 Oct 23:49 to 24 Oct 07:47 UTC (56 h).
* The SIG tape gives prints with aggressor flags. Our own prints were removed by matching our fills: same market, same
  price, within 3 s.
* Our MM's orders give the overlap. An MM order counts as live from its placement until the next MM placement on that
  market, or its expiry.

**Test.**
* **Lag event:** the outside fair moved 2+ points over 60 min, both venues the same way, within 4 points of each other,
  and SIG had copied under a third of it. The window ends after 60 min, once SIG has copied two thirds, or when the
  outside move fades below 1 point.
* 79 events: 49 on 22 Oct (training) and 30 on 23–24 Oct (test).
* **Our order:** one tick inside the DMM on the with-move side, only where its spread is 2+ ticks and the price stays on
  the right side of the outside fair, up to 500 shares per event.
* **Fill:** an against-move taker print at or through our price.
* **Markouts:** SIG mid at +5 and +30 min (the nearest poll within 4 min) minus our price, in ticks.
* **Exit cost:** 0.5 tick.
* **GO bar:** out of sample, positive with t > 2, net of what our MM already earns.

**Result.**
* **Against-move flow exists:** 85,905 shares against the move versus 151,609 with it during the windows. That is
  ~1.1k/event, as the overnight note said.
* **But almost none of it is reachable inside the spread:**
  * 340 of the 395 against-move prints came while the DMM spread was 1 tick (53 at 2 ticks, 2 at 3).
  * The real F6 order fills 11 times on the training day and 5 times on the test days.
  * Its 30-min markout: −0.5 to −0.75 ticks (training), 0.0 (test).
* **Upper bound:** joining the stale touch, and pretending every against-move print was ours despite the DMM's
  1,500–2,500 shares queued ahead:
  * markout at +5 min: +0.31 (training, t 1.7) and +0.57 (test, t 8.0) ticks;
  * at +30 min: +0.19 (t 1.2) and +0.72 (t 2.9);
  * after the 0.5-tick exit: −0.31 (t −1.9) and +0.22 (t 0.9).
  * It fails the bar even before queue position, which would take most of those fills away.
* **Overlap with the MM:** none. During lag windows our MM had no bid or ask at these prices and no fills, so its
  outside-fair gate already keeps it out. F6 would have added nothing to what the MM earns, and lost money on it.

**Decision: drop.** The only version with a positive test mean (joining the touch) is not available to us, because we
queue behind the DMM.

## 2. Idea 4: weekend lag (interim)

**Hypothesis.** The DMM copies outside moves more slowly at weekends, so the catch-up sweep's edge per share is bigger
then.

**Test.**
* **Jump:** between two consecutive `venue_fair` polls (at most 4 min apart), Polymarket and Kalshi both moved the same
  way, their fair moved 1.5+ points, they agree within 4 points afterwards, and SIG had not moved a third of it. One jump
  per market per 30 min.
* **Copied share at +h:** SIG mid change since the poll before the jump, divided by the outside move.
* **Saturday:** from 04:00 UTC 24 Oct (00:00 ET).

**Result so far.**

| | jumps | SIG copied by 10 min (median) | 30 min | 60 min | outside still there at 30 min |
|---|---|---|---|---|---|
| weekdays 22–23 Oct | 27 | 30% (n 25) | 31% | 26% | 102% |
| Saturday 24 Oct, 04:00-09:15 UTC (polls stop at 09:15) | 1 | 0% (n 1) | – | – | – |

* The weekday number reproduces the 29% kernel from the 22 Oct feed capture, so this code measures the same thing.
* The weekend sample is too small to say anything.

**Decision rule (unchanged):** on Monday 26 Oct, rerun `python research_notes/overnight_followups_study.py weekend`. Act
(raise the catch-up sweep's cap on weekends) only if the weekend copy at 10 min is under 15% with n ≥ 10.

## 3. Idea 3: absorb rivals' pre-punt liquidation (31 Oct–3 Nov)

### Detector: `sigbot/liquidation_watch.py` (read-only; in the diff)

* **Favourites:** legs whose median Polymarket/Kalshi fair is ≥ 0.70.
* **Taker sell share:** shares of taker sells divided by all classified taker shares on favourites, with our own prints
  removed.
* **Gap:** mean SIG mid minus fair, in ticks, over polls where both venues agree within 4 points. Compared with its
  baseline, the median daily gap of the 3 days before.
* **Trigger, over a rolling 24 h, all three must hold:**
  * sell share > 60%;
  * sell share at least 10 points above its own median of the 3 days before (added after the Fable review);
  * the gap 2+ ticks wider (more negative) than baseline.
  * A window with fewer than 5,000 classified shares, or with no sell-share baseline, never triggers.
* **Own prints:** only prints where we were the taker are removed. Rivals selling into our MM, inside or tilt bids are the
  flow this looks for, so they are kept.
* **Daily brief:** `python3 -m sigbot.liquidation_watch --days 7` prints one row per ET day plus the rolling 24 h.
  `--json` gives the same data for scripts.
* **Live readings, every 3 h since 22 Oct (`figures/liquidation_watch.png`):**
  * Sell share is 56–65%. It was above 60% for most of 23 Oct.
  * The gap went from −6.2 to −5.4 ticks. Against its baseline it is **+0.6 to +1.4 ticks, i.e. narrowing**.
  * The trigger has never been on.
* **Caution on the 60/40 bar.** Ordinary weekdays already reach 61–65% sells on favourites, so the absolute 60% alone
  does not separate liquidation from normal flow.
  * Hence the relative bar (`SELL_SHARE_RISE` = 0.10).
  * Its baseline is still noisy (39–58%), because our tape starts on 21 Oct and includes thin days. By 31 Oct it will
    cover full weekdays.
  * Until then the gap condition is what carries the trigger.
* **Sleeve candidates are common.** 7–10 of ~94 favourites sit 3+ ticks under their own usual gap at any moment, so the
  market-wide gap condition matters: without it the sleeve would buy noise.

### Sleeve: `sigbot/proposals/liq_absorb.py` (config `liqabs_enabled`, default **false**)

* **Entries:**
  * only while the trigger is on, refreshed hourly from a read-only connection; a failed refresh means trigger off;
  * only from 31 Oct 00:00 ET to 1 h before flat-by.
  * **Legs:** favourite YES only (fair 0.70–0.97, both venues within 4 points), with the SIG mid ≥ 3 ticks under the
    leg's usual gap and the ask under the fair.
  * The usual gap is the leg's median over the 3 days before the trigger's 24-h window, so a day of selling cannot drag
    its own baseline down. The sim found this.
* **Caps:**
  * 3,000 in total, 750 per leg, 300 per order;
  * at most 80% of the touch, at most 3 entries per cycle;
  * a 2,000 cash floor;
  * never a leg we are short or hold > 4,000 shares of.
* **Pricing:** entries and exits price off books with our own resting orders netted out. The venue cancels the crossing
  remainder of an order that meets our own quote, and our MM quotes these favourites.
* **Exits** (sell YES = buy NO at 1 − bid, never more than the venue shows held, net of exit orders still in flight):
  * +2 ticks take profit;
  * −6 ticks stop;
  * 6 h maximum hold;
  * the sleeve kill (realised + marked ≤ −300);
  * everything flat by 3 Nov 12:00 ET (17:00 UTC), starting 30 min early and retrying every 60 s.
    * With no fresh feed book, the flat-by and kill exits read the REST book.
    * Failing that, they sell through the cycle's bulk best bid by 2 ticks.
* **Kill:** the sleeve kill is stored as a param, so a restart stays killed.
* **Trigger refresh:** hourly in a daemon thread, because it takes ~7 s on today's tape. Entries read the last result; a
  failed refresh means off.
* **Cooldown:** a leg sold at a loss of 2+ ticks per share is not re-bought for 6 h (the sim found this). It is read from
  the fills, so it survives a restart.
* **Ledger:** the fills table, purposes `liqabs` / `liqabs_exit`.
  * Entries placed in the last 3 min but not yet filled count against the caps.
  * One order per market is in flight at a time.
  * An entry whose response has no orderId blocks that leg for 1 h.
* **Ledger order:** fills are read in (filled_at, id) order, so a buy and its exit in the same second leave no phantom
  lot.
* **Isolation from the rest of the bot:**
  * The MM excludes the sleeve's lots from its inventory, but never more than the venue shows held.
  * The arb set paths (recycler, free unwinds, offload) leave its races alone.
  * It skips races that the cheap-favourites sleeve or the high-frequency arb hold, and the high-frequency arb skips races
    with liqabs lots.

### Gates

* **Guard tests:** 22 new checks in `tests/test_guards.py`: detector on/off cases, own-print exclusion, leg baselines,
  candidates, caps, exits, ledger, cooldown, and bot-level behaviour through a stub. All pass, including every existing
  check (`ALL PASS`).
* **Compile and tuner tests:** pass (`test_autotune.py` exit 0). Its two tuner-group notes are unchanged code and not
  from this diff.
* **Sim scenario `liqabs` (`tests/sim_pipeline.py`):** 0 violations.
  * Setup: 4 days of synthetic history (favourites 6 ticks under the fair, then 9 ticks under over the last 24 h with
    75% taker sells) make the real detector fire.
  * Results: 10 entries and 4 exits; peak open cost 1,499; realised −20.1, from the scripted stop.
  * Checks: no buy on a leg that does not sag or whose venues disagree; ≤ 300 per order; ≤ 750 per leg; ≤ 3,000 in
    total; no entry in the last hour; no re-buy after a stop; flat at the end.
  * Run it with `nice -n 15 python3 tests/sim_pipeline.py liqabs`. `--liqabs` turns the sleeve on in every scenario.
* **Full sim:** FULL_SIM_RESULT
* **Fable review:** FABLE_RESULT
* **Not run:** the precheck's dry cycle. It reads the venue key, which this session must not read. It runs in
  `botctl.sh deploy` as usual.

### Applying

* `research_notes/liquidation_absorb_wiring.diff` is a git diff against **5228f0c + `cheap_favourites_wiring.diff`**, as
  queued. It adds two new files and touches `sigbot/config.py` (8 fields after the cheapfav ones), `sigbot/run.py`,
  `tests/sim_pipeline.py` and `tests/test_guards.py`.
* `strategy_monitor_wiring.diff` was built on 5228f0c alone and does not apply after cheap_favourites. The shared tree
  also has uncommitted edits in run.py, arb.py and the tests from another session. Expect small manual merges in the
  same files.
* **The sleeve stays off:** `liqabs_enabled` defaults to false and nothing in config.json changes.
* **Turning it on is Sizhe's call, from 31 Oct, only if the detector shows the trigger.** It conflicts with the cash
  objective (it adds positions), so it is a round trip only, capped at 3,000.
