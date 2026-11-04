# Audit of the S48 lag rule (31 Oct)

Rule: S48 = gap minus its 48 h rolling median. Enter when |S48| >= 2c, mid 15-85c, no chamber legs, `both`, `flowok`; hold 24 h; buy ask, sell bid.
Reported: 97 trades, +2.02c, 73% win, 8/8 days, worst -3.5c, train +2.06c, test +1.99c.

## Verdict: partly real

The rule is not a data artefact. I rebuilt it from raw tables without `lab.py` and got the same 97 trades and 2.02c. I found no look-ahead,
no survivorship and no fake prices. The size of the edge is overstated, though, and the independence is much weaker than reported.

| | Net per trade | Trades |
|---|---|---|
| Reported | 2.02c | 97 |
| Strict mechanics (S4 below) | **1.37c** | 58 |
| After selection haircut | **about 1.0c** (range 0.8 to 1.3c) | 58 |
| Same, 2,000 shares per side | about 0.6c | |
| Same, 5,000 shares per side | about 0.2c | |
| Part that needs no further outside move | 0.93c | |

Independent bets: **about 30** (20.8 worst case, 39.5 on signed correlation). That sits inside 67 race-level trades, 14 direction and time
clusters, 44 races, 29 states and 8 calendar days.

## Waterfall (`audit_06_waterfall.py`)

| Step | n | Races | Net | Median | Win | SE (day blocks) |
|---|---|---|---|---|---|---|
| S0 reported | 97 | 44 | 2.02c | 1.50 | 73% | 0.26 |
| S1 entry on a real poll, exit at first real poll >= 24 h | 96 | 44 | 1.97c | 1.50 | 72% | 0.26 |
| S2 + fill at the next poll (about 90 s) | 96 | 44 | 1.85c | 1.50 | 71% | 0.27 |
| S3 + one trade per race per 24 h | 67 | 44 | 1.68c | 1.00 | 67% | 0.29 |
| S4 + no ffill in the median or lags | 58 | 41 | 1.37c | 1.25 | 64% | 0.36 |

S4 is the harshest fair version: it drops signals whose 24 h anchor falls in an outage and the batch that became valid only because ffill
filled `min_periods`. S3 is the more generous version, since a stale anchor is causal information. Hence the 1.37 to 1.68c range before selection.

## Bugs and overstatements, ranked by effect

1. **Selection: about -0.4c.** Bootstrap optimism on choosing filter and threshold is 0.55c. Empirical-Bayes shrinkage against all 89 ledger
   families takes 1.68c to 1.27c. Detail in section 7.
2. **Forward fill in signal history and `min_periods`: -0.31c, 9 bets.** The first 9 trades (22 Oct 23:45 to 23 Oct 04:00) net 3.56c. They fire on
   the first valid bin, on a median with only about 24 h of data, so that deviation is not fresh. It includes IA-02 at +9.5c and +9.0c.
3. **Mirror legs: -0.17c, and n falls from 96 to 67.** 61% of trades sit in a pair or triple on the same race. Long D and short R is one bet paying two spreads.
4. **Fill latency: -0.12c per poll, and it keeps decaying.** Delaying the fill by 5 / 10 / 30 / 60 / 180 min gives 1.81 / 1.68 / 1.46 / 1.38 / 0.98c.
   The signal row and the fill row are the same poll, so the study assumes zero latency. Part of the move arrives quickly.
5. **Stale exits: -0.05c.** Small, see section 2.
6. **Size is not in the number.** Touch median is 1,966 shares (p10 182). Slippage beyond the touch, one side: 0.07c at 500 shares, 0.19c at 2,000,
   0.40c at 5,000, 0.95c at 20,000, 1.54c at 60,000. At 20k shares a side the round trip costs 1.9c, more than the edge.
7. **The train/test agreement is not out of sample, and it does not survive correction.** See section 7. S4 first half is 1.92c, second half 1.08c, not 2.06 / 1.99.
8. **`t_day` 7.24 is meaningless.** It uses 8 overlapping days. Day-block SE gives t about 3.8 on S4. Cluster t is 4.1 on 14 clusters on S3.
9. **Not all of it is lag.** About half the P&L is the outside price continuing to move in a D-trending week. See section 8.

## 1. Look-ahead: none found

- **Truncation test.** I rebuilt the whole pipeline on data cut at four dates. 0 signal cells differed, and S48 differed by exactly 0, out of 827 to 3,583 signal cells.
- **Code reading.** The rolling median is trailing, lags use `shift(+288)`, `trades()` masks at entry only, and thresholds are fixed.
- **Flow in the bin.** `sflow` can include prints after the entry poll within the same 5-minute bin. Using only prints executed and ingested by the
  entry poll gives the same 97 trades and 2.02c, so this has no effect.
- **`tape.aggressor`.** `store.ingest_tape` classifies against `last_quote(ex, trade_ts)`, a quote at or before the trade, else a tick test. That is causal.
  - One small flaw: `prev` is the latest row by `executed_at`, so the first row of a late backfill is tick-tested against a later print.
  - 5.96% of prints were ingested more than 5 minutes late and 3.68% more than 1 hour late.
  - Classification quality differs by leg, because `quotes` only exists for legs we market-make.
  - None of this matters here: net stays 1.84 to 2.02c for flow thresholds from -1,000 to -25,000.
- **`venue_fair` integrity.** The only writer is `direction.store_fair`, at poll time. Rowids run 1 to 1,498,815 with no gaps and 0 rows out of `ts` order, so
  nothing was back-inserted, replaced or deleted (a replace would have moved a row to the end). `sig_bid`/`sig_ask` come from `prices` fetched earlier in the same cycle, so the
  touch is slightly older than the fair. That is harmless to causality.

## 2. Stale data: survives

- Outages take 12.3% of the time (gaps over 30 min). 1 entry and 19 exits had no real poll in the bin; 20 of 97 trades are affected. Exit rows were up to 143 minutes old.
- Stale-exit trades net 1.13c. Dropping them gives 77 trades at 2.21c, so staleness helps nothing.
- Keeping them and exiting at the next real poll gives 1.95c, worst -4.5c instead of -3.5c.
- Dropping entries within 15 / 30 / 60 min of a restart gives 2.20 / 2.16 / 2.04c.

## 3. Fake prices: no

- `sig_bid` and `sig_ask` have 0 nulls and 0 crossed or locked rows in 1.5M rows. There are no one-sided rows at entries or exits.
- Entries: a print at our price or better within 5 min on 70%, within 30 min on 94%.
- Exits: 30% and 66%, because exits land in quiet markets. These quotes are resting, not stale: where recorded, the touch matched our quote snapshot 94% of the time.
- Both ends printed within 30 min: 62 trades at 1.93c, against 2.17c for the rest. Tape-priced on 40 trades with same-side taker prints at both ends: 1.82c, against touch 1.67c.
- At entry the snapshot matched only 77% of the time (mean difference 0.22c), because the book is moving when the signal fires. Item 4 above covers that cost.
- Caveat: I did not test whether tape prints are genuine two-party trades. Tape volume is 171.5M shares, and 1.0M of it is ours.

## 4. Double counting and independence

- 96 trades become 67 race-level bets. 45 are D-side and 20 R-side.
- Not a handful of national moves: after removing the national mid move, net is 1.62c of 1.86c (first-trade-per-race on the as-reported 97), and corr(net, national) is 0.2.
- But one chained D-side cluster holds 33 of 67 bets (26 to 29 Oct), while national D fair (mean signed price across two-leg races) rose 2.4c from 26 to 30 Oct. D-side bets net 1.96c, R-side 0.87c.
- A single week with one drift cannot separate edge from that tailwind.

## 5. Our own footprint: not the cause

- Own volume on the leg during the hold has a median share of 0.06% of tape. Above 10% on 2 trades only. Net own quantity runs slightly against the trades (mean -170).
- Our order sits at the entry touch on 3% of entries and on 0% of exits. This looks only at orders placed in the prior 150 s, since cancel times are not stored.
- No own fill in the window: 38 trades at 2.12c. Any own fill: 59 at 1.95c.
- Legs we do not market-make: 69 trades at 2.28c. Legs we do: 28 at 1.36c. So the MM gate does not create the signal; the effect is stronger where we are absent.
- Holds spanning at least 5% bot downtime net 2.42c, against 1.23c for the rest. That is the wrong way round for a footprint, though it is confounded with the late days.
- Watch: 14 trades with same-way non-MM fills (catch-up, shock, exits) net 4.14c, against 0.86c for 14 opposite. Those sleeves fire on the big moves. Removing all 28 leaves 1.82c.

## 6. Survivorship: none

- With exit-quote skipping removed: 97 signals, 0 dropped. No leg settled or vanished in a held trade.
- 22 signals in the last 24 h were cut by the panel end. One per race that gives 13 held-out signals, mean hold 11.5 h, net so far 0.88c, 77% win.
  Earlier trades at the same elapsed hours made 1.05c. This is the only data no reported number used, and it agrees, weakly.

## 7. Selection bias

- Search size: ledger 4,280 configs at the start of this audit, 5,702 by the end (another process is still appending), in 90 families. On this signal alone,
  d2 had 1,632 configs, d3 700 exit rules, d4 273 filter combinations.
- Train/test is contaminated. d2 mapped thresholds and bands over the full sample, and d3 printed test-half columns before d4 fixed the filters.
- In its favour: the rule ranks 55 of 273 by net, so it is not the grid maximum. It also sits well above the field, with the 99th percentile of family-best 24 h net at 1.28c
  and the median at -0.26c.
- Filter uplift holds in both halves. Base trades passing the filters: 1.61c and 2.06c. Failing: 0.65c and 0.54c.
- Haircut: 0.55c from day-block bootstrap on filter plus threshold, 0.41c from empirical Bayes on S3. Both are rough with 8 days, and the hold overlaps the bootstrap blocks. I use 0.4c.
- Timing placebo: circular shifts of signal and all filters, 100 draws: mean -0.43c, p95 +0.27c, max +0.37c. So the timing information is real.
- With the filters off, the base rule under the same corrections is 0.60c on 163 race-level trades, the least mined number here.

## 8. Other findings

- **P&L make-up (S3).** net = 0.93c + 0.78 x further outside move. The mean further move is 0.96c (median 0.46c). On the 68 first-per-race trades of the reported set, trades where the outside kept going net 3.52c (33), flat 0.54c (14),
  reversed 0.14c (21). So the pure lag pays about 0.9c if outside stops, and more only if the week's trend repeats. Across band legs, outside continues only 0.49c after a 1c move.
- **Concentration.** The top 5 of 68 trades give 31% of P&L and the top 10 give 53%. The median is 1.0 to 1.25c. S4 without its top 2 is 1.15c.
- **Latest entries weaker.** S4 entries on 29 Oct net 0.46c (14), the rest 1.66c (44). The held-out signals were better, so this is unresolved.
- **One regime.** All of this is one pre-election week. Nothing tests it elsewhere.
- **Not checked.** Fees: I found none in `README.md`, but did not confirm against the venue. Book walks use 43 snapshots, none within minutes of most trades.

## Reproducing

`AUDIT_CACHE=<dir>` must be set, then run from the repo root, in this order: `audit_dump.py`, `audit_01_repro.py`, `audit_02_stale.py`, `audit_03_prices.py`,
`audit_04_structure.py`, `audit_05_corrected.py`, `audit_06_waterfall.py`. Shared code is in `audit_common.py`. None of them imports `lab.py` or `extract.py`, and the DB opens read-only.
