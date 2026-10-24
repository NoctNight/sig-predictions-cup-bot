# sigbot change log

## 2026-10-22 01:29 BST
- Arbs: new baskets need >= 4% return (`arb.min_ret`); held sets kept to settlement. Revert: set min_ret 0.
- Market making: capital_frac 0.20 -> 0.35, n_markets 15 -> 25. Revert in config.json mm.
- Mean reversion: meanrev_max_ticks 1 -> 2; after a sweep the fade side quotes at 0.5x (`vol_gate_fade_mult`), the
  reducing side at 0.5x anchored at the pre-move mid. Revert: fade 0, size 0.25, max_ticks 1.
- Evidence: HF study (research_notes/hf_mean_reversion.md), settlement value +1,027 vs marked -50.

## 2026-10-22 01:41 BST
- Watchdog task (sigbot-overnight-watchdog): process count switched from `pgrep -f` to `ps | grep "[s]igbot.run --live"`.
  Why: pgrep -f also matches the shell running the check, so the count read 3 with one bot running and the watchdog's
  "two or more bots" rule could have killed a healthy bot. No bot code changed; trading not paused. Revert: edit the task.

## 2026-10-22 01:41-01:43 BST (trading paused 01:40:47-01:43)
- Directional sleeve wired live (`ExplainedDirection` in sigbot/run.py wrapping sigbot.direction.DirectionTrader):
  value signals only, only on legs marked trade_yes/trade_no in params/fair_explained.json with the matching side,
  one leg per race, per_market_cap 300, overall_cash_frac 0.04 (~3,000), min_edge_take 0.03, 60 s taker TTL.
  Why: Sizhe asked to trade large Polymarket gaps at low volume once understood; the directional session's verdicts
  explain all 21 legs as this venue's compression toward 50% (Polymarket, Kalshi and our polling model agree).
  Revert: set "direction": {"allow_live": false} in config.json and restart.
- Stopped the directional session's standalone dry loop (the bot now polls the venues itself).

## 2026-10-22 01:53-01:56 BST (trading paused) - red-team mitigations (research_notes/adversarial_backtests.md)
- Recycling: never unwinds for an opportunity whose basket just failed, re-reads the target race's book and requires the
  opportunity to still exist, and only unwinds races with filled arb baskets (not market-making inventory).
- Arb execution: a rejected all-or-none basket cools its race down for 10 minutes and the pass continues with other races
  (was: stop the pass). A basket that fills partly marks the race hostile for the session and logs ARB PARTIAL FILL.
- Market making: requote order rotates stalest-first so a write squeeze cannot starve the same markets; microprice uses
  the first level with >= 100 shares on each side, capped at 3,000; requote_ticks 0.5 -> 1.0.
- Tilt: skips markets whose spread is wider than 3 ticks (thin tails where its orders get baited).
- Checked: the 20 "open hedge orders" in the report were stale DB rows; the venue showed 230 open orders, all mm/tilt,
  all with expiries.
- Revert: restore from the pre-change versions described above (small, local edits in arb.py, run.py, mm.py, tilt.py).

## 2026-10-22 03:10 BST (trading paused 03:09-03:11) - race-consistency caps on market-making quotes
- What: on every leg, our bid is capped so that bid + the other legs' best bids <= 1 - 1 tick, and our ask is floored so
  that ask + the other legs' best asks >= 1 + 1 tick (sigbot/mm.py build_quotes; run.py passes the bulk price snapshot).
- Why: 03:00-03:09 burst of 22 fills; on U.S. Senate control we bought 364 YES D at ~0.67 and 218 YES R at 0.345, i.e.
  our bids summed to ~1.015 for a pair that pays exactly 1 - someone sold the set into our bids (a riskless arb against
  us, ~1.5c per pair). With the venue's ~2% overround, independently priced legs can do this in any race.
- Test: offline, U.S. Senate D best bid moves from 0.665 to 0.650 so D bid + R bid = 0.995.
- Revert: remove bid_cap/ask_floor from build_quotes.

## 2026-10-22 06:55 BST (trading paused 06:54-06:55) - recycling follow-through
- What: after a recycle unwind succeeds, the main loop immediately re-reads the target race's book and executes its basket
  in the same lock (was: left for the scanner's next pass); recycle_min_gain 0.01 -> 0.02.
- Why: 06:39 the bot unwound 608 Delaware Senate sets (forgoing ~18 of locked profit) to fund a CA-48 basket at 4.17%,
  but the basket had gone by the scanner's next pass, so the capital sat idle and settlement value fell 101,050 -> 101,036.
- Revert: remove the follow-through block in run.py after `rec = self.arb.recycle(...)`; set recycle_min_gain 0.01.

## 2026-10-22 08:34-08:36 BST (trading paused) - Sizhe's request: fewer ladder quotes, more position-neutral trades
- Zero-EV exits (run.py `zero_ev_exits`): directional and tilt holdings close once the venue is within exit_band (1 point)
  of the outside fair value; only the sleeve's own filled quantity, never arb-set legs; 60 s limit orders at the touch.
- Free arb-set unwinds (arb.py `free_unwinds`, every 5 min): a held set unwinds atomically when the legs' asks sum to
  <= payout (1.00 for a two-leg race), i.e. the locked profit is taken now at no cost and the capital is freed.
- Arb min_ret 0.04 -> 0.015: low-return sets no longer lock capital to Nov 4 because they cycle out for free.
- Ladder market making: capital_frac 0.35 -> 0.15, n_markets 25 -> 12 (frees writes for arbs and exits).
- Test: Delaware at asks 1.035 -> no unwind; 1.000 and 0.990 -> unwind all 5,571 sets.
- Revert: config.json mm/arb values above; set exit_band 0 and free_unwind_every_sec very large.

## 2026-10-22 10:05 BST — realtime WebSocket feed wired in
- New `sigbot/feed.py` (stdlib RFC 6455 client + Phoenix/Supabase Realtime protocol): joins `tournament:<id>:markets`
  (versioned full books + trades for every tournament market, ~25 batches/s) and our `user:<profile>` channel. Probe:
  books arrive 0.7 s after the engine stamps them, trades ~2.4 s; 236/237 books held within 90 s of connecting.
- `run.py`: `fresh_book`/`get_book`/`feed_prices` helpers. Arb scanner runs every 1 s (was 5 s) while the feed is healthy,
  evaluates up to 12 candidate races per pass from feed books (was 3 via REST), and refreshes the REST price snapshot only
  every 30 s. MM, held-set unwinds and recycle follow-through take feed books when fresh (<120 s), else REST. Public
  trades come from the feed; REST tape polls only backfill every 5th cycle. HEALTH line shows feed=[...]. Everything
  falls back to REST automatically when the feed is down. Config: feed_enabled, feed_max_age_sec, arb_scan_sec_feed,
  arb_scan_books_feed. Backups: sigbot/run.py.bak_feed, sigbot/config.py.bak_feed.
- Dry-run cycle: 16.6 s (was 26.7 s); reads per cycle fall accordingly. Paused and restarted live at ~10:06.
- 10:15 Feed resilience test (separate process, 2 token mints, no orders): forced token refresh kept both channels joined with
  batches flowing (371 in 12 s); forced socket drop reconnected in 3.2 s and rejoined. Fix queued for the next restart:
  `_last_rev` is cleared on session end so a reconnect no longer logs a "revision gap" warning per market (log noise only).

## 2026-10-22 11:15 BST — shadow tuner wired in; restart
- `sigbot/shadow.py` (from the backtester side session): replays arb, MM and directional rules on our own logs, 70/30
  walk-forward, hard bounds (never loosens kill switch, exposure caps, arb caps, all-or-none). One call per cycle
  (`shadow_tick(self)` at the end of `Bot.cycle`), a run every 3600 s in a daemon thread, ~7 s CPU, no API reads.
  `shadow.auto_apply` is false: recommendations are logged (SHADOW ...) and written to params/shadow_report.json only.
  First run: no change clears the out-of-sample tests. MM calibration: replay filled 6,292 shares vs 6,277 actual.
- Restart also picks up the feed fix (no per-market gap warnings after a reconnect). Paused ~11:15, book empty, restarted.

## 2026-10-22 11:22 BST — Sizhe's decisions: MM size step 1 (2x), arb threshold 3%
- Decisions: end-game punt adopted (execute late, ~Nov 3 ET); no concentrated sleeve; target 1,000,000 SUSQies at the end;
  MM size 2x now then 4x while markouts stay positive; arb min_ret 1.5% -> 3% (backtest best 3.0-3.2%); server later;
  backtester session resumed; capital to be allocated by measured edge per sleeve, not fixed fractions.
- MM step 1: base_qty 100 -> 200 (ladder 100/150/200/250 shares), max_inventory_notional 600 -> 900 so a 2x ladder is not
  blocked by the hard inventory stop. Basis: Kelly on measured markouts is unbounded (mean +0.09..+0.29 tick/share, sd ~1
  tick => f* = mean/var x W is millions of shares), so size is bounded by the inventory cap and by adverse selection, not by
  variance; the step is gated by the report's markout-based resizing rule (act only after >=30 fills; halve on negative edge).
- Step-up rule for the reviews: 4x (base_qty 400, cap 1,500) once >=30 MM fills at 2x with markout30 >= 0 and markout5m > 0;
  step back to the previous size if markout30 < -0.3 tick on >=20 fills; the kill switch (-1 tick) is unchanged.
- 11:27 Per Sizhe's instruction in the backtester session (shadow near-misses only): arb.recycle_min_gain 0.02 -> 0.005,
  direction.min_edge_take 0.03 -> 0.06. Neither cleared the tuner's materiality bar; arb.min_ret 0.026, mm.requote_ticks 1.5
  and mm.vol_gate_fade_mult 1 were NOT applied (worse out of sample). Restart under the pause procedure.
- 11:33 Sizhe: arb ceilings raised to capital_frac 0.70 (70k) and max_race_frac 0.10 (10k per race); these are ceilings,
  the actual split between arbs, races, MM and the other sleeves is to follow measured edge (allocator in progress).
  Shadow tuner bounds updated to match (tuner may only tighten them). Restart under the pause procedure.
- 11:36 Sizhe: aim for higher volume without negative-EV trades. mm.n_markets 12 -> 24 (reads no longer bind with the
  feed; writes ~5/min of the 15/min main budget). Next volume levers in order: MM 4x step (gated by markouts), tracker-ranked
  market selection, passive sweep fades (HF study: +1.2-1.7 ticks), arbs at the 70% ceiling.
- 11:37 Shadow tuner (first run under the 70% ceiling) recommends arb.min_ret 0.03 -> 0.024: +70.8 out of sample, won 4/4
  test blocks. Applied with this restart (Sizhe: "if this maximises EV then go for it"). With capital no longer binding,
  a lower floor deploys more sets at 2.4-3%.
- 11:40 Sizhe: "why not market make on every liquid exchange". Volume is concentrated: 15 exchanges = 50% of venue volume,
  56 = 80%, 89 = 90% (3.28M shares in the last 6 h). Step 2 of the volume plan: mm.n_markets 24 -> 40, order_ttl_sec
  900 -> 1800 and refresh_sec 720 -> 1500 (halves the re-placement writes per market; the feed requotes on real moves
  anyway), mm.capital_frac 0.15 -> 0.35 so 40 ladders at 2x are not scaled down. Next step 56 if no engine throttling
  and markouts hold; the tracker will then pick the markets by measured edge instead of volume.
- 11:45 Market tracker (`sigbot/tracker.py`) wired in: rebuilt from 24 h of logs at start, updated every cycle with feed
  trades, feed books, new MM fills and the quoted set; writes params/market_tracker.json every 5 min. MM market selection
  (`Bot.pick_markets`) now takes the tracker's top-n by expected edge per hour (fill rate x net markout, prior from venue
  trade rate and spread) inside the price band, falling back to volume ranking. Research: research_notes/volatility_vs_mm_edge.md
  (markout30 -0.01/+0.18/+0.28 ticks by vol tercile, n=146 fills). Dry run OK; restart under the pause procedure.
- 11:52 Self-tuning on (Sizhe's instruction to the backtester session: the bot must run its own out-of-sample backtests
  on a schedule and apply the results). config: shadow.auto_apply true, allocator.auto_apply true. Groups: arb every 4 h
  (24 h lookback), direction 12 h/72 h, mm_quote 12 h/48 h, mm_ladder 24 h/96 h, allocator 12 h/48 h. A value is applied only
  inside the hard bounds, with material out-of-sample gain, bootstrap confidence >= 0.90 over 8 test blocks, no worse
  drawdown, and the same value from 2 consecutive runs; applied values live in params/auto_applied.json and
  data/auto_tuning.log; manual config edits win. mm.capital_frac 0.35 -> 0.33 keeps worst-case gross exposure under 50k.
  MM size replay: 4x fails out of sample under both fill models (deep levels go negative); hold 2x unless the tuner
  confirms a larger size. Restarted under the pause procedure.
- 11:55 Free unwinds now checked every 10 s by the scanner from feed books (`ArbScanner.free_unwind_pass`, config
  free_unwind_scan_sec), instead of every 300 s over REST; positions are re-read after any unwind or execution so a stale
  snapshot cannot buy legs for sets no longer held. The 300 s main-loop check stays as a backstop. Backup run.py.bak_fu.
- 11:57 Sizhe: wire the allocator's per-race arb caps and the offload rule (report-only). `ArbEngine.race_budget_left`
  now takes min(max_race_frac ceiling, allocator cap) with the cap floored at the file's default_cap (2,000) so a new
  opportunity can still be taken small; `Bot.load_allocation` reloads params/allocation.json on change (96 races loaded,
  top RI Governor 10k, Montana 10k, Nebraska 6.9k). `Bot.offload_report` runs allocator.offload_check every 5th cycle and
  logs OFFLOAD (report-only) when a cash-blocked trade could be funded by exiting a cheaper holding. Backups
  arb.py.bak_caps, run.py.bak_caps. Restarted under the pause procedure.
- 12:25 Network dropped twice (09:05, 12:05-12:10). The second time the feed thread stayed silent for 17 min (stuck in a
  DNS lookup / connect with no timeout) while REST trading continued. Added `Bot.feed_watchdog`: when the feed has been
  silent for feed_stall_sec (240 s) the bot abandons that thread and starts a fresh feed (`exchanges_by_market` kept for
  set_universe). Restart under the pause procedure.

## 2026-10-22 12:40 BST — exploit-report fixes (research_notes/bot_exploits.md) and volume levers
- F2: mm.improve_when_spread_ticks_ge 2 -> 3 (143 of 168 level-1 fills that improved a 2-tick DMM spread earned -0.01 tick; joining earned +0.16).
- F3 + volume lever 1: ladder levels 4 -> 2 at [1,2] ticks, size mult [1.0, 0.5], base_qty 300 (deep levels: 12 fills / 941
  shares in 3 days vs 6,948 shares at level 1; level-1 size was binding on 72% of fills). Halves orders per market.
- F4: requote trigger ignores fills of <= 10 shares (the one-share prober); quoted markets rotate by cycle so the write
  budget does not starve the same ones.
- F1: `ArbScanner.fast_requote_pass` — between cycles, a quoted market whose best bid/ask moved >= 1 tick since we last
  quoted it is re-quoted from its feed book (max mm_fast_requotes_per_min 3, 2 writes each); half of MM fills were stale
  (+0.01 vs +0.24 tick fresh). QuoteState.last_touch recorded at quote time; mm.force_cancel ensures cancel-all before re-post.
- F6: arb all-or-none failure cooldown 600 s -> 60 s. Write budgets: main 15 -> 18, scanner 12 -> 10 per minute.
- Deferred: F5 size jitter (cleanup), proposal 2.1 dmm_lag_follow (directional, needs Sizhe), 2.2 inside_quoter (overlaps F2).
  Backups *.bak_f1. Restart under the pause procedure.
- 14:13 The Claude app quit and took the bot's terminal with it; 167 orders were left resting (TTLs 30 min to 6 h).
  14:21 cancelled all (book empty), restored run.py/config.py to the 12:40 version (the exploit-sleeve wiring, whose dry run
  hung, is parked in run.py.sleeves_wip / config.py.sleeves_wip), restarted.
- 14:31 The bot now runs as a macOS launchd agent (`deploy/mac/botctl.sh`, label com.sizhe.sigbot, plist in
  ~/Library/LaunchAgents): independent of the Claude app and any terminal, restarted by launchd after a crash (not after
  a clean stop, at most every 30 s), started at login, wrapped in caffeinate -i, stdout/stderr appended to data/live.log.
  Pause procedure is now: `deploy/mac/botctl.sh stop` (SIGTERM -> the bot cancels its orders; the script then checks the
  book with deploy/cancel_all.py), patch, `deploy/mac/botctl.sh start`. Parent process verified as /sbin/launchd.
- 14:59 Root cause of the "hanging" dry runs: (1) news.py fetched sources in a `with ThreadPoolExecutor` block, which waits
  for every worker on exit; a DNS lookup has no timeout, so on the flaky eduroam network the trading cycle could block for
  minutes (the same risk existed in the live bot). Fixed: collect sources for 30 s, count the rest as failed, shut the pool
  down without waiting. (2) The machine is at load ~220 from a Remotion render in another project, which makes startup slow.
  Also store.ingest_tape uses INSERT OR IGNORE (a dry run alongside the live bot hit a duplicate trade id).
- 14:59 Exploit sleeves live (were parked in run.py.sleeves_wip): `Bot.lag_cycle` follows Polymarket/Kalshi moves of
  >= 2 points that SIG has not followed (proposals/dmm_lag_follow: size min(1,000, DMM touch, directional 300-notional
  per-leg cap), exit at half convergence or 60 min, partisan-blocked sides skipped, purpose 'direction');
  `Bot.inside_cycle` rests 100 shares one tick inside the DMM where its spread is >= 3 ticks on markets the MM does not
  quote (proposals/inside_quoter: race-consistency and partisan guards, inventory cap 300, 300 s TTL, max 12 markets
  re-posted per cycle, purpose 'inside'). Dry run: full cycle 24 s, 13 inside orders. Restarted via botctl.
- 15:10 Sizhe: "trade using all of our capital, release any caps for good trades, get out of cheap longs when a higher-EV
  trade arises". Ceilings: arb capital_frac 0.70 -> 0.90, max_race_frac 0.10 -> 0.15; tilt 0.12 -> 0.20 (allocator ceiling
  0.25); direction overall 0.04 -> 0.08 (ceiling 0.10), per-leg cap 300 -> 600 notional; allocator worst-case limit 50k ->
  75k (arb sets excluded). Kept: partisan cap 5,000, kill switch, all-or-none arbs. The allocator still moves capital
  inside these ceilings by measured edge (first run: arb 0.75, mm 0.33 -> 0.20, both halves consistent; tilt/direction
  no change, thin or inconsistent evidence). Offload is now LIVE (config allocator.offload_live): a capital-blocked trade
  that beats the cheapest holding's forgone edge + exit cost by recycle_min_gain exits that holding at the touch
  (tilt/direction; arb sets via the atomic recycle path), max 2 per check. Dry run OK; restarted via botctl.
- 15:47 Kill switch fired falsely at 15:39 (drawdown 1,120): the scanner executed a WI-03 arb basket between the cycle's
  cash read and the health check, so cash had dropped but the new sets were not yet in pos_value. Fix: cash and
  positions are read together under trade_lock and the health check / cycles table use that matched cash. Kill switch
  thresholds unchanged. Bot stopped ~8 min (already halted by the false kill), restarted via botctl.
- 16:05 Battery mode for ~3 h (Sizhe on battery, lid closed): news sweeper off (it ran dry anyway: 21 fetches plus LLM
  calls every 90 s), main cycle 30 -> 60 s, feed arb scan 1 -> 3 s, free-unwind check 10 -> 30 s. Backup
  config.json.bak_battery; restore it (and restart) to return to normal speed.
- 16:17 Battery 13% (~36 min) on battery: started deploy/mac/battery_guard.sh, which runs botctl stop (cancels all orders) at <= 7% so the Mac dying cannot leave quotes resting.
- 16:31 At Sizhe's request stopped the Remotion render batch loop in dashboard-shelf/yt-pipeline (last spec night-before-electricity; its out/ mp4 from 15:09 is untouched) and 16 idle leftover headless Chrome processes, to save battery.
- 16:29 Battery guard stopped the bot at 7% (book empty). 16:32 a new render (build.mjs night-before-electricity, detached, under caffeinate) was relaunched by another process; stopped it again.
- 16:38 Normal-speed settings restored in config.json (news on, 30 s cycle, 1 s feed scan, 10 s free-unwind). The Mac still reports battery power (5%), so the bot was not started; deploy/mac/start_on_ac.sh starts it automatically once AC power is detected, then re-arms the battery guard.
- 18:50 Verified changes from the side sessions (Sizhe: "implement any of the changes that each of the subconversations verified"):
  * favourite drift (research_notes/favourite_drift.md): the gap legs beat arbs per unit of capital and the explained
    directional sleeve was capped out (overall cap 8% of a 26k cash balance = ~2.1k): direction.overall_cash_frac 0.08 -> 0.17
    (~4.5k, the study's figure), allocator ceiling 0.10 -> 0.20. Only explained verdict legs trade (RI Governor D and TN
    Governor R are verdict legs not yet held); NH-01 D, FL Senate R, Maine Gov D stay out (model disagrees or low confidence).
    A verdict refresh dropped 6 legs from the allowlist on thin post-outage data, so params/fair_explained.json was restored.
  * volatile moves (research_notes/volatile_moves.md 6.2-6.3): after a 5+ tick sweep on a quoted market the MM pauses its
    level-1 retrace-side quote for 1 h (retrace quotes lost ~1 tick, t -5.4): `Bot.volatile_cycle`, MarketMaker.retrace_pause,
    logged RETRACE PAUSE. Fade/catch-up trades are paper-logged to data/volatile_paper.jsonl for a re-test after 3+ nights.
  * correlation (research_notes/correlation_arbs.md): no trading change (no riskless arb); noted for sizing that same-state
    legs (GA, FL, MI Senate + Governor) behave as one position.
  Dry run OK; restarted via botctl.
- 19:20 Sizhe: move recycle and offload into the 1-second scanner and compare against every holding.
  `ArbScanner.recycle_pass` (every scanner pass, ~1 s): when the best arb seen in the last 10 s is capital-blocked, the
  exit of EVERY held set is priced from its feed book (unwind_quote walks the asks, so spread and depth are in the cost;
  was: the first 3 held sets, REST books, once per 30-60 s main cycle), the cheapest-to-give-up set is unwound if the new
  set beats it by recycle_min_gain, and the new set is bought at once from a <=3 s-old book, all under trade_lock.
  `ArbScanner.offload_pass` runs the cross-sleeve offload check every offload_scan_sec (3 s; was every 5th main cycle).
  The main-loop recycle path stays as a backstop when the feed is down. Unit-tested with stubs (all held sets priced),
  dry run OK, restarted via botctl. Expected latency for a capital-blocked swap: ~2-3 s (was 15-40 s); offload ~3-5 s
  (was 1.5-3 min).
- 19:11 Sizhe: held sets rarely revert to a free unwind, so buy more sets at lower edge when YES bids sum above 100%,
  but keep the bar for buy-sets (asks below 100% minus the third-party rate). arb.min_ret_sell 0.005 (sell-sets: any
  edge >= 0.5 cent, riskless to settlement; was the shared min_ret 2.4%); buy-sets keep min_ret 2.4% (the tuner's value,
  third-party risk). New arb.cash_reserve 8,000: arbs never take cash below it, so MM, tilt and directional stay funded,
  and capital-blocked better sets trigger the 1-second recycler sooner. Dry run: plan included Alaska Senate 2,040 sets
  at 2.04% and Oklahoma Senate 2,030 at 1.52%. Restarted via botctl.
- 19:25 Review: the inside quoter's 30-minute markout fell to -0.37 tick on 46 fills (rule: disable below -0.3 on >= 20):
  inside_enabled false. OFFLOAD logged the same arb-set recommendation every 3 s (MI-07 leftover NO legs, not a real arb
  set, so the recycler never acts on it): identical arb-set (and report-only) recommendations are now logged at most once
  per 10 min. Restarted via botctl.
- 19:45 Allocate by edge (Sizhe asked earlier; one-time permission to implement research results without asking): tilt
  (+19 per 1k per day, best markouts) had no orders because low-edge arbs had drawn cash to the 8k reserve and tilt sizes to
  90% of cash. arb.cash_reserve 8,000 -> 20,000 (arbs stop at 20k cash; tilt/directional/MM get it first; better arbs then
  come via the 1-s recycler), tilt.max_markets 30 -> 80 (covers the untouched longshot/favourite tails). Restarted via botctl.
- 19:40 Tilt was starved by a bookkeeping bug: its budget subtracted every tilt fill since the start (12,338 of "committed"
  capital) even for positions already exited, so it placed nothing. Now committed = collateral of tilt positions still held
  (tilt fills net of exits, clamped to the actual position); free = min(cap - committed - resting, 0.9 x cash - resting).
  Dry run: 33 tilt orders on 11 markets. Unfunded tilt candidates are recorded each cycle and become offload
  opportunities fundable by arb sets (ret = tilt edge on collateral minus one tick of exit cost); the offload now unwinds
  just enough of the cheapest real arb set (ArbEngine.unwind_set, all-or-none, limits from walking the feed asks) when the
  tilt trade beats the set's forgone return by recycle_min_gain. Tilt realisation measured on 119 fills: +1.0 tick after
  1 h, +1.6 ticks after 6 h, median 9.8 h from entry to exit on the 12 exited markets. Volume-cache warm-up accepts a cache
  up to 48 h old (it is only the fallback market ranking; a rebuild cost 7 min of startup). Restarted via botctl.
- 20:05 LOSS, my bug: the tilt-funding offload added at 19:40 unwound arb sets repeatedly (38 unwinds, 29,533 sets, 32k of
  capital, ~914 of locked arb edge given up between 19:41 and 20:04). Causes: (1) tilt's unfunded list is rebuilt only
  once per main cycle, but the offload ran every 3 s and kept unwinding for candidates that were already funded by the
  previous unwind; (2) it never checked that cash was actually short (cash rose from 8.6k to 21k); (3) the go/no-go used
  a snapshot estimate of the exit cost (~2.06%) while the real walked cost was 2.3-3.6%. Paused, allocator.offload_live
  set to false (no offload exits at all), restarted. Recycling between arb sets and free unwinds are unaffected.
- 20:15 Offload fixed and back on (Sizhe: "fix it and put it back on"). Arb-set unwinds that fund tilt/directional trades
  now require: cash below the 20k arb reserve AND the sleeve's own free budget (tilt.free_last, or 0.9 x cash for
  directional) short of the trade's capital; each opportunity funded at most once per 30 min; the REAL walked exit cost
  (unwind_quote) to leave at least recycle_min_gain against the trade's return (checked in ArbEngine.unwind_set); at most
  one unwind per 3-s check, and tilt's unfunded list cleared after each. Because unwinds stop once cash reaches the 20k
  reserve and arbs only buy above it, sets cannot ping-pong. Stub replay of the 19:41 churn: 60 offers of one trade -> 1
  unwind; cash above reserve -> 0; tilt with room -> 0. Dry run OK, restarted.
- 20:18 Research results wired (one-time mandate, memory sig-implement-permission):
  * MM outside-fair gate (research_notes/mm_upgrade.md section 4.1, verified on realised fills, Fable-checked): when the
    fresh Polymarket/Kalshi fair (<= 10 min old, venues within 8 points) is >= mm.xv_gate_ticks (3) above our mid the MM
    posts no ask on any level, when >= 3 below no bid. Expected about +1.6 to +3.1 per 1,000 MM shares (+45 to +90/day),
    at the cost of ~half the MM volume. Re-test after 48 h. Not yet wired: the one-sided-flow pause (the note says wire it
    after the gate and re-measure) and the tuner fixes (being implemented with tests by the mm-upgrade session).
  * high_upside.md: 5-minute market watcher (MARKET WATCH log line on any new or voided exchange; was an hourly refresh);
    3-leg races (RI Governor, NE Senate, MT Senate) get their own 25% per-race ceiling (arb.three_leg_race_frac), not the
    allocator's proportional cap.
  * election_night.md: the sleeve is wired (Bot.election_loop, purpose 'election', 4-s outside polls from 18:00 ET on
    3 Nov, fills fed back via Bot.election_fills) and OFF by default (config election_enabled false; caps 5,000 per race and
    90,000 total as the study asked). Before switching it on: run `python3 -m sigbot.proposals.election_night --replay`
    on a recorded stream and check fill-price units in on_fill (fills are stored in YES terms).
  * Not done, by design: the late punt / correlated roll (Sizhe's call on 3 Nov 17:00 ET).
  Dry runs OK (including one with the election sleeve on); restarted via botctl.
- 20:22 Sizhe: "make more directional trades according to [Polymarket/Kalshi] odds". ExplainedDirection now also trades the
  outside consensus: legs where Polymarket and Kalshi agree within 4 points and this venue is 5+ points away, favourite side
  only (buy the favourite / sell the longshot, per favourite_drift.md); explained verdicts still take precedence; one leg
  per race; min_edge_take 6% and 600 per leg unchanged; direction.overall_cash_frac 0.17 -> 0.60 (allocator ceiling 0.70);
  partisan cap 5,000 unchanged. 24 legs qualify now (12 buy-favourite, 12 sell-longshot). Zero-EV exits now also close
  market-making inventory that sits on the wrong side of a two-venue consensus, but only when the exit beats the outside
  fair by exit_band (e.g. MS Senate R NO, GA Senate R YES). Restarted via botctl.
- 20:20 KILL SWITCH fired (real, not an artifact): marked drawdown 402 over the trailing hour (> 400), from the 19:41-20:04
  offload churn, plus a -2.4 tick 30-min MM markout on 12 fills. Left untouched per Sizhe's rule; MM, tilt and arbs pause
  for 30 min and may pause again until the churn leaves the one-hour window (~21:05).
- 20:55 Kill pause ended 20:52 (drawdown back to 165). Loaded the self-tuner fixes from research_notes/mm_upgrade.md 4.3
  (tests/test_autotune.py: 40 checks pass). Offload exits of tilt/directional holdings that fund other trades now carry
  the arb path's guards: each funded opportunity once per 30 min, each held position offloaded at most once per hour, at
  most one per check, gain >= max(3%, 2 x recycle_min_gain), and never an exit that gives up more than 5% (one at 20:53
  gave up 6.97% for a 1.6% net gain). Restarted via botctl.
- 21:35 HFT study (research_notes/hft_strategies.md, Fable-checked): no HFT strategy pays here (Polymarket follow,
  DMM lag, refill front-running all fail out of sample; not wired). Wired its two fixes: (1) the arb scanner nets our own
  resting orders out of the books it walks (proposals/own_book.OwnLevels, from the cycle's open-order list) and skips a race
  until a feed book newer than our last basket there arrives (17-18 of 33 failed baskets priced a leg off our own tilt/MM
  bid; 7 re-sent on a stale book); (2) sell-sets returning >= 4% to settlement get a 40% per-race ceiling
  (arb.high_ret_min / high_ret_race_frac; ~120k of riskless 4-6.5% sets stand unfilled, Idaho Governor 5.9k alone), funded
  by the 1-s recycler from low-edge sets. Also: exit orders compute their 60-s expiry per order (4 had arrived expired).
  Restarted via botctl.
- 21:45 PAUSED: Fable review of today's diff confirmed money-losing bugs (offload funding tilt trades that are never placed; exits re-sent while the previous one rests, flipping two positions; recycle follow-through blocked by the cash reserve). Fixing under the new precheck gate.
- 21:47 POST-DEPLOY WATCH STOPPED THE BOT: 7 unwinds/recycles in 30 s (start value 101917.6, now 101917.6; {'unwinds': 7, 'exits': 0, 'failed_baskets': 1, 'kill': 0, 'traceback': 89, 'arb_fills': 6}).
- 21:50 Fable review findings fixed (tests in tests/test_guards.py, 25 checks, all pass), deployed through the new gate
  (`deploy/mac/botctl.sh deploy` -> deploy/precheck.sh: compile, guard tests, tuner tests, one clean dry cycle):
  (1) arb-set unwinds to fund tilt/directional trades OFF (offload_arb_for_sleeves false: they fired for tilt trades
  that tilt never placed); (2) recycle follow-through can spend the capital its unwind freed (plan extra_budget; the
  20k reserve had blocked it, e.g. 21:12 Alaska Governor unwound, Montana target "gone"); (3) scanner pre-filter passes
  the snapshot return so the 40% high-return ceiling is reachable; (4) exits net of exit orders still resting (two
  positions had been closed past zero: MA Senate D -759 NO, WV Senate R -626 NO; both closed by hand at the ask, flat,
  ~+58 EV vs holding NO on heavy favourites); (5) unwind legs recorded as orders; (6) a missing leg book no longer
  blocks a race forever after a fill; (7) open-order filled field read under either name; (8) scanner survives a missing
  feed. Not a bug (reviewer refuted by payload): the venue's open-order field is quantityFilled.
  Post-deploy watch (deploy/watch_deploy.py: every 30 s for 10 min, stops the bot on a 150 settlement drop, >3 unwinds,
  >40 exits, a traceback or kill) running. A fake-venue pipeline simulation of every sleeve is being built (tests/).
- 21:55 The post-deploy watcher stopped the bot at 21:47 on a false alarm (my bug: untimestamped old log lines were counted as new, and correct recycles at +4.5% gain were counted as churn). Watcher fixed (lines take the preceding timestamp; recycles only alarm above 12 per 10 min; offload unwinds above 3), bot restarted, watch rerun. The recycles it flagged were good: MN-02 and CA-22 sets at 2.75-3.1% closed to fund Idaho Governor and Virginia Senate at 7.53%.
- 21:50 POST-DEPLOY WATCH STOPPED THE BOT: traceback or kill switch after the deploy (start value 102565.5, now 103444.8; {'unwinds': 0, 'recycles': 5, 'exits': 0, 'failed_baskets': 0, 'kill': 1, 'traceback': 0, 'arb_fills': 6}).
- 21:58 The watch stopped the bot at 21:49 for (a) a KILL SWITCH that was an artifact: marked drawdown 936 while the
  settlement value ROSE +879 (profitable recycling closes sets at the asks and buys better ones at the bids; marked
  value counts sets at mid); and (b) a real scanner crash: fast_requote_pass read params/mm_overrides.json while the
  analytics step was rewriting it (JSONDecodeError). Fixes: the kill switch's drawdown is now measured on the
  settlement-basis value (real arb sets at their locked payout; thresholds unchanged; trailing-hour peak kept in memory);
  the overrides file is written atomically (tmp + os.replace) and a half-written file keeps the previous overrides.
  Tests added to tests/test_guards.py; deployed via botctl deploy (precheck passed). Settlement value 103,446 at 21:52.
- 23:25 PAUSED: the fake-venue pipeline test (tests/sim_pipeline.py) reports 207 violations from 10 bot bugs (lag follower flips positions and bypasses the partisan block; lag+direction stack the per-leg cap; recycle follow-through double-counts freed capital; main-loop recycle backstop races the scanner; outside-fair gate fails open; feed_ok tolerates 2 min of frozen books; own-order netting misses orders placed mid-cycle; kill switch on marks). Fixing all, sim must pass before redeploy.
- 00:55 (23 Oct) RESUMED after the sim-bug pause. Fixed everything tests/sim_pipeline.py found, plus three more its reruns
  and the Fable review turned up; full sim (14 scenarios incl. the 24-h replay) 0 violations, guard + tuner tests pass,
  two Fable reviews (second: SAFE TO DEPLOY), deployed via botctl deploy (precheck now also runs the sim):
  lag follower off (flipped positions, bypassed the partisan block; HFT study had rejected it); recycle follow-through
  budget counts freed capital once on both the cash and capital-cap terms (ArbEngine.follow_budget); main-loop recycle
  backstop only while the feed is down; feed_ok = feed spoke within 30 s (was 120); MM requotes when its outside-fair
  gate changes, waits for a fresh outside fair after a restart or kill pause (max 10 min, never mid-session), and the
  gate's fairs are refreshed right after the directional poll (the scanner's fast requotes read them between cycles);
  the arb scanner nets our orders placed since the open-order read; partisan exposure nets buy-sets as well as
  sell-sets (live netD +1,835, was shown as +2,726); the settlement adjustment reads the arb-race list every cycle (a
  5-min cache made new-race arb bursts trip the kill switch) and the kill switch takes cash, marks and adjustment from
  one locked snapshot; offload logs say "candidate" unless an order is sent. Watcher recycle alarm 12 -> 30 per 10 min.
  Tests: tests/test_guards.py (behaviour tests for budget, gate wait, own-order netting, buy-set netting, feed health).
  Directional-impact study (research_notes/directional_impact.md): do not scale directional trades; edge, not impact,
  is the limit. Daily 06:00 research task set up until 2 Nov (research_notes/BACKLOG.md, briefs in data/daily/).
- 12:40 (23 Oct) DAILY RUN: recycle sizing fix. Finding: at 07:14 BST the recycler unwound 33,458 Idaho Governor sets
  (forgoing 1,021 of locked profit) to fund baskets that plan() could only size at 17,166 (gain 639); the unwind quantity
  was min(held, target depth) and ignored the follow-through's real budget (per-race cap, capital cap, reserve). Over 24 h
  unwinds forgave 4,143 to gain 4,887, with two episodes redeploying only 22-50% of the freed capital. Fix (sigbot/arb.py):
  ArbEngine.follow_through_plan() dry-runs plan() on the target's fresh books with the unwound race's commitment reduced;
  ArbEngine.fit_recycle_qty() shrinks the unwind until >= 95% of the freed capital is spent (recycle_fit_tolerance 0.05,
  sigbot/config.py) and requires (locked edge gained - locked edge forgone)/freed >= recycle_min_gain; wired into recycle()
  after the candidate choice; dry-run return carries proceeds. Tests: tests/test_guards.py (7 behaviour checks: 2,000-cap
  target -> ~2,067 sets not 34,479; nothing to buy -> no unwind; no fresh books -> no unwind; default 15k race ceiling
  sizes the unwind; good recycle still fires; oversized unwind with forgone 827 vs gained 62 refused);
  tests/sim_pipeline.py scenario recycle_oversize (violation recycle_idle when freed capital is not redeployed within 90 s;
  the reviewer confirmed it fails on the old code). Full sim 15 scenarios incl. day: 0 violations. Fable review: SAFE TO
  DEPLOY (notes: only the top candidate is fitted; if recycles stop entirely, check committed > capital cap first).
  Rollback: git revert <this commit> && deploy/mac/botctl.sh deploy. Deployed via botctl deploy (bot stopped and restarted
  by the deploy; orders cancelled on stop), 10-min watch follows.
- 11:50 (23 Oct) Post-deploy watch passed (settlement 102,445 -> 102,479, 0 unwinds, 0 kills, 0 tracebacks). Brief:
  data/daily/2026-10-23.md. Found but not changed (decision for Sizhe): the partisan block is checked once per cycle on
  positions only; at 07:21 it lifted for two cycles and tilt's fresh ladder pushed net D from 4,826 to 5,723 (BACKLOG 0).
- 15:10 (23 Oct) REPORTING FIX, no trading change: sigbot/review.py (the settlement value used by the daily brief and
  deploy/watch_deploy.py) counted only sell-sets at their locked payout; buy-sets (YES on every leg, pays 1) were marked
  at mids. Recycles of sell-sets into buy-sets therefore showed as settlement losses: Idaho -> MN-05 House buy-sets
  (21,162 sets, +986 locked, 00:33-07:24) and RI -> Hawaii Governor buy-sets (7,199 sets, +387 locked, 14:16-14:41). Now
  buy-sets in races the arb engine bought (arb_trades buy_set) count at payout. Corrected settlement value 103,434 at
  15:00 vs 103,423 at 00:33: flat over 14 h (incl. ~2 h of Mac network outages 10:08-11:03 and 13:03-14:06), not -1,000
  as this morning's brief said. The bot's own kill switch (positions_map) already counted buy-sets correctly.
- 15:43 (23 Oct) CASH MODE (Sizhe: objective is now the most liquid cash at ~18:00 ET 3 Nov for the punt; rank does not
  matter). New ArbConfig.new_sets=false gates the scanner, the main-loop arb and both recycle paths; recycle_enabled,
  tilt.enabled, direction.allow_live (polling stays on for the MM gate), allocator.offload_live and allocator.auto_apply
  all false. Kept: free unwinds, zero-EV exits, the MM, kill switch, partisan cap. Tests: tests/test_guards.py "cash mode"
  (live config, scanner stub that fails if a set is planned or recycled, free unwinds not gated). Sim: arb-code
  scenarios force arbs on in their sandbox so that code stays covered; all 15 scenarios 0 violations. Fable review SAFE TO
  DEPLOY. Deployed via botctl deploy (restart cancel-all removed resting tilt/directional orders). Wind-down routine
  (backlog 000) to be built by the daily runs before 31 Oct.
- 15:56 POST-DEPLOY WATCH STOPPED THE BOT: traceback or kill switch after the deploy (start value 103428.8, now 103433.1; {'unwinds': 0, 'recycles': 0, 'exits': 2, 'failed_baskets': 0, 'kill': 0, 'traceback': 4, 'arb_fills': 0}).
- 16:08 (23 Oct) The first cash-mode watch stopped the bot at 15:54 on 4 tracebacks that were all the Mac's network
  dropping (DNS errors / "can't assign requested address" as it went on AC); value was +4 at the time. Watcher fixed:
  tracebacks whose exception is a network error no longer count (deploy/watch_deploy.py code_tracebacks; checked on a
  network traceback (0) and a KeyError (1)). Bot restarted, re-watch passed (103,432 -> 103,432, no sets/recycles).
- 16:40 (23 Oct) Cash mode keeps RECYCLING (Sizhe: "shouldn't we unwind if we detect a better trade"): a recycle swaps a
  held set for a better one and spends only what the unwind freed (ArbEngine.follow_budget = freed in cash mode); free
  cash still never buys a set. Review fix: unspent recycle cash (ArbEngine.recycle_credit) is re-spent before any further
  unwind, a vanished target is cooled down 60 s, the main-loop backstop is off in cash mode and while killed. Tests:
  tests/test_guards.py (scanner pass with a real candidate never plans in cash mode and is sensitive to the switch;
  follow-through budget = freed; no unwind with credit outstanding); sim recycle_cash (24 swaps, spent <= freed) and
  recycle_cash_gone (1 unwind; the pre-fix code made 12 in 30 min). 17 scenarios 0 violations; two Fable reviews (second
  SAFE TO DEPLOY). Deployed via botctl deploy, watch passed (103,407 -> 103,409). Wind-down: unwinds NOTHING for the punt
  until Sizhe sets RAISE_TARGET (30 Oct brief asks).
- 00:16 (24 Oct) HIGH-FREQUENCY ARB live (Sizhe's name; code proposals/dev_churn.py, config churn_*), launched SMALL
  at Sizhe's request: 4k pot / -200 stop until the Fable test-environment check returns (then 12k / -600). Rules: two-leg
  sell-sets only, entry when the race's mid overround is >= 4 ticks above its 24-h median, exit at +2 ticks/set or 24 h,
  never RI Governor / 3-leg races / races where we hold > 4,000 shares on a leg (Sizhe; today MN-05, Hawaii Governor),
  volatility pause (>= 1.5x normal gap movement), no entries from 1 Nov 00:00 UTC, sell-all from 3 Nov 22:30 UTC. Same
  deploy: hourly order-book snapshots (data/books_latest.json), the catch-up sweep loads its 60-min history from stored
  quotes at start-up, main-loop free unwinds read the latest positions under the lock (double-unwind fix found by the sim).
  Tests: tests/test_guards.py (churn:, books:, catchup history), sim scenario 'churn'; 19 scenarios 0 violations. Watch
  passed (Mac network dropped 00:18-00:21, recovered). 114 races have medians; gap movement 0.72x normal.
- 01:00 (24 Oct) HIGH-FREQUENCY ARB SWITCHED OFF (churn_enabled=false, bot restarted, no code change). It had made 0 live
  entries. The Fable test-environment check's adversarial runs found real stress-scenario failures: entries continued
  after the volatility pause triggered (162; the pause is only refreshed every 15 min), cumulative buys far beyond the
  pot and per-race caps in a blowout, and buy/unwind ping-pong with the recycler (27x in one race); the 24-h replay
  fails with it on and passes with it off. To fix and re-test before any relaunch. NOTE: tests/test_guards.py still
  expects churn_enabled=true in the live config; update that check with the fix.

- 2026-10-24 10:58 BST: High-frequency arb back ON at 4k pot / -200 stop (churn_enabled true), on 5228f0c + 34d0d44:
  recycler pump closed (churn races protected), positions snapshot refreshed under the trade lock, sell-all covers every
  set in traded races, 6-tick loss exit, partial fills booked as traded, and every churn exit / free unwind re-sized to a
  positions read taken under the lock right before sending (no leg crosses zero). Tests: 144 guard checks; full sim
  --churn 28 scenarios 0 violations incl. day; Fable review 3 SAFE TO DEPLOY; botctl precheck passed; 10-min watch passed
  (settlement 103,713 -> 103,764). First entries: Tennessee Governor 2,050 sets, MT-01 2,000. Rollback: set
  churn_enabled false and `botctl.sh deploy`. Follow-up in progress: same re-read for recycle()/unwind_set().

- 2026-10-24 13:26 BST: recycle and offload unwinds re-sized to a positions re-read before sending (e9eef45; Opus review 4
  SAFE TO DEPLOY; guards 148; sim --churn 29/29). The first deploy try failed precheck only because the disk was full.
  10-min watch passed. Rollback: git revert e9eef45 + botctl deploy.
- 2026-10-24 14:04 BST: Cheap favourites ON at 15k budget (161c8ab): fast-count favourites SIG prices >= 2c under the
  outside fair, held through the call, own net D within +/-2,000, honours the bot's partisan block with 1,000 headroom,
  skips catch-up races, cash floor 1,000. The first attempt was stopped by precheck (partisan cap / catch-up under the
  live config); fixed and re-reviewed (Opus SAFE TO DEPLOY), precheck sim 29/29 and day 0 violations with it on. First
  6 min: 22 fills, 6,188 shares, ~5.7k cost (RI Sen D, CO Sen D, MA Sen D, WY/WV/OK Sen R, TN Sen R via D NO, CT Gov D);
  cash 8.1k -> 2.4k. 10-min watch passed (settlement 103,847 -> 103,828, the spread paid). Rollback: cheapfav_enabled
  false + botctl deploy (positions are kept; its exits keep running only while enabled).

- 2026-10-24 14:52 BST: Strategy monitor ON (52526c7): pauses a sleeve's new entries (MM resting quotes pulled) when it
  is losing with statistical significance, until Sizhe reviews; exits, unwinds, sell-alls and the global kill switch are
  untouched. First check: mm ok (n 190, +0.084 tick), recycle ok, catch-up / HF arb / cheapfav too few trades. Tests:
  guards, test_strategy_monitor, precheck sim 28/28 with it on, two Opus reviews SAFE TO DEPLOY. 10-min watch passed.
  Rollback: strategy_monitor_enabled false + botctl deploy.

- 2026-10-24 15:24 BST: High-frequency arb loss exit widened 6 -> 9 ticks (param audit; t +2.1 on unseen data, 4-5
  events; -200 stop still binds first). Tests: guards 207/207, precheck sim 29/29, full --churn incl. day 30/30, Opus
  SAFE TO DEPLOY. Rollback: git revert + botctl deploy.

- 2026-10-24 16:53 BST: MM last look uses the freshest touch when the feed book is stale (skips the quote if none)
  instead of posting unchecked (day replay passive_order_crossed on 932). Opus SAFE TO DEPLOY; precheck 30/30. Precheck
  now also runs tests/test_strategy_monitor.py. Cash raise (+ RI trim, shock reserve) FAILED precheck (cash_raise_sets
  counted the HF arb's 9-tick loss exit as a raise); backed out, being fixed. Rollback: git revert + botctl deploy.
