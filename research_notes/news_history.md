# Can a news model trained on past election news trade SIG? (24 Oct 2026)

Sizhe asked: "why not just train the bot on more past data ... all past articles relating to US elections and compare it
to Polymarket/Kalshi". The study scored past election headlines with the bot's own news prompt, measured the Polymarket
move after each one, calibrated the scores, and then tested the calibrated signal on SIG's live news since 21 Oct.

* Code: `research_notes/news_history_study.py` (`fetch`, `events`, `label`, `analyse`, `plot`).
* Data: `data/research_news_history/` (`raw/` cache, `headlines_*.json`, `events.json`, `labels.json`, `results.json`, `out.md`).
* Figure: `research_notes/figures/news_history.png`.

Read-only study:
* Nothing under `sigbot/`, `config.json`, `params/`, `tests/` or `deploy/` was touched.
* `sigbot.news`'s prompt, schema and client and `sigbot.api._ssl_context` were imported, not changed.
* 0 SIG venue calls. No orders.
* Claude: 194 calls of `claude-haiku-5-5` for 1,844 headlines (516k input and 185k output tokens).

## Verdict: drop it for trading. Do not spend more on the classifier.

1. **The headline is old news by the time it is published.** Polymarket has already moved in the hour before a headline:
   * The excess absolute move in the hour before is +0.19 points (t 2.3), versus +0.11 (t 2.0) in the hour after.
   * After a headline, prices move only a little more than at a random time in the same race: +0.11 points in 1 h,
     +0.31 in 6 h, +0.69 in 24 h. These are excess absolute moves, with no direction attached.
   * Story type, outlet, how close the race is and days to election predict nothing out of sample. For the excess
     absolute move at 1 h, R² is −0.01; at 6 h it is +0.05 (Spearman +0.23).
2. **The bot's prompt does not call the direction.**
   * On the 1,060 past headlines, Haiku's confidence had a median of 0.15 and never reached 0.6. 0 of 1,060 calls pass
     the bot's gate (conf ≥ 0.8, move ≥ 3).
   * Scores against the realised Polymarket move, test days: Spearman −0.02 at 1 h, +0.02 at 6 h.
   * After calibration on the training days, the test bins are flat or reversed (panel A).
   * On Polymarket, every threshold loses after the 1-point round trip: −0.97 points at 1 h, −0.77 at 6 h, −0.17 at
     24 h (t −0.9).
   * The one "significant" result is the 24 h move in the call direction on 2022 test days: +0.84 points (t 4.9). It is
     a Republican lean, not news skill. Haiku backed the Republican side on 78% of those headlines while the 2022 market
     drifted Republican. Simply always backing Republicans earned +1.29.
3. **On SIG, news adds nothing once the outside price is known (the key question).**
   * Over 751 live news decisions (675 with SIG and outside prices), SIG's move beyond its usual catch-up toward Polymarket/Kalshi,
     in the direction Haiku called, on test days (23–24 Oct):
     * −0.02 points at 15 min (t −2.3)
     * −0.11 at 1 h (t −3.4)
     * 0.00 at 6 h
   * Net P&L of buying SIG in the called direction on test days: −1.2, −1.3 and −0.9 ticks/share at 15 min, 1 h and 6 h.
     The headlines the model rated highest (calibrated > 0.25) were worse: −1.3 to −2.1 ticks/share.
   * SIG did not move at all within 15 min after 76% of headlines.
4. **The live outside move "in the called direction" is one story.**
   * Haiku's direction lined up with the outside move on test days: +0.7 to +0.9 points.
   * About 28% of that came from 10 headlines on the Alaska Senate ballot lawsuit, where Republicans jumped 13 points.
     Without Alaska the figure is +0.02 to +0.03 points (t ≤ 1.4).
   * The bot's own Opus calls on the same headlines went the wrong way: −1.0 to −1.3 points.
   * Whenever the outside price does move, the catch-up sweep (`outside_stable_sweep.md`) already trades SIG's lag
     directly from the price, without needing the headline.

**Election night:** don't use headlines there either. Race calls and results reach Polymarket and Kalshi within seconds
to minutes, and the outside price feed does the same job faster and more reliably.

**Only remaining use:** a mild risk flag.
* SIG's absolute move after a headline is 1.4–1.7 times its usual size, but only 1.0–1.3 times without Alaska.
* At 1 h that is 0.28 points, under 1 tick.
* It weakly supports the market maker's existing news pause, not a trade.

**If anyone revisits the classifier**, these changes would at least make it measurable. None of them fixes the core
problem that the outside price moves first.
* (a) Give it the Polymarket/Kalshi price and their last-hour move, and ask whether the outside price has already moved.
* (b) Add few-shot examples of headlines that came after the move: "debate recap", "poll roundup" and "what's going
  on with...". These made up most of the high scores here.
* (c) Remove its party lean. Ask for D and R separately, or check that calls are balanced.
* (d) Score only items with `new_information` true and an outside move of 0 so far.

## Data

* **Headlines (step 1).**
  * Archived RSS snapshots on the Wayback Machine, first snapshot in each 6-hour block, 4 a day at most.
    * Feeds: NYT, NPR, Washington Post and CBS politics, Breitbart, Washington Examiner, National Review and Newsweek
      (both years), plus Fox (2022).
    * Each item keeps the publisher's own pubDate, to the second.
    * Windows: 15 Aug–5 Nov 2024 (12,838 unique headlines) and 1 Sep–8 Nov 2022 (9,974).
  * Sources that did not work:
    * GDELT DOC refused this IP every time ("one every 5 seconds"), including after pauses.
    * Google News RSS dates old items only to the day.
  * Matching: headlines were matched to races by candidate names (or "control of the Senate/House").
    * Result: 1,060 headline-race events, 298 from 2024 and 762 from 2022. Most political headlines are about the
      presidential race, which SIG does not trade.
* **Outside prices.**
  * 2024: Polymarket CLOB prices-history at 1-minute resolution, D leg, for 22 races plus Senate and House control.
    CO-08 returned no history.
  * 2022: pre-CLOB FPMM markets, rebuilt from the on-chain trades cached by `favourite_drift_extra.py` (18 races).
    * Block times are interpolated over about 6 h, and races have only 100–1,000 trades each.
    * So 2022 enters only at 6 h and 24 h; at 1 h most 2022 moves are 0.
  * No Kalshi: its 2024 race markets started late and were thin.
* **Labels.** The bot's `FINAL_SYSTEM` prompt and schema, unchanged:
  * Today = the publication time; venue prices = the Polymarket price then.
  * Haiku, 20 headlines a call.
  * Score = expected move × confidence; the sign comes from party and direction.
* **SIG.**
  * Source: `news_events` (751 decisions, 21 Oct 23:30 to 24 Oct UTC).
  * SIG mid: `quotes` and `venue_quotes` (venue `sig`) merged. Entry is the first SIG quote within 15 min after the
    decision. The mid h later is taken from the nearest quote within 10 min.
  * Outside fair: the Polymarket and Kalshi mids for the same party. If both are quoted they must agree within 4 points.
  * Outside move: the average of each venue's own change, so a venue dropping in or out cannot fake a move.
  * Catch-up: SIG move = a + b × gap, fitted on every leg the bot called news on, every 10 min, training days only.
    b is 0.006 at 15 min, 0.020 at 1 h and 0.10 at 6 h.
* **Splits.**
  * 2022/2024: training before 10 Oct, test from 10 Oct.
  * SIG: training 21–22 Oct, test 23–24 Oct.
  * t-statistics are clustered by day (historical) or race (SIG).
* **Costs.**
  * Polymarket: 1 point round trip.
  * SIG: the bid-ask spread at entry (usually 1 tick = 0.5 point).

## Results tables

### 2. Excess absolute Polymarket move after a headline

Points above that race's random-time average. "Pre" is the hour before the headline.

| type | n train | 1 h train | n test | 1 h test | 6 h test | 24 h test | pre-1 h test |
|---|---|---|---|---|---|---|---|
| all | 389 | −0.00 (t −0.2) | 671 | +0.11 (t 2.0) | +0.31 (t 3.2) | +0.69 (t 3.7) | +0.19 (t 2.3) |
| scandal | 52 | +0.01 | 40 | +0.13 (t 1.4) | +0.20 | +0.72 | +0.02 |
| debate | 29 | −0.02 | 133 | +0.09 | +0.27 (t 2.0) | +0.80 | +0.29 (t 2.1) |
| endorsement | 23 | −0.01 | 31 | +0.12 | +0.18 | +0.51 | −0.04 |
| poll | 43 | +0.01 | 92 | +0.18 (t 1.3) | +0.34 (t 2.0) | +0.58 (t 3.3) | +0.09 |
| fundraising | 25 | −0.06 | 37 | +0.01 | +0.46 | +0.47 | −0.03 |
| other | 205 | −0.00 | 324 | +0.10 | +0.32 (t 2.7) | +0.72 (t 3.5) | +0.25 (t 2.1) |

Dropout (8), results (5) and court (13) have too few events to read.

### 3. Prompt calls vs realised Polymarket move, test days

| horizon | Spearman(score, move) | mean move in call direction | hit rate (non-zero moves) | net after 1-point cost |
|---|---|---|---|---|
| 1 h | −0.02 | +0.03 (t 1.2) | 53% | −0.97 |
| 6 h | +0.02 | +0.23 (t 2.3) | 58% | −0.77 |
| 24 h | −0.01 | +0.79 (t 5.2), Republican lean (see verdict) | 62% | −0.17 (best threshold) |

Calibration bins (6 h, by score):

| score bin | ≤ 0.05 | 0.05–0.15 | 0.15–0.3 | 0.3–0.6 | > 0.6 |
|---|---|---|---|---|---|
| predicted move (training) | 0.00 | +0.32 | +0.08 | +0.33 | −0.10 |
| realised move (test) | +0.23 | +0.16 | +0.14 | +0.64 | +0.44 |

The ordering does not hold (test Spearman −0.02).

### 4. SIG test days (23–24 Oct)

| horizon | n | SIG beyond catch-up, Haiku direction | outside move, Haiku direction (ex-Alaska) | bot's Opus direction: outside / SIG | net P&L ticks/share |
|---|---|---|---|---|---|
| 15 min | 384 | −0.02 (t −2.3) | +0.94 (+0.02) | −1.32 / −0.04 | −1.18 |
| 1 h | 377 | −0.11 (t −3.4) | +0.85 (+0.03) | −1.17 / −0.12 | −1.33 |
| 6 h | 283 | −0.00 (t −0.0) | +0.70 (+0.02) | −0.96 / −0.95 | −0.92 |

## Reproduce

* Fetch and label with the system Python:
  * `python3 -I research_notes/news_history_study.py fetch`
  * `python3 -I research_notes/news_history_study.py label`
* `analyse` and `plot` need numpy, pandas, scipy, scikit-learn and matplotlib. The system Python has none of them, so a
  throwaway virtualenv was used.
* The network here needs the bot's SSL context (`sigbot.api._ssl_context`). The script uses it.
* Wayback fetches take about 30 min, with one stream per feed. Everything is cached.
