# MM upgrade: tuner history, prediction-market literature, tested rules (22 Oct 2026, ~20:15 BST)

Paper only (DB read-only, no venue calls). Data: 21 Oct 22:43 to 22 Oct 18:54 UTC (19.8 h): 262 MM fills, 23.4k shares, 41
markets; public tape 16.6k prints (aggressor known); `venue_fair` Polymarket/Kalshi fair per leg every ~2 min.
Reproduce: `python3 research_notes/mm_upgrade_study.py history|slices|counterfactual|robust|settle|converge|candidates|plot`
(outputs in `research_notes/mm_upgrade_data/`). Replay: `sigbot/proposals/mm_upgrade.py` (`simulate_x` = shadow.simulate_mm plus
the candidate knobs; `python3 -m sigbot.proposals.mm_upgrade` checks it reproduces simulate_mm exactly: OK).

## Verdict
* **Tuner drift is noise.** 17 rebuilt hourly runs: the fit-window optimum changed 26 times across 6 parameters; 3 changes were
  statistically real (two vol-gate flips worth < 0.005/h, one meanrev flip that reversed the run before). No run of any parameter ever met gain >= 10 with confidence >= 0.9.
* **The MM's loss is slow cross-venue adverse selection, which a 30-minute markout cannot see.** 52% of MM shares traded >= 3 ticks
  on the wrong side of the Polymarket/Kalshi fair (selling favourites / buying longshots into SIG's compression). SIG then drifted
  toward the outside fair: those fills mark -0.71 tick at 3 h and -1.01 at 6 h (market-clustered 90% CIs [-0.99,-0.41], 21
  markets, and [-2.20,-0.07], 17 markets); fills >= 3 ticks on the fair's side mark +0.46 / +1.16. At 30 min: -0.62 (CI spans 0).
  The premise holds venue-wide (Fable, venue_fair, 207-216 markets): a gap >= 3 ticks predicts SIG moving +0.10 / +0.61 / +1.24
  ticks toward the outside fair at 30 min / 3 h / 6 h (two-way clustered t 5-7), ~0 for smaller gaps; even the 3% of big gaps
  that run against the favourite converge. One day of data.
* **Two rules clear on realised fills.** The outside-fair gate (do not quote the side that trades against a fresh outside fair by
  >= 3 ticks) at 3-6 h marks, and a one-sided-flow pause (no level 1 on the side that >= 80% of the last 10 min of public volume,
  ending 30 s ago, traded into) at 5 min. In the fill-model replay nothing clears the tuner's bar, and the bar itself is broken
  (section 3B). Every other rule's apparent edge is one event cluster: 28 fills in markets 1057 and 1033 (13% of volume, -31).

## 1. Self-tuner history versus the literature
Logged runs: one scheduled run per MM group (mm_quote 10:34 UTC, mm_ladder 10:36 UTC; nothing confirmed). The four earlier hourly
runs (11:14-11:28 BST) logged "0 recommendations" without values. Rebuilt: 17 runs (hourly 04:00-18:00 UTC, the logged 10:33 run,
and 18:42). Each uses the window [first log, T] (the 48 h / 96 h lookbacks exceed the data), fit = older 70%, test = newest 30%,
the MM config in force at T (timeline from data/changes.md), the tuner's coordinate search, its 8-block test bootstrap, and three
additions: an hour-block bootstrap of the fit window giving P(each value is the optimum) (dot size in the figure); a test of
whether consecutive runs' optima differ (hour-block bootstrap CI of the score difference between the two optima, on the later
run's fit window); and hours to resolve (z = 1.28 for one-sided 90%, sd/mean of hourly differences, and 10 / mean for the
materiality bar; these are at 50% power, x2.7 for 80%). Figures: [optimum by run](figures/mm_tuner_history.svg), [out-of-sample gain by run](figures/mm_tuner_oos_gain.svg).
Validation: the rebuilt 10:33 run's participation calibration is identical to the logged one; fit scores differ by ~1 because
`data/volume_cache.json.vol` (sigma per market) was rewritten at 13:32 BST, so a past run cannot be replayed exactly.

| parameter | live | literature (report) | fit-best over 17 runs | optimum changes / real | best OOS gain (conf) | hours to resolve (90% / materiality) |
|---|---|---|---|---|---|---|
| requote_ticks | 1.0 | 0.5 (0.5-tick reservation move) | 1.5 x5, 3.0 x5, 0.5 x3 | 8 / 0 | +0.3 | 36 / 155 |
| vol_gate_fade_mult | 0.5 | 0.5 (regime: sizes x0.5) | 1.0 x9, 0.25 x7 | 2 / 2 (worth < 0.005/h) | +1.1 | 35 / 415 |
| meanrev_max_ticks | 2.0 | 1.0 (clip at +-h) | 0.5 x7, 2.0 x6, 0 x2 | 8 / 1 | +8.8 (0.91) | never at current sd (3,760) / 239 |
| ladder shape | [1,2] x [1,.5] | [1,2,3,5,8] x [1,1,.75,.5,.25] | old [1,2,3,5] x9, live x3 | 8 / 0 | +10.1 (0.69) | 13 / 21 |
| base_qty | 300 | none (cap / sum(mult)) | 100 in every run | 0 | +6.7 | 567 / 79 |
| improve threshold | 3 | 4 (join while spread <= 2h+1) | 2 in every run | 0 | +0.6 | 69 / 63 |

* Gaps to the literature are noise as well: literature-minus-live fit-score differences per hour have 90% CIs spanning 0 for
  requote [-0.03, +0.08], meanrev [-0.14, +0.24] and ladder [-0.52, +0.10]; the improve-4 gap is a real but worthless +0.003/h.
* base_qty always scores best at 100 and improve at 2 for structural reasons, not evidence: MM P&L is ~0 so the capital charge makes
  the smallest size win; fit windows are mostly pre-11:40 data, when improving on 2 was live. Out of sample, improving on 2-tick
  spreads loses (-12 to -18 per 6 h at 30-min marks, hour CI below 0), confirming the 12:40 BST F2 fix.
* Fill-model calibration drifts: replayed vs actual fit-window fills 2,851 vs 4,138 (04:00 run, -31%) and 15,677 vs 12,119
  (18:42 run, +29%); it matched only around 10:00-15:00. Results that depend on fill counts carry that error.
* **Tuner bugs found (for the main session).** (a) `mm_ladder` will crash at its next run (23 Oct 10:36 UTC): its 4-level
  `level_ticks` grid with the live 2-element `level_size_mult` raises IndexError in `simulate_mm` (reproduced); the group catches
  it and logs an error, so the ladder is silently untuned. (b) `HARD_BOUNDS` require 4-element ladders, so no 2-level ladder can
  ever be recommended. (c) `MM_IMPROVE_SPREAD_GE = 2` is hard-coded, while live is 3: since 11:40 UTC the tuner replays a ladder
  that improves 2-tick spreads, i.e. not the live rule. (d) The MM score marks P&L at the window end, dominated by inventory drift
  in 1-2 markets; a 30-minute markout misses the 3-6 h convergence above.
* **Run more often?** One mm_quote run costs 3.6-7.7 s CPU (median 4.5 s; 0.9 s to load the data) on this laptop, all 6
  parameters 22 s. No model tokens. Hourly runs re-test 93-98% of the same window (each adds 1 h to 15-20 h), so the OOS gains
  of consecutive runs are autocorrelated (lag-1 0.27-0.68 for 4 of 6 parameters) and two hourly "confirmations" are almost one
  test. More runs add no information; more hours of fills do. Keep 12 h, and require confirmations on disjoint test windows.

## 2. Literature and practitioner evidence (prediction markets) -> testable rules
| source | finding | rule tested here |
|---|---|---|
| Ng-Peng-Tao-Zhou (SSRN 5331995, abstract); Aktug-Torul (pridis.pdf): Polymarket leads Kalshi/PredictIt at 5 min; bot_exploits.md: SIG follows a 1.5-pt outside move 0% at 2 min, 53% at 60 min | cross-venue lead | outside fair as reservation price / gate / lead pull |
| Bartlett-O'Hara 2026 (NBER, Kalshi 41.6M trades): price impact almost entirely permanent; makers profit only by holding to settlement | adverse selection is slow and permanent | mark fills at 3-6 h and at the outside fair, not 30 min |
| Le 2026 (arXiv 2602.19520): political prices compressed toward 50%, slope 1.31 (Polymarket) to 1.64 (Kalshi) at long horizons | outside fair itself under-states favourites | the gate is conservative |
| Moallemi-Yuan queue value; Albers et al. 2025 (arXiv 2502.18625): back-of-queue fills are the toxic ones, a big queue ahead protects | do not chase thin queues | "quote only where the DMM queue can be beaten" |
| Cartea-Jaimungal-Ricci 2014; Pulido-Rosenbaum-Sfendourakis (arXiv 2307.15599): with the tick binding, alpha acts through size | size skew | meanrev as size skew |
| Feil-Nendel (arXiv 2607.17991), Xi-Moallemi-Pai-Wang (arXiv 2607.08199): variance flat beyond 1 day, explodes in the last hours; skew ~ p(1-p) | resolution-aware limits | not testable before 3 Nov (stop_quoting_at 3 Nov 12:00 UTC already covers it) |
| Bürgi-Deng-Whelan (Kalshi): makers -9.6% vs takers -31.5%, worst below 10c | tail filter | price band 0.20-0.80 / 0.25-0.75 |
| So-Wang 2014 (JFE), Anand-Venkataraman 2016: makers who stay through announcements are paid a premium, a DMM absorbs | scheduled-event withdrawal | not testable (no scheduled event in the logs) |
| Polymarket rewards S = ((v-s)/v)^2 b, two-sided below 10c; Kalshi LIP excludes SIG; warproxxx/poly-maker: EVENT state pulls quotes, REDUCE_ONLY near end | venue mechanics, no P&L evidence | n/a (this venue pays no rewards) |
No published MM post-mortem with P&L per share was found on either venue. Full source notes (URLs, FULL/abstract tags): side-session
notes folder, `mm-upgrade-literature-academic.md` and `mm-upgrade-literature-practitioner.md`.

## 3. Tests (out of sample, our own logs)
**A. Realised fills, no fill model** (`settle`, `converge`, `robust`): remove the fills a rule would have prevented, re-sum P&L.
| rule (state at fill) | dropped share of MM volume | P&L removed, 30-min mark | 3-h / 6-h SIG mark | fit / test halves | market-clustered 90% CI | verdict |
|---|---|---|---|---|---|---|
| outside-fair gate, gap >= 3 ticks | 52% | +27 | +0.71 / +0.91 tick per dropped share | +0.50 / +1.68 tick (3 h; test = 6 fills) | 3 h two-way [-1.12,-0.29], ex-1057/1033 [-1.04,-0.32]; 6 h [-1.92,+0.10] | **clears at 3 h**; venue-wide premise t 5-7 |
| pull side vs outside fair >= 2 ticks (30-min mark only) | 56% | +31 | - | +3.7 / +27.5 | [-2.7, +74.7] | noise (2 markets) |
| drop meanrev-aided fills | 42% | +22 | - | +1.6 / +20.2 | [-5.4, +60.5] | noise |
| drop L1 at a thin touch (< 1,500) | 55% | +30 | - | -1.5 / +31.4 | [-4.1, +75.1] | noise: thick-thin +0.64 tick overall but +0.06 ex-1057/1033 |
| pause L1 on one-sided prior flow (>= 80%, window ends 30 s before) | 39% | +16 | 5-min diff -0.32 (fit) / -1.41 (test) | 30-min diff -0.34 / -0.71 | ex-1057/1033: 5-min diff -0.63 tick, two-way t -3.8 | **clears at 5 min**, borderline at 30 |
| price band 0.25-0.75 | 57% | +12 | - | -2.2 / +14.0 | [-15.4, +51.1] | no |
Public tape, maker side (17k prints, 205 markets; hour, market and two-way clustered CIs agree): +0.51 tick at 30 min including
the concession, but the mid moves -0.71 against the passive side; the 2-tick vs 1-tick gap (+1.22 vs +0.29) is just the extra
half-spread. Outside fair moved >= 2 ticks against the maker in the prior 10 min while SIG lagged: -0.72 (two-way [-1.18,-0.26]).
Our own fills: -0.24 at 30 min = +0.86 concession - 1.10 mid move (we pay ~0.4 tick more adverse move than the average print).

**B. Replay with the calibrated fill model** (`candidates`; fit-best per family, test = newest 30%; purged marks; both fill models
agree; split 50/50 as a second check). Gains per 6-h test window (live baseline: -6.4 at 30 min, -11.0 at 3 h); material bar 12.7 (21.1 on 50/50). [Figure](figures/mm_candidates_oos.svg).
| family (fit pick) | 30-min mark: gain, 8-block conf | 3-h mark gain (70/30, 50/50) | volume | verdict |
|---|---|---|---|---|
| outside fair as reservation (gap x1.0, cap 4) | +10.2, 0.53 | +32.4; 50/50 picks x0.5 cap 2: +35.5 | -33% / -19% | consistent with A; block test impossible at 3 h |
| pull L1 vs outside fair >= 1 tick | +9.9, 0.98 (hour CI spans 0) | +19.5, +23.0 | -51% / -57% | as above |
| outside move lead pull (2-3 ticks / 5-10 min) | 0 (never triggers on 2-min polls) | 0 | 0 | untestable at this poll rate |
| no quotes on 1-tick spreads | +10.8, 0.08; score conf 0.97 on 50/50 only | +18.4, +21.1 | -69% / -78% | inconsistent, kills volume |
| meanrev as size skew (k 1) | -1.0, 0.92; not picked on 50/50 | +7.2, +8.4 | -2% | below the bar |
| improve on 2+ / on 4+ | -7 to -13 / about 0 | about 0 | +20% / 0 | keep 3 |
| price band 0.20-0.80 | worse on fit | - | -55% | no |
Independent check (Fable): the replay can neither confirm nor reject a rule at this size. (i) The 8-block confidence does not test
the reported gain: blocks re-simulate from flat inventory, and block sums differ from the whole-window gain by > 2 in 23 of 28
tests (the same construction is in `shadow.bootstrap_confidence`); a 10-minute longer window moved one confidence from 0.10 to
0.98. (ii) 28 tests are ~14 effective; 1.9 false passes expected, and best-of-25 selection alone yields ~+18 on a 6-h window,
which is the size of the top gains. (iii) The materiality floor (12.7 per 6 h = 0.33 tick per filled share) is unreachable. (iv)
The 50/50 split's test window contains the 70/30 one. Power: an effect of +0.2 tick on every fill needs 50-120 h of fills; a rule
that touches 40-60% of volume needs 300-560 h, i.e. more than the 281 h left before stop_quoting_at.

## 4. What the main session should wire (pause procedure)
1. **Outside-fair gate in `MarketMaker.build_quotes`** (`sigbot/proposals/mm_upgrade.py: xv_gate`, ready to call): with `xv = venue_fair` for the leg (age <= 10 min,
   Polymarket/Kalshi within 8 points as the directional sleeve requires), `gap = xv - mid`; if `gap >= 3 ticks` set `base_ask = 0`
   on all levels, if `gap <= -3 ticks` set `base_bid = 0`; keep the other side. Config `mm.xv_gate_ticks: 3`. Expected: -52% of MM
   shares (about -15k/day), +0.6 tick (3 h) to +1.2 tick (6 h) on the dropped flow at the venue-wide convergence rate, i.e.
   **about +1.6 to +3.1 per 1,000 MM shares and +45 to +90/day** at SIG marks; **about +28 per 1,000 and +800/day if the outside fair is right at settlement**. Re-test after 48 h of fills with
   `converge`. Same bet as the directional sleeve (favourite_drift.md): the MM stops taking the other side of it.
   Caveats (Fable): one day (4-5 six-hour blocks); about half the 3-6 h move on our fills is the day's venue-wide decompression
   (which is the convergence the rule bets on); at 30 min and 6 h the fill-level effect is still mostly 1057/1033; 92 gated fills
   are 29 independent 3-h episodes; the walk-forward test half is too short to confirm (6 fills with 3-h marks). The settlement
   valuation (+28 per 1,000) is an identity once the outside fair is assumed right, not evidence.
2. **One-sided-flow pause** (small, real): in `paused_sides`, also pause level 1 on a side when >= 80% of that market's public
   volume in [now-630 s, now-30 s] traded into it (taker sells for our bid). Removes ~39% of MM shares that averaged -0.6 tick at
   5 min and -0.4 at 30 min relative to the rest: **about +0.7 per 1,000 MM shares, +19/day** at 30-min marks. It overlaps the
   existing 3-fills-in-60-s pause; wire it after the gate and re-measure, since both hit the same sweeps.
3. **Tuner fixes**: ladder grid and bounds sized to `cfg.mm.levels` (2-level shapes) and `simulate_mm` guarded with
   `n = min(len(level_ticks), len(level_size_mult))`; read the improve threshold from `cfg.mm.improve_when_spread_ticks_ge`; add a
   3-h-mark MM score (marks capped at the window end, as in `simulate_x`); replace the flat-start 8-block confidence with a
   market/hour-clustered bootstrap of the whole-window gain; scale materiality per filled share; confirmations on disjoint test
   windows; keep 12 h.
4. Keep requote 1.0, fade 0.5, meanrev cap 2, ladder [1,2] x [1,.5], base 300, improve 3: no evidence supports moving any of them.
   Do not add a DMM-queue filter, a price band or a size-skew meanrev.

## Sources
reports/Market maker quote sizing by depth.md; research_notes/bot_exploits.md, favourite_drift.md, volatile_moves.md, hf_mean_reversion.md.
Ng et al. https://papers.ssrn.com/abstract=5331995 · Aktug-Torul https://web.bogazici.edu.tr/torul/pridis.pdf ·
Bartlett-O'Hara https://conference.nber.org/conf_papers/f243677.pdf · Le https://arxiv.org/abs/2602.19520 ·
Albers et al. https://arxiv.org/abs/2502.18625 · Moallemi-Yuan https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf ·
Pulido-Rosenbaum-Sfendourakis https://arxiv.org/abs/2307.15599 · Feil-Nendel https://arxiv.org/abs/2607.17991 · Xi-Moallemi-Pai-Wang https://arxiv.org/abs/2607.08199 ·
Bürgi-Deng-Whelan https://www.karlwhelan.com/Papers/Kalshi.pdf · Tsang-Yang https://arxiv.org/abs/2603.03152 ·
Polymarket rewards https://docs.polymarket.com/market-makers/liquidity-rewards · Kalshi LIP https://help.kalshi.com/incentive-programs/liquidity-incentive-program ·
poly-maker https://github.com/warproxxx/poly-maker
