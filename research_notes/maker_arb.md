# Resting orders for the high-frequency arb (research, 24 Oct 2026)

Read-only study. Nothing under `sigbot/`, `config.json`, `params/`, `tests/` or `deploy/` was touched; no orders; **0 venue
reads**. Script `research_notes/maker_arb_study.py`: stages 0-2 take about 100 s, `--stage3` about 75 s, and `--figures`
redraws. Tables are in `data/research_maker_arb/tables.md` and the figure is
`research_notes/figures/maker_arb_decision.png`.

**Question.** The high-frequency arb crosses the spread twice. It buys NO on both legs, which means selling YES at the
bids, and sells the set back at the asks. That costs about 2 ticks per set each way. Could it rest orders at the touch
instead and earn the spread?

## Bottom line

* **A resting fill is better priced, but fills are rare. Verdict: drop.** It would add about +25 to +65 S/day,
  roughly +0.2 to +0.5k to the 1 Nov entry cut-off, with no significant test-day evidence.
* **Each resting fill is worth about 0.7-0.9 tick per share more than crossing.** Over every 2-leg race leg every
  15 min, a 1,000-share resting YES offer that fills earns +0.31 tick against the mid 30 min later (t 10.5). Crossing
  the same side earns -0.60. In wide-gap moments (≥ +4) the figures are +0.19 vs -0.51.
* **Waiting behind the queue is slow.** The median touch has about 2k shares in front of a new order, and with a
  1-tick spread there is no way to step ahead. A joined 1,000-share order fills completely 2% of the time within 5 min,
  6% within 30 min, 11% within 1 h and 27% within 4 h. The model matches our own level-1 MM orders: it predicted 44k
  filled shares against 38k actual (0.86).
* **Leg-in removes most of the one-leg risk, but on too few signals.** In leg-in, each leg rests an offer, and when one
  fills the other leg is sold at once. On the arb's 34 signals (24 on 22 Oct, 10 on 23 Oct) it got a resting fill on
  18-41% of signals (30-240 min wait), averaging 9-38% of a 2,000-set clip.
  * Those sets earned +0.4 to +1.1 tick more than the taker entry on the same signals, but t ≤ 1.3.
  * The test day has only 1-6 filled signals.
* **Resting both legs and waiting for both fails.** It completes a set on only 7 of about 700 attempts.
* **Resting exits add nothing.** Taker entry with a resting-bid exit adds +0.04 tick per set (t 0.5). The versions that
  rest and then fall back to crossing lose -0.7 to -1.25 per set, because the earlier trigger then crosses at worse
  prices.
* **It competes with our own market maker.** The bot's MM already quotes the touch on 136 legs, in the same queue. A
  resting sleeve order would mostly take fills the MM would otherwise get, and it adds a self-trade risk against the
  sleeve's own crossing leg.

## Data and model

* **Prices:** the real touch from `venue_quotes` (SIG, about 60-120 s polls) on a 5-min grid, 21 Oct 23:50 to
  23 Oct 22:50 UTC, 114 two-leg races.
* **Deviation:** the mid overround minus its trailing 24-h median. The median uses the 14-21 Oct proxy panel as a seed,
  so signals start at 00:00 on 22 Oct.
* **Tape:** 79k public prints with the aggressor side, `tape` from 21 Oct 23:00. 1,433 of our own taker prints (arb,
  exit, direction, hedge) are removed. Prints that filled our resting MM, tilt or inside orders are kept, because those
  were other traders taking liquidity.
* **Queue model:** price-time priority.
  * A new order joins behind the touch size (`quotes` table, ≤ 3 min old, else the leg's median, else the global median
    of 1,955).
  * It fills from aggressor prints at its price after that queue is used up. A print through its price fills it in
    full.
  * Prints with an unknown aggressor are ignored.
* **Calibration:** checked on 11,546 of our own level-1 MM orders on these legs. The model predicted 44,217 filled
  shares against 37,933 actual, and 2.2% vs 2.6% of orders with any fill. It is slightly optimistic, so the results
  below are, if anything, too kind to resting orders.
* **Costs:** 0.1 tick of slippage per crossed contract. 20% basket rejections on crossed entries. Resting legs are
  single orders and are not rejected.

## 1. Fill rates and markouts (1,000 shares joining the touch, every 2-leg race leg, every 15 min)

| order | when | n | full fill within 5 / 15 / 30 / 60 / 240 min | realised edge vs mid 30 min after the fill (t) | crossing instead |
|---|---|---|---|---|---|
| YES offer at the ask (NO entry) | all times | 37,848 | 2 / 4 / 6 / 11 / 27% | +0.31 (t 10.5) | -0.60 |
| YES offer at the ask | gap ≥ +4 | 1,092 | 2 / 4 / 6 / 10 / 26% | +0.19 (t 1.0) | -0.51 |
| YES bid at the bid (NO exit) | all times | 37,848 | 2 / 4 / 7 / 11 / 27% | +0.21 (t 7.0) | -0.60 |
| YES bid at the bid | gap ≥ +4 | 1,092 | 5 / 9 / 13 / 20 / 41% | +0.02 (t 0.1) | -0.72 |

There is no adverse-selection trap: resting fills are not followed by the price running away from us. The limit is
volume. During wide-gap moments, buyers lift 160-264k shares a day at the ask on both legs combined. That would be
plenty at the front of the queue, but we join at the back.

## 2. Leg-in vs today's rule, same signals (2,000-set clip)

| panel | signals | today's rule per set (t) | wait W | signals with a resting fill | mean rested size | leg-in per set | gain vs today on the same signals (t) | extra S/day on top of today's rule |
|---|---|---|---|---|---|---|---|---|
| training (22 Oct) | 24 | +0.23 (+0.4) | 15 min | 12% | 12% | +1.03 | +0.43 (+1.3) | +31 |
| training | 24 | | 30 min | 21% | 21% | +1.30 | +1.10 (+0.8) | +65 |
| training | 24 | | 60 min | 25% | 25% | +0.87 | +1.10 (+1.0) | +52 |
| training | 24 | | 240 min | 33% | 29% | +0.58 | +0.60 (+0.7) | +39 |
| test (23 Oct) | 10 | +0.20 (+0.3) | 30 min | 10% | 10% | +2.70 (n 1) | +0.10 | +28 |
| test | 10 | | 240 min | 60% | 60% | +0.37 (n 6) | +0.27 (+0.6) | +23 |

Today's rule alone made +43 S/day on training and +17 on test at 2,000 sets per signal. Leg-in would roughly double the
sleeve's daily take from a small base. The sleeve's real clip sizes and repeated clips are larger, so scale both
numbers together. Even at 4x, the extra is about +0.1 to +0.25k/day at best, and it is not significant.

## Verdict

**Drop. Keep crossing the spread for the high-frequency arb.** Resting orders do get better prices, about 0.7-0.9 tick
per share per filled leg, and the queue model is validated on our own MM orders. But behind a 2k-share queue with a
1-tick spread, fills come too slowly:

* the added value is about +0.2 to +0.5k by 1 Nov, with t ≤ 1.3 on the same signals;
* building it means a new order type, leg-in logic, failure tests and a Fable review;
* resting orders would partly take fills from our own MM in the same queue.

The cheap version of this edge is already captured by the MM quoting those touches.

## Limitations

* Only 34 signals over 2 days, with 1-6 filled signals on the test day.
* The queue model ignores DMM cancel-and-repost behaviour. If the DMM requotes and loses its priority, a resting order
  would fill faster than modelled, but the calibration on our own orders says the model is already slightly
  optimistic.
* Completion of the other leg is priced at the next 5-min grid point, not within seconds. That adds noise in both
  directions.
