"""Shared state for automatic parameter changes made by sigbot.shadow and sigbot.allocator.

params/auto_applied.json   key -> {value, from, config_value, applied_at, by, evidence}: the values the bot set on itself.
                           Re-applied on the next start ONLY if config.json still holds the value it held when the change
                           was made (config_value); if someone edited config.json since, the manual value wins and the entry
                           is dropped.
data/auto_tuning.log       append-only audit trail of every automatic change, re-application and drop.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(ROOT, "params", "auto_applied.json")
LOG_PATH = os.path.join(ROOT, "data", "auto_tuning.log")
_lock = threading.RLock()          # re-entrant: reapply_on_start() calls audit() while holding it


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict:
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save(state: dict):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_PATH)


def audit(line: str):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with _lock, open(LOG_PATH, "a") as f:
            f.write(f"{_now_iso()} {line}\n")
    except OSError:
        pass


def get_value(bot, key: str) -> Any:
    """Live value of a tunable on the running bot."""
    if key == "exit_band":
        return bot.cfg.exit_band
    sleeve, name = key.split(".", 1)
    if sleeve == "direction":
        return getattr(bot.direction.cfg, name)
    return getattr(getattr(bot.cfg, sleeve), name)


def set_value(bot, key: str, value: Any):
    if key == "exit_band":
        bot.cfg.exit_band = value
        return
    sleeve, name = key.split(".", 1)
    if sleeve == "direction":
        setattr(bot.direction.cfg, name, value)
    else:
        setattr(getattr(bot.cfg, sleeve), name, value)


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def record(bot, key: str, value: Any, by: str, evidence: dict, config_value: Any):
    """Apply value to the live bot and remember it (with the config.json value it overrides) for the next start."""
    old = get_value(bot, key)
    set_value(bot, key, value)
    with _lock:
        st = load()
        prev = st.get(key)
        st[key] = {"value": value, "from": old, "config_value": prev["config_value"] if prev else config_value,
                   "applied_at": _now_iso(), "by": by, "evidence": evidence}
        save(st)
    audit(f"APPLIED by={by} {key}: {json.dumps(old)} -> {json.dumps(value)} evidence={json.dumps(evidence, sort_keys=True)}")


def reapply_on_start(bot, startup_values: dict, allowed: Callable[[str, Any], bool], by: str) -> list:
    """startup_values: key -> value the bot loaded from config.json at start. Re-applies remembered values whose
    config_value still matches; drops the rest (manual edits win). Only entries written by `by` are handled."""
    done = []
    with _lock:
        st = load()
        changed = False
        for key in list(st):
            e = st[key]
            if e.get("by") != by or key not in startup_values:
                continue
            if not _same(startup_values[key], e.get("config_value")):
                audit(f"DROPPED by={by} {key}: config.json now {json.dumps(startup_values[key])} (was {json.dumps(e.get('config_value'))} "
                      f"when {json.dumps(e.get('value'))} was applied); the manual value wins")
                del st[key]; changed = True
                continue
            if not allowed(key, e.get("value")):
                audit(f"DROPPED by={by} {key}: remembered value {json.dumps(e.get('value'))} is outside the current hard bounds")
                del st[key]; changed = True
                continue
            try:
                set_value(bot, key, e["value"])
                done.append(key)
                audit(f"REAPPLIED by={by} {key}: {json.dumps(startup_values[key])} -> {json.dumps(e['value'])} (applied {e.get('applied_at')})")
            except Exception as ex:   # attribute missing on this build: forget it
                audit(f"DROPPED by={by} {key}: could not set ({ex})")
                del st[key]; changed = True
        if changed:
            save(st)
    return done
