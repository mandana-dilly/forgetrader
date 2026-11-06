"""Offline net for the forgetrader_state.py CLI (Brief 3: --set-state removed).

  G  REGRESSION GUARD: --set-state is gone. `--set-state FLAT` and
     `--show --set-state FLAT` against an existing non-FLAT temp state exit 2
     (argparse) and the temp state's sha256 is unchanged; no journal is
     written. A removal cannot be bitten by a single-line mutation, so this
     is a guard, not a proof.
  I  --init refusal (D7 exit 3): an existing state, valid or empty, is
     refused, unchanged, with no broker read and no journal write. A missing
     state is written FLAT with client_order_id None (positive control).

STATE_PATH is resolved from __file__ (SCRIPT_DIR), not the cwd, so each
scenario copies the CURRENT forgetrader_state.py into its own mkdtemp dir
and runs that copy as a subprocess (cwd = the same dir). The copy's
state.json / journal.db therefore live in the temp dir only. A stub
credentials.py in the temp dir shadows the real one (the script's dir is
sys.path[0]), so read_broker_financials raises before any client exists,
even under a mutation that lets --init through. ALPACA_* and PYTHONPATH are
dropped from the child env. A guard hashes the real state.json / journal.db
before vs after.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import forgetrader_state as fs

SOURCE = Path(ROOT) / "forgetrader_state.py"
REAL_FILES = [Path(ROOT) / "state.json", Path(ROOT) / "journal.db"]
STUB = ('def load_credentials():\n'
        '    raise RuntimeError("test stub: no broker in test_state_cli")\n')
STUB_MARK = "test stub: no broker"

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


def sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def sandbox():
    d = Path(tempfile.mkdtemp(prefix="test_state_cli."))
    shutil.copy2(SOURCE, d / "forgetrader_state.py")
    (d / "credentials.py").write_text(STUB)
    return d


def run_cli(d, argv):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ALPACA_") and k != "PYTHONPATH"}
    p = subprocess.run([sys.executable, str(d / "forgetrader_state.py")] + argv,
                       cwd=str(d), env=env, capture_output=True, text=True,
                       timeout=60)
    print(f"  argv={argv} exit={p.returncode} stderr={p.stderr.strip()[-160:]!r}")
    return p


def seed_state(d, stage):
    """Existing temp state via the real atomic writer, stage forced."""
    s = fs.fresh_flat_state(last_outcome="fixture")
    s["wheel"]["stage"] = stage
    fs.write_state(s, path=d / "state.json")


def part_g():
    d = sandbox()
    seed_state(d, "CSP_PENDING")
    before = sha(d / "state.json")
    p = run_cli(d, ["--set-state", "FLAT"])
    check(p.returncode == 2, f"G1 --set-state FLAT: exit {p.returncode} want 2")
    check(sha(d / "state.json") == before, "G1 temp state.json changed")
    check(not (d / "journal.db").exists(), "G1 journal.db written")
    check("usage:" in p.stderr, "G1 stderr has no argparse usage line")

    p = run_cli(d, ["--show", "--set-state", "FLAT"])
    check(p.returncode == 2, f"G2 --show --set-state FLAT: exit {p.returncode} want 2")
    check(sha(d / "state.json") == before, "G2 temp state.json changed")
    check("unrecognized arguments: --set-state FLAT" in p.stderr,
          "G2 stderr does not name --set-state as unrecognized")
    shutil.rmtree(d)


def part_i():
    d = sandbox()
    seed_state(d, "CSP_OPEN")
    before = sha(d / "state.json")
    p = run_cli(d, ["--init"])
    check(p.returncode == 3, f"I1 --init over existing state: exit {p.returncode} want 3")
    check(sha(d / "state.json") == before, "I1 existing temp state.json changed")
    check("REFUSE:" in p.stderr, "I1 stderr has no REFUSE: line")
    check(not (d / "journal.db").exists(), "I1 journal.db written")
    check(STUB_MARK not in p.stderr, "I1 broker read attempted before refusal")
    shutil.rmtree(d)

    d = sandbox()
    (d / "state.json").write_bytes(b"")
    before = sha(d / "state.json")
    p = run_cli(d, ["--init"])
    check(p.returncode == 3, f"I2 --init over empty state: exit {p.returncode} want 3")
    check(sha(d / "state.json") == before, "I2 empty temp state.json changed")
    check("REFUSE:" in p.stderr, "I2 stderr has no REFUSE: line")
    shutil.rmtree(d)

    d = sandbox()
    p = run_cli(d, ["--init"])
    state = None
    if (d / "state.json").exists():
        state = json.loads((d / "state.json").read_text())
    check(p.returncode == 0, f"I3 --init with no state: exit {p.returncode} want 0")
    check(state is not None, "I3 no state.json written")
    check(state is not None and state["wheel"]["stage"] == "FLAT",
          "I3 written stage is not FLAT")
    check(state is not None and state["wheel"]["client_order_id"] is None,
          "I3 written client_order_id is not None")
    check(STUB_MARK in p.stderr, "I3 broker read did not hit the credentials stub")
    check((d / "journal.db").exists(), "I3 journal.db not written")
    shutil.rmtree(d)


def main():
    before = [sha(p) for p in REAL_FILES]
    for part in (part_g, part_i):
        try:
            part()
        except Exception as e:  # a crash is reported, never a silent pass
            check(False, f"{part.__name__} crashed: {type(e).__name__}: {e}")
    after = [sha(p) for p in REAL_FILES]
    print(f"isolation state.json/journal.db sha256 before={before} after={after}")
    check(before == after, "ISOLATION: real state.json / journal.db changed")
    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_state_cli set-state guard + init refusal, {n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
