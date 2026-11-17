"""Offline net for forge_instance.py and its wiring (brief instance-config).

Never the network, never a real .env: every scenario points
credentials._ENV_PATH (the path forge_instance.current() loads) at a temp
.env, and FORGE_INSTANCE is set or removed in os.environ per scenario and
restored after. Importing test_run_stages / test_probe sets FORGE_INSTANCE=a
for their own suites; it is removed again right after the imports below.

  T  the table: exactly keys a and b; a = policy.toml / "[forgetrader] ",
     b = policy-b.toml / "[forgetrader-b] "; policy_path under ROOT; resolved
     at call time (a then b without re-import).
  E  missing / empty / "A" / " a" / "a " / "c" each raise InstanceError (no
     default); the message names the bad value and no other key; the .env
     path is credentials._ENV_PATH (FORGE_INSTANCE=b from a temp .env
     resolves b); an already-set environment variable wins over .env.
  P  screener.load_policy: a and b load different fixture policy files from
     a temp ROOT (compared to literals); missing instance raises.
  A  alerts.send prefix through a fake transport: a, b, explicit prefix=;
     missing instance -> one WARNING naming InstanceError, nothing sent.
  R  run.run at CSP_OPEN (put open, a stable noop) through
     test_run_stages._drive (spies on credentials, the
     three client classes and read_broker_financials; screener.load_policy
     stubbed so a misplaced instance check reaches the broker read instead of
     crashing): FORGE_INSTANCE missing or "A" -> HALT:instance, exit 4, one
     run row with the mode prefix, no decision row, alert with prefix
     "[forgetrader-?] ", credentials / clients / clock / financials never
     touched, wheel unchanged, only last_run added. R3: FORGE_INSTANCE=a
     still runs (control).
  C  CLIs: probe refuses "instance" (exit 3, no client); cc_scan.main exits 3,
     wheel_dryrun.main and screener.main exit 1, credentials never called.
A guard hashes the real state.json / journal.db before vs after.
"""
import contextlib
import copy
import hashlib
import io
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import alerts
import credentials
import forge_instance
import forgetrader_state as fs
import screener
import test_probe as tp
import test_run_stages as trs
import wheel_dryrun
from forgetrader import cc_scan

os.environ.pop("FORGE_INSTANCE", None)  # set by the imported suites above

REAL_FILES = [Path(ROOT) / "state.json", Path(ROOT) / "journal.db"]
FAKE_TOKEN = "123456789:FAKE-test_instance-token-NOT-REAL"
CHAT = "4242"
KEYS = ("FORGE_INSTANCE", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "ALPACA_API_KEY", "ALPACA_API_SECRET")
POLICY_A = '[fixture]\nname = "policy-a-fixture"\nmin_bid = 0.10\n'
POLICY_B = '[fixture]\nname = "policy-b-fixture"\nmin_bid = 0.35\n'
RUN_POLICY = {"runtime": {"rate_limit_per_min": 1000}}

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
def env(instance=None, dotenv_lines=()):
    """FORGE_INSTANCE = instance (None = absent) in os.environ; a temp .env
    holding dotenv_lines as credentials._ENV_PATH and alerts.ENV_PATH. KEYS
    are removed during and restored after."""
    saved = {k: os.environ.pop(k, None) for k in KEYS}
    d = Path(tempfile.mkdtemp(prefix="test_instance_env."))
    (d / ".env").write_text("".join(f"{x}\n" for x in dotenv_lines))
    orig = credentials._ENV_PATH, alerts.ENV_PATH
    credentials._ENV_PATH = alerts.ENV_PATH = d / ".env"
    if instance is not None:
        os.environ["FORGE_INSTANCE"] = instance
    try:
        yield
    finally:
        credentials._ENV_PATH, alerts.ENV_PATH = orig
        for k in KEYS:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def resolve():
    """(row, error message) from forge_instance.current()."""
    try:
        return forge_instance.current(), None
    except forge_instance.InstanceError as e:
        return None, str(e)


def part_t():
    t = forge_instance._TABLE
    check(sorted(t) == ["a", "b"], f"T1 table keys {sorted(t)!r}")
    check(tuple(t["a"]) == ("a", "policy.toml", "[forgetrader] "),
          f"T2 row a {tuple(t['a'])!r}")
    check(tuple(t["b"]) == ("b", "policy-b.toml", "[forgetrader-b] "),
          f"T3 row b {tuple(t['b'])!r}")
    check(t["a"].policy_path == forge_instance.ROOT / "policy.toml"
          and t["b"].policy_path == forge_instance.ROOT / "policy-b.toml",
          "T4 policy_path not ROOT / policy file")
    check(forge_instance.ROOT == Path(ROOT).resolve(),
          f"T5 ROOT {forge_instance.ROOT} is not the repo root")
    with env("a"):
        ra, _ = resolve()
        os.environ["FORGE_INSTANCE"] = "b"
        rb, _ = resolve()
    check(ra is t["a"] and rb is t["b"],
          f"T6 call-time resolution a then b gave {ra!r} {rb!r}")


def part_e():
    for name, value in (("E1 missing", None), ("E2 empty", ""), ("E3 'A'", "A"),
                        ("E4 ' a'", " a"), ("E5 'a '", "a "), ("E6 'c'", "c"),
                        ("E7 'B'", "B")):
        with env(value, dotenv_lines=("ALPACA_API_KEY=k-secret-not-real",)):
            os.environ["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
            row, msg = resolve()
        check(row is None and msg is not None, f"{name} resolved {row!r}, no error")
        if msg is None:
            continue
        check("FORGE_INSTANCE" in msg, f"{name} message {msg!r} does not name FORGE_INSTANCE")
        if value:
            check(repr(value) in msg, f"{name} message {msg!r} does not name the value")
        check(not any(k in msg for k in KEYS[1:]) and "k-secret" not in msg
              and FAKE_TOKEN not in msg, f"{name} message {msg!r} names another key")
    with env(None):
        row, msg = resolve()
    check(msg == "FORGE_INSTANCE is not set", f"E8 missing message {msg!r}")
    check(credentials._ENV_PATH == Path(ROOT).resolve() / ".env",
          f"E9 credentials._ENV_PATH {credentials._ENV_PATH} is not the repo .env")
    with env(None, dotenv_lines=("FORGE_INSTANCE=b",)):
        row, msg = resolve()
    check(row is forge_instance._TABLE["b"], f"E10 .env FORGE_INSTANCE=b gave {row!r} {msg!r}")
    with env("a", dotenv_lines=("FORGE_INSTANCE=b",)):
        row, msg = resolve()
    check(row is forge_instance._TABLE["a"], f"E11 env a over .env b gave {row!r} {msg!r}")
    with env(None, dotenv_lines=("FORGE_INSTANCE=A",)):
        row, msg = resolve()
    check(row is None and msg is not None and "'A'" in msg,
          f"E12 .env FORGE_INSTANCE=A gave {row!r} {msg!r}")


def part_p():
    d = Path(tempfile.mkdtemp(prefix="test_instance_policy."))
    (d / "policy.toml").write_text(POLICY_A)
    (d / "policy-b.toml").write_text(POLICY_B)
    orig = forge_instance.ROOT
    forge_instance.ROOT = d
    got = {}
    try:
        for inst in ("a", "b", None):
            with env(inst):
                try:
                    got[inst] = screener.load_policy()
                except forge_instance.InstanceError as e:
                    got[inst] = f"InstanceError: {e}"
    finally:
        forge_instance.ROOT = orig
    check(got["a"] == {"fixture": {"name": "policy-a-fixture", "min_bid": 0.10}},
          f"P1 instance a loaded {got['a']!r}")
    check(got["b"] == {"fixture": {"name": "policy-b-fixture", "min_bid": 0.35}},
          f"P2 instance b loaded {got['b']!r}")
    check(got[None] == "InstanceError: FORGE_INSTANCE is not set",
          f"P3 missing instance loaded {got[None]!r}")


def _send(instance, **kw):
    box = []

    def tr(url, body, timeout):
        box.append(urllib.parse.parse_qs(body.decode("utf-8"))["text"][0])
        return 200

    err = io.StringIO()
    with env(instance, dotenv_lines=(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}",
                                     f"TELEGRAM_CHAT_ID={CHAT}")):
        with contextlib.redirect_stderr(err):
            ret = alerts.send("hello", transport=tr, **kw)
    return ret, box, err.getvalue()


def part_a():
    ret, box, _ = _send("a")
    check(ret is True and box == ["[forgetrader] hello"], f"A1 instance a sent {ret!r} {box!r}")
    ret, box, _ = _send("b")
    check(ret is True and box == ["[forgetrader-b] hello"], f"A2 instance b sent {ret!r} {box!r}")
    ret, box, _ = _send(None, prefix=forge_instance.UNKNOWN_PREFIX)
    check(ret is True and box == ["[forgetrader-?] hello"],
          f"A3 explicit prefix sent {ret!r} {box!r}")
    ret, box, err = _send(None)
    check(ret is False and box == [] and err == "WARNING: alert not sent: InstanceError\n",
          f"A4 missing instance: ret {ret!r} sent {box!r} stderr {err!r}")
    check(not hasattr(alerts, "PREFIX"), "A5 alerts.PREFIX still defined")


def _run(name, instance, live=False, expect_clock=0):
    """run.run at CSP_OPEN with the put open at the broker (a stable noop,
    no scan), so a run that wrongly gets past the instance check completes
    and fails the checks below instead of tripping a driver tripwire."""
    t = trs.T()
    seed = trs._state(fs.CSP_OPEN, contract=trs.PUT)
    sent = []
    orig_policy = screener.load_policy
    screener.load_policy = lambda: copy.deepcopy(RUN_POLICY)
    try:
        with env(instance):
            o = trs._drive(t, name, copy.deepcopy(seed),
                           trs._fin(positions=[trs._pos(trs.PUT, -1)]),
                           live=live, expect_clock=expect_clock,
                           send=lambda text, prefix=None: sent.append((text, prefix)))
    finally:
        screener.load_policy = orig_policy
    for f in t.failures:
        check(False, f"{name} driver: {f}")
    return o, sent, seed


def _halt_checks(name, o, sent, seed, mode, msg):
    outcome = f"{mode}:HALT:instance {msg}"
    check(o["ret"] == 4, f"{name} exit {o['ret']!r} != 4")
    check(o["runs"] == [{"outcome": outcome}], f"{name} runs {o['runs']!r}")
    check(o["decisions"] == [], f"{name} decisions {o['decisions']!r}")
    check(o["cred_calls"] == 0, f"{name} credentials called {o['cred_calls']}x")
    check(o["clients_built"] == 0, f"{name} clients built {o['clients_built']}")
    check(o["broker_calls"] == [], f"{name} broker calls {o['broker_calls']!r}")
    check(o["fin_calls"] == [], f"{name} financials read {o['fin_calls']!r}")
    st = o["state"] or {}
    check(st.get("wheel") == seed["wheel"], f"{name} wheel changed {st.get('wheel')!r}")
    # write_state stamps generated_at on every write (every HALT alike).
    stamp = ("last_run", "generated_at")
    rest = {k: v for k, v in st.items() if k not in stamp}
    want = {k: v for k, v in seed.items() if k not in stamp}
    check(rest == want, f"{name} state changed beyond last_run")
    check(st.get("last_run", {}).get("outcome") == outcome,
          f"{name} last_run {st.get('last_run')!r}")
    check(sent == [(f"{outcome} stage={seed['wheel']['stage']} "
                    f"ticker={seed['wheel']['ticker']}", "[forgetrader-?] ")],
          f"{name} alert {sent!r}")
    check(o["alerts"] == [f"HALT:instance {msg}"], f"{name} alert_stub {o['alerts']!r}")
    check(f"HALT:instance {msg}" in o["stderr"], f"{name} stderr {o['stderr']!r}")


def part_r():
    o, sent, seed = _run("R1 missing dry", None)
    _halt_checks("R1", o, sent, seed, "dryrun", "FORGE_INSTANCE is not set")
    o, sent, seed = _run("R2 'A' live", "A", live=True)
    _halt_checks("R2", o, sent, seed, "live",
                 "FORGE_INSTANCE='A' is not one of a, b")
    # R3 control: a resolves, the run proceeds past the instance check.
    o, sent, seed = _run("R3 a dry", "a", expect_clock=1)
    check(o["ret"] == 0 and o["cred_calls"] == 1 and o["clock_calls"] == 1,
          f"R3 instance a: exit {o['ret']!r} creds {o['cred_calls']} "
          f"clock {o['clock_calls']}")
    check(sent and sent[0][1] is None, f"R3 alert prefix overridden {sent!r}")


def part_c():
    t = tp.T()
    with env(None):
        o = tp._refusal(t, "C1 probe", "instance")
    for f in t.failures:
        check(False, f)
    check(o["clients"] == 0, f"C1 probe built {o['clients']} clients")

    calls = []

    def spy(*a, **k):
        calls.append("load_credentials")
        raise AssertionError("credentials read before the instance check")

    orig = credentials.load_credentials, wheel_dryrun.load_credentials, \
        screener.load_credentials, sys.argv
    err = io.StringIO()
    rets = {}
    try:
        credentials.load_credentials = wheel_dryrun.load_credentials = \
            screener.load_credentials = spy
        with env(None), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rets["cc_scan"] = cc_scan.main(["--ticker", "F", "--basis", "12.00"])
            for name, mod in (("wheel_dryrun", wheel_dryrun), ("screener", screener)):
                sys.argv = [f"{name}.py"]
                try:
                    mod.main()
                    rets[name] = "returned"
                except SystemExit as e:
                    rets[name] = e.code
    finally:
        (credentials.load_credentials, wheel_dryrun.load_credentials,
         screener.load_credentials, sys.argv) = orig
    check(rets == {"cc_scan": 3, "wheel_dryrun": 1, "screener": 1},
          f"C2 CLI exits {rets!r}")
    check(calls == [], f"C3 credentials called {calls!r}")
    check(err.getvalue().count("FORGE_INSTANCE is not set") == 3,
          f"C4 CLI stderr {err.getvalue()!r}")


def main():
    before = [sha(p) for p in REAL_FILES]
    for part in (part_t, part_e, part_p, part_a, part_r, part_c):
        try:
            part()
        except Exception as e:  # a crash is reported, never a silent pass
            check(False, f"{part.__name__} crashed: {type(e).__name__}: {e}")
    after = [sha(p) for p in REAL_FILES]
    print(f"isolation state.json/journal.db sha256 before={before} after={after}")
    check(before == after, "ISOLATION: real state.json / journal.db changed")
    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_instance table + errors + policy + alerts + run + cli, "
          f"{n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
