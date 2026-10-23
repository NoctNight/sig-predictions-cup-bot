# Tilt sleeve: does it deserve more capital? (backlog item 6, daily run 2026-10-23)

**Question.** The tilt sleeve (sell longshots / buy favourites, held to settlement, because venue prices are compressed
toward 50%) is capital-starved at `tilt.capital_frac` 0.2. Should capital move from arb sets to tilt?

**Data** (`python3 -m sigbot.review --minutes 1440 --json`, 11:11 BST; local SQLite only).

| sleeve | capital at cost | P&L marked | P&L settlement basis | fills 24 h | markout 5 min | markout 30 min |
|---|---|---|---|---|---|---|
| tilt | 7,509 (21 positions) | +13 (0.18%) | +13 | 233 fills, 15.3k shares | +0.04 ticks (n 286) | -0.04 ticks (n 282) |
| arb sets | 32,161 (8 races) | -89 | +1,293 locked (4.0% of payout) | 1,148 fills | riskless | riskless |

**Reading.** Tilt's thesis (compression `s` about 0.10) pays only at settlement on 4 Nov; a day of marks cannot confirm
or refute it, and the marks say exactly that: zero. Its fills are not adversely selected (markouts within 0.05 tick of
zero, t about 0.5), so nothing says the sleeve is bleeding either. Arb sets lock about 3-4% of payout in 12 days with no
risk; the allocator ranks capital by measured edge and tilt has none measured yet.

**Verdict.** Does not clear the bar. No capital moves to tilt: keep `tilt.capital_frac` 0.2, keep its resting orders.
Re-check only if settlement-basis evidence appears (it cannot before 4 Nov) or if the 30-min markout turns clearly
negative (then cut it).

**Caveat.** Tilt positions are counted at marks that are themselves compressed; if the thesis is right the sleeve is
worth more than shown, if wrong it is worth less. Both are invisible until settlement.
