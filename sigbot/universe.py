"""Market universe: groups the tournament's binary markets into races.

Every market is "Will the <Party> Party win the <race>?" with one YES exchange. A race has 2 legs
(D, R) or 3 (D, R, I). Chamber-control markets ("U.S. House", "U.S. Senate") are 2-leg races too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

TITLE_RE = re.compile(r"Will the (Democratic|Republican|Independent) Party win the (.+)\?")


@dataclass
class Leg:
    party: str            # D | R | I
    market_id: str
    exchange_id: str
    title: str
    last: Optional[float] = None


@dataclass
class Race:
    name: str
    legs: dict = field(default_factory=dict)   # party -> Leg

    @property
    def office(self) -> str:
        n = self.name
        if n in ("U.S. House", "U.S. Senate"):
            return "chamber"
        if "House race" in n:
            return "house"
        if n.endswith("Senate"):
            return "senate"
        if n.endswith("Governor"):
            return "governor"
        return "other"

    @property
    def has_independent(self) -> bool:
        return "I" in self.legs

    @property
    def exchange_ids(self) -> list[str]:
        return [l.exchange_id for l in self.legs.values()]


@dataclass
class Universe:
    races: dict = field(default_factory=dict)          # race name -> Race
    by_exchange: dict = field(default_factory=dict)    # exchange id -> (race name, party)
    markets: dict = field(default_factory=dict)        # market id -> raw market row

    @staticmethod
    def from_markets(rows: list[dict]) -> "Universe":
        u = Universe()
        for m in rows:
            if m.get("status") != "open":
                continue
            mt = TITLE_RE.match(m["title"])
            if not mt or not m.get("exchanges"):
                continue
            party, race = mt.group(1)[0], mt.group(2)
            ex = m["exchanges"][0]
            leg = Leg(party=party, market_id=str(m["id"]), exchange_id=str(ex["id"]), title=m["title"], last=ex.get("latestPrice"))
            u.races.setdefault(race, Race(name=race)).legs[party] = leg
            u.by_exchange[leg.exchange_id] = (race, party)
            u.markets[str(m["id"])] = m
        return u

    def race_of(self, exchange_id: str) -> Optional[Race]:
        r = self.by_exchange.get(str(exchange_id))
        return self.races[r[0]] if r else None

    def all_exchanges(self) -> list[str]:
        return list(self.by_exchange.keys())
