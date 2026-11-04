"""Cached Kalshi call: others_k.py <path> k=v k=v ...  (all via net.kalshi, shared rate limit). Prints a compact summary; raw JSON stays in raw/."""
import os, sys, json, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from others_common import *
def kcall(path, params=None):
    params = {k: str(v) for k, v in (params or {}).items()}
    key = hashlib.md5((path + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()[:12]
    f = os.path.join(RAW, "k_" + path.strip("/").replace("/", "_")[:60] + "_" + key + ".json")
    def go():
        code, d = net.kalshi(path, params)
        return {"code": code, "data": d} if d is not None else None
    return net.cached(f, go), f
if __name__ == "__main__":
    path = sys.argv[1]; params = dict(a.split("=", 1) for a in sys.argv[2:])
    r, f = kcall(path, params)
    print(f, r["code"] if r else None)
    d = (r or {}).get("data") or {}
    for k, v in d.items():
        print(k, type(v).__name__, len(v) if hasattr(v, "__len__") else v)
