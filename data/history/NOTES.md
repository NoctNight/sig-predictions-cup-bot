# Notes: non-major-party general-election winners, U.S. Senate / Governor / U.S. House, 1970-2025

Compiled 2026-10-21. Scope: general elections only (plus November-ballot Senate specials), U.S. states only
(territorial delegates and the Puerto Rico Resident Commissioner excluded), winner counted only if NOT the
Democratic or Republican nominee in that general election.

## Files
- `third_party_wins.csv` -- 29 rows: 11 Senate, 8 Governor, 10 House.
- `races_per_year.csv` -- number of general-election contests per office per year.
- This file.

## Inclusion rule (as applied)
A winner is counted if, on the general-election ballot, they were not the nominee of either the Democratic or
Republican Party. Consequences:
- Lisa Murkowski (AK Senate 2010, write-in) -- EXCLUDED: a Republican running as a write-in; Wikipedia's Senate
  list labels her "Republican (write-in)". (https://en.wikipedia.org/wiki/2010_United_States_Senate_election_in_Alaska)
- William Carney (NY-01, 1978) -- EXCLUDED. Often described as "elected as a Conservative," but he won the 1978
  Republican primary and appeared on both the Republican (47.18%) and Conservative (9.07%) lines; he was the
  Republican nominee. (https://en.wikipedia.org/wiki/William_Carney_(politician))
- Party switchers after election (Jim Jeffords 2001, Charlie Crist 2010, Joe Manchin 2024, Kyrsten Sinema 2022,
  Justin Amash 2019, Paul Mitchell 2020, Kevin Kiley 2026, Bob Smith 1999) -- EXCLUDED: elected as D or R.
- Appointees (Dean Barkley, MN Senate 2002) -- EXCLUDED: not elected.
- Fusion/cross-endorsement where the winner was also the D or R nominee (routine in NY and CT) -- EXCLUDED.
- Minnesota "Independent-Republican" (1975-1995) and "DFL" are the state Republican and Democratic parties -- EXCLUDED.
- Territorial delegates (Faleomavaega AS, Sablan MP, Pierluisi PR) appear as independent/NPP winners on Wikipedia's
  House performance list -- EXCLUDED (not among the 435 voting seats; House denominator is 435).

## Edge cases decided (and INCLUDED)
1. **Bernie Sanders, VT-AL 1994, 1998, 2002 (and Senate 2006, 2012, 2018, 2024).** Sanders won the Democratic primary
   as a write-in in several years but declined the nomination each time and appeared on the general ballot as
   "Independent" only. Treated as not the Democratic nominee. Source: Electoral history of Bernie Sanders,
   https://en.wikipedia.org/wiki/Electoral_history_of_Bernie_Sanders
2. **Jo Ann Emerson, MO-08 1996 (regular general election).** FLAGGED. Missouri law barred her from the Republican
   primary after Bill Emerson's death; she ran for the full term as an Independent and won 50.47%, while the actual
   Republican nominee, Richard Kline, took 10.53%. On the same day she won the special election for the remainder of
   the term as the Republican nominee (63.31%). Under the stated rule the full-term race counts (she was not the R
   nominee in it); the special does not. She rejoined the GOP in January 1997. Some analysts would exclude this
   row as a de facto Republican win -- remove it if you prefer that reading.
3. **Virgil Goode, VA-05 2000.** Left the Democratic Party in January 2000, ran as an Independent, no Republican nominee
   (GOP endorsed him). Counted. Joined the GOP in 2002.
4. **Bill Walker, AK Governor 2014.** Ran as Independent on a "unity ticket" with Democrat Byron Mallott; the Democratic
   nominee withdrew and the party backed Walker, but the ballot line was nonpartisan/independent, not Democratic. Counted.
5. **Jesse Ventura, MN 1998.** Reform Party nominee; party later renamed Independence Party. Counted as "Reform".
6. **Walter Hickel, AK 1990.** Alaskan Independence Party line; the Republican nominee was Arliss Sturgulewski. Counted.
   He rejoined the GOP in 1994, after election.
7. **Joe Lieberman, CT 2006.** Lost the Democratic primary; Ned Lamont was the Democratic nominee. Counted ("Connecticut
   for Lieberman").
8. **Harry F. Byrd Jr., VA 1970 and 1976.** Left the Democratic Party in 1970; Democrats fielded George Rawlings in 1970
   and Elmo Zumwalt in 1976. Counted both.
9. **James L. Buckley, NY 1970.** Conservative line only; Republican nominee was incumbent Charles Goodell. Counted.

## Searched for and found none
- Senate: no other non-major winners 1970-2025 per Wikipedia's Senate candidacies list (bold = winner) and the
  "Third-party and independent members of the United States Congress" table. No odd-year Senate specials were held on
  a November ballot in range (TX 1993 May/June; MA Jun 2013; NJ Oct 2013; AL Dec 2017), so no odd-year Senate rows.
- Governor: no other winners per Wikipedia's third-party gubernatorial performances list (bold = winner) and the
  "List of third-party and independent United States governors". 1970-1973 had none (Longley 1974 is the first).
- House: Wikipedia's Congress membership table lists only Sanders, Emerson, Goode and Carney as members first seated
  1970-2025 under a non-major label; Carney excluded as above. No special-election winners. Note that Wikipedia's
  "List of third-party and independent performances in United States House elections" is incomplete (no sections for
  1990-1996), so the membership table and Sanders' electoral history page were used as the authoritative House sources.

## races_per_year.csv methodology
- **Senate** (even years only): regular Class elections plus special elections held on the November general ballot,
  taken from the `seats_for_election` infobox of each "YYYY United States Senate elections" Wikipedia page
  (https://en.wikipedia.org/wiki/YYYY_United_States_Senate_elections). Where a state held a concurrent special AND
  regular election for the same seat on the same ballot, both contests are counted (they were separate ballot items
  with separate vote totals): GA 1972, NC 1986, IL 2010, CA 2022, CA 2024. Wikipedia's seat counts are one lower in
  those five years (33, 34, 37, 35, 34 respectively) if you prefer seats rather than contests. Specials held on
  non-November dates are excluded (VT Jan 1972, OR Jan 1996, MA Jan 2010, and all odd-year specials).
- **Governor** (all years): state governorships only (territories excluded), from the `seats_for_election` infobox of
  each "YYYY United States gubernatorial elections" page (https://en.wikipedia.org/wiki/YYYY_United_States_gubernatorial_elections).
  Included: UT 2010 special, WV 2011 special, OR 2016 special, CA 2003 recall (replacement election held and won),
  WI 2012 recall (election held). Excluded: CA 2021 recall (recall question failed; no replacement election).
  FLAGGED: 1971 = 3 follows Wikipedia (KY, MS, LA); Louisiana's general for that cycle was actually held Feb 1, 1972
  (closed-primary era). Louisiana's jungle-primary-era elections (1975 onward) are counted in the odd year in which
  the deciding round occurred. Odd years: 1 mod 4 = NJ, VA (2); 3 mod 4 = KY, LA, MS (3), except as noted.
- **House**: 435 in every even year per instructions; odd-year specials ignored (no rows).

## Uncertain / flagged rows
- Emerson 1996 (see edge case 2) -- definitional, not factual, uncertainty.
- Sanders 2002 and 2004 House vote percentages not recorded (not needed for inclusion; the electoral history page
  shows him winning as Independent both years).
- 1971 governor count (LA timing, see above).

## Sources consulted
- https://en.wikipedia.org/wiki/List_of_third-party_and_independent_candidacies_in_United_States_Senate_elections
- https://en.wikipedia.org/wiki/List_of_third-party_and_independent_performances_in_United_States_gubernatorial_elections
- https://en.wikipedia.org/wiki/List_of_third-party_and_independent_performances_in_United_States_House_elections
- https://en.wikipedia.org/wiki/Third-party_and_independent_members_of_the_United_States_Congress
- https://en.wikipedia.org/wiki/List_of_third-party_and_independent_United_States_governors
- https://en.wikipedia.org/wiki/Electoral_history_of_Bernie_Sanders
- https://en.wikipedia.org/wiki/Jo_Ann_Emerson ; https://en.wikipedia.org/wiki/Virgil_Goode ;
  https://en.wikipedia.org/wiki/William_Carney_(politician)
- Per-year "YYYY United States Senate elections" and "YYYY United States gubernatorial elections" pages (1970-2025).
- Per-race pages cited in `source_url` of third_party_wins.csv.
- senate.gov "Senators Representing Third or Minor Parties" returned HTTP 403 to automated fetch and was not used;
  MIT Election Data lab not used. Ballotpedia/House Clerk statistics not used for cross-check.
