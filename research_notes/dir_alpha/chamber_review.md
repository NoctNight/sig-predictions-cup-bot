# Chamber pair review: long House D, short Senate D (31 Oct 2026, data to about 05:00 UTC)

Independent read-only check. No orders, no bot files changed, no `.sig_api_key`, no calls to SIG `api/v1`. Inputs were the local
DB read-only, saved books, public SIG web pages (`/markets/151-154`, `/leaderboards`, docs), public Polymarket and Kalshi APIs,
and forecast pages.

## Verdict

| Claim | Verdict |
|---|---|
| 1. Prices and the size of the gap | **Survives.** The pair is worth about 23c outside against 11c on SIG. Kalshi's 26c bid is the high end. |
| 2. Same contract on all three venues | **Survives, with tail differences.** They do not explain the gap, and the one real SIG rule difference points the other way. |
| 3. Gap is new, and SIG moved alone because of large sellers | **Mostly fails.** A 7-10c House gap has happened three times this month. The named 30 Oct burst was a buyer. Senate moved mostly with the outside. |
| Implied: "an 11c digital" that we should trade | **Fails as stated.** It costs about 1.11 in cash, not 11c. EV is about +11% on cash, it loses 10% about 76% of the time, and it pays at settlement, after the punt. The 5,000 hard cap limits it to about 500 pairs, or about 60 SUSQies of EV. |

The mispricing is probably real. The trade as framed does not fit the account: the hard cap, the cash objective and bot
interference all get in the way.

## a. Same event, same side: yes

- **SIG legs.** From the public market pages: 840 is the only exchange of market 151, "Will the Democratic Party win the U.S.
  House?", option YES. 841, 842 and 843 are the R House, D Senate and R Senate equivalents. "NO" is the other side of the YES book.
- **Mapping.** `data/race_map.json` and `sigbot/venues/mapping.py` are correct. Kalshi's ticker suffix is House letter first,
  confirmed by the DR title ("House Control be Democratic AND Senate Control be Republican").
- **Live prices, about 04:50-04:55 UTC:**

| | SIG bid/ask | Polymarket bid/ask | Kalshi direct bid/ask | Bot's derived Kalshi |
|---|---|---|---|---|
| House D | 0.855 / 0.86 | 0.93 / 0.94 | 0.935 / 0.936 | 0.939 |
| Senate D | 0.745-0.75 / 0.755 | 0.70 / 0.71 | 0.70 / 0.71 | 0.686 |

- **Value of the digital** (P(DR) - P(RD)):
  - Polymarket balance-of-power markets: 0.235 - 0.0065 = **0.2285**.
  - Polymarket chamber markets: 0.935 - 0.705 = **0.23**.
  - Kalshi combo: DR 0.26/0.27, RD 0.006/0.007, so about **0.255**.
  - Kalshi direct R Senate 0.295 less RR 0.055 implies DR of about 0.24, so the combo looks about 2c rich.
  - **Use 0.23, not 0.26.**
- **Bot data problems:**
  - The derived Kalshi Senate leg reads about 2c below the direct market.
  - The derived House series jumps from about 0.914 to 0.939 around 30 Oct 12:00, next to the `race_map.json` rebuild at 12:02.
    Its earlier level is doubtful. I inferred the cause from timing and did not prove it.
- **Bottom-up check of Senate** (new, one-factor simulation of 35 seats, correlation 0.3-0.6, Ohio taken from Polymarket 0.705
  because SIG lists no Ohio race):
  - Polymarket's state prices give D 0.69-0.71, which matches its chamber price.
  - SIG's own state mids give D 0.64-0.66, **about 10c below SIG's own Senate market at 0.75**. This is the strongest evidence
    that the Senate leg is rich, though SIG's longshot tilt pulls that sum down somewhat.

## b. Real and tradeable: yes, for the House offer

- **The wall is real.** It shows in four separate reads:

| Source | Time (UTC) | Size at 0.86 |
|---|---|---|
| Saved book | 01:54 | 638,655 |
| `quotes` | 03:31 | 588,235 |
| Saved book | 03:56 | 586,550 |
| Public page | 04:53 | 573,487 |

  It first appeared between 01:24 and 01:51, judged from tape labels.
- **Takers fill against it.** About 50,000 went between 03:30 and 03:31. On 31 Oct, 54,220 printed at 04:45:08 (46,629 notional),
  plus 1,046 and 2,534 shortly after. The tape sum would leave about 529k against 573k on the page, so about 44k was added back
  by the wall's owner or other sellers.
- **It is not ours.** Our open orders on 840 are bids at 0.845 and 0.835, 36 shares in total. 841 has the equivalent NO bids. We
  have no fills on 840.
- **Senate side.** Depth is enough for our size. Touch sizes:
  - 842 best bid fell from 0.75 (about 1,500) to 0.745 (9,669) during this review.
  - 842 bid at 0.725: 130k.
  - 843 ask at 0.28: 18k.
  - Shorting through 842 NO at 0.255 beats 843 YES at 0.28.
- **`aggressor` is not reliable in bursts.**
  - It is classified from a stale quote, or by tick test. The 840 quote snapshot ran only intermittently: gap 21:35-03:30, none after 03:54.
  - Across 223 multi-level sweeps since 27 Oct, 8.8% of prints and 4.7% of shares are labelled against the sweep's direction.
    One sweep mixed 33 buys and 4 sells.
  - Use print order within one timestamp instead.
- **The tape has a large hole.** The Mac's network outage on 30 Oct ran from about 13:03 to 14:15, and SIG's public daily candles
  show what the DB lacks:

| Leg | Candle volume | Tape volume | Missing shares | Missing VWAP | Official range vs tape |
|---|---|---|---|---|---|
| 840 | 1,765,634 | 1,008,421 | **757,213** | 0.804 | low **0.725** vs tape 0.785 |
| 842 | 1,572,511 | 1,082,750 | 489,761 | 0.771 | high **0.80** vs tape 0.785 |
| 841 | 367,187 | 208,192 | 158,995 | 0.141 | |
| 843 | 1,217,737 | 1,146,199 | 71,538 | 0.271 | |

  Any tape study of the move is blind to its main stretch. 28 Oct also misses 22k-71k per leg.

## c. Who is on the other side: claim 3 does not hold as written

**The gap is not new.** SIG daily close minus Polymarket on House D:

| Date | House gap |
|---|---|
| 3 Oct | -9.5c |
| 7 Oct | -10.0c |
| 17-18 Oct | -8.5c, -9.0c |
| 26 Oct | +0.5c |
| 31 Oct | -7.5c |

26 Oct is the outlier. The House gap averaged about -5.1c from 1 to 24 Oct. The Senate gap was also -9c on 11 Oct, so it swings both
ways. The pair gap (Polymarket pair minus SIG pair) now stands at 12c, level with 3 and 7 Oct.

**Decomposition since 26 Oct.**
- House: SIG -6c, Polymarket +2c. So SIG is the main mover but not the only one.
- Senate: Polymarket +5c, SIG +8c. So about 60% followed the outside. SIG's own Senate gap grew from +1.5c to +4.5c.

**The named burst was a buy.** 30 Oct 14:16:28, 44 prints in one timestamp, 491,122 shares, 406,476 notional, and sequence order
rises 0.785 to 0.89. Polymarket stayed at 0.935 through 13:15-16:35, so this was not outside news.

Reconstructed order of events:
1. 29 Oct 11:33: a 90k sell sweep (78.7k notional) takes 0.895 to 0.865. The 1.3M wall at 0.90 reprices to 0.885-0.89.
2. 29-30 Oct: repeated round-dollar buys of Senate D: 19:14 (98.9k), 19:39 (99,999.52), 00:37 (61.7k), 12:08 (99,999.98). This is
   discretionary, not market making or set arbitrage.
3. 30 Oct 12:13: walls of 526k bid at 0.75 on 842 and 483k offered at 0.25 on 843, both the long Senate D side.
4. 13:03-14:15, inside the outage: 757k of House D trades at VWAP 0.804, down to 0.725. Senate D reaches 0.80.
5. 14:16:28: the 491k House buy. 14:19:31-32: 200k Senate D sold (0.78 to 0.75) and 401k Senate R bought (0.25 to 0.295).
   **That is this same pair trade, about 665k notional in 3 minutes, and possibly one actor.**
6. The House gap went from -15.7c to -7.5c. The Senate gap went from +9.4c to +5c. Both have sat there for 14 hours.

**Explanations and what each implies.**

| Explanation | Evidence | Implication for closing before results |
|---|---|---|
| One large directional actor long R House: 841 bid at 0.135 x 768k (19:51), then 840 offer at 0.86 x 638k, the same bet at 0.14. Posting the wall short costs only about 80-90k. | Best fit. Round tickets; about 121k hit on the bid at 20:51. | Persists while the actor has capital. A deep-behind player buying a 7:1 ticket for a top-3-only prize is rational and price-insensitive. It could end in a late repricing or a blow-off. |
| Leader de-risking a long-D book | Possible. 7 accounts on the top-100 board have pnl above 500k, 20 above 100k. | Persists through 3 Nov. |
| Market maker repricing | Weak. The DMM ladder is 1.5-20k per level and follows outside signals; these walls go the other way. | Would close by itself. It has not. |
| Set arbitrage | Rejected as the driver. Same-size mirror prints total only about 55k since 29 Oct. | None. |
| Wash trading or chip dumping | Not shown. On 27 Oct, a 100k House R buy at 21:44:28 was followed 57s later by a 485k sell at about 4c worse. That fits a fat finger, a stop or a transfer. | Cannot rule out. SIG can cancel trades if it finds collusion (FAQ), which could reverse fills. |

I cannot identify accounts. Everything about one actor is inference from sizes and timing.

**Does it close?** Evidence is mixed:
- The daily pair gap is mean-reverting: AR(1) 0.78, half-life about 2.8 days, n=30. After the earlier peaks of 0.07 or more it
  narrowed: 3 Oct 12c to 6c, 7 Oct 12c to 4c, 18 Oct 7c to 4c. But the 29 Oct episode widened, from 7.5c to 12.5c.
- `gap_trades.md` found no out-of-sample closing across all legs, with net -0.3 to -0.9c.
- Treat convergence by 3 Nov as possible, not expected.

## d. Settlement: no reason in the rules for SIG to price differently

- **Who and when.** Chamber markets are settled manually by an admin "once major news organizations have called control". They are
  not tied to the 4 Nov date. Trading ends 12:00 ET on 4 Nov and positions then lock. Final scores wait until every market has
  resolved. SIG "may use its discretion to resolve a market before or after" recounts or runoffs.
- **Late call.** From memory, not checked here: House control was called 16 Nov in 2022 and Senate 12 Nov. A close 2026 result,
  with Maine and Alaska RCV in the deciding set, may not be called by noon on 4 Nov. Then nothing can be sold and cash comes only
  in the final score.
- **N/A.** Refunds the "refundable cost" of held shares, which is mild here. An admin can correct a result afterwards. I found no
  sign of a fallback if an admin never acts, but final scores require every market to resolve.
- **Independents, the one real rule difference.** SIG: an independent whose caucus "cannot be determined on the date of the election"
  counts for nobody. Polymarket waits until 4 Jan. Osborn says he will "caucus with everybody".
  - Simulated P(D short of 51, R short of 50, and D plus independents at 51 or more) is 0.9-2.6%. That is roughly equal to Dave Manuel's 3% "neither".
  - SIG then falls back to the first President Pro Tempore, possibly January. The rule cannot make SIG D worth more than Polymarket D,
    so it does not justify a premium.
- **Runoffs.** SIG's Senate text says "in the November 3, 2026 U.S. midterm elections" and does not mention runoffs, unlike Polymarket,
  but the docs say chamber markets settle once the result is official. Georgia D is 0.97, so this barely matters.
- **Population.** SIG's eligible players are students, and a leaning there would be plausible, but it would lift House D too. The two
  legs moved in opposite directions, so a pure partisan lean does not explain it.

## e. What makes the pair worse than it looks

1. **Cash.** There is no margin. A pair costs 0.86 + 0.255 = 1.115 and pays 1, 2, 0, 1 in DD, DR, RD, RR.

| Probabilities used | Expected payoff | Edge per pair | Return on cash |
|---|---|---|---|
| Polymarket | 1.23 | +0.115 | +10% |
| Silver Bulletin (House 95, Senate 76.4; 23 Oct) | 1.186 | +0.071 | +6% |

   Polymarket's scenario odds: about 76% lose 0.115, 23.5% win 0.885, 0.65% lose 1.115. The Senate NO leg alone earns about 16% on
   cash at 0.255 vs 0.295. The House YES leg alone earns about 8.7%.
2. **Partisan hard cap.** netD is +4,691 against 5,000, leaving 309. `_signed_notional` counts the pair as +0.86 - 0.25 = **+0.61
   per pair**, so about **500 pairs, EV about 60**. A full 16k pairs would take netD to about 14,500, nearly three times the cap.
   Passing the cap also blocks the MM from adding D everywhere (`blocked_directions`).
   - The metric gets the sign wrong: with P(H) 0.935 and P(S) 0.705, national-swing delta is about 0.13 for House and 0.35 for
     Senate, so the pair is really net **short** D and would offset our long-D book.
   - Fixing the metric is a hard-limit change, which needs Sizhe.
3. **Cash objective.** Cash is 18,190. Projected cash for the punt is 47,785 (69,645 if early sets are sold too). Spending 18k takes
   26-38% of the punt stake. The edge pays at settlement, after the punt, unless the gap closes first.
4. **Exit cost.** On 16k pairs at the 04:53 books: bought at 0.86 and about 0.258, sold at about 0.847 and 0.237. That is **-3.4c per
   pair, about -545**. If the gap stays, this is a third of the edge.
5. **Wind-down will not raise the cash.** `leg_cost_cap` allows 1 tick against fair until 2 Nov, up to about 5.1c at 17:45 ET on
   3 Nov. Selling 840 at 0.855 against 0.935 is 8c, so it is never allowed while the gap stands. The legs stay in place by default.
   That protects the edge but means the money is not in the punt.
6. **Senate leg bookkeeping.** U.S. Senate is exempt from the wind-down as a high-frequency-arb race (842 -1,542, 843 -1,079). It
   returns to wind-down scope after churn flattens. Added 842 NO merges with churn's set and the MM's -463.
7. **MM will work the legs.** MM fair is SIG microprice plus adjustments (`mm.py:284`), not the outside. `race_inventory` counts the
   whole position, and at the cap it stops the adding side but keeps quoting the reducing side. So it would offer 840 near SIG mid and
   bid to cover 842 at about 0.75, leaking the position at SIG prices. `wind_down_hold.json` does not stop this. Any live trade needs
   an MM exclusion for these legs. I read this from code and did not run it.

## f. Could the real-money venues be wrong?

Yes, but the pair has room. It breaks even only if P(H) - P(S) is 0.11 or less. For example, 0.90 and 0.79 would do it.

- **House.** Models agree with the market: Silver Bulletin 95%, Dave Manuel 94%. Polymarket shows 2.9M bid at 0.93. This leg is the solid one.
- **Senate.** Contested. Silver Bulletin 76.4% (22-23 Oct, the latest I could confirm) is close to SIG's 0.75 and 6c above the market.
  Dave Manuel is 70% (31 Oct, not backtested). **The Senate gap may be SIG tracking Silver, not an error.** Polymarket's Senate touch is
  thin (49k at 0.70, 13k at 0.71). Even at Silver's numbers, the pair is worth 0.186.
- **History** (from memory, not checked): election-eve Senate favourites around 70% failed in 2022 (R) and needed the Georgia runoffs in
  2020. n is tiny. Errors have been correlated across chambers, and the pair partly nets that, but its modal loss is exactly a clean D sweep.
- Local work: `cheap_favourites` calibration on favourites at 0.80 or more was 52/52, which supports the House price.

## Could not check

- Tape for 30 Oct 13:03-14:15. Only daily candles exist, so the order within the outage is inferred.
- Account identities and positions behind the walls.
- Current Silver Bulletin, Economist and Race to the WH numbers. Silver's page was not readable and the latest figure is secondary (Newsweek-type reports).
- Kalshi and Polymarket price history for the combo, or a historical calibration of chamber markets.
- Full predictionscup.com/rules text: the fetch returned the site shell only, and I did not read the rules body. The docs and FAQ were read.
- MM and wind-down behaviour with a real position. Code reading only.

## Sources

[SIG settlement docs](https://sig.thesuper.market/docs/settlement-and-payouts), [SIG FAQ](https://sig.thesuper.market/docs/faq),
[SIG market 151](https://sig.thesuper.market/markets/151), [market 153](https://sig.thesuper.market/markets/153),
[SIG leaderboards](https://sig.thesuper.market/leaderboards), [Dave Manuel forecast](https://www.davemanuel.com/2026-midterm-forecast.php),
[Silver Bulletin](https://www.natesilver.net/p/nate-silver-2026-midterm-election-polls-model),
[Newsweek on Silver](https://newsweek.com/who-will-win-the-midterms-what-polls-show-10-days-out-12610772),
[Roll Call on Osborn](https://rollcall.com/2026/09/24/who-would-this-independent-caucus-with-everybody/).
Public Polymarket gamma and CLOB and Kalshi trade-api market endpoints.
