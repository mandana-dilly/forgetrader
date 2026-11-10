"""Offline net for forgetrader.submit.place_cc (Brief B6, Stage 2 consumer).

Fake trading client + tmp state.json / journal.db only. Never builds a real
TradingClient, never reads credentials, never touches the real ~/forgetrader
state or journal (sha256 isolation guard at the end).

  P1  submitted: CC_PENDING on disk with ticker + cost_basis kept, fresh
      coid, contract_symbol; intent + result orders rows; exact request.
  P2  write-ahead: CC_PENDING + coid on disk and the intent row exist when
      submit_order is called.
  P3-P6 failures after build: HALT result, CC_PENDING persisted, no retry.
  P7  one fixture per refusal token, asserting WHICH check refused, zero
      side effects, input state returned intact.
  P8  strike exactly at the Decimal floor passes (25.00 * 1.10 = 27.50).
  P9  seam: a REAL cc_scan.scan_calls candidate row (fake clients, the B5
      fixtures) passes _validate_cc unmodified and is submitted.
  P10 hardening: _put_strike_basis refuses a non-digit strike field
      (NOK261218P0001e+03 -> HALT "cannot parse strike"). REGRESSION GUARD:
      _contract_expiry (forgetrader_state.py) also requires 8 strike digits,
      so no single-line mutation can make this HALT disappear.
  P11 HOLDING -> CC_PENDING (submit_cc) keeps ticker and cost_basis.

Last line PASS: ... or FAIL: N failed of M assertions.
"""
import copy
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import traceback
import uuid
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from alpaca.trading.enums import OrderStatus  # noqa: E402

import forgetrader_state as fs  # noqa: E402
import screener  # noqa: E402
import test_cc_gates as tcg  # noqa: E402
from forgetrader import submit  # noqa: E402

SYM = "NOK261218C00027500"   # strike 27.50 = floor at basis 25.00, pct 10
ABOVE = "NOK261218C00028000"
AS_OF = "2026-11-04T10:30:00-05:00"
BASIS = 25.0
PCT = 10
REAL_FILES = [REPO / "state.json", REPO / "journal.db"]

asserts = 0
failures = []


def check(cond, msg):
    global asserts
    asserts += 1
    if not cond:
        failures.append(msg)
    return bool(cond)


def sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


class FakeOrder:
    def __init__(self):
        self.id = uuid.uuid4()
        self.status = OrderStatus.ACCEPTED


class FakeClient:
    def __init__(self, raises=None, on_call=None):
        self.calls = []
        self.raises = raises
        self.on_call = on_call
        self.order = FakeOrder()

    def submit_order(self, req):
        self.calls.append(req)
        if self.on_call:
            self.on_call(req)
        if self.raises:
            raise self.raises
        return self.order


def holding_state(ticker="NOK", basis=BASIS):
    s = fs.fresh_flat_state()
    s["wheel"].update({"stage": fs.HOLDING, "ticker": ticker,
                       "cost_basis": basis})
    return s


class Env:
    def __init__(self, state=None):
        self.dir = Path(tempfile.mkdtemp(prefix="place_cc_"))
        self.state_path = self.dir / "state.json"
        self.conn = fs.journal_connect(self.dir / "journal.db")
        fs.write_state(state or holding_state(), self.state_path)
        self.state = fs.load_state(self.state_path)

    def proposal(self, **kw):
        p = {"contract_symbol": ABOVE, "underlying_symbol": "NOK",
             "bid": 0.50, "cost_basis": BASIS, "all_pass": True}
        p.update(kw)
        return p

    def disk(self):
        return json.loads(self.state_path.read_text())

    def orders(self):
        return self.conn.execute(
            "SELECT symbol, side, qty, limit_price, status, client_order_id, "
            "broker_order_id FROM orders ORDER BY rowid").fetchall()

    def events(self):
        return self.conn.execute(
            "SELECT event_type, detail FROM events").fetchall()

    def run(self, client, proposal=None, as_of=AS_OF, pct=PCT):
        return submit.place_cc(client, self.state,
                               proposal if proposal is not None else self.proposal(),
                               self.conn, "run1", as_of, self.state_path,
                               otm_above_basis_pct=pct)

    def close(self):
        self.conn.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class Patch:
    def __init__(self, obj, name, repl):
        self.obj, self.name, self.repl = obj, name, repl

    def __enter__(self):
        self.orig = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.repl)

    def __exit__(self, *a):
        setattr(self.obj, self.name, self.orig)


def p1():
    e = Env()
    c = FakeClient()
    out, r = e.run(c)
    check(r["outcome"] == "submitted", f"P1 outcome {r}")
    check(len(c.calls) == 1, f"P1 submit count {len(c.calls)}")
    coid = c.calls[0].client_order_id if c.calls else None
    d = e.disk()["wheel"]
    check(d["stage"] == fs.CC_PENDING, f"P1 disk stage {d['stage']!r}")
    check(d["ticker"] == "NOK", f"P1 ticker {d['ticker']!r}")
    check(d["cost_basis"] == BASIS, f"P1 cost_basis {d['cost_basis']!r}")
    check(d["contract_symbol"] == ABOVE, f"P1 contract_symbol {d['contract_symbol']!r}")
    check(d["client_order_id"] == coid and coid, "P1 disk coid == request coid")
    check(d["opened_at"] == AS_OF, f"P1 opened_at {d['opened_at']!r}")
    check(d["strike"] is None and d["expiry"] is None, "P1 strike/expiry None")
    check(out["wheel"] == d, "P1 returned wheel != disk wheel")
    check(r["client_order_id"] == coid and r["broker_reached"] is True
          and r["status"] == "accepted"
          and r["broker_order_id"] == str(c.order.id), f"P1 result fields {r}")
    check(set(r) == {"outcome", "detail", "client_order_id", "broker_order_id",
                     "status", "broker_reached"}, f"P1 result keys {sorted(r)}")
    rows = e.orders()
    check(len(rows) == 2, f"P1 two orders rows ({len(rows)})")
    if len(rows) == 2:
        check(rows[0]["status"] is None and rows[0]["broker_order_id"] is None
              and rows[0]["client_order_id"] == coid, "P1 row1 intent")
        check(rows[1]["status"] == "accepted" and rows[1]["client_order_id"] == coid
              and rows[1]["broker_order_id"] == str(c.order.id), "P1 row2 result")
        check(all(x["symbol"] == ABOVE and x["side"] == "sell" and x["qty"] == 1
                  and x["limit_price"] == 0.50 for x in rows),
              f"P1 rows symbol/side/qty/limit {[tuple(x) for x in rows]}")
    # exact request: no position_intent, qty 1, sell, limit, day, limit = bid
    f = c.calls[0].to_request_fields() if c.calls else {}
    check(set(f) == {"symbol", "qty", "side", "type", "time_in_force",
                     "limit_price", "client_order_id"}, f"P1 request keys {sorted(f)}")
    val = lambda x: getattr(x, "value", x)
    check(f.get("qty") == 1.0, f"P1 qty {f.get('qty')!r}")
    check(val(f.get("side")) == "sell", "P1 side")
    check(val(f.get("type")) == "limit", "P1 type")
    check(val(f.get("time_in_force")) == "day", "P1 tif")
    check(f.get("limit_price") == 0.50, f"P1 limit_price {f.get('limit_price')!r} != bid")
    check(f.get("symbol") == ABOVE, "P1 symbol")
    e.close()


def p2():
    e = Env()
    seen = {}

    def on_call(req):
        seen["disk"] = e.disk()["wheel"]
        seen["rows"] = e.orders()

    c = FakeClient(on_call=on_call)
    e.run(c)
    check(len(c.calls) == 1, "P2 submit count")
    check(seen.get("disk", {}).get("stage") == fs.CC_PENDING,
          f"P2 disk stage at call time {seen.get('disk')!r}")
    check(seen.get("disk", {}).get("client_order_id") == c.calls[0].client_order_id,
          "P2 disk coid at call time")
    check(seen.get("disk", {}).get("cost_basis") == BASIS,
          "P2 disk cost_basis at call time")
    rows = seen.get("rows", [])
    check(len(rows) == 1 and rows[0]["status"] is None
          and rows[0]["client_order_id"] == c.calls[0].client_order_id,
          "P2 intent row present at call time")
    e.close()


def p3():
    e = Env()
    c = FakeClient()

    def boom(*a, **k):
        raise OSError("disk full")

    with Patch(fs, "write_state", boom):
        out, r = e.run(c)
    check(len(c.calls) == 0, "P3 submit count 0")
    check(r["outcome"] == "halt" and r["broker_reached"] is False, f"P3 result {r}")
    check(out["wheel"]["stage"] == fs.CC_PENDING, "P3 returns the CC_PENDING state")
    check(any(x["event_type"] == "submit_exception" for x in e.events()), "P3 events row")
    e.close()


def p4():
    e = Env()
    c = FakeClient()

    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")

    with Patch(fs, "record_order", boom):
        _, r = e.run(c)
    check(len(c.calls) == 0, "P4 submit count 0")
    d = e.disk()["wheel"]
    check(d["stage"] == fs.CC_PENDING and d["client_order_id"]
          and d["cost_basis"] == BASIS, f"P4 disk {d!r}")
    check(r["outcome"] == "halt" and r["broker_reached"] is False, f"P4 result {r}")
    check(r["client_order_id"] == d["client_order_id"], "P4 result coid")
    e.close()


def p5():
    for exc in (RuntimeError("boom"), TimeoutError("slow")):
        e = Env()
        c = FakeClient(raises=exc)
        out, r = e.run(c)
        name = type(exc).__name__
        check(len(c.calls) == 1, f"P5 {name} submit count {len(c.calls)} != 1")
        d = e.disk()["wheel"]
        check(d["stage"] == fs.CC_PENDING and c.calls
              and d["client_order_id"] == c.calls[0].client_order_id
              and d["cost_basis"] == BASIS and d["ticker"] == "NOK",
              f"P5 {name} disk {d!r}")
        check(any(x["event_type"] == "submit_exception" and name in x["detail"]
                  for x in e.events()), f"P5 {name} events row")
        check(r["outcome"] == "halt" and r["broker_reached"] is True,
              f"P5 {name} result {r}")
        check(out["wheel"] == d, f"P5 {name} returned state != disk")
        e.close()


def p6():
    e = Env()
    c = FakeClient()
    real = fs.record_order
    n = {"i": 0}

    def second_fails(*a, **k):
        n["i"] += 1
        if n["i"] == 2:
            raise sqlite3.OperationalError("locked")
        return real(*a, **k)

    with Patch(fs, "record_order", second_fails):
        out, r = e.run(c)
    check(len(c.calls) == 1, "P6 submit count 1 (no retry)")
    check(r["outcome"] == "halt" and r["broker_reached"] is True, f"P6 result {r}")
    check(out["wheel"]["stage"] == fs.CC_PENDING, "P6 returns CC_PENDING")
    check(len(e.orders()) == 1, "P6 only the intent row")
    e.close()


def _state_at(stage):
    s = holding_state()
    s["wheel"]["stage"] = stage
    return s


REFUSALS = [
    # (label, state or None, proposal patch, kwargs, token)
    ("stage FLAT", fs.fresh_flat_state(), {}, {}, "stage_not_holding"),
    ("stage CC_PENDING", _state_at(fs.CC_PENDING), {}, {}, "stage_not_holding"),
    ("stage CSP_OPEN", _state_at(fs.CSP_OPEN), {}, {}, "stage_not_holding"),
    ("basis None", holding_state(basis=None), {}, {}, "basis_invalid"),
    ("basis zero", holding_state(basis=0.0), {"cost_basis": 0.0}, {}, "basis_invalid"),
    ("basis str", holding_state(basis="25"), {"cost_basis": "25"}, {}, "basis_invalid"),
    ("basis bool", holding_state(basis=True), {"cost_basis": True}, {}, "basis_invalid"),
    ("all_pass False", None, {"all_pass": False}, {}, "all_pass_not_true"),
    ("all_pass str", None, {"all_pass": "True"}, {}, "all_pass_not_true"),
    ("all_pass 1", None, {"all_pass": 1}, {}, "all_pass_not_true"),
    ("all_pass missing", None, {"all_pass": None}, {}, "all_pass_not_true"),
    ("final_pass only", None, {"all_pass": None, "final_pass": True}, {},
     "all_pass_not_true"),
    ("contract short", None, {"contract_symbol": "NOK"}, {}, "contract_symbol_invalid"),
    ("contract empty", None, {"contract_symbol": ""}, {}, "contract_symbol_invalid"),
    ("contract None", None, {"contract_symbol": None}, {}, "contract_symbol_invalid"),
    ("contract strike non-digit", None, {"contract_symbol": "NOK261218C0002800X"}, {},
     "contract_symbol_invalid"),
    ("contract strike e-notation", None, {"contract_symbol": "NOK261218C0001e+03"}, {},
     "contract_symbol_invalid"),
    ("underlying vs root", None, {"underlying_symbol": "F"}, {}, "underlying_mismatch"),
    ("underlying empty", None, {"underlying_symbol": ""}, {}, "underlying_mismatch"),
    ("underlying vs wheel ticker", holding_state(ticker="F"), {}, {},
     "underlying_mismatch"),
    ("right P", None, {"contract_symbol": "NOK261218P00028000"}, {}, "right_not_call"),
    ("basis 24.99", None, {"cost_basis": 24.99}, {}, "basis_mismatch"),
    ("basis str in proposal", None, {"cost_basis": "25.0"}, {}, "basis_mismatch"),
    ("basis empty (gates missing value)", None, {"cost_basis": ""}, {}, "basis_mismatch"),
    ("basis missing", None, {"cost_basis": None}, {}, "basis_mismatch"),
    ("pct None", None, {}, {"pct": None}, "floor_pct_invalid"),
    ("pct nan", None, {}, {"pct": float("nan")}, "floor_pct_invalid"),
    ("pct negative", None, {}, {"pct": -1}, "floor_pct_invalid"),
    ("pct bool", None, {}, {"pct": True}, "floor_pct_invalid"),
    ("strike 27.49 under 27.50", None, {"contract_symbol": "NOK261218C00027490"}, {},
     "strike_below_floor"),
    ("strike 25.00 at basis", None, {"contract_symbol": "NOK261218C00025000"}, {},
     "strike_below_floor"),
    ("strike 27.50 under pct 10.1", None, {"contract_symbol": SYM}, {"pct": 10.1},
     "strike_below_floor"),
    ("bid empty str", None, {"bid": ""}, {}, "bid_not_float"),
    ("bid None", None, {"bid": None}, {}, "bid_not_float"),
    ("bid int", None, {"bid": 1}, {}, "bid_not_float"),
    ("bid str", None, {"bid": "0.50"}, {}, "bid_not_float"),
    ("bid float noise", None, {"bid": 0.15000000000000002}, {}, "bid_not_2dp"),
    ("bid 3dp", None, {"bid": 0.505}, {}, "bid_not_2dp"),
    ("bid zero", None, {"bid": 0.0}, {}, "bid_not_2dp"),
    ("as_of empty", None, {}, {"as_of": ""}, "as_of_ts_missing"),
    ("as_of None", None, {}, {"as_of": None}, "as_of_ts_missing"),
]


def _refusal(label, e, proposal, kw, want, patch=None):
    before_sha = sha(e.state_path)
    before_copy = copy.deepcopy(e.state)
    c = FakeClient()
    if patch:
        with Patch(*patch):
            out, r = e.run(c, proposal, **kw)
    else:
        out, r = e.run(c, proposal, **kw)
    check(r["outcome"] == "refused", f"P7 {label} outcome {r}")
    check(str(r["detail"]).startswith(want) if want.endswith(": ")
          else r["detail"] == want, f"P7 {label} detail {r['detail']!r} != {want!r}")
    check(len(c.calls) == 0, f"P7 {label} submit count {len(c.calls)}")
    check(sha(e.state_path) == before_sha, f"P7 {label} state.json changed")
    check(len(e.orders()) == 0, f"P7 {label} orders rows written")
    check(out is e.state and out == before_copy, f"P7 {label} input state not returned intact")
    check(not e.events(), f"P7 {label} wrote an event")


def p7():
    seen_tokens = set()
    for label, state, patch, kw, want in REFUSALS:
        e = Env(copy.deepcopy(state) if state is not None else None)
        prop = e.proposal()
        for k, v in patch.items():
            if v is None:
                prop.pop(k, None)
            else:
                prop[k] = v
        _refusal(label, e, prop, kw, want)
        seen_tokens.add(want)
        e.close()
    # build-step refusals (unreachable from a valid proposal; patched seams)
    e = Env()
    _refusal("apply_trigger unexpected", e, e.proposal(), {},
             "apply_trigger_unexpected: ",
             patch=(fs, "apply_trigger", lambda s, t: (s, "HALT:patched")))
    e.close()
    e = Env()

    def bad_req(*a, **k):
        raise ValueError("patched")

    _refusal("build failed", e, e.proposal(), {}, "build_failed: ",
             patch=(submit, "LimitOrderRequest", bad_req))
    e.close()
    expected = {"stage_not_holding", "basis_invalid", "all_pass_not_true",
                "contract_symbol_invalid", "underlying_mismatch",
                "right_not_call", "basis_mismatch", "floor_pct_invalid",
                "strike_below_floor", "bid_not_float", "bid_not_2dp",
                "as_of_ts_missing"}
    check(seen_tokens == expected, f"P7 token coverage {sorted(seen_tokens ^ expected)}")


def p8():
    e = Env()
    c = FakeClient()
    _, r = e.run(c, e.proposal(contract_symbol=SYM))
    check(r["outcome"] == "submitted" and len(c.calls) == 1,
          f"P8 strike exactly at the 27.50 floor not submitted: {r}")
    check(submit._validate_cc(e.state, e.proposal(contract_symbol=SYM), AS_OF, PCT)
          == (submit.Decimal("0.50"), None), "P8 _validate_cc at floor")
    # integer basis in state, float in the proposal: equal values pass
    s = holding_state(basis=25)
    lp, failed = submit._validate_cc(s, e.proposal(), AS_OF, PCT)
    check(failed is None, f"P8 int basis 25 vs proposal 25.0 refused {failed!r}")
    e.close()


def p9():
    screener.init_throttle(tcg.POLICY["runtime"]["rate_limit_per_min"])
    tmp = tempfile.mkdtemp(prefix="place_cc_seam_")
    try:
        res, tc, _sc, _oc = tcg._scan(tmp, [tcg._wl_entry("NOK")])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check(res.rejection is None and len(res.candidates) == 3,
          f"P9 producer candidates {len(res.candidates)} rejection {res.rejection!r} "
          f"tally {dict(res.reject_tally)!r}")
    check(tc.order_calls == 0, "P9 scan submitted")
    if not res.candidates:
        return
    row = res.candidates[0]
    check(row["contract_symbol"] == "NOK260212C00028000" and row["all_pass"] is True
          and type(row["bid"]) is float and row["cost_basis"] == 25.0,
          f"P9 top candidate {row['contract_symbol']!r} all_pass={row['all_pass']!r}")
    st = holding_state(basis=25.0)
    pct = tcg.POLICY["calls"]["otm_above_basis_pct"]
    lp, failed = submit._validate_cc(st, row, AS_OF, pct)
    check(failed is None and lp == submit.Decimal("0.5"),
          f"P9 real candidate refused by _validate_cc: {failed!r} lp={lp!r}")
    e = Env(st)
    c = FakeClient()
    _, r = e.run(c, copy.deepcopy(row), pct=pct)
    check(r["outcome"] == "submitted" and len(c.calls) == 1
          and c.calls[0].symbol == row["contract_symbol"]
          and c.calls[0].limit_price == row["bid"],
          f"P9 real candidate not submitted as-is: {r}")
    d = e.disk()["wheel"]
    check(d["stage"] == fs.CC_PENDING and d["cost_basis"] == 25.0
          and d["ticker"] == "NOK", f"P9 disk {d!r}")
    e.close()
    # every candidate (not only the top) passes the validator
    for cand in res.candidates:
        check(submit._validate_cc(st, cand, AS_OF, pct)[1] is None,
              f"P9 candidate {cand['contract_symbol']} refused")


def p10():
    bad = "NOK261218P0001e+03"
    got = fs._put_strike_basis(bad)
    check(got == (None, f"cannot parse strike from contract_symbol {bad!r}"),
          f"P10 _put_strike_basis {got!r}")
    st = fs.fresh_flat_state()
    st["wheel"].update({"stage": fs.CSP_OPEN, "ticker": "NOK",
                        "contract_symbol": bad, "client_order_id": "c1"})
    ns, a = fs.reconcile(st, {"shares": 100, "put_open": False}, date(2026, 12, 18))
    check(a == f"HALT:CSP_OPEN assigned cannot parse strike from contract_symbol {bad!r}",
          f"P10 reconcile action {a!r}")
    check(ns is st and st["wheel"]["stage"] == fs.CSP_OPEN
          and st["wheel"]["cost_basis"] is None, "P10 input not returned intact")
    # a well-formed put still parses (the new check is not over-broad)
    check(fs._put_strike_basis("NOK261218P00001000") == (1.0, None),
          "P10 well-formed strike refused")


def p11():
    st = holding_state()
    ns, a = fs.apply_trigger(st, "submit_cc")
    check(a == "HOLDING->CC_PENDING (submit_cc)", f"P11 action {a!r}")
    check(ns["wheel"]["stage"] == fs.CC_PENDING and ns["wheel"]["cost_basis"] == BASIS
          and ns["wheel"]["ticker"] == "NOK", f"P11 wheel {ns['wheel']!r}")
    check(st["wheel"]["stage"] == fs.HOLDING, "P11 input mutated")


def main():
    before = [sha(p) for p in REAL_FILES]
    scenarios = [p1, p2, p3, p4, p5, p6, p7, p8, p9, p10, p11]
    for fn in scenarios:
        try:
            fn()
        except Exception:
            check(False, f"{fn.__name__} CRASHED:\n{traceback.format_exc()}")
    after = [sha(p) for p in REAL_FILES]
    print(f"isolation state.json/journal.db sha256 before={before} after={after}")
    check(before == after, "ISOLATION: real state.json/journal.db changed")
    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {asserts} assertions")
        return 1
    print(f"PASS: test_place_cc {len(scenarios)} scenarios, {asserts} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
