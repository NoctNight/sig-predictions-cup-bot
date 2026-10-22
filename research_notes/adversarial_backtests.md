# Adversarial backtests: can a counter-bot farm us?

Paper only: no venue calls, no orders, no edits to bot code or config, the live process and `data/sigbot.sqlite` untouched
(statistics come from a copy). Simulator: `sigbot/adversary.py`
(`python3 -m sigbot.adversary --attacks all --cycles 200`, about 15 min). Code state reviewed: the files as of
2026-10-22 01:29, including the new `arb.min_ret = 0.04` floor and capital recycling (`recycle_enabled`), which landed
while this work was in progress.

## Bottom line

**No attack in this list makes the attacker money.** Every ladder attack (sweep-and-revert, inside pick-off, spoofing the
microprice, pushing the partisan cap) loses money to the venue's DMM ladder, and in most of them we come out ahead. Three
rules carry this: we never cross the touch, we improve only when the spread is at least 2 ticks, and the DMM is 2.4k to
170k shares deep. The real exposures are cheap **denial** and **logic** attacks:

1. **Recycle bait** (new code, worst money finding). A phantom arb the scanner saw but failed to execute makes the main loop
   unwind riskless held sets. Simulated: 13,500 sets unwound in 3 cycles, 135 SUSQies of settlement edge given away.
   The attacker pays about 10x that, so this is griefing, but it is a pure logic hole and costs nothing to close.
2. **Write-budget starvation** (free for the attacker). One large order flipped between bid and ask each cycle moves our
   microprice by at least `requote_ticks`. That forces 15+ cancel-and-replace cycles a cycle against a 14-write budget, and
   the markets skipped are always the same ones (fixed iteration order). Their quotes expire after the 15-minute TTL and
   never come back. Simulated: **16 of 25 markets blank** and 55% of market-time unquoted, at zero attacker cost.
3. **Single-bait pass abort**. `ArbEngine.execute` breaks on the first failed chunk, so one bait at the top of the plan
   aborts every arb in that pass, and the deterministic :x4.5 phase tells the attacker when to pull it. At `min_ret` 4% the
   bait needs about 25 ticks of room, which the attacker must buy from the DMM every pass, so this is uneconomic today.
4. **Unexpiring residual orders.** Arb legs carry no `expirationDate`, the `filled` flag is computed and ignored,
   `flatten_residuals` is unreachable while the scanner thread runs, and `hedge_residual` is false. Any partial fill (if
   allOrNone ever failed open) rests forever. The live DB already holds **20 open hedge orders, 842 shares, no expiry**.
   They include a contradictory pair on exchange 1033 (buy YES 0.38 + buy NO 0.63 = paying 1.01 for a set worth 1).

At current sizes the market-making sleeve earns about **0.5 SUSQies/hour** (29 fills, 970 shares, +0.1 tick 30-minute
markout, measured from the DB copy). So the worst a successful ladder denial can cost is that 0.5/hour. The money at stake
sits in the arb sets (riskless edge, about 28k shares held) and the tilt sleeve.

## Model

* **Venue**: one limit order book per exchange, price-time priority, self-trade prevention, IOC sweeps. The DMM ladder is
  the per-level quantities from `data/books.json` (12 levels; median touch about 2.4k, growing to 100k+ by level 3-5),
  refilled at the same prices at 50% of the missing size per 30 s. The quotes table shows the touch moves only 0.4x/hour
  and a depleted touch is back at the same price 91% of the time, with a median of 46% of its size. `--recenter` makes the
  DMM follow the last print (0.5 and 1.0 tested); `--dmm thin` uses a 500-share touch (the p10 of observed touches).
* **Us**: the real `MarketMaker.build_quotes` / `quote_cycle`, `TiltHarvester.cycle`, `ArbEngine.plan / execute / recycle`
  and `analytics.compute`. They run on `Universe.from_markets(data/markets.json)`, an in-memory `Store`, a fake API that
  routes orders into the paper book, and a patched clock. 25 MM markets are selected with the real `select_markets` on
  `volume_cache.json`, plus 8 tilt markets (the widest-spread tail markets with at least 4% edge). Bot cycle 30 s, config
  exactly as in `config.json`.
* **Attacker**: sees the whole book. Our very regular ladder sizes (about 44/65/86/108 shares at 1/2/3/5 ticks, the same
  every requote) make our orders trivially identifiable, so it also sees which orders are ours. It knows our 30 s cycle and
  our :x4.5 scanner phase.
* **Accounting**: cash plus positions marked at the DMM anchor mid (the fair value of a no-information world), so the only
  P&L is what moves between us, the attacker and the DMM. Arb attacks count complete sets at their settlement edge and
  missed genuine arbs at what the scanner would have earned alone. Tilt counts slippage against the unbaited touch.

## Hypotheses

Results are 200 cycles (about 3.3 h of simulated time) on the real ladders unless noted. "us/h" is our P&L per hour.
Negative means extracted from us.

### (a) Ladder sweep-and-revert: hit all four levels, let the DMM refill, trade back

* **Mechanism**: buy through our whole ask ladder (L1-L4 at +1/2/3/5 ticks), wait for our requote and the DMM refill,
  sell back through our bid ladder.
* **Observable**: our fixed ladder shape and sizes, and requote-after-fill.
* **Result**: the attacker loses **1.74M SUSQies** to the DMM (it must eat 816M DMM shares to reach our deep levels) and we
  make **+3,744** (+1,120/h): our deep levels buy the dip and sell the rally against a ladder that refills where it was.
  With a print-following DMM (recenter 0.5 and 1.0) the attacker still loses (-82k, -59k) and we stay positive (+119,
  +100). Thin ladder: attacker -1.89M, us +3.6k.
* **Verdict: blocked** by DMM depth plus the never-cross rule. The "cheap" variant is (a'): take only our quotes that sit
  alone inside a 2-tick spread. Only 2 of 25 quoted markets have one, and those fills are at mid, so the attacker gets
  -3.8 and we get 0.
* **Residual concern**: a sweep that carries real information is ordinary adverse selection, not an attack. That is what
  the analytics' toxic-shape switch is for.

### (b) Spoof the touch to move our microprice (fair = microprice)

* **Mechanism**: post 3x the resting touch size behind the DMM on one side. That flips the imbalance to about 0.86 and moves
  our fair by 0.7-0.9 ticks; trade against the shifted ladder, flip, repeat.
* **Observable**: fair = microprice of the top level only, uncapped imbalance, `requote_ticks` 0.5.
* **Result**: 3,600 requotes forced, 20k of our shares traded, **P&L 0 for both sides**. Our prices are clipped at the touch
  (`min(..., bb + improve, ba - TICK)`), so on one-tick markets a shifted fair cannot move our L1 past the DMM. On 2-tick
  markets the improved level is the mid tick, which is worth 0 at mid.
* **Data check**: in our quotes table the micro-mid offset has corr 0.18 with the next 5-minute mid change. Its predictive
  value is about **0.05 ticks** when |micro - mid| > 0.25 ticks (n = 1,100), against a spoofable shift of 0.7-0.9 ticks.
  The top-of-book imbalance carries almost no information here and is free to manipulate.
* **Verdict: blocked for extraction; open as a denial lever.** Spoofing is the engine of (f) below.

### (c) Bait the tilt sleeve

* **Mechanism**: on a wide-spread tail market, post a 1-share order one tick inside the touch. The tilt sleeve sees the
  "touch moved", cancels and re-posts its 3 levels at the new touch, +1 and +2 ticks. The attacker pulls the bait and
  sells into our improved levels, keeping the position it wanted anyway.
* **Observable**: tilt prices come from the touch only (no size floor), requote-on-touch-move, 1-2 ticks inside.
* **Result**: 1,600 baits, 8,644 shares at a worse price, **-43 SUSQies (-13/h)**. The attacker is down 30 at mid. The loss
  stops quickly because the per-market cap (1% of starting cash) fills. Worst case is about 1-2 ticks times the cap per
  market (about 5-10 per market, under 300 across 30 tilt markets), and it reduces the 4%+ edge rather than flipping it.
* **Verdict: open but bounded.** Mitigation M5 (ignore touch levels thinner than 100 shares) gives 0.

### (d) Poke the volatility gate, the wave fade and the kill switch

* **Gate**: move the mid 3 ticks with prints, hold it through one of our cycles, sell back. We pull the adding side and
  shrink and widen for 30 min.
  * Cost per trigger on the three cheapest markets: **326 SUSQies** on real ladders, 102 thin, 1,768 with a
    print-following DMM.
  * Result: 132 hot market-cycles; our resting size fell at most 5%; **our P&L +460** (the attacker's prints are
    mean-reverting flow we trade with).
  * A **wave** (15% of 25 markets = 4 hot at once) costs about 1.3k per 30 minutes and cuts every quote to 25% size and
    2 ticks wider. Pure griefing.
* **Kill switch**: the cheapest way to print a 400-SUSQie marked drawdown against our current positions costs about
  **4.6k SUSQies** across 8 markets (priced from the real ladders: 1073, 880 and 1075 are the cheapest at 3-9 SUSQies of
  cost per SUSQie of mark). The markout route needs 20 fills held 1 tick against us at the 30-minute sample. A kill costs
  us 30 min of MM and tilt income, which is a few SUSQies.
* **Verdict: blocked economically.** Mitigation M9 (gate needs the move on 2 consecutive cycles) took hot cycles from 132 to
  0 in the sim but delays a genuine gate by 30 s; optional.

### (e) Arb baiting: bids summing above 1, one pulled as our allOrNone order lands

* **Mechanism**: rest bids on both legs of a race so that sum(bids) - 1 clears `min_edge_sell` (and now `min_ret` 4%). It
  ranks first in the scanner's 3 book slots, so pull one leg just before our multi-leg arrives.
* **What allOrNone protects**: price and atomicity. In the sim the venue rejects with `AllOrNoneNotFilled` and nothing
  trades. **Our loss from the bait itself is 0.**
* **What it does not protect**:
  1. **Pass abort.** Our plan sorts by return and `execute()` does `break` on the first failure, so one bait above a
     genuine arb kills the genuine one too. Genuine arbs injected at 6/hour (22 in 3.3 h, worth 1,959 SUSQies to the
     scanner alone) were **all missed** while the bait ran: **-588/h**.
  2. **Cost of the bait.** At `min_ret` 4% the bait needs about 25 ticks of room, which the attacker buys from the DMM
     every pass: about 22M SUSQies/h (72.7M over the run) to deny us 588/h. At the old 0.5% floor it is about 192k/h to
     deny 410/h, still irrational. Races with 3+ ticks of natural room on both legs would make it nearly free; the
     current snapshot has none.
* **502 / ORDER_STATUS_UNKNOWN**: `_request` already retries 502 five times (2-32 s backoff, up to about 62 s) with the
  same body, and `execute()` retries once more with the same idempotency key. That is safe on price (the limits are the
  original ones) and on duplication (same key). But the scanner can hold `trade_lock` for about 2 minutes on a flaky
  venue, and the eventual fill can be a minute stale.
* **Partial fills** (if allOrNone ever failed open; modelled with `arb_partial`, which fills legs one by one and rests the
  rest):
  * the attacker gets one leg filled (a 5,000-share one-sided position) and stops re-baiting that race;
  * our code logs `filled=False`, books `committed` as if complete, leaves the other leg's NO buy **resting with no
    expiry**, and never hedges.
  * Flattening at the touch on every partial is worse: the attacker re-baits every 5 s and we pay a spread on 5,000 shares
    each time (-27k in 30 min in the sim).
  * Flatten once plus a 10-minute blacklist still costs about **-375/h** (-1,246 over the run: one 62.5 hedge per
    blacklist expiry), against +242 for leaving the 5,000-share residual open (flat at mid, but directional risk).
  * So M8 marks the race hostile for the rest of the session after a partial fill: one hedge, then no re-entry.
* **Verdict: atomic case blocked; pass abort and partial-fill handling open.**

### (f) Timing: front-run the :x4.5 scanner and our requote-after-fill

* **Scanner**: the phase is public to anyone polling at 1 Hz (our book reads and multi-legs cluster at :04.5, :09.5, ...),
  which is what lets an (e) bait be pulled just in time. With a 0-2 s random phase (M7) the attacker's pull misses half the
  time, its bait gets filled, and it pays us the edge. In the sim it gave up after 2 fills: bait attempts 7,200 → 5,
  missed genuine-arb value 1,959 → 0, net +839 to us.
* **Requote-after-fill**: any fill on an exchange forces a full cancel-all plus re-post (`fills_on_ex > fills_seen`), even a
  1-share fill. Combined with the fixed iteration order and the 14-write MM budget this is the **write-budget starvation**:
  * `micro_flipflop_dos`: 0 attacker cost, 14.0 writes/cycle used, 2,991 budget skips, **16/25 markets blank**, 2,760 blank
    market-cycles out of 5,000.
  * The tilt sleeve gets only `max(2, budget - mm_writes)` writes, so it starves too.
  * This can also happen with no attacker on days with touch flicker across 25 markets.
* **Verdict: open.** M1 + M2 + M3 + M4 bring it to 0 blank markets, 33 skips and 1.9 writes/cycle.

### (g) Cash-admission rule and TTL expiries to leave us one-sided

* **Cash rule**: the MM budget is `min(0.2 x start, 0.9 x cash) / 25 markets` = 800 per market at today's 81k cash, against
  a ladder notional of about 175 per side. Sizes only start to shrink below about 9.7k cash, so an attacker cannot cheaply
  shrink our buys. The tilt sleeve counts resting NO collateral correctly (NO `priceLimit` x qty).
* **TTL**: both sides of a market share one TTL and are always replaced together by an exchange-scoped cancel-all, so TTL
  alone never leaves us one-sided. What does is (f): starved markets expire on both sides and go blank. One-sidedness
  comes only from our own rules (per-race hard stop, partisan block, hot gate). That is by design and checked in (h).
* **Verdict: blocked**, except through (f).

### (h) Partisan exposure cap gaming: push us to 5k so one side is blocked, then trade the other

* **Mechanism**: sell us D-YES (and R-NO) through our bids until `net_d_notional` ≥ 5,000, then lift our one-sided asks.
* **Result on real ladders**: the per-race cap (600 notional, hard stop) and inventory skew hold net D at **about 600-680**.
  The attacker cannot reach 5,000 without pushing about 9 races to their caps through 15k+ of DMM queue each. **Blocked 0
  cycles**; the attacker paid 166k to the DMM; we made +279.
* **Result on thin ladders**: blocked for 77 cycles, but the one-sided book offered only 208 shares to trade back inside.
* **Verdict: blocked.** The cap protects; it is not a lever. Note the tilt sleeve's longshot/favourite positions count
  toward the same cap and could trip it without any attacker.

### (i) Analytics poisoning (markout model widens or shrinks the ladder)

* **Mechanism**: take our inside quote, then park a 1-share order at our fill price so the 30-minute mid sample reads
  ±0.5 tick. With n ≥ 9 per market and |mean| > 0.3 tick the shape flips to calm or toxic, with `size_scale` 0.3-1.5.
* **Result**: not achievable in 200 cycles. Only 2 of 25 markets have inside quotes; the park and our requote alternate,
  and once the park rests the spread is 1 tick and nothing is left to feed. About 8 qualifying fills per market per
  30 min; shapes stayed default and `size_scale` ≈ 1.00.
* **Bounded by construction** even if it worked: ±50% size on targeted markets, 48 h lookback, and a sleeve earning about
  0.5/h. A side effect worth knowing: the quotes-table mid includes **our own** resting orders (on 2-tick markets our
  improving quote moves the recorded mid by half a tick), and that mid feeds both the markouts and the vol gate.
* **Verdict: blocked in practice.**

### (j) Other findings

1. **Recycle bait (new).** `ArbScanner.one_pass` stores `best_unfunded` from the books **before** executing, whether or
   not the execution then fails. For the next 120 s the main loop may unwind a held set (`ArbEngine.recycle`, allOrNone at
   the asks) to fund it.
   * Simulated: 3 unwinds, 13,500 sets, **-135 SUSQies** of riskless edge forgone in 1.5 min; the attacker paid 1,463.
   * With about 14k sets held live, the exposure is about one unwind spread per set (0.01-0.02/set, so 140-280 total). It
     renews every time the budget refills with sets.
   * M10 (only recycle for a chunk that is still there on a fresh read and whose own execution did not just fail) took it
     to 0.
2. **`execute()` break-on-first-failure** (see e): one failing chunk aborts the rest of the pass, including other races.
3. **Unexpiring orders**: arb legs and hedge orders carry no `expirationDate`; 20 such hedge orders (842 shares) are open
   in the live DB now. Stale limit orders fill only when the market moves through them, which is adverse by definition.
4. **Mean-reversion skew**: it is computed from 5-minute bars of live mids, capped at 1 tick, and clipped at the touch like
   everything else. A d1 tick moves it about -0.2 ticks, so leaning it fully needs about a 5-tick bar move: the cost of (d).
   Not separately simulated (the sim's `meanrev_pred` is 0).
5. `MarketMaker.hot_until` / `hot_anchor` are class attributes shared by every instance (harmless with one bot per
   process; the sim isolates them).

## Ranked vulnerabilities (worst first)

Losses at current sizes (25 MM markets, base_qty 100, 4 levels; tilt 12% of cash; arb 25%).

| # | vulnerability | attacker cost | our simulated loss | status |
|---|---|---|---|---|
| 1 | Recycle bait unwinds riskless sets for a phantom arb | ≈1.5k per sweep | -135 per 13.5k sets in 1.5 min (≈ -5.4k/h until held sets are gone; bounded ≈140-280 per refill) | open |
| 2 | Write-budget starvation via microprice flip-flop + requote-on-any-fill | 0 | 16/25 markets blank, 55% of MM time unquoted; tilt writes starved. Money: the MM sleeve's ≈0.5/h | open |
| 3 | Unexpiring residual / hedge orders; `filled` ignored; no hedger while the scanner runs | n/a | live: 842 shares stale, open-ended; a hypothetical partial fill leaves 5k shares one-sided | open (live now) |
| 4 | Single bait aborts the whole arb pass (break + deterministic :x4.5) | ≈22M/h at min_ret 4% (≈192k/h at 0.5%) | -588/h of genuine arbs while it runs (-410/h at 0.5%) | open, uneconomic today |
| 5 | Tilt bait on wide tail markets | ≈30 | -13/h, capped ≈5-10 per market | open, bounded |
| 6 | Vol gate / wave / kill switch poking | 326 per gate, ≈1.3k per wave, ≈4.6k per kill | forgone MM income only; P&L positive in sim | griefing only |
| 7 | Sweep-revert, inside pick-off, spoof extraction, partisan cap push, analytics poisoning | ≥ 100x our loss | 0 or positive | blocked |

## Mitigations, ranked by simulated effect

| rank | mitigation | change | simulated effect |
|---|---|---|---|
| M10 | **Verify before recycling** | in `run.py`, only call `arb.recycle` when the scanner's last execution of `best_unfunded` did not fail and a fresh book read still shows the chunk at ≥ its return | recycle bait: 3 unwinds / -135 → **0 / 0** |
| M1 | **Rotate requote order / serve starved markets first** | in `quote_cycle`, iterate `self.quoted` starting at `cycle_id % n`, or sort by "orders missing" then "oldest last_quote_ts" | with M2-M4: blank markets **16 → 0**, budget skips 2,991 → 33 |
| M2 | **Fill-size threshold for requotes** | `needs_requote`: count only fills ≥ 10 shares (or ≥ 20% of the level) | part of the DoS fix; removes the 1-share requote lever |
| M3 | **Requote hysteresis** | `requote_ticks` 0.5 → 1.0 | spoof requotes 3,600 → 550 |
| M4 | **Robust microprice** | imbalance from the first level with ≥ 100 shares, each side capped at 3,000 shares, micro clipped to the touch | spoof shift 0.7-0.9 → <0.2 ticks; costs ≈0.05 tick of signal (corr 0.18) |
| M8 | **Partial-fill rule for multi-leg** | after `place_multi_leg`, if any leg's `quantityTraded < qty`: cancel that race's remaining legs, mark the race hostile for the session, flatten the residual once with a limit at the touch; add `expirationDate` (e.g. 60 s) to every arb and hedge leg; run the residual check also when the scanner thread is on | partial fill: open-ended 5k one-sided → one hedge (≈60-190). A 10-min blacklist instead of session-long costs ≈ -375/h |
| M7 | **Scanner jitter + bait blacklist + no break** | random 0-2 s phase per pass; skip a race for 10 min after `AllOrNoneNotFilled`; in `execute()` `continue` to the next race instead of `break` | bait attempts 7,200 → 5, missed genuine-arb value 1,959 → 0 (-588/h → +209/h), filled baits pay us |
| M5 | **Tilt touch floor** | tilt reads the touch as the first level with ≥ 100 shares | tilt bait -43 → 0 |
| M9 | Gate persistence (optional) | hot only if the 3-tick move holds on 2 consecutive cycles | hot cycles 132 → 0; delays real gating by 30 s |
| ops | Cancel the 20 stale no-expiry hedge orders | manual, by you (not done here: no venue calls) | removes 842 shares of open-ended exposure |

Considered and **not** recommended: capping the inside L1 size (M6). It cut what the cap-push attacker could sell us from
41.9k to 6.3k shares, but those fills were profitable for us. It only makes sense if the analytics show the inside fills
are toxic.

## Still open after the mitigations

* **Information-carrying sweeps.** A trader who knows the news before the DMM reprices will hit our deep levels. That is
  adverse selection, handled by the markout analytics and the news pause, not by anti-gaming rules.
* **Griefing that the attacker pays for**: waves (≈1.3k per 30 min) and kill-switch triggers (≈4.6k). They cost us only MM
  downtime; raising `kill_drawdown`, or computing it on mid marks instead of last trade, would raise the price further.
* **Arb denial in races with natural room**: if a race ever shows 3+ ticks of spread on every leg, a bait becomes cheap.
  M7's blacklist limits it to one attempt per 10 min per race.
* **Unverifiable venue behaviour**: real allOrNone atomicity, the refill speed and recentering rule of the DMM, and queue
  priority on refill are assumptions. `--recenter` and `--dmm thin` cover the range tested; the ranking did not change.

## Reproduce

```bash
python3 -m sigbot.adversary --attacks all --cycles 200                      # real ladders, baseline vs mitigated
python3 -m sigbot.adversary --attacks sweep,gate_poke --cycles 100 --recenter 1.0 --mitigations none
python3 -m sigbot.adversary --attacks all --cycles 100 --dmm thin
python3 -m sigbot.adversary --attacks recycle,flipflop --cycles 50 --json /tmp/out.json
```

Attack names: `sweep pickoff spoof flipflop tilt_bait gate_poke cap_push poison poison_toxic arb_bait arb_partial recycle
arb_bait_lowret`. Each runs once as a baseline and once with only the mitigations meant for it (`MITIG_FOR` in the module).
