"""Offline net for deploy/b/install_b.py (Brief b-standup). Stdlib only: temp dirs, temp
git repos and fixture unit texts only (copies of the committed deploy/ units, D20). Never
forge's home, never systemctl: the runner is a spy.

  R  correct B units from fixture A units, compared to literal expected texts; A minutes
     never in a B timer; an existing forgetrader-b path is never doubled.
  D  dry-run writes nothing, runs nothing, prints every B unit, its diff and the
     systemd-analyze calendar commands.
  P  each precondition refuses (exit 2, named reason, nothing written, runner unused),
     with --apply too; state missing prints the exact --init command.
  C  substitution-count mismatch refuses; a leftover A path, A unit name or A minute
     refuses; a wrong OnCalendar set refuses; a run service without
     TimeoutStartSec=10min refuses (G3).
  A  --apply writes exactly the B units, runs the expected runner calls, never touches
     A's files (hash before/after) and never names an A unit to systemctl.
  E  no .env content in any output.
"""
import contextlib
import hashlib
import importlib.util
import io
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("install_b", ROOT / "deploy" / "b" / "install_b.py")
ib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ib)

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)
        print(f"  FAIL {msg}")


A_UNITS = {
    "forgetrader-run.service": """[Unit]
Description=ForgeTrader live run (paper only until Phase W, D27)
OnFailure=forgetrader-crash-alert@%n.service

[Service]
Type=oneshot
WorkingDirectory=%h/forgetrader
ExecStart=%h/forgetrader/.venv/bin/python -m forgetrader.run --live
UMask=0077
TimeoutStartSec=10min
""",
    "forgetrader-run.timer": """[Unit]
Description=ForgeTrader live run, US market hours (D27)

[Timer]
OnCalendar=Mon..Fri 10:30 America/New_York
OnCalendar=Mon..Fri 13:00 America/New_York
Persistent=false

[Install]
WantedBy=timers.target
""",
    "forgetrader-digest.service": """[Unit]
Description=ForgeTrader daily digest, read-only (D38)
OnFailure=forgetrader-crash-alert@%n.service

[Service]
Type=oneshot
WorkingDirectory=%h/forgetrader
UMask=0077
ExecStart=%h/forgetrader/.venv/bin/python -m forgetrader.report digest --send %h/forgetrader
""",
    "forgetrader-digest.timer": """[Unit]
Description=ForgeTrader daily digest, 16:30 ET weekdays (D38)

[Timer]
OnCalendar=Mon..Fri 16:30 America/New_York
Persistent=false

[Install]
WantedBy=timers.target
""",
    "forgetrader-crash-alert@.service": """[Unit]
Description=ForgeTrader crash alert for %i (B7)

[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 -I %h/forgetrader/deploy/crash_alert.py %h/forgetrader/.env %i
""",
    "forgetrader-failtest.service": """[Unit]
Description=ForgeTrader crash alert end-to-end test, always fails (B7)
OnFailure=forgetrader-crash-alert@%n.service

[Service]
Type=oneshot
ExecStart=/bin/false
""",
}

EXPECTED_B = {
    "forgetrader-b-run.service": """[Unit]
Description=ForgeTrader live run (paper only until Phase W, D27)
OnFailure=forgetrader-crash-alert@%n.service

[Service]
Type=oneshot
WorkingDirectory=%h/forgetrader-b
ExecStart=%h/forgetrader-b/.venv/bin/python -m forgetrader.run --live
UMask=0077
TimeoutStartSec=10min
""",
    "forgetrader-b-run.timer": """[Unit]
Description=ForgeTrader live run, US market hours (D27)

[Timer]
OnCalendar=Mon..Fri 10:45 America/New_York
OnCalendar=Mon..Fri 13:15 America/New_York
Persistent=false

[Install]
WantedBy=timers.target
""",
    "forgetrader-b-digest.service": """[Unit]
Description=ForgeTrader daily digest, read-only (D38)
OnFailure=forgetrader-crash-alert@%n.service

[Service]
Type=oneshot
WorkingDirectory=%h/forgetrader-b
UMask=0077
ExecStart=%h/forgetrader-b/.venv/bin/python -m forgetrader.report digest --send %h/forgetrader-b
""",
    "forgetrader-b-digest.timer": """[Unit]
Description=ForgeTrader daily digest, 16:35 ET weekdays (D38)

[Timer]
OnCalendar=Mon..Fri 16:35 America/New_York
Persistent=false

[Install]
WantedBy=timers.target
""",
}

SECRET = "SENTINEL_SECRET_9f3a1c"
ENV_OK = (f"ALPACA_API_KEY={SECRET}\nALPACA_SECRET_KEY={SECRET}x\nFORGE_INSTANCE=b\n"
          f"TELEGRAM_BOT_TOKEN={SECRET}y\n")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Fixture:
    """A temp home: unit dir with A units, A repo, B repo cloned from A (same HEAD)."""

    def __init__(self, units=None, env=ENV_OK):
        self.tmp = Path(tempfile.mkdtemp(prefix="test_install_b."))
        self.unit_dir = self.tmp / "units"
        self.unit_dir.mkdir()
        for name, text in (units or A_UNITS).items():
            (self.unit_dir / name).write_text(text)
        self.a = self.tmp / "forgetrader"
        self.a.mkdir()
        git(self.a, "init", "-q")
        (self.a / "policy-b.toml").write_text("# fixture\n")
        (self.a / "README").write_text("a\n")
        git(self.a, "add", "-A")
        git(self.a, "commit", "-q", "-m", "init")
        self.b = self.tmp / "forgetrader-b"
        subprocess.run(["git", "clone", "-q", str(self.a), str(self.b)], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        (self.b / "state.json").write_text("{}\n")
        (self.b / ".venv" / "bin").mkdir(parents=True)
        (self.b / ".venv" / "bin" / "python").write_text("")
        if env is not None:
            (self.b / ".env").write_bytes(env.encode() if isinstance(env, str) else env)
        self.calls = []
        self.rc = {}

    def runner(self, argv):
        self.calls.append(list(argv))
        return self.rc.get(argv[-1], 0), f"spy-out {argv[-1]}\n"

    def run(self, *extra, apply=False):
        argv = ["--unit-dir", str(self.unit_dir), "--a-repo", str(self.a), "--b-repo", str(self.b)]
        argv += list(extra) + (["--apply"] if apply else [])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = ib.main(argv, runner=self.runner)
        self.out = buf.getvalue()
        all_outputs.append(self.out)
        return rc

    def snapshot(self):
        """sha256 of every file under the temp home except .git internals."""
        snap = {}
        for p in sorted(self.tmp.rglob("*")):
            if p.is_file() and ".git" not in p.relative_to(self.tmp).parts:
                snap[str(p.relative_to(self.tmp))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return snap

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


all_outputs = []



def render(a_name, a_text):
    """ib.render, with a refusal recorded as a failed assertion (never a traceback)."""
    try:
        return ib.render(a_name, a_text)
    except ib.Refused as e:
        check(False, f"R0 {a_name} renders without refusal, got {e}")
        return None, ""


# --------------------------------------------------------------------------- R
print("R  render")
for a_name, a_text in A_UNITS.items():
    if a_name not in ib.SPEC:
        continue
    b_name, b_text = render(a_name, a_text)
    check(b_name in EXPECTED_B, f"R1 {a_name} renders to an expected name, got {b_name}")
    check(b_text == EXPECTED_B.get(b_name), f"R2 {b_name} text equals the literal expected text")
for a_name in ("forgetrader-run.timer", "forgetrader-digest.timer"):
    b_name, b_text = render(a_name, A_UNITS[a_name])
    for m in ("10:30", "13:00", "16:30"):
        check(m not in b_text, f"R3 A minute {m} never in {b_name}")
check(ib.A_PATH_RE.subn(r"\1/forgetrader-b", "x %h/forgetrader-b/a %h/forgetrader/b "
                        "/home/forge/forgetrader-b /home/forge/forgetrader\n")
      == ("x %h/forgetrader-b/a %h/forgetrader-b/b /home/forge/forgetrader-b "
          "/home/forge/forgetrader-b\n", 2),
      "R4 existing forgetrader-b paths untouched, A paths in both forms substituted once")
for a_name, a_text in A_UNITS.items():
    check((ROOT / "deploy" / a_name).read_text() == a_text,
          f"R6 fixture {a_name} equals the committed deploy/ copy (recon counts hold)")
for unit, text in (("deploy/forgetrader-run.service",
                    (ROOT / "deploy" / "forgetrader-run.service").read_text()),
                   ("rendered forgetrader-b-run.service",
                    render("forgetrader-run.service", A_UNITS["forgetrader-run.service"])[1])):
    svc = text.split("[Service]\n", 1)[-1] if "[Service]\n" in text else ""
    check("TimeoutStartSec=10min" in svc.splitlines(),
          f"R7 {unit} has TimeoutStartSec=10min in [Service] (G3)")
check(ib.build_parser().parse_args([]).unit_dir == "/home/forge/.config/systemd/user"
      and ib.build_parser().parse_args([]).a_repo == "/home/forge/forgetrader"
      and ib.build_parser().parse_args([]).b_repo == "/home/forge/forgetrader-b"
      and ib.build_parser().parse_args([]).apply is False,
      "R5 defaults are today's paths and dry-run")

# --------------------------------------------------------------------------- D
print("D  dry-run")
f = Fixture()
before = f.snapshot()
rc = f.run()
check(rc == 0, f"D1 dry-run exit 0, got {rc}: {f.out[-300:]}")
check(f.snapshot() == before, "D2 dry-run writes nothing")
check(sorted(p.name for p in f.unit_dir.iterdir()) == sorted(A_UNITS), "D3 no B unit file exists")
check(f.calls == [], f"D4 dry-run runs nothing, got {f.calls}")
for b_name, b_text in EXPECTED_B.items():
    check(b_text in f.out, f"D5 dry-run prints {b_name} in full")
    check(f"--- diff {b_name.replace('-b-', '-')} {b_name}" in f.out, f"D6 diff header for {b_name}")
check("\n-OnCalendar=Mon..Fri 10:30 America/New_York\n" in f.out
      and "\n+OnCalendar=Mon..Fri 10:45 America/New_York\n" in f.out,
      "D7 unified diff shows 10:30 to 10:45")
check("-WorkingDirectory=%h/forgetrader\n+WorkingDirectory=%h/forgetrader-b" in f.out,
      "D8 unified diff shows the path substitution")
for c in ("Mon..Fri 10:45 America/New_York", "Mon..Fri 13:15 America/New_York",
          "Mon..Fri 16:35 America/New_York"):
    check(f"systemd-analyze calendar '{c}'" in f.out, f"D9 calendar command for {c}")
check("DRY-RUN: nothing written" in f.out, "D10 dry-run says nothing written")
f.close()

# --------------------------------------------------------------------------- P
print("P  preconditions")


def refusal(name, setup, reason, units=None, env=ENV_OK, extra=None):
    for apply in (False, True):
        f = Fixture(units=units, env=env)
        setup(f)
        before = f.snapshot()
        rc = f.run(apply=apply)
        tag = f"{name}{' --apply' if apply else ''}"
        check(rc == 2, f"{tag} exit 2, got {rc}")
        check(f"REFUSED: {reason}" in f.out, f"{tag} names reason {reason}: {f.out[-200:]!r}")
        check(f.snapshot() == before, f"{tag} nothing written")
        check(f.calls == [], f"{tag} runner never called")
        check(not any(f.unit_dir.glob("forgetrader-b-*")), f"{tag} no B unit")
        if extra:
            extra(f, tag)
        f.close()


def rm(rel):
    return lambda f: (f.b / rel).unlink() if rel != "unit" else None


refusal("P1 B not a git checkout", lambda f: shutil.rmtree(f.b / ".git"), "b-not-git-checkout")
refusal("P2 B missing", lambda f: shutil.rmtree(f.b), "b-repo-missing")


def advance_a(f):
    (f.a / "README").write_text("a2\n")
    git(f.a, "commit", "-q", "-am", "two")


refusal("P3 HEAD mismatch", advance_a, "head-mismatch")
refusal("P4 policy-b.toml missing", rm("policy-b.toml"), "policy-b-missing")


def untrack_policy(f):
    git(f.b, "rm", "-q", "--cached", "policy-b.toml")
    git(f.b, "commit", "-q", "-m", "untrack")
    # keep HEADs equal: move A to the same commit
    subprocess.run(["git", "-C", str(f.a), "fetch", "-q", str(f.b), "HEAD"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    git(f.a, "reset", "-q", "--hard", "FETCH_HEAD")


refusal("P5 policy-b.toml untracked", untrack_policy, "policy-b-untracked")


def init_cmd(f, tag):
    check(f"cd {f.b} && .venv/bin/python forgetrader_state.py --init" in f.out,
          f"{tag} prints the exact --init command")


refusal("P6 state.json missing", rm("state.json"), "state-missing", extra=init_cmd)
refusal("P7 B venv missing", lambda f: shutil.rmtree(f.b / ".venv"), "b-venv-missing")
refusal("P8 .env missing", lambda f: None, "env-missing", env=None)
refusal("P9 FORGE_INSTANCE=a", lambda f: None, "env-instance-other",
        env=ENV_OK.replace("FORGE_INSTANCE=b", "FORGE_INSTANCE=a"))
refusal("P10 FORGE_INSTANCE=B", lambda f: None, "env-instance-other",
        env=ENV_OK.replace("FORGE_INSTANCE=b", "FORGE_INSTANCE=B"))
refusal("P11 FORGE_INSTANCE= b", lambda f: None, "env-instance-other",
        env=ENV_OK.replace("FORGE_INSTANCE=b", "FORGE_INSTANCE= b"))
refusal("P12 FORGE_INSTANCE=b with CRLF", lambda f: None, "env-instance-other",
        env=ENV_OK.replace("FORGE_INSTANCE=b\n", "FORGE_INSTANCE=b\r\n"))
refusal("P13 a later binding overrides b", lambda f: None, "env-instance-other",
        env=ENV_OK + "export FORGE_INSTANCE=a\n")
refusal("P14 no FORGE_INSTANCE line", lambda f: None, "env-instance-missing",
        env=ENV_OK.replace("FORGE_INSTANCE=b\n", ""))
refusal("P15 FORGE_INSTANCE=bb", lambda f: None, "env-instance-other",
        env=ENV_OK.replace("FORGE_INSTANCE=b", "FORGE_INSTANCE=bb"))
refusal("P16 crash-alert@ template missing", lambda f: (f.unit_dir / ib.CRASH_TEMPLATE).unlink(),
        "a-unit-missing")
refusal("P17 A run timer missing", lambda f: (f.unit_dir / "forgetrader-run.timer").unlink(),
        "a-unit-missing")
refusal("P18 non-utf8 .env without the line", lambda f: None, "env-instance-missing",
        env=b"\xff\xfeKEY=" + SECRET.encode() + b"\n")

# --------------------------------------------------------------------------- C
print("C  counts and leftovers")


def with_unit(name, text):
    u = dict(A_UNITS)
    u[name] = text
    return u


run_svc = A_UNITS["forgetrader-run.service"]
run_tmr = A_UNITS["forgetrader-run.timer"]
refusal("C1 extra A path in run.service", lambda f: None, "count-mismatch",
        units=with_unit("forgetrader-run.service",
                        run_svc + "Environment=FT_LOG=%h/forgetrader/logs\n"))
refusal("C2 missing A path in digest.service", lambda f: None, "count-mismatch",
        units=with_unit("forgetrader-digest.service",
                        A_UNITS["forgetrader-digest.service"].replace(" --send %h/forgetrader",
                                                                      " --send .")))
refusal("C3 literal /home/forge path extra", lambda f: None, "count-mismatch",
        units=with_unit("forgetrader-run.service",
                        run_svc + "Environment=FT=/home/forge/forgetrader/x\n"))
refusal("C4 run timer missing its 13:00 line", lambda f: None, "count-mismatch",
        units=with_unit("forgetrader-run.timer",
                        run_tmr.replace("OnCalendar=Mon..Fri 13:00 America/New_York\n", "")))
refusal("C5 run timer 10:30 twice", lambda f: None, "count-mismatch",
        units=with_unit("forgetrader-run.timer", run_tmr.replace("(D27)", "10:30 (D27)")))
refusal("C6 leftover quoted A path", lambda f: None, "leftover-a-path",
        units=with_unit("forgetrader-run.service",
                        run_svc + 'Environment="FT=%h/forgetrader"\n'))
refusal("C7 leftover A unit name", lambda f: None, "count-mismatch",
        units=with_unit("forgetrader-run.service", run_svc + "After=forgetrader-digest.service\n"))
refusal("C8 leftover A minute (10:30:00)", lambda f: None, "leftover-a-minute",
        units=with_unit("forgetrader-run.timer",
                        run_tmr.replace("Persistent", "OnCalendar=Mon..Fri 10:30:00\nPersistent")))
refusal("C9 OnCalendar timezone changed", lambda f: None, "oncalendar-mismatch",
        units=with_unit("forgetrader-run.timer", run_tmr.replace("13:00 America/New_York",
                                                                 "13:00 UTC")))
refusal("C10 OnFailure dropped", lambda f: None, "onfailure-missing",
        units=with_unit("forgetrader-run.service",
                        run_svc.replace("OnFailure=forgetrader-crash-alert@%n.service\n", "")))
refusal("C11 existing forgetrader-b path in an A unit", lambda f: None, "count-mismatch",
        units=with_unit("forgetrader-run.service",
                        run_svc.replace("WorkingDirectory=%h/forgetrader",
                                        "WorkingDirectory=%h/forgetrader-b")))
refusal("C12 TimeoutStartSec dropped from run.service", lambda f: None, "timeout-missing",
        units=with_unit("forgetrader-run.service",
                        run_svc.replace("TimeoutStartSec=10min\n", "")))

# --------------------------------------------------------------------------- A
print("A  --apply")
f = Fixture()
a_files = [f.unit_dir / n for n in A_UNITS]
a_before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in a_files}
repo_before = {k: v for k, v in f.snapshot().items() if not k.startswith("units/")}
rc = f.run(apply=True)
check(rc == 0, f"A1 --apply exit 0, got {rc}: {f.out[-300:]}")
check({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in a_files} == a_before,
      "A2 every A unit file hash unchanged")
check({k: v for k, v in f.snapshot().items() if not k.startswith("units/")} == repo_before,
      "A3 A and B repos unchanged")
check(sorted(p.name for p in f.unit_dir.iterdir()) == sorted([*A_UNITS, *EXPECTED_B]),
      "A4 unit dir holds exactly the A units plus the four B units (no temp files)")
for b_name, b_text in EXPECTED_B.items():
    p = f.unit_dir / b_name
    check(p.is_file() and p.read_text() == b_text, f"A5 {b_name} written with the expected text")
check(f.calls == [
    ["systemctl", "--user", "daemon-reload"],
    ["systemd-analyze", "calendar", "Mon..Fri 10:45 America/New_York"],
    ["systemd-analyze", "calendar", "Mon..Fri 13:15 America/New_York"],
    ["systemd-analyze", "calendar", "Mon..Fri 16:35 America/New_York"],
    ["systemctl", "--user", "enable", "--now", "forgetrader-b-run.timer",
     "forgetrader-b-digest.timer"],
    ["systemctl", "--user", "--no-pager", "list-timers", "--all", "forge*"],
], f"A6 runner calls are exactly the expected sequence, got {f.calls}")
check(not any(a in " ".join(c) for c in f.calls for a in A_UNITS),
      "A7 no runner call names an A unit")
check(not any(w in c for c in f.calls for w in ("disable", "restart", "stop", "start", "mask")),
      "A8 no disable/restart/stop/start/mask call")
check("spy-out forge*" in f.out and "APPLIED" in f.out, "A9 list-timers output printed")
# a re-run replaces only B units, A still untouched
rc = f.run(apply=True)
check(rc == 0 and "(replaces existing)" in f.out, "A10 re-run replaces B units and says so")
check({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in a_files} == a_before,
      "A11 A unit hashes unchanged after re-run")
f.close()

f = Fixture()
f.rc["Mon..Fri 13:15 America/New_York"] = 1
rc = f.run(apply=True)
check(rc == 1 and "FAILED: systemd-analyze calendar" in f.out,
      f"A12 a failing calendar check exits 1, got {rc}")
check(not any("enable" in c for c in f.calls), "A13 timers not enabled after a failed check")
f.close()

# --------------------------------------------------------------------------- E
print("E  no .env content in any output")
check(len(all_outputs) > 40, f"E1 outputs collected ({len(all_outputs)})")
check(not any(SECRET in o for o in all_outputs), "E2 no .env value in any output")
check(not any(s in o for o in all_outputs for s in ("FORGE_INSTANCE=a", "FORGE_INSTANCE=B",
                                                     "FORGE_INSTANCE= b", "FORGE_INSTANCE=bb",
                                                     "ALPACA_", "TELEGRAM_")),
      "E3 no .env line in any output")

if failures:
    print(f"FAIL: {len(failures)} failed of {n} assertions")
    raise SystemExit(1)
print(f"PASS: test_install_b {n} assertions")
