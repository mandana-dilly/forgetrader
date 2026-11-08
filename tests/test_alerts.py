"""Offline net for alerts.py and its wiring into run.py / probe.py (Brief 4).

Never the network: every send goes through a fake transport. The token and
chat id come from a temp .env file that alerts.ENV_PATH is pointed at, so the
real .env loading path (load_dotenv on ENV_PATH) is exercised with a fake
token; TELEGRAM_* environment variables are removed for the duration and
restored after.

  U  alerts.send unit: URL / body / timeout / prefix / no parse_mode; missing
     token or chat id -> one WARNING, no send; transport exception -> one
     WARNING naming the TYPE only, never raised, token never printed; non-200
     -> one WARNING; ENV_PATH is credentials' .env path; no getUpdates.
  R  run.run through test_run_stages._drive with the real send + fake
     transport. The transport records, AT SEND TIME, last_run.outcome on disk,
     the journal runs rows and the HALT dump count, so the ordering (state
     written and run row journalled before the alert; HALT dump before its
     alert) is asserted on every send. Exactly one message per run.
     R1 summary  R2 top_rejects  R3 HALT snapshot  R4 HALT state_missing
     R5 HALT submit  R6 no token  R7 transport raises  R8 raises on a HALT
     R9 refused:not_paper
  P  probe.main HALT through test_probe._drive: one alert after probe:halt is
     journalled; a raising transport leaves exit 4 and the journal unchanged.
The run/probe drivers' own per-scenario checks are collected into a sink and
asserted clean once per R scenario. A guard hashes the real state.json /
journal.db before vs after.
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import urllib.parse
from collections import Counter
from pathlib import Path

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import alerts
import credentials
import forgetrader_state as fs
import test_probe as tp
import test_run_stages as trs

REAL_FILES = [Path(ROOT) / "state.json", Path(ROOT) / "journal.db"]
FAKE_TOKEN = "123456789:FAKE-test_alerts-token-NOT-REAL"
CHAT = "4242"
URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
ENV_KEYS = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
REAL_SEND = alerts.send

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


def sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


@contextlib.contextmanager
def env(token=FAKE_TOKEN, chat=CHAT):
    """Temp .env with the given values (None = line absent) as alerts.ENV_PATH;
    TELEGRAM_* removed from os.environ during and restored after."""
    saved = {k: os.environ.pop(k, None) for k in ENV_KEYS}
    d = Path(tempfile.mkdtemp(prefix="test_alerts_env."))
    lines = []
    if token is not None:
        lines.append(f"TELEGRAM_BOT_TOKEN={token}")
    if chat is not None:
        lines.append(f"TELEGRAM_CHAT_ID={chat}")
    (d / ".env").write_text("\n".join(lines) + "\n")
    orig = alerts.ENV_PATH
    alerts.ENV_PATH = d / ".env"
    try:
        yield
    finally:
        alerts.ENV_PATH = orig
        for k in ENV_KEYS:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def transport(box, raises=None, status=200):
    """Fake transport: records the call and what is on disk at send time."""
    def tr(url, body, timeout):
        snap = {"url": url, "body": body, "timeout": timeout,
                "fields": urllib.parse.parse_qs(body.decode("utf-8"))}
        st, jp, sd = Path(fs.STATE_PATH), Path(fs.JOURNAL_PATH), Path(fs.SCANS_DIR)
        snap["disk_outcome"] = (json.loads(st.read_text())["last_run"]["outcome"]
                                if st.exists() else None)
        snap["runs"] = []
        if jp.exists():
            c = sqlite3.connect(str(jp))
            snap["runs"] = [r[0] for r in c.execute("SELECT outcome FROM runs")]
            c.close()
        snap["dumps"] = len(list(sd.glob("state_halt_*.txt"))) if sd.exists() else 0
        snap["text"] = snap["fields"].get("text", [None])[0]
        box.append(snap)
        if raises is not None:
            raise raises
        return status
    return tr


def unit(tr):
    """Call the real send; returns (ret, escaped_exception, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    ret, exc = None, None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            ret = REAL_SEND("hello", transport=tr)
        except Exception as e:
            exc = type(e).__name__
    return ret, exc, out.getvalue(), err.getvalue()


def part_u():
    box = []
    with env():
        ret, exc, out, err = unit(transport(box))
    check(ret is True and exc is None, f"U1 send returned {ret!r} exc {exc!r}")
    check(len(box) == 1, f"U1 transport calls {len(box)}")
    check(bool(box) and box[0]["url"] == URL, "U1 url is not bot<token>/sendMessage")
    check(bool(box) and box[0]["fields"] == {"chat_id": [CHAT],
                                             "text": ["[forgetrader] hello"]},
          f"U1 body fields {box and box[0]['fields']!r}")
    check(bool(box) and box[0]["timeout"] == 10, "U1 timeout is not 10")
    check(bool(box) and FAKE_TOKEN.encode() not in box[0]["body"], "U1 token in body")
    check(out == "" and err == "", f"U1 printed {out!r} {err!r}")

    for name, kw in (("U2", {"token": None}), ("U3", {"chat": None})):
        box = []
        with env(**kw):
            ret, exc, out, err = unit(transport(box))
        check(ret is False and exc is None, f"{name} send returned {ret!r} exc {exc!r}")
        check(box == [], f"{name} transport called without config")
        lines = err.splitlines()
        check(len(lines) == 1 and lines[0].startswith("WARNING: alert skipped"),
              f"{name} stderr {err!r}")

    box = []
    with env():
        ret, exc, out, err = unit(transport(box, raises=ValueError(f"bad url {URL}")))
    check(ret is False and exc is None, f"U4 send returned {ret!r} escaped {exc!r}")
    check(err == "WARNING: alert not sent: ValueError\n", "U4 WARNING is not type-only")
    check(FAKE_TOKEN not in out + err, "U4 token printed")

    box = []
    with env():
        ret, exc, out, err = unit(transport(box, status=403))
    check(ret is False and exc is None, f"U5 send returned {ret!r} exc {exc!r}")
    check(err == "WARNING: alert not sent: non-200 reply\n", "U5 non-200 WARNING")

    check(alerts.ENV_PATH == credentials._ENV_PATH,
          "U6 alerts.ENV_PATH is not credentials' .env path")
    check("getUpdates" not in Path(alerts.__file__).read_text(),
          "U7 alerts.py mentions getUpdates")


def drive_run(name, seed, fin, box, raises=None, **kw):
    """test_run_stages._drive with the real send + fake transport.
    Returns (obs or None if run raised, sink of the driver's own checks)."""
    sink = trs.T()
    tr = transport(box, raises=raises)
    try:
        o = trs._drive(sink, name, seed, fin,
                       send=lambda text: REAL_SEND(text, transport=tr), **kw)
    except Exception:
        o = None
    return o, sink


def journal_text(o):
    return json.dumps([o["runs"], o["decisions"], o["events"], o["orders"]])


def part_r():
    hold = trs._state(fs.HOLDING, None, None)
    hold_fin = trs._fin([trs._pos("F", 100)])
    flat_with_shares = trs._fin([trs._pos("F", 100)])
    ok_out = "dryrun:noop:HOLDING stable"

    # R1 summary after state write + run row, one message
    box = []
    with env():
        o, sink = drive_run("R1", hold, hold_fin, box)
    check(o is not None and o["ret"] == 0, "R1 exit not 0")
    check(len(box) == 1, f"R1 sends {len(box)} != 1")
    check(bool(box) and all(b["text"] == f"[forgetrader] {ok_out} stage=HOLDING ticker=F"
                            for b in box), f"R1 texts {[b['text'] for b in box]!r}")
    check(bool(box) and all(b["disk_outcome"] == ok_out for b in box),
          f"R1 state not written before send: {[b['disk_outcome'] for b in box]!r}")
    check(bool(box) and all(b["runs"] == [ok_out] for b in box),
          f"R1 run row not journalled before send: {[b['runs'] for b in box]!r}")
    check(o is not None and FAKE_TOKEN not in (o["stdout"] + o["stderr"] + journal_text(o)
                                               + json.dumps(o["state"])),
          "R1 token in stdout / stderr / journal / state")
    check(sink.failures == [], f"R1 driver checks {sink.failures!r}")

    # R2 all_rejected carries top_rejects
    def scan(*a, **k):
        return types.SimpleNamespace(
            passing_rows=[], approved=[], wl_rejected=[],
            reject_tally=Counter({"bid_below_min": 3, "dte": 1}),
            clock=types.SimpleNamespace(is_open=False))
    box = []
    with env():
        o, sink = drive_run("R2", trs._state(fs.FLAT), trs._fin(), box, scan=scan)
    check(o is not None and o["ret"] == 0, "R2 exit not 0")
    check(len(box) == 1, f"R2 sends {len(box)} != 1")
    want = ("[forgetrader] dryrun:noop:FLAT stable stage=FLAT ticker=None "
            "top_rejects=[bid_below_min=3, dte=1]")
    check(bool(box) and all(b["text"] == want for b in box),
          f"R2 texts {[b['text'] for b in box]!r}")
    check(sink.failures == [], f"R2 driver checks {sink.failures!r}")

    # R3 HALT snapshot: the HALT outcome, after dump + journal
    box = []
    with env():
        o, sink = drive_run("R3", trs._state(fs.FLAT), flat_with_shares, box)
    check(o is not None and o["ret"] == 4, "R3 exit not 4")
    check(len(box) == 1, f"R3 sends {len(box)} != 1")
    check(bool(box) and all(
        b["text"].startswith("[forgetrader] dryrun:HALT:snapshot unexpected position F")
        and b["text"].endswith(" stage=FLAT ticker=None") for b in box),
        f"R3 texts {[b['text'] for b in box]!r}")
    check(bool(box) and all(len(b["runs"]) == 1 and b["runs"] == [b["disk_outcome"]]
                            and b["runs"][0].startswith("dryrun:HALT:snapshot")
                            for b in box),
          f"R3 not journalled/written before send {[(b['runs'], b['disk_outcome']) for b in box]!r}")
    check(bool(box) and all(b["dumps"] == 1 for b in box), "R3 dump not written before send")
    check(sink.failures == [], f"R3 driver checks {sink.failures!r}")

    # R4 HALT state_missing (no state to write; run row first)
    box = []
    with env():
        o, sink = drive_run("R4", None, trs._fin(), box, expect_clock=0)
    check(o is not None and o["ret"] == 4, "R4 exit not 4")
    check(len(box) == 1, f"R4 sends {len(box)} != 1")
    check(bool(box) and all(b["text"] == "[forgetrader] dryrun:HALT:state_missing "
                            "stage=None ticker=None" for b in box),
          f"R4 texts {[b['text'] for b in box]!r}")
    check(bool(box) and all(b["runs"] == ["dryrun:HALT:state_missing"] for b in box),
          "R4 run row not journalled before send")
    check(sink.failures == [], f"R4 driver checks {sink.failures!r}")

    # R5 HALT:submit (live, submit_order raises after the write-ahead)
    live = dict(live=True, paper=True, is_open=True, clock_ts=trs.CLOCK_OPEN,
                scan=lambda *a, **k: trs._scan_result([], [trs.LIVE_ROW]))
    box = []
    with env():
        o, sink = drive_run("R5", trs._state(fs.FLAT), trs._fin(), box,
                            submit_raises=RuntimeError("wire down"), **live)
    check(o is not None and o["ret"] == 4, "R5 exit not 4")
    check(len(box) == 1, f"R5 sends {len(box)} != 1")
    check(bool(box) and all(b["text"] == "[forgetrader] live:HALT:submit RuntimeError: "
                            "wire down stage=CSP_PENDING ticker=NOK" for b in box),
          f"R5 texts {[b['text'] for b in box]!r}")
    check(bool(box) and all(len(b["runs"]) == 1 and b["runs"] == [b["disk_outcome"]]
                            for b in box), "R5 not journalled/written before send")
    check(sink.failures == [], f"R5 driver checks {sink.failures!r}")

    # R6 no token: WARNING, no send, run unchanged
    box = []
    with env(token=None):
        o, sink = drive_run("R6", hold, hold_fin, box)
    check(o is not None and o["ret"] == 0, "R6 exit not 0")
    check(box == [], "R6 transport called without a token")
    check(o is not None and o["stderr"].count("WARNING: alert skipped") == 1,
          f"R6 stderr {o and o['stderr']!r}")
    check(o is not None and o["runs"] == [{"outcome": ok_out}]
          and o["state"]["last_run"]["outcome"] == ok_out, "R6 journal / state changed")

    # R7 transport raises: swallowed, type-only WARNING, run unchanged
    box = []
    with env():
        o, sink = drive_run("R7", hold, hold_fin, box,
                            raises=RuntimeError(f"net down {URL}"))
    check(o is not None and o["ret"] == 0, "R7 exit not 0 (alert failure leaked)")
    check(o is not None and "WARNING: alert not sent: RuntimeError\n" in o["stderr"],
          "R7 stderr has no type-only WARNING")
    check(o is not None and FAKE_TOKEN not in o["stdout"] + o["stderr"],
          "R7 token printed")
    check(o is not None and o["runs"] == [{"outcome": ok_out}]
          and o["state"]["last_run"]["outcome"] == ok_out, "R7 journal / state changed")

    # R8 transport raises on a HALT: exit 4 kept, journal unchanged
    box = []
    with env():
        o, sink = drive_run("R8", trs._state(fs.FLAT), flat_with_shares, box,
                            raises=RuntimeError(f"net down {URL}"))
    check(o is not None and o["ret"] == 4, "R8 exit not 4 (alert failure leaked)")
    check(o is not None and "WARNING: alert not sent: RuntimeError\n" in o["stderr"],
          "R8 stderr has no type-only WARNING")
    check(o is not None and len(o["runs"]) == 1
          and o["runs"][0]["outcome"].startswith("dryrun:HALT:snapshot")
          and o["state"]["last_run"]["outcome"] == o["runs"][0]["outcome"],
          "R8 journal / state changed")
    check(o is not None and FAKE_TOKEN not in o["stdout"] + o["stderr"], "R8 token printed")

    # R9 refused:not_paper sends its outcome after its run row
    box = []
    with env():
        o, sink = drive_run("R9", trs._state(fs.FLAT), trs._fin(), box,
                            expect_clock=0, **dict(live, paper=False))
    check(o is not None and o["ret"] == 3, "R9 exit not 3")
    check(len(box) == 1 and box[0]["text"] == "[forgetrader] live:refused:not_paper "
          "stage=FLAT ticker=None", f"R9 texts {[b['text'] for b in box]!r}")
    check(bool(box) and all(b["runs"] == ["live:refused:not_paper"] for b in box),
          "R9 run row not journalled before send")


def part_p():
    box = []
    tr = transport(box)
    with env():
        o = tp._drive(["--submit"], raise_submit=RuntimeError("boom"),
                      send=lambda text: REAL_SEND(text, transport=tr))
    check(o["exc"] is None and o["ret"] == 4, f"P1 ret {o['ret']!r} exc {o['exc']!r}")
    check(len(box) == 1, f"P1 sends {len(box)} != 1")
    check(bool(box) and all(b["text"].startswith(
        "[forgetrader] probe:halt RuntimeError: boom coid=") for b in box),
        f"P1 texts {[b['text'] for b in box]!r}")
    check(bool(box) and all(b["runs"] == ["probe:halt"] for b in box),
          "P1 probe:halt not journalled before send")

    box = []
    tr = transport(box, raises=RuntimeError(f"net down {URL}"))
    with env():
        o = tp._drive(["--submit"], raise_submit=RuntimeError("boom"),
                      send=lambda text: REAL_SEND(text, transport=tr))
    check(o["exc"] is None and o["ret"] == 4,
          f"P2 alert failure changed the probe exit: ret {o['ret']!r} exc {o['exc']!r}")
    check(o["runs"] == ["probe:halt"], f"P2 runs {o['runs']!r}")


def main():
    before = [sha(p) for p in REAL_FILES]
    for part in (part_u, part_r, part_p):
        try:
            part()
        except Exception as e:  # a crash is reported, never a silent pass
            check(False, f"{part.__name__} crashed: {type(e).__name__}")
    after = [sha(p) for p in REAL_FILES]
    print(f"isolation state.json/journal.db sha256 before={before} after={after}")
    check(before == after, "ISOLATION: real state.json / journal.db changed")
    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_alerts unit + run + probe wiring, {n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
