# The 3 Nov punt: the plan for Sizhe to approve (25 Oct 2026)

Read-only research, plus a switched-off executor (Part B, `research_notes/punt_executor_wiring.diff`). No orders were
placed, no config or bot code in the shared tree was changed, `.sig_api_key` was never read, and there were **2 SIG reads**
(leaderboard `period=all` and `period=7d`, top 100, 25 Oct 22:59 UTC, through `data/research_punt/sig_read.py`).

Rebuild (about 20 s, no venue calls):

    deploy/simq.sh <venv with numpy+matplotlib>/bin/python research_notes/punt_plan_study.py

* Numbers: `data/research_punt/plan_results.json`.
* Figure: `research_notes/figures/punt_plan_decision.png`.
* The script reuses `punt_screener` (races, SIG ladders, the calibrated map, the correlated race simulation) and the
  rival field model in `punt_strategy_study.py`, both read-only.
* Inputs: books of 25 Oct 22:23 UTC (`data/books_latest.json`) and `params/wind_down_report.json` (25 Oct 18:23 ET).

## Status (26 Oct ~01:30 UTC, 2-hour time box)

* **Part A (this plan): done.**
* **Part B (executor): built and gated, OFF.**
  * Opus review round 2: **SAFE TO DEPLOY with punt_enabled false.** Switching it on: **SAFE WITH FIXES.**
  * Its one remaining blocker (R2-1) is fixed with a test, but a third review round has not been run.
* **UNFINISHED before enabling** (full list in the shared notes `punt-plan-handover.md`):
  * Opus round 3;
  * the round-2 SHOULDs R2-2, R2-3, R2-4, R2-6 (rest) and S10;
  * the round-1 SHOULDs S1, S2b, S3, S4, S7, S8, S11, S12;
  * stronger 409 and election-sleeve tests.

## 0. Answer in brief

* **Cash at stage 1 (18:00 ET 3 Nov): about 87k if everything is sold, 72.6k if the cheap favourites ride to their call.**
* **Only the two-stage roll has prize value.**
  * At 87k it gives **P(top 3) ≈ 1.85%** and P(1st) ≈ 1.7%.
  * Its expected prize is **about $510**: $450-550 across the rival scenarios, and $330 if stage 1 sells badly.
  * A single longshot, a correlated basket and a laddered spread are each worth **about $0**: SIG's books cap any ticket
    bought with 87k at about 1.0M, which is below every plausible 3rd-place bar.
* **Recommended:** roll **AL-02 House D** (stage 1, bought 17:30-19:45 ET) into a late fair-price leg sized to
  **max(1.55M, 1.12 × the live leader)**.
* **Two alternatives:** the same roll aimed at 1.30M (the highest P(top 3), 2.3%, but half the prize EV), and a
  Vermont Governor D stage 1 (the earliest sale).
* **Cap exemption: YES, needed.** Every version is 87k one-sided at stage 1 and 150-300k one-sided at stage 2, against the
  5,000 partisan cap.
* **Two cash decisions for Sizhe:**
  * sell the cheap favourites before 18:00 ET: +14.4k of stage-1 cash, +0.3 pt of P(top 3), +$130 of EV;
  * sell MN-05 before 18:00 ET: already assumed in the 87k.

## 1. Projected cash at 18:00 ET on 3 Nov

| item | value | when it becomes cash | source |
|---|---|---|---|
| cash now | 17.6k | now | wind_down_report 25 Oct 18:23 ET |
| late-call sets (16, incl. RI Gov 10,742 sell-sets, HI Gov 7,658 buy-sets) | 29.9k | wind-down clips 31 Oct-3 Nov 18:00 ET | walked sale value |
| other legs and MM inventory (46) | 2.5k | 31 Oct-3 Nov | 1-3 ticks from fair |
| **wind-down total** | **50.0k** | **18:00 ET** | `projected_cash_1800_et` |
| MN-05 21,162 buy-sets (early call) | 20.3k | sold before 18:00 ET if the punt starts then (idea 3: waiting gains 0-0.8k) | today's sale value |
| HF arb (VA-07 sets) | 2.2k | churn sells everything from 17:30 ET | today's bid |
| **stage-1 cash, cheap favourites held** | **72.6k** | | |
| cheap favourites (36 legs) | 14.4k | **held to their call by design**; sold before 18:00 only if Sizhe says so | today's bid / 1 - ask |
| **stage-1 cash, everything sold** | **87.0k** | | |

**Uncertain:**

* **Cheap favourites.** Kept to the call, they bring 14.4-15.3k at about 20:00-24:00 ET. That is too late for stage 1 but
  usable for stage 2. Selling them early gives up about 0.9k of settlement edge (+6.4c a share). Those SUSQies are worth
  nothing unless we place, so selling is right if the roll is approved.
* **Carry to 3 Nov.** MM, churn, catch-up and shock P&L run for 9 more days. Roughly ±2k; shock mode's own stop is -2.5k.
* **Sale prices.** The wind-down figure is walked against today's books. Day-to-day moves on the hedged sets are small,
  but the 46 directional legs plus the cheap favourites carry ~±1k of mark risk.
* **RI Governor.** 10,742 sets have to go out in ~1,000-set clips, which takes about 5 h of the remaining window. It should
  finish, but a stalled clip leaves cash in the set.
* **Range: 80-90k (everything sold), 66-75k (cheap favourites held).** Refresh `params/wind_down_report.json` on 3 Nov
  16:30 ET.

## 2. Rivals and the bar (25 Oct 22:59 UTC read; 1,783 ranked; we are #474)

| rank | account | balance | P&L per day 23-25 Oct | P&L per day 18-23 Oct |
|---|---|---|---|---|
| 1 | kind-brook-6861 | 1,121k | +10.7k | +3.5k |
| 2 | bright-finch-0400 | 1,025k | +11.4k | 0 |
| 3 | steady-finch-4087 | 965k | -1.2k | +6.8k |
| 4 | nimble-beacon-4953 | 958k | +7.2k | +9.4k |
| 5-8 | bright-badger / noble-birch / merry-coral / calm-badger | 698k / 659k / 616k / 578k | +4-22k | 0-4k |

**Projected bar on 4 Nov** (top 60 rivals, carry at the mean of both rates × 8.8 days, settlement noise, rival punts; same
model as punt_strategy.md section 2). Values are 10th / 50th / 90th percentile, k SUSQies:

| scenario | 1st-place bar | 3rd-place bar |
|---|---|---|
| base case (35% of ranks 5-30 punt) | 1,202 / **1,367** / 1,589 | 1,057 / **1,172** / 1,303 |
| few rivals punt (10%) | 1,126 / 1,220 / 1,389 | 996 / 1,050 / 1,144 |
| most rivals punt (70%) | 1,299 / 1,456 / 1,661 | 1,152 / 1,268 / 1,393 |
| rivals copy our bet (corr 0.6) | 1,333 / 1,485 / 1,688 | 1,189 / 1,311 / 1,429 |

**What rivals likely hold, and how to bet differently:**

* **Leaders (top 4).**
  * Their gains were banked by 18 Oct (leader_study.md) and they now earn 0-11k a day, which looks like carry or market
    making.
  * They hold favourite-heavy books. The favourite-longshot bias says these gain at settlement; the field model gives them
    +4% × the held share.
  * Their exposure is not identifiable from public data (punt_strategy.md section 5).
  * At the 1.3M+ we need, we must clear them outright. Their book's reaction to our race does not matter.
* **Chasers (ranks 5-30, 165-700k).** Only a punt gets them to 1M+.
* **Where the crowd's money already is.** From the local tape (aggressor-tagged, 22 Oct 23:00 UTC to now), the most taker
  cash backing an outcome priced under 35% went to:
  * Wyoming Senate D (480k, mostly the alternating sweeps);
  * FL-20 R (351k);
  * Hawaii Governor R (232k);
  * Alaska Senate R (197k);
  * Senate control R (141k);
  * WA-03 R (129k);
  * VA-05 D (116k);
  * Mississippi Senate D, CA-48 R, Texas Governor D, MI-10 R and IA-02 R (69-78k each);
  * Vermont Governor D (58k).

  This is gross flow, not net positions, but it marks the "obvious" punts.
* **The rule for us.**
  * Use state-specific House and statewide upsets, not chamber control or national-wave parlays.
  * Avoid the crowded legs above as stage 1. AL-02 D and FL-13 D are not in them; VT Gov D and VA-05 D are.
  * Set stage 2's target from the live leaderboard on the night: 1-2 reads.
  * The "rivals copy our bet" column shows the cost of getting this wrong: about -25% of P(top 3) and -60% of prize EV
    at a 1.3M target.

## 3. Candidate structures (87k, books of 25 Oct; figure panel B)

Win chances:

* Polymarket/Kalshi blend, corrected for the favourite-longshot bias (the ev_hunt/punt_screener map, a 0.14, b 1.37).
* No haircut on 22-40c stage-1 legs (ev_hunt part 3; Fable fix 3).
* Stage 2 is a plain fair-price bet. It pays SIG's markup and the overnight longshot bias, taken half-way between 2024 and
  the calibrated 2022, with no correlated edge and no DMM lag credited. It walks a ladder measured on today's legs at the
  same price: about 1.3-1.4x the touch for 200-300k.

| structure | best version | executable at 87k (walk) | P(win) | final if it wins | **P(top 3)** base (range) | P(1st) | **expected prize** base (range) | expected SUSQies |
|---|---|---|---|---|---|---|---|---|
| single longshot (all-in before 18:00) | Alabama Senate D, touch 0.04 | 87k fills at **0.086** avg | 0.5% | 1.02M | 0.01% (0-0.1%) | 0 | **$0** ($0-3) | 5k |
| correlated basket (same party, national swing) | SD Senate D + SD Governor D | 43.5k each | both: 0.24% | 1.02M | 0.01% | 0 | **$0** | 15k |
| laddered spread (both parties) | NE-02 R + SD Governor D | 43.5k each | each <1.2% | ≤ 0.94M | 0 | 0 | **$0** | 17k |
| **roll** (stage 1 then late stage 2) | **AL-02 D (0.30 → 0.322 avg) → fair leg to 1.60M** | 87k at ≤ 0.35; stage 2 230k at ~0.10 touch, 1.42x walk | 1.85% | 1.60M | **1.85%** (1.43-1.85) | 1.68% | **$512** ($449-547) | 42k |
| roll | FL-13 D (0.295 → 0.317) → 1.60M | same | 1.79% | 1.60M | 1.79% (1.38-1.79) | 1.63% | $495 ($435-529) | 41k |
| roll | Vermont Gov D (0.26 → 0.285) → 1.55M | same | 1.80% | 1.55M | 1.80% (1.21-1.80) | 1.55% | $476 ($390-528) | 39k |
| roll, pessimistic sale | AL-02 D, 60% sellable in time, at 0.80 | | 1.33% | 1.50M | 1.33% | 1.05% | $328 ($241-386) | 54k |
| roll with cheap favourites held (72.6k) | VT Gov D → 1.50M | | 1.54% | 1.50M | 1.53% | 1.21% | $379 ($279-446) | 33k |
| no punt | | | | ~88k | 0 | 0 | $0 | 88k |

**Reading.**

* **Singles, baskets and ladders fail on depth, not on odds.** 87k into any outcome walks to an 8-9c average because SIG's
  ladder only gets deep from about 10c. No single ticket clears ~1.02M; at 72.6k the best is 0.87M.
  * A basket splits the cash, so each leg walks less. But if all its legs win it pays the same C / avg price as a single,
    and the tails are nearly independent (cross-state binary correlation ~0.03, sig-correlation-model), so P(all) is tiny.
  * A ladder (any one of k hits) pays only 1/k as much.
  * None reaches the 3rd-place bar. The best single starts to matter only above ~105k of cash (panel C, grey).
* **The roll works because it buys two 3-4x steps at prices where SIG and the outside are nearly fair**, instead of one
  12x step at 8c where they are not.
* **"Stage 1 then roll" and the sellability finding (shock_playbook section 8).**
  * A winning 20-35c stage 1 is almost never sellable at 0.85 before ~22:30 ET, so stage 2 cannot ride a correlated lag.
  * That is now the base case here: stage 2 is a late fair-price bet, with no P(B|A) edge.
  * What is not blocked: ranking is by **settled** balance and trading runs to 4 Nov 12:00 ET. So stage 2 can be bought
    any time from the stage-1 sale (typically 22:30-02:00 ET) until ~04:00 ET.
  * Stage 1 must count fast, so it can be sold at ≥ 0.85. Stage 2 just has to be uncertain when bought and resolve
    eventually.
  * The model sells stage 1 in time 85% of the time. The pessimistic row (60% at 0.80) costs about a third of the value.
* **The target trades P(top 3) against prize EV** (panel C).
  * P(top 3) peaks at **T2 ≈ 1.30M**: 2.28% base, but only 1.16-1.59% if rivals punt or copy.
  * Prize EV peaks at **T2 ≈ 1.55-1.60M**, where P(1st | win) is ~0.9 in every scenario.
  * 1st place is 80% of the money, and a 1.6M target is robust to how many rivals punt.
* **Cash matters roughly linearly.** The best roll's P(top 3) is 0.8% at 50k, 1.15% at 60k, 1.53% at 72.6k, 1.85% at 87k
  and 2.07% at 95k: about +$45 of EV per extra 10k. Every SUSQie raised for 18:00 ET is worth it.

## 4. Timing, and the partisan cap

* **Stage 1 (AL-02 D; Alabama polls close 20:00 ET).**
  * Buy 17:30-19:45 ET in chunks, never above 0.35. Stop 15 min before the close: after it, SIG's price follows the count.
  * The 18:00 ET cash arrives through the afternoon (wind-down clips to 18:00, MN-05 and cheap favourites sold 15:00-17:45,
    churn from 17:30), so stage 1 spends what is there and tops up until 19:45.
  * Earlier is no better: 2024 20-40c legs drifted ±4 pts with no direction between 16:30 and 18:55 ET (shock_playbook
    section 8).
* **Holdings vs punt timing (arb_ideas idea 3).** The punt starts at 17:30 ET, before every call, so holding early-call
  positions to their call gains nothing for stage 1:
  * MN-05 and the cheap favourites must be sold before 17:30 to count;
  * waiting for their calls is worth +0.5k (central) to +1.6k, and only for stage 2.
* **Stage-1 sale.** Start once Polymarket AND Kalshi both show ≥ 0.95 for 3 polls (single-venue fallback 0.97). Use either:
  * a resting sell at ≥ 0.85, or
  * buying the R leg at ≤ 0.15, which nets each pair to 1 at once.

  Detect auto-settlement via `settlements[]` and treat it as a bonus.
* **Stage 2.**
  1. Once ≥ 70% of stage 1 is sold, read the leaderboard (2 reads).
  2. Set T2 = max(1.55M, 1.12 × the best live rival).
  3. Buy the first approved candidate whose walked average fill ≤ W1 / T2, with a touch of 0.08-0.30, while its race is
     uncalled.
  4. Window 22:00-04:00 ET.
  5. If nothing fits by 04:00 ET, keep the cash.

  Pre-approved candidates (fast count, competitive today, not crowded): Kansas Senate, Iowa Senate, Kansas Governor,
  Texas Senate, Wisconsin Governor, PA-01, MN-01, TX-23, WI-01, MT-01. Either side, chosen on the night by price. Avoid
  NY-17 and every slow-count state.
* **Partisan cap exemption: YES, required.**
  * Stage 1 is 87k net D (17x the 5,000 cap) and stage 2 150-300k on one side.
  * The executor's orders are exempt only when Sizhe has said so in chat, as with the election sleeve.
  * The cap stays in force for every other sleeve.

## 5. The plans to approve

Each fits one screen. Numbers assume 87k (everything sold), 25 Oct books and the base case; ranges are over the four
rival scenarios.

### Recommended: P1, roll AL-02 D → 1.55M+

| field | value |
|---|---|
| cash decisions | sell MN-05, cheap favourites and every holding before 17:30 ET 3 Nov (late-call sets by the wind-down) |
| stage 1 | BUY AL-02 House race **D** (exchange 1007) YES, all cash (cap 90k), max price **0.35**, 17:30-19:45 ET, chunks ≤ 5k every ≥ 20 s |
| fallback stage-1 route | BUY NO via SELL of AL-02 R (exchange 1008) bids at ≥ 0.65, same cap, if cheaper per share |
| stage-1 sale | SELL at ≥ **0.85** (or buy R ≤ 0.15) once Polymarket AND Kalshi D ≥ 0.95 for 3 polls; deadline 04:00 ET 4 Nov |
| stage 2 | target T2 = max(**1.55M**, 1.12 × best live rival); first approved candidate (section 4) with walked avg ≤ W1 / T2 and touch 0.08-0.30; 22:00-04:00 ET |
| aborts | stage-1 price > 0.35, AL-02 called before we buy, outside D < 0.20, any order rejected 3 times, leaderboard read fails twice (use 1.6M) |
| cap | partisan-cap exemption for the executor's orders: **yes** |
| P(top 3) / P(1st) | **1.85%** (1.43-1.85%) / 1.68% |
| expected prize | **$512** ($449-547); $328 if stage 1 sells late or low |
| expected SUSQies | ~42k (vs ~88k with no punt); about 95% of the time we end below 15k (stage 1 loses ~69%; it wins but cannot be sold in time ~5%, ending ~270k) |

### Alternative A1: same roll, aimed at 3rd place (T2 = 1.30M)

| P(top 3) | P(1st) | expected prize | why not |
|---|---|---|---|
| **2.28%** (1.16-2.54%) | ~0.8% | $303 ($79-614) | the best P(top 3), but it lands near the 3rd-place bar, so it needs few rivals to punt; it loses most of its value if they punt or copy |

### Alternative A2: stage 1 Vermont Governor D (19:00 ET close), T2 = 1.55M

| P(top 3) | P(1st) | expected prize | why / why not |
|---|---|---|---|
| 1.80% (1.21-1.80%) | 1.55% | $476 ($390-528) | earliest close and sale (VT counts fast), cheaper (0.26 touch, bigger W1); but 58k of rival taker money already backs it, so copy risk is higher; stage-1 window 17:30-18:45 ET |

**No punt** gives P(top 3) = 0 and ends with ~88k.

## 5b. The executor (Part B; built, gated, OFF)

`research_notes/punt_executor_wiring.diff` against 7cc4e16 (not applied, not committed, not deployed).

**What it is.**

* `sigbot/proposals/punt_executor.py` runs a plan file of up to 8 steps of three kinds:
  * `buy`: capped limit chunks inside a window;
  * `sell`: sells what a buy step bought, at ≥ a floor, after Polymarket AND Kalshi have shown ≥ 0.95 for 3 distinct polls;
  * `target_buy`: picks the first pre-approved candidate whose capped ladder reaches max(target_min, k × best rival), then
    buys it.
* A template of P1 is `research_notes/punt_plan_P1_template.json`.
* It runs in its own thread every 10 s.
* Each order is a capped limit order with a 30-s TTL. The limit is the book level that fills the chunk, never above the
  step's cap. There is one order per step at a time.
* Progress comes from the venue position. Idempotency keys are `punt-<plan>-<step>-<n>`: a 5xx or timeout retries the same
  key, and the state file `params/punt_state.json` survives restarts.
* Purposes: buys are recorded as `punt`, sales as `punt_exit`. Strategy stops attribute both to "punt" with no stop.

**Switches and aborts.**

* **One switch: `punt_enabled`**, false in config.py and in config.json.
* The plan file must hash to `punt_plan_sha256`, or nothing starts.
* `params/punt_abort` (create the file) cancels the executor's resting orders and stops it for good.
* A step aborts after 3 rejected orders in a row, or (buy steps) when the outside price falls below
  `abort_if_outside_below`.
* The bot's own dry-run mode logs and fills virtually. The CLI dry run prints the whole sequence against a book dump:
  `python3 -m sigbot.proposals.punt_executor --plan <file> --books data/books_latest.json --at 2026-11-03T17:29:00-05:00`.
* Partisan cap: buys are capped at 5,000 unless the plan says `"cap_exempt": true` (Sizhe's yes).

**Fix 0c and exclusions** (apply even with the executor off).

* Every race with a `punt`, `punt_exit`, `election` or `election_exit` fill or order, plus every race of an enabled plan,
  is excluded from:
  * zero-EV exits, the wind-down (whole race), the cash raise;
  * free unwinds, the recycler and offload (`arb.protected`);
  * churn exits, churn entries and sell-all;
  * cheap-favourite entries;
  * the MM and the inside quoter.
* Zero-EV exits also never close the shares of a held set: the set's min |position| per leg is subtracted from what they
  may close.
* Before its first order on a leg, the executor cancels any other resting order of ours there (self-trade).

**Dry run against the 25 Oct books (87k, P1 template).**

* Stage 1 buys 275,128 AL-02 D at ≤ 0.325 (avg 0.316) in 19 chunks and spends all 87k.
* With a faked call (outside 0.97, SIG bid 0.90), it sells everything in 14 chunks at ≥ 0.85: 247.6k of cash.
* Stage 2 then keeps the cash: no listed candidate reaches 1.55M on today's (pre-count) prices, because every one is at
  0.28-0.60 and 1.55M needs a ~0.10-0.16 average.
* **On the night, the stage-2 list must be re-made from live books, with legs at about 6-15c in uncalled fast-count
  races.** Today's competitive races only get there once their count turns against one side.

**Enabling it** (Sizhe's call in chat only):

1. Apply the diff through the usual botctl deploy, still off.
2. On 3 Nov, re-price the plan from live books.
3. Show Sizhe the exact file and get a yes in chat, plus the cap-exemption yes.
4. Write `params/punt_plan.json`, put its SHA-256 in `punt_plan_sha256`, set `punt_enabled: true` and restart through botctl.

Enabling excludes the plan's races from the wind-down. Enable after its last clips (~17:15 ET), or accept that the few
sets held in those races (per the 25 Oct report: AL-02 31 sets, Kansas Governor 41) stay unsold.

## 6. Caveats

1. **Rival model.** Inherited from punt_strategy.md: the punting share, rival targets and settlement noise are guesses,
   bracketed by the four scenarios. The ranking (roll ≫ everything else) holds in all four; the dollar values do not.
2. **Books are from 25 Oct.**
   * Re-walk on 3 Nov 16:30 ET: `punt_screener --report --books <dump>` and this script.
   * The stage-2 walk (1.3-1.4x for 200-300k into a 10-15c leg) is the most fragile number, because night depth is
     unmeasured.
3. **Stage-2 calibration.** It uses 2024's overnight bias, shrunk half-way. If 2024's 0/158 at 2-5c and 5/72 at 10-15c
   is the truth, a 10c stage 2 is worth ~25% less.
4. **Stage-1 sale.** Recovery 0.85 and 85% in-time sale are judgements, not measurements; no SIG market has settled yet.
   The pessimistic row is the hedge.
5. **AL-02.** It is a newly drawn district: the outside price (0.31) has thin history and the count speed is less certain
   than Florida's. FL-13 D is within 3% on every number and is the drop-in substitute.
6. **No P(B | A) edge is credited.** If SIG lags the repricing of a correlated stage 2, the roll is worth more.

## Sources

* `punt_strategy.md` (bar model, 2024/2022 history), `shock_playbook.md` section 8 (sellability), `ev_hunt.md` part 3
  (calibration), `high_variance.md` (sale value, cap), `arb_ideas.md` idea 3 (cash by punt time), `leader_study.md`.
* `concentrated_risk_sizing.md` (sleeve sizing; national swing not the loss driver).
* Memory: correlation model (cross-state binary tail correlation ~0.03).
* Venue: 2 GETs, `/tournaments/midterm-elections/leaderboard?period=all|7d&sort=pnl&limit=100`, 25 Oct 22:59 UTC
  (`data/research_punt/sig_reads/lb_*_1025.json`).
