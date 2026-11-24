"""Forced-path net for funding criterion 4 (brief forced-paths).

Forces the four funding-4 broker paths through the REAL engine on BOTH legs
(CSP_PENDING via place_csp, CC_PENDING via place_cc):

  P1  tracked order partially_filled at *_PENDING (guards a size-up; the
      1-contract cap makes it unreachable today)
  P2  tracked order pending_cancel at *_PENDING, then canceled
  P3  a REAL process kill (child process, os._exit) right after the
      write-ahead state is on disk, before the intent row and the submit;
      then fresh child runs against a broker holding NO order with that id
  P4  submit_order raises the exception alpaca-py 0.43.5 raises for a gateway
      timeout (APIError, HTTP 504, after its own retries); variant a: the
      broker never got the order, variant b: it holds it under that id
  P5  (gap G2) run 1 accepted, then the broker shows a SECOND open order under
      the tracked client_order_id: the snapshot must HALT, never submit

Real: run.run(live=True) (stage + submit branch), fs.read_broker_financials
(incl. 404 -> not_found), fs.build_snapshot, fs.reconcile_and_handle,
submit.place_csp, submit.place_cc, fs.write_state, the journal.
Faked: the TradingClient class (FakeBroker: one JSON file per scenario holds
money, positions, orders by client_order_id, extra open orders that reuse a
client_order_id, every submit_order call and the pinned clock, so a child process and the parent share one broker), the two
data clients (raise on any call), credentials (placeholders), the two scans
(one passing row, reused from test_run_stages), alerts.send / fs.alert_stub
(files). Expected outcomes, code references and gaps: docs/FORCED_PATHS.md.

Temp dirs are mkdtemp and removed per scenario. A guard hashes the real
state.json / journal.db and lists the real scans/ before vs after.
"""
import contextlib
import datetime
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import types
import uuid
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import requests  # noqa: E402
import alpaca.data.historical.option as _alp_option  # noqa: E402
import alpaca.data.historical.stock as _alp_stock  # noqa: E402
import alpaca.trading.client as _alp_trading  # noqa: E402
from alpaca.common.exceptions import APIError  # noqa: E402
from alpaca.trading.enums import OrderStatus  # noqa: E402

import test_run_stages as trs  # noqa: E402  (sets FORGE_INSTANCE=a)
import alerts  # noqa: E402
import credentials  # noqa: E402
import forgetrader_state as fs  # noqa: E402
import wheel_dryrun  # noqa: E402
from forgetrader import cc_scan as _cc_scan_mod  # noqa: E402
from forgetrader.run import run  # noqa: E402

_REAL_STATE = Path(fs.STATE_PATH)
_REAL_JOURNAL = Path(fs.JOURNAL_PATH)
_REAL_SCANS = Path(fs.SCANS_DIR)

KILL_RC = 77
KILL_MARK = "KILLED_AFTER_WRITE_AHEAD"
UTC = datetime.timezone.utc
SEED_TS = datetime.datetime(2026, 11, 4, 14, 30, tzinfo=UTC)
CLOCKS = [trs.CLOCK_OPEN + datetime.timedelta(hours=i) for i in range(3)]
SEED_MONEY = {"equity": 5000.0, "cash": 5000.0, "options_buying_power": 5000.0}
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
TIMEOUT_TEXT = "504 Gateway Time-out"
TIMEOUT_TOKEN = f"HALT:submit APIError: {TIMEOUT_TEXT}"


def money_at(i):
    """Broker money for run i (1-based); differs every run so a refresh shows."""
    return {"equity": 5100.0 + i, "cash": 4900.0 + i,
            "options_buying_power": 4800.0 + i}


LEGS = {
    "CSP": {"home": fs.FLAT, "pending": fs.CSP_PENDING, "open": fs.CSP_OPEN,
            "stable": "noop:FLAT stable", "propose": "propose_csp",
            "sym": trs.LIVE_SYM, "base_positions": []},
    "CC": {"home": fs.HOLDING, "pending": fs.CC_PENDING, "open": fs.CC_OPEN,
           "stable": "noop:HOLDING stable", "propose": "propose_cc",
           "sym": trs.CC_SYM, "base_positions": [["F", 100]]},
}


# --------------------------------------------------------------------------- #
# Fake broker (file-backed, shared by the parent and child processes)         #
# --------------------------------------------------------------------------- #

BROKER = {"path": None}


def _api_error(code, text):
    """The SDK's own APIError, built as alpaca/common/rest.py:199-207 builds it."""
    resp = requests.Response()
    resp.status_code = code
    resp._content = text.encode()
    resp.url = "https://paper-api.alpaca.markets/v2/orders"
    return APIError(resp.text, requests.HTTPError(f"{code} Error", response=resp))


def broker_load(path):
    return json.loads(Path(path).read_text())


def broker_save(path, b):
    Path(path).write_text(json.dumps(b, indent=1, sort_keys=True))


def _order_obj(coid, o):
    return types.SimpleNamespace(id=uuid.UUID(o["id"]), client_order_id=coid,
                                 symbol=o["symbol"], status=OrderStatus(o["status"]))


class FakeBroker:
    """TradingClient stand-in. Every call reads the broker file; every change
    is saved before it returns or raises."""

    def __init__(self, *a, **k):
        self.path = BROKER["path"]

    def _b(self):
        return broker_load(self.path)

    def get_clock(self):
        b = self._b()
        return types.SimpleNamespace(
            timestamp=datetime.datetime.fromisoformat(b["clock_ts"]), is_open=True)

    def get_account(self):
        m = self._b()["money"]
        return types.SimpleNamespace(equity=str(m["equity"]), cash=str(m["cash"]),
                                     options_buying_power=str(m["options_buying_power"]))

    def get_all_positions(self):
        return [types.SimpleNamespace(symbol=s, qty=str(q), avg_entry_price="1.0",
                                      market_value="1.0")
                for s, q in self._b()["positions"]]

    def get_orders(self, req):
        if getattr(req.status, "value", req.status) != "open":
            raise AssertionError(f"unexpected get_orders filter {req!r}")
        b = self._b()
        # "dup_orders" (P5 only): further orders under an id already in "orders";
        # listed here, never returned by get_order_by_client_id.
        return [_order_obj(c, o) for c, o in
                list(b["orders"].items()) + [(o["coid"], o) for o in b.get("dup_orders", [])]
                if o["status"] not in TERMINAL]

    def get_order_by_client_id(self, coid):
        orders = self._b()["orders"]
        if coid not in orders:
            raise _api_error(404, '{"code":40410000,"message":"order not found"}')
        return _order_obj(coid, orders[coid])

    def submit_order(self, req):
        b = self._b()
        coid = req.client_order_id
        b["submits"].append({"coid": coid, "symbol": req.symbol, "qty": req.qty,
                             "limit_price": req.limit_price})
        mode = b["submit_mode"]
        oid = str(uuid.uuid4())
        if mode in ("accept", "timeout_kept"):
            b["orders"][coid] = {"id": oid, "symbol": req.symbol,
                                 "status": "accepted" if mode == "accept" else "new"}
        broker_save(self.path, b)
        if mode != "accept":
            raise _api_error(504, TIMEOUT_TEXT)
        return _order_obj(coid, b["orders"][coid])

    def __getattr__(self, attr):
        def unexpected(*a, **k):
            raise AssertionError(f"unexpected broker call {attr}")
        return unexpected


class FakeData:
    def __init__(self, *a, **k):
        pass

    def __getattr__(self, attr):
        def unexpected(*a, **k):
            raise AssertionError(f"unexpected data call {attr}")
        return unexpected


# --------------------------------------------------------------------------- #
# Engine environment (patches; used in-process and by the child)              #
# --------------------------------------------------------------------------- #

def _log(path, line):
    with open(path, "a") as f:
        f.write(line + "\n")


@contextlib.contextmanager
def engine_env(d, kill=False):
    d = Path(d)
    orig = {k: getattr(fs, k) for k in (
        "STATE_PATH", "JOURNAL_PATH", "SCANS_DIR", "load_state", "write_state",
        "journal_connect", "alert_stub")}
    orig_creds = credentials.load_credentials
    orig_send = alerts.send
    orig_scan = wheel_dryrun.scan_watchlist
    orig_cc_scan = _cc_scan_mod.scan_calls
    orig_clients = (_alp_trading.TradingClient, _alp_stock.StockHistoricalDataClient,
                    _alp_option.OptionHistoricalDataClient)
    ol, ow, oj = orig["load_state"], orig["write_state"], orig["journal_connect"]

    def write(state, path=None):
        r = ow(state, path or fs.STATE_PATH)
        if kill and state["wheel"]["stage"] in (fs.CSP_PENDING, fs.CC_PENDING):
            os.write(1, (KILL_MARK + "\n").encode())
            os._exit(KILL_RC)
        return r

    def scan(*a, **k):
        _log(d / "scans.log", "scan_watchlist")
        return trs._scan_result([], [trs.LIVE_ROW])

    def cc(*a, **k):
        _log(d / "scans.log", "scan_calls")
        return trs._cc_result([trs.CC_ROW])

    try:
        BROKER["path"] = d / "broker.json"
        fs.STATE_PATH, fs.JOURNAL_PATH, fs.SCANS_DIR = (
            d / "state.json", d / "journal.db", d / "scans")
        fs.load_state = lambda path=None: ol(path or fs.STATE_PATH)
        fs.write_state = write
        fs.journal_connect = lambda path=None: oj(path or fs.JOURNAL_PATH)
        fs.alert_stub = lambda msg: _log(d / "alerts.log", str(msg))
        credentials.load_credentials = lambda: ("k", "s", True)
        alerts.send = lambda text, *a, **k: _log(d / "sent.log", str(text))
        wheel_dryrun.scan_watchlist = scan
        _cc_scan_mod.scan_calls = cc
        _alp_trading.TradingClient = FakeBroker
        _alp_stock.StockHistoricalDataClient = FakeData
        _alp_option.OptionHistoricalDataClient = FakeData
        yield
    finally:
        for k, v in orig.items():
            setattr(fs, k, v)
        credentials.load_credentials = orig_creds
        alerts.send = orig_send
        wheel_dryrun.scan_watchlist = orig_scan
        _cc_scan_mod.scan_calls = orig_cc_scan
        (_alp_trading.TradingClient, _alp_stock.StockHistoricalDataClient,
         _alp_option.OptionHistoricalDataClient) = orig_clients
        BROKER["path"] = None


def child_main(argv):
    """python tests/test_forced_paths.py --child DIR [--kill]: one live run."""
    with engine_env(argv[2], kill="--kill" in argv):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run(live=True)
    return rc


# --------------------------------------------------------------------------- #
# Scenario plumbing                                                           #
# --------------------------------------------------------------------------- #

def new_scenario(leg):
    """mkdtemp dir with the leg's seed state and an idle broker at the home stage."""
    d = Path(tempfile.mkdtemp(prefix="forced_paths_"))
    fs.pin_now(SEED_TS)
    try:
        s = fs.fresh_flat_state(**SEED_MONEY)
        if leg == "CC":
            s["wheel"].update({"stage": fs.HOLDING, "ticker": "F", "cost_basis": 12.0})
        fs.write_state(s, d / "state.json")
    finally:
        fs.pin_now(None)
    broker_save(d / "broker.json", {
        "clock_ts": CLOCKS[0].isoformat(), "money": money_at(1),
        "positions": [list(p) for p in LEGS[leg]["base_positions"]],
        "orders": {}, "submits": [], "submit_mode": "accept"})
    return d


def set_broker(d, i, **kw):
    """Before run i (1-based): pin its clock + money, apply scenario changes."""
    b = broker_load(d / "broker.json")
    b["clock_ts"] = CLOCKS[i - 1].isoformat()
    b["money"] = money_at(i)
    b.update(kw)
    broker_save(d / "broker.json", b)


def set_order(d, coid, status):
    b = broker_load(d / "broker.json")
    if coid in b["orders"]:
        b["orders"][coid]["status"] = status
    broker_save(d / "broker.json", b)


def add_dup_order(d, coid, symbol, status="new"):
    """A second broker order under an id the broker already holds (gap G2)."""
    b = broker_load(d / "broker.json")
    b.setdefault("dup_orders", []).append(
        {"coid": coid, "id": str(uuid.uuid4()), "symbol": symbol, "status": status})
    broker_save(d / "broker.json", b)


def _journal(d):
    p = Path(d) / "journal.db"
    if not p.exists():
        return {"runs": [], "decisions": [], "orders": [], "events": []}
    conn = sqlite3.connect(str(p))
    try:
        return {
            "runs": [r[0] for r in conn.execute("SELECT outcome FROM runs ORDER BY rowid")],
            "decisions": [r[0] for r in conn.execute(
                "SELECT action FROM decisions ORDER BY rowid")],
            "orders": [(r[0], r[1]) for r in conn.execute(
                "SELECT status, client_order_id FROM orders ORDER BY rowid")],
            "events": [r[0] for r in conn.execute(
                "SELECT event_type FROM events ORDER BY rowid")],
        }
    finally:
        conn.close()


def _lines(p):
    p = Path(p)
    return p.read_text().splitlines() if p.exists() else []


def run_once(d, child=False, kill=False):
    """One live run; returns this run's observations (journal rows after the
    pre-run watermark, the state file, the broker's submit total)."""
    d = Path(d)
    before = _journal(d)
    scans_before = len(_lines(d / "scans.log"))
    obs = {"stdout": ""}
    if child:
        argv = [sys.executable, os.path.abspath(__file__), "--child", str(d)]
        if kill:
            argv.append("--kill")
        p = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=300)
        obs["rc"], obs["stdout"] = p.returncode, p.stdout
    else:
        with engine_env(d), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                obs["rc"] = run(live=True)
            except Exception as e:  # reported by the rc assertion, never a crash
                obs["rc"] = f"EXC {type(e).__name__}: {e}"
    after = _journal(d)
    for k in after:
        obs[k] = after[k][len(before[k]):]
    obs["scanned"] = len(_lines(d / "scans.log")) > scans_before
    raw = (d / "state.json").read_text() if (d / "state.json").exists() else ""
    try:
        obs["state"], obs["parse_ok"] = json.loads(raw), True
    except ValueError:
        obs["state"], obs["parse_ok"] = None, False
    obs["tmp_left"] = (d / "state.json.tmp").exists()
    obs["submits"] = broker_load(d / "broker.json")["submits"]
    return obs


def _wheel(o):
    st = o["state"] if isinstance(o.get("state"), dict) else {}
    return st.get("wheel") or {}


def _money(state):
    if not isinstance(state, dict):
        return None
    return {k: state.get(k) for k in SEED_MONEY}


def _strip(state):
    if not isinstance(state, dict):
        return None
    return {k: v for k, v in state.items() if k not in ("last_run", "generated_at")}


def _is_uuid(v):
    try:
        return isinstance(v, str) and str(uuid.UUID(v)) == v
    except ValueError:
        return False


ANY_UUID = object()


def expect(t, label, o, *, rc, outcome, stage, coid, decisions, orders, events,
           submits, money, scan, last=None, unchanged_from=None):
    """The per-run assertions (11, plus 1 on a broker-mismatch HALT)."""
    st = o["state"] if isinstance(o.get("state"), dict) else {}
    t.ok(o["rc"] == rc, f"{label} exit {o['rc']!r} != {rc!r}")
    want_runs = [outcome] if outcome is not None else []
    t.ok(o["runs"] == want_runs, f"{label} runs {o['runs']!r} != {want_runs!r}")
    want_last = outcome if last is None else last
    got_last = (st.get("last_run") or {}).get("outcome")
    t.ok(got_last == want_last, f"{label} last_run {got_last!r} != {want_last!r}")
    w = _wheel(o)
    t.ok(w.get("stage") == stage, f"{label} stage {w.get('stage')!r} != {stage!r}")
    got_coid = w.get("client_order_id")
    if coid is ANY_UUID:
        t.ok(_is_uuid(got_coid), f"{label} coid {got_coid!r} is not a uuid4 string")
    else:
        t.ok(got_coid == coid, f"{label} coid {got_coid!r} != {coid!r}")
    t.ok(o["decisions"] == decisions, f"{label} decisions {o['decisions']!r} != {decisions!r}")
    t.ok(o["orders"] == orders, f"{label} orders {o['orders']!r} != {orders!r}")
    t.ok(o["events"] == events, f"{label} events {o['events']!r} != {events!r}")
    t.ok(len(o["submits"]) == submits,
         f"{label} submit_order calls so far {len(o['submits'])} != {submits}")
    t.ok(_money(st) == money, f"{label} money {_money(st)!r} != {money!r}")
    t.ok(o["scanned"] == scan, f"{label} scan ran={o['scanned']!r} != {scan!r}")
    if unchanged_from is not None:
        t.ok(_strip(st) == _strip(unchanged_from),
             f"{label} state written beyond last_run on a broker-mismatch HALT")


def _submitted_coid(d, n=0):
    s = broker_load(Path(d) / "broker.json")["submits"]
    return s[n]["coid"] if len(s) > n else None


def run1_live(t, label, leg, d, mode):
    """Run 1: FLAT / HOLDING stable, a passing proposal, one live submit."""
    L = LEGS[leg]
    set_broker(d, 1, submit_mode=mode)
    o = run_once(d)
    c = _submitted_coid(d)
    if mode == "accept":
        tok = f"submit:submitted status=accepted coid={c}"
        expect(t, f"{label} run1", o, rc=0, outcome=f"live:{tok}", stage=L["pending"],
               coid=c, decisions=[L["stable"], L["propose"], tok],
               orders=[(None, c), ("accepted", c)], events=["live_complete"],
               submits=1, money=money_at(1), scan=True)
    else:
        expect(t, f"{label} run1", o, rc=4, outcome=f"live:{TIMEOUT_TOKEN}",
               stage=L["pending"], coid=c,
               decisions=[L["stable"], L["propose"], TIMEOUT_TOKEN],
               orders=[(None, c)], events=["submit_exception"],
               submits=1, money=money_at(1), scan=True)
    # The broker is healthy again after run 1 (a resubmit would be accepted).
    set_broker(d, 1, submit_mode="accept")
    return o, c


def scenario_end(t, label, d):
    n = len(broker_load(Path(d) / "broker.json")["submits"])
    t.ok(n <= 1, f"{label} DOUBLE SUBMIT: {n} submit_order calls in the scenario")


# --------------------------------------------------------------------------- #
# Scenarios                                                                   #
# --------------------------------------------------------------------------- #

def p1(t, leg):
    L, label = LEGS[leg], f"P1-{leg}"
    d = new_scenario(leg)
    try:
        o1, c = run1_live(t, label, leg, d, "accept")
        set_order(d, c, "partially_filled")
        set_broker(d, 2, positions=L["base_positions"] + [[L["sym"], -1]])
        o2 = run_once(d)
        halt = f"HALT:{L['pending']} order status 'partially_filled' requires review"
        expect(t, f"{label} run2", o2, rc=4, outcome=f"live:{halt}", stage=L["pending"],
               coid=c, decisions=[halt], orders=[], events=[], submits=1,
               money=money_at(1), scan=False, unchanged_from=o1["state"])
        scenario_end(t, label, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def p2(t, leg):
    L, label = LEGS[leg], f"P2-{leg}"
    d = new_scenario(leg)
    try:
        o1, c = run1_live(t, label, leg, d, "accept")
        set_order(d, c, "pending_cancel")
        set_broker(d, 2)
        o2 = run_once(d)
        wait = f"noop:{L['pending']} awaiting fill"
        expect(t, f"{label} run2", o2, rc=0, outcome=f"live:{wait}", stage=L["pending"],
               coid=c, decisions=[wait], orders=[], events=["live_complete"],
               submits=1, money=money_at(2), scan=False)
        set_order(d, c, "canceled")
        set_broker(d, 3)
        o3 = run_once(d)
        tr = f"{L['pending']}->{L['home']} (cancel_or_expire)"
        expect(t, f"{label} run3", o3, rc=0, outcome=f"live:{tr}", stage=L["home"],
               coid=None, decisions=[tr], orders=[], events=["live_complete"],
               submits=1, money=money_at(3), scan=False)
        scenario_end(t, label, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def p3(t, leg):
    L, label = LEGS[leg], f"P3-{leg}"
    d = new_scenario(leg)
    try:
        set_broker(d, 1, submit_mode="accept")
        o1 = run_once(d, child=True, kill=True)
        t.ok(KILL_MARK in o1["stdout"], f"{label} run1 kill hook did not fire "
             f"(rc {o1['rc']!r}, stdout {o1['stdout'][-200:]!r})")
        t.ok(o1["parse_ok"], f"{label} run1 state.json is not parseable JSON after the kill")
        t.ok(not o1["tmp_left"], f"{label} run1 state.json.tmp left after the kill")
        expect(t, f"{label} run1", o1, rc=KILL_RC, outcome=None, last="init",
               stage=L["pending"], coid=ANY_UUID,
               decisions=[L["stable"], L["propose"]], orders=[], events=[],
               submits=0, money=money_at(1), scan=True)
        killed = _wheel(o1).get("client_order_id")
        nf = f"HALT:{L['pending']} tracked order not_found at broker"
        prev = o1
        for i in (2, 3):
            set_broker(d, i)
            o = run_once(d, child=True)
            expect(t, f"{label} run{i}", o, rc=4, outcome=f"live:{nf}",
                   stage=L["pending"], coid=killed, decisions=[nf], orders=[],
                   events=[], submits=0, money=money_at(1), scan=False,
                   unchanged_from=prev["state"])
            prev = o
        scenario_end(t, label, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def p4a(t, leg):
    L, label = LEGS[leg], f"P4a-{leg}"
    d = new_scenario(leg)
    try:
        o1, c = run1_live(t, label, leg, d, "timeout_lost")
        nf = f"HALT:{L['pending']} tracked order not_found at broker"
        prev = o1
        for i in (2, 3):
            set_broker(d, i)
            o = run_once(d)
            expect(t, f"{label} run{i}", o, rc=4, outcome=f"live:{nf}",
                   stage=L["pending"], coid=c, decisions=[nf], orders=[], events=[],
                   submits=1, money=money_at(1), scan=False,
                   unchanged_from=prev["state"])
            prev = o
        scenario_end(t, label, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def p4b(t, leg):
    L, label = LEGS[leg], f"P4b-{leg}"
    d = new_scenario(leg)
    try:
        _o1, c = run1_live(t, label, leg, d, "timeout_kept")
        set_broker(d, 2)
        o2 = run_once(d)
        wait = f"noop:{L['pending']} awaiting fill"
        expect(t, f"{label} run2", o2, rc=0, outcome=f"live:{wait}", stage=L["pending"],
               coid=c, decisions=[wait], orders=[], events=["live_complete"],
               submits=1, money=money_at(2), scan=False)
        set_order(d, c, "filled")
        set_broker(d, 3, positions=L["base_positions"] + [[L["sym"], -1]])
        o3 = run_once(d)
        tr = f"{L['pending']}->{L['open']} (fill)"
        expect(t, f"{label} run3", o3, rc=0, outcome=f"live:{tr}", stage=L["open"],
               coid=None, decisions=[tr], orders=[], events=["live_complete"],
               submits=1, money=money_at(3), scan=False)
        scenario_end(t, label, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def p5(t, leg):
    L, label = LEGS[leg], f"P5-{leg}"
    d = new_scenario(leg)
    try:
        o1, c = run1_live(t, label, leg, d, "accept")
        add_dup_order(d, c, L["sym"])
        set_broker(d, 2)
        o2 = run_once(d)
        halt = f"HALT:snapshot more than one open order with tracked client_order_id {c}: 2"
        expect(t, f"{label} run2", o2, rc=4, outcome=f"live:{halt}", stage=L["pending"],
               coid=c, decisions=[halt], orders=[], events=[], submits=1,
               money=money_at(1), scan=False, unchanged_from=o1["state"])
        scenario_end(t, label, d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def main():
    t = trs.T()
    before = (_sha(_REAL_STATE), _sha(_REAL_JOURNAL),
              sorted(os.listdir(_REAL_SCANS)) if _REAL_SCANS.exists() else None)
    scenarios = 0
    for fn in (p1, p2, p3, p4a, p4b, p5):
        for leg in ("CSP", "CC"):
            scenarios += 1
            try:
                fn(t, leg)
            except Exception as e:  # a crash is reported, never a silent pass
                t.ok(False, f"{fn.__name__}-{leg} crashed: {type(e).__name__}: {e}")
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
    print(f"PASS: test_forced_paths {scenarios} scenarios (P1 P2 P3 P4a P4b P5 x CSP CC), "
          f"{t.n} assertions")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        sys.exit(child_main(sys.argv))
    sys.exit(main())
