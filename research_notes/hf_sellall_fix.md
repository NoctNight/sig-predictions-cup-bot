# Precheck failure `churn_blowout: sets_left_at_sell_all` (29 Oct) - checker false positive, fixed in the sim

Base: main 552d50e. Diff: `hf_sellall_fix.diff` (tests/sim_pipeline.py, tests/test_guards.py; no bot code touched).

## Cause
Not a bot bug. The high-frequency arb sold everything at the sell-all cycle (150, t+75 min): at that snapshot the venue holds 0
sets in all 14 churn races. `ChurnBlowout.after_cycle` then overwrote `left_after` on EVERY later cycle, so the check judged the
end of the run (t+100 min), not the sell-all cycle. After the sell-all the sim's taker flow hit our MM bids on both legs of IA-02
(leg 905 -744 NO at t+47..73 min, leg 904 -34 at 81.5 min and -41 at 93 min), which `venue_sets` reads as 75 sets. Churn never
traded IA-02 (no arb/unw/fre basket there), its ledger was empty (lots_left 0, state file 0). The comment "MM quotes are passive:
nothing traded since" was wrong. Which MM fills land depends on live params/data (mm_overrides, allocation, books), hence "passed at
11:04, fails now" and independent of the gap pilot.

## Fix
`ChurnBlowout.after_cycle` snapshots only the first cycle at/after the sell-all (the sell-all cycle itself); the violation text says
so. The check still counts the venue's whole set position in churn races at that cycle (the sell-all must sell every set in its
races, MM-formed ones included). First attempt (min of venue sets and churn-basket net) was dropped after review: it weakened the check.

## Test
`tests/test_guards.py`: `sim churn_blowout: sets_left_at_sell_all reads the sell-all cycle only ...` drives `after_cycle` with a stub
sim: no snapshot before the deadline, clean at the sell-all cycle, later MM-built sets do not overwrite it, a set held at the deadline
is still reported. Mutant (sell-all disabled via `--patch`) still fails churn_blowout with churn_sell_all_incomplete + sets_left_at_sell_all.

## Results (live config + params copied from the main tree at ~12:00 BST)
- `churn_blowout`: 0 violations (venue_sets_left at end 75, all MM).
- Full precheck sim line (40 scenarios, all but `day`): 40 ok, 0 violations, 281 s.
- `python3 tests/test_guards.py`: ALL PASS (600 PASS).

## Review
Opus (high): no false negative today. Found the min(venue, baskets) measure unnecessary and masking (sell-all must clear MM-formed
sets too; YES baskets/'unb' skew the net): removed, fix (1) alone kept. Residual note: the check is strict to the sell-all cycle;
if the sell-all ever legitimately spreads over several cycles on thin asks (`sell()` halves and retries), this check would flag it.
Not an issue in any current scenario.
