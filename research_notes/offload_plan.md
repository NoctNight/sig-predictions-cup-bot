# Cheapest way to turn holdings into cash for a quick punt (31 Oct 2026, 01:33 UTC)

Read-only. No orders, no SIG calls, nothing changed in the bot. Figures are from positions at 01:33 UTC and books at 00:53 UTC.

## Verdict

* **Money is cheap to raise. The partisan cap is what stops a big trade.** Net D is +4.8k against the 5,000 cap. Under it, a
  Republican-side trade fits up to **9.8k** and a Democrat-side trade only **0.2k**, however much cash we raise.
  Anything bigger needs Sizhe to exempt the punt from the cap.
* **Raising 20k in 5 minutes costs about 120 S (0.6%).** Worst case, one pass through the book: 180.
  Mostly this means selling Rhode Island Governor (15.5k) plus small late-call sets.
* **Raising 60k in 5 minutes costs about 1,560 S (2.6%).** Worst case 1,960. Most of that is locked profit given up on
  MN-05 (834), plus the Hawaii Governor sets, cheap favourites and legs that would otherwise ride to settlement.
* **Speed barely matters.** Taking 6 hours instead of 5 minutes saves about 55 S on 20k and 90 S on 60k.
  Nothing is gained by waiting, and depth on our legs is falling (58k shares near the touch tonight, 141k on 24 Oct).
* **If everything would be sold by 18:00 ET 3 Nov anyway, the true cost is about zero** at any target: -187 at 20k and -236 at 60k
  tonight, because MN-05 and Hawaii are 2 ticks cheaper to exit than usual. Over the last 17 saved books the 60k figure ran from
  -430 to +240.
* **Nothing built today can do this quickly.** See "Limits". A one-off executor is needed. It should send MN-05's first basket as a
  test, because the bot has never sold a YES set live.

**Decisions for Sizhe**

1. **Partisan cap:** exempt the punt, or accept 9.8k Republican / 0.2k Democrat. Democrat-side room can be bought by selling
   long-Democrat legs: 5k of room costs about 142, 10k about 426, 15k about 838 (raising 4.9k, 10.4k and 16.3k).
2. **Shock reserve:** 15k of our 17.9k cash is reserved, so only 2.9k is spendable. Releasing it is the cheapest 15k available, at no
   exit cost. The price is losing shock mode. No change is proposed here.
3. **Cheap favourites:** sell them only for targets above about 25k, and pause that sleeve first (see "Limits").

## Questions 1 and 3: cost by target and speed

Value given up, in S, as things are configured today. "Per 1,000" is the 5-minute column per 1,000 raised.

| new cash | one pass (seconds, worst case) | 5 min | 1 h | 6 h | per 1,000 | if all sold by 18:00 ET anyway (5 min) | net D after (5 min) | writes (5 min) |
|---|---|---|---|---|---|---|---|---|
| 5k | 4 | -4 | -8 | -9 | -1 | -60 | +4,688 | 37 |
| 10k | 51 | 29 | 12 | -5 | 3 | -109 | +4,938 | 53 |
| 20k | 181 | 116 | 85 | 60 | 6 | -187 | +4,938 | 57 |
| 40k | 796 | 689 | 639 | 604 | 17 | -252 | +4,826 | 93 |
| 60k | 1,962 | 1,556 | 1,506 | 1,470 | 26 | -236 | +4,533 | 99 |
| 80k | 3,407 | 2,868 | 2,808 | 2,771 | 36 | -80 | -1,736 | 112 |

* New cash is on top of the 17.9k we hold. Selling everything raises about 82.6k.
* The same plan on each of the last 17 saved books gives, in 5 minutes: 20k -130 to +360 (median about 0), 60k 1,360 to 2,690
  (median 1,575). Tonight is typical. The dear books are 29 Oct 11:12 and 30 Oct 12:13.
* 5 min, 1 h and 6 h mean several baskets per race, so treat them as floors and the one-pass column as the ceiling (see question 2).

**The order, 5-minute plan** (exact list per target and speed: `data/research_offload/plan.json`):

1. Small late-call sets and scraps: free.
2. Rhode Island Governor, all 8,078 sets: 15.5k for 114. It is at its usual exit cost.
3. Nebraska Senate sets: 2.2k for 23. Legs that lean Republican or sit near fair come in here too.
4. MN-05, in stages up to all 21,162 sets: 20.3k for 834 against payout.
5. Hawaii Governor sets: 7.3k for 390 against payout (see question 4).
6. Cheap favourites and the legs in their races: 26.3k for about 1,290 by the 80k target (all of them: 28.6k for about 1,460).

## Question 2: ways of selling

**Routes that exist.** Checked in the API spec.

* **No merge or redeem route.** NO on Democrat plus NO on Republican cannot be cashed in as a pair. Holding YES and NO on the same
  leg nets and pays 1 a share, so closing a holding by buying the opposite side is the merge, leg by leg.
* Closing costs the same as a conventional sale: `sell yes at p` equals `buy no at 1 - p`.
* Baskets of up to 10 legs can be all-or-none. Batches of up to 50 single orders count as one write but are not all-or-none, so a
  remainder rests unless it carries a short expiry.

| holding | at touch | cash at touch size alone | lost to walking, one pass | lost to walking, 5 baskets | best route |
|---|---|---|---|---|---|
| late-call sets | 27.9k | 6.3k | 264 | 130 | all-or-none baskets, several per race |
| early-call sets, mainly MN-05 | 23.8k | 2.9k | 447 | 93 | all-or-none baskets, 5 or more |
| cheap favourites | 15.0k | 11.0k | 22 | 22 | single orders at the touch |
| legs in cheap-favourite races | 13.6k | 11.7k | 18 | 17 | single orders at the touch |
| legs the wind-down sells | 2.5k | 2.5k | 0 | 0 | single orders at the touch |
| high-frequency arb lots | 1.3k | 1.3k | 0 | 0 | leave them: they sell themselves, so they are outside the plan |

* **Crossing at the touch** is enough for most single legs but only about a sixth of the sets' value.
* **Walking the book** is cheap: public sweeps of 20-50k shares walked about 2 ticks, and tonight's ladder takes all our legs for 40 S.
* **Resting as a maker: no.** A resting 1,000-share order fills completely 11% of the time in 1 hour and 27% in 4 hours
  (`maker_arb.md`), for 0.7-0.9 tick more. Across 40 legs for 6 hours that saves about 50 S. MN-05's legs see only 52 and 657 shares an
  hour on the side that would fill us. A set also needs every leg filled.
* **Leg by leg instead of whole sets: no.** The cash is the same, but selling one leg leaves the other naked. One MN-05 basket's worth
  would add about 4k of net D and break the cap. Keep baskets all-or-none.

**How the ladder refills** (`data/research_offload/impact.json`)

* **Single legs: size comes back, the price does not.** After public sweeps the touch size is back to 0.8-0.95 of usual within
  30 seconds, except for sweeps over 30k (about a third). The price stays moved: 0.2, 0.5, 0.9 and 2.6 ticks for about 1.8k, 5k, 17k
  and 120k shares of net flow. The move is about the same at 5 minutes, 1 hour and 6 hours, and fits a square-root law
  (0.19 × (thousand shares)^0.56). Holds strongly to 1 hour, softer beyond. So slicing a single leg saves little.
* **Whole sets: little or no lasting mark on NO sets.** 89 of our own exits (average 1,534 sets) changed the exit cost by -0.1 ± 0.15
  tick at 2 minutes and 0.0 ± 0.2 at 1 hour. 19 runs of repeated exits moved it +0.2 ± 0.3 tick. One run sold 34,479 Idaho Governor sets
  in 3 minutes at about the same price throughout. So several baskets beat one pass, and that is the whole gain from going slower.

## Question 4: value of each holding

| holding | sale now, at touch | if left alone | cost of selling |
|---|---|---|---|
| Rhode Island Governor (8,078) | 15.6k | wind-down at its usual 13 ticks | **close to free** (114 in the plan, mostly walking) |
| other late-call sets | 5.0k | wind-down | **close to free** |
| legs the wind-down sells | 2.5k | wind-down at the touch, or fair where it would not sell | 167 |
| Hawaii Governor (7,658) | 7.3k | **may not be sold at all** (below) | 0 if the wind-down would have sold it, 390 if it rides to payout |
| MN-05 (21,162) | 20.4k | payout, 21.2k | **dear: 834**, about 4 ticks a set. About zero if it would be sold before 18:00 ET anyway, as the punt plan assumed |
| other early-call sets | 3.4k | payout | about 100 |
| cheap favourites | 15.0k | outside fair at settlement, 15.8k | **dear: about 790**, 5.3% |
| legs in cheap-favourite races | 13.6k | outside fair at settlement, 14.3k | **dear: about 670**, 4.9% |

* The gap to fair on cheap-favourite legs is persistent: on legs with a gap over 3 ticks, the touch came within 2 ticks of fair on
  0.4% of polls. Their value is only reached at settlement, so under the cash-at-18:00 objective it does not count.
* **Finding for the wind-down: Hawaii Governor may be stranded.** Its usual exit cost (12 ticks) is above the wind-down's final cap
  (10.4 ticks, 5.5% of proceeds). The touch was inside that cap 27% of the last 24 hours and 68% of the last 7 days, and the cap only
  reaches 5.5% at 17:45 ET 3 Nov. If it is not sold, 7.3k of cash misses the punt. Tonight it sits at 10 ticks, so selling in a dip like
  this is sensible whether or not a punt happens.
* Legs in cheap-favourite races (13.6k) are also not sold by anyone: the wind-down skips those whole races and the cash raise excludes
  them. The bot's own report already leaves them out of its projected cash.

## Question 5: limits

* **Partisan cap.** Sets are neutral, so the first 60k barely moves net D. The plan keeps it within 5,000 at every step; the cap costs
  at most about 10 S. Selling Republican-leaning legs pushes net D up and is blocked near the cap. Selling long-Democrat legs helps.
* **Shock reserve.** Unchanged by this plan. If a punt takes cash below 15k, spendable goes negative and every other sleeve stops
  until cash returns.
* **Sleeves blocking each other.**
  * The wind-down starts at 04:00 UTC today. It sells slowly, is limited to 1 clip a cycle, and runs behind timing gates and a 200-an-hour
    cost budget. The cash raise then stands aside for its races. Together they cannot raise tens of thousands in minutes. The raise's whole-set
    tier is off. The positions change overnight, so rerun this plan first.
  * The bot's set-exit code refuses cheap-favourite races (New Hampshire Senate, Florida Governor, PA-10 and two small ones, 2.1k sets).
    They are marked `bot_refuses` in `plan.json`; an executor must allow them or skip them.
  * Cheap favourites will buy back if sold under their own exit tag, because budget room reopens. Sold under another tag, their ledger
    goes wrong. So pause that sleeve first.
  * High-frequency arb can still spend up to its 4k pot until 1 Nov 00:00 UTC. The market maker's budget also grows with cash. Raised cash
    should not sit idle long.
  * A sold leg's market-making quotes must be pulled first: an all-or-none basket that crosses our own quote is refused, and the MM is
    kept off for 15 minutes after.
* **Per-strategy stops.** Sets sold below payout count as a fall in the raise ledger, and a 2,000-an-hour fall pauses the exit paths. 60k in
  5 minutes shows about 1,840, under the line; 80k about 2,150, over it. Orders must carry a known tag or they are treated as untagged losses.
* **Writes.** 60k needs about 100 writes in 5 minutes against a shared 150. The bot's median is 9 orders a minute, but it bursts past 30.
  The configured ceiling leaves only 2 a minute guaranteed, so pause the market maker during the sale or allow 10 minutes. Counts include 25%
  extra for baskets: 26% of past exit baskets failed all-or-none and needed a retry.
* **Cash check.** A buy needs cash, and no past exit has grossed above cash balance (peak 89%). 5-minute baskets peak at 4.4k, which is fine.
  One MN-05 basket for the whole holding would gross 21.9k against 17.9k, so use a conventional sell there or split it.

## Question 6: the way back

* **Yes, the bot runs normally on the cash after the punt is sold.** Set arbs buy no new sets, recycling is off from 04:00 UTC, and no
  sleeve needs a set in order to work.
* **What is lost is only what the tables above show:** MN-05's 834 of payout and the cheap favourites' 790 of settlement value if those were sold.
  Buying them back would cross the spread again, so do not. Nothing else depends on the sets.
* **If the punt loses,** the shortfall comes from the 3 Nov cash, which is a matter for the punt, not this plan.

## Caveats

* **YES sets have never been sold live.** There are no such exits on record, and MN-05 and Hawaii are 28k of the top end. The evidence above
  is for NO sets. If those legs behaved like single legs, full size would cost about 215 more, still under the one-pass line. Send one basket first.
* **3-leg races left a mark:** Nebraska +3 ticks and Rhode Island +2 ticks in their runs, the only two 3-leg runs on record. That adds about 40 to
  Rhode Island. Idaho, the largest run, did not repeat: it is one episode.
* Books are saved hourly, so a real trade needs a fresh read. Past exits came in 1.25 ticks better than the saved book on average (n = 21, books up
  to 3 hours old), which tests book drift more than the walk.
* Selling under an outside fair assumes that fair is right. The sleeve's own record has not yet tested it.
* No election-eve data. Depth has fallen steadily, which argues for selling sooner.

## Refreshing it

Run `research_notes/offload_plan_study.py` with the Python that has numpy and pandas. It takes about 10 seconds and uses local data only. `study` reruns the ladder study (through
`deploy/simq.sh`) and `plot` redraws the figure.

* List: `data/research_offload/plan.json` (`speeds` holds the order per target, `partisan` the room and the legs to sell for it).
* Study: `data/research_offload/impact.json`.
* Figure: `research_notes/figures/offload_plan_decision.png`.

## Independent check

An Opus reviewer recomputed the work from the database and books and tried to break it. It found no error in positions, signs, walking or the sell
order: all 145 legs tie out, and the sets match the bot's own report. Three findings changed the results and are applied or stated above:

* the all-sold view valued legs at a price they almost never reach (fixed: its 60k figure went from +315 to -236);
* Hawaii may be stranded (now valued at payout in the configured view, which adds about 320 at 60k);
* the "no mark" evidence covers NO sets only (see Caveats).

I confirmed the Hawaii claim directly, and softened it: the touch does dip inside the cap, so it may sell partly.
