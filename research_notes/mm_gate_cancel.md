# Market maker: resting quotes on a side the gate just closed (29 Oct, two holes)

Diff: `research_notes/mm_gate_cancel_wiring.diff` (`git diff -- sigbot tests` against 9a61a39, the live tree's head on 29 Oct, the gap
pilot commit). Files: `sigbot/mm.py`, `sigbot/run.py`, `tests/test_guards.py` (blocks "mm gate-pull" and "mm gate-sweep"). Not deployed
by this session. Two Opus reviews: review 1 NOT SAFE (one blocker, fixed), review 2 below.

## Hole 1: an empty ladder skipped the cancel

`quote_cycle` (mm.py) evaluated each quoted leg as: `build_quotes` -> `if not quotes: continue` -> `record_quote` -> `needs_requote` ->
write budget -> last look -> `cancel_all(exchange_id=ex)` -> post. Two gates zero a side of the ladder inside `build_quotes`: the
portfolio partisan block (a blocked party zeroes the side that adds to it) and the outside-fair gate (`xv_gate`: with the fresh
Polymarket/Kalshi fair >= 3 ticks below the mid no bids, above no asks). When the partisan block left one side and the outside fair
then closed that side too, `build_quotes` returned nothing and the leg was skipped BEFORE its cancel: the earlier quotes (1,800-s TTL)
stayed on the book on the side the gate blocks. The same `quote_cycle` runs in the scanner thread's `fast_requote_pass`.

Seen in the 24-h `day` sim on 29 Oct once the gap pilot changed the bot's path (a clean head passed by path luck): leg 872 (Nevada
Governor R). 10:05 sim time D blocked, bids only (0.235 x375, 0.23 x187); 10:20 outside fair 0.2352 -> 0.213; from 10:22
`xv_gate(mid 0.2375, fair 0.213)` blocks the bid side; six cycles with no quotes row and no action on the leg; 10:23:15 a crowd sell
takes the 0.235 level (x27), 10:24 the rest (x348): `mm_filled_on_gated_side`. A probe with the real `MarketMaker` confirmed it: with
the asks open a gate flip gives `cancel_all('872')` plus a requote; with D blocked the same flip made zero API calls.

## Hole 2: the poll lands after the MM block

The main cycle quotes (MM block) BEFORE the directional sleeve polls Polymarket/Kalshi; `mm.xv_fair` is refreshed right after the poll
(run.py "outside fair for the MM's gate, refreshed right after the directional poll"), so each cycle the MM quotes against the previous
poll's fair, and when a poll moves the fair past the gate the quotes rest a whole cycle (30-60 s) on the newly gated side. Nothing acted
on it between cycles: the fast path only watches touch moves. Found by the main session's deploy precheck, which failed `catchup`
deterministically on the live code (97b841d) with the live params: leg 1067 (Alaska Senate R): the outside fair moved 3 pts at 15:14:30
sim time; cycle 151 at 15:15:00 quoted with the old fair (0.304) and then polled the new one (0.274, 5.7 ticks under the mid); a crowd
sell hit the resting 0.30 bid at 15:15:15; the MM reacted at cycle 152, 15:15:30 (traced with a sandbox patch on the real run).

## The fix

Hole 1, `quote_cycle` when `build_quotes` returns no quotes:
- `info` empty (a side of the book is missing, the mid unknown): `continue` as before. Nothing is ever cancelled blind.
- book known: write the quotes row (the vol-regime reference mids stay continuous); `mine = own_resting(ex, oo, dedupe)`: our orders
  (store purpose `mm` / `inside`) among the venue's open orders on the leg; if any, pull them (`pull_quotes`), respecting the write
  budget (`skipped_budget`, next cycle otherwise); record the state (`last_gate`, `last_reserve`, `last_quote_ts`, `fills_seen`,
  `last_touch`) ONLY when the pull was clean, so the re-open is a gate change `needs_requote` fires on. While a quote of ours may
  still rest (a failed or partial pull) nothing is recorded (review 1's blocker: a recorded closed gate plus resting quotes let a
  later re-open with the same gate skip the requote the old code would have made).
- `needs_requote(..., stale_side)`: a quote of ours resting on a side the ladder no longer has (the gate, the partisan block or the
  inventory cap closed it) forces a requote whatever the recorded state. This is the backstop for every re-open path.
- `own_resting`: with `dedupe` (the caller's list may be older than its own actions: `force_cancel` set, i.e. the fast path's cache or
  the gate sweep) orders the store marks cancelled are dropped (a cancel is not repeated from a stale list) and our store-open orders
  on the leg placed since `open_orders_ts` (this cycle's quotes, which the list lacks) are added. A fresh list (main cycle) is trusted
  as it is, so a cancel-all that answered OK but left an order resting is retried.
- `pull_quotes`: one `cancel_all(exchange_id=ex)` (the requote path's cancel) when every order the venue lists on the leg is ours and
  no other sleeve placed an order there since the list was read (the `newer` store check `cancel_mm_quotes` makes); otherwise order by
  order (`cancel_order`) for ours only, so an exit, a catch-up entry or an order the store does not know is left alone. Any exception
  is logged (the API client re-raises URLError / ConnectionError after its retries) and the pull is retried next cycle; dry run logs.
- `fast_requote_pass` (run.py): a pull counts in `fast_log` like a requote (it is a write), `stats["fast_pulls"]`, one log line.

Hole 2, the gate sweep (`Bot.mm_gate_sweep` -> `mm.pull_closed`), wired right after the post-poll `xv_fair` refresh, only in a cycle
that polled, with the MM, inside and tilt writes and the directional sleeve's 2 taken off `mm_write_budget` (cap 8):
- `gate_for(ex, mid)` is now the one xv gate, used by `build_quotes` and by `gate_closed(books)`: the quoted legs on which the fresh
  fair, at this cycle's book mid, CLOSED a side that was open when their quotes were built (`st.last_gate`). A side that only opened
  is left to the next cycle's requote.
- `pull_closed`: on each such leg, pull our quotes on the closed side(s) now: one cancel-all when the leg is all ours (the next cycle
  re-posts the open side), otherwise only our orders on the closed side, one by one, and the open side keeps resting. Nothing is
  posted by the sweep: posting on the just-polled fair with the cycle's books is left to the next cycle (and the sim's checker judges
  posts against the fair it saw before the cycle, so a posting sweep showed up as `mm_posted_on_gated_side` x7 in catchup; cancel-only
  is also the conservative choice live). `st.last_gate = new` when clean. Under the trade lock, as a fast requote.
- In the catchup run the sweep pulled the closed side on 896, 1066 and 1067 at 15:15:00 with 3 writes (the catch-up sleeve had just
  placed a resting order on each leg, so cancel-all was refused and the closed side went order by order).

Review 2's should-fixes (the final version):
- `mark_ours_cancelled(ex)` after every successful exchange-scoped cancel-all (the requote path and the pull's cancel-all): every
  order of ours the store still holds open on the leg is off the venue now, listed or not (this cycle's quotes, an earlier fast
  requote's), so they are all marked; before, the unlisted ones stayed "open" as phantoms a later sweep or fast pull spent its
  cancels on.
- the cancel-all test in `pull_quotes` counts a listed order as ours when its store purpose is mm/inside whatever its status, so a
  cycle's list that still holds the previous ladder's (cancelled) ids no longer forces the pull order by order (1 write, not 5).
- a cancel answered 404 / 409 / 410 (the venue has no open order by that id: filled, expired or cancelled since) marks the order
  cancelled and counts as clean; any other exception leaves it open and the pull not clean (retried next cycle).
`research_notes/mm_gate_cancel_v6_on_v5.diff` is this step alone, for a tree that already has the reviewed first version applied.

## Tests (tests/test_guards.py, real `build_quotes`, real sqlite store, stubbed venue, leg 872)

"mm gate-pull": control (D blocked -> bids only, gate open); (a) the fair 4.9 ticks under the mid: one cancel-all, nothing posted, one
write, the closed gate recorded, a quotes row; (e) a stale list with `force_cancel` does not repeat the cancel, a fresh list that still
shows the order is trusted (pulled again); (c) the recorded gate makes the re-open a requote trigger on its own, the bids come back;
(b) a side of the book missing or no book: no cancel, nothing recorded; (d) an exit and an unknown order beside ours: ours one by one;
(f) no write budget: skipped, nothing recorded, pulled next cycle; a failed cancel-all (ConnectionError): logged, counted, nothing
recorded, retried next cycle; dry run; the inventory cap plus the block pulls too; fast path: a pull counted against the per-minute
budget, the touch remembered; `force_cancel` never cancels a leg where nothing of ours rests. Review 1: `needs_requote(stale_side)`;
P1/P2 a fast pass whose cache misses this cycle's bids pulls them through the store; P2 a recorded closed gate with the bids still
resting, the block lifting: cancel-all, asks only (the stale-side trigger); P9 a failed pull then the block lifting: requote; P10 a
pull cut short by the budget: nothing recorded, finished next cycle, the exit untouched; P3 a non-MM order placed on the leg since
the read: order by order, an old one: cancel-all.
"mm gate-sweep": `gate_for` (xv_gate at the mid; open with no fair, the gate off, a stale fair); `gate_closed` (a closed side, not an
unknown mid, not a leg forced to requote, not one never quoted, not a side that only opened); `pull_closed` on the real MM (the list
predates the quotes: found in the store, one cancel-all, nothing posted, the gate recorded, a second sweep finds nothing, the next cycle
posts the open side only); an exit beside ours: ours one by one, no budget: reported; a catch-up order placed since the read with
both sides quoted: only the bids go, the asks keep resting and the next cycle leaves them alone; `Bot.mm_gate_sweep` under the trade
lock on a stub; the wiring (after the post-poll refresh, only after a poll, the budget arithmetic, cap 8).
Review 2: a requote's cancel-all marks this cycle's earlier quotes too; a sweep on a leg requoted this cycle is one cancel-all; a 409
on a cancel marks the order gone and the pull clean, a 500 does not.
Guard suite: ALL PASS (599). `tests/test_gap_failure_paths.py`: ALL PASS.

## Sims (deploy/simq.sh at nice 5, the live config at 9a61a39 with the gap pilot on, the live tree's params snapshot)

Three versions ran: v1 (hole 1 only), v5 (both holes, review 2's code), v6 (v5 plus review 2's should-fixes, the delivered diff).
Every run below used the live tree's params snapshot and `data/race_map.json` (the precheck's own inputs; the catchup failure only
reproduces with them) and the untracked data files copied from the live tree.
- Short sims (v1): calm, sweep6, outside_move, slow_writes: 0 violations.
- Full precheck line (every scenario but day), v1: 40 scenarios, 1 violation: catchup `mm_filled_on_gated_side` x1 (hole 2, leg
  1067, traced). churn_blowout passes with the live params (the earlier note of it failing was on the tracked params).
- catchup, v3 (a sweep that re-posted the open side): the hole-2 fill gone, but `mm_posted_on_gated_side` x7: the checker judges
  posts against the fair it snapshotted before the cycle. v4 (cancel-only sweep, all of ours, cap 6): still the x1 fill: three legs
  closed at once with a catch-up order fresh on each, 3 writes per leg order by order, the budget ran out before 1067. v5 (closed
  side only, cap 8): 0 violations, 3 legs pulled with 3 writes.
- Full precheck line, v5: 40 scenarios in 1,045 s, 0 violations.
- day (24 h), v1 on the tracked params: 0 violations (1,141 s wall); with the pull counter: 131 empty-ladder pulls, all cancel-all.
- day, v5 on the live params: 0 violations (293 s wall); the sweep evaluated 1,199 polled cycles, pulled 19 legs with 22 writes, no
  budget skips; the empty-ladder branch pulled 148 times (151 writes); every pull clean.
- catchup, v6: 0 violations; 159 sweep evaluations, 3 legs pulled, 3 writes, all clean.
- Full precheck line, v6 (the delivered diff): 40 scenarios in 329 s, 0 violations.
- day, v6: 0 violations (263 s wall); 1,199 sweep evaluations, 19 legs pulled with 19 writes (22 in v5: the cancel-all eligibility
  fix), no budget skips; 148 empty-ladder pulls with 148 writes (151 in v5); every pull clean. Same P&L and fills as v5.

## Review

Review 1 (Opus 5.5, high effort, fresh; `mm-gate-cancel-review-1.md` in the side-session notes): NOT SAFE. Blocker: the empty-ladder
branch recorded the closed gate even when the pull left quotes resting (a stale fast-path cache, a failed cancel, a partial pull), so a
later re-open with the same gate could skip the requote the old code made. Should-fixes: the fast path could not pull quotes posted by the
current main cycle; the cancel-all decided "all ours" from a snapshot; `except ApiError` only. All fixed (see "The fix") with guards for
each reproduction.
Review 2 (Opus 5.5, high effort, fresh; `mm-gate-cancel-review-2.md`): SAFE TO DEPLOY. Review 1's blocker closed (its probes re-run,
mutation runs of the fix fail the guards they should). Should-fixes left: (1) a requote marks only the listed orders cancelled, so quotes
the main cycle posted earlier in the same cycle (then cancelled by a fast requote) stay "open" in the store and a later sweep spends its
budget on cancels of phantoms; (2) the cancel-all test counts our own already-cancelled ids in the cycle's list as foreign, so a sweep on
a leg requoted this cycle goes order by order (5 writes, not 1). Notes: a 4xx answer to a cancel should mark the order gone; a failed
open-orders read makes the cancel-all eligible by default; after a cancel-all sweep foreign orders can hide the re-post (quoting time,
not risk); the sweep measures the gate at the cycle-start book's mid. The reviewer's bottom line: nothing makes the live bot worse than
the base code; the should-fixes make the sweep effective in fast markets. Landed (see "Review 2's should-fixes").
Review 3 (Opus 5.5, high effort, fresh; `mm-gate-cancel-review-3.md`): SAFE TO DEPLOY. The three fixes verified against the venue's
cancel endpoint spec (404 not found / other account, 409 already closed, 410 archived; no "try later" 409), every `status='open'`
reader of mm/inside rows checked (the mark is more accurate for all of them; nothing in P&L, attribution, analytics, the tracker or the
dashboard filters orders on status), review 2's probes re-run (a sweep on a leg requoted this cycle is 1 cancel-all, was 5 writes; no
phantom cancels; a fast pass pulls all of this cycle's gated quotes), mutation runs fail the guards they should. Notes only: a guard gap
(now closed: the old open row of ours in guard (2)), a cross-thread window of milliseconds in which a fast-pass quote could be marked
cancelled while resting (the base code double-ladders on the same interleaving; the next cycle's fresh list handles it), 404 taken at
its word, and review 2's notes 3 / 6 / 7 / 8 unchanged, none blocking a 29-30 Oct deploy; the sweep cannot run on election night (the
3 Nov 12:00 ET flat sets `mm_paused`).

## Notes for the deployer

- Precheck gate on this diff: guards ALL PASS (599), gap failure paths ALL PASS, the full line 40/40 with 0 violations, day 0 violations.
  `catchup` (the precheck failure the main session hit on the live code) passes.
- Not a config change; no new parameters. Two summary keys (`pulled` in the MM summary, `gate_sweep` on it) and one scanner stat
  (`fast_pulls`); log lines "MM pulled ...", "MM gate sweep ...", "fast pull ...", "pull cancel ... gone".
- Known limits (from the reviews, none blocking): a sweep leg whose closed side rests beside a foreign order placed since the read goes
  order by order (budget-bound; the rest next cycle); a failed open-orders read makes the cancel-all eligible by default (needs a
  foreign order older than 60 s on an MM leg); the sweep measures the gate at the cycle-start book's mid; the trade lock is held for
  the sweep's writes (mostly 1 per leg now). The MM is flat from 3 Nov 12:00 ET, so the sweep never runs on election night.

- The worktree lacked the untracked data files the sims read (`data/books.json`, `volume_cache.json`, `candles_5m.json`,
  `tapes.json`, `model_history.json`, `volatile_paper.jsonl`, `books_latest.json`, `research_contract_rules/tc_mapping.json`):
  copied from the live tree, not part of the diff.
- `summary["pulled"]` is a new key in the MM summary; `stats["fast_pulls"]` a new scanner stat. Log lines: "MM pulled N resting
  order(s) on EX: no quotable side (...)" and "fast pull EX: ...".
