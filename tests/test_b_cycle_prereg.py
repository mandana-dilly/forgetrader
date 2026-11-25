"""B full-cycle pre-registration net (brief wheel-prep-1125 T1, milestone 8,
funding criterion 3). Never the network, never a key, never a live file.

Account B holds BAC261127C00049000 at CC_OPEN (expiry Fri 27 Nov 2026); its
1 Dec 10:45 ET --live timer run resolves it. This suite drives one whole BAC
cycle through the REAL engine (run.run(live=True), read_broker_financials,
build_snapshot, reconcile_and_handle, place_csp, place_cc, write_state, the
journal) against the file-backed FakeBroker of tests/test_forced_paths.py,
with FORGE_INSTANCE=b so policy-b.toml (calls.otm_above_basis_pct 2) is the
policy place_cc rechecks. Then it runs the REAL forgetrader.journal_report
main() over the journal the engine wrote, with broker orders as alpaca-py
Order models and activities as raw dicts, both shaped exactly as
tests/test_journal_report.py shapes them (border / activity / opasn / opexp /
optrd / FakeClient / Env, copied here because importing that suite runs it).

The put leg is a FIXTURE: BAC261120P00048000 (basis 48.00), OPASN + OPTRD
dated 2026-11-23. The repo holds no real 23 Nov OPASN fixture; B's real put
symbol and basis are read from B's own journal on the day (see
docs/prereg/B_CYCLE_2026-12-01.md). The call leg is B's real contract.

  A  CALLED_AWAY: CSP submit, fill, assigned, CC submit, fill, called away
     (shares 0) at the 1 Dec 15:45Z clock; the 13:15 ET run after it.
     Report: discrepancies=0, exact ACTIVITY OPASN lines.
  B  EXPIRED: same to CC fill, then call gone with 100 shares on 1 Dec
     (cc_expired_worthless, expiry guard passes); the 13:15 ET run after.
     Report: discrepancies=0, exact ACTIVITY OPEXP line.
  C  negative controls on the CALLED_AWAY journal, each discrepancies >= 1
     with its own flag: C1 activity missing (TRANSITION_ONLY), C3 extra broker
     order (BROKER_ONLY), C4 journal transition with no broker event
     (TRANSITION_ONLY, contract "-"). C2 (share_qty wrong sign) is NOT here:
     journal_report does not check the OPTRD sign, so it reports
     discrepancies=0 - a finding (docs/findings/FINDINGS_2026-11-25.md).
  S  --since trap: --since after the OPASN date drops the activity and the
     1 Dec transition reports TRANSITION_ONLY.
"""
import contextlib
import datetime
import hashlib
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
import uuid
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import test_forced_paths as fp  # noqa: E402  (imports test_run_stages: FORGE_INSTANCE=a)

os.environ["FORGE_INSTANCE"] = "b"  # account B: policy-b.toml (calls 2% above basis)

import alpaca.trading.client as alpaca_client  # noqa: E402
from alpaca.trading.models import Order  # noqa: E402

import credentials  # noqa: E402
import forgetrader_state as fs  # noqa: E402
import screener  # noqa: E402
import wheel_dryrun  # noqa: E402
from forgetrader import cc_scan as _cc_scan_mod  # noqa: E402
from forgetrader import journal_report as jr  # noqa: E402
from forgetrader.run import run  # noqa: E402

_REAL_STATE = Path(fs.STATE_PATH)
_REAL_JOURNAL = Path(fs.JOURNAL_PATH)
_REAL_SCANS = Path(fs.SCANS_DIR)

UTC = datetime.timezone.utc
LABEL = "forgetrader-b"  # REPORT label = basename of B's account dir
ACCOUNT = "21c36b57-2304-4e5b-8364-8effbcba853e"
KEY, SECRET = "FAKEKEY-not-a-key-111", "FAKESECRET-not-a-secret-222"

PUT_B = "BAC261120P00048000"   # FIXTURE put (no real 23 Nov fixture in the repo)
CALL_B = "BAC261127C00049000"  # B's real covered call (brief)
BASIS = 48.0
PUT_ROW = {"underlying_symbol": "BAC", "contract_symbol": PUT_B, "strike": 48.0,
           "expiration_date": "2026-11-20", "bid": 0.45, "open_interest": 1200,
           "annualized_yield_pct": 85.5469, "collateral": 4800.0, "final_pass": True}
CALL_ROW = {"underlying_symbol": "BAC", "contract_symbol": CALL_B, "strike": 49.0,
            "expiration_date": "2026-11-27", "cost_basis": BASIS,
            "strike_floor": 48.96, "bid": 0.3, "ask": 0.35, "open_interest": 900,
            "annualized_yield_pct": 55.8673, "all_pass": True}

# Run clocks (UTC; 10:45 ET = 15:45Z and 13:15 ET = 18:15Z under EST).
R1 = datetime.datetime(2026, 11, 16, 15, 45, tzinfo=UTC)  # CSP submit
R2 = datetime.datetime(2026, 11, 16, 18, 15, tzinfo=UTC)  # CSP fill
R3 = datetime.datetime(2026, 11, 23, 15, 45, tzinfo=UTC)  # put assigned
R4 = datetime.datetime(2026, 11, 23, 18, 15, tzinfo=UTC)  # CC submit
R5 = datetime.datetime(2026, 11, 24, 15, 45, tzinfo=UTC)  # CC fill
R6 = datetime.datetime(2026, 12, 1, 15, 45, tzinfo=UTC)   # 1 Dec 10:45 ET resolve
R7 = datetime.datetime(2026, 12, 1, 18, 15, tzinfo=UTC)   # 1 Dec 13:15 ET
MONEY = {"equity": 5000.0, "cash": 5000.0, "options_buying_power": 5000.0}

FRESH_WHEEL = {"stage": "FLAT", "ticker": None, "cost_basis": None, "intent": None,
               "contract_symbol": None, "strike": None, "expiry": None,
               "opened_at": None, "client_order_id": None}
HOLDING_WHEEL = dict(FRESH_WHEEL, stage="HOLDING", ticker="BAC", cost_basis=BASIS)

t = fp.trs.T()
scenarios = 0


def _sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


BEFORE = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
          sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)


def scenario(fn):
    global scenarios
    scenarios += 1
    try:
        fn()
    except Exception as e:  # a crash is reported, never a silent pass
        t.ok(False, f"{fn.__name__} crashed: {type(e).__name__}: {e}")
    return fn


# --------------------------------------------------------------------------- #
# Engine side: the real run path against the file-backed FakeBroker           #
# --------------------------------------------------------------------------- #

def _empty_scan(*a, **k):
    return fp.trs._scan_result([])


def _empty_cc(*a, **k):
    return types.SimpleNamespace(
        ticker="BAC", cost_basis=BASIS, candidates=[], all_rows=[],
        reject_tally=Counter(), rejection=None, chain_fetch_errors=[],
        wl_rejected=[], wl_warnings=[])


def _put_scan(*a, **k):
    return fp.trs._scan_result([], [dict(PUT_ROW)])


def _call_scan(*a, **k):
    r = _empty_cc()
    r.candidates, r.all_rows = [dict(CALL_ROW)], [dict(CALL_ROW)]
    return r


def broker(d, clock, positions, **orders):
    """Before a run: pin the clock and positions; set order statuses by coid."""
    b = fp.broker_load(d / "broker.json")
    b["clock_ts"] = clock.isoformat()
    b["money"] = dict(MONEY)
    b["positions"] = [list(p) for p in positions]
    for coid, status in orders.items():
        b["orders"][coid]["status"] = status
    fp.broker_save(d / "broker.json", b)


def live_run(d, scan=_empty_scan, cc=_empty_cc):
    """One run.run(live=True) inside fp.engine_env with this cycle's scans.
    Returns this run's journal rows, rc, stdout, the state and sent alerts."""
    before = fp._journal(d)
    sent_before = len(fp._lines(d / "sent.log"))
    out = io.StringIO()
    with fp.engine_env(d):
        wheel_dryrun.scan_watchlist = scan  # engine_env restores the originals
        _cc_scan_mod.scan_calls = cc
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = run(live=True)
            except Exception as e:  # reported by the rc assertion, never a crash
                rc = f"EXC {type(e).__name__}: {e}"
    after = fp._journal(d)
    o = {k: after[k][len(before[k]):] for k in after}
    o["rc"], o["stdout"] = rc, out.getvalue()
    o["state"] = json.loads((d / "state.json").read_text())
    o["sent"] = fp._lines(d / "sent.log")[sent_before:]
    return o


def expect_run(label, o, rc, outcome, stage, decisions, events=("live_complete",)):
    t.ok(o["rc"] == rc, f"{label} exit {o['rc']!r} != {rc}")
    t.ok(o["runs"] == [outcome], f"{label} runs {o['runs']!r}")
    t.ok(o["state"]["last_run"]["outcome"] == outcome,
         f"{label} last_run {o['state']['last_run']!r}")
    t.ok(o["state"]["wheel"]["stage"] == stage,
         f"{label} stage {o['state']['wheel']['stage']!r} != {stage}")
    t.ok(o["decisions"] == list(decisions), f"{label} decisions {o['decisions']!r}")
    t.ok(o["events"] == list(events), f"{label} events {o['events']!r}")


def engine_cycle(branch):
    """Seed FLAT, run R1..R7 live. branch "called_away" or "expired".
    Returns (dir, {run label: observations}, put coid, call coid)."""
    d = Path(tempfile.mkdtemp(prefix="b_cycle_")) / LABEL
    d.mkdir()
    fs.pin_now(R1 - timedelta(days=1))
    try:
        fs.write_state(fs.fresh_flat_state(**MONEY), d / "state.json")
    finally:
        fs.pin_now(None)
    fp.broker_save(d / "broker.json", {
        "clock_ts": R1.isoformat(), "money": dict(MONEY), "positions": [],
        "orders": {}, "submits": [], "submit_mode": "accept"})
    obs = {}
    broker(d, R1, [])
    obs["R1"] = live_run(d, scan=_put_scan)
    sub = fp.broker_load(d / "broker.json")["submits"]
    put_coid = sub[0]["coid"] if sub else None
    broker(d, R2, [[PUT_B, -1]], **{put_coid: "filled"})
    obs["R2"] = live_run(d)
    broker(d, R3, [["BAC", 100]])
    obs["R3"] = live_run(d)
    broker(d, R4, [["BAC", 100]])
    obs["R4"] = live_run(d, cc=_call_scan)
    sub = fp.broker_load(d / "broker.json")["submits"]
    call_coid = sub[1]["coid"] if len(sub) > 1 else None
    broker(d, R5, [["BAC", 100], [CALL_B, -1]], **{call_coid: "filled"})
    obs["R5"] = live_run(d)
    end = [] if branch == "called_away" else [["BAC", 100]]
    broker(d, R6, end)
    obs["R6"] = live_run(d)
    broker(d, R7, end)
    obs["R7"] = live_run(d)
    return d, obs, put_coid, call_coid


def check_common_legs(tag, obs, put_coid, call_coid):
    """R1..R5, identical on both branches."""
    tok1 = f"submit:submitted status=accepted coid={put_coid}"
    expect_run(f"{tag} R1", obs["R1"], 0, f"live:{tok1}", "CSP_PENDING",
               ["noop:FLAT stable", "propose_csp", tok1])
    t.ok(obs["R1"]["orders"] == [(None, put_coid), ("accepted", put_coid)],
         f"{tag} R1 orders {obs['R1']['orders']!r}")
    expect_run(f"{tag} R2", obs["R2"], 0, "live:CSP_PENDING->CSP_OPEN (fill)",
               "CSP_OPEN", ["CSP_PENDING->CSP_OPEN (fill)"])
    expect_run(f"{tag} R3", obs["R3"], 0, "live:CSP_OPEN->HOLDING (assigned)",
               "HOLDING", ["CSP_OPEN->HOLDING (assigned)"])
    t.ok(obs["R3"]["state"]["wheel"] == HOLDING_WHEEL,
         f"{tag} R3 wheel {obs['R3']['state']['wheel']!r}")
    tok4 = f"submit:submitted status=accepted coid={call_coid}"
    expect_run(f"{tag} R4", obs["R4"], 0, f"live:{tok4}", "CC_PENDING",
               ["noop:HOLDING stable", "propose_cc", tok4])
    w = obs["R4"]["state"]["wheel"]
    t.ok(w["contract_symbol"] == CALL_B and w["cost_basis"] == BASIS
         and w["client_order_id"] == call_coid and w["opened_at"] == R4.isoformat(),
         f"{tag} R4 wheel {w!r}")
    expect_run(f"{tag} R5", obs["R5"], 0, "live:CC_PENDING->CC_OPEN (fill)",
               "CC_OPEN", ["CC_PENDING->CC_OPEN (fill)"])
    w = obs["R5"]["state"]["wheel"]
    t.ok(w == dict(FRESH_WHEEL, stage="CC_OPEN", ticker="BAC", cost_basis=BASIS,
                   contract_symbol=CALL_B, opened_at=R4.isoformat()),
         f"{tag} R5 wheel (B's wheel today) {w!r}")
    t.ok(put_coid and call_coid and put_coid != call_coid, f"{tag} two coids")


def r6_decision_detail(d):
    conn = sqlite3.connect(str(d / "journal.db"))
    try:
        rows = conn.execute("SELECT ts, stage_from, stage_to, action, detail FROM decisions "
                            "WHERE ts = ? ORDER BY rowid", (R6.isoformat(),)).fetchall()
    finally:
        conn.close()
    return rows


# --------------------------------------------------------------------------- #
# Report side: shapes copied from tests/test_journal_report.py                 #
# --------------------------------------------------------------------------- #

def border(coid, symbol, status, limit="0.16", qty="1", side="sell", ts=None,
           filled_qty="0", filled_avg_price=None):
    return Order(id=uuid.uuid4(), client_order_id=coid, created_at=ts,
                 updated_at=ts, submitted_at=ts, symbol=symbol, qty=qty,
                 filled_qty=filled_qty, filled_avg_price=filled_avg_price,
                 limit_price=limit, side=side, type="limit",
                 time_in_force="day", status=status, extended_hours=False)


def activity(atype, symbol, day, qty, net="0", description="", price=None):
    a = {"id": f"{day.strftime('%Y%m%d')}000000000::{uuid.uuid4()}",
         "account_id": ACCOUNT, "activity_type": atype, "date": day.isoformat(),
         "net_amount": net, "description": description, "symbol": symbol,
         "qty": qty, "status": "executed"}
    if price is not None:
        a["price"] = price
    return a


def opasn(contract, day):
    return activity("OPASN", contract, day, "1")


def opexp(contract, day):
    return activity("OPEXP", contract, day, "-1", description="Option Expiry")


def optrd(symbol, day, qty, price):
    return activity("OPTRD", symbol, day, qty, net=str(-int(qty) * float(price)),
                    description="Option Trade", price=price)


class FakeClient:
    """As test_journal_report.FakeClient: get_orders honours after / limit,
    get serves activities ascending by (date, id) with after / page_token /
    page_size."""

    def __init__(self, orders, activities=()):
        self.orders = sorted(orders, key=lambda o: o.submitted_at)
        self.activities = sorted(activities, key=lambda a: (a["date"], a["id"]))
        self.requests, self.gets = [], []

    def get(self, path, data=None):
        self.gets.append((path, dict(data or {})))
        out = list(self.activities)
        if "after" in data:
            out = [a for a in out if a["date"] >= data["after"]]
        if "page_token" in data:
            ids = [a["id"] for a in out]
            out = out[ids.index(data["page_token"]) + 1:]
        return out[:data["page_size"]]

    def get_orders(self, filter=None):
        self.requests.append(filter)
        out = [o for o in self.orders
               if filter.after is None or o.submitted_at > filter.after]
        return out[:filter.limit]


def report(d, border_orders, activities, argv_extra=()):
    """jr.main on DIR d with credentials / _ENV_PATH / TradingClient patched
    (test_journal_report.Env). Returns (rc, stdout, stderr)."""
    client = FakeClient(border_orders, activities)
    saved = (credentials.load_credentials, credentials._ENV_PATH,
             alpaca_client.TradingClient)
    credentials.load_credentials = lambda: (KEY, SECRET, True)
    credentials._ENV_PATH = d / ".env"
    alpaca_client.TradingClient = lambda *a, **k: client
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = jr.main([*argv_extra, str(d)])
    finally:
        (credentials.load_credentials, credentials._ENV_PATH,
         alpaca_client.TradingClient) = saved
    return rc, out.getvalue(), err.getvalue()


def broker_orders(d, put_coid, call_coid):
    """The two engine orders as the broker lists them after both filled."""
    return [border(put_coid, PUT_B, "filled", limit="0.45", ts=R1,
                   filled_qty="1", filled_avg_price="0.45"),
            border(call_coid, CALL_B, "filled", limit="0.30", ts=R4,
                   filled_qty="1", filled_avg_price="0.30")]


PUT_ACTS = [opasn(PUT_B, date(2026, 11, 23)), optrd("BAC", date(2026, 11, 23), "100", "48.00")]


def called_away_acts():
    return PUT_ACTS + [opasn(CALL_B, date(2026, 11, 27)),
                       optrd("BAC", date(2026, 11, 27), "-100", "49.00")]


def lines(out, prefix):
    return [ln for ln in out.splitlines() if ln.startswith(prefix)]


def last(out):
    ls = out.strip().splitlines()
    return ls[-1] if ls else ""


def order_line(coid, symbol, limit, fill):
    """limit is the journal REAL as SQLite returns it (0.3, not 0.30);
    fill is the broker's filled_avg_price string, shown raw."""
    return (f"ORDER coid={coid} symbol={symbol} side=sell qty=1 limit={limit} "
            f"journal_status=filled broker_status=filled filled_qty=1 "
            f"filled_avg_price={fill} flags=OK")


ACT_PUT = ("ACTIVITY symbol=BAC261120P00048000 from=CSP_OPEN to=HOLDING trigger=assigned "
           "journal_ts=2026-11-23T15:45:00+00:00 expected=OPASN broker_type=OPASN "
           "broker_date=2026-11-23 broker_qty=1 share_qty=100 share_price=48.00 flags=OK")
ACT_CALLED = ("ACTIVITY symbol=BAC261127C00049000 from=CC_OPEN to=FLAT trigger=called_away "
              "journal_ts=2026-12-01T15:45:00+00:00 expected=OPASN broker_type=OPASN "
              "broker_date=2026-11-27 broker_qty=1 share_qty=-100 share_price=49.00 flags=OK")
ACT_EXPIRED = ("ACTIVITY symbol=BAC261127C00049000 from=CC_OPEN to=HOLDING "
               "trigger=cc_expired_worthless journal_ts=2026-12-01T15:45:00+00:00 "
               "expected=OPEXP broker_type=OPEXP broker_date=2026-11-27 broker_qty=-1 "
               "share_qty=- share_price=- flags=OK")
CLEAN = (f"REPORT {LABEL} orders=2 matched=2 activities=2 activities_matched=2 "
         "discrepancies=0")
SNAP_CALLED = '{"shares": 0, "put_open": false, "call_open": false, "pending_order_status": null}'
SNAP_EXPIRED = '{"shares": 100, "put_open": false, "call_open": false, "pending_order_status": null}'

CYCLES = {}


# --------------------------------------------------------------------------- #
# Scenarios                                                                   #
# --------------------------------------------------------------------------- #

@scenario
def p_policy_b():
    pol = screener.load_policy()
    t.ok(pol["calls"]["otm_above_basis_pct"] == 2, f"P policy-b calls {pol['calls']!r}")
    t.ok(49.0 >= BASIS * 1.02, "P fixture call strike clears the policy-b floor")


@scenario
def a_called_away():
    d, obs, pc, cc = engine_cycle("called_away")
    CYCLES["called_away"] = (d, obs, pc, cc)
    check_common_legs("A", obs, pc, cc)
    o = obs["R6"]
    expect_run("A R6", o, 0, "live:CC_OPEN->FLAT (called_away)", "FLAT",
               ["CC_OPEN->FLAT (called_away)"])
    t.ok(o["state"]["wheel"] == FRESH_WHEEL, f"A R6 wheel {o['state']['wheel']!r}")
    t.ok(o["state"]["last_run"] == {"ts": "2026-12-01T15:45:00+00:00",
                                    "outcome": "live:CC_OPEN->FLAT (called_away)"},
         f"A R6 last_run {o['state']['last_run']!r}")
    t.ok(o["state"]["positions"] == [] and o["state"]["cash"] == 5000.0,
         f"A R6 money/positions {o['state']['positions']!r}")
    t.ok(o["orders"] == [], f"A R6 no order rows {o['orders']!r}")
    t.ok("RUN OK  stage=FLAT  action=CC_OPEN->FLAT (called_away)  state: " in o["stdout"],
         f"A R6 stdout {o['stdout']!r}")
    t.ok(o["sent"] == ["live:CC_OPEN->FLAT (called_away) stage=FLAT ticker=None"],
         f"A R6 alert {o['sent']!r}")
    rows = r6_decision_detail(d)
    t.ok(rows == [("2026-12-01T15:45:00+00:00", "CC_OPEN", "FLAT",
                   "CC_OPEN->FLAT (called_away)", SNAP_CALLED)], f"A R6 decision row {rows!r}")
    # 13:15 ET: FLAT stable scans (nothing passes here), never submits
    o = obs["R7"]
    expect_run("A R7", o, 0, "live:skip:no_passing_proposal", "FLAT",
               ["noop:FLAT stable", "all_rejected", "skip:no_passing_proposal"])
    t.ok(len(fp.broker_load(d / "broker.json")["submits"]) == 2, "A submit_order calls != 2")

    rc, out, err = report(d, broker_orders(d, pc, cc), called_away_acts())
    t.ok(rc == 0, f"A report exit {rc} err={err!r}")
    t.ok(lines(out, "ACTIVITY ") == [ACT_PUT, ACT_CALLED], f"A ACTIVITY lines {lines(out, 'ACTIVITY ')!r}")
    t.ok(sorted(lines(out, "ORDER ")) == sorted([order_line(pc, PUT_B, "0.45", "0.45"),
                                                 order_line(cc, CALL_B, "0.3", "0.30")]),
         f"A ORDER lines {lines(out, 'ORDER ')!r}")
    t.ok(last(out) == CLEAN, f"A last {last(out)!r}")
    print(f"A report (coids {pc} {cc}):")
    for ln in out.splitlines():
        print(f"  {ln}")


@scenario
def b_expired():
    d, obs, pc, cc = engine_cycle("expired")
    CYCLES["expired"] = (d, obs, pc, cc)
    check_common_legs("B", obs, pc, cc)
    o = obs["R6"]
    expect_run("B R6", o, 0, "live:CC_OPEN->HOLDING (cc_expired_worthless)", "HOLDING",
               ["CC_OPEN->HOLDING (cc_expired_worthless)"])
    t.ok(o["state"]["wheel"] == HOLDING_WHEEL, f"B R6 wheel {o['state']['wheel']!r}")
    t.ok(o["state"]["last_run"] == {"ts": "2026-12-01T15:45:00+00:00",
                                    "outcome": "live:CC_OPEN->HOLDING (cc_expired_worthless)"},
         f"B R6 last_run {o['state']['last_run']!r}")
    t.ok([p["symbol"] for p in o["state"]["positions"]] == ["BAC"]
         and o["state"]["positions"][0]["qty"] == 100, f"B R6 positions {o['state']['positions']!r}")
    t.ok("RUN OK  stage=HOLDING  action=CC_OPEN->HOLDING (cc_expired_worthless)  state: "
         in o["stdout"], f"B R6 stdout {o['stdout']!r}")
    t.ok(o["sent"] == ["live:CC_OPEN->HOLDING (cc_expired_worthless) stage=HOLDING ticker=BAC"],
         f"B R6 alert {o['sent']!r}")
    rows = r6_decision_detail(d)
    t.ok(rows == [("2026-12-01T15:45:00+00:00", "CC_OPEN", "HOLDING",
                   "CC_OPEN->HOLDING (cc_expired_worthless)", SNAP_EXPIRED)],
         f"B R6 decision row {rows!r}")
    # 13:15 ET: HOLDING stable runs the CC scan (nothing passes here), no submit
    o = obs["R7"]
    expect_run("B R7", o, 0, "live:skip:no_passing_proposal", "HOLDING",
               ["noop:HOLDING stable", "cc_all_rejected", "skip:no_passing_proposal"])
    t.ok(len(fp.broker_load(d / "broker.json")["submits"]) == 2, "B submit_order calls != 2")

    acts = PUT_ACTS + [opexp(CALL_B, date(2026, 11, 27))]
    rc, out, err = report(d, broker_orders(d, pc, cc), acts)
    t.ok(rc == 0, f"B report exit {rc} err={err!r}")
    t.ok(lines(out, "ACTIVITY ") == [ACT_PUT, ACT_EXPIRED], f"B ACTIVITY lines {lines(out, 'ACTIVITY ')!r}")
    t.ok(last(out) == CLEAN, f"B last {last(out)!r}")
    print(f"B report (coids {pc} {cc}):")
    for ln in out.splitlines():
        print(f"  {ln}")


def _called():
    d, obs, pc, cc = CYCLES["called_away"]
    return d, broker_orders(d, pc, cc)


@scenario
def c1_activity_missing():
    d, bo = _called()
    rc, out, err = report(d, bo, PUT_ACTS)  # no OPASN / OPTRD for the call
    call = [ln for ln in lines(out, "ACTIVITY ") if f"symbol={CALL_B} " in ln]
    t.ok(rc == 3, f"C1 exit {rc}")
    t.ok(len(call) == 1 and "trigger=called_away" in call[0] and "broker_type=- " in call[0]
         and call[0].endswith("flags=TRANSITION_ONLY"), f"C1 call line {call!r}")
    t.ok(last(out) == f"REPORT {LABEL} orders=2 matched=2 activities=2 "
                      "activities_matched=1 discrepancies=1", f"C1 last {last(out)!r}")


@scenario
def c3_extra_broker_order():
    d, bo = _called()
    extra = border("manual-bac-call", "BAC261204C00050000", "filled", limit="0.20",
                   ts=R5 + timedelta(hours=1), filled_qty="1", filled_avg_price="0.20")
    rc, out, err = report(d, bo + [extra], called_away_acts())
    ln = [x for x in lines(out, "ORDER ") if "coid=manual-bac-call " in x]
    t.ok(rc == 3, f"C3 exit {rc}")
    t.ok(len(ln) == 1 and ln[0].endswith("flags=BROKER_ONLY")
         and "journal_status=- broker_status=filled" in ln[0], f"C3 line {ln!r}")
    t.ok(last(out) == f"REPORT {LABEL} orders=3 matched=2 activities=2 "
                      "activities_matched=2 discrepancies=1", f"C3 last {last(out)!r}")


@scenario
def c4_transition_without_event():
    d0, bo = _called()
    d = Path(tempfile.mkdtemp(prefix="b_cycle_c4_")) / LABEL
    shutil.copytree(d0, d)
    conn = fs.journal_connect(d / "journal.db")
    fs.record_decision(conn, fs.new_run_id(), fs.CC_OPEN, fs.FLAT,
                       "CC_OPEN->FLAT (called_away)", detail=SNAP_CALLED,
                       ts=(R7 + timedelta(days=1)).isoformat())
    conn.close()
    rc, out, err = report(d, bo, called_away_acts())
    ln = [x for x in lines(out, "ACTIVITY ") if x.startswith("ACTIVITY symbol=- ")]
    t.ok(rc == 3, f"C4 exit {rc}")
    t.ok(len(ln) == 1 and "from=CC_OPEN to=FLAT trigger=called_away" in ln[0]
         and ln[0].endswith("flags=TRANSITION_ONLY"), f"C4 line {ln!r}")
    t.ok(lines(out, "ACTIVITY ")[:2] == [ACT_PUT, ACT_CALLED], "C4 real lines unchanged")
    t.ok(last(out) == f"REPORT {LABEL} orders=2 matched=2 activities=3 "
                      "activities_matched=2 discrepancies=1", f"C4 last {last(out)!r}")
    shutil.rmtree(d.parent, ignore_errors=True)


@scenario
def s_since_trap():
    d, bo = _called()
    rc, out, err = report(d, bo, called_away_acts(), argv_extra=("--since", "2026-11-27"))
    t.ok(rc == 0 and lines(out, "ACTIVITY ") == [ACT_CALLED]
         and last(out) == f"REPORT {LABEL} orders=0 matched=0 activities=1 "
                          "activities_matched=1 discrepancies=0",
         f"S since 2026-11-27 rc={rc} {out!r}")
    rc, out, err = report(d, bo, called_away_acts(), argv_extra=("--since", "2026-11-28"))
    t.ok(rc == 3 and len(lines(out, "ACTIVITY ")) == 1
         and lines(out, "ACTIVITY ")[0].endswith("flags=TRANSITION_ONLY")
         and last(out).endswith("discrepancies=1"),
         f"S since 2026-11-28 rc={rc} {out!r}")


def main():
    for d, *_ in CYCLES.values():
        shutil.rmtree(d.parent, ignore_errors=True)
    after = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
             sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)
    print(f"isolation state.json sha256 before={BEFORE[0]} after={after[0]}")
    print(f"isolation journal.db sha256 before={BEFORE[1]} after={after[1]}")
    t.ok(BEFORE == after, "ISOLATION: real state.json / journal.db / scans changed")
    if t.failures:
        for f in t.failures:
            print("  " + f)
        print(f"FAIL: {len(t.failures)} failed of {t.n} assertions")
        return 1
    print(f"PASS: test_b_cycle_prereg {scenarios} scenarios, {t.n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
