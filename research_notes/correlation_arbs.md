# Cross-race correlation model and correlation arbs (2026-10-22, read-only side session)

Code: `sigbot/history_build.py` (dataset), `sigbot/correlation_model.py` (`--fit`, `--report`, `--matrix`), `sigbot/proposals/correlation_arb.py`
(`--backtest`, `--paper`, `cycle(prices, positions)`). Params: `params/correlation_model.json`, `params/correlation_report.json`,
`params/correlation_arb_backtest.json`, `data/correlation_matrix.csv` (6,555 pairs). No orders placed; SIG prices come from the bot's own
`venue_quotes`/`quotes` log (read-only), zero REST reads.

## 1. Historical dataset (`data/history/races.csv`, 5,474 rows)
| office | source | years | contested rows 2006-24 | features |
|---|---|---|---|---|
| Senate | MIT Election Data + Science Lab, "U.S. Senate statewide 1976-2024" (doi:10.7910/DVN/PEJ5QU), `data/history/raw/` | 1976-2024 | 331 | incumbency by name match to the seat's previous winner; Jan runoffs folded into the November race |
| Governor | Wikipedia per-year "YYYY United States gubernatorial elections" race-summary tables (MediaWiki API), `governor_results.csv` | 2005-2025 | 236 | incumbent party / running / open from the table; 5 specials/recalls flagged |
| House | Wikipedia per-year "YYYY United States House of Representatives elections" state tables, `house_results.csv` (4,350 rows, seat totals verified per year) | 2006-2024 | 3,876 | incumbent from the Result column; MEDSL House file is guestbook-gated on Dataverse |
| President | MEDSL "U.S. President 1976-2024" (doi:10.7910/DVN/42MVDX) | 1976-2024 | - | state lean = two-party margin minus national, mean of the two previous presidential elections (`state_lean.csv`) |
| polls | FiveThirtyEight `raw_polls.csv` (`data/fair/`), final-21-day average per race; generic ballot `House-G-US` rows | 1998-2022 | 1,333 polled | 2024 generic ballot: 538 average D+0.8 on 2024-11-04 (Dark Horse Politics citing 538); 2024 House popular vote R+2.56 (Wikipedia) |
National environment per year (`national_environment.csv`): final generic-ballot average, House popular vote, White House party, presidential year.
Region = Census division (9). Margin = D% - R% of the total vote; uncontested and same-party top-two races are dropped.

## 2. Model and fit (2006-2024; factor structure from the 567 contested statewide races, House adds its own loading and widths)
margin_i = E_i + beta_office * u_year + g_division,year + s_state,year + e_i. E_i = poll blend (weight 0.75*sqrt(min(1, n/4))) where polled,
else a per-office leave-one-year-out regression on lean, incumbency, open-seat party, generic ballot and presidential-year flag
(Senate: lean 0.71, incumbent +11.2, generic 0.81, LOYO RMSE 12; Governor: lean 0.33, incumbent +15.7, generic 0.64, RMSE 15; House:
lean 0.34, incumbent +26.4, generic 0.79, RMSE 18). Fundamentals alone are poor, which is why the venue work uses polls and outside markets.
A first attempt that pooled the 3,876 House rows into the year means read House model misfit (redistricting years) as swing (Senate loading
fell to 0.4-0.65); a competitive-only subset was too thin (73 state pairs, components above the total). The statewide fit is the one kept:
| component | value | how |
|---|---|---|
| national swing u | sd 3.7 pts (var 13.8) | race-weighted year mean of statewide residuals net of sampling noise; u by year 2006 +1.6, 2008 +1.4, 2010 0.0, 2012 +6.5, 2014 -3.2, 2016 -4.7, 2018 -0.6, 2020 -8.2, 2022 +0.2, 2024 -5.0 |
| loadings beta | senate 0.91, governor 1.12, house 0.71 | leave-office-out slope of office year-means on the other office's year mean (House: slope of its polled-race year means on u) |
| division shock | sd 1.9 (var 3.7) | division-year means net of noise |
| same-state shock | sd 2.9 (var 8.2; 6.0 on the 165 Senate-Governor pairs alone) | mean product of residuals of different statewide races in one state-year |
| idiosyncratic (>= 3 polls) | senate 2.8 (total 5.6), governor 2.5 (total 5.0), house 6.3 (total 7.7) | leftover; floor 2.5 |
Variance shares of a well-polled race: Senate national 37% / division 12% / state 26% / idiosyncratic 25%; Governor 49 / 10 / 23 / 18;
House 12 / 6 / 14 / 68. The national factor correlates 0.54 with the generic-ballot polling error (slope 0.8): about half of the swing is the
generic ballot missing, the rest a common race-poll miss. Generic-ballot error 2006-2024: mean -2.0 (polls overstate D), sd 2.8.

## 3. Implied correlations between the venue's legs (D legs; R legs are the complement)
Latent (margin) correlation rho = cov / (sigma_a sigma_b) with sigma from the fit (competitive races use the >= 3-poll widths, safe seats
the wide bucket); binary correlation via the bivariate normal at the believed fair probabilities (`params/fair_explained.json`, fallback
`fair_model.json`). Full table: `data/correlation_matrix.csv`.
| pair type | pairs | mean rho (latent) | mean binary corr |
|---|---|---|---|
| different division | 5,788 | 0.24 | 0.08 |
| same division, different state | 627 | 0.31 | 0.11 |
| same state (Senate / Governor / House) | 140 | 0.43 | 0.20 |
| both competitive (0.15-0.85), different division | 907 | 0.21 | 0.12 |
| both competitive, same state | 45 | 0.42 | 0.25 |
Examples: Delaware Senate D vs New Jersey Senate D rho 0.13 (both safe: binary 0.03); Delaware vs North Carolina Senate 0.29 (same
division, binary 0.08); Georgia Senate vs North Carolina Senate 0.49 (binary 0.20); Maine vs New Hampshire Senate 0.49 (0.27); same-state
Senate-Governor pairs 0.78 latent: Georgia binary 0.28, Michigan 0.45, Florida 0.50, New Hampshire 0.11 (one leg safe). Binary correlation
between two tails is tiny whatever the latent rho: the venue's mispricings are tail compression, and tails hardly co-move in outcome space.

## 4. Strict arbs (riskless regardless of the model)
* Set arbs inside a race (sum of YES bids > 1, sum of asks < 1 - x): the live arb sleeve's domain; the report lists them from legs quoted
  within 120 s of each other (single-digit, 0.2-2 cents, all already targeted by the 5-s scanner; the first report run showed 74 because
  the quote log prices legs of the same race hours apart, which is not an arb).
* Chamber vs seats (Senate control = D wins >= 17 of the 35 seats up; D holds 34 not up; VP Vance breaks ties; the Ohio special is NOT
  listed on the venue and the bounds treat it conservatively). All four Boole/Frechet portfolios have large negative edge at the touch:
| bound | portfolio | edge/unit |
|---|---|---|
| P(D ctrl) <= sum D_i / 17 | sell U.S. Senate D at bid, buy 1/17 of every D leg at ask | -0.40 |
| P(D ctrl) >= 1 - sum over 17 strongest (1 - D_i) | buy U.S. Senate D + NO on the 17 strongest D seats | -2.07 |
| P(D ctrl) <= sum over any 19 seats D_i | sell U.S. Senate D, buy the 19 cheapest D legs | -2.96 |
| P(R ctrl) <= sum R_i / 19 | sell U.S. Senate R, buy 1/19 of every R leg | -0.61 |
  These bounds hold for any dependence and are loose by construction; with 35 compressed seat prices nothing gets near them. There is no
  logical constraint between two different races (Delaware D and New York D can take any joint value), so every cross-race trade is model-dependent.

## 5. Model-dependent mispricings (current prices, 2026-10-22 ~15:30 UTC)
Chamber markets, factor-model Monte Carlo (10k paths; Senate adds Ohio at 0.42; House = 435 seats: 44 venue fairs, 92 rated seats via
rating margin / sigma 8, 290 safe incumbents at 0.985, 6 by lean, 1 unknown; mid-decade maps as on the Wikipedia 2026 page):
| market | venue D mid | Polymarket | model P(D), fair seat marginals | from the venue's own seat prices | independent seats | sd_u x0.5 / x1.5 |
|---|---|---|---|---|---|---|
| U.S. Senate | 0.660 | 0.666 | 0.58 (expected wins 17.25, p10-p90 12-22) | 0.58 | 0.67 | 0.63 / 0.56 |
| U.S. House | 0.880 | 0.91 | 0.88 (expected D seats 233, p10-p90 216-251) | 0.89 | 0.9996 | 0.97 / 0.81 |
Reading: the House price is exactly what the seats imply under the model. The Senate sits 8 points above the model because the fair seat
probabilities sum to 17.25 wins against a threshold of 17: the control price is then a bet on the width of the distribution, and half a
seat of expected wins (one Georgia-sized disagreement) or the swing-sd band moves it by 5-8 points. Polymarket's 0.666 agrees with the
venue, so this is our model against the world, not a venue error. No chamber trade.

Relative-value pairs (long D leg below fair / short D leg above fair, hedge ratio h = shares of b per share of a that zero the
national-swing exposure, EV per share of a to settlement, residual sd from the model's pairwise binary covariance):
| long D | short D (buy R) | fair / venue long | fair / venue short | h | rho | EV mid | EV touch | resid sd | EV/sd |
|---|---|---|---|---|---|---|---|---|---|
| Rhode Island Senate | Tennessee Governor | .984 / .865 | .047 / .127 | 0.36 | 0.43 | +.147 | +.142 | .145 | 0.97 |
| Rhode Island Senate | Mississippi Senate | .984 / .865 | .117 / .195 | 0.21 | 0.37 | +.135 | +.131 | .140 | 0.93 |
| Rhode Island Senate | Tennessee Senate | .984 / .865 | .041 / .114 | 0.79 | 0.22 | +.176 | +.164 | .198 | 0.83 |
| competitive only: Florida Governor | VA-05 House | .249 / .210 | .323 / .410 | 1.79 | 0.32 | +.194 | +.170 | .866 | 0.20 |
| competitive only: Iowa Senate | VA-05 House | .467 / .443 | .323 / .410 | 1.96 | 0.21 | +.194 | +.176 | .984 | 0.18 |
| same state: New Hampshire Senate | New Hampshire Governor | .869 / .820 | .079 / .100 | 1.25 | 0.78 | +.050 | +.048 | .450 | 0.11 |
| same state: Florida Governor | Florida Senate | .249 / .210 | .136 / .162 | 1.67 | 0.78 | +.083 | +.047 | .516 | 0.09 |
What this says: (i) the EV is the known tail tilt (SIG longshots 7-13 points rich, favourites 5-12 cheap), the same legs the tilt /
explained-direction sleeves already hold; (ii) for safe legs the swing hedge ratio is 0.1-0.4 because a safe seat barely responds to the
swing, so the "pair" is a single-leg tilt position with a small hedge; (iii) among competitive races EV/sd is 0.1-0.2 because the hedge
removes only the national share and the idiosyncratic plus state risk stays; (iv) same-state pairs have the highest correlation (0.78 latent)
but the venue prices them consistently, so their EV is again just the two legs' own tilt.

## 6. Backtest of the gaps (`--backtest`: 4,197 five-minute bars over 14.6 days, SIG candles + Polymarket/Kalshi history + venue_fair)
Gap g = (sig_a - outside_a) - h (sig_b - outside_b). Pooled over the 23 pairs: AR(1) 0.88-0.95 at 1 h, 0.43-0.78 at 6 h, 0.0-0.37 at 24 h
(the level is persistent; the 24-h number is low because the gap's range is 1-2 cents around a 12-cent mean). Slope of the gap change on
the gap level: -0.004 (1 h), -0.013 (6 h), -0.036 (24 h) -> half-life of a deviation about 19 days, i.e. the tilt does not close before Nov 4.
Markouts of entering against the gap when |g| >= 3 cents, marked at the mid: +0.04 pts (1 h, hit 46%), +0.12 (6 h, 55%), +0.25 (24 h, 54%)
per share: a twentieth of a tick a day against a 2-tick spread, and the mark is a mid that cannot be traded at that size. Conclusion: no pre-settlement convergence trade; the sleeve's P&L is realised
at settlement, which no backtest can measure before election night.

## 7. Recommendation and wiring
* Nothing riskless beyond the set arbs the scanner already takes. No chamber trade (House consistent; Senate gap is model-width, not venue error).
* The model's useful product is risk, not alpha: `pair_stats` / `data/correlation_matrix.csv` give the book's joint distribution, and
  `price_chambers` the chamber legs' exposure. The concentrated-risk sizing note used one 0.32 common-factor correlation for every pair;
  this fit says ~0.2 for cross-state legs (0.1 for two safe seats) and 0.4-0.8 for same-state ones, with 26% of a polled Senate race's
  variance in the state factor. Same-state legs (Georgia Senate + Governor, Florida, Michigan) should be sized as one position.
* If the sleeve is wanted anyway: `CorrelationArb().cycle(prices, positions)` returns resting touch orders for up to 6 swing-neutral pairs
  (paper cycle now: RI Senate D 346 @ .865 + TN Governor R 124 @ .87; MA Senate D 326 @ .92 + MS Senate R 45 @ .795; 743 notional) and a
  chamber leg only when the model gap >= 10 points, within 300 per leg / 3,000 total / net-D 5,000; riskless arbs come back as quantity-0
  "flag" rows. Wiring = one object + one call per cycle with the bot's price snapshot, orders tagged purpose 'correlation'.
* Refresh: `python3 -m sigbot.history_build && python3 -m sigbot.correlation_model --fit` (10 s), `--report` (~5 s CPU, 2-3 min wall on a
  loaded machine; no API reads). Ohio Senate D probability is the constant `OHIO_SENATE_D` in `correlation_model.py`.
