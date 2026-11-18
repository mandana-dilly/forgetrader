"""Offline net for forgetrader/journal_report.py (funding criterion 3). Never
the network, never a key, never a live file: every account DIR is a mkdtemp
dir (refused if under /home/forge), journal rows come from forgetrader_state's
own producers (record_order, record_decision, reconcile_and_handle) with
pinned ts, broker orders are real alpaca-py Order models served by a fake
client, and credentials.load_credentials / credentials._ENV_PATH /
alpaca TradingClient are patched for the duration of each call.

  C  clean match exit 0 (filled via a decision, canceled via a decision, a
     non-terminal result row against a terminal broker status).
  J  JOURNAL_ONLY (coid absent at broker; a row with no coid).
  B  BROKER_ONLY (coid absent from journal; empty coid at broker).
  M  MISMATCH:symbol / side / qty / limit / status (decision-resolved,
     result-row terminal, intent_only), one flag per line.
  P  pagination: more orders than one page, a submitted_at tie across the
     page boundary, request fields; a stalled full page is exit 1.
  S  --since: both sides filtered, first request after = since - overlap.
  R  read-only: sha256 + mtime_ns + dir listing unchanged; a missing journal
     is not created, exit 1, no credentials read.
  N  paper not True refused before any client or list call.
  E  errors exit 1: account_dir_mismatch, bad --since, broker exception
     (type name only, no URL, no key).
  X  seam: the decision written by fs.reconcile_and_handle resolves an order.
  A  AST isolation: no call named submit / cancel / replace / close /
     exercise / delete / post / patch, no getattr / eval / exec; the only
     client method called is get_orders.
"""
import ast
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import alpaca.trading.client as alpaca_client  # noqa: E402
from alpaca.trading.models import Order  # noqa: E402

import credentials  # noqa: E402
import forgetrader_state as fs  # noqa: E402
from forgetrader import journal_report as jr  # noqa: E402

SOURCE = Path(ROOT) / "forgetrader" / "journal_report.py"
FORBIDDEN = Path("/home/forge")
KEY, SECRET = "FAKEKEY-not-a-key-111", "FAKESECRET-not-a-secret-222"
PUT = "NOK261211P00007000"
CALL = "NOK270115C00008000"
T0 = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)

failures = []
n = 0
scenarios = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


def scenario(fn):
    global scenarios
    scenarios += 1
    try:
        fn()
    except Exception as e:  # a crash is reported, never a silent pass
        check(False, f"{fn.__name__} crashed: {type(e).__name__}: {e}")
    return fn


def tmpdir(name="acct"):
    d = Path(tempfile.mkdtemp(prefix="test_journal_report.")) / name
    d.mkdir()
    if d.resolve() == FORBIDDEN or FORBIDDEN in d.resolve().parents:
        raise SystemExit(f"REFUSED: fixture path under {FORBIDDEN}: {d}")
    return d


def iso(t):
    return t.isoformat()


def journal(d, orders=(), decisions=()):
    """orders: dicts for one submit each (coid, symbol, side, qty, limit,
    status or None for intent only, ts). decisions: (ts, stage_from,
    stage_to, pending_order_status) recorded as reconcile_and_handle does
    (detail = json.dumps of the broker snapshot)."""
    conn = fs.journal_connect(d / "journal.db")
    for o in orders:
        run_id = fs.new_run_id()
        fs.record_order(conn, run_id, o["symbol"], o.get("side", "sell"),
                        o.get("qty", 1), o["limit"], status=None,
                        ts=iso(o["ts"]), client_order_id=o["coid"],
                        broker_order_id=None)
        if o.get("status") is not None:
            fs.record_order(conn, run_id, o["symbol"], o.get("side", "sell"),
                            o.get("qty", 1), o["limit"], status=o["status"],
                            ts=iso(o["ts"] + timedelta(seconds=1)),
                            client_order_id=o["coid"],
                            broker_order_id=str(uuid.uuid4()))
    for ts, sf, st, pos in decisions:
        snap = {"shares": 0, "put_open": st == fs.CSP_OPEN,
                "call_open": st == fs.CC_OPEN, "pending_order_status": pos}
        fs.record_decision(conn, fs.new_run_id(), sf, st,
                           f"{sf}->{st} (x)", detail=json.dumps(snap), ts=iso(ts))
    conn.close()
    return d


def border(coid, symbol, status, limit="0.16", qty="1", side="sell", ts=T0,
           filled_qty="0", filled_avg_price=None):
    return Order(id=uuid.uuid4(), client_order_id=coid, created_at=ts,
                 updated_at=ts, submitted_at=ts, symbol=symbol, qty=qty,
                 filled_qty=filled_qty, filled_avg_price=filled_avg_price,
                 limit_price=limit, side=side, type="limit",
                 time_in_force="day", status=status, extended_hours=False)


class FakeClient:
    """get_orders honours after (strict), direction asc and limit."""

    def __init__(self, orders, raises=None):
        self.orders = sorted(orders, key=lambda o: o.submitted_at)
        self.requests = []
        self.raises = raises

    def get_orders(self, filter=None):
        self.requests.append(filter)
        if self.raises is not None:
            raise self.raises
        out = [o for o in self.orders
               if filter.after is None or o.submitted_at > filter.after]
        return out[:filter.limit]


class Env:
    """Patch credentials + TradingClient; record what was touched."""

    def __init__(self, d, client, paper=True, env_dir=None):
        self.d, self.client, self.paper = d, client, paper
        self.env_dir = env_dir or d
        self.cred_calls = 0
        self.built = []

    def load(self):
        self.cred_calls += 1
        return KEY, SECRET, self.paper

    def factory(self, *a, **kw):
        self.built.append((a, kw))
        return self.client

    def run(self, argv):
        saved = (credentials.load_credentials, credentials._ENV_PATH,
                 alpaca_client.TradingClient)
        credentials.load_credentials = self.load
        credentials._ENV_PATH = self.env_dir / ".env"
        alpaca_client.TradingClient = self.factory
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = jr.main(argv)
        finally:
            (credentials.load_credentials, credentials._ENV_PATH,
             alpaca_client.TradingClient) = saved
        return rc, out.getvalue(), err.getvalue()


def run(d, broker, argv_extra=(), **kw):
    client = broker if isinstance(broker, FakeClient) else FakeClient(broker)
    env = Env(d, client, **kw)
    rc, out, err = env.run([*argv_extra, str(d)])
    return rc, out, err, env, client


def line_for(out, coid):
    hits = [ln for ln in out.splitlines() if f"coid={coid} " in ln]
    return hits[0] if len(hits) == 1 else ""


def last(out):
    lines = out.strip().splitlines()
    return lines[-1] if lines else ""


def no_secret(text, tag):
    check(KEY not in text and SECRET not in text, f"{tag}: key/secret printed")
    check("http" not in text.lower(), f"{tag}: URL printed")


# --------------------------------------------------------------------------- #

@scenario
def c_clean():
    d = tmpdir("acctA")
    journal(d, orders=[
        dict(coid="c1", symbol=PUT, limit=0.16, status="accepted", ts=T0),
        dict(coid="c2", symbol=CALL, limit=0.45, status="new", ts=T0 + timedelta(days=3)),
        dict(coid="c3", symbol=PUT, limit=0.2, status="accepted", ts=T0 + timedelta(days=6)),
    ], decisions=[
        (T0 + timedelta(hours=2), fs.CSP_PENDING, fs.CSP_OPEN, "filled"),
        (T0 + timedelta(days=4), fs.CC_PENDING, fs.HOLDING, "canceled"),
    ])
    rc, out, err, env, client = run(d, [
        border("c1", PUT, "filled", limit="0.16", ts=T0, filled_qty="1", filled_avg_price="0.16"),
        border("c2", CALL, "canceled", limit="0.45", ts=T0 + timedelta(days=3)),
        border("c3", PUT, "filled", limit="0.20", ts=T0 + timedelta(days=6), filled_qty="1",
               filled_avg_price="0.21"),
    ])
    check(rc == 0, f"C exit {rc} err={err!r}")
    check(last(out) == "REPORT acctA orders=3 matched=3 discrepancies=0", f"C last {last(out)!r}")
    l1 = line_for(out, "c1")
    check(l1 == f"ORDER coid=c1 symbol={PUT} side=sell qty=1 limit=0.16 journal_status=filled "
               "broker_status=filled filled_qty=1 filled_avg_price=0.16 flags=OK", f"C c1 {l1!r}")
    check("journal_status=canceled broker_status=canceled" in line_for(out, "c2"), "C c2 resolved")
    check("journal_status=accepted broker_status=filled" in line_for(out, "c3")
          and "flags=OK" in line_for(out, "c3"), "C c3 non-terminal journal is not a contradiction")
    check(env.cred_calls == 1 and len(env.built) == 1, "C one credentials read, one client")
    a, kw = env.built[0]
    check(a == (KEY, SECRET) and kw == {"paper": True}, "C TradingClient(key, secret, paper=True)")
    no_secret(out + err, "C")


@scenario
def j_journal_only():
    d = tmpdir("acctJ")
    journal(d, orders=[
        dict(coid="c1", symbol=PUT, limit=0.16, status="accepted", ts=T0),
        dict(coid="gone", symbol=PUT, limit=0.17, status="accepted", ts=T0 + timedelta(days=1)),
        dict(coid=None, symbol=PUT, limit=0.18, status=None, ts=T0 + timedelta(days=2)),
    ])
    rc, out, err, *_ = run(d, [border("c1", PUT, "accepted", ts=T0)])
    check(rc == 3, f"J exit {rc}")
    ln = line_for(out, "gone")
    check(ln.endswith("flags=JOURNAL_ONLY") and "broker_status=- " in ln, f"J gone {ln!r}")
    nl = [x for x in out.splitlines() if x.startswith("ORDER coid=- ")]
    check(len(nl) == 1 and nl[0].endswith("flags=JOURNAL_ONLY") and "limit=0.18" in nl[0],
          f"J no-coid row {nl!r}")
    check("flags=OK" in line_for(out, "c1"), "J c1 still OK")
    check(last(out) == "REPORT acctJ orders=3 matched=1 discrepancies=2", f"J last {last(out)!r}")


@scenario
def b_broker_only():
    d = tmpdir("acctB")
    journal(d, orders=[dict(coid="c1", symbol=PUT, limit=0.16, status="accepted", ts=T0)])
    rc, out, err, *_ = run(d, [
        border("c1", PUT, "accepted", ts=T0),
        border("manual", CALL, "filled", limit="1.05", ts=T0 + timedelta(days=1)),
        border("", "NOK", "filled", limit="7.00", qty="100", side="buy", ts=T0 + timedelta(days=2)),
    ])
    check(rc == 3, f"B exit {rc}")
    ln = line_for(out, "manual")
    check(ln.endswith("flags=BROKER_ONLY") and f"symbol={CALL}" in ln
          and "journal_status=- broker_status=filled" in ln, f"B manual {ln!r}")
    nl = [x for x in out.splitlines() if x.startswith("ORDER coid=- ")]
    check(len(nl) == 1 and nl[0].endswith("flags=BROKER_ONLY") and "side=buy qty=100" in nl[0],
          f"B empty coid {nl!r}")
    check(last(out) == "REPORT acctB orders=3 matched=1 discrepancies=2", f"B last {last(out)!r}")


@scenario
def m_mismatch_fields():
    d = tmpdir("acctM")
    t = [T0 + timedelta(days=i) for i in range(8)]
    journal(d, orders=[
        dict(coid="sym", symbol=PUT, limit=0.16, status="accepted", ts=t[0]),
        dict(coid="side", symbol=PUT, limit=0.16, status="accepted", ts=t[1]),
        dict(coid="qty", symbol=PUT, limit=0.16, status="accepted", ts=t[2]),
        dict(coid="lim", symbol=PUT, limit=0.16, status="accepted", ts=t[3]),
        dict(coid="dec", symbol=PUT, limit=0.16, status="accepted", ts=t[4]),
        dict(coid="res", symbol=PUT, limit=0.16, status="rejected", ts=t[5]),
        dict(coid="intent", symbol=PUT, limit=0.16, status=None, ts=t[6]),
        dict(coid="dec_live", symbol=PUT, limit=0.16, status="accepted", ts=t[7]),
    ], decisions=[
        (t[4] + timedelta(hours=1), fs.CSP_PENDING, fs.FLAT, "canceled"),
        (t[7] + timedelta(hours=1), fs.CSP_PENDING, fs.CSP_OPEN, "filled"),
    ])
    rc, out, err, *_ = run(d, [
        border("sym", "NOK261211P00008000", "accepted", ts=t[0]),
        border("side", PUT, "accepted", side="buy", ts=t[1]),
        border("qty", PUT, "accepted", qty="2", ts=t[2]),
        border("lim", PUT, "accepted", limit="0.17", ts=t[3]),
        border("dec", PUT, "filled", ts=t[4]),
        border("res", PUT, "filled", ts=t[5]),
        border("intent", PUT, "accepted", ts=t[6]),
        border("dec_live", PUT, "accepted", ts=t[7]),
    ])
    check(rc == 3, f"M exit {rc}")
    want = {"sym": "MISMATCH:symbol broker_symbol=NOK261211P00008000",
            "side": "MISMATCH:side broker_side=buy",
            "qty": "MISMATCH:qty broker_qty=2",
            "lim": "MISMATCH:limit broker_limit=0.17",
            "dec": "MISMATCH:status", "res": "MISMATCH:status",
            "intent": "MISMATCH:status", "dec_live": "MISMATCH:status"}
    for coid, tail in want.items():
        ln = line_for(out, coid)
        check(ln.endswith(f"flags={tail}"), f"M {coid}: {ln!r}")
    check("journal_status=canceled broker_status=filled" in line_for(out, "dec"), "M dec shown")
    check("journal_status=intent_only" in line_for(out, "intent"), "M intent_only shown")
    check(last(out) == "REPORT acctM orders=8 matched=8 discrepancies=8", f"M last {last(out)!r}")
    # exact decimals: 0.16 vs "0.160" is equal, 0.16 vs "0.1600001" is not
    check(not jr._field_differs("limit", 0.16, "0.160"), "M 0.16 == 0.160")
    check(jr._field_differs("limit", 0.16, "0.1600001"), "M 0.16 != 0.1600001")
    check(jr._field_differs("qty", 1, None), "M qty None differs")


@scenario
def p_pagination():
    check(jr.PAGE_LIMIT == 500, f"P documented page limit {jr.PAGE_LIMIT}")
    saved = jr.PAGE_LIMIT
    jr.PAGE_LIMIT = 3
    try:
        d = tmpdir("acctP")
        ts = [T0 + timedelta(minutes=i) for i in range(8)]
        ts[3] = ts[2]  # a submitted_at tie across the first page boundary
        journal(d, orders=[dict(coid=f"p{i}", symbol=PUT, limit=0.16, status="accepted",
                                ts=ts[i]) for i in range(8)])
        rc, out, err, env, client = run(d, [border(f"p{i}", PUT, "accepted", ts=ts[i])
                                            for i in range(8)])
        check(rc == 0, f"P exit {rc} err={err!r}")
        check(last(out) == "REPORT acctP orders=8 matched=8 discrepancies=0", f"P last {last(out)!r}")
        check(len(client.requests) >= 3, f"P pages {len(client.requests)}")
        check(all(r.limit == 3 and r.status.value == "all" and r.direction.value == "asc"
                  for r in client.requests), "P request fields")
        check(client.requests[0].after is None, "P first page from the start")

        # stalled: a full page that brings nothing new is an error, not a loop
        d2 = tmpdir("acctP2")
        journal(d2)
        same = [border(f"s{i}", PUT, "accepted", ts=T0) for i in range(3)]
        rc, out, err, env, client = run(d2, same)
        check(rc == 1 and "pagination stalled" in err, f"P stall rc={rc} err={err!r}")
        check("REPORT" not in out, "P stall prints no REPORT")
    finally:
        jr.PAGE_LIMIT = saved


@scenario
def s_since():
    d = tmpdir("acctS")
    since = datetime(2026, 11, 10, tzinfo=timezone.utc)
    journal(d, orders=[
        dict(coid="old", symbol=PUT, limit=0.16, status="accepted", ts=since - timedelta(days=3)),
        dict(coid="new", symbol=PUT, limit=0.16, status="accepted", ts=since + timedelta(hours=1)),
    ])
    rc, out, err, env, client = run(d, [
        border("old", PUT, "accepted", ts=since - timedelta(days=3)),
        border("edge", PUT, "accepted", ts=since - timedelta(milliseconds=500)),
        border("new", PUT, "accepted", ts=since + timedelta(hours=1)),
    ], argv_extra=("--since", "2026-11-10"))
    check(rc == 0, f"S exit {rc} err={err!r}")
    check(last(out) == "REPORT acctS orders=1 matched=1 discrepancies=0", f"S last {last(out)!r}")
    check(line_for(out, "old") == "" and line_for(out, "edge") == "", "S old orders filtered")
    check(client.requests[0].after == since - jr.PAGE_OVERLAP, "S first after = since - overlap")
    # without --since the old order matches too and edge is BROKER_ONLY
    rc, out, *_ = run(d, [border("old", PUT, "accepted", ts=since - timedelta(days=3)),
                          border("new", PUT, "accepted", ts=since + timedelta(hours=1))])
    check(rc == 0 and last(out) == "REPORT acctS orders=2 matched=2 discrepancies=0",
          f"S no since {last(out)!r}")


def snapshot(d):
    p = d / "journal.db"
    return (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns,
            sorted(os.listdir(d)))


@scenario
def r_read_only():
    d = tmpdir("acctR")
    journal(d, orders=[dict(coid="c1", symbol=PUT, limit=0.16, status="accepted", ts=T0)])
    before = snapshot(d)
    run(d, [border("c1", PUT, "accepted", ts=T0)])
    run(d, [border("c1", PUT, "filled", ts=T0), border("x", PUT, "filled", ts=T0)])
    check(snapshot(d) == before, "R journal sha256 / mtime / dir listing changed")

    m = tmpdir("acctMissing")
    rc, out, err, env, client = run(m, [border("c1", PUT, "accepted", ts=T0)])
    check(rc == 1 and "journal missing" in err, f"R missing rc={rc} err={err!r}")
    check(not (m / "journal.db").exists() and os.listdir(m) == [], "R missing journal created")
    check(env.cred_calls == 0 and not client.requests, "R missing: no credentials, no list")


@scenario
def n_not_paper():
    for paper in (False, "true", None):
        d = tmpdir("acctN")
        journal(d, orders=[dict(coid="c1", symbol=PUT, limit=0.16, status="accepted", ts=T0)])
        rc, out, err, env, client = run(d, [border("c1", PUT, "accepted", ts=T0)], paper=paper)
        check(rc == 1 and "refused:not_paper" in err, f"N paper={paper!r} rc={rc} err={err!r}")
        check(env.cred_calls == 1 and env.built == [] and client.requests == [],
              f"N paper={paper!r}: client built or list called")
        check("REPORT" not in out, f"N paper={paper!r} printed a REPORT")


@scenario
def e_errors():
    d = tmpdir("acctE")
    journal(d, orders=[dict(coid="c1", symbol=PUT, limit=0.16, status="accepted", ts=T0)])
    other = tmpdir("other")
    rc, out, err, env, client = run(d, [], env_dir=other)
    check(rc == 1 and "account_dir_mismatch" in err, f"E mismatch rc={rc} err={err!r}")
    check(env.cred_calls == 0 and not client.requests, "E mismatch read credentials")

    rc, out, err, *_ = run(d, [], argv_extra=("--since", "2026-13-01"))
    check(rc == 1, f"E bad since rc={rc}")
    rc, out, err, *_ = run(d, [], argv_extra=("--since", "2026-11-1"))
    check(rc == 1, f"E short since rc={rc}")

    boom = FakeClient([], raises=RuntimeError(f"401 https://paper-api.example/v2/orders {KEY}"))
    rc, out, err, env, client = run(d, boom)
    check(rc == 1 and err.strip() == "ERROR acctE RuntimeError", f"E broker error {err!r}")
    no_secret(out + err, "E")


@scenario
def x_seam_reconcile():
    d = tmpdir("acctX")
    journal(d, orders=[dict(coid="cx", symbol=PUT, limit=0.16, status="accepted", ts=T0)])
    st = fs.fresh_flat_state(equity=5000.0, cash=5000.0)
    st["wheel"].update(stage=fs.CSP_PENDING, ticker="NOK", contract_symbol=PUT,
                       client_order_id="cx")
    conn = fs.journal_connect(d / "journal.db")
    _, action = fs.reconcile_and_handle(
        st, {"shares": 0, "put_open": False, "call_open": False,
             "pending_order_status": "canceled"}, conn=conn, run_id=fs.new_run_id())
    conn.close()
    check(action.startswith(f"{fs.CSP_PENDING}->{fs.FLAT}"), f"X action {action!r}")
    rc, out, *_ = run(d, [border("cx", PUT, "filled", ts=T0)])
    ln = line_for(out, "cx")
    check(rc == 3 and "journal_status=canceled" in ln and ln.endswith("flags=MISMATCH:status"),
          f"X {rc} {ln!r}")
    rc, out, *_ = run(d, [border("cx", PUT, "canceled", ts=T0)])
    check(rc == 0, f"X agree rc={rc}")


@scenario
def a_ast_isolation():
    tree = ast.parse(SOURCE.read_text())
    banned = ("submit", "cancel", "replace", "close", "exercise", "delete", "post", "patch")
    names, client_calls = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            names.append(name)
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                    and f.value.id == "client":
                client_calls.add(f.attr)
    bad = sorted({x for x in names if any(b in x.lower() for b in banned)})
    check(not bad, f"A forbidden calls {bad}")
    check(not ({"getattr", "eval", "exec", "__import__", "setattr"} & set(names)),
          "A dynamic dispatch")
    check(client_calls == {"get_orders"}, f"A client calls {client_calls}")
    check(len(names) > 20, f"A walker saw {len(names)} calls")


def main():
    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_journal_report {scenarios} scenarios, {n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
