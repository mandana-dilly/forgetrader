"""Offline stage-matrix net for forgetrader/run.py (Brief 12b.2).

Drives run() at several wheel stages with the REAL build_snapshot, REAL
reconcile_and_handle and REAL submit.place_csp. Patched seams:
credentials.load_credentials (paper flag per scenario),
fs.read_broker_financials (canned per scenario; kwargs + call count recorded),
wheel_dryrun.scan_watchlist (AssertionError if called unless a scan is given;
the clock kwarg it receives is recorded), fs.alert_stub (captured), and the
three alpaca client classes run.py builds (FakeTC / FakeData: every
construction and every broker method call is counted; get_clock returns a
recorded clock object; any method a scenario does not expect raises).
fs.STATE_PATH / JOURNAL_PATH / SCANS_DIR point at a mkdtemp dir
(load_state/write_state/journal_connect re-read the module attribute, same
idiom as test_propose_csp). A guard hashes the real state.json / journal.db and
lists the real scans/ before vs after.

Checked inside _drive for EVERY scenario (Brief 2):
  T2 dry-run tripwire: in every dry-run scenario FakeTC.submit_order raises if
     called, and zero submit calls is asserted.
  T6 get_clock is called exactly once per run (0 where the run stops before
     the clock), and a scan, when called, receives that same clock object.
  T7 every runs.outcome and last_run.outcome starts with dryrun: or live:, and
     no stdout / stderr / outcome text has a redirect token followed by a path.

Scenarios:
  S1 Ford CSP_OPEN, put gone, 100 F      -> HOLDING + hold:stage2_not_built
  S2 HOLDING stable                      -> noop:HOLDING stable + hold row
  S3 FLAT seed, 100 F in broker          -> HALT:snapshot (unexpected position)
  S4 CSP_OPEN put open AND 100 F         -> reconcile HALT (not snapshot)
  S5 CSP_PENDING awaiting fill           -> tracked coid passed to the read
  S6 no state.json                       -> HALT:state_missing, nothing created
  S7 FLAT, scan raises WatchlistStructureError -> HALT:watchlist_structure
  S8 tz-naive broker clock               -> HALT:clock, financials never read
  S9 CSP_OPEN put gone shares 0, clock before expiry (ET) -> reconcile HALT
  S10 same, clock on expiry (ET)         -> FLAT
  S6L no state.json, --live              -> live:HALT:state_missing
Brief 2 live scenarios (L*):
  L1 all five submit conditions true     -> exactly one submit, submitted
  L2-L6 each condition false alone       -> zero submits (live, paper,
     action, proposal, is_open), plus final_pass not True on a passing row
  L7 not_paper                           -> exit 3, zero broker calls
  L8 submit_order raises after write-ahead -> HALT:submit, CSP_PENDING kept
  L9 place_csp refuses (bid off 2dp)     -> refused, state untouched, exit 0
The broker clock is faked on FakeTC.get_clock (Brief 13a.2).
"""

import contextlib
from collections import Counter
import datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import types
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import alpaca.data.historical.option as _alp_option
import alpaca.data.historical.stock as _alp_stock
import alpaca.trading.client as _alp_trading
import credentials
import forgetrader_state as fs
import wheel_dryrun
from alpaca.trading.enums import OrderStatus
from forgetrader.run import _proposal_line, run
from watchlist import WatchlistStructureError

_REAL_STATE = Path(fs.STATE_PATH)
_REAL_JOURNAL = Path(fs.JOURNAL_PATH)
_REAL_SCANS = Path(fs.SCANS_DIR)

PUT = "F261120P00012000"
COID = "cid-test-1"
SEED = {"equity": 1300.0, "cash": 1300.0, "options_buying_power": 1300.0}
UTC = datetime.timezone.utc
CLOCK_DEFAULT = datetime.datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
CANNED_MONEY = {"equity": 1500.0, "cash": 1400.0, "options_buying_power": 1450.0}
CLOCK_OPEN = datetime.datetime(2026, 11, 4, 15, 30, tzinfo=UTC)
LIVE_SYM = "NOK261204P00007500"
# A passing proposal row: the 7 keys _proposal_line reads plus the 4 keys
# place_csp validates. The real producer (scan_watchlist) feeding the real
# validator is proven in test_live_submit.py (T1 seam).
LIVE_ROW = {"underlying_symbol": "NOK", "contract_symbol": LIVE_SYM,
            "strike": 7.5, "expiration_date": "2026-12-04", "bid": 0.16,
            "open_interest": 655, "annualized_yield_pct": 25.9556,
            "collateral": 750.0, "final_pass": True}
REDIRECT_RE = re.compile(r"(->|>|<)\s*/")
PREFIXES = ("dryrun:", "live:")
ALL_OUTCOMES = []  # (scenario, outcome) from every runs row and last_run


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


def _drive(t, name, seed_state, financials, clock_ts=CLOCK_DEFAULT, scan=None,
           live=False, paper=True, is_open=False, submit_raises=None,
           expect_clock=1):
    """Returns a dict of observations. Restores every patch in finally.
    Asserts the per-scenario T2 / T6 / T7 checks on `t` before returning."""
    tmp = Path(tempfile.mkdtemp(prefix="run_stages_"))
    tmp_state, tmp_journal, tmp_scans = tmp / "state.json", tmp / "journal.db", tmp / "scans"

    orig = {k: getattr(fs, k) for k in (
        "STATE_PATH", "JOURNAL_PATH", "SCANS_DIR", "load_state", "write_state",
        "journal_connect", "read_broker_financials", "alert_stub")}
    orig_creds = credentials.load_credentials
    orig_scan = wheel_dryrun.scan_watchlist
    orig_clients = (_alp_trading.TradingClient, _alp_stock.StockHistoricalDataClient,
                    _alp_option.OptionHistoricalDataClient)
    ol, ow, oj = orig["load_state"], orig["write_state"], orig["journal_connect"]

    obs = {"fin_calls": [], "cred_calls": 0, "alerts": [], "scan_called": False, "clock_calls": 0,
           "tmp_state": tmp_state, "tmp_scans": tmp_scans, "clients_built": 0,
           "broker_calls": [], "submit_calls": [], "clock_obj": None,
           "scan_clock": "NOT_CALLED", "disk_at_submit": None}

    def fake_fin(trading_client=None, tracked_client_order_id=None):
        obs["fin_calls"].append({"tracked_client_order_id": tracked_client_order_id})
        return dict(financials)

    def fake_creds():
        obs["cred_calls"] += 1
        return ("k", "s", paper)

    def fake_scan(*a, **k):
        obs["scan_called"] = True
        obs["scan_clock"] = k.get("clock", "NOT_PASSED")
        if scan is None:
            raise AssertionError("scan_watchlist must not be called in this scenario")
        return scan(*a, **k)

    class FakeOrder:
        def __init__(self):
            self.id = uuid.uuid4()
            self.status = OrderStatus.ACCEPTED

    class FakeTC:
        def __init__(self, *a, **k):
            obs["clients_built"] += 1

        def get_clock(self):
            obs["clock_calls"] += 1
            obs["broker_calls"].append("get_clock")
            obs["clock_obj"] = types.SimpleNamespace(timestamp=clock_ts, is_open=is_open)
            return obs["clock_obj"]

        def submit_order(self, req):
            obs["broker_calls"].append("submit_order")
            obs["submit_calls"].append(req)
            obs["disk_at_submit"] = ol(tmp_state) if tmp_state.exists() else None
            if not live:
                raise AssertionError("TRIPWIRE: submit_order called in a dry-run")
            if submit_raises is not None:
                raise submit_raises
            return FakeOrder()

        def __getattr__(self, attr):
            def unexpected(*a, **k):
                obs["broker_calls"].append(attr)
                raise AssertionError(f"unexpected broker call {attr}")
            return unexpected

    class FakeData(FakeTC):
        pass

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
        _alp_trading.TradingClient = FakeTC
        _alp_stock.StockHistoricalDataClient = FakeData
        _alp_option.OptionHistoricalDataClient = FakeData
        if seed_state is not None:
            ow(seed_state, tmp_state)
        obs["seed_sha"] = _sha(tmp_state)

        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            obs["ret"] = run(live=live)

        conn = sqlite3.connect(str(tmp_journal))
        conn.row_factory = sqlite3.Row
        obs["decisions"] = [dict(r) for r in conn.execute(
            "SELECT stage_from, stage_to, action, detail FROM decisions ORDER BY rowid")]
        obs["runs"] = [dict(r) for r in conn.execute("SELECT outcome FROM runs")]
        obs["events"] = [dict(r) for r in conn.execute(
            "SELECT event_type, detail FROM events ORDER BY rowid")]
        obs["orders"] = [dict(r) for r in conn.execute(
            "SELECT status, client_order_id, broker_order_id FROM orders ORDER BY rowid")]
        conn.close()
        obs["stdout"], obs["stderr"] = out.getvalue(), err.getvalue()
        obs["state"] = ol(tmp_state) if tmp_state.exists() else None
        obs["state_sha"] = _sha(tmp_state)
        obs["halt_dumps"] = (sorted(p.name for p in tmp_scans.glob("state_halt_*.txt"))
                             if tmp_scans.exists() else [])
    finally:
        for k, v in orig.items():
            setattr(fs, k, v)
        credentials.load_credentials = orig_creds
        wheel_dryrun.scan_watchlist = orig_scan
        (_alp_trading.TradingClient, _alp_stock.StockHistoricalDataClient,
         _alp_option.OptionHistoricalDataClient) = orig_clients

    # T2 dry-run tripwire: every dry-run scenario, zero submit calls.
    if not live:
        t.ok(obs["submit_calls"] == [],
             f"{name} T2 TRIPWIRE: dry-run called submit_order {len(obs['submit_calls'])}x")
    # T6 one clock per run; the scan gets that same object.
    t.ok(obs["clock_calls"] == expect_clock,
         f"{name} T6 get_clock calls {obs['clock_calls']} != {expect_clock}")
    if obs["scan_called"]:
        t.ok(obs["scan_clock"] is obs["clock_obj"] and obs["clock_obj"] is not None,
             f"{name} T6 scan clock {obs['scan_clock']!r} is not the run clock")
    # T7 mode prefix on every outcome + no redirect-shaped token before a path.
    outs = [r["outcome"] for r in obs["runs"]]
    if obs["state"] is not None and "last_run" in obs["state"] and obs["state_sha"] != obs["seed_sha"]:
        outs.append(obs["state"]["last_run"]["outcome"])
    ALL_OUTCOMES.extend((name, x) for x in outs)
    want = "live:" if live else "dryrun:"
    t.ok(len(obs["runs"]) == 1 and all(x.startswith(want) for x in outs),
         f"{name} T7 outcomes {outs!r} not all {want}")
    text = obs["stdout"] + obs["stderr"] + "\n".join(outs)
    t.ok(not REDIRECT_RE.search(text), f"{name} T7 redirect token in output")
    return obs


class T:
    def __init__(self):
        self.failures, self.n = [], 0

    def ok(self, cond, msg):
        self.n += 1
        if not cond:
            self.failures.append(msg)


def _scan_result(wl_rejected, passing_rows=()):
    return types.SimpleNamespace(
        passing_rows=list(passing_rows), approved=[], reject_tally=Counter(),
        wl_rejected=wl_rejected, clock=types.SimpleNamespace(is_open=False))


def _wl_events(o):
    return [e for e in o["events"] if e["event_type"] == "watchlist_rejected"]


def _money(state):
    return {k: state[k] for k in SEED}


def main():
    t = T()
    before = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
              sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)

    # S1 -------------------------------------------------------------------
    o = _drive(t, "S1", _state(fs.CSP_OPEN, PUT, COID), _fin([_pos("F", 100)]))
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
    o = _drive(t, "S2", _state(fs.HOLDING, None, None), _fin([_pos("F", 100)]))
    t.ok(o["ret"] == 0, f"S2 exit {o['ret']!r} != 0")
    acts = [d["action"] for d in o["decisions"]]
    t.ok(acts == ["noop:HOLDING stable", "hold:stage2_not_built"],
         f"S2 decisions {acts!r}")
    t.ok(o["decisions"][-1]["detail"] == "noop:HOLDING stable", "S2 hold detail")
    t.ok(o["runs"] == [{"outcome": "dryrun:noop:HOLDING stable"}], f"S2 runs {o['runs']!r}")
    t.ok(not o["scan_called"], "S2 scan called")

    # S3 -------------------------------------------------------------------
    s3 = _state(fs.FLAT)
    o = _drive(t, "S3", s3, _fin([_pos("F", 100)]))
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
    o = _drive(t, "S4", _state(fs.CSP_OPEN, PUT, COID), _fin([_pos("F", 100), _pos(PUT, -1)]))
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
    o = _drive(t, "S5", _state(fs.CSP_PENDING, PUT, COID), _fin(orders=[order], tracked="new"))
    t.ok(o["ret"] == 0, f"S5 exit {o['ret']!r} != 0")
    t.ok([d["action"] for d in o["decisions"]] == ["noop:CSP_PENDING awaiting fill"],
         f"S5 decisions {o['decisions']!r}")
    t.ok(o["fin_calls"] == [{"tracked_client_order_id": COID}],
         f"S5 fin kwargs {o['fin_calls']!r}")
    t.ok(o["state"]["wheel"]["stage"] == fs.CSP_PENDING, "S5 stage changed")
    t.ok(not o["scan_called"], "S5 scan called")

    # S6 -------------------------------------------------------------------
    o = _drive(t, "S6", None, _fin(), expect_clock=0)
    t.ok(o["ret"] == 4, f"S6 exit {o['ret']!r} != 4")
    t.ok(o["runs"] == [{"outcome": "dryrun:HALT:state_missing"}], f"S6 runs {o['runs']!r}")
    t.ok(len(o["alerts"]) == 1 and "state_missing" in o["alerts"][0],
         f"S6 alerts {o['alerts']!r}")
    t.ok(not o["tmp_state"].exists(), "S6 state.json was created")
    t.ok(len(o["fin_calls"]) == 0, "S6 read_broker_financials called")
    t.ok(o["cred_calls"] == 0, "S6 load_credentials called")
    t.ok("state_missing" in o["stderr"], "S6 stderr reason missing")

    # S6L: same with --live; the prefix comes from argv, before state load.
    o = _drive(t, "S6L", None, _fin(), live=True, expect_clock=0)
    t.ok(o["ret"] == 4, f"S6L exit {o['ret']!r} != 4")
    t.ok(o["runs"] == [{"outcome": "live:HALT:state_missing"}], f"S6L runs {o['runs']!r}")
    t.ok(not o["tmp_state"].exists(), "S6L state.json was created")
    t.ok(o["cred_calls"] == 0 and o["broker_calls"] == [] and o["clients_built"] == 0,
         f"S6L creds/broker touched: {o['cred_calls']} {o['broker_calls']!r}")

    # S7 -------------------------------------------------------------------
    def bad_watchlist(*a, **k):
        raise WatchlistStructureError("bad")

    o = _drive(t, "S7", _state(fs.FLAT), _fin(), scan=bad_watchlist)
    t.ok(o["ret"] == 4, f"S7 exit {o['ret']!r} != 4")
    t.ok([d["action"] for d in o["decisions"]] ==
         ["noop:FLAT stable", "HALT:watchlist_structure bad"],
         f"S7 decisions {o['decisions']!r}")
    t.ok(sum(d["action"].startswith("HALT:watchlist_structure")
             for d in o["decisions"]) == 1, "S7 HALT row count")
    t.ok(o["runs"] == [{"outcome": "dryrun:HALT:watchlist_structure bad"}],
         f"S7 runs {o['runs']!r}")
    t.ok(o["state"]["last_run"]["outcome"] == "dryrun:HALT:watchlist_structure bad",
         f"S7 last_run {o['state']['last_run']!r}")
    t.ok(o["alerts"] == ["HALT watchlist_structure bad"], f"S7 alerts {o['alerts']!r}")
    t.ok(o["state"]["wheel"]["stage"] == fs.FLAT, "S7 stage changed")

    # S8 -------------------------------------------------------------------
    o = _drive(t, "S8", _state(fs.FLAT), _fin(),
               clock_ts=datetime.datetime(2026, 10, 20, 15, 0))
    t.ok(o["ret"] == 4, f"S8 exit {o['ret']!r} != 4")
    t.ok(len(o["fin_calls"]) == 0, f"S8 financials read: {o['fin_calls']!r}")
    t.ok(o["clock_calls"] == 1, f"S8 clock calls {o['clock_calls']!r}")
    t.ok([d["action"] for d in o["decisions"]] ==
         ["HALT:clock timestamp missing or tz-naive"], f"S8 decisions {o['decisions']!r}")
    t.ok(o["runs"] == [{"outcome": "dryrun:HALT:clock timestamp missing or tz-naive"}],
         f"S8 runs {o['runs']!r}")
    t.ok(len(o["alerts"]) == 1, f"S8 alerts {o['alerts']!r}")
    t.ok(not o["scan_called"], "S8 scan called")

    # S9 / S10: ET conversion decides the expiry date (expiry 2026-11-20) --
    PUT_E = "F261120P00012000"
    o = _drive(t, "S9", _state(fs.CSP_OPEN, PUT_E, COID), _fin(),
               clock_ts=datetime.datetime(2026, 11, 20, 3, 0, tzinfo=UTC))
    t.ok(o["ret"] == 4, f"S9 exit {o['ret']!r} != 4")
    t.ok([d["action"] for d in o["decisions"]] ==
         ["HALT:CSP_OPEN contract gone before expiry 2026-11-20 (as_of 2026-11-19)"],
         f"S9 decisions {o['decisions']!r}")
    t.ok(o["state"]["wheel"]["stage"] == fs.CSP_OPEN, "S9 stage changed")
    t.ok(len(o["alerts"]) == 1, f"S9 alerts {o['alerts']!r}")

    o = _drive(t, "S10", _state(fs.CSP_OPEN, PUT_E, COID), _fin(),
               clock_ts=datetime.datetime(2026, 11, 21, 3, 0, tzinfo=UTC))
    t.ok(o["ret"] == 0, f"S10 exit {o['ret']!r} != 0")
    t.ok(o["state"]["wheel"]["stage"] == fs.FLAT, "S10 stage not FLAT")
    t.ok([d["action"] for d in o["decisions"]] ==
         ["CSP_OPEN->FLAT (expired_worthless)"], f"S10 decisions {o['decisions']!r}")
    t.ok(o["state"]["wheel"]["contract_symbol"] is None, "S10 wheel not reset")
    t.ok(not o["scan_called"], "S10 scan called")

    # T1-T4: wl_rejected visibility + RUN OK line -----------------------------
    o = _drive(t, "T1", _state(fs.FLAT), _fin(), scan=lambda *a, **k: _scan_result([]))
    t.ok(o["ret"] == 0, f"T1 exit {o['ret']!r}")
    t.ok(f"state: {o['tmp_state']}" in o["stdout"], "T1 'state: <path>' missing")
    t.ok("state->" not in o["stdout"], "T1 'state->' still in stdout")
    scan_rows = [d for d in o["decisions"] if d["action"] == "all_rejected"]
    t.ok(len(scan_rows) == 1 and scan_rows[0]["detail"].endswith("wl_rejected=[]"),
         f"T1 decisions {o['decisions']!r}")
    t.ok(_wl_events(o) == [], "T1 watchlist_rejected event present")
    t.ok("WARNING: watchlist_rejected" not in o["stderr"], "T1 stderr warning")

    WL2 = [("XYZ", "earnings_lapsed"), ("ABC", "bad_ticker")]
    R2 = "[XYZ:earnings_lapsed, ABC:bad_ticker]"
    o = _drive(t, "T2", _state(fs.FLAT), _fin(), scan=lambda *a, **k: _scan_result(WL2))
    scan_rows = [d for d in o["decisions"] if d["action"] == "all_rejected"]
    t.ok(len(scan_rows) == 1, f"T2 scan decision rows {o['decisions']!r}")
    det = scan_rows[0]["detail"] if scan_rows else ""
    t.ok(f"  {det}\n" in o["stdout"], "T2 printed detail != journaled detail")
    t.ok(f"wl_rejected={R2}" in det, f"T2 detail {det!r}")
    t.ok([e["detail"] for e in _wl_events(o)] == [R2], f"T2 events {o['events']!r}")
    t.ok(f"WARNING: watchlist_rejected {R2}" in o["stderr"], "T2 stderr warning")

    ROW = {"underlying_symbol": "F", "strike": 12.0,
           "expiration_date": "2026-11-20", "bid": 0.25, "open_interest": 500,
           "annualized_yield_pct": 18.0, "collateral": 1200.0}
    o = _drive(t, "T3", _state(fs.FLAT), _fin(),
               scan=lambda *a, **k: _scan_result([("XYZ", "earnings_lapsed")], [ROW]))
    scan_rows = [d for d in o["decisions"] if d["action"] == "propose_csp"]
    t.ok(len(scan_rows) == 1 and scan_rows[0]["detail"] == _proposal_line(ROW, False),
         f"T3 decisions {o['decisions']!r}")
    t.ok([e["detail"] for e in _wl_events(o)] == ["[XYZ:earnings_lapsed]"],
         f"T3 events {o['events']!r}")
    t.ok("WARNING: watchlist_rejected [XYZ:earnings_lapsed]" in o["stderr"],
         "T3 stderr warning")

    order = {"id": "oid-1", "client_order_id": COID, "symbol": PUT, "status": "new"}
    o = _drive(t, "T4", _state(fs.CSP_PENDING, PUT, COID), _fin(orders=[order], tracked="new"))
    t.ok(_wl_events(o) == [] and not o["scan_called"], "T4 event on non-FLAT stage")
    t.ok(o["ret"] == 0, f"T4 exit {o['ret']!r}")
    t.ok(o["state"]["wheel"]["stage"] == fs.CSP_PENDING, "T4 stage changed")

    # Brief 2 live scenarios ------------------------------------------------
    def live_scan(rows):
        return lambda *a, **k: _scan_result([], rows)

    ALL_TRUE = dict(live=True, paper=True, is_open=True, clock_ts=CLOCK_OPEN,
                    scan=live_scan([LIVE_ROW]))

    # L1: all five submit conditions hold -> exactly one submit (T3 positive)
    o = _drive(t, "L1", _state(fs.FLAT), _fin(), **ALL_TRUE)
    t.ok(o["ret"] == 0, f"L1 exit {o['ret']!r} != 0")
    t.ok(len(o["submit_calls"]) == 1, f"L1 submit calls {len(o['submit_calls'])} != 1")
    req = o["submit_calls"][0] if o["submit_calls"] else None
    coid = getattr(req, "client_order_id", None)
    t.ok(req is not None and req.symbol == LIVE_SYM and req.qty == 1
         and req.limit_price == 0.16, f"L1 request {req!r}")
    w = o["state"]["wheel"]
    t.ok(w["stage"] == fs.CSP_PENDING and w["client_order_id"] == coid
         and coid is not None, f"L1 persisted wheel {w!r} coid {coid!r}")
    t.ok(w["opened_at"] == CLOCK_OPEN.isoformat(), f"L1 opened_at {w['opened_at']!r}")
    # B4: the state place_csp got was money-refreshed (write-ahead on disk at
    # submit time, and the persisted state after).
    das = o["disk_at_submit"]
    t.ok(das is not None and das["wheel"]["stage"] == fs.CSP_PENDING
         and das["wheel"]["client_order_id"] == coid, f"L1 write-ahead {das!r}")
    t.ok(das is not None and _money(das) == CANNED_MONEY,
         f"L1 B4 write-ahead money {das and _money(das)!r}")
    t.ok(_money(o["state"]) == CANNED_MONEY, f"L1 B4 persisted money {_money(o['state'])!r}")
    # B6/B7: broker status printed + journalled, exact token.
    tok = f"live:submit:submitted status=accepted coid={coid}"
    t.ok(o["runs"] == [{"outcome": tok}], f"L1 runs {o['runs']!r}")
    t.ok(o["state"]["last_run"]["outcome"] == tok, f"L1 last_run {o['state']['last_run']!r}")
    t.ok(f"LIVE submit:submitted status=accepted coid={coid}" in o["stdout"],
         f"L1 stdout missing status line: {o['stdout']!r}")
    t.ok([r["status"] for r in o["orders"]] == [None, "accepted"]
         and all(r["client_order_id"] == coid for r in o["orders"]),
         f"L1 orders rows {o['orders']!r}")
    sub_rows = [d for d in o["decisions"] if d["action"].startswith("submit:")]
    t.ok(len(sub_rows) == 1 and sub_rows[0]["action"] == tok[len("live:"):]
         and json.loads(sub_rows[0]["detail"])["status"] == "accepted"
         and sub_rows[0]["stage_to"] == fs.CSP_PENDING,
         f"L1 submit decision {sub_rows!r}")
    t.ok([e["event_type"] for e in o["events"]][-1:] == ["live_complete"],
         f"L1 events {o['events']!r}")

    # L2-L6: each B3 condition false ALONE -> zero submits (T3 negatives)
    o = _drive(t, "L2", _state(fs.FLAT), _fin(), **dict(ALL_TRUE, live=False))
    t.ok(o["ret"] == 0 and o["submit_calls"] == [], f"L2 live=False submitted ret={o['ret']!r}")
    t.ok(o["runs"] == [{"outcome": "dryrun:noop:FLAT stable"}], f"L2 runs {o['runs']!r}")
    t.ok(o["state"]["wheel"] == fs.fresh_flat_state()["wheel"], "L2 wheel changed")
    t.ok("LIVE " not in o["stdout"], "L2 printed a LIVE line in dry-run")

    o = _drive(t, "L3", _state(fs.FLAT), _fin(), expect_clock=0,
               **dict(ALL_TRUE, paper=False))
    t.ok(o["ret"] == 3 and o["submit_calls"] == [], f"L3 paper=False ret={o['ret']!r}")

    cancel = _state(fs.CSP_PENDING, LIVE_SYM, COID, ticker="NOK")
    o = _drive(t, "L4", cancel, _fin(tracked="canceled"), **ALL_TRUE)
    t.ok(o["ret"] == 0 and o["submit_calls"] == [], f"L4 not-FLAT-stable ret={o['ret']!r}")
    t.ok(o["runs"] == [{"outcome": "live:CSP_PENDING->FLAT (cancel_or_expire)"}],
         f"L4 runs {o['runs']!r}")
    t.ok(not o["scan_called"], "L4 scanned on a transition into FLAT")

    o = _drive(t, "L5", _state(fs.FLAT), _fin(), **dict(ALL_TRUE, scan=live_scan([])))
    t.ok(o["ret"] == 0 and o["submit_calls"] == [], f"L5 no proposal ret={o['ret']!r}")
    t.ok(o["runs"] == [{"outcome": "live:skip:no_passing_proposal"}], f"L5 runs {o['runs']!r}")
    t.ok(o["state"]["wheel"] == fs.fresh_flat_state()["wheel"], "L5 wheel changed")

    for nm, fp in (("L5a", ""), ("L5b", "True"), ("L5c", None)):
        row = dict(LIVE_ROW)
        if fp is None:
            del row["final_pass"]
        else:
            row["final_pass"] = fp
        o = _drive(t, nm, _state(fs.FLAT), _fin(), **dict(ALL_TRUE, scan=live_scan([row])))
        t.ok(o["ret"] == 0 and o["submit_calls"] == [],
             f"{nm} final_pass={fp!r} submitted ret={o['ret']!r}")
        t.ok(o["runs"] == [{"outcome": "live:skip:no_passing_proposal"}],
             f"{nm} runs {o['runs']!r}")

    o = _drive(t, "L6", _state(fs.FLAT), _fin(), **dict(ALL_TRUE, is_open=False))
    t.ok(o["ret"] == 0 and o["submit_calls"] == [], f"L6 market closed ret={o['ret']!r}")
    t.ok(o["runs"] == [{"outcome": "live:skip:market_closed"}], f"L6 runs {o['runs']!r}")
    t.ok("LIVE skip:market_closed" in o["stdout"], "L6 stdout missing skip line")
    t.ok(o["state"]["wheel"] == fs.fresh_flat_state()["wheel"], "L6 wheel changed")

    # L7 (T4): not_paper -> exit 3, zero broker calls, run row journalled.
    seed7 = _state(fs.FLAT)
    o = _drive(t, "L7", seed7, _fin(), expect_clock=0, **dict(ALL_TRUE, paper=False))
    t.ok(o["ret"] == 3, f"L7 exit {o['ret']!r} != 3")
    t.ok(o["broker_calls"] == [] and o["clients_built"] == 0 and o["fin_calls"] == [],
         f"L7 broker touched: calls={o['broker_calls']!r} built={o['clients_built']} "
         f"fin={o['fin_calls']!r}")
    t.ok(o["runs"] == [{"outcome": "live:refused:not_paper"}], f"L7 runs {o['runs']!r}")
    t.ok(o["state_sha"] == o["seed_sha"], "L7 state.json was written")
    t.ok("REFUSED not_paper" in o["stderr"], f"L7 stderr {o['stderr']!r}")
    t.ok(not o["scan_called"], "L7 scan called")

    # L8 (T5): submit_order raises after the write-ahead -> HALT:submit, exit 4,
    # persisted state is the CSP_PENDING state place_csp returned.
    o = _drive(t, "L8", _state(fs.FLAT), _fin(),
               **dict(ALL_TRUE, submit_raises=RuntimeError("wire down ->/tmp/x")))
    t.ok(o["ret"] == 4, f"L8 exit {o['ret']!r} != 4")
    t.ok(len(o["submit_calls"]) == 1, f"L8 submit calls {len(o['submit_calls'])}")
    coid = getattr(o["submit_calls"][0], "client_order_id", None) if o["submit_calls"] else None
    w = o["state"]["wheel"]
    t.ok(w["stage"] == fs.CSP_PENDING and w["client_order_id"] == coid and coid is not None,
         f"L8 persisted wheel {w!r}")
    t.ok(_money(o["state"]) == CANNED_MONEY, f"L8 money {_money(o['state'])!r}")
    t.ok(len(o["runs"]) == 1 and o["runs"][0]["outcome"].startswith(
        "live:HALT:submit RuntimeError: wire down"), f"L8 runs {o['runs']!r}")
    t.ok(o["state"]["last_run"]["outcome"] == (o["runs"][0]["outcome"] if o["runs"] else None),
         f"L8 last_run {o['state']['last_run']!r}")
    t.ok("HALT submit" in o["stderr"], f"L8 stderr {o['stderr']!r}")
    t.ok(any(a.startswith("HALT:submit") for a in o["alerts"]), f"L8 alerts {o['alerts']!r}")

    # L9: place_csp refuses (bid not 2dp) -> refused token, state untouched.
    o = _drive(t, "L9", _state(fs.FLAT), _fin(),
               **dict(ALL_TRUE, scan=live_scan([dict(LIVE_ROW, bid=0.155)])))
    t.ok(o["ret"] == 0 and o["submit_calls"] == [], f"L9 ret={o['ret']!r}")
    t.ok(o["runs"] == [{"outcome": "live:submit:refused:bid_not_2dp"}], f"L9 runs {o['runs']!r}")
    t.ok(o["state"]["wheel"] == fs.fresh_flat_state()["wheel"], f"L9 wheel {o['state']['wheel']!r}")
    t.ok(o["orders"] == [], f"L9 orders {o['orders']!r}")

    # T7 aggregate: every outcome of every scenario carries a mode prefix.
    bad = [(n, x) for n, x in ALL_OUTCOMES if not x.startswith(PREFIXES)]
    print(f"T7 outcomes checked={len(ALL_OUTCOMES)} unprefixed={bad!r}")
    t.ok(len(ALL_OUTCOMES) >= 30 and not bad, f"T7 unprefixed outcomes {bad!r}")

    # ISOLATION ------------------------------------------------------------
    after = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
             sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)
    print(f"isolation state.json sha256 before={before[0]} after={after[0]}")
    print(f"isolation journal.db sha256 before={before[1]} after={after[1]}")
    print(f"isolation scans/ entries before={len(before[2] or [])} after={len(after[2] or [])}")
    t.ok(before == after, "ISOLATION: real state.json / journal.db / scans changed")

    if t.failures:
        for f in t.failures:
            print("  " + f)
        print(f"FAIL: {len(t.failures)} failed of {t.n} assertions")
        return 1
    print(f"PASS: 11 scenarios + 4 wl_rejected scenarios + 12 live scenarios "
          f"+ 1 isolation guard, {t.n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
