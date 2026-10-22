# Volume target for the bot (written 2026-10-22, day 21.5 of 34.7; 13.2 days left)

Goal (Sizhe): as much volume as possible without negative-EV trades, measured by markouts. Tools: `python3 -m sigbot.volume_report`
(2 API reads), `python3 research_notes/volume_target_model.py data/leaderboard_full.json` (no reads), target in `params/volume_target.json`.
Volume on the leaderboard = "bid+ask notional" = quantity x price paid on the side bought (calibrated: leaderboard +18,346 vs our fills +18,723).

## 1a. Winners' volume on this venue (full leaderboard, 1,751 accounts, pulled 11:07 UTC)

| group | n | volume p25 / med / p75 | med vol/day | trades/day | avg trade | P&L med | P&L / vol |
|---|---|---|---|---|---|---|---|
| top 3 | 3 | 20.0 / 23.2 / 25.3M | 1,081k | 786 | 1,375 | 895k | 3.9% |
| top 10 | 10 | 7.6 / 15.4 / 21.7M | 719k | 223 | 2,475 | 564k | 4.9% |
| top 50 | 50 | 1.9 / 5.9 / 12.1M | 277k | 190 | 1,572 | 69k | 2.5% |
| top 1% | 17 | 5.9 / 9.1 / 17.2M | 424k | 221 | 2,018 | 320k | 3.9% |
| top 5% | 87 | 0.8 / 2.7 / 6.9M | 124k | 116 | 981 | 36k | 2.3% |
| all | 1,751 | 0.01 / 0.10 / 0.24M | 5k | 1 | 1,331 | 0 | 0.0% |
| us | 1 | 56.7k (volume rank 1,127) | 2.6k since 1 Oct; 107k/day pace over the last 13 h | 15 | 174 | 316 (rank 646) | 0.5% |

Does volume predict P&L? Spearman(volume, P&L) = 0.11 over all accounts, +0.55 inside the top 50, +0.81 for ranks 201-1000 and **-0.60** for
ranks 1001-1751 (volume tracks |P&L|: active accounts win big or lose big). Logistic fit P(top 10) = s(-35.9 + 4.93 log10 V): 50% odds need
19.3M total (900k/day); P(top 50) hits 50% at 5.0M total (234k/day); at our 56.7k both are ~0. These are correlations: the top 10 earn
4-5% of their volume, which is the signature of large directional positions marked up 6-10x on 100k, not of market making (0.1-0.5% margins).
The top 10 by volume are P&L ranks 3, 7, 31, 12, 2, 6, 1, 36, 9, 32. Sum of all accounts' volume: 40M/day; the top 10 alone 7.0M/day.

## 1b. Polymarket and Kalshi (details and URLs: side-session note `volume-research-sources.md`)

| finding | number | source |
|---|---|---|
| Polymarket all-time top-20 by P&L: 15 of 20 earn >10% of volume (Theo4 $22M on $43M, 14 markets) | directional, few markets | data-api.polymarket.com/v1/leaderboard |
| Top-20 by volume: 5 of 20 net negative, 8 earn <0.1% of volume; swisstony $1.85B vol, +1.0%, ~15-23k trades/day | volume != P&L | Bitquery; polyresearchrobotics.com/reports/swisstony |
| Kalshi highest-volume account szg.szg: $14.5M, 50k trades in 30 d (~1,680/day), P&L rank 70; #1 P&L made 2,415 trades | same | rivo.markets/kalshi-leaderboard/monthly |
| Winners provide liquidity with limit orders, losers take with market orders; top 1% of winners take 76.5% of profits | maker posture | CEPR DP21615 (Akey et al. 2026) |
| Automated traders earned $133M at 49.9% directional accuracy; ~70% of edge is timing/spread capture | execution edge | Della Vedova, SSRN 6191618 |
| Kalshi takers -32%, makers -10% average return; one Knicks market: MMs 58% of volume and lost $22M pre-fee | MM volume is not free | VoxEU "Makers and Takers"; ingame.com |
| Polymarket liquidity rewards pay for resting depth near mid (score ((v-s)/v)^2 x size), not for fills | no SIG analogue | docs.polymarket.com/market-makers/liquidity-rewards |
| Jane Street ETC teams: +-2 tick quoting earned $3-12k/round, +-1 tick lost money | width beats share | github.com/manoflearning/jane-street-etc |
| Kalshi Basic API tier: 10 writes/second | our 30/min is 1/20th | docs.kalshi.com/getting_started/rate_limits |

No published winner of any play-money contest reports trade counts; this Cup is the first season.

## 2. Target and where we stand

| quantity | per day | derivation |
|---|---|---|
| benchmark: top-10 median pace | 719k | 15.4M / 21.5 d; reaching the top-10 median total by 4 Nov would need 1.9M/day: directional size only |
| stretch: top-50 median pace | 277k | 5.94M / 21.5 d; ~ the 50% point of the top-50 logistic (234k/day) |
| **target: position-neutral capacity** | **100k** | measured, see round 1: MM 7k -> ~25k/day at 4x level-1 size while markouts hold; arb turnover 30-40k (recycle, unwind at >= cost); tilt 7-14k; direction 5k. Just under the top-5% median pace (124k) |
| ceiling | 150k | MM at 8x size with every full fill scaling (~50k) + the 70k arb sleeve turned once a day + tilt and direction at their caps; beyond this only taker or directional volume |
| our pace (13 h) | 107k | arb 80k (front-loaded first deployment of the 70k sleeve), tilt 14k, mm 7k, direction 5k |
| our pace since 1 Oct | 2.6k | 56.7k / 21.5 d (bot live since 21 Oct 22:42 UTC) |

The 13-hour pace (107k) is above the 100k target only because the arb sleeve was being deployed (80k of it); the steady-state pace is
~30-40k/day (tilt 14k, mm 7k, direction 5k, arb recycling 10-20k), i.e. 30-40% of target and a gap of 60-70k/day. Account limits: 30 writes/min is not binding (batch placement; 45 writes per 60-s cycle used, 18 budgeted for MM); the binding constraint
is queue position behind 2,400-2,600 DMM shares at every touch on one-tick books (95% of quote rows): our share of the public tape in quoted markets
is 0.23% (7.3k of 3.4M shares in 12 h), and MM fills run at 22.8 shares per quoted market-hour.

## 3. Levers, ranked (volume per day in leaderboard terms; EV evidence from our logs unless stated)

| # | lever | est. volume/day | EV evidence | verdict |
|---|---|---|---|---|
| 1 | size at level 1-2 (100/150 -> 400/600) | +10k to +20k | 72% of MM fills are full (size binding), so a doubling adds <= 1.7x; 5-min markout L1 +0.15 ticks (n=147), L2 +0.79 (n=7); but the shadow size replay fails base 400 out of sample (test P&L -1.4/-3.3 vs +0.7/+2.0 at 200) | step 200 -> 300 -> 400 only while the 48-h markout at that level stays >= 0 (round 1) |
| 2 | arb turnover: recycle + unwinds (sell a held set back when its unwind value >= cost and a better set exists) | +5k to +10k | 6 of 23 held baskets (6,263 sets) unwindable at >= cost now, 33% of hourly samples; an unwind without re-entry forgoes the 3%/set edge to settlement, so only the recycle form is zero-EV (round 2) | keep the recycle rule; scan every held basket each cycle |
| 3 | quote both legs of every quoted race (now 22 of 40 races) | +3k | D-yes bid + R-yes bid summing <= 1 - x makes a passive riskless set; fills still need sweeps; our fill share is the limit, not coverage | do it, cheap |
| 4 | more markets: 60 -> all 132 non-tail markets | +3k | unquoted markets carry 31% of tape, but at a 0.23% share that is 6k shares/day; adds reads | low |
| 5 | requote when the DMM steps back (touch < 1,000: 13% of time; 41% of our fills happen then) | +3k | markout when DMM absent +0.06 ticks vs +0.15 with DMM present (n=60/95): fills arrive, but adverse | keep, no size-up |
| 6 | quote the 105 tail markets (<0.12 or >0.88, 27% of tape) | +5k | longshot YES is overpriced here (tilt s~0.10): bidding YES on longshots is -EV; selling them is the tilt sleeve, capital-bound | only the tilt side |
| 7 | two-sided quotes in DMM ladder gaps | ~0 | books: only 5% of sides have any level within 6 ticks under 500 shares; gaps barely exist | drop |
| 8 | passive sweep fades deeper (levels 3-5) | +5k | fills at 3-5 ticks: 4 fills, +0.5 to +1.0 tick markouts; a 6,400-share sweep rarely clears the 87k+ DMM depth within 5 ticks | keep levels, no extra size |

Negative-EV levers, not used: crossing the spread to churn (pays 1 tick each way on a 0.5% book: -1.0% of volume, > any rebate since SIG has
none); self-matching (one account, and the rules forbid manipulation); copy-trading or taker mean-reversion (public bot study: both lost);
buying longshot YES at the touch; news-taking is +EV but directional, outside this count.

## 4. Rounds (appended while pace < target)

**Round 1 (11:40 UTC), lever 1: size.** Joining the 180 MM fills to the tape: only 24% sit within 90 s of a >2,500-share sweep, 21% near a
1,000-2,500 print, 55% near no large print (DMM stepped back or we were alone at an improved price). Sweep fills take 56 shares on average.
The resting level-1 size after the size scale and the volatility gate is only 16-41 shares (not the nominal 100): 121 of 169 level-1 fills were
full, and in the same 5 seconds a median 954 more shares printed at or through our price (p25 156, p75 3,670). Size is binding on 72% of fills, so
raising level-1 to 100-200 shares should multiply MM volume 3-5x (7k -> 20-35k/day) as long as markouts hold: 30-min markout of sweep-filled
fills +0.11 ticks (median +0.50, n=44) vs +0.09 for the rest (n=119). Against it: the shadow size replay rejects base 400 out of sample. Ask of
the main session: let the analytics size gate step `base_qty` 200 -> 300 -> 400 one notch per 12 h while the 48-h markout at each level is >= 0,
and revert on the first negative reading. Volume gained per notch ~ +5-8k/day. Pace after this lever: ~45-55k/day of the 100k target.

**Round 2 (11:45 UTC), lever 2: arb turnover.** 23 held sell-set baskets (28,587 sets, cost 40.1k, locked edge 1,503 = 3.7%). Marking each at the
quote log's touch: 6 baskets (6,263 sets, 6.1k of volume) are unwindable at >= cost right now; a basket is unwindable in 33% of hourly samples
(median 25%). Unwinding gives the volume twice (out, then back in) but forgoes the 3%/set edge unless the capital re-enters at the same or better
return the same hour, which the sell-set condition (bids summing above 1) rarely allows at the moment asks have fallen: zero-EV only as a recycle.
Estimated +5-10k/day from recycling every held basket each cycle instead of on the 5-minute timer. Pace after levers 1-2: ~55-65k/day.

**Round 3 (11:50 UTC), levers 3-4: coverage.** Markets where both legs of the race are quoted fill 25.3 shares per market-hour (48 markets, 340
market-hours) against 8.9 for single-leg markets (17 markets, 50 hours), partly a liquidity effect; completing the 18 half-quoted races adds ~3k/day and
makes the two legs net as a set. The 31% of tape in markets we do not quote, at our 0.23% share, is 6k shares = ~2.5k/day. Coverage is not the
constraint; fill share is. Pace after levers 1-4: ~60-70k/day, still below the 100k target, and the remaining gap is the DMM's queue priority, which
no position-neutral lever removes. **Conclusion: at target is not reachable without taker or directional volume; the note stops here.** The right
measure of success is not the gap but the markout-weighted volume; `volume_report` reports both inputs.
