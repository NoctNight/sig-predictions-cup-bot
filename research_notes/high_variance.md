# High-variance plan: sell the book and swing in the most volatile markets (25 Oct 2026)

Sizhe's idea: measure the volatility of each market, sell the current holdings, and put most or all of the capital
into the biggest swings, but only where a statistically significant signal gives the direction.

Rebuild everything (about 1 min, local data only, DB opened read-only, no SIG API calls):

    <venv with numpy+pandas+matplotlib>/bin/python research_notes/high_variance_study.py [dir]

Data: `venue_quotes` (SIG bid/ask and Polymarket/Kalshi mids, 21 Oct 23:49 to 25 Oct 22:30 UTC, ~81 s polls, put on a
5-min grid), `tape` (aggressor-classified prints), `data/books_latest.json` (depth now) and `params/wind_down_report.json`
(walked exit cost per holding). Figure: `figures/high_variance_decision.png`. One tick = 0.005, one point = 0.01.

## Verdict

**Do not do it.** Three reasons, each enough alone:

1. **No direction signal passes.** Of 8 signals × 4 horizons, tested in the high-vol subset, none earns more than the spread
   on the test days with t > 2. The best one, following the outside−SIG gap for 48 h, makes +1.8 ticks per share before the
   spread (t 2.9) but only +0.6 ticks after it (t 1.0). It is one of 64 tests, and it is the same raw gap that
   `sig_convergence.md` dropped on 24 Oct.
2. **Volatility to 3 Nov cannot reach the prize.** The 15 most volatile legs move a typical 4-12 points by 3 Nov. With all
   ~87k of cash in the 3 most volatile legs, the 95th percentile is 128k and the 99.9th is 155k. A top-3 finish needs about
   900k, which is 8.7 times today's balance. Only settlement (the 3 Nov punt) offers multiples like that, and the punt is
   already planned.
3. **The 5,000 partisan cap blocks the concentrated version.** The all-in top-3 plan is net 58k short D (91% blocked). The
   15-leg plan is net 30k (6× the cap). Only a book balanced between D and R fits under the cap, and balancing it removes
   the shared national swing, which is most of the variance Sizhe wants.

Every version has a lower average than keeping the book (-1.8k to -5.2k). Every version also has P(gain) of 14-39%,
because the round-trip spread is certain while the moves average out to zero.

## 1. Volatility per market (22-25 Oct)

Realised volatility of the SIG mid is the root-mean-square change over each horizon. The median leg moves 0.29 / 0.68 /
1.19 points per 1 h / 6 h / 24 h. The expected move to 3 Nov 18:00 ET (9.0 days) is the 24 h sigma (or 2× the 6 h sigma,
whichever is larger) × √9, capped at the binary bound √(p(1−p)), × √(2/π). The rank is that expected move, less the
spread, × the shares available within 3 ticks now (capped at 87k of capital).

| # | leg | mid | σ 1 h | σ 6 h | σ 24 h | outside σ 24 h | spread | depth ≤3 ticks (sh) | E\|move\| to 3 Nov (pts) | swing per $ | expected swing |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Rhode Island Senate R | 0.10 | 0.34 | 0.77 | 1.45 | 0.14 | 0.5 | 476k | 3.7 | 0.32 | 15.2k |
| 2 | Rhode Island Governor I | 0.07 | 0.35 | 0.90 | 1.43 | 0.98 | 0.5 | 217k | 4.3 | 0.52 | 8.2k |
| 3 | U.S. Senate R | 0.36 | 0.40 | 0.93 | 1.79 | 1.23 | 0.5 | 398k | 4.4 | 0.11 | 7.7k |
| 4 | MI-07 D | 0.71 | 0.56 | 2.37 | 3.61 | 3.25 | 0.5 | 60k | 11.4 | 0.15 | 6.6k |
| 5 | MT-01 R | 0.54 | 0.78 | 2.05 | 3.00 | 2.41 | 0.5 | 56k | 9.8 | 0.17 | 5.2k |
| 6 | FL-22 D | 0.62 | 0.24 | 0.67 | 2.05 | 2.72 | 0.5 | 103k | 4.9 | 0.07 | 4.5k |
| 7 | Alaska Governor D | 0.82 | 0.55 | 1.74 | 4.97 | 18.5* | 0.5 | 40k | 11.9 | 0.14 | 4.5k |
| 8 | PA-08 R | 0.24 | 0.42 | 1.33 | 2.44 | 2.51 | 0.5 | 75k | 6.4 | 0.25 | 4.4k |
| 9 | AZ-01 D | 0.82 | 0.43 | 1.10 | 2.53 | 2.85 | 1.0 | 134k | 6.1 | 0.06 | 4.3k |
| 10 | Texas Senate D | 0.61 | 0.57 | 1.54 | 2.07 | 1.81 | 0.5 | 62k | 7.4 | 0.11 | 4.3k |
| 11 | MT-01 D | 0.46 | 0.86 | 2.10 | 2.25 | 2.22 | 0.5 | 44k | 10.1 | 0.21 | 4.2k |
| 12 | Tennessee Governor R | 0.92 | 0.36 | 0.97 | 2.49 | 0.45 | 0.5 | 77k | 6.0 | 0.06 | 4.2k |
| 13 | WI-01 D | 0.48 | 0.33 | 0.93 | 2.32 | 1.10 | 0.5 | 79k | 5.6 | 0.11 | 4.0k |
| 14 | PA-10 D | 0.80 | 0.48 | 1.17 | 1.39 | 1.84 | 1.0 | 258k | 5.6 | 0.06 | 4.0k |
| 15 | MI-10 R | 0.32 | 0.44 | 1.18 | 1.82 | 1.09 | 0.5 | 75k | 5.7 | 0.16 | 3.9k |

σ, outside σ, spread and E|move| are in points. *Alaska's outside series has a bad print or contract switch; its SIG σ is
genuine. The top 3 rank high on **depth**, not volatility. The most volatile legs per dollar (MI-07, MT-01, Alaska Gov,
VA-05) have 40-60k shares within 3 ticks, so ~25-40k of capital each is the most they can take before impact.
Caveats: 24 h σ rests on about 4 independent days; volatility usually rises into election day, so these figures
understate the last 48 h. That raises the variance, not the average.

## 2. Direction: is there any signal in the volatile markets?

**Already tried, not redone** (BACKLOG Done list and notes): impact-sized directional trades (`directional_impact.md`,
OOS -0.48 t -3.7); fading or following SIG-only moves (`ev_hunt.md` 1a/1b, `volatile_moves.md`); follow-the-move long-vol
straddles (`long_vol.md`, pooled test t 1.2); raw and deviation gap convergence (`sig_convergence.md`, dropped by Sizhe on 24 Oct);
taker-flow types (`flow_patterns.md`); favourite drift and Polymarket/Kalshi recalibration (`favourite_drift.md`, `ev_hunt.md` part 3:
a settlement-horizon effect, not a 6-48 h price signal).

**New here:** the same families, restricted to the high-vol subset, scored on the direction of the SIG mid 6-48 h ahead.
One anchor per leg every 6 h. The rule (follow or fade) was picked on the earlier half of anchors and scored on the later
half. t is clustered by race. "Net" subtracts the spread at entry (half in, half out).

Top third of legs by 24 h σ (79 legs, 51 races):

| signal | rule | 6 h test gross / net (t net) | 12 h | 24 h | 48 h |
|---|---|---|---|---|---|
| gap: outside − SIG | follow | +0.25 / −0.93 (−8.5) | +0.43 / −0.73 (−3.9) | +0.97 / −0.16 (−0.5) | **+1.77 / +0.60 (t 1.0)**; gross t 2.9 |
| outside momentum 6 h | follow | +0.21 / −0.96 | 0.00 / −1.16 | −0.10 / −1.25 | 0.00 / −1.20 |
| outside momentum 24 h | follow | +0.29 / −0.90; gross t 3.8 | +0.28 / −0.91 | +0.24 / −0.90 | +0.14 / −1.03 |
| SIG momentum 6 h | follow | 0.00 / −1.23 | +0.24 / −0.97 | +0.26 / −0.91 | +0.21 / −1.00 |
| SIG momentum 24 h | follow/fade | 0.03 / −1.18 | −0.06 / −1.25 | +0.40 / −0.74 (fade) | −0.53 / −1.69 |
| favourite (mid − 0.5) | follow | +0.01 / −1.17 | −0.02 / −1.18 | +0.11 / −1.03 | +0.04 / −1.14 |
| calibrated outside − SIG | follow | −0.05 / −1.22 | −0.04 / −1.20 | +0.07 / −1.07 | +0.03 / −1.15 |
| order flow 6 h | follow | −0.11 / −1.31 | +0.06 / −1.12 | +0.02 / −1.10 | +0.14 / −1.05 |

The top-15 subset (14 races) says the same thing: the best net figure is the gap at 24-48 h (−0.2, t −0.3). Outside
momentum does **not** reverse on SIG. At 24 h it continues weakly (+0.3 ticks gross, t 3.8 at 6 h), which is SIG catching
up with the outside price, but that is a quarter of the spread. Favourite/longshot calibration has zero power over 6-48 h:
it pays at settlement, not on the way there. The t statistics do not allow for national swing moving all races on the same
day, so they are, if anything, too generous.

## 3. EV of the plan, to 3 Nov 18:00 ET

Monte Carlo, 20,000 runs. Each leg moves by its σ to 3 Nov; 43% of each race's variance is a shared national swing
(`sig-correlation-model`: 37-49%). Equal capital per leg, capped by depth within 3 ticks. Sold at 3 Nov 18:00 ET
(cash objective). The value of the book today is 103.8k at settlement.

Selling the book (walked, `wind_down_report.json` 25 Oct 18:23 ET):

| holding | value | sells for now | exit cost |
|---|---|---|---|
| cash | 17,622 | 17,622 | 0 |
| sets (16; the wind-down already sells these by 3 Nov) | 52,261 payout | 50,205 | 2,056 |
| other legs and MM legs (46) | — | 2,534 | ~25 (1 tick) |
| exempt: cheap-favourite and HF-arb legs (24) | 16,678 at mid | 16,606 | 73, plus the cheap-favourite settlement edge given up |
| **total** | | **86,967** | **2,154** |

About 2.1k of the exit cost is already in the wind-down plan, so selling early adds almost nothing. What it adds is
giving up the cheap-favourite edge, which pays at settlement.

| plan | average vs keep | P(gain) | 5th pct | median | 95th pct | 99.9th pct | capital deployed | net partisan | cap blocks |
|---|---|---|---|---|---|---|---|---|---|
| Keep the book (planned punt runs on the same cash) | 0 | 50% | 103.4k | 103.8k | 104.2k | 104.5k | — | within cap | — |
| Sell all; 15 volatile legs; cheaper side, no signal | **−4.6k** | 33% | 82.1k | 99.3k | 116.3k | 132.3k | 81.7k | −29.8k (R) | 6× over |
| Sell all; 15 legs; gap-signal side, test edge assumed | −1.8k | 28% | 96.9k | 102.0k | 107.1k | 111.5k | 78.6k | +25.1k (D) | 5× over |
| Same, shrunk to fit the 5,000 cap | −2.0k | 14% | 98.7k | 101.8k | 104.9k | 107.7k | 58.5k | +5.0k | 41% of the D side cut |
| Sell all; top 3 legs only; cheaper side | **−5.2k** | 39% | 68.7k | 98.7k | 128.4k | 154.8k | 73.7k | −58.0k (R) | 91% |

The trading cost alone (leaving out the 2.15k exit cost) is −2.4k for 15 legs, −3.0k for the top 3, and +0.4k for the gap
version. That +0.4k needs the gross test edge, which has net t 1.0, to hold over a 9-day hold.

**Against the punt.** The punt runs on whatever cash exists on 3 Nov evening, so the plan's only effect on it is the size
of that cash. A 1.5M target needs 14.4× from 104k. From the plan's median of 99k it needs 15.2×. Even the top-3 plan's
95th percentile (128k) still needs 11.7×. So the plan lowers the punt's odds in the median case, and does little for them
in the lucky case. The punt's odds come from settlement multiples (`punt_strategy.md`: about 2.4% for 1st). Pre-election
swings cannot supply those.

## 4. Partisan cap (hard limit, only Sizhe can lift it)

`portfolio_max_partisan_notional` = 5,000 net D−R. Any concentrated swing is one-sided by construction: the national
factor is what makes the top legs move together. The cap blocks **91%** of the top-3 plan. The 15-leg plans run 5-6× over it. Fitting the gap-signal
version under the cap means cutting its D side by 41%, which leaves 20k of the cash idle. A D/R-balanced book fits, but it cancels the national swing and leaves only the
smaller race-specific variance. Lifting the cap does not change the verdict: without a direction signal, more size means
more spread paid and a wider band around a lower average.

## 5. What would change the answer

* A direction signal with net-of-spread OOS t > 2. Nothing in sections 2 or "Already tried" qualifies. The gap at 48 h is
  the only candidate. Testing it properly needs ~40 more test anchors per leg, which means days that are no longer available.
* A maker entry instead of a taker one, which halves the spread: the gap at 48 h would then be about +1.2 ticks. That is
  still about 1k on 80k, and a fill is not guaranteed.
* An election-night variance source. That is the existing punt plus election-night capacity (`election_night.md`), and it
  already has its own approval path.
