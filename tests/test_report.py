"""Offline net for forgetrader/report.py (Brief D38). Never the network, never
the real clock, never a live file: every account DIR is a mkdtemp dir (refused
if under /home/forge), journal rows come from forgetrader_state's own record_*
producers with pinned ts, state from fresh_flat_state / write_state, and
alerts is a stub module in sys.modules (the real one loads .env).

Pinned as-of: MON = 2026-11-16 21:30Z = Mon 16:30 EST; its HALT window starts
Fri 2026-11-13 16:30 EST = 21:30Z, exclusive.

  S  scenarios: FLAT, CSP_OPEN NOK261211P00007000 (25 days, collateral 700,
     14.0%), HOLDING, cash absent, fill record, Monday / Tuesday HALT
     windows, HALT truncation, runs today 0/2 2/2 0/0, stamps 13/15/overdue,
     STATE MISSING / JOURNAL MISSING exit 3 (in-process and as a subprocess),
     HEAD MISMATCH across two temp git repos, naive --as-of refused.
  R  read-only: sha256 + mtime_ns of the three inputs unchanged after state
     and digest --send.
  A  alerts.send stub: once per DIR with --send, zero without; each message
     under 3500 chars with the alerts prefix.
  W  atomic write: --out complete, first line exact, no temp file left.
  I  isolation: report.py imports none of alpaca, credentials, requests,
     dotenv, and imports alerts only inside a function.
"""
import ast
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import forgetrader_state as fs  # noqa: E402
from forgetrader import report  # noqa: E402

SOURCE = Path(ROOT) / "forgetrader" / "report.py"
FORBIDDEN = Path("/home/forge")
MON = "2026-11-16T21:30:00Z"
TUE = "2026-11-17T21:30:00Z"
SAT = "2026-11-14T21:30:00Z"
CONTRACT = "NOK261211P00007000"

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
    d = Path(tempfile.mkdtemp(prefix="test_report.")) / name
    d.mkdir()
    if d.resolve() == FORBIDDEN or FORBIDDEN in d.resolve().parents:
        raise SystemExit(f"REFUSED: fixture path under {FORBIDDEN}: {d}")
    return d


def entry(ticker, review_by):
    return {"ticker": ticker, "added": "2026-08-24", "earnings_date": "2027-01-01",
            "earnings_source": "test fixture", "earnings_stamped": "2026-11-02",
            "own_100_note": "test fixture", "review_by": review_by}


def make_acct(state=True, journal=True, wl=True, wheel=None, drop_cash=False,
              runs=(), name="acct", entries=None, d=None):
    """runs: (ts, outcome) pairs recorded through fs.record_run."""
    d = d or tmpdir(name)
    if state:
        st = fs.fresh_flat_state(equity=5000.0, cash=5000.0)
        if wheel:
            st["wheel"].update(wheel)
        if drop_cash:
            del st["cash"]
        fs.write_state(st, d / "state.json")
    if journal:
        conn = fs.journal_connect(d / "journal.db")
        for ts, outcome in runs:
            fs.record_run(conn, fs.new_run_id(), outcome, ts=ts)
        conn.close()
    if wl:
        ents = entries if entries is not None else [entry("NOK", "2026-12-31")]
        (d / "watchlist.json").write_text(json.dumps({"version": 2, "entries": ents}))
    return d


class Sender:
    def __init__(self):
        self.calls = []

    def send(self, text, transport=None):
        self.calls.append(text)
        return True


def run_main(argv, sender=None):
    sender = sender or Sender()
    stub = types.ModuleType("alerts")
    stub.send = sender.send
    stub.PREFIX = "[forgetrader] "
    saved = sys.modules.get("alerts")
    sys.modules["alerts"] = stub
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = report.main(argv)
            except SystemExit as e:
                rc = e.code
    finally:
        if saved is None:
            sys.modules.pop("alerts", None)
        else:
            sys.modules["alerts"] = saved
    return rc, out.getvalue(), err.getvalue(), sender


def digest(d, as_of=MON, extra=()):
    return run_main(["digest", "--as-of", as_of, *extra, str(d)])


@scenario
def s_flat():
    d = make_acct()
    rc, out, _, s = digest(d)
    check(rc == 0, f"FLAT rc {rc}")
    check("stage FLAT  ticker None  contract None" in out, "FLAT stage line")
    check("days to expiry: n/a (no contract)" in out, "FLAT dte n/a")
    check("committed collateral: 0\n" in out, "FLAT collateral 0")
    check("capital utilisation: 0.0% (0 / cash 5000)" in out, "FLAT util")
    check("fill record: 0 broker-acknowledged submits, 0 fills" in out, "FLAT fill")
    check(out.startswith("DIGEST acct as of 2026-11-16T21:30:00Z\naccount acct  HEAD unknown"),
          f"FLAT header {out[:80]!r}")
    check("HEAD MISMATCH" not in out, "FLAT single DIR no mismatch")
    check(s.calls == [], "FLAT no send without --send")


@scenario
def s_csp_open():
    d = make_acct(wheel={"stage": fs.CSP_OPEN, "ticker": "NOK", "contract_symbol": CONTRACT})
    rc, out, _, _ = digest(d)
    check(rc == 0, f"CSP_OPEN rc {rc}")
    check(f"stage CSP_OPEN  ticker NOK  contract {CONTRACT}" in out, "CSP_OPEN stage line")
    check("days to expiry: 25 (expiry 2026-12-11)" in out, "CSP_OPEN dte 25")
    check("committed collateral: 700\n" in out, "CSP_OPEN collateral 700")
    check("capital utilisation: 14.0% (700 / cash 5000)" in out, "CSP_OPEN util 14.0%")
    d2 = make_acct(wheel={"stage": fs.CSP_PENDING, "ticker": "NOK", "contract_symbol": CONTRACT})
    _, out2, _, _ = digest(d2)
    check("committed collateral: 700\n" in out2, "CSP_PENDING collateral 700")


@scenario
def s_holding():
    d = make_acct(wheel={"stage": fs.HOLDING, "ticker": "NOK", "cost_basis": 7.0})
    rc, out, _, _ = digest(d)
    check(rc == 0, f"HOLDING rc {rc}")
    check("stage HOLDING  ticker NOK  contract None" in out, "HOLDING stage line")
    check("committed collateral: 0\n" in out, "HOLDING collateral 0")
    check("days to expiry: n/a (no contract)" in out, "HOLDING dte n/a")


@scenario
def s_cash_absent():
    d = make_acct(drop_cash=True,
                  wheel={"stage": fs.CSP_OPEN, "ticker": "NOK", "contract_symbol": CONTRACT})
    rc, out, _, _ = digest(d)
    check(rc == 0, f"cash absent rc {rc}")
    check("capital utilisation: n/a (cash not in state)" in out, "cash absent n/a")
    check("committed collateral: 700\n" in out, "cash absent collateral still 700")


@scenario
def s_fill_record():
    d = make_acct()
    conn = fs.journal_connect(d / "journal.db")
    rid = fs.new_run_id()
    fs.record_order(conn, rid, CONTRACT, "sell", 1, 0.16, status=None,
                    client_order_id="c1", broker_order_id=None, ts="2026-11-16T15:30:00+00:00")
    fs.record_order(conn, rid, CONTRACT, "sell", 1, 0.16, status="accepted",
                    client_order_id="c1", broker_order_id="b1", ts="2026-11-16T15:30:01+00:00")
    fs.record_decision(conn, rid, fs.CSP_PENDING, fs.CSP_OPEN,
                       "CSP_PENDING->CSP_OPEN (fill)", ts="2026-11-16T18:00:00+00:00")
    conn.close()
    _, out, _, _ = digest(d)
    check("fill record: 1 broker-acknowledged submits, 1 fills (lifetime, journalled)" in out,
          "fill record 1/1")


@scenario
def s_monday_window():
    d = make_acct(runs=[
        ("2026-11-13T21:29:00+00:00", "live:HALT:early"),       # Fri 16:29 ET, out
        ("2026-11-13T21:30:00+00:00", "live:HALT:boundary"),    # Fri 16:30:00 ET, out
        ("2026-11-13T21:31:00+00:00", "live:HALT:in_fri"),      # Fri 16:31 ET, in
        ("2026-11-14T15:00:00+00:00", "live:HALT:in_sat"),      # Sat, in
        ("2026-11-16T21:30:00+00:00", "live:HALT:at_asof"),     # = as-of, in
        ("2026-11-16T21:31:00+00:00", "live:HALT:after_asof"),  # after as-of, out
        ("2026-11-16T15:30:00+00:00", "live:noop:FLAT stable"),
    ])
    rc, out, _, _ = digest(d)
    check(rc == 0, f"Monday rc {rc}")
    check("HALT rows since 2026-11-13 16:30 EST: 3 (lifetime 6)" in out, "Monday count 3/6")
    check("live:HALT:in_fri" in out, "Monday includes Fri 16:31")
    check("live:HALT:in_sat" in out and "live:HALT:at_asof" in out, "Monday includes Sat + as-of")
    check("live:HALT:boundary" not in out, "Monday excludes Fri 16:30:00")
    check("live:HALT:early" not in out, "Monday excludes Fri 16:29")
    check("live:HALT:after_asof" not in out, "Monday excludes after as-of")


@scenario
def s_tuesday_window():
    d = make_acct(runs=[
        ("2026-11-13T21:31:00+00:00", "live:HALT:fri"),       # out on Tuesday
        ("2026-11-16T21:30:00+00:00", "live:HALT:mon_1630"),  # Mon 16:30:00 ET, out
        ("2026-11-16T21:31:00+00:00", "live:HALT:mon_1631"),  # in
        ("2026-11-17T15:30:00+00:00", "live:HALT:tue"),       # in
    ])
    _, out, _, _ = digest(d, TUE)
    check("HALT rows since 2026-11-16 16:30 EST: 2 (lifetime 4)" in out, "Tuesday count 2/4")
    check("live:HALT:fri" not in out and "live:HALT:mon_1630" not in out, "Tuesday excludes")
    check("live:HALT:mon_1631" in out and "live:HALT:tue" in out, "Tuesday includes")


@scenario
def s_truncate():
    runs = [(f"2026-11-16T15:{m:02d}:00+00:00", f"live:HALT:row{m:02d}") for m in range(13)]
    d = make_acct(runs=runs)
    _, out, _, _ = digest(d)
    check("HALT rows since 2026-11-13 16:30 EST: 13 (lifetime 13)" in out, "truncate count")
    check("live:HALT:row09" in out and "live:HALT:row10" not in out, "truncate shows first 10")
    check("  +3 more" in out, "truncate +3 more")


@scenario
def s_runs_today():
    d0 = make_acct(runs=[("2026-11-13T15:30:00+00:00", "dryrun:noop:FLAT stable")])
    _, out0, _, _ = digest(d0)
    check("runs today (2026-11-16 ET): 0/2" in out0, "runs 0/2")
    d2 = make_acct(runs=[("2026-11-16T15:30:00+00:00", "live:noop:FLAT stable"),
                         ("2026-11-16T18:00:00+00:00", "live:noop:FLAT stable"),
                         ("2026-11-16T04:59:00+00:00", "live:noop:FLAT stable")])  # Sun ET
    _, out2, _, _ = digest(d2)
    check("runs today (2026-11-16 ET): 2/2" in out2, "runs 2/2 (Sun 23:59 ET excluded)")
    _, outw, _, _ = digest(d0, SAT)
    check("runs today (2026-11-14 ET): 0/0" in outw, "runs weekend 0/0")


@scenario
def s_stamps():
    d = make_acct(entries=[entry("AAA", "2026-11-29"), entry("BBB", "2026-12-01"),
                           entry("CCC", "2026-11-10"), entry("DDD", "2026-11-30")])
    _, out, _, _ = digest(d)
    check("  AAA review_by 2026-11-29 13d" in out, "stamp 13 days shown")
    check("BBB" not in out, "stamp 15 days not shown")
    check("  CCC review_by 2026-11-10 OVERDUE 6d" in out, "stamp overdue")
    check("  DDD review_by 2026-11-30 14d" in out, "stamp 14 days shown")
    d2 = make_acct(entries=[entry("BBB", "2026-12-01")])
    _, out2, _, _ = digest(d2)
    check("stamps lapsing within 14d: none" in out2, "stamps none")


@scenario
def s_state_missing():
    d = make_acct(state=False)
    rc, out, err, s = digest(d, extra=["--send"])
    check(rc == 3, f"state missing rc {rc}")
    check("STATE MISSING (state.json not found)" in out, "STATE MISSING line")
    check(len(s.calls) == 1 and "STATE MISSING" in s.calls[0], "state missing still sent")
    check("runs today" in out, "state missing journal still reported")
    r = subprocess.run([sys.executable, "-m", "forgetrader.report", "digest",
                        "--as-of", MON, str(d)], cwd=ROOT, capture_output=True,
                       text=True, timeout=60)
    check(r.returncode == 3, f"subprocess state missing rc {r.returncode}")
    check("STATE MISSING" in r.stdout, "subprocess STATE MISSING")


@scenario
def s_journal_missing():
    d = make_acct(journal=False)
    rc, out, _, s = digest(d, extra=["--send"])
    check(rc == 3, f"journal missing rc {rc}")
    check("JOURNAL MISSING" in out, "JOURNAL MISSING line")
    check(not (d / "journal.db").exists(), "mode=ro never created journal.db")
    check(len(s.calls) == 1, "journal missing still sent")
    out_p = tmpdir("out") / "PROJECT_STATE.md"
    rc2, _, _, _ = run_main(["state", "--out", str(out_p), "--as-of", MON, str(d)])
    check(rc2 == 3 and "JOURNAL MISSING" in out_p.read_text(), "state cmd journal missing rc 3")


@scenario
def s_head_mismatch():
    dirs = []
    for name in ("acct_a", "acct_b"):
        d = make_acct(name=name)
        git = ["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", name], check=True)
        dirs.append(d)
    heads = [subprocess.run(["git", "-C", str(d), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip() for d in dirs]
    rc, out, _, s = run_main(["digest", "--send", "--as-of", MON, *map(str, dirs)])
    check(rc == 0, f"mismatch rc {rc}")
    check(f"account acct_a  HEAD {heads[0]}" in out, "acct_a head")
    check(f"account acct_b  HEAD {heads[1]}" in out, "acct_b head")
    check(out.count("HEAD MISMATCH") == 2, "HEAD MISMATCH in each message")
    check(len(s.calls) == 2, f"send once per DIR, got {len(s.calls)}")
    _, out1, _, _ = digest(dirs[0])
    check("HEAD MISMATCH" not in out1, "single repo no mismatch")


@scenario
def s_naive_as_of():
    d = make_acct()
    rc, _, err, _ = digest(d, "2026-11-16T21:30:00")
    check(rc == 2 and "tz-aware" in err, f"naive as-of refused rc {rc}")


def fingerprint(d):
    out = {}
    for f in ("state.json", "journal.db", "watchlist.json"):
        p = d / f
        out[f] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


@scenario
def s_read_only_and_send():
    d = make_acct(wheel={"stage": fs.CSP_OPEN, "ticker": "NOK", "contract_symbol": CONTRACT},
                  runs=[("2026-11-16T15:30:00+00:00", "live:HALT:x" + "y" * 500)])
    before = fingerprint(d)
    out_p = tmpdir("out") / "PROJECT_STATE.md"
    rc1, _, _, _ = run_main(["state", "--out", str(out_p), "--as-of", MON, str(d)])
    rc2, out, _, s = digest(d, extra=["--send"])
    check(rc1 == 0 and rc2 == 0, f"read-only rcs {rc1} {rc2}")
    check(fingerprint(d) == before, "inputs sha256 + mtime unchanged")
    check(len(s.calls) == 1, "send exactly once for one DIR")
    check(all(len("[forgetrader] " + t) < 3500 for t in s.calls), "message under 3500")
    _, _, _, s0 = digest(d)
    check(s0.calls == [], "zero sends without --send")
    text = out_p.read_text()
    check(text.splitlines()[0] ==
          "# PROJECT_STATE (GENERATED 2026-11-16T21:30:00Z) - do not edit", "state first line")
    check("last 5 runs:" in text and text.endswith("\n"), "state complete with last runs")
    check(sorted(p.name for p in out_p.parent.iterdir()) == ["PROJECT_STATE.md"],
          "no temp file left")


@scenario
def s_digest_cap():
    runs = [(f"2026-11-16T15:{m:02d}:00+00:00", "live:HALT:" + "z" * 400) for m in range(10)]
    ents = [entry("A" + chr(65 + i) + chr(65 + j), "2026-11-20") for i in range(5) for j in range(26)]
    d = make_acct(runs=runs, entries=ents)
    _, _, _, s = digest(d, extra=["--send"])
    check(len(s.calls) == 1 and len("[forgetrader] " + s.calls[0]) < 3500,
          f"capped message length {len(s.calls[0]) if s.calls else None}")
    check(s.calls and s.calls[0].endswith("[truncated]"), "capped message marked")


@scenario
def s_isolation():
    tree = ast.parse(SOURCE.read_text())
    top, nested = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""])
            (top if node in tree.body else nested).update(n.split(".")[0] for n in names)
    bad = (top | nested) & {"alpaca", "credentials", "requests", "dotenv"}
    check(not bad, f"forbidden imports {bad}")
    check("alerts" not in top and "alerts" in nested, "alerts only imported in a function")
    check(SOURCE.read_text().count("datetime.now(") == 1, "one clock read")


def main():
    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_report {scenarios} scenarios, {n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
