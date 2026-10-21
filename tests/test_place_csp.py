"""Offline net for forgetrader.submit.place_csp (Brief 13b.1).

Fake trading client + tmp state.json / journal.db only. Never imports
credentials.py, never builds a real TradingClient, never touches the real
~/forgetrader state or journal (sha256 isolation guard at the end).
"""
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alpaca.trading.enums import OrderStatus

import forgetrader_state as fs
from forgetrader import submit

SYM = "NOK261218P00005000"
AS_OF = "2026-10-21T09:35:00-04:00"
REAL_FILES = [Path(ROOT) / "state.json", Path(ROOT) / "journal.db"]

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


class Env:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="place_csp_"))
        self.state_path = self.dir / "state.json"
        self.conn = fs.journal_connect(self.dir / "journal.db")
        self.state = fs.fresh_flat_state()
        fs.write_state(self.state, self.state_path)
        self.state = fs.load_state(self.state_path)

    def proposal(self, **kw):
        p = {"contract_symbol": SYM, "underlying_symbol": "NOK", "bid": 0.15,
             "final_pass": True}
        p.update(kw)
        return p

    def disk(self):
        return json.loads(self.state_path.read_text())

    def orders(self):
        return self.conn.execute(
            "SELECT status, client_order_id, broker_order_id FROM orders "
            "ORDER BY rowid").fetchall()

    def events(self):
        return self.conn.execute(
            "SELECT event_type, detail FROM events").fetchall()

    def run(self, client, proposal=None, as_of=AS_OF):
        return submit.place_csp(client, self.state, proposal or self.proposal(),
                                self.conn, "run1", as_of, self.state_path)

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


def s1():
    e = Env()
    c = FakeClient()
    out, r = e.run(c)
    check(r["outcome"] == "submitted", f"S1 outcome {r}")
    check(len(c.calls) == 1, "S1 submit count")
    coid = c.calls[0].client_order_id
    d = e.disk()["wheel"]
    check(d["stage"] == fs.CSP_PENDING, "S1 disk stage")
    check(d["ticker"] == "NOK", "S1 ticker")
    check(d["contract_symbol"] == SYM, "S1 contract_symbol")
    check(d["client_order_id"] == coid, "S1 disk coid == request coid")
    check(d["opened_at"] == AS_OF, "S1 opened_at")
    check(d["strike"] is None and d["expiry"] is None, "S1 strike/expiry None")
    check(d["intent"] is None and d["cost_basis"] is None, "S1 intent/cost_basis None")
    check(out["wheel"]["stage"] == fs.CSP_PENDING, "S1 returned stage")
    check(r["client_order_id"] == coid and r["broker_reached"] is True
          and r["status"] == "accepted", "S1 result fields")
    rows = e.orders()
    check(len(rows) == 2, f"S1 two orders rows ({len(rows)})")
    if len(rows) == 2:
        check(rows[0]["status"] is None and rows[0]["broker_order_id"] is None
              and rows[0]["client_order_id"] == coid, "S1 row1 intent")
        check(rows[1]["status"] == "accepted" and rows[1]["client_order_id"] == coid
              and rows[1]["broker_order_id"] == str(c.order.id), "S1 row2 result")
        check(r["broker_order_id"] == str(c.order.id), "S1 result broker id")
    e.close()


def s2():
    e = Env()
    seen = {}

    def on_call(req):
        seen["disk"] = e.disk()["wheel"]
        seen["rows"] = e.orders()

    c = FakeClient(on_call=on_call)
    e.run(c)
    check(len(c.calls) == 1, "S2 submit count")
    check(seen["disk"]["stage"] == fs.CSP_PENDING, "S2 disk stage at call time")
    check(seen["disk"]["client_order_id"] == c.calls[0].client_order_id,
          "S2 disk coid at call time")
    check(len(seen["rows"]) == 1 and seen["rows"][0]["status"] is None
          and seen["rows"][0]["client_order_id"] == c.calls[0].client_order_id,
          "S2 intent row present at call time")
    e.close()


def s3():
    e = Env()
    c = FakeClient()

    def boom(*a, **k):
        raise OSError("disk full")

    with Patch(fs, "write_state", boom):
        _, r = e.run(c)
    check(len(c.calls) == 0, "S3 submit count 0")
    check(r["outcome"] == "halt" and r["broker_reached"] is False, f"S3 result {r}")
    check(any(x["event_type"] == "submit_exception" for x in e.events()),
          "S3 events row")
    e.close()


def s4():
    e = Env()
    c = FakeClient()
    real = fs.record_order

    def boom(*a, **k):
        raise sqlite3.OperationalError("locked")

    with Patch(fs, "record_order", boom):
        _, r = e.run(c)
    check(len(c.calls) == 0, "S4 submit count 0")
    d = e.disk()["wheel"]
    check(d["stage"] == fs.CSP_PENDING and d["client_order_id"], "S4 disk pending+coid")
    check(r["outcome"] == "halt" and r["broker_reached"] is False, f"S4 result {r}")
    check(r["client_order_id"] == d["client_order_id"], "S4 result coid")
    e.close()


def s5():
    for exc in (RuntimeError("boom"), TimeoutError("slow")):
        e = Env()
        c = FakeClient(raises=exc)
        out, r = e.run(c)
        name = type(exc).__name__
        check(len(c.calls) == 1, f"S5 {name} submit count 1")
        d = e.disk()["wheel"]
        check(d["stage"] == fs.CSP_PENDING
              and d["client_order_id"] == c.calls[0].client_order_id,
              f"S5 {name} disk pending same coid")
        check(any(x["event_type"] == "submit_exception" and name in x["detail"]
                  for x in e.events()), f"S5 {name} events row")
        check(r["outcome"] == "halt" and r["broker_reached"] is True,
              f"S5 {name} result {r}")
        check(out["wheel"]["stage"] == fs.CSP_PENDING, f"S5 {name} returns new")
        e.close()


def s6():
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
    check(len(c.calls) == 1, "S6 submit count 1")
    check(r["outcome"] == "halt" and r["broker_reached"] is True, f"S6 result {r}")
    check(out["wheel"]["stage"] == fs.CSP_PENDING, "S6 returns new")
    check(len(e.orders()) == 1, "S6 only intent row")
    e.close()


def s7():
    cases = [
        ("stage_pending", dict(stage=True), "stage_not_flat"),
        ("final_pass_false", dict(p=dict(final_pass=False)), "final_pass_not_true"),
        ("bid_empty_str", dict(p=dict(bid="")), "bid_not_float"),
        ("bid_none", dict(p=dict(bid=None)), "bid_not_float"),
        ("bid_true", dict(p=dict(bid=True)), "bid_not_float"),
        ("bid_int", dict(p=dict(bid=1)), "bid_not_float"),
        ("bid_str", dict(p=dict(bid="0.15")), "bid_not_float"),
        ("bid_float_noise", dict(p=dict(bid=0.15000000000000002)), "bid_not_2dp"),
        ("bid_3dp", dict(p=dict(bid=0.155)), "bid_not_2dp"),
        ("bid_zero", dict(p=dict(bid=0.0)), "bid_not_2dp"),
        ("right_call", dict(p=dict(contract_symbol="NOK261218C00005000")), "right_not_put"),
        ("root_mismatch", dict(p=dict(underlying_symbol="F")), "underlying_mismatch"),
        ("as_of_empty", dict(as_of=""), "as_of_ts_missing"),
    ]
    for name, spec, want in cases:
        e = Env()
        if spec.get("stage"):
            pend, _ = fs.apply_trigger(e.state, "submit_csp")
            fs.write_state(pend, e.state_path)
            e.state = fs.load_state(e.state_path)
        before_sha = sha(e.state_path)
        before_rows = len(e.orders())
        before_copy = copy.deepcopy(e.state)
        c = FakeClient()
        out, r = e.run(c, e.proposal(**spec.get("p", {})), spec.get("as_of", AS_OF))
        check(r["outcome"] == "refused", f"S7 {name} outcome {r}")
        check(r["detail"] == want, f"S7 {name} detail {r['detail']!r} != {want!r}")
        check(len(c.calls) == 0, f"S7 {name} submit count 0")
        check(sha(e.state_path) == before_sha, f"S7 {name} state.json changed")
        check(len(e.orders()) == before_rows, f"S7 {name} orders rows changed")
        check(out is e.state and out == before_copy, f"S7 {name} input state not returned intact")
        check(not e.events(), f"S7 {name} wrote an event")
        e.close()


def s8():
    e = Env()
    log = []
    real_fsync, real_replace = os.fsync, os.replace

    def f_fsync(fd):
        log.append("dir_fsync" if os.path.isdir(f"/proc/self/fd/{fd}") else "file_fsync")
        return real_fsync(fd)

    def f_replace(a, b):
        log.append("replace")
        return real_replace(a, b)

    with Patch(os, "fsync", f_fsync), Patch(os, "replace", f_replace):
        fs.write_state(fs.fresh_flat_state(), e.state_path)
    check(log == ["file_fsync", "replace", "dir_fsync"], f"S8 order {log}")
    e.close()


COLS_NEW = None


def cols(conn):
    return [r["name"] for r in conn.execute("PRAGMA table_info(orders)")]


def s9():
    d = Path(tempfile.mkdtemp(prefix="place_csp_mig_"))
    try:
        fresh = fs.journal_connect(d / "fresh.db")
        a = cols(fresh)
        check("client_order_id" in a and "broker_order_id" in a, f"S9a columns {a}")
        fresh.close()
        raw = sqlite3.connect(str(d / "legacy.db"))
        raw.executescript("""
CREATE TABLE orders (
    run_id      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         INTEGER,
    limit_price REAL,
    status      TEXT
);
INSERT INTO orders VALUES ('r0','t0','OLD','sell',1,0.1,'filled');
""")
        raw.commit()
        raw.close()
        leg = fs.journal_connect(d / "legacy.db")
        check(cols(leg) == a, f"S9b legacy cols {cols(leg)} vs {a}")
        row = leg.execute("SELECT * FROM orders").fetchall()
        check(len(row) == 1 and row[0]["symbol"] == "OLD" and row[0]["status"] == "filled",
              "S9b old row preserved")
        check(row[0]["client_order_id"] is None and row[0]["broker_order_id"] is None,
              "S9b new columns NULL")
        leg.close()
        try:
            again = fs.journal_connect(d / "legacy.db")
            check(cols(again) == a, "S9c same columns after second connect")
            again.close()
        except Exception as ex:
            check(False, f"S9c second connect raised {ex!r}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def s10():
    e = Env()
    c = FakeClient()
    e.run(c)
    f = c.calls[0].to_request_fields()
    check(set(f) == {"symbol", "qty", "side", "type", "time_in_force",
                     "limit_price", "client_order_id"}, f"S10 keys {sorted(f)}")
    val = lambda x: getattr(x, "value", x)
    check(f["qty"] == 1.0, "S10 qty")
    check(val(f["side"]) == "sell", "S10 side")
    check(val(f["type"]) == "limit", "S10 type")
    check(val(f["time_in_force"]) == "day", "S10 tif")
    check(f["limit_price"] == 0.15, "S10 limit_price")
    check(f["symbol"] == SYM, "S10 symbol")
    e.close()


def main():
    before = [sha(p) for p in REAL_FILES]
    scenarios = [s1, s2, s3, s4, s5, s6, s7, s8, s9, s10]
    crashed = 0
    for fn in scenarios:
        try:
            fn()
        except Exception:
            crashed += 1
            failures.append(f"{fn.__name__} CRASHED:\n{traceback.format_exc()}")
    after = [sha(p) for p in REAL_FILES]
    check(before == after, "ISOLATION: real state.json/journal.db changed")
    for f in failures:
        print("FAIL: " + f)
    if failures:
        print(f"{len(failures)} failed ({asserts} assertions)")
        return 1
    print(f"PASS: {len(scenarios)} scenarios, {asserts} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
