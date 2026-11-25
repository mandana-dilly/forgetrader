"""Recovery drill simulation for account B (brief wheel-prep-1125 T4,
milestone 14, funding criterion 5). Never the network, never a key, never a
live file: every account dir is a mkdtemp dir named forgetrader-b.

docs/drills/RECOVERY_DRILL_B.md is the paste-ready drill. This suite walks
the same steps in a tempdir against the file-backed FakeBroker of
tests/test_forced_paths.py (fp.engine_env: real fs paths re-pointed, fake
credentials and clients, alerts / alert_stub to files), with
FORGE_INSTANCE=b, for BOTH post-1-Dec stages:

  D1 FLAT     (CC_OPEN called away: wheel fresh, broker holds nothing)
  D2 HOLDING  (CC expired: wheel HOLDING BAC basis 48.00, broker 100 BAC)

Each drill, in order (steps numbered as in the doc):
  2  timestamped copies of state.json and journal.db, sha256 equal
  3a --init refuses while state.json exists (exit 3, file untouched)
  3  delete state.json
  4  --dry-run: exit 4, HALT:state_missing, exact stdout / stderr lines,
     one runs row dryrun:HALT:state_missing, no state.json created, no
     clock or broker read, one alert
  5  real `forgetrader_state.py --init` (main(["--init"])) against the
     FakeBroker: exit 0, exact stdout, FLAT state with broker positions,
     journal rows init / init; for HOLDING the jq edit of the doc (stage,
     ticker, cost_basis from the copy) written to a temp file in the same
     dir, then renamed over
  6  --dry-run: exit 0, dryrun:noop:<STAGE> stable, RUN OK line, the scan
     for that stage ran, zero submit_order calls; the rebuilt wheel equals
     the copied wheel
  R  rollback: a wrong rebuild (FLAT while the broker holds 100 BAC) HALTs
     (snapshot, exit 4); restoring the copy makes the dry-run noop again
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import test_forced_paths as fp  # noqa: E402  (imports test_run_stages: FORGE_INSTANCE=a)

os.environ["FORGE_INSTANCE"] = "b"  # account B

import alpaca.trading.client as _alp_trading  # noqa: E402
import forgetrader_state as fs  # noqa: E402
import wheel_dryrun  # noqa: E402
from forgetrader import cc_scan as _cc_scan_mod  # noqa: E402
from forgetrader.run import run  # noqa: E402

_REAL_STATE = Path(fs.STATE_PATH)
_REAL_JOURNAL = Path(fs.JOURNAL_PATH)
_REAL_SCANS = Path(fs.SCANS_DIR)

UTC = timezone.utc
CLOCK = datetime(2026, 12, 2, 20, 0, tzinfo=UTC)  # after 1 Dec, outside B's timer minutes
BASIS = 48.0
MONEY = {"equity": 5100.0, "cash": 300.0, "options_buying_power": 300.0}
FRESH_WHEEL = {"stage": "FLAT", "ticker": None, "cost_basis": None, "intent": None,
               "contract_symbol": None, "strike": None, "expiry": None,
               "opened_at": None, "client_order_id": None}
STAGES = {
    "FLAT": {"wheel": FRESH_WHEEL, "positions": []},
    "HOLDING": {"wheel": dict(FRESH_WHEEL, stage="HOLDING", ticker="BAC", cost_basis=BASIS),
                "positions": [["BAC", 100]]},
}

t = fp.trs.T()


def _sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


class CountingBroker(fp.FakeBroker):
    CALLS = []

    def get_clock(self):
        CountingBroker.CALLS.append("get_clock")
        return super().get_clock()

    def get_account(self):
        CountingBroker.CALLS.append("get_account")
        return super().get_account()

    def submit_order(self, req):
        CountingBroker.CALLS.append("submit_order")
        raise AssertionError("TRIPWIRE: submit_order in a recovery drill")


def _scans(d):
    def flat(*a, **k):
        fp._log(d / "scans.log", "scan_watchlist")
        return fp.trs._scan_result([])

    def cc(*a, **k):
        fp._log(d / "scans.log", "scan_calls")
        return types.SimpleNamespace(
            ticker="BAC", cost_basis=BASIS, candidates=[], all_rows=[],
            reject_tally=Counter(), rejection=None, chain_fetch_errors=[],
            wl_rejected=[], wl_warnings=[])
    return flat, cc


def engine(d, fn):
    """fn() inside fp.engine_env(d) with the counting broker and empty scans.
    Returns (result, stdout, stderr, broker calls, journal rows added)."""
    before = fp._journal(d)
    CountingBroker.CALLS = []
    out, err = io.StringIO(), io.StringIO()
    with fp.engine_env(d):
        _alp_trading.TradingClient = CountingBroker  # engine_env restores the original
        wheel_dryrun.scan_watchlist, _cc_scan_mod.scan_calls = _scans(d)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                res = fn()
            except Exception as e:  # reported by the caller's rc assertion
                res = f"EXC {type(e).__name__}: {e}"
    after = fp._journal(d)
    rows = {k: after[k][len(before[k]):] for k in after}
    return res, out.getvalue(), err.getvalue(), list(CountingBroker.CALLS), rows


def seed(stage):
    d = Path(tempfile.mkdtemp(prefix="recovery_drill_")) / "forgetrader-b"
    d.mkdir()
    spec = STAGES[stage]
    fs.pin_now(CLOCK - timedelta(days=1))
    try:
        s = fs.fresh_flat_state(**MONEY)
        s["wheel"] = dict(spec["wheel"])
        s["positions"] = [{"symbol": sym, "qty": q, "avg_entry_price": 1.0,
                           "market_value": 1.0} for sym, q in spec["positions"]]
        s["last_run"] = {"ts": (CLOCK - timedelta(days=1)).isoformat(),
                         "outcome": f"live:noop:{stage} stable"}
        fs.write_state(s, d / "state.json")
        conn = fs.journal_connect(d / "journal.db")
        fs.record_run(conn, fs.new_run_id(), f"live:noop:{stage} stable")
        conn.close()
    finally:
        fs.pin_now(None)
    fp.broker_save(d / "broker.json", {
        "clock_ts": CLOCK.isoformat(), "money": dict(MONEY),
        "positions": [list(p) for p in spec["positions"]], "orders": {},
        "submits": [], "submit_mode": "accept"})
    return d


def dry(d):
    return engine(d, lambda: run(live=False))


def init(d):
    return engine(d, lambda: fs.main(["--init"]))


def jq_edit(d, backup_state):
    """The doc's step 5 HOLDING edit: jq sets stage / ticker / cost_basis from
    the copy into a temp file in the same dir, then mv over state.json."""
    s = json.loads((d / "state.json").read_text())
    w = backup_state["wheel"]
    s["wheel"]["stage"], s["wheel"]["ticker"] = "HOLDING", w["ticker"]
    s["wheel"]["cost_basis"] = w["cost_basis"]
    tmp = d / "state.json.drill.tmp"
    tmp.write_text(json.dumps(s, indent=2) + "\n")
    os.replace(tmp, d / "state.json")


def expect_noop(tag, d, stage, res, out, calls, rows, scan):
    st = json.loads((d / "state.json").read_text())
    outcome = f"dryrun:noop:{stage} stable"
    t.ok(res == 0, f"{tag} dry-run exit {res!r}")
    t.ok(rows["runs"] == [outcome], f"{tag} runs {rows['runs']!r}")
    t.ok(rows["decisions"][:1] == [f"noop:{stage} stable"], f"{tag} decisions {rows['decisions']!r}")
    t.ok(st["last_run"]["outcome"] == outcome, f"{tag} last_run {st['last_run']!r}")
    t.ok(f"RUN OK  stage={stage}  action=noop:{stage} stable  state: {d / 'state.json'}" in out,
         f"{tag} RUN OK line {out!r}")
    t.ok(f"SCAN {scan}" in out, f"{tag} scan line {out!r}")
    t.ok("submit_order" not in calls and calls.count("get_clock") == 1,
         f"{tag} broker calls {calls!r}")
    return st


def drill(stage):
    tag = f"D-{stage}"
    d = seed(stage)
    state_p, journal_p = d / "state.json", d / "journal.db"
    try:
        # 2. timestamped copies
        bk = d.parent / "drill-b-20261202T200000Z"
        bk.mkdir()
        shutil.copy2(state_p, bk / "state.json")
        shutil.copy2(journal_p, bk / "journal.db")
        t.ok(_sha(bk / "state.json") == _sha(state_p) and _sha(bk / "journal.db") == _sha(journal_p),
             f"{tag} copies differ")
        backup = json.loads((bk / "state.json").read_text())

        # 3a. --init refuses while state.json exists
        sha0 = _sha(state_p)
        res, out, err, calls, rows = init(d)
        t.ok(res == 3 and "REFUSE: " in err and _sha(state_p) == sha0 and calls == [],
             f"{tag} init over existing state rc={res!r} err={err!r} calls={calls!r}")

        # 3. delete state.json
        state_p.unlink()

        # 4. --dry-run must HALT:state_missing
        res, out, err, calls, rows = dry(d)
        t.ok(res == 4, f"{tag} state_missing exit {res!r}")
        t.ok(rows["runs"] == ["dryrun:HALT:state_missing"] and rows["decisions"] == [],
             f"{tag} state_missing rows {rows!r}")
        t.ok(out == "", f"{tag} state_missing stdout {out!r}")
        t.ok(err == f"HALT state_missing: {state_p} absent; run --init deliberately\n",
             f"{tag} state_missing stderr {err!r}")
        t.ok(fp._lines(d / "alerts.log")[-1:] ==
             [f"HALT state_missing | {state_p} absent; run --init deliberately"],
             f"{tag} alert_stub {fp._lines(d / 'alerts.log')!r}")
        t.ok(fp._lines(d / "sent.log")[-1:] == ["dryrun:HALT:state_missing stage=None ticker=None"],
             f"{tag} telegram {fp._lines(d / 'sent.log')!r}")
        t.ok(not state_p.exists() and not (d / "state.json.tmp").exists(),
             f"{tag} state_missing created a state file")
        t.ok(calls == [], f"{tag} state_missing touched the broker {calls!r}")

        # 5. rebuild: --init (broker facts), then the HOLDING edit
        res, out, err, calls, rows = init(d)
        t.ok(res == 0, f"{tag} init exit {res!r} err={err!r}")
        t.ok(out == "broker facts read OK (equity/cash/options_bp/positions populated)\n"
                    f"wrote fresh FLAT state at {state_p}\n", f"{tag} init stdout {out!r}")
        t.ok(rows["runs"] == ["init"] and rows["decisions"] == ["init"], f"{tag} init rows {rows!r}")
        t.ok(calls == ["get_account"], f"{tag} init broker calls {calls!r}")
        st = json.loads(state_p.read_text())
        t.ok(st["wheel"] == FRESH_WHEEL, f"{tag} init wheel {st['wheel']!r}")
        t.ok([(p["symbol"], p["qty"]) for p in st["positions"]]
             == [tuple(p) for p in STAGES[stage]["positions"]], f"{tag} init positions {st['positions']!r}")
        t.ok((st["cash"], st["equity"], st["options_buying_power"])
             == (MONEY["cash"], MONEY["equity"], MONEY["options_buying_power"]),
             f"{tag} init money {st!r}")
        if stage == "HOLDING":
            jq_edit(d, backup)

        # 6. --dry-run must be noop:<STAGE>
        res, out, err, calls, rows = dry(d)
        scan = "all_rejected" if stage == "FLAT" else "cc_all_rejected"
        st = expect_noop(tag, d, stage, res, out, calls, rows, scan)
        t.ok(st["wheel"] == backup["wheel"], f"{tag} rebuilt wheel {st['wheel']!r} != copy")
        t.ok(_sha(bk / "state.json") is not None and _sha(bk / "journal.db") is not None,
             f"{tag} copies gone")
        conn = sqlite3.connect(str(journal_p))
        n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        conn.close()
        t.ok(n_runs == 4, f"{tag} journal runs rows {n_runs} != seed + HALT + init + noop")
    finally:
        shutil.rmtree(d.parent, ignore_errors=True)


def rollback():
    """A wrong rebuild HALTs; the rollback block restores the copy."""
    tag = "R"
    d = seed("HOLDING")
    state_p = d / "state.json"
    try:
        bk = d.parent / "drill-b-20261202T200000Z"
        bk.mkdir()
        shutil.copy2(state_p, bk / "state.json")
        state_p.unlink()
        init(d)  # rebuild left at FLAT: the HOLDING edit was skipped
        res, out, err, calls, rows = dry(d)
        t.ok(res == 4 and rows["runs"] == ["dryrun:HALT:snapshot unexpected position BAC qty 100"],
             f"{tag} wrong rebuild rc={res!r} runs={rows['runs']!r}")
        # rollback: copy back to a temp name in the same dir, then mv over
        shutil.copy2(bk / "state.json", d / "state.json.rollback.tmp")
        os.replace(d / "state.json.rollback.tmp", state_p)
        t.ok(_sha(state_p) == _sha(bk / "state.json"), f"{tag} restored copy differs")
        res, out, err, calls, rows = dry(d)
        expect_noop(tag, d, "HOLDING", res, out, calls, rows, "cc_all_rejected")
    finally:
        shutil.rmtree(d.parent, ignore_errors=True)


def main():
    before = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
              sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)
    scenarios = 0
    for fn, arg in ((drill, "FLAT"), (drill, "HOLDING"), (rollback, None)):
        scenarios += 1
        try:
            fn(arg) if arg else fn()
        except Exception as e:  # a crash is reported, never a silent pass
            t.ok(False, f"{fn.__name__} {arg} crashed: {type(e).__name__}: {e}")
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
    print(f"PASS: test_recovery_drill {scenarios} scenarios (FLAT HOLDING rollback), "
          f"{t.n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
