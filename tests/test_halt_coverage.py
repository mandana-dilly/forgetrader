"""HALT coverage net (brief wheel-prep-1125 T2, milestone 10, D35). Never the
network, never a key, never a live file.

docs/HALT_COVERAGE.md lists every HALT token the engine can write. These are
the classes that had only pure reconcile / build_snapshot unit tests and no
run-path test, forced here through the REAL run.run() with
tests/test_run_stages.py's _drive harness (real build_snapshot, real
reconcile_and_handle, canned read_broker_financials, faked clients; _drive
also asserts its own T2 / T6 / T7 checks per run). Each case runs dry and
live (the timers run --live; a reconcile-class HALT returns before the
submit gate either way) and asserts:
  exit 4; one runs row MODE:TOKEN...; a decisions row carrying the HALT
  action; last_run.outcome == the runs row; state.json unchanged beyond
  last_run and generated_at (no money refresh, stage not moved); a
  scans/state_halt_*.txt dump; one alert, the HALT outcome.

  HC1 holding_basis_invalid        HOLDING with cost_basis null
  HC2 broker_snapshot['shares']    short the wheel ticker (D19a: act before open)
  HC3 FLAT                         hand-edited FLAT wheel with a ticker, 100 shares
  HC4 HOLDING                      HOLDING, broker shows 0 shares
  HC5 CC_OPEN                      call open, 50 shares (D19a: act before open)
  HC6 CC_OPEN                      call gone, 100 shares, before expiry
  HC7 unknown                      wheel.stage not one of STAGES
  HC8 snapshot                     tracked contract qty -2 (D19a: act before open)
"""
import copy
import hashlib
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import test_run_stages as trs  # noqa: E402  (sets FORGE_INSTANCE=a)
import forgetrader_state as fs  # noqa: E402

_REAL_STATE = Path(fs.STATE_PATH)
_REAL_JOURNAL = Path(fs.JOURNAL_PATH)
_REAL_SCANS = Path(fs.SCANS_DIR)

PUT, CALL = trs.PUT, trs.CC_SYM  # F261120P00012000, F261218C00013500
pos = trs._pos


def _seed(stage, contract=None, **wheel):
    s = trs._state(stage, contract)
    s["wheel"].update(wheel)
    return s


# (name, seed state, financials, exact HALT action as reconcile / run writes it)
CASES = [
    ("HC1 holding_basis_invalid", _seed(fs.HOLDING, cost_basis=None),
     trs._fin([pos("F", 100)]),
     "HALT:holding_basis_invalid cost_basis=None"),
    ("HC2 broker_snapshot['shares']", _seed(fs.HOLDING),
     trs._fin([pos("F", -100)]),
     "HALT:broker_snapshot['shares'] not a non-negative int: -100"),
    ("HC3 FLAT", _seed(fs.FLAT, ticker="F"),
     trs._fin([pos("F", 100)]),
     "HALT:FLAT but broker shows shares=100 put_open=False call_open=False"),
    ("HC4 HOLDING", _seed(fs.HOLDING),
     trs._fin([]),
     "HALT:HOLDING but broker shows shares=0 put_open=False call_open=False"),
    ("HC5 CC_OPEN call open short of cover", _seed(fs.CC_OPEN, CALL),
     trs._fin([pos("F", 50), pos(CALL, -1)]),
     "HALT:CC_OPEN call still open but broker shows shares=50"),
    ("HC6 CC_OPEN call gone before expiry", _seed(fs.CC_OPEN, CALL),
     trs._fin([pos("F", 100)]),
     "HALT:CC_OPEN contract gone before expiry 2026-12-18 (as_of 2026-10-20)"),
    ("HC7 unknown", _seed("BOGUS"),
     trs._fin([]),
     "HALT:unknown stage 'BOGUS'"),
    ("HC8 snapshot tracked contract qty", _seed(fs.CSP_OPEN, PUT),
     trs._fin([pos(PUT, -2)]),
     f"HALT:snapshot tracked contract {PUT} qty -2, expected -1"),
]


def _strip(state):
    return {k: v for k, v in state.items() if k not in ("last_run", "generated_at")}


def _sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def check_case(t, name, seed, fin, action, live):
    mode = "live" if live else "dryrun"
    label = f"{name} {mode}"
    seed = copy.deepcopy(seed)
    want = copy.deepcopy(seed)
    o = trs._drive(t, label, seed, fin, live=live, is_open=True)
    outcome = f"{mode}:{action}"
    t.ok(o["ret"] == 4, f"{label} exit {o['ret']!r} != 4")
    t.ok(o["runs"] == [{"outcome": outcome}], f"{label} runs {o['runs']!r}")
    halts = [d["action"] for d in o["decisions"] if d["action"].startswith("HALT:")]
    t.ok(halts == [action], f"{label} HALT decision rows {halts!r}")
    st = o["state"] or {}
    t.ok((st.get("last_run") or {}).get("outcome") == outcome,
         f"{label} last_run {st.get('last_run')!r}")
    t.ok(_strip(st) == _strip(want), f"{label} state written beyond last_run")
    t.ok(len(o["halt_dumps"]) == 1, f"{label} dumps {o['halt_dumps']!r}")
    t.ok(len(o["sent"]) == 1 and o["sent"][0].startswith(outcome + " stage="),
         f"{label} alert {o['sent']!r}")
    t.ok(o["submit_calls"] == [] and len(o["fin_calls"]) == 1,
         f"{label} submits {len(o['submit_calls'])} financial reads {len(o['fin_calls'])}")


def main():
    t = trs.T()
    before = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
              sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)
    scenarios = 0
    for name, seed, fin, action in CASES:
        for live in (False, True):
            scenarios += 1
            try:
                check_case(t, name, seed, fin, action, live)
            except Exception as e:  # a crash is reported, never a silent pass
                t.ok(False, f"{name} live={live} crashed: {type(e).__name__}: {e}")
    after = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
             sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)
    print(f"isolation state.json sha256 before={before[0]} after={after[0]}")
    print(f"isolation journal.db sha256 before={before[1]} after={after[1]}")
    t.ok(before == after, "ISOLATION: real state.json / journal.db / scans changed")
    if t.failures:
        for f in t.failures:
            print("  " + f)
        print(f"FAIL: {len(t.failures)} failed of {t.n} assertions")
        return 1
    print(f"PASS: test_halt_coverage {scenarios} scenarios (8 classes x dry live), "
          f"{t.n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
