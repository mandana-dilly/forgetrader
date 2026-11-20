"""Offline net for Brief B8: one broker clock per run (D13), the pre-clock
carve-out (D13a) and the option quote timestamp + ask on gate rows (D31).

The broker clock is pinned to 2026-11-05 02:30 UTC (21:30 ET, ET date
2026-11-04) and the host clock is patched to a different day, 2026-11-09
13:00 UTC, in forgetrader.run AND forgetrader_state (their module-level
datetime), so a host stamp anywhere on the run path reads as that other day.

  K  one clock: post-clock runs (dry FLAT propose_csp, live CSP submit, live
     CC submit, HALT:snapshot, reconcile HALT) stamp every runs / decisions /
     events / orders ts, last_run.ts, generated_at and opened_at with the
     clock instant (literal); reconcile gets as_of_date = its ET date; the CSP
     scan gets now_et = the clock in ET and run_date = its ET date; the HALT
     dump is named and stamped from the clock. K7 (D13b): the same instant
     handed over with the ET offset still stamps every row with the UTC string.
  P  pre-clock: state_missing, instance, not_paper, tz-naive clock and
     missing clock timestamp stamp every row / last_run / generated_at with
     the host instant (literal); a credentials, client-build or get_clock
     failure is uncaught and writes nothing. PRECLOCK_OUTCOMES is exactly
     the six tokens; preclock_now returns host time for each and refuses
     every post-clock outcome.
  Q  D31: quote_fields literal values and None; the real CSP producer
     (wheel_dryrun.scan_watchlist) and CC producer (cc_scan.scan_calls) put
     quote_ts / quote_ask (literal, or None when the quote lacks them) on
     every gate row without changing any gate outcome; the journalled
     propose_csp / propose_cc rows carry them.
  I  isolation: an AST scan finds host-clock calls in run.py and submit.py
     only inside preclock_now (and bites on a planted one).

Temp dirs only (test_run_stages._drive redirects state / journal / scans);
never the network, never a real .env. A guard hashes the real state.json /
journal.db before vs after. Last line PASS: test_clock ... or FAIL: N
failed of M assertions.
"""
import ast
import contextlib
import copy
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
from zoneinfo import ZoneInfo

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import alpaca.trading.client as _alp_trading  # noqa: E402
import alerts  # noqa: E402
import credentials  # noqa: E402
import forgetrader_state as fs  # noqa: E402
import gates  # noqa: E402
import screener  # noqa: E402
import test_cc_gates as tcg  # noqa: E402
import test_live_submit as tls  # noqa: E402
import test_run_stages as trs  # noqa: E402
import wheel_dryrun  # noqa: E402
from alpaca.data.models.snapshots import OptionsSnapshot  # noqa: E402
from forgetrader import run as run_mod  # noqa: E402

os.environ["FORGE_INSTANCE"] = "a"  # the imported suites set it; pin it here

UTC = datetime.timezone.utc
CLOCK = datetime.datetime(2026, 11, 5, 2, 30, tzinfo=UTC)
CLOCK_ISO = "2026-11-05T02:30:00+00:00"
CLOCK_ET_DATE = datetime.date(2026, 11, 4)
CLOCK_ET_ISO = "2026-11-04T21:30:00-05:00"
CLOCK_NY = CLOCK.astimezone(ZoneInfo("America/New_York"))  # K7: Alpaca's ET-offset form
HOST = datetime.datetime(2026, 11, 9, 13, 0, tzinfo=UTC)
HOST_ISO = "2026-11-09T13:00:00+00:00"
REAL_FILES = [Path(ROOT) / "state.json", Path(ROOT) / "journal.db"]

n = 0
failures = []


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)
    return bool(cond)


def sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


class HostDT(datetime.datetime):
    """The patched host clock: always 2026-11-09 13:00 UTC."""

    @classmethod
    def now(cls, tz=None):
        return HOST.astimezone(tz) if tz else HOST.replace(tzinfo=None)

    @classmethod
    def utcnow(cls):
        return HOST.replace(tzinfo=None)


@contextlib.contextmanager
def host_clock():
    orig = run_mod.datetime, fs.datetime
    run_mod.datetime = fs.datetime = HostDT
    try:
        yield
    finally:
        run_mod.datetime, fs.datetime = orig


@contextlib.contextmanager
def spy_reconcile(seen):
    orig = fs.reconcile_and_handle

    def spy(*a, **k):
        seen.append(k.get("as_of_date", "NOT_PASSED"))
        return orig(*a, **k)

    fs.reconcile_and_handle = spy
    try:
        yield
    finally:
        fs.reconcile_and_handle = orig


def _journal(obs):
    """Every ts in the four journal tables of a _drive run, by table."""
    path = Path(obs["tmp_state"]).parent / "journal.db"
    conn = sqlite3.connect(str(path))
    try:
        return {tbl: [r[0] for r in conn.execute(f"SELECT ts FROM {tbl} ORDER BY rowid")]
                for tbl in ("runs", "decisions", "events", "orders")}
    finally:
        conn.close()


def _drive(label, *a, **k):
    t = trs.T()
    seen = []
    with host_clock(), spy_reconcile(seen):
        obs = trs._drive(t, label, *a, **k)
    for f in t.failures:
        check(False, f"{label} (harness) {f}")
    obs["as_of_dates"] = seen
    obs["ts"] = _journal(obs)
    return obs


def _all_stamped(label, obs, want, tables=("runs", "decisions", "events", "orders")):
    """Every journal row ts of the run is want; returns the row count."""
    rows = [(tbl, ts) for tbl in tables for ts in obs["ts"][tbl]]
    bad = [(tbl, ts) for tbl, ts in rows if ts != want]
    check(rows and not bad, f"{label} journal ts not all {want}: {bad or rows!r}")
    return len(rows)


def _state_stamped(label, obs, want):
    st = obs["state"]
    check(st is not None and st["last_run"]["ts"] == want,
          f"{label} last_run.ts {st and st['last_run']!r} != {want}")
    check(st is not None and st["generated_at"] == want,
          f"{label} generated_at {st and st['generated_at']!r} != {want}")


# --------------------------------------------------------------------------- #
# K - one broker clock for every post-clock stamp                              #
# --------------------------------------------------------------------------- #

QUOTE_TS = "2026-11-05T02:29:10+00:00"
CSP_ROW = dict(trs.LIVE_ROW, quote_ts=QUOTE_TS, quote_ask=0.21)
CC_ROW = dict(trs.CC_ROW, quote_ts=QUOTE_TS, quote_ask=0.3)


def part_k():
    flat = trs._state(fs.FLAT)
    fin = trs._fin()
    scan_args = []

    def scan(*a, **k):
        scan_args.append(a[4:6])
        return trs._scan_result([], [CSP_ROW])

    # K1 dry FLAT propose_csp.
    o = _drive("K1", flat, fin, clock_ts=CLOCK, scan=scan)
    check(o["ret"] == 0, f"K1 exit {o['ret']!r}")
    _all_stamped("K1", o, CLOCK_ISO)
    _state_stamped("K1", o, CLOCK_ISO)
    check(o["as_of_dates"] == [CLOCK_ET_DATE], f"K1 as_of_date {o['as_of_dates']!r}")
    check(len(scan_args) == 1 and scan_args[0][0].isoformat() == CLOCK_ET_ISO
          and scan_args[0][1] == CLOCK_ET_DATE,
          f"K1 scan now_et / run_date {scan_args!r}")
    check(len(o["ts"]["decisions"]) == 2 and len(o["ts"]["runs"]) == 1
          and len(o["ts"]["events"]) == 1, f"K1 row counts {o['ts']!r}")

    # K2 live FLAT submit: opened_at and both orders rows are the clock.
    o = _drive("K2", flat, fin, clock_ts=CLOCK, live=True, is_open=True,
               scan=lambda *a, **k: trs._scan_result([], [CSP_ROW]))
    check(o["ret"] == 0 and len(o["submit_calls"]) == 1,
          f"K2 exit {o['ret']!r} submits {len(o['submit_calls'])}")
    w = o["state"]["wheel"]
    check(w["stage"] == fs.CSP_PENDING and w["opened_at"] == CLOCK_ISO,
          f"K2 opened_at {w['opened_at']!r} stage {w['stage']!r}")
    check(len(o["ts"]["orders"]) == 2, f"K2 orders rows {o['ts']['orders']!r}")
    _all_stamped("K2", o, CLOCK_ISO)
    _state_stamped("K2", o, CLOCK_ISO)

    # K3 live HOLDING covered-call submit.
    hold = trs._state(fs.HOLDING, None, None)
    o = _drive("K3", hold, trs._fin([trs._pos("F", 100)]), clock_ts=CLOCK,
               live=True, is_open=True,
               cc_scan=lambda *a, **k: trs._cc_result([CC_ROW]))
    check(o["ret"] == 0 and len(o["submit_calls"]) == 1,
          f"K3 exit {o['ret']!r} submits {len(o['submit_calls'])}")
    w = o["state"]["wheel"]
    check(w["stage"] == fs.CC_PENDING and w["opened_at"] == CLOCK_ISO,
          f"K3 opened_at {w['opened_at']!r} stage {w['stage']!r}")
    check(len(o["ts"]["orders"]) == 2, f"K3 orders rows {o['ts']['orders']!r}")
    _all_stamped("K3", o, CLOCK_ISO)
    _state_stamped("K3", o, CLOCK_ISO)
    check(o["as_of_dates"] == [CLOCK_ET_DATE], f"K3 as_of_date {o['as_of_dates']!r}")

    # K4 HALT:snapshot (after the clock): rows, state and the dump.
    o = _drive("K4", flat, trs._fin([trs._pos("F", 100)]), clock_ts=CLOCK)
    check(o["ret"] == 4 and o["runs"] and "HALT:snapshot" in o["runs"][0]["outcome"],
          f"K4 exit {o['ret']!r} runs {o['runs']!r}")
    _all_stamped("K4", o, CLOCK_ISO)
    _state_stamped("K4", o, CLOCK_ISO)
    check(o["halt_dumps"] == ["state_halt_20261105_023000Z.txt"],
          f"K4 dump name {o['halt_dumps']!r}")
    dumps = list(Path(o["tmp_scans"]).glob("state_halt_*.txt"))
    check(len(dumps) == 1 and f"written_at : {CLOCK_ISO}\n" in dumps[0].read_text(),
          "K4 dump written_at is not the clock")

    # K5 reconcile HALT: reconcile_and_handle's own decision row + its dump.
    o = _drive("K5", trs._state(fs.CSP_OPEN, trs.PUT, trs.COID),
               trs._fin([trs._pos(trs.PUT, -1), trs._pos("F", 100)]), clock_ts=CLOCK)
    check(o["ret"] == 4, f"K5 exit {o['ret']!r}")
    _all_stamped("K5", o, CLOCK_ISO)
    _state_stamped("K5", o, CLOCK_ISO)
    check(o["halt_dumps"] == ["state_halt_20261105_023000Z.txt"],
          f"K5 dump name {o['halt_dumps']!r}")
    check(o["as_of_dates"] == [CLOCK_ET_DATE], f"K5 as_of_date {o['as_of_dates']!r}")

    # K6 the pin is released after a run: outside a run fs reads the host.
    with host_clock():
        check(fs._now_iso() == HOST_ISO, f"K6 pin leaked past the run {fs._now_iso()!r}")

    # K7 (D13b) the clock instant arrives with the ET offset, as Alpaca sends
    # it; every stamp is still the UTC string, now_et and as_of_date unchanged.
    scan_args.clear()
    o = _drive("K7a", flat, fin, clock_ts=CLOCK_NY, scan=scan)
    check(o["ret"] == 0, f"K7a exit {o['ret']!r}")
    _all_stamped("K7a", o, CLOCK_ISO)
    _state_stamped("K7a", o, CLOCK_ISO)
    check(o["as_of_dates"] == [CLOCK_ET_DATE], f"K7a as_of_date {o['as_of_dates']!r}")
    check(len(scan_args) == 1 and scan_args[0][0].isoformat() == CLOCK_ET_ISO
          and scan_args[0][1] == CLOCK_ET_DATE,
          f"K7a scan now_et / run_date {scan_args!r}")
    o = _drive("K7b", flat, fin, clock_ts=CLOCK_NY, live=True, is_open=True,
               scan=lambda *a, **k: trs._scan_result([], [CSP_ROW]))
    check(o["ret"] == 0 and len(o["submit_calls"]) == 1,
          f"K7b exit {o['ret']!r} submits {len(o['submit_calls'])}")
    w = o["state"]["wheel"]
    check(w["stage"] == fs.CSP_PENDING and w["opened_at"] == CLOCK_ISO,
          f"K7b opened_at {w['opened_at']!r} stage {w['stage']!r}")
    check(len(o["ts"]["orders"]) == 2, f"K7b orders rows {o['ts']['orders']!r}")
    _all_stamped("K7b", o, CLOCK_ISO)
    _state_stamped("K7b", o, CLOCK_ISO)
    check(o["as_of_dates"] == [CLOCK_ET_DATE], f"K7b as_of_date {o['as_of_dates']!r}")


# --------------------------------------------------------------------------- #
# P - the pre-clock carve-out                                                   #
# --------------------------------------------------------------------------- #

PRECLOCK = frozenset({"state_missing", "instance", "not_paper", "credentials",
                      "clients", "clock"})
POST_CLOCK = ("snapshot", "reconcile", "propose_csp", "submit", "complete",
              "watchlist_structure", "HALT:state_missing", "", None)


@contextlib.contextmanager
def no_instance():
    """FORGE_INSTANCE absent and an empty temp .env (never the real one)."""
    saved = os.environ.pop("FORGE_INSTANCE", None)
    d = Path(tempfile.mkdtemp(prefix="test_clock_env."))
    (d / ".env").write_text("")
    orig = credentials._ENV_PATH, alerts.ENV_PATH
    credentials._ENV_PATH = alerts.ENV_PATH = d / ".env"
    try:
        yield
    finally:
        credentials._ENV_PATH, alerts.ENV_PATH = orig
        os.environ.pop("FORGE_INSTANCE", None)
        if saved is not None:
            os.environ["FORGE_INSTANCE"] = saved


def _bare(label, creds=None, tc_cls=None):
    """run() with credentials / TradingClient replaced; returns (exception
    class name, journal row count, state sha before, after)."""
    tmp = Path(tempfile.mkdtemp(prefix="test_clock_bare."))
    st, jr = tmp / "state.json", tmp / "journal.db"
    orig = {k: getattr(fs, k) for k in ("STATE_PATH", "JOURNAL_PATH", "SCANS_DIR",
                                        "load_state", "write_state", "journal_connect")}
    ol, ow, oj = orig["load_state"], orig["write_state"], orig["journal_connect"]
    ocreds, otc, osend = credentials.load_credentials, _alp_trading.TradingClient, alerts.send
    ow(trs._state(fs.FLAT), st)
    before = sha(st)
    exc = None
    try:
        fs.STATE_PATH, fs.JOURNAL_PATH, fs.SCANS_DIR = st, jr, tmp / "scans"
        fs.load_state = lambda path=None: ol(path or fs.STATE_PATH)
        fs.write_state = lambda state, path=None: ow(state, path or fs.STATE_PATH)
        fs.journal_connect = lambda path=None: oj(path or fs.JOURNAL_PATH)
        credentials.load_credentials = creds or (lambda: ("k", "s", True))
        if tc_cls is not None:
            _alp_trading.TradingClient = tc_cls
        alerts.send = lambda *a, **k: None
        with host_clock(), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                run_mod.run(live=False)
            except Exception as e:  # uncaught by design (exit 1, D7)
                exc = type(e).__name__
    finally:
        for k, v in orig.items():
            setattr(fs, k, v)
        credentials.load_credentials, _alp_trading.TradingClient, alerts.send = \
            ocreds, otc, osend
    conn = sqlite3.connect(str(jr))
    rows = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
               for t in ("runs", "decisions", "events", "orders"))
    conn.close()
    return exc, rows, before, sha(st)


def part_p():
    # P1 the closed set, exactly.
    check(run_mod.PRECLOCK_OUTCOMES == PRECLOCK
          and isinstance(run_mod.PRECLOCK_OUTCOMES, frozenset),
          f"P1 PRECLOCK_OUTCOMES {run_mod.PRECLOCK_OUTCOMES!r}")
    # P2 preclock_now: host time for every member, refusal for anything else.
    with host_clock():
        for tok in sorted(PRECLOCK):
            got = run_mod.preclock_now(tok)
            check(got == HOST and got.tzinfo is not None,
                  f"P2 preclock_now({tok!r}) {got!r}")
        for tok in POST_CLOCK:
            try:
                got = run_mod.preclock_now(tok)
                check(False, f"P2 preclock_now({tok!r}) accepted: {got!r}")
            except ValueError:
                check(True, "")

    flat = trs._state(fs.FLAT)
    fin = trs._fin()

    # P3 state_missing: one runs row, host time, nothing created.
    o = _drive("P3", None, fin, clock_ts=CLOCK, expect_clock=0)
    check(o["ret"] == 4 and o["state"] is None, f"P3 exit {o['ret']!r}")
    check(o["ts"]["runs"] == [HOST_ISO] and o["ts"]["decisions"] == []
          and o["ts"]["events"] == [], f"P3 ts {o['ts']!r}")

    # P4 instance: runs row + last_run + generated_at, host time.
    sent = []
    with no_instance():
        o = _drive("P4", flat, fin, clock_ts=CLOCK, expect_clock=0,
                   send=lambda text, prefix=None: sent.append(text))
    check(o["ret"] == 4 and o["runs"] and "HALT:instance" in o["runs"][0]["outcome"],
          f"P4 exit {o['ret']!r} runs {o['runs']!r}")
    check(o["ts"]["runs"] == [HOST_ISO], f"P4 ts {o['ts']!r}")
    _state_stamped("P4", o, HOST_ISO)

    # P5 --live paper refusal: decision + runs rows, host time, state untouched.
    o = _drive("P5", flat, fin, clock_ts=CLOCK, live=True, paper=False,
               expect_clock=0)
    check(o["ret"] == 3 and o["state_sha"] == o["seed_sha"],
          f"P5 exit {o['ret']!r} state changed {o['state_sha'] != o['seed_sha']}")
    check(o["ts"]["decisions"] == [HOST_ISO] and o["ts"]["runs"] == [HOST_ISO],
          f"P5 ts {o['ts']!r}")

    # P6 tz-naive clock, P7 missing clock timestamp: HALT, host time.
    for label, cts in (("P6", CLOCK.replace(tzinfo=None)), ("P7", None)):
        o = _drive(label, flat, fin, clock_ts=cts)
        check(o["ret"] == 4 and not o["fin_calls"],
              f"{label} exit {o['ret']!r} fin calls {o['fin_calls']!r}")
        check(o["ts"]["decisions"] == [HOST_ISO] and o["ts"]["runs"] == [HOST_ISO],
              f"{label} ts {o['ts']!r}")
        _state_stamped(label, o, HOST_ISO)

    # P8 credentials / client build / get_clock failures: uncaught, nothing
    #    written (so nothing stamped), state untouched.
    def creds_fail():
        raise OSError("fake credentials failure")

    class TCFail:
        def __init__(self, *a, **k):
            raise RuntimeError("fake client build failure")

    class TCClockFail:
        def __init__(self, *a, **k):
            pass

        def get_clock(self):
            raise ConnectionError("fake get_clock failure")

    for label, kw, want in (("P8 credentials", {"creds": creds_fail}, "OSError"),
                            ("P8 clients", {"tc_cls": TCFail}, "RuntimeError"),
                            ("P8 get_clock", {"tc_cls": TCClockFail}, "ConnectionError")):
        exc, rows, before, after = _bare(label, **kw)
        check(exc == want and rows == 0 and before == after,
              f"{label} exc {exc!r} rows {rows} state changed {before != after}")


# --------------------------------------------------------------------------- #
# Q - D31 quote timestamp + ask on gate rows                                    #
# --------------------------------------------------------------------------- #

def _csp_scan(option_client):
    """The real scan_watchlist against test_live_submit's fakes, with the
    option client given; returns ScanResult."""
    tmp = Path(tempfile.mkdtemp(prefix="test_clock_scan."))
    wl = tmp / "watchlist.json"
    wl.write_text(json.dumps(tls.WATCHLIST))
    orig_wl, orig_dt = wheel_dryrun.WATCHLIST_PATH, wheel_dryrun.datetime
    try:
        wheel_dryrun.WATCHLIST_PATH = wl
        wheel_dryrun.datetime = tls.PinnedDT
        screener.init_throttle(tls.POLICY["runtime"]["rate_limit_per_min"])
        with contextlib.redirect_stdout(io.StringIO()):
            return wheel_dryrun.scan_watchlist(
                tls.FakeTrading(), tls.FakeStock(), option_client, tls.POLICY,
                tls.NOW_ET, tls.RUN_DATE,
                clock=types.SimpleNamespace(timestamp=tls.PINNED_NOW, is_open=True))
    finally:
        wheel_dryrun.WATCHLIST_PATH, wheel_dryrun.datetime = orig_wl, orig_dt


class NoQuoteOption:
    def get_option_chain(self, req):
        return {tls.SYM: OptionsSnapshot(tls.SYM, {})}


def _strip(row):
    return {k: v for k, v in row.items() if k not in ("quote_ts", "quote_ask")}


def part_q():
    # Q1 quote_fields, literal and None.
    q_ts = datetime.datetime(2026, 11, 4, 15, 29, 30, tzinfo=UTC)
    full = types.SimpleNamespace(latest_quote=types.SimpleNamespace(
        bid_price=0.16, ask_price=0.2, timestamp=q_ts))
    check(gates.quote_fields(full) == ("2026-11-04T15:29:30+00:00", 0.2),
          f"Q1 full {gates.quote_fields(full)!r}")
    bare = types.SimpleNamespace(latest_quote=types.SimpleNamespace(
        bid_price=0.16, ask_price=None))
    for label, snap in (("None", None), ("no quote", types.SimpleNamespace(latest_quote=None)),
                        ("no ts, ask None", bare)):
        check(gates.quote_fields(snap) == (None, None),
              f"Q1 {label} {gates.quote_fields(snap)!r}")

    # Q2 the real CSP producer: literal quote ts / ask, gate outcome unchanged.
    res = _csp_scan(tls.FakeOption())
    rows = res.all_rows
    check(len(rows) == 1 and len(res.passing_rows) == 1,
          f"Q2 rows {len(rows)} passing {len(res.passing_rows)}")
    if rows:
        r = rows[0]
        check(r.get("quote_ts") == "2026-11-04T15:29:00+00:00" and r.get("quote_ask") == 0.2,
              f"Q2 CSP quote_ts / quote_ask {r.get('quote_ts')!r} {r.get('quote_ask')!r}")
        check(r["final_pass"] is True and r["rejected_by"] == "",
              f"Q2 CSP gate outcome changed {r['final_pass']!r} {r['rejected_by']!r}")
    # Q3 quote absent: None on the row; the gate outcome is the existing one.
    res = _csp_scan(NoQuoteOption())
    rows = res.all_rows
    check(len(rows) == 1 and "quote_ts" in rows[0] and "quote_ask" in rows[0]
          and rows[0]["quote_ts"] is None and rows[0]["quote_ask"] is None,
          f"Q3 CSP null quote {rows!r}")
    check(rows and rows[0]["rejected_by"].startswith("null_quote")
          and rows[0]["final_pass"] is False, f"Q3 CSP outcome {rows and rows[0]['rejected_by']!r}")

    # Q4 the real CC producer: ts-bearing quotes vs the suite's ts-less ones.
    screener.init_throttle(tcg.POLICY["runtime"]["rate_limit_per_min"])
    contracts, chain = tcg._chain_fixture()
    cq_ts = datetime.datetime(2026, 1, 15, 14, 59, 30, tzinfo=UTC)
    stamped = {sym: types.SimpleNamespace(latest_quote=types.SimpleNamespace(
        bid_price=s.latest_quote.bid_price, ask_price=s.latest_quote.ask_price,
        timestamp=cq_ts)) for sym, s in chain.items()}
    with tempfile.TemporaryDirectory() as tmp:
        base, *_ = tcg._scan(tmp, [tcg._wl_entry("NOK")])
        res, *_ = tcg._scan(tmp, [tcg._wl_entry("NOK")], chain=stamped)
        nulls = dict(chain, **{contracts[0].symbol: tcg.FakeSnap(quote=False)})
        nres, *_ = tcg._scan(tmp, [tcg._wl_entry("NOK")], chain=nulls)
    asks = {r["contract_symbol"]: (r.get("quote_ts"), r.get("quote_ask")) for r in res.all_rows}
    check(asks == {"NOK260212C00028000": ("2026-01-15T14:59:30+00:00", 0.55),
                   "NOK260212C00029000": ("2026-01-15T14:59:30+00:00", 0.45),
                   "NOK260212C00030000": ("2026-01-15T14:59:30+00:00", 0.35),
                   "NOK260212C00027000": ("2026-01-15T14:59:30+00:00", 0.65)},
          f"Q4 CC quote fields {asks!r}")
    check([_strip(r) for r in res.all_rows] == [_strip(r) for r in base.all_rows]
          and [r["contract_symbol"] for r in res.candidates]
          == [r["contract_symbol"] for r in base.candidates],
          "Q4 CC gate rows / ranking changed by the quote timestamp")
    check(all(r.get("quote_ts") is None and r.get("quote_ask") is not None
              for r in base.all_rows), "Q4 CC ts-less quote: quote_ts not None")
    nrow = [r for r in nres.all_rows if r["contract_symbol"] == contracts[0].symbol]
    check(len(nrow) == 1 and nrow[0]["quote_ts"] is None and nrow[0]["quote_ask"] is None
          and nrow[0]["rejected_by"].startswith("null_quote"),
          f"Q4 CC null quote {nrow!r}")

    # Q5 the journalled gate rows carry them (literal, then None).
    flat, fin = trs._state(fs.FLAT), trs._fin()
    for label, row, tail in (
            ("Q5 CSP", CSP_ROW, f" quote_ts={QUOTE_TS} quote_ask=0.21"),
            ("Q5 CSP null", dict(CSP_ROW, quote_ts=None, quote_ask=None),
             " quote_ts=None quote_ask=None")):
        o = _drive(label, flat, fin, clock_ts=CLOCK,
                   scan=lambda *a, row=row, **k: trs._scan_result([], [row]))
        d = [x["detail"] for x in o["decisions"] if x["action"] == "propose_csp"]
        check(len(d) == 1 and d[0].endswith("market_open=False" + tail),
              f"{label} journalled detail {d!r}")
    hold, hfin = trs._state(fs.HOLDING, None, None), trs._fin([trs._pos("F", 100)])
    for label, row, tail in (
            ("Q5 CC", CC_ROW, f" quote_ts={QUOTE_TS} quote_ask=0.3"),
            ("Q5 CC null", dict(CC_ROW, quote_ts=None, quote_ask=None),
             " quote_ts=None quote_ask=None")):
        o = _drive(label, hold, hfin, clock_ts=CLOCK,
                   cc_scan=lambda *a, row=row, **k: trs._cc_result([row]))
        d = [x["detail"] for x in o["decisions"] if x["action"] == "propose_cc"]
        check(len(d) == 1 and d[0].endswith("market_open=False" + tail),
              f"{label} journalled detail {d!r}")


# --------------------------------------------------------------------------- #
# I - isolation: host-clock calls only inside preclock_now                      #
# --------------------------------------------------------------------------- #

HOST_ATTRS = {"now", "utcnow", "today"}


def host_clock_calls(src):
    """[(enclosing function or '<module>', line)] for every host-clock call:
    X.now() / X.utcnow() / X.today() / time.time() / time.monotonic()."""
    found = []

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                attr = child.func.attr
                base = child.func.value
                if attr in HOST_ATTRS or (
                        attr in ("time", "monotonic", "time_ns")
                        and isinstance(base, ast.Name) and base.id == "time"):
                    found.append((fn, child.lineno))
            visit(child, fn)

    visit(ast.parse(src), "<module>")
    return found


def part_i():
    run_src = (Path(ROOT) / "forgetrader" / "run.py").read_text()
    sub_src = (Path(ROOT) / "forgetrader" / "submit.py").read_text()
    rc = host_clock_calls(run_src)
    check(len(rc) == 1 and rc[0][0] == "preclock_now",
          f"I1 run.py host-clock calls {rc!r} (want one, inside preclock_now)")
    sc = host_clock_calls(sub_src)
    check(sc == [], f"I2 submit.py host-clock calls {sc!r}")
    for name, src in (("run.py", run_src), ("submit.py", sub_src)):
        tree = ast.parse(src)
        mods = {a.name for x in ast.walk(tree) if isinstance(x, ast.Import) for a in x.names}
        check("time" not in mods, f"I3 {name} imports time")
    # I4 the scanner bites on planted calls.
    planted = run_src + "\n\ndef _planted():\n    return datetime.now(timezone.utc)\n"
    check(len(host_clock_calls(planted)) == len(rc) + 1,
          "I4 scanner missed a planted datetime.now")
    check(host_clock_calls("import time\nx = time.time()\n") == [("<module>", 2)],
          "I4 scanner missed a planted time.time")


def main():
    before = [sha(p) for p in REAL_FILES]
    for part in (part_k, part_p, part_q, part_i):
        try:
            part()
        except Exception as e:  # a crash is a failure, never a silent pass
            check(False, f"{part.__name__} crashed: {type(e).__name__}: {e}")
    check([sha(p) for p in REAL_FILES] == before,
          "ISOLATION: real state.json / journal.db changed")
    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_clock one clock + pre-clock + quote fields + isolation, "
          f"{n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
