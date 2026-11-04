# Strategy monitor: switch off a sleeve once it is clearly losing (24 Oct)

Sizhe's request: test every new strategy on its real-time results, all the time; once it is statistically clear that a
sleeve is losing money, stop it until Sizhe has looked at it, and keep testing in the meantime.

Code: `sigbot/strategy_monitor.py` (new, not wired yet). Tests: `tests/test_strategy_monitor.py` (all pass).
Wiring: `research_notes/strategy_monitor_wiring.diff` (not applied: another session is editing run.py/config).
Figure: `research_notes/figures/strategy_monitor_decision.png`.

    python3 -m sigbot.strategy_monitor              # read-only report on the live DB (writes nothing)
    python3 -m sigbot.strategy_monitor --simulate   # false alarms and detection delay on synthetic sleeves
    python3 tests/test_strategy_monitor.py

## What each sleeve is judged on (one number per trade)

| sleeve (kill-file name) | one observation | source (read-only) |
|---|---|---|
| market making (`mm`) | 30-min markout in ticks (mid 30 min later minus our price, our favour); fills on one market within 30 min of the first merged, share-weighted | `fills` purpose 'mm', `quotes` (else first `tape` print) |
| catch-up sweep (`catchup`) | realised P&L per closed trade (flat -> position -> flat), ticks per share; open positions only reported | `fills` 'catchup'/'catchup_exit', `venue_fair.sig_mid` |
| high-frequency arb (`hf_arb`, code dev_churn) | realised P&L per exit, ticks per set ("CHURN exit" log lines) + each open lot at what selling it back at the current asks fetches | `data/sigbot.log`, `params/churn_state.json`, `venue_quotes` (sig asks) |
| recycles (`recycle`) | locked profit gained by the follow-through baskets (other races, within 10 min, capped at the capital freed) minus the locked profit the unwind gave up, % of capital freed; a burst of unwinds of one race within 30 min = one observation | `arb_trades` + log lines to tell recycles from churn exits, "fund ..." offloads and free unwinds |
| cheap favourites (`cheapfav`) | per open leg: outside fair now minus our average price (ticks per share, signed for our side), closed legs at their realised exit | `fills` purpose LIKE 'cheapfav%', `venue_fair.fair` |

Why the merging: fills on one market inside one 30-min horizon share the same later mids (live lag-1 correlation 0.46 in
market order before merging, 0.09 after); recycles come in bursts of the same swap (0.56 before, 0.49 after, the rest is a
slowly drifting gain level, handled by the variance below).

## The rule

One-sided **normal-mixture sequential probability ratio test** (Robbins 1970; the "always valid" mSPRT of Johari et al.
2017). With n observations, sum S, plug-in variance s^2 and prior width rho = 0.3 (in units of s):

    evidence L_n = (1 + n rho^2)^-1/2 * exp(rho^2 S^2 / (2 s^2 (1 + n rho^2)))

* **kill** when n >= 30, the mean is negative and L_n >= 100 (alpha 1%); **warn** at L_n >= 10.
* For independent trades with zero edge, L_n is a nonnegative martingale, so by Ville's inequality the chance it EVER
  reaches 100 is <= 1%, however often it is re-checked. A t-test re-run every hour has no such bound (its false alarms
  grow with the number of looks). A positive-edge sleeve is never killed (the mean must be negative).
* Why not CUSUM: it needs a pre-set "bad" mean and its false-alarm rate comes from simulation only; the mixture covers
  every loss size at once and has the analytic bound. Why not a betting confidence sequence: needs hard bounds on each
  observation, which markouts and P&L do not have.
* Robustness additions (each found necessary in simulation): the variance is the largest of the sample variance, the
  Newey-West long-run variance (Bartlett, 5 lags) and the AR(1) long-run variance s^2 (1+r)/(1-r) from the lag-1
  autocorrelation r (capped at 0.8); observations clipped at a generous bound (MM 15 ticks, catch-up/churn 30, recycles
  20%, cheapfav 50); a variance floor; minimum 30 observations (20 let autocorrelated sleeves false-alarm ~2%).
* Cheap favourites hold to 4 Nov, so its legs are a cross-section that all move with the national swing, not a sequence of
  independent trades: kill only on clear deterioration (>= 8 legs, mean <= -2 ticks per share, L >= 1000 and mark P&L
  negative); otherwise at most a warning.

## Simulation (`--simulate`; zero edge 2,000 runs, others 600; the rule checked after EVERY trade)

Noise 1.8 ticks per trade (= live MM markout sd after merging is 1.1; raw fills 1.85), 1,500 trades (~ the rest of the
competition for MM at ~80 merged fills a day):

| noise | autocorr | P(kill), edge 0 | P(kill), +0.5 | -0.5 tick: P(kill), median / p90 trades | -1 tick: median / p90 |
|---|---|---|---|---|---|
| normal | 0 | 0.4% | 0 | 100%, 148 / 295 | 44 / 81 |
| heavy tails (t3) | 0 | 0.2% | 0 | 100%, 121 / 267 | 35 / 69 |
| normal | 0.3 | 1.4% | 0 | 100%, 237 / 514 | 61 / 131 |
| heavy tails | 0.3 | 0.4% (plot run: 1.2%) | 0 | 100%, 200 / 467 | 45 / 112 |
| heavy tails | 0.5 | 1.7% | 0 | 99%, 338 / 814 | 61 / 180 |

A noisier sleeve (4 ticks per trade, only 300 trades, e.g. catch-up or churn): zero edge 0.1-0.6% (1.3% at autocorr
0.5); -0.5 tick caught in only 9-17% of runs, -1 tick in 38-88% (median ~135-155 trades). So the monitor reliably
catches a clearly losing sleeve (>= ~1 tick per trade at MM noise) within a day or two; it cannot prove a small loss on a
sleeve with few, noisy trades before 4 Nov. Those keep their own hard stops (catch-up -50, churn -200/-600).

**False-alarm rate: <= 0.6% for independent or heavy-tailed trades; 1.2-1.7% if trades stay 0.3-0.5 autocorrelated after
merging.** Live residual autocorrelation is 0.09 for MM (well inside); recycles are ~0.5 (so ~1.5% for that sleeve, which
is winding down in cash mode anyway).

Before the fixes: plain variance + minimum 20 gave 6-7% false alarms at autocorrelation 0.3; Newey-West alone 2-3%.

## Live status (24 Oct ~01:20 BST, read-only, nothing written)

| sleeve | n | mean | evidence | verdict |
|---|---|---|---|---|
| market making | 162 merged (from 345 fills) | +0.05 ticks | 0.3 | ok (no sign of losing) |
| recycles | 50 bursts (94 unwinds; 36 "fund tilt" offloads excluded) | +0.93% of freed capital | 2.7 | ok (profitable side) |
| high-frequency arb | 0 exits, 0 open lots | - | - | ok, not tested yet (launched 24 Oct) |
| catch-up sweep | 0 closed trades | - | - | ok, not tested yet |
| cheap favourites | 0 legs | - | - | ok, not tested yet (sleeve being built) |

## Action and wiring (`research_notes/strategy_monitor_wiring.diff`)

* `evaluate(store, now)` returns `{strategy: {n, mean, sd, stat, verdict: ok|warn|kill, reason, ...}}`; new kills are
  added to `params/strategy_kill.json` (`{name: {ts, utc, reason, n, mean, stat}}`, atomic tmp + rename) and never removed
  by the bot. A sleeve stays killed even if later results recover.
* **Re-enable**: Sizhe deletes the sleeve's entry. The monitor notices on its next run and restarts that sleeve's test
  from that moment (`params/strategy_monitor_state.json`), so the old evidence cannot re-kill it at once; new losses can.
* Each sleeve checks the file before NEW entries only: MM stops quoting and its resting quotes are pulled (budgeted
  writes, only orders recorded as purpose 'mm'); recycles stop (scanner recycle_pass, credit spending, main-loop
  backstop); catch-up and high-frequency arb stop entering. Exits, unwinds, free unwinds and the sleeves' own exit
  rules keep running. Cheap favourites: one condition to add once the sleeve is in run.py (diff header).
* Logs `STRATEGY MONITOR: <name> paused: <reason>` once per pause, a one-line summary every run, warnings for `warn`.
* Runs every 15 min (`strategy_monitor_sec`), only writes when live (not in dry runs), never touches the global kill
  switch (`killed_until`, drawdown 400 / markout -1) or any cap. An unreadable kill file keeps the last good content.

### Wiring checks (scratch copy of the repo, nothing live touched)

* `tests/test_guards.py`: same result with and without the patch (one pre-existing failure on both: the churn live-config
  check, config.json is mid-edit by the other session).
* `tests/sim_pipeline.py`, all 19 scenarios incl. the 24-h replay, patched, no kill file: 0 violations.
* Every sleeve paused from the start: calm/catchup/churn/recycle_cash place no MM, catch-up, churn or recycle orders (the
  scenarios' own "sleeve must have traded" checks fire, as they should).
* MM paused 30 min into `calm`: 105 resting MM quotes pulled within ~150 s (write budget), no MM fill afterwards, 0
  violations. A stub test also found and fixed a bug (orders not yet cancelled were being marked cancelled).

## Caveats

* The log is needed to tell churn exits / offloads / free unwinds from recycles and to read churn exits; without a
  matching log line an unwind with forgone profit counts as a recycle.
* Churn open lots are priced at the touch (not walked asks): optimistic for large lots.
* While MM is paused its quote snapshots stop, so later markouts of other purposes fall back to the tape.
* Recycle history includes 22-23 Oct logic that has since changed; `evaluate(..., since={name: ts})` restricts a sleeve's
  test window if Sizhe wants to judge only the current version.
* Cheap-favourites thresholds are a judgement call (cross-section, correlated legs); it can only warn unless the loss is
  large, significant and showing in mark P&L.
