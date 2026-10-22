"""Candidate fixes for the bot bugs tests/sim_pipeline.py finds, as monkeypatches of the SANDBOX copy only (sigbot/ is
never touched). Each block is a sketch of the minimal fix, to check that it removes the violations before porting it:

    python3 tests/sim_pipeline.py --patch=tests/sim_candidate_fixes.py              # all of them
    FIXES=lag,plan python3 tests/sim_pipeline.py --patch=tests/sim_candidate_fixes.py outside_move recycle

  lag       Bot.lag_cycle: exits never close more than the leg holds; entries sized against the per-leg cap net of what the
            leg already holds and re-check the partisan block as they add; expiry per order; cooldown after a failed order
  plan      ArbEngine.plan: capital freed by a recycle unwind is counted once (it is already in the cash passed in)
  gate      MarketMaker: requote a market when its outside-fair gate state changes
  gate0     MarketMaker: do not quote before the first outside fair exists (restart / first cycle)
  feed      Bot.feed_ok: the feed counts as healthy only if it spoke within 15 s (was 120 s)
  own       ArbScanner: net out our orders placed since the cycle's open-order read (from the store)
  backstop  Bot.cycle: the main-loop recycle runs only while the feed (and so the scanner's recycler) is down
"""
import os, time
from datetime import timedelta, timezone

R, A, MMm, risk = mods["run"], mods["arb"], mods["mm"], mods["risk"]
WHICH = set((os.environ.get("FIXES") or "lag,plan,gate,gate0,feed,own,backstop").split(","))

if "lag" in WHICH:
    def lag_cycle(self, positions, blocked):
        rows = getattr(self.direction, "rows", None) or []
        if not rows:
            return 0
        touch = {}
        for r in rows:
            ex = r.get("exchange_id")
            fb = self.fresh_book(ex) if ex else None
            if fb and fb.get("bids") and fb.get("asks"):
                touch[ex] = min(fb["bids"][0]["quantity"], fb["asks"][0]["quantity"])
        orders = self.lag.cycle(time.time(), rows, touch)
        cap = float(getattr(self.direction.cfg, "per_market_cap", 300.0))
        pos = dict(positions)
        done = 0
        for o in orders:
            ex, side, px = str(o["exchangeId"]), o["side"], float(o["price"])
            q = int(o["quantity"])
            p = pos.get(ex, 0.0)
            if o.get("reason") == "exit":
                q = min(q, int(max(0.0, -p if side == "yes" else p)))      # never close more than is held
                if q < 1:
                    continue
            else:
                used = max(abs(p) * px, float((getattr(self.direction, "held", None) or {}).get(ex, 0.0)))
                q = min(q, int(max(0.0, cap - used) / max(px, 0.005)))   # cap shared with what the leg already holds
                bl = risk.blocked_directions(risk.exposure(self.universe, pos, getattr(self, "latest_prices", ({}, 0))[0] or {}),
                                             self.cfg.mm.portfolio_max_partisan_notional) | set(blocked)
                if risk.adds(self.universe, ex, side) in bl or q < 10:
                    self.lag.open.pop(ex, None)
                    continue
            ttl = (R.datetime.now(timezone.utc) + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")   # per order
            try:
                resp = self.api.place_order(ex, side, "buy", q, px, expiration=ttl, key=self.api.new_key("lag"))
                self.store.record_order(resp.get("orderId"), ex, None, side, "buy", px, q, "exit" if o.get("reason") == "exit" else "direction", response=resp)
                if o.get("reason") != "exit" and ex in self.lag.open:
                    self.lag.open[ex]["qty"] = q
                pos[ex] = p + (q if side == "yes" else -q)
                done += 1
            except mods["api"].ApiError:
                self.lag.open.pop(ex, None)
                self.lag.cooldown[ex] = time.time() + self.lag.cooldown_sec          # no retry every cycle
        return done
    R.Bot.lag_cycle = lag_cycle

if "plan" in WHICH:
    _orig_budget_left = A.ArbEngine.budget_left
    _orig_plan = A.ArbEngine.plan

    def plan(self, candidate_books, cash, extra_budget=0.0):
        if extra_budget > 0:
            # the capital a recycle freed is already in `cash`: count it once
            self._extra = extra_budget
        try:
            return _orig_plan(self, candidate_books, cash, extra_budget)
        finally:
            self._extra = 0.0

    def budget_left(self, cash):
        return _orig_budget_left(self, cash - getattr(self, "_extra", 0.0))
    A.ArbEngine.plan = plan
    A.ArbEngine.budget_left = budget_left

if "gate" in WHICH:
    from sigbot.proposals.mm_upgrade import xv_gate
    _orig_nr = MMm.MarketMaker.needs_requote

    def needs_requote(self, ex, info, fills_on_ex, open_orders_on_ex, expected_orders):
        xv = getattr(self, "xv_fair", {}).get(ex)
        g = (True, True)
        if xv and self.cfg.xv_gate_ticks > 0:
            g = xv_gate(info["mid"], xv[0], xv[1], xv[2], time.time() - xv[3], gap_ticks=self.cfg.xv_gate_ticks)
        st = self.state.setdefault(ex, MMm.QuoteState(ex))
        st.gate_pending = g
        if st.last_reserve is not None and getattr(st, "gate", (True, True)) != g:
            return True
        return _orig_nr(self, ex, info, fills_on_ex, open_orders_on_ex, expected_orders)
    MMm.MarketMaker.needs_requote = needs_requote
    _orig_qc0 = MMm.MarketMaker.quote_cycle

    def quote_cycle_g(self, *a, **k):
        summ = _orig_qc0(self, *a, **k)
        for ex in summ.get("requoted", []):          # the gate in force is the one the market was actually requoted under
            st = self.state.get(ex)
            if st is not None:
                st.gate = getattr(st, "gate_pending", (True, True))
        return summ
    MMm.MarketMaker.quote_cycle = quote_cycle_g

if "gate0" in WHICH:     # do not quote before the first outside poll has produced a fair (restart window)
    _orig_qc = MMm.MarketMaker.quote_cycle

    def quote_cycle(self, *a, **k):
        if not getattr(self, "xv_fair", None):
            return {"requoted": [], "orders": 0, "writes": 0, "skipped_budget": 0}
        return _orig_qc(self, *a, **k)
    MMm.MarketMaker.quote_cycle = quote_cycle

if "feed" in WHICH:
    R.Bot.feed_ok = lambda self: self.feed is not None and self.feed.healthy(15)

if "own" in WHICH:
    _Own = R.OwnLevels
    _orig_one = R.ArbScanner.one_pass
    _bot = {}

    class OwnFromStore(_Own):
        def __init__(self):
            super().__init__()
            b = _bot.get("b")
            if b is not None:     # orders our sleeves placed after the cycle's open-order read
                for r in b.store.rows("SELECT order_id, exchange_id, side, action, price, quantity FROM orders WHERE status='open' "
                                      "AND purpose NOT IN ('arb') AND placed_at>=?", (time.time() - 120,)):
                    self.upsert(r["order_id"], r["exchange_id"], r["side"], r["action"], r["price"], r["quantity"])

    def one_pass(self):
        _bot["b"] = self.bot
        return _orig_one(self)
    R.OwnLevels = OwnFromStore
    R.ArbScanner.one_pass = one_pass

if "backstop" in WHICH:   # main-loop recycle only when the scanner's feed path is down; it refreshes the snapshot either way
    _orig_cycle = R.Bot.cycle
    def cycle(self):
        if self.feed_ok() and self.scanner is not None:
            self.scanner.best_unfunded_saved, self.scanner.best_unfunded = self.scanner.best_unfunded, None
            try:
                return _orig_cycle(self)
            finally:
                if self.scanner.best_unfunded is None:
                    self.scanner.best_unfunded = self.scanner.best_unfunded_saved
        return _orig_cycle(self)
    R.Bot.cycle = cycle
