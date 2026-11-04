# Chamber plan C1 (31 Oct 2026)

Status: approved by Sizhe in chat on 31 Oct. Built on branch `chamber-c1`, two reviews, plan in `params/punt_plan.json`. Numbers: `chamber_plan_study.py`, `data/research_diralpha/chamber/plan_table.pkl`. Plot: `figures/dir_alpha_chamber_plan.png`.
Prices at 14:55 UTC: House D SIG 85.5/86, outside 94.4. Senate D SIG 74.5/75, outside 70.3 (Polymarket 71.5, Kalshi 68.9). Net D +4,957, cash 18.3k, 3.3k above the shock reserve.
Senate forecasts seen in search results, not on the forecasters' own pages: Silver Bulletin 76.4% (22-23 Oct), Decision Desk HQ 57% (26 Oct), Akashic 57.9%.

## Changes after approval
- Cash: no 15,000 floor. The adapter's `cash()` already nets the reserve, and a second floor sent nothing. `punt_cash_floor` stays 100.
- Exit clips are 10 minutes apart, not 60 s, and each is cut to what fits the 5,000 partisan cap. If the cap holds the sale to 15:00 ET the rest rides to the result, with an error logged.
- Shock and catch-up now skip punt races.
- A new plan is refused while the timed exit is open, so the 3 Nov plan goes in after c1_exit is done. Back up `params/punt_state.json` first.
- Known cost: spendable cash falls from about 3,060 to about 460 until the wind-down brings cash in, which cuts the MM budget about 85% and pauses other entries. 842 was the tracker's top MM leg.
- `params/punt_abort` stops the exit too, so after using it the position has to be sold by hand.

## Recommended: C1, short Senate D, 10,000 shares, sold before the punt
| Item | Setting |
|---|---|
| Order | Buy NO on leg 842, limit 26c (selling Senate D at 74c or better, never chasing) |
| Size | Up to 2,600 cash, about 10,000 shares, 900 clips, at least 60 s apart |
| Entry window | 24 hours from deploy. Stops if outside NO falls below 24c (outside Senate D above 76c) |
| Exit | Sell all 3 Nov 12:00-15:00 ET, 3,000 clips, floor 20c. Anything unsold rides to the result |
| Cap | Inside it, `cap_exempt: false`. Net D goes from +4,957 to about +2,370 |
| Market making | No MM code change. An enabled plan puts U.S. Senate on the punt list, and MM, exits, raise, arb and wind-down skip punt races |
| Plan file | `chamber_plan_C1.json`. Dry run with the gate raised: 10,107 bought for 2,586, all sold, -67 with the gap unchanged |

Outcomes on 10,000 shares:
- Sold 3 Nov: -$89 if the gap stays, +$59 if it closes a third as in October.
- Limit of the exit floor: -$600. Worst case if the floor stops the sale: -$2,600.
- Effect on the punt: none, the cash is back at least 2.5 hours before stage 1.
- Why it is worth doing at all: the first live run of the punt executor, with $2,600 instead of the whole stake, and 2.6k of D room back for the catch-up sleeve, which is blocked at 4,800.

Honest size of this: about break-even on the trade. The 2.6k of D room alone could instead be bought by selling D legs for about $75, which raises cash rather than using it.

## Work needed for C1
1. Executor: `validate` refuses `outside_min` under 0.5, and a sell only fires on outside price. Add a timed exit flag for sides under 50c, with failure tests. This is the punt's own code, so it needs an Opus review and precheck. Two sell steps on one buy can oversell, so C1 has one sell step and no take-profit.
2. Config: `punt_enabled` true, `punt_plan_path`, `punt_plan_sha256`, and `punt_cash_floor` 15,000 so C1 cannot touch the shock reserve (restore 100 before the punt).
3. Tests: starting position of 856 NO already on 842 is never sold, resting MM and exit orders on 842 and 843 are cancelled, and punt-race protection stays on after C1 is flat.
4. Deploy through `botctl`, one restart. A second restart swaps in the punt plan on 3 Nov after 15:00 ET.
5. Left behind: the 856 NO already held stays out of the wind-down once the race is protected, about $215 to sell by hand.

## Not recommended
| Version | Size and cash | Needs | Sold by 3 Nov | Held to the result |
|---|---|---|---|---|
| C2: add House D long | 10,000 at up to 86.5c, 8.6k | Your cap exception to about +11k, about 20k raised | -$91 to +$196 | +$840, 6% chance of losing 8.6k |
| C3: pairs, held | 20,000, 22.5k | Cap exception to about +17k, about 20k raised | -$490 to +$380 | +$2.4k, but 22.5k gone from stage 1: about -0.5 points of top-3 chance, about -$200 expected prize, scaled from the 25 Oct punt plan |

Held to the result pays after the punt, so it only counts if we already place. House has the better edge (8.7c, forecasts agree), but only the cap and the cash objective stand in the way, and both are yours to change.
