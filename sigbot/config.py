"""Bot configuration. Defaults live here; `config.json` next to the package overrides any field.

Capital model (100,000 SUSQies, cash account, no margin):
  * arb_capital_frac      share of cash the set-arb engine may tie up (riskless, settles Nov 4)
  * mm_capital_frac       share of cash the market maker may have resting at any time
  * the remainder stays in cash as a buffer for netting/partial fills
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Optional

TICK = 0.005
PRICE_MIN, PRICE_MAX = 0.005, 0.995


@dataclass
class ArbConfig:
    enabled: bool = True
    # sell-set: buy NO on every leg of a race when sum(best YES bids) > 1 + min_edge_sell
    min_edge_sell: float = 0.005
    # buy-set: buy YES on every leg when sum(best YES asks) < 1 - x_race - min_edge_buy
    min_edge_buy: float = 0.005
    # fallback third-party win probability by office (overwritten by compute_x.py output in params)
    x_default: dict = field(default_factory=lambda: {"senate": 0.012, "governor": 0.020, "house": 0.002, "chamber": 0.0})
    # per-race x overrides, e.g. races with a serious independent but no Independent market
    x_overrides: dict = field(default_factory=dict)
    capital_frac: float = 0.60           # max share of starting cash committed to arbs
    max_race_frac: float = 0.35          # max share of starting cash in one race
    min_set_qty: int = 20                # ignore chunks smaller than this
    max_legs_per_order: int = 10
    # delta control: after each execution, verify legs are equal; tolerance in shares
    delta_tolerance: int = 5
    hedge_residual: bool = True          # if a residual appears, flatten it at market when still profitable
    # capital recycling: when the arb budget is used up, unwind a held set at current asks (realising its edge so far)
    # if a new set's return beats the return the old set still has to earn, by at least recycle_min_gain
    recycle_enabled: bool = True
    min_ret: float = 0.0                 # new baskets must return at least this per unit of capital
    min_ret_sell: float = -1.0           # sell-sets only (riskless to settlement); < 0 means use min_ret
    cash_reserve: float = 0.0            # arbs never take cash below this (keeps MM, tilt and directional funded)
    three_leg_race_frac: float = 0.25    # per-race ceiling for races with an Independent leg (3-leg sets: riskless, deepest edge)
    recycle_min_gain: float = 0.01       # new return minus forgone return, per unit of capital
    recycle_min_new_ret: float = 0.02    # only recycle into sets returning at least this much


@dataclass
class MMConfig:
    enabled: bool = True
    capital_frac: float = 0.30           # share of starting cash resting in MM quotes (both sides, notional)
    n_markets: int = 40                  # quote the N most liquid markets
    min_price: float = 0.12              # skip markets whose mid is outside [min_price, max_price] (tilt sleeve owns the tails)
    max_price: float = 0.88
    levels: int = 5                      # ladder levels per side (level 5 dropped inside the last 24h)
    # distance of each level from the *fair* in ticks (level 1 closest). Research-tuned defaults.
    level_ticks: list = field(default_factory=lambda: [1, 2, 3, 5, 8])   # report: offsets 0/1/2/4/7 beyond a 1-tick half-spread
    # size multipliers per level relative to level 1. Default is depth-heavy ("decreasing volume as we get tighter"):
    # the venue's 5-minute prices mean-revert hard (AR(1) -0.45), so being filled deep during a sweep is good on average.
    # The equity-LOB literature prefers touch-heavy ladders in calm flow; analytics.py switches shape per market from
    # realized markouts (see calm_level_size_mult / toxic_level_size_mult).
    level_size_mult: list = field(default_factory=lambda: [0.5, 0.75, 1.0, 1.25, 1.5])   # user spec: increasing with depth, but ~5x cumulative not 50x
    calm_level_size_mult: list = field(default_factory=lambda: [1.5, 1.0, 0.5, 0.25, 0.0])    # report 'benign' row: touch markouts >= 0
    toxic_level_size_mult: list = field(default_factory=lambda: [0.5, 0.75, 1.0, 0.75, 0.5])  # report 'toxic' row: touch markouts < -0.3 half-spread
    base_qty: int = 40                   # level-1 size in shares before scaling by budget and price
    batch_size: int = 15                 # orders per batch write (the engine throttles long serial batches)
    join_touch: bool = True              # if our level-1 price would cross the touch, sit at the touch instead
    xv_gate_ticks: float = 3.0           # outside-fair gate: no ask when Polymarket/Kalshi fair >= this many ticks above mid, no bid when below (0 = off)
    improve_when_spread_ticks_ge: int = 2  # improve the best quote by one tick when spread >= this many ticks
    max_inventory_notional: float = 1500.0  # per race, in SUSQies at risk (|net delta| * price)
    hard_inventory_stop: bool = True     # at the per-race cap, post nothing on the accumulating side
    portfolio_max_partisan_notional: float = 5000.0  # |net D - R notional| cap across all non-arb positions; blocks the heavy side
    inventory_skew_ticks_at_max: float = 3.0  # shift reservation price by this many ticks at max inventory
    size_skew_at_max: float = 0.8        # cut size on the inventory-increasing side by this fraction at max inventory
    pressure_skew_ticks: float = 1.0     # shift fair by pressure * this many ticks
    pressure_size_skew: float = 0.5      # cut size on the side being hit by pressure * this fraction
    requote_ticks: float = 0.5           # requote when the reservation price moved by at least this many ticks
    order_ttl_sec: int = 1800            # safety expiry on resting quotes
    refresh_sec: int = 1500              # re-post before expiry; no other age-based refresh (queue position is worth keeping)
    same_side_pause_sec: int = 60        # pause level 1 on a side after 3 same-side fills within this window
    # volatility gate (after a sweep / news): stop adding inventory, widen, anchor the reducing side, until calm
    vol_gate_move_ticks: float = 3.0     # |mid now - mid 15 min ago| at or above this = hot
    vol_gate_var_mult: float = 2.0       # or 1h realised variance above this multiple of the trailing baseline = hot
    vol_gate_cooldown_sec: int = 1800    # stay hot this long after the last trigger
    vol_gate_spread_add: int = 1         # extra ticks on every level while hot
    vol_gate_size_mult: float = 0.25     # size multiplier on the reducing side while hot
    vol_gate_fade_mult: float = 0.0      # size multiplier on the adding (fade) side while hot; 0 pulls it
    wave_hot_frac: float = 0.15          # if this share of quoted markets is hot at once (news wave), fade all quotes
    wave_size_mult: float = 0.25         # size multiplier on every level during a wave
    wave_spread_add: int = 2             # extra ticks on every level during a wave
    meanrev_weight: float = 1.0          # fair value += weight * predicted 1-bar change (meanrev model)
    meanrev_max_ticks: float = 1.0       # cap on that shift
    vol_lookback_hours: int = 24
    gamma: float = 0.5                   # risk aversion in the reservation-price shift (AS-style)
    stop_quoting_at: str = "2026-11-03T12:00:00Z"   # election day: results-driven flow, stop quoting
    overrides_file: str = "params/mm_overrides.json"  # written by analytics.py


@dataclass
class TiltConfig:
    enabled: bool = True
    capital_frac: float = 0.25           # share of starting cash this sleeve may lock until settlement
    max_market_frac: float = 0.03        # per market
    max_markets: int = 30
    longshot_max: float = 0.12           # sell YES (buy NO) when mid <= this
    favourite_min: float = 0.88          # buy YES when mid >= this
    calibration_slope: float = 1.5       # logit model: p_fair = sigmoid(slope * logit(mid)); politics slopes 1.3-1.8
    tilt_s: float = 0.10                 # mixture model: venue price = (1-s) p_true + s/2; measured s 0.09-0.13 on this venue
    min_edge: float = 0.04               # minimum edge per unit of collateral (conservative model of the two)
    level_ticks: list = field(default_factory=lambda: [0, 1, 2])     # at the touch, 1 and 2 ticks better for us
    level_weights: list = field(default_factory=lambda: [1.0, 1.5, 2.0])
    order_ttl_sec: int = 6 * 3600
    refresh_sec: int = 1800


@dataclass
class NewsConfig:
    """News-driven sweeper (sigbot/news.py). Off by default; dry-run unless the bot runs --live."""
    enabled: bool = False
    allow_live: bool = False             # even when the bot runs --live, the sweeper only places orders if this is true
    poll_sec: int = 90                   # feeds are polled once per bot cycle when the cycle is at least this long apart
    # outlet feeds (RSS/Atom). AP, Reuters and Politico block direct RSS fetches, so they come in through Google News site queries.
    feeds: dict = field(default_factory=lambda: {
        "The Hill": "https://thehill.com/feed/",
        "NYT Politics": "https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml",
        "CBS Politics": "https://www.cbsnews.com/latest/rss/politics",
        "NBC Politics": "https://feeds.nbcnews.com/nbcnews/public/politics",
        "NPR Politics": "https://feeds.npr.org/1014/rss.xml",
        "Guardian US politics": "https://www.theguardian.com/us-news/us-politics/rss",
    })
    google_news_sites: list = field(default_factory=lambda: ["apnews.com", "reuters.com", "politico.com"])
    google_news_site_terms: str = "senate OR governor OR house OR midterm OR poll"
    race_queries: bool = True            # Google News query per race ("Rhode Island Senate"), rotating
    race_queries_per_poll: int = 6
    race_query_min_mid: float = 0.04     # only races with a leg priced inside [min, 1-min] get their own query
    venue_news: bool = True              # the venue's own per-market "Related News" JSON (web route, not /api/v1)
    venue_news_per_poll: int = 6
    venue_news_url: str = "https://sig.thesuper.market/api/markets/%5Bid%5D/news?marketId={market_id}"
    fetch_article: bool = True           # fetch the first paragraphs when a feed gives only a headline (robots.txt honoured)
    article_timeout_sec: float = 8.0
    article_max_chars: int = 1500
    max_item_age_hours: float = 6.0      # ignore anything older when first seen
    user_agent: str = "sigbot-news/0.1 (RSS reader; contact via sig.thesuper.market)"
    # classification
    screen_model: str = "claude-haiku-5-5"
    final_model: str = "claude-sonnet-5-5"
    screen_batch: int = 30
    llm_timeout_sec: float = 90.0
    min_confidence: float = 0.8
    min_move_pts: float = 3.0
    already_moved_frac: float = 0.34     # skip when the market already moved more than this fraction of the expected move
    # execution
    capital_frac: float = 0.05           # share of cash the sleeve may hold in open news positions
    per_event_budget: float = 1000.0     # SUSQies per event
    max_events_per_race_per_day: int = 2
    order_ttl_sec: int = 120             # unfilled remainder of the taker order dies after this
    mm_pause_sec: int = 1800             # market maker stops quoting the race for this long
    followup_minutes: list = field(default_factory=lambda: [5, 30, 120])


@dataclass
class BotConfig:
    api_key: str = ""
    base_url: str = "https://sig.thesuper.market/api/v1"
    tournament_slug: str = "midterm-elections"
    db_path: str = "data/sigbot.sqlite"
    log_path: str = "data/sigbot.log"
    cycle_sec: int = 90
    reads_per_min: int = 36              # main-loop read budget (account limit 100, shared with the scanner and other tools)
    writes_per_min: int = 15             # main-loop write budget (account limit 30)
    scan_reads_per_min: int = 38         # arb scanner's own read budget (3 per pass at 5 s = 36)
    scan_writes_per_min: int = 12
    arb_scan_sec: float = 5.0            # dedicated arb scanner period (0 = scan inside the main cycle)
    arb_scan_offset: float = 4.5         # fire at :04.5, :09.5 ... :59.5, half a second before whole-second pollers
    arb_scan_books: int = 3              # candidate races whose books the scanner fetches over REST per pass
    feed_enabled: bool = True            # Supabase Realtime WebSocket feed: versioned books for every tournament market
    feed_max_age_sec: float = 120.0      # a feed book received longer ago than this is refetched over REST (quiet-topic drop guard)
    arb_scan_sec_feed: float = 1.0       # scanner period while the feed is healthy (candidate books come from the feed)
    arb_scan_min_gap: float = 0.25       # event-driven scanner: minimum seconds between passes
    arb_scan_books_feed: int = 12        # candidate races evaluated per pass from feed books
    feed_stall_sec: float = 240.0        # feed silent this long -> the bot abandons the feed thread and starts a new one
    lag_enabled: bool = True             # follow Polymarket/Kalshi moves before the DMM requotes (proposals/dmm_lag_follow)
    lag_move_pts: float = 0.02           # outside move that triggers a follow
    lag_qty: int = 1000                  # shares per follow, before the directional per-leg notional cap
    inside_enabled: bool = True          # rest one tick inside the DMM where its spread is >= 3 ticks (proposals/inside_quoter)
    inside_qty: int = 100
    inside_max_markets: int = 12         # markets re-posted per cycle (each costs a cancel + a share of one batch write)
    inside_ttl_sec: int = 300
    election_enabled: bool = False       # election-night sleeve (proposals/election_night): Sizhe switches it on for 3 Nov
    election_per_race_cap: float = 5000.0
    election_total_cap: float = 90000.0
    election_poll_sec: float = 4.0
    market_watch_sec: float = 300.0      # new / voided market watcher (high_upside.md): one /markets read every 5 min
    mm_fast_requotes_per_min: int = 3    # scanner-thread requotes of a quoted market whose touch moved since we last quoted (2 writes each)
    offload_scan_sec: float = 3.0        # scanner runs the cross-sleeve offload check this often (it reads the fills table)
    free_unwind_scan_sec: float = 10.0   # scanner checks held sets for a free unwind (asks sum <= payout) this often, from feed books
    kill_drawdown: float = 400.0         # marked P&L drop over the trailing hour that pauses MM and tilt
    kill_markout_ticks: float = -1.0     # 30-min MM markout (mean ticks, n>=20) below this pauses MM and tilt
    kill_pause_sec: int = 1800
    analytics_every_sec: int = 300
    exit_band: float = 0.01              # close directional/tilt legs once the venue is within this of fair
    free_unwind_every_sec: int = 300     # how often held arb sets are checked for a costless unwind
    cycle_sec_fast: int = 30             # cycle length during volatile US hours
    fast_hours_et: list = field(default_factory=lambda: [9, 24])   # [start, end) hour in America/New_York for the fast cycle
    tape_poll_markets: int = 10          # how many quoted/arb markets' tapes to poll per cycle (read budget)
    arb_books_per_cycle: int = 8         # candidate races whose books are fetched per cycle
    mm_write_budget: int = 18            # writes the market maker may spend per cycle (30/min account limit)
    starting_cash: float = 100000.0
    arb: ArbConfig = field(default_factory=ArbConfig)
    mm: MMConfig = field(default_factory=MMConfig)
    tilt: TiltConfig = field(default_factory=TiltConfig)
    news: NewsConfig = field(default_factory=NewsConfig)

    @staticmethod
    def load(path: Optional[str] = None) -> "BotConfig":
        cfg = BotConfig()
        path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
        if os.path.exists(path):
            with open(path) as f:
                raw = json.load(f)
            for k, v in raw.items():
                if k in ("arb", "mm", "tilt", "news") and isinstance(v, dict):
                    for kk, vv in v.items():
                        setattr(getattr(cfg, k), kk, vv)
                else:
                    setattr(cfg, k, v)
        cfg.api_key = os.environ.get("SIG_API_KEY") or cfg.api_key
        if not cfg.api_key:
            keyfile = os.path.join(os.path.dirname(path), ".sig_api_key")
            if os.path.exists(keyfile):
                cfg.api_key = open(keyfile).read().strip()
        return cfg

    def dump(self) -> dict:
        d = asdict(self)
        d["api_key"] = "***"
        return d


def round_tick(p: float) -> float:
    return min(PRICE_MAX, max(PRICE_MIN, round(round(p / TICK) * TICK, 3)))


def ticks(p: float) -> int:
    return int(round(p / TICK))
