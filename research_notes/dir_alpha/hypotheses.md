# Directional alpha on SIG: hypothesis list (31 Oct overnight)

Goal: find a signal for 1 or 2 big trades held hours, not days, sold early. Everything is judged net of the full spread at the
touch, on later days than it was found on, with the count of tests kept in `data/research_diralpha/ledger.jsonl`.

Known already (not retested as such): raw gap to outside does not close before the punt (`gap_trades.md`); 5-min mean reversion
exists but is not a taker edge (`meanrev.json`); news moves are mostly priced (`news_history.md`).

## Round 1: price and flow on the leg itself
- H01 momentum / reversal of the SIG mid over 15m to 48h
- H02 signed taker flow over 1h to 24h (informed flow continues)
- H03 big prints only (>= 2k notional): follow or fade
- H04 flow against price: price up on net selling (absorption)
- H05 volume shock with direction
- H06 inventory of the designated market maker (cumulative flow into it) mean-reverts or skews quotes

## Round 2: outside venues
- H07 fresh gap: gap now minus its own 24 h / 48 h median (old gaps are structural, new ones may close)
- H08 outside moved, SIG did not (lag) by lookback and by office
- H09 Polymarket-only vs Kalshi-only moves: which venue leads
- H10 Polymarket order book imbalance leads
- H11 outside trend (slow post-news drift) over 12 h to 72 h
- H12 venue disagreement: gap signal only when venues agree
- H13 gap and flow agree (double confirmation)

## Round 3: cross-market
- H14 national factor lead: Senate legs lead House legs, or the reverse
- H15 laggards of a national move catch up
- H16 same-state peer (Senate and Governor) lead
- H17 chamber control price vs the sum of seat prices
- H18 complement leg moves first (D leg before R leg) and overround
- H19 office-level gap: which office is cheap relative to outside

## Round 4: calendar and structure
- H20 hour of day and overnight drift, by office
- H21 scheduled repricing of the market maker (step changes at fixed hours)
- H22 favourite drift and compression unwinding toward the election, by office
- H23 round-number barriers and ladder step effects
- H24 spread and quote asymmetry: one side steps first

## Round 5: events
- H25 multi-leg sweeps (a big seller across many legs at once) then recovery, as on 30 Oct 10:48
- H26 single-leg sweeps by office and size
- H27 repeated clip sizes (a working order): follow it
- H28 news events by direction and confidence, by office
- H29 new polls against the price
- H30 our own model against SIG
- H31 large trader footprints (leaderboard changes, known whale behaviour)

## Then
For anything that survives: how often is it right, how long until X% of the eventual move, best exit, worst-case loss, capacity at
20k to 60k, permutation test, independent red team. Keep adding ideas as results come in.
