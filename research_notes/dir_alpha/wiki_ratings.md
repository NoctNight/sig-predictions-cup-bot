# Wikipedia rating changes vs SIG prices (31 Oct)

Script `wiki_ratings_study.py`; events `data/research_diralpha/wiki_ratings.csv` (521 cell changes, 21 reverted within 6 h and dropped).
Public MediaWiki API via curl. I diffed the parsed wikitext of every revision rather than `action=compare` HTML, because a compare diff has no race name on the changed row.
Raters seen: Silver, IE, Split Ticket, DDHQ, Sabato, FPO, Cook, Economist, RCP, Fox, NBC. Score scale as specified (Solid/Safe +-3 ... Tilt +-0.5, Tossup 0), fixed before prices.

Event = one revision x one race, summed score change over raters nonzero, entered at the first 5-min bin at/after the edit time, in the direction of the change on D and R legs, race counted once (mean over its legs).
Panel covers 21 Oct 23:45 to 31 Oct 00:55, so 210 priced events, 177 distinct race-days, 9 days, 73 races (134 House, 45 Senate, 31 governor).

**An edit time is when an editor typed it. The rating itself often changed hours earlier (the rater's site, then an editor batches it in, e.g. "Sabato changes 10-29" at 10:22 on 10-30). The test measures what happens after a public wiki edit, not after the rating.**

| all 210 events | 1h | 4h | 12h | 24h |
|---|---|---|---|---|
| SIG mid move (c) | +0.03 | +0.12 | +0.22 | +0.58 |
| outside fair move (c) | +0.05 | +0.17 | +0.38 | +0.54 |
| net at touch (c) | | -0.52 | -0.41 | -0.03 |
| net win rate / t (race-day) | | 15% / -5.8 | 24% / -3.7 | 39% / -0.6 |

Split by whether fair had already moved that way in the prior 24 h (>= +1c, 57 events) or not (106 events):

| | 4h net | 12h net | 24h net | 24h mid |
|---|---|---|---|---|
| already moved | -0.53 | -0.23 | +0.41 (n=38, t +0.5) | +1.01 |
| not moved | -0.56 | -0.74 | -0.46 (n=80, t -1.5) | +0.18 |

By office, 12h net: House -0.37, governor -0.36, Senate -0.59; 24h: -0.19, +0.15, +0.32, none significant. First event per race-day (177): 12h -0.42, 24h -0.13.

Reading: prices and fair drift the right way (24h mid +0.6c, about +1c when fair already led) but the move is smaller than the touch spread at every horizon up to 12h, and at 24h it only matches it. The 24h positive subset is n=38 with t 0.5, and later horizons have fewer events (the panel ends on 31 Oct). No tradable edge after the edit; a rating is mostly already in `fair` and the SIG mid by the time it reaches Wikipedia.
Caveats: 9 days, overlapping events within a race-day, unreverted junk edits by anonymous users may remain, the House page batches raters in bursts.
