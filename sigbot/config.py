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
    # False = cash mode (Sizhe 23 Oct: maximise liquid cash for the 3 Nov punt): free cash never buys a new set; recycles
    # still swap a held set for a better one, spending only what the unwind freed; free unwinds keep running
    new_sets: bool = True
    min_ret: float = 0.0                 # new baskets must return at least this per unit of capital
    min_ret_sell: float = -1.0           # sell-sets only (riskless to settlement); < 0 means use min_ret
    cash_reserve: float = 0.0            # arbs never take cash below this (keeps MM, tilt and directional funded)
    high_ret_min: float = 0.04           # sell-sets returning at least this to settlement get the larger per-race ceiling
    high_ret_race_frac: float = 0.40     # hft_strategies.md: ~120k of riskless sell-sets at 4-6.5% stand unfilled (Idaho Governor)
    three_leg_race_frac: float = 0.25    # per-race ceiling for races with an Independent leg (3-leg sets: riskless, deepest edge)
    recycle_min_gain: float = 0.01       # new return minus forgone return, per unit of capital
    recycle_fit_tolerance: float = 0.05   # unwind only what the follow-through will spend, within this fraction
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
    lag_enabled: bool = False            # follow Polymarket/Kalshi moves before the DMM requotes (proposals/dmm_lag_follow)
    lag_move_pts: float = 0.02           # outside move that triggers a follow
    lag_qty: int = 1000                  # shares per follow, before the directional per-leg notional cap
    inside_enabled: bool = True          # rest one tick inside the DMM where its spread is >= 3 ticks (proposals/inside_quoter)
    inside_qty: int = 100
    inside_max_markets: int = 12         # markets re-posted per cycle (each costs a cancel + a share of one batch write)
    inside_ttl_sec: int = 300
    election_enabled: bool = False       # election-night sleeve (proposals/election_night): Sizhe switches it on for 3 Nov
    catchup_enabled: bool = False        # catch-up sweep trial (proposals/outside_sweep): take SIG's stale touch when PM+Kalshi moved
    catchup_notional: float = 300.0      # per position; at most 4 open; sleeve switches itself off at catchup_kill
    catchup_kill: float = -50.0
    churn_enabled: bool = False          # deviation churn (proposals/dev_churn): buy sell-sets only when unusually cheap,
    churn_capital: float = 12000.0       # sell after +2 ticks/set or 24 h; pot, cash floor and stop below
    churn_cash_floor: float = 3000.0
    churn_kill: float = -600.0
    churn_skip_holding: float = 4000.0   # no entries in races where we already hold > this many shares on a leg (0 = off)
    cheapfav_enabled: bool = False       # cheap safe favourites (proposals/cheap_favourites): buy fast-count favourites SIG prices
    cheapfav_budget: float = 15000.0     # >= cheapfav_min_edge under the outside fair, hold through the call; total cost cap,
    cheapfav_min_edge: float = 0.02      # min edge at the touch (+1c on single-venue legs),
    cheapfav_leg_cap: float = 2000.0     # cost per race outcome,
    cheapfav_order_notional: float = 500.0   # per order (touch only, <= 80% of the touch),
    cheapfav_cash_floor: float = 1000.0  # never spend cash below this
    cheapfav_net_max: float = 2000.0     # the sleeve's own net D cost stays within +-this whatever the book
    cheapfav_headroom: float = 1000.0    # an add that widens the book's |net D| keeps it this far under the partisan cap
    wind_down_enabled: bool = False      # 31 Oct-3 Nov wind-down (proposals/wind_down, backlog 000): late-call sets, legs and MM
    wind_down_clip_sets: int = 1000      # inventory to cash by 18:00 ET 3 Nov; does nothing before 31 Oct 00:00 ET but the report.
    wind_down_gap_sec: float = 1800.0    # sets per clip, and at least this long between two clips in a race (or on a leg)
    wind_down_max_walk_ticks: float = 1.0    # a clip shrinks (halving) while walking the book costs more than this per set
    wind_down_min_clip: int = 100
    wind_down_leg_clip: int = 2000       # shares per single-leg clip (also <= 80% of the touch), fill-or-cancel
    wind_down_min_leg: int = 10
    wind_down_max_cost_per_hour: float = 200.0   # exit cost (vs payout / fair) per hour: the kill switch trips at 400 an hour
    wind_down_clips_per_cycle: int = 1
    wind_down_fallback_buffer_sec: float = 7200.0   # a race drops the timing gate when only clips x gap + this is left
    wind_down_force_late: list = field(default_factory=lambda: ["Rhode Island Governor"])   # sold even if called early
    wind_down_sell_at_call: bool = False  # backlog 000c: selling early-call sets at their call stays report-only until Sizhe approves
    strategy_monitor_enabled: bool = False   # strategy_monitor.py: test each sleeve on its own results every strategy_monitor_sec
    strategy_monitor_sec: float = 900.0      # and pause a losing one's new entries (params/strategy_kill.json; a human re-enables)
    raise_enabled: bool = False          # cash raise (proposals/cash_raise): sell holdings beyond the arb sets that cost
    raise_max_cost: float = 0.01         # < this vs mid to exit, only while a higher-edge sleeve posted a cash need this cycle;
    raise_buffer: float = 200.0          # raise the shortfall plus this,
    raise_max_total: float = 32000.0     # at most this in any 24 h,
    raise_max_clips: int = 4             # at most this many clips (one per leg) a cycle,
    raise_min_clip: float = 50.0         # each able to fetch at least this (no crumbs: a clip pauses the MM on its leg);
    raise_pause_sec: float = 900.0       # the MM does not quote a leg for this long after a raise clip on it
    raise_hold_limit: float = 4000.0     # never a leg holding more than this many shares
    raise_cap_margin: float = 1000.0     # within this of the partisan cap, clips that pull net D toward zero go first
    raise_exclude_races: list = field(default_factory=lambda: ["Rhode Island Governor"])
    raise_sets_enabled: bool = False     # tier 2: whole sets / buy-sets, all-or-none, when tier 1 cannot cover the need and
    raise_set_max_cost: float = 0.045    # high-frequency arb skipped >= raise_set_min_skips entries for cash in 6 h; exit
    raise_set_min_skips: int = 3         # half-hours (of the last 6 h) with a skip; cost now <= raise_set_max_cost (the
    raise_set_skip_window: float = 1800.0   # 31 Oct-3 Nov wind-down costs ~3.75-4.75% anyway),
    raise_set_max_pieces: int = 2        # at most this many set pieces a cycle,
    raise_set_hold_races: list = field(default_factory=list)   # never races we hold through an early call
    raise_sets_races: list = field(default_factory=list)   # the RI trim (Sizhe 24 Oct): whole-set clips in these races only
    raise_ri_trim_sets: float = 4500.0   # total sets the trim may unwind (counted from its own orders: survives a restart)
    raise_ri_trim_clip: int = 1000       # sets per clip, at most one clip per raise_ri_trim_gap_sec, only at or under the race's
    raise_ri_trim_gap_sec: float = 1800.0   # trailing 24-h median exit cost ("level 0") and at no realised loss
    shock_reserve: float = 15000.0       # with shock_enabled: free cash kept for shock mode only (every other sleeve's floor sits
                                         # on top of it); the cash raise refills it with tier 1 (Sizhe 24 Oct)
    shock_enabled: bool = False          # shock mode (proposals/shock_mode.py): both-venue 5+ pt jumps, SIG lagging; exempt from
    shock_poll_sec: float = 5.0          # the partisan cap (Sizhe 24 Oct). Its own thread: a batched Polymarket /midpoints check
    shock_full_sec: float = 30.0         # every shock_poll_sec, a full two-venue poll every shock_full_sec or at once on a 3-pt move
    shock_per_shock: float = 20000.0     # notional per shocked race (both legs), see research_notes/shock_mode.md
    shock_total: float = 20000.0         # notional open across all shocks (one race at a time)
    shock_kill: float = -1500.0          # sleeve P&L stop: entries off, open positions closed
    shock_exit_maker: bool = False       # R3: rest the exit at the 70%-convergence price (the DMM's walk fills it) instead of
                                         # taking the bid at convergence; any exit rule cancels it and takes. Off: see shock_mode.md
    punt_enabled: bool = False           # the 3 Nov punt executor (proposals/punt_executor.py). Only ever switched on by a session in
                                         # which Sizhe approved the exact plan in chat; the plan file must hash to punt_plan_sha256
    punt_plan_path: str = "params/punt_plan.json"
    punt_plan_sha256: str = ""           # SHA-256 of the approved plan file; any other bytes are refused
    punt_tick_sec: float = 10.0          # executor thread cadence (2 reads a tick while a step is in its window)
    punt_max_budget: float = 150000.0    # hard ceiling on a plan's budget (cash brought in from outside the punt)
    punt_cash_floor: float = 100.0       # free cash the executor never spends
    strategy_stops_enabled: bool = False     # strategy_stops.py: per-strategy P&L stops replace the bot-wide kill switch
    stop_limits: dict = field(default_factory=dict)   # {strategy: P&L floor since armed}; defaults in strategy_stops.DEFAULT_LIMITS
    unattributed_limit: float = 2000.0       # unexplained account loss that pauses the strategies on its legs (or all entries)
    untagged_limit: float = 200.0            # loss on fills no strategy owns that does the same
    election_per_race_cap: float = 5000.0
    election_total_cap: float = 90000.0
    election_poll_sec: float = 4.0
    market_watch_sec: float = 300.0      # new / voided market watcher (high_upside.md): one /markets read every 5 min
    mm_fast_requotes_per_min: int = 3    # scanner-thread requotes of a quoted market whose touch moved since we last quoted (2 writes each)
    offload_arb_for_sleeves: bool = False   # review 22 Oct: unwinding arb sets to fund tilt/directional trades kept firing for trades tilt never placed; off
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
