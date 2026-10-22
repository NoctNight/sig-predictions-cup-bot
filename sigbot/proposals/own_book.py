"""Take our own resting orders out of the books the arb scanner walks (paper helper).

Problem (hft_strategies.md, Fable check): 17 of 33 failed sell_set baskets in `arb_trades` (22 Oct) priced a leg off a
best bid that was our own tilt bid (Delaware Senate YES 0.92/0.925, 12 failures in 14:07-14:14 alone). The venue does
not match us against ourselves, so the all-or-none basket fails and the race cools down for 60 s; 7 more re-sent the same
basket within 30 s of our own fill, on a book that still showed the size we had just taken.

Wiring (main session): keep `own = OwnLevels()` updated from the account feed (orderUpdates: open/remaining/cancel) or from
the open-order list the bot already holds, then in the scanner, before `arb.chunks_for_race(race, books)`:
    books = {e: own.net(e, b) for e, b in books.items()}
and in `candidates_from_prices` use `own.net_best(ex, bid, ask, book)`. After an executed basket, mark its races with
`own.just_filled(race, ts)` and skip a race until a book with asOf newer than the fill arrives.

    python3 -m sigbot.proposals.own_book --backtest     # failed baskets whose leg price had one of our non-arb orders resting
"""
from __future__ import annotations

import argparse, collections, json, os, sqlite3, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def yes_level(side: str, action: str, price: float):
    """Map an order to the YES book: ('bids'|'asks', yes_price). Prices are side-denominated (NO orders at the NO price)."""
    if side == "yes":
        return ("bids", round(price, 4)) if action == "buy" else ("asks", round(price, 4))
    return ("asks", round(1 - price, 4)) if action == "buy" else ("bids", round(1 - price, 4))


class OwnLevels:
    def __init__(self):
        self.orders = {}          # order_id -> (exchange_id, book side, yes price, remaining)
        self.filled_at = {}       # race -> ts of our last executed basket

    def upsert(self, order_id, exchange_id, side, action, price, remaining):
        if not remaining or remaining <= 0:
            self.orders.pop(order_id, None)
            return
        bs, p = yes_level(side, action, price)
        self.orders[order_id] = (str(exchange_id), bs, p, float(remaining))

    def remove(self, order_id):
        self.orders.pop(order_id, None)

    def levels(self, exchange_id):
        out = {"bids": collections.Counter(), "asks": collections.Counter()}
        for ex, bs, p, q in self.orders.values():
            if ex == str(exchange_id):
                out[bs][p] += q
        return out

    def net(self, exchange_id, book: dict) -> dict:
        """book: {'bids': [{'price','quantity'}], 'asks': [...]} (feed/REST shape). Returns a copy without our size."""
        lv = self.levels(exchange_id)
        if not lv["bids"] and not lv["asks"]:
            return book
        nb = dict(book)
        for side in ("bids", "asks"):
            rows = []
            for l in book.get(side) or []:
                q = l["quantity"] - lv[side].get(round(l["price"], 4), 0)
                if q > 1e-9:
                    rows.append(dict(l, quantity=q))
            nb[side] = rows
        nb["bestBid"] = nb["bids"][0]["price"] if nb.get("bids") else None
        nb["bestAsk"] = nb["asks"][0]["price"] if nb.get("asks") else None
        return nb

    def just_filled(self, race, ts=None):
        self.filled_at[race] = ts or time.time()

    def stale(self, race, book_asof_ts) -> bool:
        """True while no book newer than our last fill in this race has arrived."""
        return book_asof_ts is not None and book_asof_ts <= self.filled_at.get(race, -1)


def backtest():
    c = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'sigbot.sqlite')}?mode=ro", uri=True)
    fails = c.execute("select ts, race, legs, response from arb_trades where kind='sell_set' and ok=0").fetchall()
    fails = [f for f in fails if "dry_run" not in (f[3] or "")]
    oks = c.execute("select ts, race, quantity from arb_trades where ok=1").fetchall()
    own_hits = resend = 0
    for ts, race, legs, _ in fails:
        legs = json.loads(legs)
        hit = False
        for l in legs:
            ex, yes_bid = str(l["exchangeId"]), round(1 - l["price"], 4)     # sell_set leg = buy NO at 1 - YES bid
            for side, action, price, placed, exp, status in c.execute(
                    "select side, action, price, placed_at, expires_at, status from orders where exchange_id=? and purpose!='arb' "
                    "and placed_at between ? and ?", (ex, ts - 6 * 3600, ts)):
                if price is None or status == "filled":
                    continue
                bs, p = yes_level(side, action, price)
                if bs == "bids" and abs(p - yes_bid) < 1e-6:
                    hit = True
        own_hits += hit
        resend += any(r == race and 0 < ts - t <= 30 for t, r, q in oks)
    print(f"failed sell_set baskets {len(fails)}: a non-arb order of ours was placed at a leg's priced bid in the 6 h before "
          f"(upper bound; cancel times are not logged) {own_hits}; re-sent within 30 s of our own fill on the race {resend}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    if ap.parse_args().backtest:
        backtest()
