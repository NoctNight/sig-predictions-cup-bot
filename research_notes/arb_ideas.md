# Three cash-by-3-Nov ideas: wide-overround sets, SIG favourite drift, election-night timing (25 Oct 2026)

Read-only. No code, config, params, deploy or bot changes; no SIG calls; local data only. Script
`research_notes/arb_ideas_study.py` (`idea1`, `idea2`, `idea3`, `plot`; each well under a minute, run through
`deploy/simq.sh`, with numpy/pandas from `uv run --with`). Outputs are in `data/research_arb_ideas/`. Figure:
`research_notes/figures/arb_ideas_decision.png`.

**Scope change (Sizhe, in this session):** "don't necessarily have the ideas be about the normal arb. have them be about
any kind of trading strategy". The original idea 2 (recycler tuning) is moot in cash mode: `arb.new_sets=false`
switches off recycles too, so its parameters cannot change cash by 3 Nov. It was replaced with a non-arb idea, the
multi-day drift of SIG favourites. Ideas 1 and 3 are as briefed. The bar for every idea: adds liquid cash by ~18:00 ET on
3 Nov, out of sample, t > 2.

## Verdicts

| idea | verdict | key numbers |
|---|---|---|
| 1. Buy sets when the overround is unusually wide against a multi-day level, hold for days, sell before 3 Nov | **DROP** | The rule picked in-sample (sell-sets, 24-h median, k = 4 ticks, exit at the usual exit price, 24 h) makes +0.86 tick/set in training (t 4.8), and **+0.41 out of sample (t 0.8, n 29)**. The best test cell, +2.1 tick/set at t 3.1 (48-h median, k 4, held 48 h), is 13 trades all entered within 15 h on 23 Oct, which is one episode, and 1 of 76 test cells with n ≥ 10. A multi-day baseline does no better than the 24-h one. This is the high-frequency arb's own signal, and that arb measures ~0 on real quotes. |
| 2. (new) SIG favourites drift up for days before the election: buy now, sell 3 Nov before any call | **DROP** | The apparent test-period drift (+2.6 to +15 ticks per unit of p − 0.5, t ≈ 10) sits entirely in windows that cross the 21/22 Oct switch from trade VWAP to quote mids. **On real quotes alone the slope is +0.1 to +1.1 (t 0.3-1.8)**, and buying a 0.75-0.90 favourite nets about **−0.9 tick a share** after the 1.2-tick spread at 1-3 days. Per-day slopes flip sign day to day (one common shock a day), so the effective sample is a handful of days. |
| 3. Election-night timing: how much extra cash waiting for the calls adds, by punt time | **INFO** (no change needed) | Early-call holdings are MN-05 (21,162 buy-sets, 20.35k sale value today) plus the cheap-favourite sleeve (36 legs, 14.5k today). **Waiting for their calls instead of selling before 18:00 ET adds about −0.15k to +0.6k by 21:30-01:00 ET, and +1.55k only if SIG settles a race at the call** (never observed). Buying more early-call sets now adds ≤ ~0.1k in total: drop. |

## Idea 1: wide-overround sets, held for days

* **Hypothesis.** A race's overround (sum of YES bids − 1 for sell-sets; 1 − sum of asks for buy-sets) that is well
  above its multi-day level reverts. Hold for days, then sell before the deadline at a better price than both spreads cost.
* **Data.** Training: `race_paths_30min.csv` panel A (14-21 Oct, trade-price proxy, 113 races). Test: `venue_quotes`
  SIG, all legs every 30 min, 22-25 Oct (real touch).
* **Test.** Grid: side (sell, buy) x trailing median L (24, 48, 72 h) x threshold k (2, 3, 4, 6, 8 ticks) x max hold
  H (6, 24, 48, 72 h) x exit (fixed at H, or "level 0": the first bar whose exit price is at least as good as the
  race's 24-h median, the wind-down's rule). One position per race at a time. Profit = entry − exit at the touch
  (both spreads paid). t is clustered by race. The rule is chosen on training by t and read on test.
* **Result** (figure panels A-C):
  * Training is positive for sell-sets at k ≥ 4 in most cells (proxy noise inflates this ~1.5x; see arb_convergence.md).
    Buy-sets are weak even in training.
  * The chosen rule fails the test: +0.41 tick/set, t 0.8. The test cells that look good (sell, k ≥ 4, held 48 h:
    +1.3 to +2.1 ticks, t 1.9-3.1) are 13-17 trades, nearly all entered between 00:30 and 15:00 UTC on 23 Oct. The
    median sum of asks also tightened market-wide on 25 Oct (1.015 → 1.010), which flatters every exit in the window.
  * "Multi-day level" adds nothing: L = 48/72 is no better than L = 24. Holding 48 h instead of the HF arb's 24 h is a
    +0.5-1 tick difference in both panels, but not significant out of sample.
* **Capacity** (not reached, since the test fails): venue_quotes logs no sizes for SIG. Using the ~2k-share touch
  from maker_arb.md and ~13 signals on the one active test day, the ceiling would have been about +0.2-0.3k/day *if*
  the +1-2 tick edge were real.
* **Decision: DROP.** Cash mode stays as it is for plain sets. If anyone revisits the HF arb's 24-h forced exit, a 48-h
  max hold is the only variant worth a paper look, and it needs a week of new quotes first.

## Idea 2 (replaces recycler tuning): multi-day drift of SIG favourites

* **Hypothesis.** SIG compresses prices toward 0.5 (favourites ~6 ticks cheap, sig_convergence.md). If that
  compression unwinds as election day nears, favourites rise before any call, so buy now and sell on 3 Nov before 18:00
  ET with no call risk.
* **Data.** One price per leg per day (15:00-21:00 UTC): the median 5-min VWAP from `candles_5m.json` (1-21 Oct), then
  the median quote mid (22-25 Oct). Spreads come from the real quotes (median 1.2 ticks).
* **Test.** Forward change over 1-7 days by price bucket, and the slope of the change on (p − 0.5), clustered by race.
  Training starts are up to 19 Oct, test starts 20-25 Oct.
* **Result.**
  * Training shows no decompression (slope −0.5 to −7 ticks; mild compression, partly VWAP bounce).
  * The test looks strongly positive, but only in windows that span 21 → 22 Oct (+7 to +12, t 6-10). On those days
    the data switches from trade VWAP, which sits below the mid on favourites because takers sell favourites and buy
    longshots, to the quote mid. It is an artefact.
  * Real quotes only: slope +0.09 (h 1, t 0.3), +1.08 (h 2, t 1.8), +0.37 (h 3, t 0.5). Favourites at 0.75-0.90 gain
    +0.2-0.3 tick gross and **−0.9 net**.
* **Self-check.** With only 3 real days, a drift of ~0.15 tick/day can't be excluded. That would just cover the
  spread over the 9 days left, about +1 tick/share, or ~+50 S on 10k shares. Immaterial either way.
* **Decision: DROP.** sig_convergence.md already found the per-leg offset steady and not converging. This adds that
  there is no time trend toward election day either, as far as the data go.

## Idea 3: election-night cash by punt time

* **Question.** If the punt goes in after time X ET, how much extra cash do early-call positions bring if held to their
  call instead of sold before 18:00 ET? And is buying more early-call sets now worth it?
* **Data.**
  * Positions: `params/wind_down_report.json` (25 Oct 18:23 ET). MN-05, 21,162 YES-sets, is the only early-call set;
    RI Gov and Hawaii are sold as late anyway. Cheap-favourite legs come from `fills` purpose `cheapfav`, net per leg.
  * Prices: the last SIG quote per leg and `venue_fair`. Depth: `books_latest.json`. Poll closes:
    `data/election_night/poll_close_2026.json`.
* **Model.**
  * A position becomes sellable 30 min after its state's last poll close. Safe seats are called at the close
    (election_night.md: AP lead +1 min for ≥ 0.95 races), and SIG's maker lags by tens of minutes.
  * SIG's post-call prices have never been observed; no market has settled. So three scenarios:
    * **A**: SIG keeps today's compression. Today the bid on legs with outside fair 0.97-0.99 has median 0.945 (26
      legs), and on legs with fair 0.01-0.03 the bid is 0.06 and the ask 0.065 (8 legs).
    * **B**: SIG reprices to near-certain (0.985 / 0.005).
    * **D**: SIG settles at the call (1.00).
  * Losing-side probability uses the outside fair. Walk cost comes from today's ladder. MN-05 sells in 1,000-set
    clips every 30 min, the wind-down rule, which takes ~10.5 h for all of it.
* **Result: extra cash vs selling everything early-call before 18:00 ET at today's prices** (`idea3.json`, figure
  panel D):

  | punt after (ET) | positions called | A (compression persists) | B (reprices to near-certain) | D (settles at call) |
  |---|---|---|---|---|
  | 19:00 | 0 | 0 | 0 | 0 |
  | 20:00 | 5 | −0.08k | +0.01k | +0.04k |
  | 20:30-21:00 | 29-30 | −0.10k | +0.39k | +0.58k |
  | 21:30-23:00 (MN-05 sellable) | 37 | −0.15k to −0.07k | +0.49k to +0.54k | +1.55k |
  | 01:00 | 37 | +0.04k | +0.61k | +1.55k |

  * MN-05 alone: today's sale value is 20.35k (0.962/set). Sold after the call it brings 20.6k all at once or 20.9k
    in clips (A), 20.4k / 20.7k (B), or 21.16k (D). So waiting is worth +0.0 to +0.8k, and the clips are mostly
    unsold before ~01:00 ET.
  * Why A gains on the set but not on the favourites: a buy-set sells for winner bid + loser bid, so SIG's compression
    cancels out (0.945 + 0.055 ≈ 1.00). A lone favourite only reaches ~0.945, which is about its price today.
  * Cheap favourites (36 legs): 14.53k today vs 14.38k (A), 15.02k (B), 15.27k (D) at the call. Their upside depends
    on SIG decompressing after the call.
* **Buy early-call sets now** (all legs in states closing by 21:00 ET, favourite ≥ 0.95; 38 races checked): only four
  races have a positive A case at 1,000 sets (SD Sen, MA Gov, ME Gov, WY Gov; +5 to +15 S each, up to +37-61 S at 5k
  sets in MA Gov). B is negative for most of them, and ME is ranked-choice. The total is ≤ ~0.1k, and it ties up punt
  cash until the call. **Drop.**
* **Decision: INFO.** Holding early-call positions to the call, rather than selling before the punt, is worth about
  +0.5k in the central case (B) and at most +1.6k (if SIG settles at the call). Against a ~70k punt budget, punt
  timing should be set by the punt's own logic, not by set cash.
  * **The biggest unknown is still whether SIG settles or reprices at the call** (BACKLOG 0000 addendum). If it settles
    (D), MN-05 alone is +0.8k by ~21:30 ET.
  * If Sizhe punts at or before ~19:00 ET, sell MN-05 before then (the 1 Nov brief question already covers this). The
    loss vs waiting is ≤ ~0.8k.

## Caveats and what is unfinished

* Idea 1 uses touch prices with no depth walk (`venue_quotes` has no SIG sizes), and training is a trade-price proxy.
  Both make it optimistic, and it still fails.
* Idea 2 has only 3-4 days of real quotes, so a small trend can't be excluded, but it would be immaterial.
* Idea 3's post-call SIG behaviour is a scenario, not a measurement. Depth after a call is assumed to look like
  today's ladder. Call times are poll close + 30 min for safe seats only; MN-05 is safe D (fair 0.964).
* Not done: no Opus/Fable review (no GO to review; the brief allowed one per GO). The one adversarial self-check
  per idea is the "why the test is weak" / junction / scenario analysis above.
