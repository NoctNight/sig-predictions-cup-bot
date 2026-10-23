# Election-night capital plan (backlog item 3, daily research run 2026-10-23)

This is a plan for Sizhe to act on when he returns on 3 Nov. It is not a trade. The election-night sleeve stays OFF until
Sizhe switches it on, and the late punt is his call. Nothing under `sigbot/` or `config.json` was changed, and no venue
calls were made. Everything here comes from local data.

## 0. Bottom line

* **Unwinding an arb set before settlement costs about as much as the profit it has locked in.** Rhode Island Governor
  (RI Gov) holds 88% of the arb capital. It is a 3-leg set, and over the last 8 days it has never been sellable within 2% of
  payout. Its usual exit cost is 3.75-4.75% of payout. Its locked profit is 4.04% of payout, so selling it back is roughly
  selling at cost.
* **Arb unwinds can raise liquid cash to about 52k at most** (19.9k cash now, plus about 32k of proceeds).
  * **30k** costs about 0.4k.
  * **50k** costs about 1.2-1.6k, which means unwinding nearly all of RI Gov.
  * **70k** also needs about 18k from the market-maker (MM), tilt and directional books, for a total cost of about 2.5-3.5k.
* **Waiting costs nothing.** The overround (the amount by which a race's leg prices sum above 1) has been stationary. So the
  plan makes no unwinds before Sizhe decides on 3 Nov. On that day it unwinds patiently from his go-ahead until 17:45 ET,
  with a cap on the cost that loosens over the afternoon.
* **The unwinds pay off only if the sleeve is switched on and works.** The sleeve's simulated central return is about 0.19
  per SUSQie (see `election_night.md`). Unwinding breaks even if P(sleeve on and the dealing market maker (DMM) lags as
  simulated) is about 21% (X = 30k), 25% (50k) or 32% (70k).

## 1. Data and method

| source | what it gives | coverage |
|---|---|---|
| `venue_quotes` (venue='sig'), sqlite | touch bid/ask per leg for every race; no sizes (ask_sz is empty for SIG) | 21 Oct 23:49 to 23 Oct 10:17 UTC, about 230 polls per leg |
| `data/candles_5m.json` | 5-min last-trade closes per leg | 14 Oct 03:40 to 21 Oct 22:18 UTC (1,000 candles) |
| `data/books.json` | full depth (200 levels) for every leg | one snapshot, 21 Oct 22:03 UTC |
| `data/feed_capture_20261022.jsonl` | 6-level books from the realtime feed | 22 Oct, about 25 min, 470 joint RI Gov states |
| `arb_trades` (kind unwind_set, ok=1) | realised unwinds: 117 baskets, 126,951 sets, 135k proceeds | 22-23 Oct |
| `data/live.log` "OFFLOAD candidate ... gives up x%" | the bot's own walked exit cost for the full held size | 23 Oct 00:33-11:19, every few minutes |

**Unwind cost.** Unwinding a sell-set (NO on every leg) means buying YES on every leg at the asks. Proceeds per set are
`n - sum(asks)` and the payout is `n - 1`. The forgone profit is therefore `sum(asks) - 1` per set. I quote it as a fraction
of payout: `(sum(asks) - 1) / (n - 1)`. This is the same as `Arb.unwind_quote`.

**Holdings.** These are the review tool's figures at 11:11 BST: 8 races, cost 32.2k, locked 1,293.

* RI Gov: 14,793 sets, cost 28,392, locked 1,194.
* Montana Senate: 1,249 sets, 3 legs, cost about 2.4k.
* CO-04: 261 sets.
* Five small 2-leg sets, about 1.2k in total.

The sqlite ledger is not used for holdings because some races net out negative in it.

## 2. (a) How much can be freed, and at what cost

### Rhode Island Governor (3 legs: D 1073, R 1074, I 880; payout 2/set; locked profit = 4.04% of payout)

| measure | min | p10 | p50 | p90 |
|---|---|---|---|---|
| touch unwind cost, % of payout (129 joint polls, 21-23 Oct) | 3.50 | 3.75 | **4.25** | 4.75 |
| mid-overround from 5-min closes, % of payout (1,353 points, 14-21 Oct) | | 3.00 | 4.25 | 7.50 |
| walked from the 22 Oct feed: 2,000 sets | 4.50 | 4.50 | 4.73 | 4.76 |
| walked: 5,000 sets | 4.75 | 4.75 | 4.86 | 4.90 |
| walked: all 14,793 at once | 5.25 | 5.25 | **5.32** | 5.39 |
| full-depth snapshot 21 Oct 22:03 (touch / 14,793 / 30,000) | | | 6.49 / 7.27 / 7.53 | |
| realised RI unwinds 23 Oct 03:21 (1,411 sets) | proceeds 1.914-1.920/set = **4.0-4.3%** | | | |
| bot's OFFLOAD estimate for the whole holding (87 readings, 23 Oct) | 2.86 | | 3.39 | 3.94 (forgone return on proceeds) |

**Share of the 14,793 RI Gov sets sellable within a given cost, before election night (observed):**

* **Within 0.5%, 1% or 2% of payout: none.** No poll and no 5-min point since 14 Oct got below 2%. The touch minimum was 3.5%.
* **Within 3.5%:** only touch-sized clips at the best moments, perhaps a few thousand sets in total. Touch sizes are not
  logged, so this is a guess.
* **Within 4.0%** (the locked profit, so no loss against cost): plausibly most of the holding. It needs patient chunks of
  1-2k sets at the touch, waiting for below-median moments (about p10-p40). The DMM refills a swept level in 3.7 s
  (`bot_exploits.md`).
* **Within 4.5%:** almost all of it, with chunking over a few hours.
* **Within 5.5%:** all of it, immediately (6-level sweep).

**Why RI Gov is structurally expensive.** The DMM compresses prices toward 0.5 (the kappa model in `election_night.md`).
With kappa about 0.8, a 3-leg race with a near-certain winner has mids that sum to `1.5 - 0.5*kappa = 1.10`. That is a 5%
overround per set. RI Gov fits this: D 0.885 against an outside price of 0.988, R about 0.11, I about 0.105, summing to about
1.085-1.10. Over 2-legged races the compression cancels (0.9 + 0.1 = 1). So 3-leg sets (RI Gov, Montana Senate, Nebraska
Senate) are overround by construction. This explains why:

* the bot keeps finding RI sell-set arbs at about 3.5% entry edge;
* RI cannot be exited cheaply;
* a post-call unwind on election night will probably be no cheaper unless the DMM drops its compression when a race is
  called (with kappa 0.8 the losing legs stay near 0.10).

Across all 3-leg race polls, only 2.7% had a touch unwind cost of 2% or less; across 2-leg race polls it was 70%.

### Montana Senate (3 legs, 1,249 sets) and the 2-leg sets

| race | touch unwind cost, % of payout, p10 / p50 / p90 | realised |
|---|---|---|
| Montana Senate | 2.75 / 3.50 / 4.25 | 2.9-3.1% (500-set clips); 4.0% (5,996 sets in one go, 22 Oct 21:49) |
| CO-04 | 2.00 / 2.50 / 3.50 | |
| Alaska Senate | 2.50 / 3.00 / 4.00 | 3.1% |
| WI-01, Texas Senate, MI-10 (2-leg, for comparison) | 0.5 / 2.5 / 3.0; -0.5 / 2.0 / 3.5; 0.5 / 1.0 / 2.5 | |
| all 117 realised unwinds, capital-weighted | mean 2.98% of payout; 2.2% of proceeds unwound at 1.5% or less, 32% at 2.5% or less, 90% at 4% or less | |

The held 2-leg sets were bought because their bids summed above 1, so they are selected for persistent overround. Across
all 114 two-leg races the touch unwind cost was 1% or less in 42% of polls, but the held ones sit at about 2.5-3%.

**Non-RI total.** About 3.8k of cost, at about 2.5-3.5% of payout, so roughly 120 forgone (range 100-150).

## 3. (b) The value of holding to settlement

| bucket | capital (cost) | locked profit | locked, % of payout | cost to unwind now (p10 - p50 - full sweep) | unwind cost as a share of locked profit |
|---|---|---|---|---|---|
| RI Gov | 28,392 | 1,194 | 4.04% | 1,110 - 1,257 - 1,568 | 93% - 105% - 131% |
| everything else | about 3.8k | about 99 | about 2.6% | about 100 - 120 - 150 | about 100-150% |
| **total** | **32.2k** | **1,293** | | **about 1.2k - 1.4k - 1.7k** | |

Holding earns the locked profit of about 1.3k at settlement on 4 Nov 12:00 ET. That is after the sleeve window, so held set
cash cannot fund election night. Unwinding gives up nearly all of the 1.3k: freeing arb capital costs about 4-5% of the
capital freed (RI Gov) or 2.5-3.5% (2-leg sets).

A related finding: `allocator.py` assumes a 1% unwind cost by `EXIT_DEADLINE`. The measured cost is 2.5-3% for 2-leg sets
and 4-5% for 3-leg sets. If every set really had to come back to cash, the allocator overstates the arb rate. Also,
`EXIT_DEADLINE` 2026-11-03T04:00Z is 23:00 ET on 2 Nov, not "Nov 3 00:00 ET" as the comment says (EST from 1 Nov).

## 4. (c) and (d): what each X costs

Liquid cash on 3 Nov 18:00 ET starts from about 19.9k. `arb.cash_reserve` is already 20,000, so arbs never take cash below
that. Tilt and directional trades do not obey this reserve. The costs below are forgone settlement profit plus slippage
against holding everything.

| X (cash at 18:00 ET) | extra to free | where it comes from | expected cost (range) | sleeve EV on the extra cash, central (no lag / pull / fast) | net, central | breakeven P(sleeve on and works) |
|---|---|---|---|---|---|---|
| 20k (status quo) | 0 | | 0 | | | |
| **30k** | 10.1k | the small sets (3.7k proceeds) + 3,340 RI sets (23% of RI) | **about 0.4k** (0.35-0.5k) | +1.9k (0 / 1.1k / 3.1k) | +1.5k | about 21% |
| **50k** | 30.1k | all small sets + about 13,750 RI sets (93%) | **about 1.4k** (1.2-1.6k) | +5.7k (0 / 3.3k / 9.3k) | +4.3k | about 25% |
| **70k** | 50k | all arb (about 32k proceeds, max total about 52k) + cancel MM resting orders (about 6-8k, about 0 cost) + sell about 10-12k of tilt/directional holdings | **about 3k** (arb 1.4-1.7k + MM about 0.1k + tilt/dir 0.6-1.2k forgone edge and spread) | +9.5k (0 / 5.5k / 15.5k) | +6.5k | about 32% |

* **Sleeve EV per SUSQie** comes from `election_night.md`: +17k central on 90k (0.19), `pull` 10.2k (0.11), `fast_k1`
  28k (0.31), and 0 if the DMM does not lag. Cash binds and the sleeve funds the best races first, so the marginal return
  falls as X grows. The 70k row is the most optimistic.
* **The sleeve does not need all the cash at 18:00.** 75% of its simulated P&L comes from races called after the close,
  which happens hours into the night. Even so, freeing the cash by 18:00 is the simple, safe target.
* **The punt competes for the same cash.** That is Sizhe's decision (`election_night.md` section 5).

## 5. Schedule

**Principle.** Overround has been stationary for 8 days, so unwinding early does not get better prices. Unwinding early
only gives up locked profit that would be kept if Sizhe leaves the sleeve off. So: no unwinds before Sizhe's decision.
Prepare instead, then execute patiently on 3 Nov. All times are ET; the UK is ET+5 after 1 Nov.

**Now to 2 Nov: no unwinds, no trading changes from this note.**

1. Keep `arb.cash_reserve` at 20k, as it is now.
2. Optional, decision D3: stop moving capital into 3-leg sets after 31 Oct 00:00 ET, either by capping RI Gov at its
   current size or by having RECYCLE skip 3-leg targets.
   * Today RECYCLE sells 2-leg sets at about 2.5% to buy RI Gov at about 3.6% (+1% gain).
   * Each SUSQie moved that way costs about 1.5-2 points more to exit later.
   * It is worth it only if P(X >= 50k) is below about 60%.
3. **2 Nov evening: read-only checkpoint.** Run `grep "OFFLOAD candidate" data/live.log | grep "Rhode Island" | tail`. It
   gives the bot's walked full-size exit cost.
   * If RI is above 5% of payout and rising (the DMM widening before the event), the 50k and 70k costs rise by about
     0.3k per extra point.
   * Re-derive holdings with the review tool, because the bot rotates sets daily (73.6k sets unwound on 22 Oct alone).

**3 Nov, when Sizhe decides (by 12:00 ET at the latest, to allow 6 h of patient unwinding):**

| step | time (ET) | action |
|---|---|---|
| 0 | at the decision | **Before any unwind**, set the cash floor to X across every sleeve: `arb.cash_reserve = X`, plus tilt, directional and MM held off below X. Otherwise the scanner, tilt or directional sleeve re-spends the freed cash. Only arb obeys the reserve today, so this needs a small change that passes the gates (D4). |
| 1 | decision to 15:00 | Patient chunks of 2,000 sets or fewer, at the touch, in this order: 2-leg sets (cap 2.5% of payout), then Montana Senate (3.25%), then RI Gov (**4.0%**, the locked profit, so no loss against cost). |
| 2 | 15:00-17:00 | Loosen the caps to 2-leg 3.0%, Montana 3.75%, RI 4.5%. |
| 3 | 17:00-17:45 | Sweep whatever is still needed for X, with a cap of 5.5% of payout. If RI is above 6%, stop and accept a smaller X; the extra cost is not worth it at the margin. |
| 4 | 17:45 | Check cash against X. First polls close at 18:00 (parts of IN and KY) and the main wave at 19:00. The sleeve's first entries are calls after a state's last poll close. |
| 5 | election night (D5) | After a race whose sets we hold is called, unwind it if the cost is 1% of payout or less, and give the cash to later calls. 2-leg sets should get cheap after a call because the compression is symmetric. Do not expect RI Gov (calls at 20:00) to get cheap unless the DMM stops compressing; the kappa model says about 5%. |

**Tooling gap.** There is no operator command to unwind N sets of race R with a cost cap and chunking. `Arb.unwind_set`
exists, but only RECYCLE and OFFLOAD call it. Steps 1-3 need a small "capital raise" routine: target X, per-race caps by
time, chunk size and a hard stop time. It would be built OFF or dry-run by default, and pass the failure-scenario test,
the botctl precheck gate and a Fable review before 3 Nov. Building it is safe; running it is Sizhe's decision.

## 6. Decisions for Sizhe (yes/no)

* **D1. Switch the election-night sleeve on?** This is the hard rule.
  * If no: X stays at about 20k, there are no unwinds, and every set is held to settlement (+1.3k).
* **D2. If yes, which X?**
  * My recommendation is **50k**: almost all arb freed for about 1.4k, about +4.3k net central, breakeven at about 25%
    confidence.
  * 30k if lukewarm (about 0.4k).
  * 70k only with high conviction in the DMM-lag result. It also liquidates the tilt and directional books (about 3k cost),
    and its marginal sleeve return is the most uncertain.
* **D3. Stop recycling into 3-leg sets (cap RI Gov) from 31 Oct?** Yes if you expect to choose 50k or more.
* **D4. Approve a cash floor of X across all sleeves and the capital-raise unwinder** (built off; enabled on 3 Nov only)?
* **D5. Allow post-call unwinds of held sets on election night** (cost cap 1%) to recycle cash into later calls?
* **D6. Punt or sleeve:** the same cash cannot do both.

## 7. Caveats: what the data cannot tell

* **Depth is thinly measured.** SIG touch sizes are not logged. Depth comes from one full snapshot (21 Oct) and 25 minutes
  of 6-level books (22 Oct). The chunked costs assume the DMM keeps refilling at the touch as it did then (3.7 s).
* **The history is short and quiet.** It covers 8 days of candles and about 1.5 days of joint quotes. Nothing shows how the
  DMM behaves on 3 Nov itself; it may widen or pull quotes before the event, which would raise every cost above.
* **The 5-min data are last trades, not quotes.** The trade-based overround is a proxy for the quote-based one; the two
  agree on RI Gov (p50 4.25% both ways).
* **The sleeve EVs are simulated, not measured.** They extrapolate a lag kernel fitted to 1.5-point pre-election moves.
  With no lag the sleeve earns 0, and every unwind is a pure loss of up to 1.4k (50k) or 3k (70k).
* **The 70k row is the roughest.** The tilt and directional exit costs are estimates (spread plus giving up a 4-10 point
  tilt edge), not measured. "Cancel MM resting orders" assumes most MM collateral is resting orders, as the allocator's
  cap-days split suggests.
* **Holdings will change by 3 Nov.** Redo the arithmetic with the rule of thumb: 3-leg sets cost about 4-5.3% of payout to
  exit, 2-leg sets about 2.5-3%.
