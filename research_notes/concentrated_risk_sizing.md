# Concentrated-risk directional sleeve: sizing numbers (2026-10-22, read-only)
Script: `research_notes/concentrated_risk_sizing.py` (stdlib, ~70 s, seed fixed, reproduces every number below). No orders placed.

## Assumptions (read first)
- Legs: the 19 with verdict trade_yes/trade_no in `params/fair_explained.json`. "Fair" = its `believed_fair` (outside blend nudged by model, the number that drives the verdict). `model` and `outside_blend` shown separately.
- Prices: latest SIG bid/ask from `data/sigbot.sqlite`. `quotes` only holds ~32 MM legs, so for the rest the same bot log's `venue_quotes` (venue='sig') was used; the later of the two wins (all 1-10 min old). Prices have moved up to 1 tick vs the file's.
- Entry: YES leg pays the bid; NO leg pays 1 - ask (rests a sell of YES at the ask). Notional = cost; loss if wrong = notional; shares = notional / price. 100% fill assumed (see caveats). Edge/share = fair-of-our-side - entry.
- Greedy by edge/share, 6,000/leg, 40,000 total, |net D| <= 15,000 (net D from cost; D-long = D YES + R NO). Current caps: 300/leg, 3,000 total, 5,000 netD.
- Sim: 20,000 elections, same draws in every scenario. Race D-margin = 5.3*z(pD) + s + e, s~N(0,3), e~N(0,4.37) (4.37 = sqrt(5.3^2-3^2), so each race's marginal P(win) equals its fair exactly; common-factor correlation 0.32). A race with D and R legs is ONE outcome (pD = mean of D fair and 1-R fair). "Literal" variant: e sd 5.3 on top of s (marginals broader, shown as last scenario).

## 1. Verdict legs (passive entry, ranked by edge; sized = proposal)
| leg | side | SIG bid/ask | entry | model | outside | fair | edge/sh | size | exp profit | win | loss |
|---|---|---|---|---|---|---|---|---|---|---|---|
| RI Senate [R] | NO | .135/.14 | .860 | .984 | .984 | .984 | +.124 | 6,000 | 867 | 977 | -6,000 |
| RI Senate [D] | YES | .865/.87 | .865 | .984 | .984 | .984 | +.119 | 6,000 | 824 | 936 | -6,000 |
| MS Senate [D] | NO | .20/.205 | .795 | .864 | .890 | .883 | +.088 | 6,000 | 661 | 1,547 | -6,000 |
| MS Senate [R] | YES | .795/.80 | .795 | .864 | .887 | .880 | +.085 | 6,000 | 642 | 1,547 | -6,000 |
| MA Senate [D] | YES | .905/.91 | .905 | .995 | .984 | .989 | +.084 | 6,000 | 560 | 630 | -6,000 |
| TN Senate [D] | NO | .12/.125 | .875 | .955 | .961 | .959 | +.084 | 6,000 | 576 | 857 | -6,000 |
| CT Governor [D] | YES | .90/.905 | .900 | .995 | .968 | .981 | +.081 | 4,000 | 361 | 444 | -4,000 |
Not sized (budget used; edge/sh): MA Gov D .079, TN Sen R .079, RI Gov D .076, DE Sen R .074, MA Sen R .074, GA Sen R .070, TN Gov R .069, GA Sen D .064, IL Sen R .063, OK Gov D .060, NC Sen R .060, FL Sen D .049.
Totals: 40,000 notional, expected profit +4,490, max win +6,939, max loss -40,000, net D +4,000 (cap 15,000 not binding). Current caps: 3,000 notional, EV ~+310.
**Structure issue:** the greedy takes BOTH legs of two races (RI Senate 12,000, MS Senate 12,000): D and R legs are the same bet, so "6,000 per leg" is 12,000 per race and 24,000 of the 40,000 sits in two races. Net-D is low (+4,000) because MS/TN R offsets RI/MA/CT D, but gross exposure is not hedged.

## 2. Portfolio P&L to settlement (sleeve only; account P&L = this + existing book)
| scenario | book | mean | p5 | p50 | p95 | P(<-10k) | P(<-20k) | P(<0) | worst |
|---|---|---|---|---|---|---|---|---|---|
| base | proposal | 4,420 | -8,155 | 6,939 | 6,939 | 1.5% | 0.0% | 14.1% | -28,698 |
| base | current caps | 309 | -283 | 471 | 471 | 0% | 0% | 17.7% | -1,311 |
| base | proposal, 1 leg/race | 4,024 | -1,664 | 5,883 | 5,883 | 0.3% | 0.0% | 20.9% | -17,816 |
P(all 7 legs win) = 80.8% (p50 = p95 = max win). Left tail: the 14% losing elections are 87% Mississippi (loses -12,000 gross, both legs); P&L < -10k is 84% Mississippi and 77% Tennessee Senate. Mean/sd 4,420/5,552 vs 309/318.
National swing: with no race noise the book is flat (+6,939) for s from -9 to +6; it only flips at s=+9 (-8,155, Mississippi) and s=-12 (-11,419). The swing term barely matters; the loss driver is single-race upsets (Mississippi 12.2%, Tennessee Senate 4.1%, others 1-2%).

## 3. Sensitivities (proposal / current caps)
| case | prop mean | prop p5 | P(<-10k) | P(<-20k) | P(<0) | cur mean | cur p5 |
|---|---|---|---|---|---|---|---|
| fair shrunk 50% to venue mid | 2,256 | -11,419 | 5.6% | 0.4% | 24.4% | 157 | -617 |
| 1 tick worse entry (= taking the touch here) | 4,161 | -8,336 | 1.5% | 0.0% | 17.0% | 290 | -299 |
| shrink 50% + 1 tick worse | 2,010 | -11,588 | 5.6% | 0.4% | 29.5% | 139 | -630 |
| truth = outside blend only | 4,443 | -8,155 | 1.6% | 0.0% | 13.4% | 303 | -283 |
| truth = our model only | 4,220 | -8,155 | 1.6% | 0.0% | 15.8% | 298 | -283 |
| truth = venue mid (fully shrunk) | 89 | -15,013 | 11.5% | 1.2% | 34.7% | 4 | -967 |
| literal noise (e sd 5.3 + swing) | 3,405 | -8,155 | 2.8% | 0.2% | 18.8% | 234 | -283 |
Sizes held at the base allocation in each row. One-leg-per-race book under 50% shrink: mean 2,072, p5 -8,330, P(<-10k) 2.0%. Edge is mostly SIG-vs-outside (RI Senate model = outside = .984), so the model-only and outside-only rows barely move; it vanishes only if SIG's price is right.
Sleeve size (base, 6,000/leg): total 10k mean 1,395 / worst -10k; 20k mean 2,499, p5 -6,087; 40k mean 4,420, p5 -8,155 (EV is ~11% of notional, roughly linear; tail grows faster).

## 4. Dominant legs and prize relevance
- Expected profit: RI Senate (2 legs, one race) ~1,670 (38%); Mississippi ~1,300 (29%); TN Senate 577; MA Senate 563; CT Gov 364. Loss risk: Mississippi (12.2% x 12,000), then Tennessee Senate (4.1% x 6,000). RI Senate has the biggest edge but only 1.8% loss chance.
- Stop-loss: bot drawdown = trailing-hour peak minus account value. 40,000 sleeve holds ~46,900 shares: 1 point on all prices = ~469. Marked on `data/hist.json` hourly closes (53 usable hours, 10/13-10/21) the hypothetical book had worst 1-hour drop 1,130 and worst 24h peak-to-now 2,224. A 400 stop trips on ~0.9 pt of book-wide move; 2,000 on ~4.3 pts.
- Prizes (1st $30k, 2nd $5k, 3rd $2.5k; top-3 only). Leaders per `data/leaderboard_snapshot.json` (08:43 today, 1,752 traders): #1 +983k, #3 +855k. We need ~+854k; the sleeve's maximum win is +6.9k (123x short), and even 100% of cash at these ~0.80-0.90 prices returns ~11-26%. The sleeve does not change prize odds (P(final >= #3) = 0 in every scenario).
- Field rank (snapshot, others also move): +270 is rank ~635/1752; +270+4,490 EV ~221; +270+6,939 (all win) ~157; -20k ~1,596. Field p50 0, p75 +1.4k, p90 +6.4k. So the effect is expected finish position only: roughly +400 places in the base/all-win case vs ~-960 places in a -20k case.

## Caveats
- Passive fills not modelled: expected profit scales with fill share; a bid that does not fill earns 0, so the realised book may be smaller and drift to whichever legs the market sells into.
- Fair is the bot's own believed fair, not independent truth; several legs have thin outside depth (MS Senate R deep at 399k; RI/TN Senate and TN/RI Gov thin).
- Settlement can lag and be corrected after resolution (per research_notes/past_winners_advice.md); settlement-risk and void-refund cases not modelled.
- Existing positions (cash 67k, positions 33k, netD +293) are not included; the sleeve adds to them.
