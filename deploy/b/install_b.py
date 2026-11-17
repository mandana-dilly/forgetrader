"""Account B stand-up kit (Brief b-standup). Run AS forge:

    /usr/bin/python3 -I /home/forge/forgetrader/deploy/b/install_b.py [--apply]

Stdlib only. Source of truth is A's INSTALLED units in forge's
~/.config/systemd/user, read at run time (D20: the committed copies under
deploy/ mirror them). It renders four B units:

    forgetrader-run.service     -> forgetrader-b-run.service
    forgetrader-run.timer       -> forgetrader-b-run.timer      10:30 -> 10:45, 13:00 -> 13:15
    forgetrader-digest.service  -> forgetrader-b-digest.service
    forgetrader-digest.timer    -> forgetrader-b-digest.timer   16:30 -> 16:35

Every A repo path (%h/forgetrader or /home/forge/forgetrader, ending at "/",
whitespace or end of line) becomes the same path with "-b"; an existing
forgetrader-b path is never touched. Each substitution is counted against the
count found in recon (SPEC below); any other count, any leftover A path, A unit
name or A minute in a B unit, or a B timer whose OnCalendar set is not exactly
the expected one refuses (exit 2, named reason, nothing written). OnFailure=
stays on A's forgetrader-crash-alert@ template; %n names the B unit.

Preconditions, each refused (exit 2) before anything is written: A's four source
units and the crash-alert@ template exist; the B repo is a git checkout whose
HEAD equals the A repo HEAD; policy-b.toml exists there and is tracked;
state.json exists there (else the exact --init command is printed);
.venv/bin/python exists there; B's .env has a line exactly FORGE_INSTANCE=b and
no other line naming FORGE_INSTANCE. .env is compared as bytes and no .env line
or value is ever printed.

Default is a dry-run: each B unit in full, a unified diff against its A source,
and the systemd-analyze calendar commands; nothing written, nothing run. --apply
writes ONLY the four B units, then (through one runner) systemctl --user
daemon-reload, systemd-analyze calendar per OnCalendar, enable --now of the two
B timers, and list-timers filtered to forge. No A unit is modified, disabled or
restarted.

Exit: 0 ok, 2 refused (nothing written), 1 a systemctl/systemd-analyze call
failed after the B units were written.
"""
import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_UNIT_DIR = "/home/forge/.config/systemd/user"
DEFAULT_A_REPO = "/home/forge/forgetrader"
DEFAULT_B_REPO = "/home/forge/forgetrader-b"

CRASH_TEMPLATE = "forgetrader-crash-alert@.service"
ONFAILURE_LINE = "OnFailure=forgetrader-crash-alert@%n.service"
ENV_LINE = b"FORGE_INSTANCE=b"

# An A repo path: %h/forgetrader or /home/forge/forgetrader ending at "/", whitespace or EOL.
A_PATH_RE = re.compile(r"(%h|/home/forge)/forgetrader(?=/|\s|$)", re.M)
# Leftover: any A-repo prefix that is not exactly the B repo path.
LEFTOVER_PATH_RE = re.compile(r"(%h|/home/forge)/forgetrader(?!-b(?=/|\s|$))", re.M)
A_UNIT_RE = re.compile(r"forgetrader-(run|digest)\.(service|timer)")
A_MINUTES = ("10:30", "13:00", "16:30")

# A source -> (B name, A path count, A unit-name count, ((A minute, B minute, count), ...),
#              expected B OnCalendar values or None). Counts are from recon of deploy/.
SPEC = {
    "forgetrader-run.service": ("forgetrader-b-run.service", 2, 0, (), None),
    "forgetrader-run.timer": ("forgetrader-b-run.timer", 0, 0,
                              (("10:30", "10:45", 1), ("13:00", "13:15", 1)),
                              ("Mon..Fri 10:45 America/New_York",
                               "Mon..Fri 13:15 America/New_York")),
    "forgetrader-digest.service": ("forgetrader-b-digest.service", 3, 0, (), None),
    "forgetrader-digest.timer": ("forgetrader-b-digest.timer", 0, 0,
                                 (("16:30", "16:35", 2),),
                                 ("Mon..Fri 16:35 America/New_York",)),
}
B_TIMERS = ("forgetrader-b-run.timer", "forgetrader-b-digest.timer")


class Refused(Exception):
    """A named reason to write nothing and exit 2."""


def default_runner(argv):
    """Run one systemctl/systemd-analyze command; return (exit code, combined output)."""
    p = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def _git(repo, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    return p.returncode, p.stdout.strip()


def _instance_line_ok(line):
    return line == ENV_LINE


def check_env(b_repo):
    """B's .env has a line exactly FORGE_INSTANCE=b and no other FORGE_INSTANCE line.
    Bytes only; no line or value is ever put in a message."""
    path = Path(b_repo) / ".env"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise Refused(f"env-missing: {path} does not exist")
    except OSError as e:
        raise Refused(f"env-unreadable: {path} ({type(e).__name__})")
    found = False
    for i, line in enumerate(data.split(b"\n"), 1):
        if _instance_line_ok(line):
            found = True
        elif b"FORGE_INSTANCE" in line:
            raise Refused(f"env-instance-other: {path} line {i} names FORGE_INSTANCE "
                          f"but is not exactly FORGE_INSTANCE=b")
    if not found:
        raise Refused(f"env-instance-missing: {path} has no line exactly FORGE_INSTANCE=b")


def check_preconditions(unit_dir, a_repo, b_repo):
    unit_dir, a_repo, b_repo = Path(unit_dir), Path(a_repo), Path(b_repo)
    for name in (*SPEC, CRASH_TEMPLATE):
        if not (unit_dir / name).is_file():
            raise Refused(f"a-unit-missing: {unit_dir / name} is not an installed unit")
    if not b_repo.is_dir():
        raise Refused(f"b-repo-missing: {b_repo} is not a directory")
    rc, top = _git(b_repo, "rev-parse", "--show-toplevel")
    if rc != 0 or Path(top).resolve() != b_repo.resolve():
        raise Refused(f"b-not-git-checkout: {b_repo} is not the top of a git checkout")
    rc_a, head_a = _git(a_repo, "rev-parse", "--verify", "HEAD")
    if rc_a != 0:
        raise Refused(f"a-head-unreadable: git HEAD of {a_repo} could not be read")
    rc_b, head_b = _git(b_repo, "rev-parse", "--verify", "HEAD")
    if rc_b != 0:
        raise Refused(f"b-head-unreadable: git HEAD of {b_repo} could not be read")
    if head_a != head_b:
        raise Refused(f"head-mismatch: {b_repo} HEAD {head_b} is not {a_repo} HEAD {head_a}")
    if not (b_repo / "policy-b.toml").is_file():
        raise Refused(f"policy-b-missing: {b_repo / 'policy-b.toml'} does not exist")
    rc, _ = _git(b_repo, "ls-files", "--error-unmatch", "--", "policy-b.toml")
    if rc != 0:
        raise Refused(f"policy-b-untracked: {b_repo / 'policy-b.toml'} is not tracked by git")
    if not (b_repo / "state.json").is_file():
        raise Refused(f"state-missing: {b_repo / 'state.json'} does not exist. Initialise it "
                      f"first, as forge:\n    cd {b_repo} && .venv/bin/python "
                      f"forgetrader_state.py --init")
    if not (b_repo / ".venv" / "bin" / "python").exists():
        raise Refused(f"b-venv-missing: {b_repo / '.venv/bin/python'} does not exist")
    check_env(b_repo)


def _subst(text, regex, repl, want, what, unit):
    new, got = regex.subn(repl, text)
    if got != want:  # substitution count check
        raise Refused(f"count-mismatch: {unit} {what} substituted {got} times, expected {want}")
    return new


def oncalendars(text):
    return tuple(line.split("=", 1)[1].strip() for line in text.splitlines()
                 if line.strip().startswith("OnCalendar="))


def render(a_name, a_text):
    """B unit name and text for one A unit, or Refused."""
    b_name, n_path, n_unit, minutes, cal = SPEC[a_name]
    t = _subst(a_text, A_PATH_RE, r"\1/forgetrader-b", n_path, "A repo path", a_name)
    t = _subst(t, A_UNIT_RE, r"forgetrader-b-\1.\2", n_unit, "A unit name", a_name)
    for a_min, b_min, n in minutes:
        t = _subst(t, re.compile(rf"(?<![\d:]){re.escape(a_min)}(?![\d:])"), b_min, n,
                   f"minute {a_min}", a_name)
    m = LEFTOVER_PATH_RE.search(t)
    if m:
        raise Refused(f"leftover-a-path: {b_name} still contains {m.group(0)!r}")
    if A_UNIT_RE.search(t):
        raise Refused(f"leftover-a-unit: {b_name} still names an A unit")
    for a_min in A_MINUTES:
        if a_min in t:
            raise Refused(f"leftover-a-minute: {b_name} still contains {a_min}")
    if cal is not None and oncalendars(t) != cal:
        raise Refused(f"oncalendar-mismatch: {b_name} OnCalendar {oncalendars(t)} is not {cal}")
    if b_name.endswith(".service") and ONFAILURE_LINE not in t.splitlines():
        raise Refused(f"onfailure-missing: {b_name} has no line {ONFAILURE_LINE}")
    return b_name, t


def render_all(unit_dir):
    out = []
    for a_name in SPEC:
        a_text = (Path(unit_dir) / a_name).read_text()
        b_name, b_text = render(a_name, a_text)
        out.append((a_name, a_text, b_name, b_text))
    return out


def calendar_cmds(rendered):
    return [["systemd-analyze", "calendar", c]
            for _, _, _, b_text in rendered for c in oncalendars(b_text)]


def show(unit_dir, rendered):
    for a_name, a_text, b_name, b_text in rendered:
        a_path, b_path = Path(unit_dir) / a_name, Path(unit_dir) / b_name
        print(f"=== B unit {b_path}{' (replaces existing)' if b_path.exists() else ''}")
        print(b_text, end="" if b_text.endswith("\n") else "\n")
        print(f"--- diff {a_name} {b_name}")
        sys.stdout.writelines(difflib.unified_diff(
            a_text.splitlines(True), b_text.splitlines(True), str(a_path), str(b_path)))
    print("=== calendar checks")
    for argv in calendar_cmds(rendered):
        print(f"    systemd-analyze calendar '{argv[2]}'")


def write_units(unit_dir, rendered):
    """Write ONLY the B units (temp file in the same dir, then rename)."""
    for _, _, b_name, b_text in rendered:
        assert b_name.startswith("forgetrader-b-"), b_name
        dst = Path(unit_dir) / b_name
        tmp = Path(unit_dir) / f".{b_name}.tmp"
        tmp.write_text(b_text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dst)
        print(f"wrote {dst}")


def build_parser():
    p = argparse.ArgumentParser(prog="install_b", description="Account B stand-up kit.")
    p.add_argument("--apply", action="store_true", help="write B units and enable B timers")
    p.add_argument("--unit-dir", default=DEFAULT_UNIT_DIR)
    p.add_argument("--a-repo", default=DEFAULT_A_REPO)
    p.add_argument("--b-repo", default=DEFAULT_B_REPO)
    return p


def main(argv=None, runner=default_runner):
    args = build_parser().parse_args(argv)
    unit_dir = Path(args.unit_dir)
    try:
        check_preconditions(unit_dir, args.a_repo, args.b_repo)
        rendered = render_all(unit_dir)
    except Refused as e:
        print(f"REFUSED: {e}")
        print("nothing written")
        return 2
    show(unit_dir, rendered)
    if not args.apply:
        print("DRY-RUN: nothing written, nothing run. Re-run with --apply to install.")
        return 0
    write_units(unit_dir, rendered)
    steps = ([["systemctl", "--user", "daemon-reload"]] + calendar_cmds(rendered)
             + [["systemctl", "--user", "enable", "--now", *B_TIMERS]])
    for argv_ in steps:
        print(f"$ {' '.join(argv_)}")
        rc, out = runner(argv_)
        print(out, end="" if out.endswith("\n") or not out else "\n")
        if rc != 0:
            print(f"FAILED: {' '.join(argv_)} exit={rc}; B units are written, timers may "
                  f"not be enabled")
            return 1
    argv_ = ["systemctl", "--user", "--no-pager", "list-timers", "--all", "forge*"]
    print(f"$ {' '.join(argv_)}")
    rc, out = runner(argv_)
    print(out, end="" if out.endswith("\n") or not out else "\n")
    print("APPLIED: B units installed, B timers enabled")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
