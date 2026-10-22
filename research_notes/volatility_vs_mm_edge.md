# Does volatility predict venue volume, our MM fills and our markouts?
Generated 2026-10-22 10:37 UTC by `python3 -m sigbot.tracker --research --hours 36`. Tick = 0.005. Vol = realised vol of
the mid (quoted markets: quote-snapshot mids, 49 s apart) or of the last trade price (every other market; bid-ask bounce
inflates it), 5-minute grid, 1 h changes, averaged over the window; nvol divides each change by sqrt(p(1-p)). Markouts:
(mid 30 min after the fill - fill price) signed in our favour, ticks (same as analytics.py). z = rho x sqrt(n-1).
Samples differ per test because `quotes` only exists for markets the bot quoted: **tape sample** = 156 exchanges with
>= 3 prints in the last 36 h (of 205 seen; the REST tape poll covers quoted + arb + a 4-market rotation, the feed covers
all); **quote sample** = 30 exchanges quoted >= 1 h (quotes and MM fills only span the last ~12 h, 2026-10-21 22:42 to
2026-10-22 10:27 UTC); **fill sample** = 146 of 157 MM fills whose 30 m markout has resolved.

| test | sample | x | y | Spearman |
|---|---|---|---|---|
| (a) vol -> venue volume | tape, n=156 | tape nvol 1h | volume/h | rho=+0.38 (z=+4.8) |
| (a) vol -> venue trade count | tape, n=156 | tape nvol 1h | trades/h | rho=+0.28 (z=+3.5) |
| (a) vol -> venue volume | quoted, n=30 | mid nvol 1h | volume/h | rho=+0.64 (z=+3.5) |
| (b) vol -> our MM fills | quoted, n=30 | mid nvol 1h | MM fills / quoted h | rho=+0.53 (z=+2.8) |
| (b) vol -> our MM fills | quoted, n=30 | mid vol 5m (ticks) | MM fills / quoted h | rho=+0.66 (z=+3.5) |
| (c) vol -> our 30m markout | exchanges with >= 3 fills, n=18 | mid nvol 1h | mean markout 30m | rho=-0.18 (z=-0.7) |
| (c) vol -> our 30m markout | fills, n=146 | EW vol 5m at fill time | fill markout 30m | rho=+0.18 (z=+2.1) |
| (c) vol -> our 5m markout | fills, n=146 | EW vol 5m at fill time | fill markout 5m | rho=+0.15 (z=+1.8) |

Terciles by tape nvol 1h (tape sample, 36 h):
| tercile | n | nvol 1h | volume/h | trades/h | avg print | spread (quoted only) |
|---|---|---|---|---|---|---|
| low | 52 | 0.32 | 475 | 1.2 | 361 | 1.0 |
| mid | 52 | 2.33 | 3116 | 6.2 | 423 | 1.1 |
| high | 52 | 5.68 | 1151 | 1.9 | 604 | 1.0 |

Terciles by mid nvol 1h (quote sample, ~12 h):
| tercile | n | nvol 1h | vol 5m | volume/h | spread | sweeps/h | MM fills | fills / quoted h | markout 5m (n) | markout 30m (n) | half-spread |
|---|---|---|---|---|---|---|---|---|---|---|---|
| low | 10 | 0.43 | 0.07 | 3269 | 1.0 | 0.00 | 11 | 0.21 | -0.05 (11) | -0.05 (11) | 0.36 |
| mid | 10 | 1.26 | 0.18 | 3519 | 1.1 | 0.00 | 50 | 0.51 | +0.26 (50) | +0.23 (50) | 0.57 |
| high | 10 | 2.38 | 0.30 | 8901 | 1.1 | 0.03 | 94 | 0.99 | +0.21 (94) | +0.10 (94) | 0.75 |

Fill-level terciles by EW vol 5m at fill time (fill sample):
| tercile | n fills | vol 5m at fill | half-spread captured | markout 5m | markout 30m | adverse 30m |
|---|---|---|---|---|---|---|
| low | 48 | 0.07 | 0.83 | +0.10 | -0.01 | 0.84 |
| mid | 49 | 0.21 | 0.50 | +0.24 | +0.18 | 0.32 |
| high | 49 | 0.47 | 0.47 | +0.34 | +0.28 | 0.19 |

Findings. (a) Vol predicts activity but not monotonically: across the 156 traded markets rho = +0.38 with volume, yet
the top tercile is quieter than the middle one because its nvol comes from 1-2 tick prints in 0.03-0.05 markets (tiny
sqrt(p(1-p))); on the 30 quoted markets, where mids are clean, rho = +0.64. (b) Our fill rate rises strongly with vol
(rho +0.53 to +0.66): the high tercile fills ~5x the low one per quoted hour at the same 1-tick spread. (c) Markouts do
not worsen with vol: fill-level rho = +0.18 (z 2.1); fills in the quietest markets are the only ones with a negative
30 m markout (-0.01) despite the widest half-spread (0.83 ticks), because the mid moves 0.84 ticks against them (a
resting order in a dead book only fills when a sweep goes through it). The 18-exchange test is flat (rho -0.18, n too
small). Net: here the extra fills in volatile markets are uninformed two-sided flow, so edge/hour = fill rate x markout
grows with vol; the tracker's prior scales fill rate with venue trade rate and does *not* raise the adverse prior with
vol. Caveats: ~12 h of fills from one night/morning, 157 fills on 30 markets, a single DMM resting on every book.

Literature. Avellaneda-Stoikov (2008) set the optimal half-spread as gamma sigma^2 (T-t) plus a flow term: higher
volatility means a wider quote and a larger inventory penalty, not necessarily lower profit. Glosten-Milgrom (1985)
and Kyle (1985) distinguish the source: volatility from informed trading widens the spread and raises adverse
selection, while volatility from uninformed (noise) flow is where makers earn the spread. Empirically, realised spread
(the maker's captured edge net of post-trade drift) falls as the information share of volatility rises (Huang-Stoll
1996; Hendershott-Jones-Menkveld 2011), so a positive vol-markout relation, as here, signals noise-driven volatility.
