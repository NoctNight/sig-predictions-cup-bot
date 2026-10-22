# HFT strategies on SIG: what the books allow (2026-10-22, hft side session)

Data: `data/feed_capture_20261022.jsonl` (25 min, 10:46-11:11 UTC, our MM/tilt orders inside the books), a new 90-min
recording `sigbot/proposals/hft_data/rec_20261022_1941.jsonl` (19:41-21:11 UTC: every SIG book push and print, our own open
orders (0 at start: the bot was in its kill state) and account batches, plus Polymarket's public CLOB market WebSocket for 230 mapped
tokens), the DB read-only (`arb_trades` 264 rows, `fills`, `tape` 34.7k prints, `venue_quotes`/`venue_fair` 608 polls over 19.5 h).
No orders placed. Reads: one realtime token, two open-order snapshots. Scripts: `sigbot/proposals/hft_record.py` (recorder),
`hft_study.py` (arbs | standing | moves | lead | pairs), `refill_front.py`, `pm_follow.py` (all `--backtest`/`--replay`).

## 1. The bottleneck, per sleeve

| sleeve | measured | what binds |
|---|---|---|
| set arbs | 21 h of live bot: 146 sell-sets filled (91k sets, 3,251 edge), 32 all-or-none failures; capture: 24 episodes/h with return >= 2.4%, 6 of 10 gone in < 5 s (about 7 SUSQies each), the rest standing for the whole capture | **capital, not supply**. Standing sell-sets at the end of the recording: return >= 3% to settlement: 122k capital for 5.9k (Idaho Governor alone 117k / 5.7k at 4.9%); >= 2%: 312k / 10.3k; >= 1%: 713k / 15.3k. At 20:36 UTC cash was 21.1k against the 20k reserve, 54 candidates were pending, and recycle was unwinding 2.6% sets to fund 3.4% ones |
| arb failures | 41 of 48 failed rows had no other trader's print on any leg in the 5 s before our send; 19 of 20 failures with a later poll still showed >= 1c of set edge ~55 s later; 13 of 32 retaken within 15 min | the book (DMM size on one leg) moved under a ~1 s-old view; nobody is racing us. Cooldown is already 60 s |
| market making | our MM fills = 0.21% of tape volume in quoted markets (26.3k of 12.6M shares, 21 h) | queue: 1,500-2,500 DMM shares at every touch; FIFO, no rebates |
| DMM moves | daytime capture: 639 touch changes/25 min, of which 186 with a print (sweeps), 453 without; of those only 12 (29/h, 9 markets) are both sides shifting together (a DMM requote); the rest are third-party inside orders and cancels. Evening recording: RESULT_MOVES | the DMM barely requotes; after a sweep the emptied level is back within 120 s only 47% of the time (median 21 s), i.e. it steps as often as it refills |
| Polymarket lead | RESULT_LEAD | |

## 2. Strategies, ranked by SUSQies per day (expected, after costs)

| # | strategy | trades/day | edge/trade | SUSQies/day | latency needed | risk / evidence | verdict |
|---|---|---|---|---|---|---|---|
RESULT_TABLE

## 3. Paper modules and out-of-sample results
RESULT_MODULES

## 4. What the main session should wire
RESULT_WIRE

## Sources
Budish-Cramton-Shim 2015 QJE (https://conference.nber.org/conf_papers/f69665.pdf): ES-SPY arbitrage median life 97 ms (2005) -> 7 ms (2011), profits flat. ·
Aquilina-Budish-O'Neill 2022 (https://www.bis.org/publ/work955.pdf): latency races ~20% of volume, mean 79 us. ·
Foucault-Kozhan-Tham 2017 RFS "Toxic arbitrage" (https://wrap.warwick.ac.uk/83159, abstract). ·
Polymarket 15-min markets: taker fees up to 3% from Jan 2026 (https://unchainedcrypto.com/polymarket-introduces-taker-fees-in-15-minute-markets/); practitioner bot 78 trades/day at ~$0.03 EV (https://dev.to/lkto1m/february-2026-changed-polymarket-forever-heres-what-happened-to-my-bots-numbers-2fi5). ·
Ng-Peng-Tao-Zhou SSRN 5331995 via https://quantpedia.com/systematic-edges-in-prediction-markets/: cross-venue gaps last seconds to minutes. ·
Moallemi-Yuan queue value (https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf); Albers et al. arXiv 2502.18625 (front-of-queue markout -0.06 bp vs back -0.78 bp). ·
Menkveld 2013 JFM; Brogaard-Hendershott-Riordan 2014 RFS (HFT takers trade against transitory errors). ·
Saguillo et al. arXiv 2508.03474: $39.6M realised sum-of-prices arbitrage on Polymarket, no lifetime distribution. ·
Sibling notes: research_notes/bot_exploits.md, mm_upgrade.md, volume_target.md, high_upside.md.
