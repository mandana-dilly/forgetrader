"""Offline net for deploy/keyprobe.sh (Brief keyprobe-users). Stdlib only, temp dirs only:
forge's home, the decoy, the search root and the target homes all live in a temp dir; sudo,
getent and id are fakes on PATH. Fake key values are random per run, never written in this
file. Never real sudo, never a real key, never the network.

The fake sudo runs the command as the invoking user with HOME set to the target's fake home,
and models the kernel boundary (/home/forge is 750) by refusing `test`/`cat` on a path under
the fake forge home unless the test exposes it. `sudo -n` is denied unless FAKE_SUDO_N=allow.
grep, git, tee, cat, cp, stat, env and the python are argv-logging wrappers.

  R  refusals: root, forge, a uid 0 alias, a sudo member, a docker member, an unknown user,
     two arguments, an invalid name: exit 2, REFUSED, last line KEYPROBE ... FAIL, no sudo
     call, no canary written.
  C  clean: default target is forgedev; A only prints B absent and covers A only (13 PASS);
     A and B covers both (19 PASS); every sudo runs as the target.
  F  B keys in a file the target reads, in the target's login env, in B's git history,
     in B's history only on a tag (--all); B .env or B canary exposed; sudo allowed; target
     in group forge; B .env with 2 keys (ABORT, nothing probed); an A leak is listed; a stray
     key of an absent B is not covered (PASS).
  N  in every run no key value appears in the output or in any logged argv.
  S  static: no bash -x, forgedev only as the default, echo/printf/res lines use only
     non-secret variables, the pattern files are only read by grep or cp, no eval/read,
     the python snippet prints one count, the canary marker is never literal.
"""
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "keyprobe.sh"
PY = ROOT / ".venv" / "bin" / "python"
MARK = "CANARY-NOT-" + "A-KEY"

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


FAKE_SUDO = r'''
import os, sys
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write("sudo " + " ".join(a) + "\n")
if a[:1] == ["-n"]:
    sys.exit(0 if os.environ.get("FAKE_SUDO_N") == "allow" else 1)
if a[:1] != ["-u"]:
    sys.exit(1)
u, a = a[1], a[2:]
if a[:1] == ["-H"]:
    a = a[1:]
forge = os.environ["FAKE_FORGE_HOME"] + "/"
expose = os.environ.get("FAKE_EXPOSE", "").split(":")
if a[0] in ("test", "cat") and a[-1].startswith(forge) and a[-1] not in expose:
    sys.exit(1)
os.environ["HOME"] = os.path.join(os.environ["FAKE_HOMES"], u)
os.environ["USER"] = u
os.execvp(a[0], a)
'''
FAKE_GETENT = r'''
import os, sys
if sys.argv[1:2] != ["passwd"]:
    sys.exit(2)
for line in open(os.environ["FAKE_PASSWD"]):
    if line.split(":")[0] == sys.argv[2]:
        print(line.strip())
        sys.exit(0)
sys.exit(2)
'''
FAKE_ID = r'''
import os, sys
for line in open(os.environ["FAKE_GROUPS"]):
    name, _, groups = line.strip().partition(":")
    if sys.argv[1:2] == ["-nG"] and name == sys.argv[2]:
        print(groups.strip())
        sys.exit(0)
sys.exit(1)
'''
LOGGED = ("grep", "git", "tee", "cat", "cp", "stat", "env")
USERS = {  # name: (uid, groups)
    "root": (0, "root"), "forge": (1001, "forge sudo docker"), "forgedev": (1002, "forgedev"),
    "hermes": (1003, "hermes"), "toor": (0, "toor"), "admin1": (1004, "admin1 sudo"),
    "dock1": (1005, "dock1 docker"),
}


def base_env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FT_", "FAKE_", "GIT_", "ALPACA_", "TELEGRAM_"))}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    return env


def run(cmd, env, cwd=None):
    p = subprocess.run(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def fresh_keys():
    return {"ALPACA_API_KEY": "PK" + secrets.token_hex(10).upper(),
            "ALPACA_API_SECRET": secrets.token_hex(20),
            "TELEGRAM_BOT_TOKEN": f"{secrets.randbelow(10**9)}:{secrets.token_urlsafe(26)}"}


class Box:
    def __init__(self, b=True, b_keys=None, groups=None):
        self.tmp = Path(tempfile.mkdtemp(prefix="kp_"))
        self.bin = self.tmp / "bin"
        self.forge = self.tmp / "forge"
        self.a = self.forge / "forgetrader"
        self.b = self.forge / "forgetrader-b"
        self.root = self.tmp / "root"
        self.homes = self.root / "home"
        self.decoy = self.root / "srv" / "forgedrop" / "to-dev" / "decoy.txt"
        self.log = self.tmp / "argv.log"
        self.env = base_env()
        self.bin.mkdir()
        for name, src in (("sudo", FAKE_SUDO), ("getent", FAKE_GETENT), ("id", FAKE_ID)):
            self.exe(name, f"#!{sys.executable}\n{src}")
        for name in LOGGED:
            real = shutil.which(name)
            self.exe(name, f'#!/bin/sh\nprintf "%s\\n" "{name} $*" >> "$FAKE_LOG"\n'
                           f'exec {real} "$@"\n')
        self.exe("python", f'#!/bin/sh\nprintf "%s\\n" "python $*" >> "$FAKE_LOG"\n'
                           f'exec {PY} "$@"\n')
        users = dict(USERS)
        users.update(groups or {})
        (self.tmp / "passwd").write_text("".join(
            f"{u}:x:{uid}:{uid}::{self.homes / u}:/bin/bash\n" for u, (uid, _) in users.items()))
        (self.tmp / "groups").write_text("".join(f"{u}: {g}\n" for u, (_, g) in users.items()))
        for u in users:
            (self.homes / u).mkdir(parents=True)
        self.decoy.parent.mkdir(parents=True)
        self.ka = fresh_keys()
        self.repo(self.a, self.ka)
        self.kb = None
        if b:
            self.kb = fresh_keys() if b_keys is None else b_keys
            self.repo(self.b, self.kb)
        self.env.update(
            PATH=f"{self.bin}:{self.env['PATH']}", FT_FORGE_HOME=str(self.forge),
            FT_PY=str(self.bin / "python"), FT_DECOY=str(self.decoy),
            FT_SEARCH_ROOT=str(self.root), FAKE_LOG=str(self.log),
            FAKE_FORGE_HOME=str(self.forge), FAKE_HOMES=str(self.homes),
            FAKE_PASSWD=str(self.tmp / "passwd"), FAKE_GROUPS=str(self.tmp / "groups"))

    def exe(self, name, text):
        p = self.bin / name
        p.write_text(text)
        p.chmod(0o755)

    def repo(self, d, keys):
        d.mkdir(parents=True)
        run(["git", "init", "-q", "-b", "main", str(d)], self.env)
        (d / "README").write_text("r\n")
        run(["git", "-C", str(d), "add", "README"], self.env)
        run(["git", "-C", str(d), "commit", "-q", "-m", "c"], self.env)
        (d / ".env").write_text("".join(f"{k}={v}\n" for k, v in keys.items())
                                + "ALPACA_PAPER=true\n")
        (d / ".env").chmod(0o600)

    def values(self):
        return [v for k in (self.ka, self.kb) if k for v in k.values() if v]

    def go(self, *args):
        self.log.write_text("")
        rc, out = run(["bash", str(SCRIPT), *args], self.env, cwd="/")
        self.out = out
        self.lines = out.splitlines()
        self.argv = self.log.read_text()
        for v in self.values():
            check(v not in out, f"N output holds a key value ({args})")
            check(v not in self.argv, f"N an argv holds a key value ({args})")
        return rc

    def fails(self):
        return [x for x in self.lines if x.startswith("FAIL ")]

    def passes(self):
        return [x for x in self.lines if x.startswith("PASS ")]

    def sudo_users(self):
        return {x.split()[2] for x in self.argv.splitlines() if x.startswith("sudo -u ")}

    def done(self):
        shutil.rmtree(self.tmp)


# R refusals
for tag, args, why in (("R1", ["root"], "never probed"), ("R2", ["forge"], "never probed"),
                       ("R3", ["toor"], "uid 0"), ("R4", ["admin1"], "in group sudo"),
                       ("R5", ["dock1"], "in group docker"), ("R6", ["nobody9"], "no user nobody9"),
                       ("R7", ["hermes", "x"], "usage"), ("R8", ["Bad;x"], "not a valid user name")):
    box = Box()
    rc = box.go(*args)
    tgt = args[0] if tag not in ("R7", "R8") else "invalid"
    check(rc == 2, f"{tag} rc {rc}: {box.out!r}")
    check(any(x.startswith("REFUSED: ") and why in x for x in box.lines), f"{tag} reason: {box.out!r}")
    check(box.lines[-1:] == [f"KEYPROBE target={tgt} result=FAIL"], f"{tag} last line {box.lines[-1:]}")
    check("sudo" not in box.argv, f"{tag} sudo called: {box.argv!r}")
    check(not (box.a / "canary.secret").exists(), f"{tag} canary written")
    box.done()

# C clean runs
box = Box(b=False)
rc = box.go()
check(rc == 0 and box.lines[-1] == "KEYPROBE target=forgedev result=PASS", f"C1 default target: {box.out!r}")
check(box.sudo_users() == {"forgedev"}, f"C1 sudo users {box.sudo_users()}")
box.done()

box = Box(b=False)
rc = box.go("hermes")
check(rc == 0 and box.lines[-1] == "KEYPROBE target=hermes result=PASS", f"C2 rc {rc}: {box.out!r}")
check(f"account B: {box.b}/.env absent, B not covered" in box.lines, f"C2 B absence line: {box.out!r}")
check([x for x in box.lines if x.startswith("coverage: ")] == [f"coverage: A {box.a}/.env"],
      f"C2 coverage: {box.out!r}")
check(box.fails() == [] and len(box.passes()) == 13, f"C2 13 PASS: {box.passes()} {box.fails()}")
check(box.sudo_users() == {"hermes"}, f"C2 sudo users {box.sudo_users()}")
check(oct((box.a / "canary.secret").stat().st_mode & 0o777) == "0o600", "C2 canary A mode")
check(sorted(x.strip() for x in box.lines if x.startswith("  ")) ==
      sorted([f"{box.a}/.env", f"{box.a}/canary.secret", str(box.decoy),
              str(box.homes / "hermes" / "keyprobe-decoy.txt")]), f"C2 listings: {box.out!r}")
box.done()

box = Box()
rc = box.go("hermes")
check(rc == 0 and box.lines[-1] == "KEYPROBE target=hermes result=PASS", f"C3 rc {rc}: {box.out!r}")
check(not any(x.startswith("account B:") for x in box.lines), "C3 B wrongly reported absent")
check([x for x in box.lines if x.startswith("coverage: ")] ==
      [f"coverage: A {box.a}/.env", f"coverage: B {box.b}/.env"], f"C3 coverage: {box.out!r}")
check("PASS patterns B: 3 of 3 keys loaded" in box.lines, f"C3 B patterns: {box.out!r}")
for want in ("PASS canary B: same owner and mode as .env", "PASS canary B: read by hermes BLOCKED",
             "PASS .env B: read by hermes BLOCKED", "PASS git history B: 0 key strings",
             "PASS positive control B: .env and canary.secret found"):
    check(want in box.lines, f"C3 missing {want!r}")
check(box.fails() == [] and len(box.passes()) == 19, f"C3 19 PASS: {box.passes()} {box.fails()}")
check(box.sudo_users() == {"hermes"}, f"C3 sudo users {box.sudo_users()}")
box.done()


# F failures
def fail_case(tag, want, prep, b=True, target="hermes", **kw):
    box = Box(b=b, **kw)
    prep(box)
    rc = box.go(target)
    check(rc == 1 and box.lines[-1] == f"KEYPROBE target={target} result=FAIL", f"{tag} rc {rc}: {box.out!r}")
    got = [x.split(":")[0] for x in box.fails()]
    check(got == want, f"{tag} FAIL lines {box.fails()} expected {want}")
    box.done()
    return box


fail_case("F1", ["FAIL filesystem search as hermes"],
          lambda x: (x.homes / "hermes" / "notes.txt").write_text(f"t {x.kb['TELEGRAM_BOT_TOKEN']}\n"))
fail_case("F2", ["FAIL env of hermes", "FAIL filesystem search as hermes"],
          lambda x: (x.homes / "hermes" / ".profile").write_text(f"export NOTE={x.kb['ALPACA_API_SECRET']}\n"))


def leak_history(x):
    (x.b / "old.txt").write_text(x.kb["ALPACA_API_KEY"] + "\n")
    run(["git", "-C", str(x.b), "add", "old.txt"], x.env)
    run(["git", "-C", str(x.b), "commit", "-q", "-m", "leak"], x.env)
    run(["git", "-C", str(x.b), "rm", "-q", "old.txt"], x.env)
    run(["git", "-C", str(x.b), "commit", "-q", "-m", "unleak"], x.env)


fail_case("F3", ["FAIL git history B"], leak_history)


def leak_side_branch(x):
    run(["git", "-C", str(x.b), "checkout", "-q", "-b", "side"], x.env)
    leak_history(x)
    run(["git", "-C", str(x.b), "checkout", "-q", "main"], x.env)
    run(["git", "-C", str(x.b), "branch", "-q", "-D", "side"], x.env)
    run(["git", "-C", str(x.b), "tag", "kept", "HEAD@{1}"], x.env)


fail_case("F3b", ["FAIL git history B"], leak_side_branch)
fail_case("F4", ["FAIL .env B"], lambda x: x.env.update(FAKE_EXPOSE=f"{x.b}/.env"))
fail_case("F5", ["FAIL canary B"], lambda x: x.env.update(FAKE_EXPOSE=f"{x.b}/canary.secret"))
fail_case("F6", ["FAIL sudo for hermes BLOCKED"], lambda x: x.env.update(FAKE_SUDO_N="allow"))
fail_case("F7", ["FAIL hermes not in group forge"], lambda x: None,
          groups={"hermes": (1003, "hermes forge")})
box = fail_case("F8", ["FAIL patterns B"], lambda x: None,
                b_keys={"ALPACA_API_KEY": "PK" + secrets.token_hex(10), "ALPACA_API_SECRET": secrets.token_hex(20)})
check("ABORT: key patterns incomplete, nothing probed" in box.lines, f"F8 abort line: {box.out!r}")
check("PASS patterns A: 3 of 3 keys loaded" in box.lines and "FAIL patterns B: 2 of 3 keys loaded" in box.lines,
      f"F8 counts: {box.out!r}")
check(box.sudo_users() == set() and not (box.a / "canary.secret").exists(), f"F8 probed after abort: {box.argv!r}")

box = Box(b=False)
(box.homes / "hermes" / "notes.txt").write_text(f"t {box.ka['TELEGRAM_BOT_TOKEN']}\n")
check(box.go("hermes") == 1 and "FAIL filesystem search as hermes: 3 file(s), only decoys" in box.lines,
      f"F9 A leak listed: {box.out!r}")
check(f"  {box.homes}/hermes/notes.txt" in box.lines, f"F9 leaking file named: {box.out!r}")
box.done()
box = Box(b=False)
stray = fresh_keys()["TELEGRAM_BOT_TOKEN"]
(box.homes / "hermes" / "notes.txt").write_text(f"t {stray}\n")
check(box.go("hermes") == 0, f"F10 a key of an absent B is not covered: {box.out!r}")
box.done()

# S static
text = SCRIPT.read_text()
code = [x for x in text.splitlines() if not x.lstrip().startswith("#")]
check(MARK not in text and MARK not in Path(__file__).read_text(), "S1 marker literal in a file")
check(not any("bash -x" in x or "set -x" in x or "set -o xtrace" in x for x in code), "S2 xtrace")
check([x for x in code if "forgedev" in x] == ["T=${1:-forgedev}"], "S3 forgedev outside the default")
check(not any(w in x for x in code for w in ("eval ", "$(cat", "`")), "S4 eval/cat-subst/backtick")
check(not any(re.search(r"(^|[;|&]\s*|while\s+)read\b", x.strip()) for x in code), "S4 read builtin")
VAR = re.compile(r"\$\{?#?!?([A-Za-z_][A-Za-z0-9_]*|[0-9])")
SAFE = {"T", "TH", "TG", "SROOT", "B", "i", "LABELS", "DIRS", "MARK", "EXPECT", "d", "1", "c", "n",
        "r", "nn", "nv", "W", "L", "PW", "2", "E", "C", "D", "R", "DECOY", "HDECOY", "fails"}
for x in code:
    for seg in re.findall(r"\b(?:echo|printf|res)\b([^;|>&]*)", x):
        bad = set(VAR.findall(seg)) - SAFE
        check(not bad, f"S5 printed variable(s) {bad} in: {x}")
for x in code:
    if "$KEYS" in x or "$ALL" in x:
        ok = (re.search(r'grep -[a-zA-Z]*F[a-zA-Z]* -f "\$(KEYS|ALL)"', x)
              or re.search(r'grep -rlF -f - .*|^\s*< "\$ALL"', x) or x.strip().startswith("< \"$ALL\"")
              or x.strip() in ('cp "$KEYS" "$ALL"', "printf '%s\\n' \"$MARK\" >> \"$ALL\"", ': >| "$KEYS"',
                               "' \"$1\" \"$KEYS\" 2>/dev/null; }", "KEYS=$W/keys", "ALL=$W/all"))
        check(ok, f"S6 pattern file used outside grep/cp: {x}")
for x in code:
    m = re.match(r"\s*([A-Za-z_]+)=\$\((.*)", x)
    if m and ("KEYS" in m.group(2) or "ALL" in m.group(2)):
        check("grep -c" in m.group(2), f"S7 assignment reads a pattern file: {x}")
snippet = text.split("extract() {", 1)[1].split("; }", 1)[0]
check(re.findall(r"print\((.*)\)", snippet) == ["len(good)"], "S8 python prints more than a count")

if failures:
    for f in failures:
        print("  " + f)
    print(f"FAIL: {len(failures)} failed of {n} assertions")
    raise SystemExit(1)
print(f"PASS: test_keyprobe refusals + coverage + failures + static, {n} assertions")
