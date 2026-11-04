# The 3 Nov punt re-priced on live books (3 Nov ~19:45 UTC)

Method: research_notes/punt_plan_study.py (same rival model, sale rule, SIG markup and overnight calibration) on today's
inputs: the feed's full-depth books (19:39 UTC, after the 18:32-19:38 outage), outside fair from venue_fair (19:42 UTC),
the wind-down projection (14:41 ET) and 3 leaderboard reads (19:44 UTC). Code: the scratchpad `punt_live/` scripts.
Figure: `research_notes/figures/punt_live_3nov.png`. 3 SIG reads, no orders.

## Answer

* **The 25 Oct plan (P1, AL-02 D roll, $512 expected, 1.85% top 3) is dead.**
  * AL-02 D costs 0.51 now (it was 0.30), outside 0.54.
  * More importantly, **SIG's books are 2-4x thinner than in October** (figure B). 150k into a 15c leg now averages 3.4x
    the first price; on 21 Oct it was 1.3x.
  * So no single bet of 250k (what a won first bet raises) pays more than ~0.65M at a 5%+ chance (figure A). The
    3rd-place bar tonight is 1.09-1.30M.
* **Best plan left: P2, an Oklahoma Governor D roll.**
  * Expected prize **$43** and 0.23% top 3 with outside odds; **$17** and 0.09% with the longshot correction.
  * Every roll in the study's 22-40c band is worth $0: Vermont Gov D, SC Senate D, FL-13 D, FL-07 D, PA-01 R and others.
* **No punt is worth $0** (P(top 3) = 0, about 69-81k kept).
  * **P2 is a lottery ticket:** about 99.8% of the time it ends near 0-20k.
  * It is better than no punt only if the final balance short of a prize is worth nothing to Sizhe.

## Plans (cash 68.8k with the older favourites held, 81.5k if sold)

| plan | first bet (live) | if it wins | P(top 3) | P(1st) | expected prize (4 rival cases) |
|---|---|---|---|---|---|
| **P2** | Oklahoma Gov D (875) YES, touch 0.155, ≤0.205, 51k at avg 0.189; OK polls close 20:00 ET | 247k after the 0.85 sale → stage 2 to 1.35-1.40M | 0.23% (0.09% calibrated) | 0.13% | **$43** ($20-64); $17 calibrated; $23 if the sale is late or low |
| P2 with 81.5k | same | 260k | 0.23% | 0.16% | $50 ($30-65) |
| Vermont Gov D roll | 1001, touch 0.325, avg 0.353 | 154k → stage 2 tops out ~0.5M | 0 | 0 | $0 |
| AL-02 D / R roll | touch 0.51 / 0.47 | outside the 22-45c band; 2x at best | 0 | 0 | $0 |
| best single, all in now | RI Senate R, touch 0.10 | 189k | 0 | 0 | $0 |
| no punt | | ~69-81k | 0 | 0 | $0 |

Bar tonight, base case: 3rd place 1.09-1.30M (median 1.175M), 1st 1.20-1.59M. Leader 1.117M, #3 1.075M.

## P2 file

* Scratchpad `punt_live/punt_plan_P2.json`. It passes `punt_executor.validate` (budget ≤ 150k, cap_exempt true).
  * Stage 1 buys 875 YES ≤ 0.205, 17:30-19:45 ET, 5k chunks every ≥ 20 s. It aborts if the outside price is < 0.05.
  * The sale is at ≥ 0.85 once Polymarket AND Kalshi show ≥ 0.95 for 3 polls (0.97 for one venue alone), 19:45 ET-04:00 ET.
  * Stage 2: target max(1.35M, 1.12 × leader) (≈1.35M), fallback 1.40M, 22:00-04:00 ET.
* **Stage-2 candidates changed.** The 20 pre-approved legs are now 30-65c toss-ups. None of them can turn 250k into 1.35M,
  so P2 lists 24 legs of fast-count races closing 21:00-22:00 ET (outside 6-40% now) with touch 0.02-0.20.
  * The model's stage 2 needs a leg near 5c at night (x2 ≈ 0.054).
  * If none is available, the executor keeps the cash.
* **Dry run** (executor CLI, today's books, 68.8k at 17:31 ET): 15 chunks fill in ~5 min.
  * The dry adapter does not consume the book, so its 0.16 average is a best case. The static walk is 0.189.

## Caveats

* **Network.** This Mac lost DNS three times today: 09:4x, 11:30-11:48 and **18:32-19:38 UTC**. The bot ran no cycles
  in the last one. An outage during 22:30-00:45 UTC (stage 1) or the night sale stalls the executor (it retries; nothing
  fires twice). A wired or phone-hotspot fallback is advisable.
* Oklahoma Gov D costs 0.155 on SIG against 0.095 outside: we pay 1.6x the outside price. An upset there is called late,
  so the 85% sellable-in-time assumption is optimistic. The low-sale case gives $23.
* Stage 2 is modelled on today's book depth. If the market maker refills its ladders tonight, value rises. If it thins
  further, it falls.
* Not covered: three-stage rolls (each extra step loses about half to the walk on these books), and any SIG-lag trade
  after calls (the election sleeve, off by hard limit).
