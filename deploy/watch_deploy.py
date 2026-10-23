"""Post-deploy watch: every 30 s for N minutes, check that the newly deployed bot is not losing money through its own new
trades, and stop it (orders cancelled) if it is. Sizhe's rule (22 Oct): "after pushing a change monitor continuously at
30 sec intervals for 10 minutes ... if we get drawdown that is due to new trades, stop and bugfix".

    python3 deploy/watch_deploy.py [--minutes 10] [--every 30] [--max-drop 150]

Each tick (DB and log only, plus 2 API reads every other tick for the settlement value):
  * settlement value vs the value at the start (positions marked as review.py does: arb sets at their locked payout)
  * actions that have churned before: arb-set unwinds, exits, offload exits, failed baskets, kill switch, tracebacks
  * whether the bot process is alive
Stops the bot via deploy/mac/botctl.sh stop and exits 2 when: settlement value falls more than --max-drop below the start,
or more than 3 offload unwinds, more than 30 recycles, or more than 40 exits, or a traceback / kill switch appears. Exit 0 when the window passes cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "data", "live.log")


def settlement_value() -> float | None:
    try:
        out = subprocess.run([sys.executable, "-m", "sigbot.review", "--minutes", "5", "--json"], cwd=ROOT, capture_output=True,
                             text=True, timeout=90).stdout
        return float(json.loads(out)["settlement_value"])
    except Exception:
        return None


TS = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")


def log_since(ts0: str) -> list[str]:
    """Lines logged at or after ts0. Lines without their own timestamp (traceback frames, wrapped output) belong to the
    last timestamped line before them; they used to be compared as text and counted as new (false alarm, 22 Oct 21:47)."""
    out, last = [], ""
    with open(LOG, errors="replace") as f:
        for line in f:
            m = TS.match(line)
            if m:
                last = m.group(0)
            if last >= ts0:
                out.append(line)
    return out


NET = re.compile(r"URLError|urlopen error|gaierror|nodename nor servname|Can't assign requested address|ConnectionResetError|"
                 r"RemoteDisconnected|TimeoutError|timed out|Network is unreachable|ssl\.SSL")
EXC = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Interrupt)\b")


def code_tracebacks(lines: list[str]) -> int:
    """Tracebacks NOT caused by the Mac's network dropping (DNS failures, closed sockets): those are retried and recovered
    by the bot and are not a bug in a deploy (false stop on 23 Oct 15:54, network loss while the Mac went on AC)."""
    n = 0
    for i, l in enumerate(lines):
        if "Traceback" not in l:
            continue
        exc = next((x for x in lines[i + 1:i + 80] if EXC.match(x.strip())), "")
        if not NET.search(exc):
            n += 1
    return n


def running() -> bool:
    ps = subprocess.run(["ps", "-eo", "command"], capture_output=True, text=True).stdout
    return any("sigbot.run --live" in l and "caffeinate" not in l for l in ps.splitlines())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--every", type=float, default=30)
    ap.add_argument("--max-drop", type=float, default=150.0)
    a = ap.parse_args()
    t_start = time.time()
    ts0 = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    v0 = settlement_value()
    print(f"{ts0} watch start: settlement value {v0}, {a.minutes:.0f} min at {a.every:.0f} s", flush=True)
    tick, v = 0, v0
    while time.time() - t_start < a.minutes * 60:
        time.sleep(a.every)
        tick += 1
        lines = log_since(ts0)
        c = {k: sum(1 for l in lines if p.search(l)) for k, p in {
            "unwinds": re.compile(r"UNWIND \("), "recycles": re.compile(r"RECYCLE "), "exits": re.compile(r"ZERO-EV EXIT|OFFLOAD: exit \d"),
            "failed_baskets": re.compile(r"not filled atomically"), "kill": re.compile(r"KILL SWITCH"),
            "traceback": re.compile(r"Traceback"), "arb_fills": re.compile(r"INFO sigbot.arb: ARB ")}.items()}
        c["traceback"] = code_tracebacks(lines)
        if tick % 2 == 0:
            v = settlement_value() or v
        drop = (v0 - v) if (v0 is not None and v is not None) else 0.0
        alive = running()
        print(f"{datetime.now():%H:%M:%S} tick {tick}: settlement {v} (drop {drop:+.1f}) alive={alive} {c}", flush=True)
        reason = None
        if drop > a.max_drop:
            reason = f"settlement value fell {drop:.0f} since the deploy"
        elif c["unwinds"] > 3:
            reason = f"{c['unwinds']} offload unwinds in {tick * a.every:.0f} s"
        elif c["recycles"] > 30:
            # each recycle must beat the set it closes by recycle_min_gain (settlement value rises); the simulation's
            # standing-sell-set scenarios run 16-42 per 10 min legitimately, so only a rate far above that suggests a loop
            reason = f"{c['recycles']} recycles in {tick * a.every:.0f} s (each must beat the set it closes; this many suggests a loop)"
        elif c["exits"] > 40:
            reason = f"{c['exits']} exits in {tick * a.every:.0f} s"
        elif c["traceback"] or c["kill"]:
            reason = "traceback or kill switch after the deploy"
        elif not alive:
            reason = "bot process not running"
        if reason:
            print(f"ALERT: {reason}; stopping the bot", flush=True)
            subprocess.run([os.path.join(ROOT, "deploy", "mac", "botctl.sh"), "stop"], cwd=ROOT)
            with open(os.path.join(ROOT, "data", "changes.md"), "a") as f:
                f.write(f"- {datetime.now():%H:%M} POST-DEPLOY WATCH STOPPED THE BOT: {reason} (start value {v0}, now {v}; {c}).\n")
            sys.exit(2)
    print(f"{datetime.now():%H:%M:%S} watch passed: settlement {v0} -> {v}", flush=True)


if __name__ == "__main__":
    main()
