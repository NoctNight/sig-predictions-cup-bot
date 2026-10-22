"""Cancel every resting order in the tournament and verify the book is empty.

    python3 deploy/cancel_all.py            # cancel and verify, exit 0 when nothing rests
    python3 deploy/cancel_all.py --check    # only count resting orders

Stdlib only; reads the key from SIG_API_KEY or .sig_api_key and the tournament from config.json via sigbot.config.
Used by the systemd unit (ExecStopPost) and by the cutover script.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from sigbot.api import ApiError, SuperMarketClient  # noqa: E402
from sigbot.config import BotConfig  # noqa: E402


def count_open(api: SuperMarketClient) -> int:
    return len(api.open_orders())


def main() -> int:
    cfg = BotConfig.load(os.path.join(HERE, "config.json"))
    if not cfg.api_key:
        print("no API key", file=sys.stderr)
        return 2
    api = SuperMarketClient(cfg.api_key, cfg.base_url, reads_per_min=20, writes_per_min=10)
    api.tournament_id = api.tournament(cfg.tournament_slug)["id"]
    if "--check" in sys.argv:
        print(f"open orders: {count_open(api)}")
        return 0
    for attempt in range(5):
        try:
            res = api.cancel_all()
            n = count_open(api)
            print(f"cancel-all: cancelled {res.get('cancelled')} | open orders now {n}")
            if n == 0:
                return 0
        except ApiError as e:
            print(f"cancel-all attempt {attempt + 1}: {e}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    return 1


if __name__ == "__main__":
    sys.exit(main())
