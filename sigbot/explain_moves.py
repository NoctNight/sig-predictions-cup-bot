"""Which of the largest 5-minute moves had a headline behind them?

    python3 -m sigbot.explain_moves                       # 40 largest moves, headline match, Claude one-liners if a key exists
    python3 -m sigbot.explain_moves --top 60 --no-llm     # skip the Claude step

For every market in data/candles_5m.json the 1-bar close changes are scaled by that market's own sigma; the --top
moves (both closes inside [0.05, 0.95]; when both legs of a race jump on the same bar only the larger one is kept,
it is one event) are matched against `news_items` in data/sigbot.sqlite published from 3 hours before the bar to
1 hour after it. Matching is keyword-based on the market title: state name, district code ("AZ-01", "Arizona's 1st"),
office word (Senate / Governor / House), party word, and the sweeper's own `screen_race` label.

If `.anthropic_api_key` exists next to config.json (or ANTHROPIC_API_KEY is set), claude-haiku-5-5 is asked for a
one-line probable explanation per move given the matched headlines (batched, structured JSON, a few thousand tokens
in total). The table is printed and written to params/move_explanations.json. The SQLite file is opened read-only.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import statistics
from datetime import datetime, timezone
from typing import Optional

from .meanrev import HERE, MIN_PRICE, MAX_PRICE, iso, load_universe, parse_time
from .news import STATES, ClaudeClient, _key_file

OUT_PATH = os.path.join(HERE, "params", "move_explanations.json")
MODEL = "claude-haiku-5-5"
BEFORE_SEC, AFTER_SEC = 3 * 3600, 1 * 3600
BAR_SEC = 300
MAX_INPUT_TOKENS = 150_000       # hard stop on spend; a full run uses a few thousand
ORDINAL = {1: "1st", 2: "2nd", 3: "3rd"}
STATE_BY_NAME = {v: k for k, v in STATES.items()}
PARTY_WORDS = {"D": r"democrat(s|ic)?|dem(s)?\b", "R": r"republican(s)?|gop", "I": r"independent"}

log = logging.getLogger("sigbot.explain_moves")


# --------------------------------------------------------------------------- headline matching
def race_patterns(race: str) -> tuple[list[tuple[str, re.Pattern, float]], float, Optional[re.Pattern]]:
    """(label, regex, weight) patterns, the score a headline needs to count as a match, and a regex for the *other*
    statewide office (a Michigan-Senate headline should not explain a Michigan-Governor move)."""
    pats = []
    m = re.match(r"([A-Z]{2})-(\d{2}) House race", race)
    if m:
        st, dist = m.group(1), int(m.group(2))
        state = STATES.get(st, st)
        ordn = ORDINAL.get(dist, f"{dist}th")
        pats.append(("state", re.compile(r"\b" + re.escape(state) + r"\b", re.I), 2))
        pats.append(("district", re.compile(rf"\b{st}-?0?{dist}\b|\b{re.escape(state)}['’]s {ordn}\b|\b{ordn} (congressional )?district\b", re.I), 3))
        pats.append(("office", re.compile(r"\b(congress(ional|man|woman)?|house (seat|race|district|member|gop|democrat)|u\.s\. house|district|rep\.)\b", re.I), 1))
        return pats, 3, None          # state alone is not enough for a district race
    if race in ("U.S. House", "U.S. Senate"):
        chamber = race.split()[-1]
        pats.append(("chamber", re.compile(rf"\b(control|majority|flip|balance) of (the )?{chamber}\b|\b{chamber} (control|majority|map|battleground|seats)\b", re.I), 3))
        pats.append(("office", re.compile(rf"\b{chamber}\b", re.I), 1))
        pats.append(("midterm", re.compile(r"\bmidterm(s)?\b", re.I), 0.5))
        return pats, 3, None
    parts = race.rsplit(" ", 1)
    state, office = (parts[0], parts[1]) if len(parts) == 2 else (race, "")
    pats.append(("state", re.compile(r"\b" + re.escape(state) + r"\b", re.I), 2))
    if state in STATE_BY_NAME:
        pats.append(("state_abbr", re.compile(r"\b" + STATE_BY_NAME[state] + r"\b"), 1))
    senate_rx = re.compile(r"\b(senate|senator|sen\.)\b", re.I)
    gov_rx = re.compile(r"\b(governor|gubernatorial|gov\.)\b", re.I)
    other = None
    if office == "Senate":
        pats.append(("office", senate_rx, 1))
        other = gov_rx
    elif office == "Governor":
        pats.append(("office", gov_rx, 1))
        other = senate_rx
    elif office:
        pats.append(("office", re.compile(r"\b" + re.escape(office) + r"\b", re.I), 1))
    return pats, 2, other


def match_headline(text: str, pats, need: float, other_office, party: str, screen_race: Optional[str], race: str) -> tuple[float, list[str]]:
    score, hits = 0.0, []
    for label, rx, w in pats:
        if rx.search(text):
            score += w
            hits.append(label)
    if other_office is not None and "office" not in hits and other_office.search(text):
        score -= 1                # names the state's other race, not this one
        hits.append("other_office")
    if party in PARTY_WORDS and re.search(PARTY_WORDS[party], text, re.I):
        hits.append("party")      # party words alone never qualify; they only annotate
    if screen_race and screen_race == race:
        score += 3
        hits.append("screen_race")
    return (score if score >= need else 0.0), hits


def item_time(row) -> Optional[float]:
    if row["published"]:
        try:
            return parse_time(row["published"])
        except ValueError:
            pass
    return float(row["first_seen"]) if row["first_seen"] is not None else None


# --------------------------------------------------------------------------- moves
def largest_moves(candles_path: str, universe, top: int, since: Optional[float] = None, until: Optional[float] = None) -> list[dict]:
    raw = json.load(open(candles_path))
    cands = []
    for ex, h in raw.items():
        c = [x for x in h.get("candles", []) if x.get("close") is not None]
        if len(c) < 50:
            continue
        closes = [float(x["close"]) for x in c]
        d = [b - a for a, b in zip(closes, closes[1:])]
        sigma = statistics.pstdev(d)
        if sigma <= 0:
            continue
        rp = universe.by_exchange.get(str(ex), ("?", "?"))
        for i in range(1, len(closes)):
            if not (MIN_PRICE <= closes[i - 1] <= MAX_PRICE and MIN_PRICE <= closes[i] <= MAX_PRICE):
                continue
            dp = closes[i] - closes[i - 1]
            if dp == 0:
                continue
            ts = parse_time(c[i]["time"])
            if (since is not None and ts < since) or (until is not None and ts > until):
                continue
            cands.append({"exchange_id": str(ex), "race": rp[0], "party": rp[1], "time": c[i]["time"], "ts": parse_time(c[i]["time"]),
                          "prev_close": closes[i - 1], "close": closes[i], "change": dp, "sigma": sigma, "z": abs(dp) / sigma,
                          "volume": c[i].get("volume"), "tradeCount": c[i].get("tradeCount"),
                          "gap_min": (parse_time(c[i]["time"]) - parse_time(c[i - 1]["time"])) / 60.0})
    cands.sort(key=lambda r: -r["z"])
    seen, out = set(), []
    for r in cands:
        key = (r["race"], r["time"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= top:
            break
    return out


def load_items(db_path: str) -> list[tuple[float, sqlite3.Row]]:
    """(timestamp, row) for every news_items row; the database is opened read-only (the live bot owns it)."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    items = db.execute("SELECT id, source, url, title, published, lead, first_seen, screen_race FROM news_items").fetchall()
    db.close()
    timed = [(item_time(r), r) for r in items]
    return sorted(((t, r) for t, r in timed if t is not None), key=lambda x: x[0])


def attach_headlines(moves: list[dict], timed: list[tuple[float, sqlite3.Row]]) -> None:
    for mv in moves:
        pats, need, other_office = race_patterns(mv["race"])
        lo, hi = mv["ts"] - BEFORE_SEC, mv["ts"] + BAR_SEC + AFTER_SEC
        found = []
        for t, r in timed:
            if not (lo <= t <= hi):
                continue
            text = f"{r['title'] or ''} {r['lead'] or ''}"
            score, hits = match_headline(text, pats, need, other_office, mv["party"], r["screen_race"], mv["race"])
            if score > 0:
                found.append({"id": r["id"], "title": r["title"], "source": r["source"], "url": r["url"],
                              "published": iso(t), "minutes_before_bar": round((mv["ts"] - t) / 60.0), "score": score, "hits": hits})
        found.sort(key=lambda x: (-x["score"], abs(x["minutes_before_bar"])))
        mv["headlines"] = found
        mv["news_in_window"] = sum(1 for t, _ in timed if lo <= t <= hi)


# --------------------------------------------------------------------------- Claude one-liners
EXPLAIN_SYSTEM = """You explain sudden 5-minute price moves on a play-money prediction market for the 2026 U.S. midterm
elections. Each market is "Will the <party> Party win the <race>?" and trades in YES-probability cents. For every move
you get the race, the party, the price before and after, the move in sigmas of that market's usual 5-minute change,
volume and trade count in the bar, and the headlines the news sweeper saw in the 3 hours before to 1 hour after the
bar that mention that race (possibly none). Give ONE short line per move (max 25 words) with the most probable
explanation: cite a headline if one plausibly explains the direction and timing; otherwise say it looks like
order flow / a thin book / set-arbitrage rebalancing / a sweep without news, as appropriate. Be concrete and brief."""

EXPLAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "news_driven": {"type": "boolean"},
                },
                "required": ["idx", "explanation", "news_driven"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["explanations"],
    "additionalProperties": False,
}


def ask_claude(moves: list[dict], key: str, batch: int = 20) -> dict:
    client = ClaudeClient(key)
    for start in range(0, len(moves), batch):
        chunk = moves[start:start + batch]
        if client.usage["input_tokens"] > MAX_INPUT_TOKENS:
            log.warning("token budget reached; skipping the rest")
            break
        lines = []
        for j, mv in enumerate(chunk):
            hl = "; ".join(f"[{h['minutes_before_bar']:+d} min before bar] {h['title']} ({h['source']})" for h in mv["headlines"][:6]) or "none"
            lines.append(f"#{start + j}: {mv['party']} {mv['race']} at {iso(mv['ts'])}: {mv['prev_close']*100:.1f}c -> {mv['close']*100:.1f}c "
                         f"({mv['change']*100:+.1f}c, {mv['z']:.1f} sigma), volume {mv['volume']}, trades {mv['tradeCount']}, "
                         f"{mv['gap_min']:.0f} min since the previous bar. Headlines: {hl}")
        user = "Moves:\n" + "\n".join(lines) + "\n\nReturn one explanation per move, keyed by idx."
        out = client.json_call(MODEL, EXPLAIN_SYSTEM, user, EXPLAIN_SCHEMA, max_tokens=4096, effort="low")
        for e in (out or {}).get("explanations", []):
            k = e.get("idx")
            if isinstance(k, int) and 0 <= k < len(moves):
                moves[k]["explanation"] = e.get("explanation", "").strip()
                moves[k]["news_driven"] = bool(e.get("news_driven"))
    return client.usage


# --------------------------------------------------------------------------- CLI
def print_table(title: str, moves: list[dict]) -> None:
    with_news = sum(1 for m in moves if m["headlines"])
    print(f"\n{title}: {len(moves)} moves (|change| / market sigma, closes inside [{MIN_PRICE}, {MAX_PRICE}], one per race and bar); "
          f"{with_news} had a matching headline within -3h/+1h")
    print(f"{'#':>2} {'bar (UTC)':>17} {'market':<30} {'move':>13} {'sigma':>5} {'vol':>7} {'trd':>4} {'gap':>5} {'news':>4}  headline / explanation")
    for k, mv in enumerate(moves):
        head = mv["headlines"][0]["title"][:70] if mv["headlines"] else "-"
        print(f"{k:>2} {iso(mv['ts']):>17} {(mv['party'] + ' ' + mv['race'])[:30]:<30} {mv['prev_close']*100:>5.1f}->{mv['close']*100:<5.1f} "
              f"{mv['z']:>5.1f} {mv['volume'] or 0:>7.0f} {mv['tradeCount'] or 0:>4} {mv['gap_min']:>4.0f}m {len(mv['headlines']):>4}  {head}")
        if mv.get("explanation"):
            print(f"{'':>2} {'':>17} {'':<30} {'claude:':>13} {mv['explanation']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candles", default="data/candles_5m.json")
    ap.add_argument("--markets", default="data/markets.json")
    ap.add_argument("--db", default="data/sigbot.sqlite")
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    path = lambda p: p if os.path.isabs(p) else os.path.join(HERE, p)

    uni = load_universe(path(args.markets))
    timed = load_items(path(args.db))
    coverage = (timed[0][0], timed[-1][0]) if timed else None
    print(f"news_items: {len(timed)} headlines" + (f", {iso(coverage[0])} .. {iso(coverage[1])}" if coverage else ""))

    # 1. the largest moves over the whole candle history
    moves = largest_moves(path(args.candles), uni, args.top)
    attach_headlines(moves, timed)
    # 2. the largest moves among bars whose -3h/+1h window overlaps the news table, where a match is possible at all
    moves_cov = []
    if coverage:
        moves_cov = largest_moves(path(args.candles), uni, args.top, since=coverage[0] - AFTER_SEC - BAR_SEC, until=coverage[1] + BEFORE_SEC)
        attach_headlines(moves_cov, timed)

    usage = None
    key = "" if args.no_llm else (os.environ.get("ANTHROPIC_API_KEY", "") or _key_file())
    if key:
        usage = ask_claude(moves + moves_cov, key)
    else:
        print("(no Anthropic key: skipping the Claude explanation step)" if not args.no_llm else "(--no-llm)")

    print_table("largest 5-minute moves, whole history", moves)
    if moves_cov:
        print_table("largest 5-minute moves inside the news table's coverage", moves_cov)
    if usage:
        print(f"\nclaude usage: {usage['calls']} calls, {usage['input_tokens']} input / {usage['output_tokens']} output tokens, {usage['errors']} errors")

    out = {"generated_at": iso(datetime.now(timezone.utc).timestamp()), "candles": args.candles, "db": args.db,
           "window": {"before_sec": BEFORE_SEC, "after_sec": AFTER_SEC}, "model": MODEL if key else None,
           "news_coverage": [iso(coverage[0]), iso(coverage[1])] if coverage else None, "claude_usage": usage,
           "n_moves": len(moves), "n_with_headline": sum(1 for m in moves if m["headlines"]),
           "n_moves_in_coverage": len(moves_cov), "n_with_headline_in_coverage": sum(1 for m in moves_cov if m["headlines"]),
           "moves": moves, "moves_in_news_coverage": moves_cov}
    os.makedirs(os.path.dirname(path(args.out)), exist_ok=True)
    with open(path(args.out), "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {path(args.out)}")


if __name__ == "__main__":
    main()
