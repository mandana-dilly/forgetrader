"""Offline net for forgetrader/probe.py (Brief 13b.2).

Fake trading + option clients (test_run_stages pattern). Patched seams:
credentials.load_credentials, probe.TradingClient / OptionHistoricalDataClient,
probe.WATCHLIST_PATH, fs.read_broker_financials, fs.alert_stub. fs.STATE_PATH /
JOURNAL_PATH point at a mkdtemp dir; the real state.json / journal.db are
hashed before vs after.

Refusals R1-R11 each assert: exact check token, exit 3, zero submit calls,
state file bytes unchanged.
  S1 dry run   S2 submitted   S3 submit raises   S4 wheel_dryrun not imported
"""

import contextlib
import datetime
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import credentials
import forgetrader_state as fs
from forgetrader import probe

_REAL_STATE = Path(fs.STATE_PATH)
_REAL_JOURNAL = Path(fs.JOURNAL_PATH)

UTC = datetime.timezone.utc
CLOCK = datetime.datetime(2026, 10, 22, 14, 0, tzinfo=UTC)  # 10:00 ET
PUT = "F261120P00012000"
SEED = {"equity": 1300.0, "cash": 1300.0, "options_buying_power": 1300.0}
FIN = {"equity": 1300.0, "cash": 1300.0, "options_buying_power": 1300.0,
       "positions": [], "open_orders": [], "tracked_order_status": None}
WATCH = {"version": 2, "entries": [{
    "ticker": "F", "added": "2026-08-24", "earnings_date": "2026-12-01",
    "earnings_source": "test", "earnings_stamped": "2026-08-24",
    "own_100_note": "Willing to own 100 F.", "review_by": "2027-01-01"}]}


def _sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


class FakeOrder:
    def __init__(self):
        self.id = "oid-1"
        self.status = types.SimpleNamespace(value="accepted")


class FakeTC:
    constructed = 0

    def __init__(self, *a, **k):
        type(self).constructed += 1
        self.submits = []
        self.raise_on_submit = None
        self.clock = types.SimpleNamespace(timestamp=CLOCK, is_open=True)
        self.cash = 1300.0

    def get_clock(self):
        return self.clock

    def get_account(self):
        return types.SimpleNamespace(cash=str(self.cash))

    def submit_order(self, req):
        self.submits.append(req)
        if self.raise_on_submit:
            raise self.raise_on_submit
        return FakeOrder()


class FakeOC:
    constructed = 0

    def __init__(self, *a, **k):
        type(self).constructed += 1
        self.quote = None

    def get_option_latest_quote(self, req):
        return {} if self.quote is None else {PUT: self.quote}


def _quote(bid=0.95, ask=0.99, age_min=1, naive=False):
    ts = CLOCK - datetime.timedelta(minutes=age_min)
    if naive:
        ts = ts.replace(tzinfo=None)
    return types.SimpleNamespace(bid_price=bid, ask_price=ask, timestamp=ts)


def _drive(argv, paper=True, clock_ts=CLOCK, is_open=True, fin=None,
           contract=PUT, watch=WATCH, cash=1300.0, quote=None, raise_submit=None,
           state=None, no_state=False, quote_absent=False):
    tmp = Path(tempfile.mkdtemp(prefix="probe_"))
    st, jr, wl = tmp / "state.json", tmp / "journal.db", tmp / "watchlist.json"
    wl.write_text(json.dumps(watch))
    seed = state if state is not None else fs.fresh_flat_state(**SEED)
    if not no_state:
        fs.write_state(seed, st)
    before = st.read_bytes() if st.exists() else None

    orig = {k: getattr(fs, k) for k in (
        "STATE_PATH", "JOURNAL_PATH", "read_broker_financials", "alert_stub")}
    orig_creds = credentials.load_credentials
    orig_tc, orig_oc = probe.TradingClient, probe.OptionHistoricalDataClient
    orig_wl = probe.WATCHLIST_PATH

    FakeTC.constructed = FakeOC.constructed = 0
    box = {}

    class TC(FakeTC):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.clock = types.SimpleNamespace(timestamp=clock_ts, is_open=is_open)
            self.cash = cash
            self.raise_on_submit = raise_submit
            box["tc"] = self

    class OC(FakeOC):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.quote = quote if quote is not None else _quote()

        def get_option_latest_quote(self, req):
            return {} if quote_absent else super().get_option_latest_quote(req)

    obs = {"alerts": []}
    out = io.StringIO()
    try:
        fs.STATE_PATH, fs.JOURNAL_PATH = st, jr
        fs.read_broker_financials = lambda tc=None, coid=None: dict(fin or FIN)
        fs.alert_stub = lambda msg: obs["alerts"].append(msg)
        credentials.load_credentials = lambda: ("k", "s", paper)
        probe.TradingClient, probe.OptionHistoricalDataClient = TC, OC
        probe.WATCHLIST_PATH = wl
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                obs["ret"] = probe.main(["--contract", contract] + argv)
                obs["exc"] = None
            except Exception as e:
                obs["ret"] = None
                obs["exc"] = repr(e)
        conn = sqlite3.connect(str(jr))
        conn.row_factory = sqlite3.Row
        obs["decisions"] = [dict(r) for r in conn.execute(
            "SELECT action, detail FROM decisions ORDER BY rowid")]
        obs["runs"] = [r["outcome"] for r in conn.execute("SELECT outcome FROM runs")]
        conn.close()
        obs["stdout"] = out.getvalue()
        obs["submits"] = box["tc"].submits if "tc" in box else []
        obs["clients"] = FakeTC.constructed + FakeOC.constructed
        obs["before"] = before
        obs["after"] = st.read_bytes() if st.exists() else None
        obs["state_exists"] = st.exists()
        obs["state"] = fs.load_state(st) if st.exists() else None
        return obs
    finally:
        for k, v in orig.items():
            setattr(fs, k, v)
        credentials.load_credentials = orig_creds
        probe.TradingClient, probe.OptionHistoricalDataClient = orig_tc, orig_oc
        probe.WATCHLIST_PATH = orig_wl


class T:
    def __init__(self):
        self.failures, self.n = [], 0

    def ok(self, cond, msg):
        self.n += 1
        if not cond:
            self.failures.append(msg)


def _refusal(t, name, check, **kw):
    o = _drive([], **kw)
    t.ok(o["exc"] is None, f"{name} raised {o['exc']}")
    t.ok(o["ret"] == 3, f"{name} exit {o['ret']!r} != 3")
    t.ok(f"PROBE REFUSED {check}" in o["stdout"], f"{name} stdout {o['stdout']!r}")
    t.ok([d["action"] for d in o["decisions"]] == [f"probe:refused:{check}"],
         f"{name} decisions {o['decisions']!r}")
    t.ok(o["runs"] == [f"probe:refused:{check}"], f"{name} runs {o['runs']!r}")
    t.ok(len(o["submits"]) == 0, f"{name} submit calls {len(o['submits'])}")
    t.ok(o["before"] == o["after"], f"{name} state bytes changed")
    return o


def main():
    t = T()
    before = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL))

    o = _refusal(t, "R1", "not_paper", paper=False)
    t.ok(o["clients"] == 0, f"R1 clients constructed {o['clients']}")
    _refusal(t, "R2", "clock_invalid", clock_ts=CLOCK.replace(tzinfo=None))
    _refusal(t, "R3", "market_closed", is_open=False)
    held = dict(FIN, positions=[{"symbol": "F", "qty": 100,
                                 "avg_entry_price": 1.0, "market_value": 1.0}])
    st = fs.fresh_flat_state(**SEED)
    st["wheel"]["ticker"] = "F"
    o = _drive([], fin=held, state=st)
    t.ok(o["ret"] == 3 and "PROBE REFUSED not_flat_stable:" in o["stdout"],
         f"R4 stdout {o['stdout']!r} ret {o['ret']!r}")
    t.ok(o["decisions"][0]["action"].startswith("probe:refused:not_flat_stable:"),
         f"R4 decisions {o['decisions']!r}")
    t.ok(len(o["submits"]) == 0 and o["before"] == o["after"], "R4 submit/state")
    _refusal(t, "R5", "contract_invalid", contract="F261120C00012000")
    w6 = json.loads(json.dumps(WATCH))
    w6["entries"][0]["ticker"] = "XYZ"
    _refusal(t, "R6", "not_on_watchlist", watch=w6)
    w = json.loads(json.dumps(WATCH))
    w["entries"][0]["earnings_date"] = "2026-11-20"
    _refusal(t, "R7", "earnings_inside_option", watch=w)
    _refusal(t, "R8", "collateral", contract="F261120P00014000")
    _refusal(t, "R9", "quote_stale", quote=_quote(age_min=31))
    _refusal(t, "R10", "quote_crossed", quote=_quote(bid=1.0, ask=0.9))
    _refusal(t, "R11", "collar_risk", quote=_quote(bid=0.80, ask=0.90))

    o = _refusal(t, "R12", "state_missing", no_state=True)
    t.ok(o["clients"] == 0, f"R12 clients constructed {o['clients']}")
    t.ok(not o["state_exists"], "R12 state.json was created")
    _refusal(t, "R13", "contract_expired", contract="F261022P00012000")
    _refusal(t, "R14", "quote_missing", quote_absent=True)
    _refusal(t, "R15", "quote_stale", quote=_quote(naive=True))
    _refusal(t, "R16", "quote_invalid", quote=_quote(bid=float("nan")))
    _refusal(t, "R17", "quote_invalid", quote=_quote(ask=float("nan")))

    _refusal(t, "R18", "quote_missing", quote=_quote(bid=0.0, ask=0.99))
    _refusal(t, "R19", "quote_stale", quote=_quote(age_min=-1))

    # S1 dry
    o = _drive([])
    t.ok(o["exc"] is None, f"S1 raised {o['exc']}")
    t.ok(o["ret"] == 0, f"S1 exit {o['ret']!r}")
    t.ok(len(o["submits"]) == 0, "S1 submitted")
    t.ok(o["before"] == o["after"], "S1 state bytes changed")
    t.ok([d["action"] for d in o["decisions"]] == ["probe:propose"],
         f"S1 decisions {o['decisions']!r}")
    t.ok(o["runs"] == ["probe:dry"], f"S1 runs {o['runs']!r}")
    d = json.loads(o["decisions"][0]["detail"])
    t.ok(d["limit"] == 1.0 and d["gates_skipped"] == ["bid", "oi", "spread", "yield"],
         f"S1 detail {d!r}")

    # S2 submitted
    o = _drive(["--submit"])
    t.ok(o["exc"] is None, f"S2 raised {o['exc']}")
    t.ok(o["ret"] == 0, f"S2 exit {o['ret']!r}")
    t.ok(len(o["submits"]) == 1, f"S2 submit calls {len(o['submits'])}")
    req = o["submits"][0] if o["submits"] else None
    t.ok(req is not None and req.qty == 1 and req.side.value == "sell"
         and req.limit_price == 1.0 and req.time_in_force.value == "day"
         and req.symbol == PUT,
         f"S2 request {req!r}")
    res = json.loads(o["decisions"][-1]["detail"])
    t.ok(o["decisions"][-1]["action"] == "probe:submitted", "S2 decision action")
    t.ok(req is not None and req.client_order_id == res["client_order_id"],
         "S2 coid mismatch")
    t.ok(o["state"]["wheel"]["stage"] == fs.CSP_PENDING
         and o["state"]["wheel"]["client_order_id"] == res["client_order_id"],
         f"S2 disk state {o['state']['wheel']!r}")
    t.ok(o["runs"] == ["probe:submitted"], f"S2 runs {o['runs']!r}")

    # S3 submit raises
    o = _drive(["--submit"], raise_submit=RuntimeError("boom"))
    t.ok(o["exc"] is None, f"S3 raised {o['exc']}")
    t.ok(o["ret"] == 4, f"S3 exit {o['ret']!r}")
    res = json.loads(o["decisions"][-1]["detail"])
    t.ok(o["state"]["wheel"]["stage"] == fs.CSP_PENDING
         and o["state"]["wheel"]["client_order_id"] == res["client_order_id"]
         and res["client_order_id"] is not None,
         f"S3 disk state {o['state']['wheel']!r}")
    t.ok(len(o["alerts"]) >= 1, f"S3 alerts {o['alerts']!r}")
    t.ok(o["runs"] == ["probe:halt"], f"S3 runs {o['runs']!r}")

    # S4 isolation
    t.ok("wheel_dryrun" not in sys.modules, "S4 wheel_dryrun imported")

    after = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL))
    t.ok(before == after, "ISOLATION: real state.json / journal.db changed")

    if t.failures:
        print(f"{len(t.failures)} failed")
        for f in t.failures:
            print("  " + f)
        return 1
    print(f"PASS: 23 scenarios, {t.n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
