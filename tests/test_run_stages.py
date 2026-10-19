"""Offline stage-matrix net for forgetrader/run.py (Brief 12b.2).

Drives run() at several wheel stages with the REAL build_snapshot and REAL
reconcile_and_handle. Patched seams: credentials.load_credentials,
fs.read_broker_financials (canned per scenario; kwargs + call count recorded),
wheel_dryrun.scan_watchlist (AssertionError if called), fs.alert_stub
(captured). fs.STATE_PATH / JOURNAL_PATH / SCANS_DIR point at a mkdtemp dir
(load_state/write_state/journal_connect re-read the module attribute, same
idiom as test_propose_csp). A guard hashes the real state.json / journal.db and
lists the real scans/ before vs after.

Scenarios:
  S1 Ford CSP_OPEN, put gone, 100 F      -> HOLDING + hold:stage2_not_built
  S2 HOLDING stable                      -> noop:HOLDING stable + hold row
  S3 FLAT seed, 100 F in broker          -> HALT:snapshot (unexpected position)
  S4 CSP_OPEN put open AND 100 F         -> reconcile HALT (not snapshot)
  S5 CSP_PENDING awaiting fill           -> tracked coid passed to the read
  S6 no state.json                       -> HALT:state_missing, nothing created
"""

import contextlib
import hashlib
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import credentials
import forgetrader_state as fs
import wheel_dryrun
from forgetrader.run import run

_REAL_STATE = Path(fs.STATE_PATH)
_REAL_JOURNAL = Path(fs.JOURNAL_PATH)
_REAL_SCANS = Path(fs.SCANS_DIR)

PUT = "F261120P00012000"
COID = "cid-test-1"
SEED = {"equity": 1300.0, "cash": 1300.0, "options_buying_power": 1300.0}
CANNED_MONEY = {"equity": 1500.0, "cash": 1400.0, "options_buying_power": 1450.0}


def _pos(sym, qty):
    return {"symbol": sym, "qty": qty, "avg_entry_price": 1.0, "market_value": 1.0}


def _fin(positions=(), orders=(), tracked=None):
    d = dict(CANNED_MONEY)
    d.update({"positions": list(positions), "open_orders": list(orders),
              "tracked_order_status": tracked})
    return d


def _state(stage, contract=None, coid=None, ticker="F"):
    s = fs.fresh_flat_state(**SEED)
    if stage != fs.FLAT:
        s["wheel"].update({"stage": stage, "ticker": ticker,
                           "contract_symbol": contract})
        if coid is not None:
            s["wheel"]["client_order_id"] = coid
    return s


def _sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _drive(seed_state, financials):
    """Returns a dict of observations. Restores every patch in finally."""
    tmp = Path(tempfile.mkdtemp(prefix="run_stages_"))
    tmp_state, tmp_journal, tmp_scans = tmp / "state.json", tmp / "journal.db", tmp / "scans"

    orig = {k: getattr(fs, k) for k in (
        "STATE_PATH", "JOURNAL_PATH", "SCANS_DIR", "load_state", "write_state",
        "journal_connect", "read_broker_financials", "alert_stub")}
    orig_creds = credentials.load_credentials
    orig_scan = wheel_dryrun.scan_watchlist
    ol, ow, oj = orig["load_state"], orig["write_state"], orig["journal_connect"]

    obs = {"fin_calls": [], "cred_calls": 0, "alerts": [], "scan_called": False,
           "tmp_state": tmp_state, "tmp_scans": tmp_scans}

    def fake_fin(trading_client=None, tracked_client_order_id=None):
        obs["fin_calls"].append({"tracked_client_order_id": tracked_client_order_id})
        return dict(financials)

    def fake_creds():
        obs["cred_calls"] += 1
        return ("k", "s", True)

    def fake_scan(*a, **k):
        obs["scan_called"] = True
        raise AssertionError("scan_watchlist must not be called in this scenario")

    out, err = io.StringIO(), io.StringIO()
    try:
        fs.STATE_PATH, fs.JOURNAL_PATH, fs.SCANS_DIR = tmp_state, tmp_journal, tmp_scans
        fs.load_state = lambda path=None: ol(path or fs.STATE_PATH)
        fs.write_state = lambda state, path=None: ow(state, path or fs.STATE_PATH)
        fs.journal_connect = lambda path=None: oj(path or fs.JOURNAL_PATH)
        fs.read_broker_financials = fake_fin
        fs.alert_stub = lambda msg: obs["alerts"].append(msg)
        credentials.load_credentials = fake_creds
        wheel_dryrun.scan_watchlist = fake_scan
        if seed_state is not None:
            ow(seed_state, tmp_state)

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            obs["ret"] = run(dry_run=True)

        conn = sqlite3.connect(str(tmp_journal))
        conn.row_factory = sqlite3.Row
        obs["decisions"] = [dict(r) for r in conn.execute(
            "SELECT stage_from, stage_to, action, detail FROM decisions ORDER BY rowid")]
        obs["runs"] = [dict(r) for r in conn.execute("SELECT outcome FROM runs")]
        conn.close()
        obs["stdout"], obs["stderr"] = out.getvalue(), err.getvalue()
        obs["state"] = ol(tmp_state) if tmp_state.exists() else None
        obs["halt_dumps"] = (sorted(p.name for p in tmp_scans.glob("state_halt_*.txt"))
                             if tmp_scans.exists() else [])
        return obs
    finally:
        for k, v in orig.items():
            setattr(fs, k, v)
        credentials.load_credentials = orig_creds
        wheel_dryrun.scan_watchlist = orig_scan


class T:
    def __init__(self):
        self.failures, self.n = [], 0

    def ok(self, cond, msg):
        self.n += 1
        if not cond:
            self.failures.append(msg)


def _money(state):
    return {k: state[k] for k in SEED}


def main():
    t = T()
    before = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
              sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)

    # S1 -------------------------------------------------------------------
    o = _drive(_state(fs.CSP_OPEN, PUT, COID), _fin([_pos("F", 100)]))
    t.ok(o["ret"] == 0, f"S1 exit {o['ret']!r} != 0")
    t.ok(o["state"]["wheel"]["stage"] == fs.HOLDING, "S1 stage not HOLDING")
    acts = [d["action"] for d in o["decisions"]]
    t.ok(acts == [f"{fs.CSP_OPEN}->{fs.HOLDING} (assigned)",
                    "hold:stage2_not_built"],
         f"S1 decisions {acts!r}")
    t.ok("WARNING: HOLDING" in o["stderr"], "S1 stderr missing WARNING")
    t.ok(not o["scan_called"], "S1 scan called")
    t.ok(o["fin_calls"] == [{"tracked_client_order_id": None}],
         f"S1 fin kwargs {o['fin_calls']!r}")
    t.ok(_money(o["state"]) == CANNED_MONEY, f"S1 money {_money(o['state'])!r}")

    # S2 -------------------------------------------------------------------
    o = _drive(_state(fs.HOLDING, None, None), _fin([_pos("F", 100)]))
    t.ok(o["ret"] == 0, f"S2 exit {o['ret']!r} != 0")
    acts = [d["action"] for d in o["decisions"]]
    t.ok(acts == ["noop:HOLDING stable", "hold:stage2_not_built"],
         f"S2 decisions {acts!r}")
    t.ok(o["decisions"][-1]["detail"] == "noop:HOLDING stable", "S2 hold detail")
    t.ok(o["runs"] == [{"outcome": "dryrun:noop:HOLDING stable"}], f"S2 runs {o['runs']!r}")
    t.ok(not o["scan_called"], "S2 scan called")

    # S3 -------------------------------------------------------------------
    s3 = _state(fs.FLAT)
    o = _drive(s3, _fin([_pos("F", 100)]))
    t.ok(o["ret"] == 4, f"S3 exit {o['ret']!r} != 4")
    t.ok(o["state"]["wheel"]["stage"] == fs.FLAT, "S3 stage changed")
    t.ok(len(o["halt_dumps"]) == 1, f"S3 dumps {o['halt_dumps']!r}")
    t.ok(len(o["alerts"]) == 1, f"S3 alerts {o['alerts']!r}")
    t.ok(_money(o["state"]) == SEED, f"S3 money refreshed on HALT: {_money(o['state'])!r}")
    t.ok(not o["scan_called"], "S3 scan called")
    t.ok(len(o["decisions"]) == 1 and o["decisions"][0]["action"].startswith(
        "HALT:snapshot unexpected position F"), f"S3 decisions {o['decisions']!r}")
    t.ok(len(o["runs"]) == 1 and o["runs"][0]["outcome"].startswith(
        "dryrun:HALT:snapshot "), f"S3 runs {o['runs']!r}")

    # S4 -------------------------------------------------------------------
    o = _drive(_state(fs.CSP_OPEN, PUT, COID), _fin([_pos("F", 100), _pos(PUT, -1)]))
    t.ok(o["ret"] == 4, f"S4 exit {o['ret']!r} != 4")
    t.ok(len(o["decisions"]) == 1, f"S4 decisions {o['decisions']!r}")
    a = o["decisions"][0]["action"] if o["decisions"] else ""
    t.ok(a.startswith("HALT:") and not a.startswith("HALT:snapshot"), f"S4 action {a!r}")
    t.ok(a.startswith("HALT:CSP_OPEN put still open"), f"S4 action {a!r}")
    t.ok(len(o["alerts"]) == 1, f"S4 alerts {o['alerts']!r}")
    t.ok(len(o["halt_dumps"]) == 1, f"S4 dumps {o['halt_dumps']!r}")
    t.ok(o["state"]["wheel"]["stage"] == fs.CSP_OPEN, "S4 stage changed")
    t.ok(_money(o["state"]) == SEED, "S4 money refreshed on HALT")
    t.ok(not o["scan_called"], "S4 scan called")

    # S5 -------------------------------------------------------------------
    order = {"id": "oid-1", "client_order_id": COID, "symbol": PUT, "status": "new"}
    o = _drive(_state(fs.CSP_PENDING, PUT, COID), _fin(orders=[order], tracked="new"))
    t.ok(o["ret"] == 0, f"S5 exit {o['ret']!r} != 0")
    t.ok([d["action"] for d in o["decisions"]] == ["noop:CSP_PENDING awaiting fill"],
         f"S5 decisions {o['decisions']!r}")
    t.ok(o["fin_calls"] == [{"tracked_client_order_id": COID}],
         f"S5 fin kwargs {o['fin_calls']!r}")
    t.ok(o["state"]["wheel"]["stage"] == fs.CSP_PENDING, "S5 stage changed")
    t.ok(not o["scan_called"], "S5 scan called")

    # S6 -------------------------------------------------------------------
    o = _drive(None, _fin())
    t.ok(o["ret"] == 4, f"S6 exit {o['ret']!r} != 4")
    t.ok(o["runs"] == [{"outcome": "HALT:state_missing"}], f"S6 runs {o['runs']!r}")
    t.ok(len(o["alerts"]) == 1 and "state_missing" in o["alerts"][0],
         f"S6 alerts {o['alerts']!r}")
    t.ok(not o["tmp_state"].exists(), "S6 state.json was created")
    t.ok(len(o["fin_calls"]) == 0, "S6 read_broker_financials called")
    t.ok(o["cred_calls"] == 0, "S6 load_credentials called")
    t.ok("state_missing" in o["stderr"], "S6 stderr reason missing")

    # ISOLATION ------------------------------------------------------------
    after = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
             sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)
    print(f"isolation state.json sha256 before={before[0]} after={after[0]}")
    print(f"isolation journal.db sha256 before={before[1]} after={after[1]}")
    print(f"isolation scans/ entries before={len(before[2] or [])} after={len(after[2] or [])}")
    t.ok(before == after, "ISOLATION: real state.json / journal.db / scans changed")

    if t.failures:
        print("FAIL")
        for f in t.failures:
            print("  " + f)
        return 1
    print(f"PASS: 6 scenarios + 1 isolation guard, {t.n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
