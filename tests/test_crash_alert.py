"""Offline net for deploy/crash_alert.py (Brief B7). Never the network: every
send is either patched out or goes through a fake urlopen. The fake .env files
are temp files named fake.env holding a fake token and fake ALPACA_ lines.

  M  build_message: exact text for exit 1 and exit 4; pure (no environment).
  E  .env parse: only the two Telegram keys returned, ALPACA_ values never in
     the result; python-dotenv binding rules (export, quotes, escapes, inline
     comment, whitespace, last binding wins); refuses a missing or empty key.
  S  main: send patched to raise an error whose text holds the fake token, the
     real send with urlopen raising the same, and a missing key. Exit 1, the
     message and the error CLASS on stderr, the fake token in neither stdout
     nor stderr. Success: exit 0, URL / body / timeout 15 / no parse_mode,
     token not on stdout. MONITOR_* absent -> "unknown".
  I  isolation: crash_alert imports none of forgetrader, alerts, credentials,
     dotenv; the template unit runs /usr/bin/python3 -I, no EnvironmentFile=.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
DEPLOY = Path(ROOT) / "deploy"

_spec = importlib.util.spec_from_file_location("crash_alert", DEPLOY / "crash_alert.py")
ca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ca)

FAKE_TOKEN = "987654321:FAKE-test_crash_alert-token-NOT-REAL"
FAKE_ALPACA = "FAKE-ALPACA-SECRET-NOT-REAL"
CHAT = "4242"
MON = {"MONITOR_SERVICE_RESULT": "exit-code", "MONITOR_EXIT_STATUS": "1"}
REAL_SEND = ca.send
REAL_URLOPEN = ca.urllib.request.urlopen

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


def env_file(text):
    d = Path(tempfile.mkdtemp(prefix="test_crash_alert."))
    p = d / "fake.env"
    p.write_text(text)
    return p


GOOD_ENV = (f"ALPACA_API_KEY={FAKE_ALPACA}\n"
            f"ALPACA_API_SECRET={FAKE_ALPACA}\n"
            f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n"
            f"TELEGRAM_CHAT_ID={CHAT}\n")


def run_main(argv, environ):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = ca.main(argv, environ)
    return rc, out.getvalue(), err.getvalue()


def part_m():
    m1 = ca.build_message("forgetrader-run.service", "exit-code", "1")
    check(m1 == ("[forgetrader] CRASH unit=forgetrader-run.service result=exit-code exit=1\n"
                 "check: journalctl --user -u forgetrader-run.service --since today"),
          f"M1 exit 1 text {m1!r}")
    m4 = ca.build_message("forgetrader-run.service", "exit-code", 4)
    check(m4 == ("[forgetrader] CRASH unit=forgetrader-run.service result=exit-code exit=4\n"
                 "check: journalctl --user -u forgetrader-run.service --since today"),
          f"M2 exit 4 text {m4!r}")
    check(m4.startswith("[forgetrader] CRASH "), "M3 prefix")


def part_e():
    cfg = ca.parse_env_text(GOOD_ENV)
    check(cfg == {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": CHAT},
          "E1 only the two keys, exact values")
    check(FAKE_ALPACA not in repr(cfg), "E2 ALPACA value in result")
    check(ca.load_env(env_file(GOOD_ENV)) == cfg, "E3 load_env from a file")
    rules = ("# comment\n\n"
             f"export TELEGRAM_BOT_TOKEN = '{FAKE_TOKEN}'\n"
             f'ALPACA_API_SECRET="{FAKE_ALPACA}"\n'
             "TELEGRAM_CHAT_ID=1  # inline comment\n"
             "TELEGRAM_CHAT_ID=\"42\\t43\"\n")
    cfg = ca.parse_env_text(rules)
    check(cfg == {"TELEGRAM_BOT_TOKEN": FAKE_TOKEN, "TELEGRAM_CHAT_ID": "42\t43"},
          "E4 export / spaces / quotes / escape / last wins")
    check(ca.parse_env_text(f"TELEGRAM_BOT_TOKEN=a#b\nTELEGRAM_CHAT_ID=7 \n")
          == {"TELEGRAM_BOT_TOKEN": "a#b", "TELEGRAM_CHAT_ID": "7"},
          "E5 # without space kept, trailing space stripped")
    for name, text in (
            ("no token", f"ALPACA_API_KEY={FAKE_ALPACA}\nTELEGRAM_CHAT_ID={CHAT}\n"),
            ("no chat", f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n"),
            ("empty chat", f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_CHAT_ID=\n"),
            ("chat no =", f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_CHAT_ID\n")):
        try:
            ca.parse_env_text(text)
            check(False, f"E6 {name}: not refused")
        except ca.EnvError as e:
            check(FAKE_TOKEN not in str(e) and FAKE_ALPACA not in str(e),
                  f"E6 {name}: refusal text carries a value")


def part_s():
    path = str(env_file(GOOD_ENV))
    msg = ca.build_message("forgetrader-run.service", "exit-code", "1")

    class FakeNetError(Exception):
        pass

    def raising_send(token, chat_id, text):
        raise FakeNetError(f"boom https://api.telegram.org/bot{token}/sendMessage")

    ca.send = raising_send
    try:
        rc, out, err = run_main([path, "forgetrader-run.service"], MON)
    finally:
        ca.send = REAL_SEND
    check(rc == 1, f"S1 patched send raises: rc {rc}")
    check(FAKE_TOKEN not in out and FAKE_TOKEN not in err, "S1 token in stdout/stderr")
    check("FakeNetError" in err and msg in err, f"S1 stderr {err!r}")

    def raising_urlopen(req, timeout=None):
        raise urllib.error.URLError(f"refused {req.full_url}")

    ca.urllib.request.urlopen = raising_urlopen
    try:
        rc, out, err = run_main([path, "forgetrader-run.service"], MON)
    finally:
        ca.urllib.request.urlopen = REAL_URLOPEN
    check(rc == 1, f"S2 urlopen raises: rc {rc}")
    check(FAKE_TOKEN not in out and FAKE_TOKEN not in err, "S2 token in stdout/stderr")
    check("URLError" in err and msg in err, f"S2 stderr {err!r}")

    sent = []
    ca.send = lambda *a: sent.append(a)
    try:
        rc, out, err = run_main([str(env_file(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n")),
                                 "forgetrader-run.service"], MON)
    finally:
        ca.send = REAL_SEND
    check(rc == 1 and sent == [], f"S3 missing chat id: rc {rc} sent {sent!r}")
    check("EnvError" in err and msg in err and FAKE_TOKEN not in err + out, f"S3 stderr {err!r}")

    calls = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, req.data, req.get_method(), timeout))
        return Resp()

    ca.urllib.request.urlopen = fake_urlopen
    try:
        rc, out, err = run_main([path, "forgetrader-failtest.service"], {})
    finally:
        ca.urllib.request.urlopen = REAL_URLOPEN
    check(rc == 0 and len(calls) == 1, f"S4 success: rc {rc} calls {len(calls)}")
    if calls:
        url, data, method, timeout = calls[0]
        body = urllib.parse.parse_qs(data.decode("utf-8"))
        check(url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage" and method == "POST",
              "S4 url/method")
        check(timeout == 15, f"S4 timeout {timeout}")
        check(body == {"chat_id": [CHAT], "text": [ca.build_message(
            "forgetrader-failtest.service", "unknown", "unknown")]},
            f"S4 body {body!r}")
    check(FAKE_TOKEN not in out and FAKE_TOKEN not in err, "S4 token on stdout/stderr")

    def status_urlopen(req, timeout=None):
        r = Resp()
        r.status = 500
        return r

    ca.urllib.request.urlopen = status_urlopen
    try:
        rc, out, err = run_main([path, "forgetrader-run.service"], MON)
    finally:
        ca.urllib.request.urlopen = REAL_URLOPEN
    check(rc == 1 and "SendError" in err, f"S5 non-200: rc {rc} err {err!r}")


def part_i():
    src = (DEPLOY / "crash_alert.py").read_text()
    imports = [l.split()[1] for l in src.splitlines() if l.startswith(("import ", "from "))]
    check(set(imports) <= {"codecs", "os", "re", "sys", "urllib.parse", "urllib.request"},
          f"I1 imports {imports!r}")
    unit = (DEPLOY / "forgetrader-crash-alert@.service").read_text()
    check("ExecStart=/usr/bin/python3 -I %h/forgetrader/deploy/crash_alert.py "
          "%h/forgetrader/.env %i" in unit, "I2 ExecStart")
    check("EnvironmentFile" not in unit, "I3 EnvironmentFile present")
    for f in ("forgetrader-run.service", "forgetrader-failtest.service"):
        check("OnFailure=forgetrader-crash-alert@%n.service\n" in (DEPLOY / f).read_text(),
              f"I4 {f} OnFailure")


def main():
    for part in (part_m, part_e, part_s, part_i):
        try:
            part()
        except Exception as e:  # a crash is reported, never a silent pass
            check(False, f"{part.__name__} crashed: {type(e).__name__}")
    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_crash_alert {n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
