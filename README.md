# sigbot — Susquehanna Predictions Cup bot

Traded the Midterm Elections tournament on https://sig.thesuper.market (play-money SUSQies; the tournament ran until
2026-11-04 12:00 ET, and the bot is no longer live).
Stdlib-only Python 3.10+; no packages to install.

```bash
python3 -m sigbot.run --dry-run --once     # one cycle, logs every intended order, places nothing
python3 -m sigbot.run --dry-run            # loop, still places nothing
python3 -m sigbot.run --live               # trade (arb + market making)
python3 -m sigbot.run --live --no-mm       # arbs + tilt only
python3 -m sigbot.run --live --no-tilt     # arbs + market making only
python3 -m sigbot.analytics --print        # recompute fill-driven MM overrides from the log
python3 -m sigbot.compute_x --half-life 16 # recompute third-party win rates
python3 -m sigbot.backtest --candles data/candles_5m.json
python3 -m sigbot.news --once --dry-run       # news sweeper: poll feeds once, classify, print what it would do
python3 -m sigbot.news_report --backfill      # hit rate / move captured from news_events
```

The API key is read from `SIG_API_KEY` or `.sig_api_key`; the news sweeper also needs `ANTHROPIC_API_KEY` in the environment or in `.anthropic_api_key` next to `config.json`. Settings are in `config.json` (defaults in `sigbot/config.py`).

## What it does each cycle (75 s)

1. Reads account, positions and open orders, then a bulk price snapshot for all 237 markets (6 reads).
2. **Set arbitrage.** Groups markets into 117 races ("Will the D / R / I Party win X?"). For every race:
   * sell-set when the YES bids sum above 1: buy NO on every leg; pays at least `legs - 1`, so the edge
     `sum(bids) - 1` is riskless;
   * buy-set when the YES asks sum below `1 - x`: buy YES on every leg; loses only if nobody listed wins, which
     happens with probability `x`.
   `x` is the exponentially weighted (16-year half-life) historical rate of non-D/R winners since 1970
   (`params/x.json`: senate 1.6%, governor 0.9%, house 0.07%), with per-race overrides in `config.json` for
   states with an independent tradition (Maine, Alaska, Vermont, Minnesota, Connecticut). Races that already have an
   Independent market use the three-way set and `x = 0`.
   Chunks are built by walking all legs' books in lockstep, ranked by return on capital, and executed with
   `POST /orders/multi-leg` in `allOrNone` mode, so every leg fills the same quantity or nothing trades. That keeps
   the arb delta-neutral by construction; a residual check after each cycle re-hedges any leg imbalance beyond
   `delta_tolerance` with a limit order at the touch. Budget: 60% of starting cash, 35% per race.
3. **Fill logging.** New fills are pulled from `/portfolio/fills` and joined to the order we placed (purpose, ladder
   level, ticks from mid, fair, imbalance, inventory at placement) in `data/sigbot.sqlite`.
4. **Tape polling.** Public trades (`side` gives the aggressor) for quoted and arb markets, round-robin.
5. **Market making** on the 40 most liquid markets with mid in [0.06, 0.94]:
   * fair = microprice of the touch, tilted by flow pressure;
   * reservation = fair shifted against race-level inventory (YES on the D leg nets against YES on the R leg);
   * ladder of 3 levels per side at `level_ticks` from the reservation with sizes `base_qty * level_size_mult`,
     increasing with depth (small at the touch, bigger deeper), skewed by inventory and by flow pressure;
   * never crosses the touch; improves it by one tick only when the spread is at least two ticks;
   * requotes only when fair moved a tick, a fill happened, orders are missing or the refresh timer elapsed, using
     cancel-all per exchange plus batch placement, within a per-cycle write budget (30 writes/min account limit);
   * every order carries a 15-minute expiry so quotes die if the bot does.
6. **Tilt harvest** (`sigbot/tilt.py`) on the tails the market maker skips (mid <= 0.12 or >= 0.88). Prices on this
   venue are compressed toward 50% relative to outside markets (measured tilt `s` of 0.09 to 0.13: venue price =
   (1 - s) * true + s / 2), so longshots are overpriced and favourites underpriced. Fair value is the more
   conservative of the mixture model and a logit recalibration (slope 1.5); markets with at least 4% edge per unit
   of collateral get resting sells (buy NO) on longshots and resting buys on favourites at the touch and one and two
   ticks better, sized to grow away from the touch, held to settlement. Budget 25% of cash, 3% per market.
7. **Analytics** every 10 minutes (`sigbot/analytics.py`) turns the log into `params/mm_overrides.json`:
   per-market flow pressure (EWMA of signed tape volume blended with our own fill imbalance), post-fill markouts at
   5 and 30 minutes by side and level, fill rates per level, and from those a recommended per-level size multiplier,
   a spread widening flag and an overall size scale. The market maker reloads the file each cycle.

## News sweeper (`sigbot/news.py`, off by default)

Reads headlines, asks Claude which race moves which way, and takes liquidity before the venue reprices. Enable with
`"news": {"enabled": true}` in `config.json` or `--news` on `sigbot.run`. Two switches gate real orders: the bot must run
`--live` **and** `news.allow_live` must be true; otherwise the sweeper runs dry inside the live bot (classifies, logs
`news_events`, places nothing, does not pause the market maker).
Standalone: `python3 -m sigbot.news --once --dry-run` (add `--no-llm` to see the feeds and pre-filter without a key).

1. **Sources**, every `news.poll_sec` (90 s): RSS from The Hill, NYT, CBS, NBC, NPR and the Guardian; Google News RSS
   `site:` queries for AP, Reuters and Politico (their own feeds block scripted fetches); one Google News query per
   competitive race (a leg priced inside [4%, 96%]), 6 per poll rotating; and the venue's own per-market "Related News"
   JSON (`/api/markets/[id]/news?marketId=N`, a web route outside `/api/v1`, 6 markets per poll rotating). When a
   feed gives only a headline the article's first paragraphs are fetched (robots.txt honoured, 8 s timeout, no JS;
   Google News links are redirects and are not fetched). Items are deduplicated by URL in `news_items`; anything older
   than `max_item_age_hours` (6) is dropped on first sight.
2. **Classification.** A keyword pre-filter (state names, district codes such as `AZ-01` / "Arizona's 1st", office
   words) keeps the political items; `claude-haiku-5-5` screens them in batches of 30 for relevance and race;
   `claude-sonnet-5-5` makes the final call with the race's current venue prices in the prompt: party, direction,
   expected move in points, confidence 0-1, one-line reason, and whether it is new information. Both calls use the
   Messages API over stdlib HTTPS with structured JSON output. Acts only on confidence >= 0.8, expected move >= 3 pts,
   new information.
3. **Execution.** Reference mid = 5-minute candle close before publication. If the market already moved more than a
   third of the expected move, the event is logged as `skipped_moved`. Otherwise it re-reads the touch and buys YES at
   the ask (up) or NO at `1 - bid` (down) for `min(per_event_budget, free sleeve capital)`, with a 120 s expiry on the
   unfilled remainder; sleeve capital is `news.capital_frac` (5%) of cash, per event 1,000 SUSQies, at most 2 events
   per race per day. Positions are held (no automatic exit). Every leg of the race is written to the `news_pause`
   param, which the market maker reads each cycle: it cancels its quotes there and stays out for `mm_pause_sec`
   (30 min), then returns through the volatility gate (wider, smaller).
4. **Measurement.** Every decision, including dry-run and skipped ones, is a `news_events` row with the quotes before
   and the mid at +5, +30 and +120 minutes (filled from the live snapshot while the loop runs;
   `python3 -m sigbot.news_report --backfill` fills gaps from candles). The report prints hit rate (realised move
   at least a third of the expected move), average move captured, and P&L per share versus the fill price, per horizon.
   Run it dry for a few days and size up only if the +30 / +120 hit rate holds above 50% with a positive mean.

## Directional sleeve from outside venues (`sigbot/direction.py`, dry-run only until wired)

Prices every race from LIVE Polymarket (Gamma + CLOB) and Kalshi (public API) quotes and takes SIG liquidity when SIG is
mispriced. `python3 -m sigbot.direction --once --dry-run` prints the mispricing table (race, SIG bid/ask, Polymarket,
Kalshi, fair, edge, size, action); `--loop --dry-run` polls every 30 s (09:00-24:00 ET) / 120 s overnight with about 6 SIG
reads a minute. `python3 -m sigbot.venues.mapping` rebuilds `data/race_map.json` (117/117 races matched by at least one
venue; see `sigbot/venues/`). Fair = liquidity-weighted blend of the venues' party probabilities (sqrt depth / spread),
venue disagreement widens the required edge. Signals: value (SIG ask at least 2 points under fair -> buy YES; bid at least
2 points over -> buy NO) and move (an outside venue moved 2+ points in 5 minutes and SIG did not follow). Sizes scale with
edge and outside depth under caps (1,000 per market, 10% of cash, 3,000 per poll, 30% reserved for move trades) and never
add to a blocked partisan direction. Tables: `venue_quotes` (every change, all venues, one timestamp per poll), `venue_fair`,
`direction_signals` (dry rows are the paper book). `python3 -m sigbot.direction_report --backfill` scores hit rates and
paper P&L at +5/+30/+120 min; `python3 -m sigbot.direction_model [--fetch]` fits the Senate-control -> race-move model
(ridge, cross-market features penalised 10x) on 14 days of outside history plus SIG candles and writes
`params/direction_model.json`. Parent wiring: `DirectionTrader(api, store, None, universe).cycle(prices, positions, cash,
write_budget, dry_run)`; live orders additionally need `"direction": {"allow_live": true}` in `config.json`.

## Layout

| file | role |
|---|---|
| `sigbot/api.py` | REST client: auth, rate budget (90 reads / 27 writes per min), retries, idempotency keys |
| `sigbot/store.py` | SQLite: fills, orders, quotes, tape, arb_trades, cycles, params, news_items, news_events |
| `sigbot/universe.py` | race grouping of markets |
| `sigbot/arb.py` | set-arb scanner, greedy allocator, atomic executor, residual-delta hedger |
| `sigbot/mm.py` | ladder market maker |
| `sigbot/tilt.py` | longshot/favourite harvest held to settlement |
| `sigbot/news.py` | news sweeper: feeds -> Claude classification -> taker orders, MM pause |
| `sigbot/news_report.py` | hit rate and move captured from `news_events` |
| `sigbot/direction.py` | directional sleeve: live Polymarket/Kalshi fair value -> value taking + move following |
| `sigbot/direction_report.py`, `sigbot/direction_model.py` | scoring of direction signals; Senate-shift ridge model |
| `sigbot/venues/` | outside venues: HTTP helper, Polymarket, Kalshi, race mapping (`data/race_map.json`) |
| `sigbot/analytics.py` | fills → pressure, markouts, sizing overrides |
| `sigbot/tracker.py` | per-market vol / flow / book / fill-quality tracker, MM edge score, `params/market_tracker.json` |
| `sigbot/run.py` | main loop |
| `sigbot/shadow.py` | scheduled replays of arb / MM / directional rules on the logs, out-of-sample tuner (`params/shadow_report.json`) |
| `sigbot/allocator.py` | edge per sleeve, capital fractions by measured edge (`params/allocation.json`) |
| `sigbot/tuned.py` | remembered automatic changes and their audit trail (`params/auto_applied.json`, `data/auto_tuning.log`) |
| `sigbot/compute_x.py` | third-party win rate from `data/history/*.csv` |
| `sigbot/backtest.py` | mean reversion / fade / complement-sum / momentum tests on candles |
| `data/` | downloaded snapshots (markets, books, tapes, candles), SQLite, logs |
| `research_notes/` | literature notes: 5 deep-research tracks, a 6-query Sonnet arXiv sweep (`arxiv_sweep/SUMMARY.md`), public alpha |
| `reports/` | the sizing report |
| `params/` | `x.json` (third-party rates), `mm_overrides.json` (written by analytics) |

## Tests

Stdlib only; each script prints a `PASS` / `FAIL` line per check and exits non-zero on any failure.

```bash
python3 tests/test_guards.py                 # guard regression tests, about a minute
python3 tests/test_gap_failure_paths.py      # gap pilot failure paths
python3 tests/test_offload.py                # offload executor
python3 tests/test_strategy_monitor.py       # strategy monitor
python3 tests/test_strategy_stops.py         # strategy stops
python3 tests/test_autotune.py "$PWD"        # shadow tuner + allocator, about 3 min; needs data/sigbot.sqlite (below)
python3 tests/sim_pipeline.py --list         # fake-venue scenarios; `python3 tests/sim_pipeline.py <name ...>` runs them
deploy/precheck.sh                           # all of the above, then one dry cycle against the venue (needs the API key)
```

What a fresh clone does not have: `data/sigbot.sqlite`, the bot's runtime log (SQLite, about 1 GB by the end of the
tournament, gitignored with the rest of `data/`'s runtime files). `tests/test_autotune.py` replays it read-only and
stops with a message when it is missing; a live or `--dry-run` run of the bot creates it. Everything else runs from
the tracked files, including two fixtures under `data/`: `research_contract_rules/tc_mapping.json`, the contract-terms
map the cheap-favourites sleeve, the guard tests and the simulation read, and `books.json`, the 21 Oct book snapshot
that seeds every fake-venue scenario.

One known failure: with the end-of-tournament database, `tests/test_autotune.py` reports `group mm_ladder: ... cpu`
above its 8 s budget plus 1 s. The tuner checks its CPU budget between parameters, so one parameter's replay on the
dense election-week window can overrun it; the tuner then skips the remaining parameters as designed. It is a timing
check on the real data, not a code error, and it also failed in the bot's own precheck on 3 Nov.

## Findings that shaped the parameters

* Books: median touch size ~2.4k shares, growing to ~170k shares 10 ticks deep (the designated market maker's
  ladder); 172 of 237 markets have a one-tick (0.005) spread. We are tiny relative to the DMM, so touch fills come
  from queue position; the edge is in the inside quotes on the 65 wider markets and in reacting to flow.
* Hourly candles: price changes are strongly mean-reverting (AR(1) slope −0.21, t −46), momentum continues only 21% of
  the time, and the complement sum of a race's legs reverts with a ~9-hour half-life. Fading moves as a taker loses
  after the spread, so the mean reversion is harvested passively by the ladder, not by taking.
* 5-minute candles: AR(1) slope on price changes is −0.45 (t −228); fading a 3-sigma 5-minute move earns +1.7 ticks
  on average, still +0.33 cents a share after crossing the spread. That is why the ladder keeps larger size deeper
  (being filled during a sweep is good on average here), the opposite of the equity-market literature; the analytics
  switch a market to a touch-heavy or off-the-touch ladder when its realized markouts say otherwise.
* Public participant data (research_notes/public_prediction_market_alpha.md): the one instrumented public bot made
  about 15% per unit of cash from the tilt harvest and 5 cents a share from cross-venue value versus Polymarket;
  taker mean-reversion, imbalance taking, copy-trading and tilt momentum all lost money.
* Leaderboard: top accounts sat near +900k P&L on 100k starting cash on 21 Oct. Riskless arb capacity across all
  races was ~24k of profit on 1.6M of capital at that moment; with 100k the greedy plan captures ~2.5k. Arb alone
  will not win a prize; it is the safe floor under the market-making book.

## Exposure limits (`sigbot/risk.py`)

* Per race: inventory is measured net across the race's legs (long D-yes offsets long R-yes) as SUSQies at risk;
  quotes skew and shrink toward `max_inventory_notional` (600) and the accumulating side is pulled entirely at the cap.
* Portfolio: net partisan notional (long-D minus short-D, where short R counts as long D) across all non-arb positions
  is capped at `portfolio_max_partisan_notional` (5,000); beyond it the market maker and the tilt sleeve stop posting
  any order that adds to the heavy side. Arb sets (NO on every leg) are netted out first because they are neutral.
* Post-sweep gate: a 3-tick move in 15 minutes or doubled hourly variance marks a market hot for 30 minutes; the
  adding side is pulled, the reducing side halves and anchors at the pre-move mid, levels widen a tick.
* Every fill is tagged with the sleeve that produced it (mm, arb, tilt, hedge) via its order id.

## Self-monitoring (replaces manual minute checks)

* Every cycle logs a `HEALTH` line: fills by sleeve over 5 and 30 minutes, the mean 30-minute markout of
  market-making fills, drawdown of marked value against the trailing-hour peak, net partisan exposure, wave/hot state.
* Kill switch: a drawdown above `kill_drawdown` (400) or a 30-minute markout below `kill_markout_ticks` (-1 tick,
  at least 20 fills) cancels all market-making and tilt quotes and pauses both sleeves for `kill_pause_sec` (30 min).
  Arb sets are never touched (they never rest).
* Analytics rerun every `analytics_every_sec` (5 min) and resize the ladder from realized markouts and flow.
* Cycle is 30 s from 09:00 to 24:00 Eastern and 60 s overnight; order books are only re-read when the touch moved
  or a requote is due; the client read budget is `reads_per_min` (65) because the account limit is shared.
* Mean-reversion skew: the market maker builds 5-minute bars from live mids (seeded from candle history) and shifts
  its fair value by the 1-bar ridge prediction (`params/meanrev.json`), capped at one tick, so quotes lean into the
  predicted reversion instead of taking it.

## Self-tuning: shadow replays and edge-based allocation (`sigbot/shadow.py`, `sigbot/allocator.py`, `sigbot/tuned.py`)

The bot backtests itself on its own logs (`data/sigbot.sqlite`, read-only) on a schedule and, when allowed, changes its
own parameters, but only after an out-of-sample test passes. No orders and no venue calls are made by any of this.

```
python3 -m sigbot.shadow --report                 # latest table (params/shadow_report.json)
python3 -m sigbot.shadow --run [--groups arb,mm_quote] [--hours 48]
python3 -m sigbot.shadow --sizes 100,200,400      # MM base_qty replay (information only, never applied)
python3 -m sigbot.allocator --report              # edge per sleeve and recommended capital fractions
```

| group | parameters | runs every | lookback |
|---|---|---|---|
| arb | `arb.min_ret`, `arb.recycle_min_gain`, `arb.max_race_frac` (tighten only); free unwinds shown only | 4 h | 24 h |
| direction | `direction.min_edge_take`, `exit_band` | 12 h | 72 h |
| mm_quote | `mm.requote_ticks`, `mm.vol_gate_fade_mult` (`mm.meanrev_max_ticks` shown only) | 12 h | 48 h |
| mm_ladder | `mm.level_ticks`, `mm.level_size_mult` | 24 h | 96 h |
| allocator | `arb.capital_frac`, `mm.capital_frac`, `tilt.capital_frac`, `direction.overall_cash_frac` | 12 h | 48 h |

* Replays. Arbs: SIG touch of every leg from `venue_quotes`, starting from the sets the bot actually held at the start
  of each window; size at the touch assumed (500 sets). Market making: the ladder rebuilt from `quotes` snapshots;
  each order fills its calibrated share of the public tape volume at or through its price, measured from our own orders
  by distance from mid (replaying the live parameters reproduces the real fills within a few %). Directional: value
  takes on verdict legs and zero-EV exits from `venue_fair`, marked at outside fair and at SIG mid.
* Test. Candidates are picked on the older 70% of the window and scored on the newest 30%. A change needs: hard bounds,
  test gain >= max(10, 50/day of test window, 10% of the current score), bootstrap confidence >= 0.90 that the mean
  per-block gain over 8 test blocks is positive (directional: on both marks), no worse drawdown, and the same answer from
  two consecutive scheduled runs. Score = settlement-value P&L minus 1 per 1,000 of capital per day.
* Allocator. Measures each sleeve's realised edge per 1,000 of capital per day over 48 h (arbs: locked profit minus a 1%
  unwind cost, capital held to Nov 3; MM: P&L with inventory at mid vs resting + inventory capital; tilt and directional:
  the lower of the fair and mid marks). It splits 95% of account value in proportion to edge within floors (20% of the
  current value) and ceilings (arb 0.70, MM 0.35, tilt 0.12, directional 0.04; worst case under 50,000). A change needs
  the full window, the older 70% and the newest 30% to agree on its direction, two consecutive runs, and moves halfway.
* Hard bounds never touch the kill switch, inventory or partisan caps, all-or-none arbs, `mm.base_qty`, or the arb 70% /
  10% and directional 300 / 3,000 ceilings.
* Applying. Off by default: `config.json` `"shadow": {"auto_apply": true}` and `"allocator": {"auto_apply": true}`
  switch each on. Applied values go to the live config objects, `params/auto_applied.json` and the audit trail
  `data/auto_tuning.log`; after a restart they are re-applied only if `config.json` still has the value they replaced,
  so a manual edit of `config.json` always wins. Log lines: `SHADOW ...`, `SHADOW APPLIED ...`, `ALLOCATOR ...`.
* In the bot: the existing `shadow_tick(self)` call at the end of `Bot.cycle` drives everything (the allocator too), one
  group at a time in a background thread, at most 8 s of CPU per group (arb ~1 s, directional ~0.6 s, MM ~3-4 s).
  Schedule state lives in `params/shadow_state.json` and `params/allocator_state.json`.

## Safety

* Default is dry run. `--live` is the only way to place orders.
* Buys are admitted against cash only on fill; resting quotes across markets can exceed cash, so the MM budget is
  capped at 30% of cash and the arb engine takes capital first.
* Quoting stops at `stop_quoting_at` (election day noon UTC) to avoid results-driven flow; set it earlier if needed.
* One account, one person: the rules forbid shared accounts, multiple accounts and collusion.

## Mean-reversion model (`sigbot/meanrev.py`, `params/meanrev.json`)

```bash
python3 -m sigbot.meanrev --train --candles data/candles_5m.json   # refit, walk-forward report, writes params/meanrev.json
python3 -m sigbot.meanrev --report                                  # 20 markets with the largest current predicted move
python3 -m sigbot.explain_moves                                     # largest 5-minute moves vs. headlines (+ Claude one-liners if a key exists)
```

Ridge regression (z-scored features, strength chosen on the last 30% of the training window) of the next 1-bar and
next 6-bar change of a market's YES close on 14 features from its own candle window and the other legs of its race:
close change over 1/3/6/12 bars, deviation from the 12- and 36-bar EWMAs, bar range over the rolling 36-bar
volatility, volume and tradeCount z-scores, complement-sum deviation (sum of the race's leg closes as-of the bar's
timestamp minus 1), distance from 0.5, UTC hour (sin/cos) and the log gap since the previous bar (candles exist only
for bars with trades, so one "bar" is 5 minutes only about half the time). Validation is walk-forward in time:
rows whose target is fully realised before the 70% time cutoff (2026-10-15 20:15 UTC) train, rows after it test;
feature scaling and the ridge strength come from the training rows only. Prices outside [0.05, 0.95] are excluded.
Pure Python (`statistics` + a 14x14 Gaussian elimination); a refit takes about 30 s.

Out of sample (61,707 test rows, 237 markets, 15 to 21 Oct):

| horizon | R² | corr | share with \|pred\| ≥ 0.25c | hit rate there | edge | taker (−0.5c spread) | passive (+0.25c) |
|---|---|---|---|---|---|---|---|
| 1 bar | 0.117 | 0.36 | 15.8% | 83% (W 4971 / L 995 / flat 3808) | +0.31c | **−0.19c** | +0.56c |
| 1 bar, \|pred\| ≥ 0.5c | | | 2.4% | 79% | +0.37c | −0.13c | +0.62c |
| 6 bars | 0.029 | 0.18 | 10.3% | 69% (W 3283 / L 1447 / flat 1653) | +0.24c | **−0.26c** | +0.49c |

"edge" is the mean realised change in the predicted direction per share; "passive" assumes the fill happens.
Conditioned on the next bar actually trading through the passive level the P&L is negative (−0.13c for the 1-bar
model at the 0.25c threshold, fill rate 14%): a resting order one tick inside the close is filled mostly when the
price keeps going, so the signal has to be used as a quote skew and sizing input, not as a reason to take.
Features by |t| (1-bar model): `d1` −76, `d3` −36, `dev_ewma12` −28, `comp_dev` −18, `d6` −14; everything else
|t| < 4. Every price-change coefficient is negative (one sd of `d1`, 0.65c, predicts −0.14c next bar).
The complement sum is the fourth-strongest feature and the only non-own-price one that matters: dropping it costs
0.0012 of R² at 1 bar and 0.0052 at 6 bars (t −18 and −33); a race whose legs sum above 1 sees each leg drift back.
Hour of day, volume, tradeCount, range and distance from 0.5 add nothing out of sample.

To retrain: `python3 -m sigbot.meanrev --train` (add `--lambda 0.1` to fix the ridge strength, `--split 0.8` to move
the cutoff, `--no-save` to only print the report). In code: `f = meanrev.build_features(candles, others=[leg candles])`
then `meanrev.predict(f, horizon=1)` returns the expected change in the YES price; `meanrev.load()` / `save()` handle
`params/meanrev.json`. `explain_moves.py` writes `params/move_explanations.json`: of the 40 largest moves in the whole
candle history (all on 1-2 Oct, the opening session: 20c->89c->21c swings on a dozen trades) none has a headline,
because `news_items` only starts on 21 Oct; of the 40 largest moves inside the news table's coverage only 3 have a
matching headline (two U.S. Senate control moves after the NYT / Cook poll stories, one Oklahoma candidate guide); the
rest are one-to-fifteen-trade prints in thin books, which is what the model fades.

## Realtime feed (`sigbot/feed.py`, stdlib only)

`RealtimeFeed(api_key, base_url, tournament_id).start()` keeps a background daemon thread on the venue's Supabase
Realtime WebSocket (Phoenix channels; RFC 6455 framing, TLS, heartbeats, token refresh and capped-backoff reconnects
are all in the module, no pip packages). It mints `POST /realtime/token`, joins `tournament:{id}:markets` and our
`user:{profile}` channel as private broadcast channels, and keeps one versioned book per exchange in the REST
orderbook shape (`book(ex)`, `books()`, `best(ex)`; each carries `asOf` and `feed_ts`). `drain_trades()` returns the
public prints since the last call, `drain_fills()` our own fills from `account_batch` (REST fill shape, side-denominated
`price` plus `price_yes`; realtime fills carry no `id`). `needs_resync()` lists exchange ids whose book must be refetched
over REST (initial join, reconnect, token refresh, revision gap, `resyncRequired`, `book_dirty`); clear them with
`mark_resynced(ids)`. `stats()` / `healthy()` feed the HEALTH line. Verified live on 22 Oct: both joins ok, about 25
market batches per second (median message 73 KB, max 496 KB), 236 of 237 books held within 90 s (the topic only sends
books for markets that changed, so seed the rest over REST at start), every revision step exactly 1, trades arrive
about 2.4 s after `executedAt`, books about 0.7 s after their `asOf.at`, and best bid/ask matched REST on 3 of 3 checks.
Probe: `python3 -m sigbot.feed --probe 90` (add `--no-rest` to skip the 3 REST orderbook reads).

## Market tracker (`sigbot/tracker.py`, stdlib only, no venue calls)

Per-exchange state the bot pushes into each cycle and can rebuild from the log: `tracker = Tracker(universe)` or
`Tracker.from_store(db_path, hours=24)` (read-only SQLite: `quotes` for the quoted markets' mids, `tape` for every
market's prints, `fills`/`orders` for our MM activity); then `tracker.update(feed.drain_trades(), feed.books(), now,
fills=new_mm_fill_rows, quoted=mm.quoted)`. Tracked per exchange and rolled up per race in `snapshot()["races"]`:
realised vol of the mid at 5 m and 1 h (EW, half-life 2 h, ticks; `nvol_*` divides changes by sqrt(p(1-p)); tape-price
vol when no book), trades/h and volume/h (EW), average print size, quoted spread, best-level depth, the designated
maker's 1,800-2,700 touch clip (`dmm_touch`, `dmm_frac`), sweeps (>= 2 ticks within 60 s), our MM fills per quoted hour,
5 m / 30 m markouts (mean, n; same sign convention as analytics.py). `score(ex)` = fills/h x (half-spread captured -
adverse 30 m markout) = fills/h x markout_30m, own data weighted n/(n+10) against a prior (k x venue trades/h with k
pooled from exchanges with >= 10 fills; spread/2 minus the pooled adverse markout); `rank(n, price_band=(0.12, 0.88))`
gives the top n; `params/market_tracker.json` is rewritten every 5 minutes from inside `update`. CLI:
`python3 -m sigbot.tracker --report [--hours 24] [--band 0.12 0.88] [--write]` prints the top 25 by score;
`--research` prints the volatility vs volume / fills / markout tables behind `research_notes/volatility_vs_mm_edge.md`.

## Running on the Mac without the app (launchd)

The live bot runs as a launchd agent so it survives the Claude app quitting, terminal tabs closing and crashes:

```bash
deploy/mac/botctl.sh status     # launchd state, process count, last cycle line
deploy/mac/botctl.sh stop       # SIGTERM: the bot cancels every resting order, then the script verifies the book is empty
deploy/mac/botctl.sh start
deploy/mac/botctl.sh restart    # after a code or config change
deploy/mac/botctl.sh logs       # follow data/live.log
deploy/mac/botctl.sh uninstall  # stop and remove ~/Library/LaunchAgents/com.sizhe.sigbot.plist
```

It still needs the Mac awake and online: launchd keeps it running, caffeinate keeps the Mac from idling to sleep, and
`pmset disablesleep` covers a closed lid. For trading with the laptop off, use the server kit in `deploy/`.
