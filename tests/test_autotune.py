# Run: python3 tests/test_autotune.py "$PWD"   (from the repo root; ~3 min; temp root, real DB read-only, never touches live files)
"""End-to-end checks of the scheduled shadow tuner + allocator with a fake bot, in a temporary root (never the live files)."""
import json, os, shutil, sys, tempfile, time, types, logging

REPO = sys.argv[1]
sys.path.insert(0, REPO)
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
from sigbot import shadow, allocator, tuned
from sigbot.config import BotConfig

tmp = tempfile.mkdtemp(prefix="autotune_test_")
os.makedirs(os.path.join(tmp, "params"))
os.symlink(os.path.join(REPO, "data"), os.path.join(tmp, "data"))           # read-only use of the real DB
for f in ("fair_explained.json", "x_live.json", "x.json"):
    if os.path.exists(os.path.join(REPO, "params", f)):
        shutil.copy(os.path.join(REPO, "params", f), os.path.join(tmp, "params", f))
tuned.STATE_PATH = os.path.join(tmp, "params", "auto_applied.json")
tuned.LOG_PATH = os.path.join(tmp, "auto_tuning.log")
raw = json.load(open(os.path.join(REPO, "config.json")))


def write_cfg(auto):
    r = dict(raw); r["shadow"] = {"auto_apply": auto}; r["allocator"] = {"auto_apply": auto}
    json.dump(r, open(os.path.join(tmp, "config.json"), "w"))


def new_bot():
    b = types.SimpleNamespace()
    b.cfg = BotConfig.load(os.path.join(REPO, "config.json"))
    d = raw.get("direction", {})
    b.direction = types.SimpleNamespace(cfg=types.SimpleNamespace(min_edge_take=d.get("min_edge_take", 0.02), overall_cash_frac=d.get("overall_cash_frac", 0.04)))
    b.cash = 57000.0
    return b


def new_tuner(b):
    t = shadow.ShadowTuner.from_config(b.cfg, tmp)
    b._shadow_tuner = t
    return t


def run_group(t, b, g):
    """Make only group g due, tick to start it, wait, tick to consume."""
    now = time.time()
    t.last_run = {k: now for k in t.schedule}
    t.last_run[g] = 0.0
    t.tick(b)
    assert t._thread is not None and t._thread.name == f"shadow-{g}", t._thread
    t._thread.join()
    return t.tick(b)


fails = []
def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


# 1. bootstrap edge cases
check(shadow.bootstrap_confidence([]) == 0.0, "bootstrap: no blocks -> 0")
check(shadow.bootstrap_confidence([0, 0, 0]) == 0.5, "bootstrap: all-zero -> 0.5")
check(shadow.bootstrap_confidence([1, 2, 3, 4, 5, 6, 7, 8]) == 1.0, "bootstrap: all positive -> 1")
check(shadow.bootstrap_confidence([-1] * 8) == 0.0, "bootstrap: all negative -> 0")
c = shadow.bootstrap_confidence([5, -1, 2, -1, 3, 0, -2, 4])
check(0.5 < c < 1.0, f"bootstrap: mixed -> {c:.3f}")

# 2. schedule ordering
write_cfg(False)
b = new_bot(); t = new_tuner(b)
check(t.due_groups()[0] == "arb", f"fresh state: arb most overdue first ({t.due_groups()})")
t.last_run = {g: time.time() for g in t.schedule}
check(t.due_groups() == [], "nothing due right after a run")
t.last_run["mm_ladder"] = time.time() - 25 * 3600
check(t.due_groups() == ["mm_ladder"], "mm_ladder due after 25 h")

# 3. auto_apply off: runs, logs, applies nothing, streak persisted
write_cfg(False)
b = new_bot(); t = new_tuner(b)
before = b.cfg.arb.min_ret
c0 = time.process_time(); applied = run_group(t, b, "arb")
check(applied == [] and b.cfg.arb.min_ret == before, f"auto_apply off: nothing applied (min_ret {b.cfg.arb.min_ret})")
st = json.load(open(os.path.join(tmp, "params", "shadow_state.json")))
check("arb" in st["last_run"], "schedule state saved")
rep = json.load(open(os.path.join(tmp, "params", "shadow_report.json")))
ap = rep["groups"]["arb"]["params"]["arb.min_ret"]
print("   arb.min_ret:", ap["current"], "->", ap["best"], ap["reason"], "gain", ap["oos_gain"], "conf", ap["confidence"], "cpu", rep["groups"]["arb"]["cpu_sec"])
rec_min_ret = ap["recommend"]

# 4. auto_apply on: needs 2 confirming runs, then applies, persists, audits
write_cfg(True)
for f in ("shadow_state.json", "auto_applied.json"):
    p = os.path.join(tmp, "params", f)
    if os.path.exists(p):
        os.remove(p)
b = new_bot(); b.cfg.arb.min_ret = 0.035; before = 0.035     # deliberately off, so the real data produces a recommendation
t = new_tuner(b)
a1 = run_group(t, b, "arb")
ap = json.load(open(os.path.join(tmp, "params", "shadow_report.json")))["groups"]["arb"]["params"]["arb.min_ret"]
print("   from 0.035:", ap["best"], ap["reason"], "gain", ap["oos_gain"], "conf", ap["confidence"])
rec_min_ret = ap["recommend"]
check(a1 == [] and b.cfg.arb.min_ret == before, "first recommending run: not applied yet (confirmation 1/2)")
a2 = run_group(t, b, "arb")
if rec_min_ret:
    best = ap["best"]
    check("arb.min_ret" in a2 and abs(b.cfg.arb.min_ret - best) < 1e-12, f"second run: applied arb.min_ret {before} -> {b.cfg.arb.min_ret}")
    stt = tuned.load()
    check(stt.get("arb.min_ret", {}).get("config_value") == before, "persisted with the config.json value it overrides")
    check("APPLIED by=shadow arb.min_ret" in open(tuned.LOG_PATH).read(), "audit log line written")
    # 5. restart, config.json unchanged -> re-applied
    b2 = new_bot(); b2.cfg.arb.min_ret = 0.035; t2 = new_tuner(b2)
    t2.last_run = {g: time.time() for g in t2.schedule}
    t2.tick(b2)
    check(abs(b2.cfg.arb.min_ret - best) < 1e-12, f"restart with unchanged config: re-applied ({b2.cfg.arb.min_ret})")
    # 6. restart after a manual config edit -> manual value wins, entry dropped
    b3 = new_bot(); b3.cfg.arb.min_ret = 0.027; t3 = new_tuner(b3)
    t3.last_run = {g: time.time() for g in t3.schedule}
    t3.tick(b3)
    check(b3.cfg.arb.min_ret == 0.027 and "arb.min_ret" not in tuned.load(), "restart after manual edit: manual value kept, entry dropped")
else:
    print("   (arb.min_ret not recommended on this data: apply path tested with a synthetic recommendation below)")

# 7. apply() guards
b = new_bot(); t = new_tuner(b); t.startup_values = {}
cur = b.cfg.arb.max_race_frac
r = t.apply([{"key": "arb.max_race_frac", "from": cur, "to": 0.5, "oos_gain": 99, "confidence": 1.0}], b)
check(r == [] and b.cfg.arb.max_race_frac == cur, "out-of-bounds value refused (max_race_frac 0.5)")
r = t.apply([{"key": "mm.meanrev_max_ticks", "from": 2.0, "to": 1.0, "oos_gain": 99, "confidence": 1.0}], b)
check(r == [] and b.cfg.mm.meanrev_max_ticks == 2.0, "report-only key refused (meanrev)")
r = t.apply([{"key": "kill_drawdown", "from": 400, "to": 4000, "oos_gain": 99, "confidence": 1.0}], b)
check(r == [] and b.cfg.kill_drawdown == 400, "kill switch not tunable")
r = t.apply([{"key": "exit_band", "from": 0.02, "to": 0.015, "oos_gain": 99, "confidence": 1.0}], b)
check(r == [] and b.cfg.exit_band == raw["exit_band"], "stale 'from' (live value changed since replay) -> skipped")
r = t.apply([{"key": "exit_band", "from": raw["exit_band"], "to": 0.015, "oos_gain": 99, "confidence": 1.0}], b)
check(r == ["exit_band"] and b.cfg.exit_band == 0.015, "valid recommendation applied (exit_band)")
r = t.apply([{"key": "direction.min_edge_take", "from": b.direction.cfg.min_edge_take, "to": 0.05, "oos_gain": 99, "confidence": 1.0}], b)
check(r == ["direction.min_edge_take"] and b.direction.cfg.min_edge_take == 0.05, "direction key set on bot.direction.cfg")
r = t.apply([{"key": "mm.level_ticks", "from": b.cfg.mm.level_ticks, "to": [0, 2, 3, 5], "oos_gain": 99, "confidence": 1.0}], b)
check(r == [], "invalid ladder refused")

# 8. every group runs within the CPU budget on a fake bot, no exceptions
write_cfg(False)
b = new_bot(); t = new_tuner(b)
for g in ("direction", "mm_quote", "mm_ladder"):
    run_group(t, b, g)
rep = json.load(open(os.path.join(tmp, "params", "shadow_report.json")))
for g, gr in rep["groups"].items():
    check("error" not in gr and gr["cpu_sec"] <= t.cpu_budget_sec + 1.0, f"group {g}: no error, cpu {gr['cpu_sec']} s, skipped {gr.get('skipped')}")

# 9. a failing run never raises out of tick
b = new_bot(); t = new_tuner(b)
t.run = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
now = time.time(); t.last_run = {g: now for g in t.schedule}; t.last_run["arb"] = 0
try:
    t.tick(b); t._thread.join(); t.tick(b); ok = True
except Exception as e:
    ok = False
check(ok, "exception inside a run does not escape tick()")

# 10. allocator through the shadow tick (temp root), report written, guards
write_cfg(True)
for f in ("allocator_state.json",):
    if os.path.exists(os.path.join(tmp, "params", f)):
        os.remove(os.path.join(tmp, "params", f))
b = new_bot(); t = new_tuner(b); t.last_run = {g: time.time() for g in t.schedule}
t.tick(b)
al = b._allocator
check(al.root == tmp and al.auto_apply, "allocator created by the shadow tick with the same root and its own auto_apply flag")
al._thread.join(); t.tick(b)
check(os.path.exists(os.path.join(tmp, "params", "allocation.json")), "allocation.json written in the temp root")
check(not os.path.exists(os.path.join(tmp, "params", "allocation.json.tmp")), "no temp file left behind")
r = al.apply([{"key": "arb.capital_frac", "from": b.cfg.arb.capital_frac, "to": 0.9}], b)
check(r == [] and b.cfg.arb.capital_frac == raw["arb"]["capital_frac"], "allocator refuses arb.capital_frac above the 0.70 ceiling")
r = al.apply([{"key": "mm.capital_frac", "from": b.cfg.mm.capital_frac, "to": 0.10, "target": 0.05}], b)
check(r == ["mm.capital_frac"] and b.cfg.mm.capital_frac == 0.10, "allocator applies an in-bounds change")
r = al.apply([{"key": "kill_drawdown", "from": 400, "to": 1}], b)
check(r == [], "allocator refuses keys it does not own")
# confirmation: a consistent change needs two runs
al.streak = {}
rep = al.run(cash=57000.0)
n1 = {k: v for k, v in al.streak.items()}
print("   allocator changes after 1 run:", rep["changes"], "streak", n1)
check(all(c.get("confirmations", 0) >= 2 for c in rep["changes"]), "allocator changes only after 2 confirming runs")

print("\nRESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
print("audit log:\n" + (open(tuned.LOG_PATH).read() if os.path.exists(tuned.LOG_PATH) else "(empty)"))
shutil.rmtree(tmp, ignore_errors=True)
