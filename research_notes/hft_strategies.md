# HFT strategies on SIG: what the books allow (2026-10-22, hft side session)

Data: `data/feed_capture_20261022.jsonl` (25 min, 10:46-11:11 UTC, daytime; our MM/tilt orders are inside these books); a new
39-min recording `sigbot/proposals/hft_data/rec_20261022_1941.jsonl` (19:41-20:21 UTC: every SIG book push and print, our open
orders (0 at the start: the bot was in its kill state; it resumed after ~20:00, 884 order updates logged), and Polymarket's public
CLOB market WebSocket for 230 mapped tokens); the DB read-only (`arb_trades`, `fills`, `orders`, `tape` 34.7k prints,
`venue_quotes`/`venue_fair` 608 polls over 19.5 h). No orders placed; SIG cost: one realtime token, one open-order read.
Statistics re-derived by a Fable pass (section 3). Reproduce: `python3 -m sigbot.proposals.hft_study arbs|standing|moves|lead|pairs <file>`,
`refill_front --backtest <file>`, `pm_follow --backtest | --replay <file>`, `own_book --backtest`; recorder `hft_record --minutes N --out F`.

## 1. The bottleneck, per sleeve

| sleeve | measured | what binds |
|---|---|---|
| set arbs, supply | standing riskless sell-sets (buy NO on every leg; 6-level books, so lower bounds) in the clean part of the recording (19:53 UTC): return to settlement >= 3%: **~125k capital for ~6.0k** (Idaho Governor bids 0.95 + 0.12: 122k / 5.9k, 4.9-6.5%); >= 2%: ~300k / 10.3k; >= 1%: ~710k / 15k. 44 of 114 two-leg races have bid sum >= 1.01 at any moment | **capital**. At 20:36 UTC cash 21.1k vs the 20k reserve, 54 candidates pending, recycle unwinding 2.6% sets to fund 3.4% ones; we hold 2,469 Idaho sets |
| set arbs, transient | daytime: 8 transient episodes >= 2.4% return in 25 min on 4 races; 4 clean ones under 5 s worth ~6.5 SUSQies each (we took 1); the rest were our own tilt bids (Delaware) or standing. Evening: only a 599-share Illinois Senate bid flickering every ~18 s for 4-9 s, never printed | none worth racing for: 25-50/day gross, at 2.5% return, below the 5-6% standing margin |
| set arbs, failures | 33 real all-or-none failures in 21 h (146 fills, 91k sets, 3,251 edge): **17-18 priced a leg off our own resting tilt bid** (the venue will not cross us with ourselves), 7 re-sent the basket within 30 s of our own fill on a stale book; 26 of 33 had no other trader's print in the 5 s before | our own book, not competitors and not latency |
| market making | our MM fills = 0.21% of tape volume in the markets we quote (26.3k of 12.6M shares, 21 h) | FIFO queue: 1,500-2,500 DMM shares at every touch, no rebates |
| DMM moves | daytime: 639 touch changes / 25 min; 186 with a print, 453 without, of which only 12 (29/h, 9 markets) shift both sides (a DMM requote), the rest are third-party inside orders and cancels. Evening: 9 shifts in 39 min (14/h, 5 markets), all 1 tick. After a sweep the DMM refills the emptied level within 120 s in only 34 of 80 cases (median 6 s): it steps as often as it refills | the DMM barely moves, so there is little to pick off |
| Polymarket lead | seconds (recording): 52 Polymarket moves >= 1c in 30 s on 12 legs, SIG mid followed 0/52 at 1-10 s and 1/52 at 30 s-10 min; but SIG had 1 DMM shift on 57 mapped legs in 35 min, so this bounds only a fast response. Minutes (venue_quotes, 20 h, one leg per race): after a >= 1-pt Polymarket poll move SIG moves +0.38 tick at 10 min (6% of the move, se 0.20, n 52), +0.05 at 30 min, +0.04 at 60 min; after >= 2-pt moves -0.25 at 60 min. The Polymarket move itself reverts 50-76% within 4-60 min. Polymarket books for these races update every few seconds with 2-3c spreads | no exploitable lead at any horizon we can measure; the earlier "44% at 30 min" (bot_exploits.md) is not reproduced |

## 2. Strategies, ranked by SUSQies per day (expected, after costs)

| # | strategy | trades/day | edge/trade | SUSQies/day | latency needed vs our ~1 s | risk / evidence | verdict |
|---|---|---|---|---|---|---|---|
| 0 | (not HFT) **put capital into the standing sell-sets** at >= 4% (Idaho Governor 6.5% net per Fable, RI Governor 3-leg) | a few baskets | 4-6.5% of cost to 4 Nov | **~400-450 accrued per day** on ~120k (5.9k to settlement); every 10k moved from a < 2% set into Idaho adds ~+400 at settlement | none (standing for hours) | riskless if SIG settles normally (NO on both legs pays 1 per set; no collateral netting: collateralSavings 0 on our Idaho fills); capped today by `max_race_frac 0.15` and the 20k cash reserve | biggest number in this note |
| 1 | **exclude our own resting orders from the arb scanner's books** (`own_book.py`) + no re-send on a book older than our last fill | removes ~24 of 33 failed baskets/day | each failure costs a write and a 60-s race cooldown | 10-50 (recovers arbs blocked by cooldown; stops selling into our own tilt bids) | none | none; pure bug fix | wire |
| 2 | inside-spread quoting on 3+-tick DMM spreads (`inside_quoter.py`, already live as purpose `inside`) | 100-400 fills | +0.07-0.14 tick | 30-80 (bot_exploits.md estimate, not re-tested) | 1-5 s | inventory | keep |
| 3 | follow Polymarket moves on SIG (`pm_follow.py`, WebSocket entry) | 5-15 signals | defaults (1.5c/5 min, 1 h hold): **-0.46 pts/share, 95% CI [-0.74, -0.18]**, n 13; best of 48 configs +0.75 [-0.46, +1.96], n 6 | **about 0 to negative** | none (nobody else trades it; the DMM does not follow) | SIG catches 6% of the move at 10 min, ~0 at 1 h; the Polymarket move reverts 50-76% | reject; do not wire `dmm_lag_follow` (its +95/day is 21 independent signals, ±1-tick coin flips, daily CI about [-20, +210]) |
| 4 | transient cross-leg arbs (DMM requoting one leg first) | ~10 real/day | ~6.5 | 25-50 gross, ~0 incremental | 1-3 s, we lose about half at ~1 s | capital-locked at 2.5% when 5-6% sets stand unfilled | covered by the scanner; no new sleeve |
| 5 | D-R stat-arb (mid sum vs 1 - x) | - | - | 0 | - | mid sum ~1.010 in every office (bid sum ~1.005), AR(1) 0.983, 94% of consecutive snapshots unchanged: a persistent per-race overround = the standing sell-set pool, not a mean-reverting signal | reject (it is row 0) |
| 6 | 2-tick queue games (improve to the mid, rebate-free) | - | improving fills -0.01 tick (bot_exploits F2); refill-at-mid -0.3 | 0 / negative | - | adverse selection | reject (F2 already live) |
| 7 | refill front-running: post at the level a print just emptied, first in the FIFO queue (`refill_front.py`) | ~1,000-2,800 fills | **-0.34 tick at mid at 1 and 5 min** (daytime: 49 fills on 35 markets, se 0.10; evening: 27 fills, -0.29 / -0.34); -1.0 tick exiting at the touch | **negative**, about -15/1,000 shares | 1 s is enough (queue empty on arrival in 80 of 81) | sweeps carry information: the DMM steps toward the flow | reject |
| 8 | DMM pick-off from the Polymarket WebSocket (take its stale touch before it requotes) | <30 | the DMM requotes 1 tick, 14-29 times/h venue-wide, never seconds after Polymarket | 0 | would need a predictable requote, there is none | - | reject |

Literature fit: latency races elsewhere are 7-97 ms and ~20% of volume (Budish-Cramton-Shim 2015; Aquilina-Budish-O'Neill 2022);
here the DMM's "staleness" is minutes long and does not resolve toward the outside price, so speed buys nothing. Polymarket's
own 15-minute markets were sniped from Binance until taker fees (up to 3%, Jan 2026) because those quotes did converge to the
signal; SIG's DMM does not. Queue papers (Moallemi-Yuan; Albers et al. 2025: front-of-queue markout -0.06 bp vs -0.78 bp at the
back) predict that front-of-queue fills are less toxic; here the front-of-queue fills after a sweep are still -0.34 tick, because
the DMM itself steps. Sum-of-prices arbitrage is the one item that transfers (Saguillo et al. 2025: $39.6M on Polymarket), and here
it is limited by capital, not by finding it.

## 3. Paper modules and out-of-sample results (Fable-checked)

* `hft_record.py`: the read-only recorder (SIG feed + own orders + Polymarket WebSocket, best bid/ask on change only; ~30 MB/h).
* `refill_front.py --backtest`: `RefillFront.on_book(ex, prev, book, prints)` returns the post. Out of sample = the evening recording
  (no parameter was fitted on the capture): -0.29 / -0.34 tick at 1/5 min, the same as in-sample. The fill model now also fills us
  when the opposite touch moves through our price (the crossing case Fable found: 8 extra fills, result unchanged). Rejected.
* `pm_follow.py --backtest`: fit on the first 9.75 h, test on the second. The fit-best config gets 1 test trade; 23 of 48 configs
  lose out of sample, 12 never trade, 13 win; defaults lose the spread reliably. `--replay`: 7 WebSocket signals, 0 SIG responses. Rejected.
* `own_book.py --backtest`: `OwnLevels.net(ex, book)` removes our resting size from a feed book; on `arb_trades`, 18 of 33 failed
  baskets had one of our non-arb orders at the leg's priced bid (upper bound: cancel times are not logged; Fable's tighter count: 17),
  7 were re-sent within 30 s of our own fill.
* Fable corrections applied: the standing-episode flag now uses each race's first full book (the capture's initial snapshot spans
  85 s); the walk notes that 6-level books truncate depth (Idaho's R leg exhausts all 6 levels, so its 122k is a lower bound);
  pm_follow trades the race's D leg. Own-order bias: the capture's Delaware "arbs" were our own tilt bids; the recording after
  ~20:00 UTC contains our orders again, so the clean standing numbers are from the 19:53 UTC prefix.

## 4. What the main session should wire

1. **Arb scanner: net our own resting orders out of the books** before `candidates_from_prices` and `chunks_for_race`
   (`own_book.OwnLevels`, fed from the account feed's orderUpdates or the bot's open-order list), and skip a race until a book
   newer than our last basket fill on it arrives. Removes ~24 of 33 failed baskets (each a wasted write and a 60-s cooldown).
2. **Capital decision, not HFT**: the standing riskless sell-sets earn 4-6.5% to settlement on ~120k (Idaho Governor alone),
   more than anything in section 2. Options: raise `max_race_frac` for sell-sets with return >= 4% (they are riskless, so a per-race
   cap guards only settlement-mechanics risk), and let `offload` feed them from sets returning < 2%. Note: the bot was in its kill
   state (markout30 -2.42, n 12) at 19:36 UTC, and `ArbScanner.one_pass` returns early while killed, so no arbs execute then.
3. Do not wire `pm_follow`, `dmm_lag_follow` or `refill_front`. Keep the inside quoter and the outside-fair MM gate (mm_upgrade.md).
4. Optional: record a daytime session (`hft_record --minutes 60` at 14-16 UTC) to re-test the Polymarket lead when the DMM is active;
   the evening book was too static to bound a sub-minute response.

## Sources
Budish-Cramton-Shim 2015 QJE https://conference.nber.org/conf_papers/f69665.pdf (median ES-SPY arb life 97 ms in 2005, 7 ms in 2011, profits flat) ·
Aquilina-Budish-O'Neill 2022 https://www.bis.org/publ/work955.pdf (races ~20% of volume, mean 79 us) ·
Foucault-Kozhan-Tham 2017 RFS https://wrap.warwick.ac.uk/83159 (abstract) ·
Polymarket 15-min taker fees https://unchainedcrypto.com/polymarket-introduces-taker-fees-in-15-minute-markets/ ;
practitioner bot https://dev.to/lkto1m/february-2026-changed-polymarket-forever-heres-what-happened-to-my-bots-numbers-2fi5 ·
Ng-Peng-Tao-Zhou SSRN 5331995 via https://quantpedia.com/systematic-edges-in-prediction-markets/ ·
Moallemi-Yuan https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf ·
Albers et al. https://arxiv.org/abs/2502.18625 · Menkveld 2013 JFM https://repec.tinbergen.nl/tin/wpaper/11076.rdf ·
Brogaard-Hendershott-Riordan 2014 RFS · Saguillo et al. https://arxiv.org/abs/2508.03474 ·
Polymarket CLOB market WebSocket wss://ws-subscriptions-clob.polymarket.com/ws/market (docs.polymarket.com) ·
Sibling notes: bot_exploits.md, mm_upgrade.md, volume_target.md, high_upside.md.
