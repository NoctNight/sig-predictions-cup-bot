"""SQLite store for everything the bot observes and does.

Tables
------
fills            every fill on our account (from /portfolio/fills), enriched with our own
                 context at placement time (purpose, level, fair, mid, imbalance) when known.
orders           every order we placed (purpose = mm | arb | hedge), with level metadata.
quotes           one row per (cycle, exchange): bid/ask/mid/imbalance/fair/inventory snapshot;
                 used for markouts and for classifying tape trades as buyer/seller initiated.
tape             public trades per exchange (from /exchanges/{id}/trades), deduplicated by id.
arb_trades       each executed set arb: legs, qty, edge, cost and the raw response.
cycles           per-cycle account snapshot (cash, positions value, counts, api stats).
params           key/value JSON (e.g. computed x, mm overrides, news_pause) with timestamps.
news_items       every headline seen by the news sweeper (deduplicated), with the LLM screen / final JSON.
news_events      every classified event that cleared the thresholds: quotes before, the action taken, mids afterwards.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY,
  order_id INTEGER,
  exchange_id TEXT NOT NULL,
  market_id TEXT,
  side TEXT NOT NULL,            -- yes|no as reported by the API (our resulting position side)
  price REAL,                    -- YES-denominated price
  quantity REAL NOT NULL,
  filled_at TEXT NOT NULL,
  purpose TEXT,                  -- mm | arb | hedge | manual (NULL if unknown)
  level INTEGER,                 -- ladder level index (1 = closest to fair) for mm orders
  ticks_from_mid REAL,           -- distance of the resting price from mid at placement, in ticks
  fair_at_place REAL,
  mid_at_place REAL,
  imbalance_at_place REAL,
  inventory_before REAL,
  ingested_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_ex_time ON fills(exchange_id, filled_at);

CREATE TABLE IF NOT EXISTS orders (
  order_id INTEGER PRIMARY KEY,
  exchange_id TEXT NOT NULL,
  market_id TEXT,
  side TEXT NOT NULL,
  action TEXT NOT NULL,
  price REAL,
  quantity INTEGER NOT NULL,
  purpose TEXT NOT NULL,
  level INTEGER,
  ticks_from_mid REAL,
  fair REAL,
  mid REAL,
  imbalance REAL,
  inventory REAL,
  cycle_id INTEGER,
  placed_at REAL NOT NULL,
  expires_at TEXT,
  status TEXT DEFAULT 'open',
  response TEXT
);
CREATE INDEX IF NOT EXISTS orders_ex ON orders(exchange_id, placed_at);

CREATE TABLE IF NOT EXISTS quotes (
  ts REAL NOT NULL,
  cycle_id INTEGER,
  exchange_id TEXT NOT NULL,
  best_bid REAL, best_ask REAL, bid_qty REAL, ask_qty REAL,
  last REAL, mid REAL, microprice REAL, imbalance REAL,
  fair REAL, inventory REAL, pressure REAL,
  PRIMARY KEY (ts, exchange_id)
);
CREATE INDEX IF NOT EXISTS quotes_ex_ts ON quotes(exchange_id, ts);

CREATE TABLE IF NOT EXISTS tape (
  trade_id TEXT PRIMARY KEY,
  exchange_id TEXT NOT NULL,
  market_id TEXT,
  price REAL NOT NULL,
  quantity REAL NOT NULL,
  executed_at TEXT NOT NULL,
  sequence INTEGER,
  aggressor TEXT,                -- buy | sell | NULL (classified against our last quote snapshot)
  ingested_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tape_ex_time ON tape(exchange_id, executed_at);

CREATE TABLE IF NOT EXISTS arb_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  race TEXT NOT NULL,
  kind TEXT NOT NULL,            -- sell_set | buy_set
  legs TEXT NOT NULL,            -- JSON
  quantity INTEGER NOT NULL,
  edge_per_set REAL NOT NULL,
  cost_per_set REAL NOT NULL,
  ok INTEGER NOT NULL,
  response TEXT
);

CREATE TABLE IF NOT EXISTS cycles (
  cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  cash REAL, positions_value REAL, total_value REAL,
  n_positions INTEGER, n_open_orders INTEGER,
  reads INTEGER, writes INTEGER,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS params (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS news_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,      -- dedupe key: normalised URL, else a hash of source+title
  source TEXT, url TEXT, title TEXT, published TEXT, lead TEXT,
  first_seen REAL NOT NULL,
  prefilter INTEGER NOT NULL,    -- 1 if the keyword pre-filter kept it for the LLM screen
  screened INTEGER DEFAULT 0,    -- 0 pending, 1 screened, 2 final call done, -1 dropped (age / no key)
  screen_race TEXT, screen_json TEXT, final_json TEXT
);
CREATE INDEX IF NOT EXISTS news_items_seen ON news_items(first_seen);

CREATE TABLE IF NOT EXISTS news_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  item_id INTEGER,
  race TEXT NOT NULL, party TEXT NOT NULL, exchange_id TEXT,
  direction TEXT NOT NULL,       -- up | down (YES price of the party's market)
  expected_move_pts REAL, confidence REAL, reason TEXT, model TEXT,
  headline TEXT, source TEXT, url TEXT, published TEXT,
  ref_mid REAL,                  -- mid before the news (5m candle close before publication, else last quote, else mid_before)
  bid_before REAL, ask_before REAL, mid_before REAL,
  moved_pts REAL,                -- (mid_before - ref_mid) in points, signed in the predicted direction
  action TEXT NOT NULL,          -- traded | dry | skipped_moved | skipped_budget | skipped_race_cap | skipped_threshold | error
  side TEXT, price REAL, quantity INTEGER, cost REAL, order_id INTEGER, filled_qty REAL,
  dry_run INTEGER NOT NULL,
  mid_5 REAL, mid_30 REAL, mid_120 REAL,
  response TEXT
);
CREATE INDEX IF NOT EXISTS news_events_ts ON news_events(ts);
"""



class Store:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    # ----------------------------------------------------------------- utils
    def exec(self, sql: str, args: Iterable = ()):
        with self.lock:
            return self.conn.execute(sql, tuple(args))

    def execmany(self, sql: str, rows: Iterable[Iterable]):
        with self.lock:
            return self.conn.executemany(sql, [tuple(r) for r in rows])

    def rows(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    def one(self, sql: str, args: Iterable = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(args)).fetchone()

    # ---------------------------------------------------------------- params
    def set_param(self, key: str, value: Any):
        self.exec("INSERT INTO params(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                  (key, json.dumps(value), time.time()))

    def get_param(self, key: str, default: Any = None) -> Any:
        r = self.one("SELECT value FROM params WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    # ---------------------------------------------------------------- cycles
    def new_cycle(self, cash, positions_value, n_positions, n_open_orders, reads, writes, notes="") -> int:
        cur = self.exec(
            "INSERT INTO cycles(ts,cash,positions_value,total_value,n_positions,n_open_orders,reads,writes,notes) VALUES(?,?,?,?,?,?,?,?,?)",
            (time.time(), cash, positions_value, (cash or 0) + (positions_value or 0), n_positions, n_open_orders, reads, writes, notes))
        return cur.lastrowid

    # ---------------------------------------------------------------- orders
    def record_order(self, order_id: Optional[int], exchange_id: str, market_id: Optional[str], side: str, action: str,
                     price: Optional[float], quantity: int, purpose: str, level: Optional[int] = None,
                     ticks_from_mid: Optional[float] = None, fair: Optional[float] = None, mid: Optional[float] = None,
                     imbalance: Optional[float] = None, inventory: Optional[float] = None, cycle_id: Optional[int] = None,
                     expires_at: Optional[str] = None, status: str = "open", response: Any = None):
        if order_id is None:
            return
        self.exec(
            "INSERT OR REPLACE INTO orders(order_id,exchange_id,market_id,side,action,price,quantity,purpose,level,ticks_from_mid,fair,mid,imbalance,inventory,cycle_id,placed_at,expires_at,status,response) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, str(exchange_id), market_id, side, action, price, quantity, purpose, level, ticks_from_mid, fair, mid,
             imbalance, inventory, cycle_id, time.time(), expires_at, status, json.dumps(response) if response is not None else None))

    def mark_orders(self, order_ids: Iterable[int], status: str):
        self.execmany("UPDATE orders SET status=? WHERE order_id=?", [(status, oid) for oid in order_ids])

    def order_meta(self, order_id: Optional[int]) -> Optional[sqlite3.Row]:
        if order_id is None:
            return None
        return self.one("SELECT * FROM orders WHERE order_id=?", (order_id,))

    # ----------------------------------------------------------------- fills
    def ingest_fills(self, fills: list[dict]) -> int:
        """Insert fills we have not seen; enrich with order metadata. Returns number inserted."""
        n = 0
        for f in fills:
            if self.one("SELECT 1 FROM fills WHERE id=?", (f["id"],)):
                continue
            meta = self.order_meta(f.get("orderId"))
            # the fills endpoint reports the price in the fill's own side terms; store it YES-denominated
            price = f.get("price")
            if price is not None and f["side"] == "no":
                price = round(1.0 - float(price), 4)
            self.exec(
                "INSERT INTO fills(id,order_id,exchange_id,market_id,side,price,quantity,filled_at,purpose,level,ticks_from_mid,fair_at_place,mid_at_place,imbalance_at_place,inventory_before,ingested_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f["id"], f.get("orderId"), str(f["exchangeId"]), str(f.get("marketId")) if f.get("marketId") is not None else None,
                 f["side"], price, f["quantity"], f["filledAt"],
                 meta["purpose"] if meta else None, meta["level"] if meta else None, meta["ticks_from_mid"] if meta else None,
                 meta["fair"] if meta else None, meta["mid"] if meta else None, meta["imbalance"] if meta else None,
                 meta["inventory"] if meta else None, time.time()))
            n += 1
        return n

    def last_fill_id(self) -> int:
        r = self.one("SELECT MAX(id) AS m FROM fills")
        return int(r["m"]) if r and r["m"] is not None else 0

    # ---------------------------------------------------------------- quotes
    def record_quote(self, cycle_id: int, exchange_id: str, best_bid, best_ask, bid_qty, ask_qty, last, mid, microprice,
                     imbalance, fair, inventory, pressure):
        self.exec(
            "INSERT OR REPLACE INTO quotes(ts,cycle_id,exchange_id,best_bid,best_ask,bid_qty,ask_qty,last,mid,microprice,imbalance,fair,inventory,pressure) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), cycle_id, str(exchange_id), best_bid, best_ask, bid_qty, ask_qty, last, mid, microprice, imbalance, fair,
             inventory, pressure))

    def last_quote(self, exchange_id: str, before_ts: Optional[float] = None) -> Optional[sqlite3.Row]:
        if before_ts is None:
            return self.one("SELECT * FROM quotes WHERE exchange_id=? ORDER BY ts DESC LIMIT 1", (str(exchange_id),))
        return self.one("SELECT * FROM quotes WHERE exchange_id=? AND ts<=? ORDER BY ts DESC LIMIT 1", (str(exchange_id), before_ts))

    # ------------------------------------------------------------------ tape
    def ingest_tape(self, exchange_id: str, trades: list[dict]) -> int:
        """Tape rows look like {id, createdAt, price, size, side: YES|NO, volume}. `side` is only the space the fill
        was recorded in, not the aggressor, so the aggressor is classified (a) against our most recent quote snapshot
        taken within 10 minutes before the trade, else (b) by a tick test against the previous trade on the exchange."""
        n = 0
        rows = sorted((t for t in trades if t.get("price") is not None and (t.get("size") or t.get("quantity")) is not None
                       and (t.get("createdAt") or t.get("executedAt"))), key=lambda t: t.get("createdAt") or t.get("executedAt"))
        prev = self.one("SELECT price, aggressor FROM tape WHERE exchange_id=? ORDER BY executed_at DESC LIMIT 1", (str(exchange_id),))
        prev_price = prev["price"] if prev else None
        prev_aggr = prev["aggressor"] if prev else None
        for t in rows:
            qty = t.get("size", t.get("quantity"))
            when = t.get("createdAt") or t.get("executedAt")
            tid = str(t.get("id") or f"{exchange_id}:{t.get('sequence')}:{when}:{t.get('price')}:{qty}")
            if self.one("SELECT 1 FROM tape WHERE trade_id=?", (tid,)):
                prev_price = t["price"]
                continue
            aggressor = None
            try:
                ts = _iso_to_ts(when)
                q = self.last_quote(exchange_id, ts)
                if q and q["ts"] >= ts - 600 and q["best_bid"] is not None and q["best_ask"] is not None:
                    if t["price"] >= q["best_ask"]:
                        aggressor = "buy"
                    elif t["price"] <= q["best_bid"]:
                        aggressor = "sell"
            except Exception:
                pass
            if aggressor is None and prev_price is not None:
                if t["price"] > prev_price:
                    aggressor = "buy"
                elif t["price"] < prev_price:
                    aggressor = "sell"
                else:
                    aggressor = prev_aggr
            self.exec("INSERT OR IGNORE INTO tape(trade_id,exchange_id,market_id,price,quantity,executed_at,sequence,aggressor,ingested_at) VALUES(?,?,?,?,?,?,?,?,?)",
                      (tid, str(exchange_id), str(t.get("marketId")) if t.get("marketId") is not None else None, t["price"], qty,
                       when, t.get("sequence"), aggressor, time.time()))
            prev_price, prev_aggr = t["price"], aggressor
            n += 1
        return n

    def latest_tape_time(self, exchange_id: str) -> Optional[str]:
        r = self.one("SELECT MAX(executed_at) AS m FROM tape WHERE exchange_id=?", (str(exchange_id),))
        return r["m"] if r else None

    # ------------------------------------------------------------------- arb
    def record_arb(self, race: str, kind: str, legs: list[dict], quantity: int, edge: float, cost: float, ok: bool, response: Any):
        self.exec("INSERT INTO arb_trades(ts,race,kind,legs,quantity,edge_per_set,cost_per_set,ok,response) VALUES(?,?,?,?,?,?,?,?,?)",
                  (time.time(), race, kind, json.dumps(legs), quantity, edge, cost, int(ok), json.dumps(response)[:20000]))

    # ------------------------------------------------------------------ news
    def news_item_exists(self, key: str) -> bool:
        return self.one("SELECT 1 FROM news_items WHERE key=?", (key,)) is not None

    def add_news_item(self, key: str, source: str, url: str, title: str, published: Optional[str], lead: Optional[str],
                      prefilter: bool) -> int:
        cur = self.exec("INSERT OR IGNORE INTO news_items(key,source,url,title,published,lead,first_seen,prefilter,screened) VALUES(?,?,?,?,?,?,?,?,?)",
                        (key, source, url, title, published, lead, time.time(), int(prefilter), 0 if prefilter else -1))
        if cur.lastrowid:
            return cur.lastrowid
        return self.one("SELECT id FROM news_items WHERE key=?", (key,))["id"]

    def pending_news_items(self, limit: int = 200) -> list[sqlite3.Row]:
        return self.rows("SELECT * FROM news_items WHERE screened=0 AND prefilter=1 ORDER BY first_seen ASC LIMIT ?", (limit,))

    def set_news_screen(self, item_id: int, race: Optional[str], screen: Any, status: int = 1):
        self.exec("UPDATE news_items SET screened=?, screen_race=?, screen_json=? WHERE id=?", (status, race, json.dumps(screen), item_id))

    def set_news_final(self, item_id: int, final: Any):
        self.exec("UPDATE news_items SET screened=2, final_json=? WHERE id=?", (json.dumps(final), item_id))

    def record_news_event(self, **f) -> int:
        cols = ["ts", "item_id", "race", "party", "exchange_id", "direction", "expected_move_pts", "confidence", "reason", "model",
                "headline", "source", "url", "published", "ref_mid", "bid_before", "ask_before", "mid_before", "moved_pts", "action",
                "side", "price", "quantity", "cost", "order_id", "filled_qty", "dry_run", "response"]
        f.setdefault("ts", time.time())
        if f.get("response") is not None and not isinstance(f["response"], str):
            f["response"] = json.dumps(f["response"])[:20000]
        cur = self.exec(f"INSERT INTO news_events({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [f.get(c) for c in cols])
        return cur.lastrowid

    def news_events_pending_followup(self, col: str, min_age_sec: float) -> list[sqlite3.Row]:
        assert col in ("mid_5", "mid_30", "mid_120")
        return self.rows(f"SELECT * FROM news_events WHERE {col} IS NULL AND exchange_id IS NOT NULL AND ts<=?", (time.time() - min_age_sec,))

    def set_news_followup(self, event_id: int, col: str, mid: float):
        assert col in ("mid_5", "mid_30", "mid_120")
        self.exec(f"UPDATE news_events SET {col}=? WHERE id=?", (mid, event_id))

    def news_open_cost(self) -> float:
        r = self.one("SELECT COALESCE(SUM(cost),0) AS c FROM news_events WHERE action='traded'")
        return float(r["c"]) if r else 0.0

    def news_events_for_race_since(self, race: str, since_ts: float) -> int:
        r = self.one("SELECT COUNT(*) AS n FROM news_events WHERE race=? AND ts>=? AND action IN ('traded','dry')", (race, since_ts))
        return int(r["n"]) if r else 0


def _iso_to_ts(s: str) -> float:
    from datetime import datetime, timezone

    s = s.replace("Z", "+00:00")
    return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
