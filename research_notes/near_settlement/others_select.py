"""Selection of legs (top two by price at T-10, 2c..98c) and Kalshi market registry helpers."""
import os, sys, json, glob, re, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from others_common import *
EXCLUDE = {"venezuela-2024", "honduras-2025", "argentina-2025", "france-2024"}   # resolved on a different date than the scheduled poll close / disputed
def load_pm():
    c = [dict(o, category="fed") for o in json.load(open(os.path.join(RAW, "pm_candidates_fed.json")))]
    c += json.load(open(os.path.join(RAW, "pm_candidates_other.json")))
    if os.path.exists(os.path.join(RAW, "pm_candidates_other_extra.json")): c += json.load(open(os.path.join(RAW, "pm_candidates_other_extra.json")))
    c = [o for o in c if o["cluster"] not in EXCLUDE]
    for o in c: o["subject"] = o["contract"][len(o["cluster"]) + 1:].split("-")[0] if o["category"] != "fed" else "fed"
    return c
def select_pm(cands, k=2):
    by = collections.defaultdict(list)
    for o in cands:
        if o["outcome"] is None or o["n"] < 24 or o["p10"] is None: continue
        by[(o["cluster"], o["subject"])].append(o)
    sel = []
    for key, v in by.items():
        v.sort(key=lambda o: -o["p10"])
        sel += [o for o in v[:k] if .02 <= o["p10"] <= .98]
    return sel
def kalshi_markets():
    """ticker -> market dict, from every cached list call"""
    out = {}
    for f in glob.glob(os.path.join(RAW, "k_historical_markets_[0-9a-f]*.json")) + glob.glob(os.path.join(RAW, "k_markets_[0-9a-f]*.json")):
        for m in json.load(open(f))["data"].get("markets", []):
            if "ticker" in m and "event_ticker" in m: out[m["ticker"]] = m
    return out
def norm(s): return re.sub(r"[^a-z0-9 ]", "", (s or "").lower())
