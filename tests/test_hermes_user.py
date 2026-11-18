"""Offline net for deploy/hermes/create_user.sh (Brief keyprobe-users, D43). Stdlib only:
sudo, getent, id and stat are fakes on PATH backed by one JSON file (fakesys.json) in a temp
dir, so no real user, group, path or sudo is touched. The fake sudo applies useradd,
passwd --lock, chmod, tee -a and install -d to that file and logs every argv.

  H1 dry run on a fresh box: exact stdout, rc 0, no mutating command, fake system unchanged.
  H2 --apply on a fresh box: the mutating commands are exactly the dry run's "+ " lines in
     order; RESULT OK; the fake system matches D43.
  H3 a second run (dry and --apply) on the H2 result reports OK and mutates nothing.
  H4 an existing wrong hermes (extra groups, unlocked password, home 755, no umask line,
     output dir 2770, output dir missing, wrong home) is a FAIL with the exact reason and is
     never fixed, also under --apply.
  H5 leftovers without the user (group hermes, /home/hermes, /srv/hermes-out) and a missing
     group forge refuse before any command, also under --apply.
  H6 usage: an unknown flag and two arguments exit 2 with nothing run.
  H7 static: no bash -x, no .env or key variable in code, never sudo with -E or a key.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "hermes" / "create_user.sh"

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


FAKE = r'''
import json, os, sys
db = os.environ["FAKE_SYS"]
s = json.load(open(db))
me = os.path.basename(sys.argv[0])
a = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps([me] + a) + "\n")
def save():
    json.dump(s, open(db, "w"))
if me == "getent":
    kind, name = a
    if kind == "passwd" and name in s["users"]:
        u = s["users"][name]
        print(f"{name}:x:{u['uid']}:{u['uid']}::{u['home']}:/bin/bash"); sys.exit(0)
    if kind == "group" and name in s["groups"]:
        print(f"{name}:x:1:"); sys.exit(0)
    sys.exit(2)
if me == "id":
    if a[0] == "-nG" and a[1] in s["users"]:
        print(" ".join(s["users"][a[1]]["groups"])); sys.exit(0)
    sys.exit(1)
if me == "stat":
    p = a[-1]
    if p not in s["stat"]:
        sys.exit(1)
    if a[0] == "-c":
        print(s["stat"][p])
    sys.exit(0)
assert me == "sudo"
if a[:2] == ["passwd", "-S"]:
    u = s["users"].get(a[2])
    if not u:
        sys.exit(1)
    print(f"{a[2]} {u['pw']} 11/18/2026 0 99999 7 -1"); sys.exit(0)
if a[:3] == ["-u", "hermes", "grep"]:
    sys.exit(0 if a[4] in s["files"].get(a[5], []) else 1)
if a[:3] == ["-u", "hermes", "tee"] and a[3] == "-a":
    data = sys.stdin.read()
    s["files"].setdefault(a[4], []).extend(data.splitlines()); save()
    sys.stdout.write(data); sys.exit(0)
if a[0] == "useradd":
    name = a[-1]
    s["users"][name] = {"uid": 1003, "home": f"/home/{name}", "groups": [name], "pw": "L"}
    s["groups"].append(name)
    s["stat"][f"/home/{name}"] = f"755 {name} {name}"
    s["files"][f"/home/{name}/.profile"] = ["# skel"]
    save(); sys.exit(0)
if a[:2] == ["passwd", "--lock"]:
    s["users"][a[2]]["pw"] = "L"; save(); sys.exit(0)
if a[0] == "chmod":
    _, owner, group = s["stat"][a[2]].split()
    s["stat"][a[2]] = f"{a[1]} {owner} {group}"; save(); sys.exit(0)
if a[:2] == ["install", "-d"]:
    o, g, m, p = a[3], a[5], a[7], a[8]
    s["stat"][p] = f"{m} {o} {g}"; save(); sys.exit(0)
sys.exit(97)
'''

FRESH = {"users": {"forge": {"uid": 1001, "home": "/home/forge", "groups": ["forge", "sudo", "docker"],
                             "pw": "P"}},
         "groups": ["forge", "sudo", "docker"], "stat": {"/home/forge": "750 forge forge"}, "files": {}}
GOOD = {"users": {**FRESH["users"],
                  "hermes": {"uid": 1003, "home": "/home/hermes", "groups": ["hermes"], "pw": "L"}},
        "groups": FRESH["groups"] + ["hermes"],
        "stat": {**FRESH["stat"], "/home/hermes": "750 hermes hermes", "/srv/hermes-out": "2750 hermes forge"},
        "files": {"/home/hermes/.profile": ["# skel", "umask 027"]}}
MUTATING = ("useradd", "chmod", "tee", "install", "--lock")

NEXT = """NEXT: Baxman, as forge: sudo -u hermes -i, put the LLM key in hermes's own environment with an
      editor (never on a command line, never in forge's files), exit, then:
      cd / && sudo -v && bash ~/forgetrader/deploy/keyprobe.sh hermes
"""
DRY = """create_user.sh: user hermes (D43), mode DRY RUN
check: user hermes absent, group hermes absent, /home/hermes absent, /srv/hermes-out absent, group forge present
+ sudo useradd --create-home --user-group --shell /bin/bash hermes
+ sudo passwd --lock hermes
+ sudo chmod 750 /home/hermes
+ printf '%s\\n' 'umask 027' | sudo -u hermes tee -a /home/hermes/.profile
+ sudo install -d -o hermes -g forge -m 2750 /srv/hermes-out
RESULT: DRY RUN, nothing changed. Re-run with --apply to execute.
""" + NEXT


class Box:
    def __init__(self, state):
        self.tmp = Path(tempfile.mkdtemp(prefix="hu_"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.sys = self.tmp / "fakesys.json"
        self.log = self.tmp / "argv.log"
        self.sys.write_text(json.dumps(state))
        fake = self.tmp / "fake.py"
        fake.write_text(f"#!{sys.executable}\n{FAKE}")
        fake.chmod(0o755)
        for name in ("sudo", "getent", "id", "stat"):
            (self.bin / name).symlink_to(fake)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("FT_", "FAKE_"))}
        self.env.update(PATH=f"{self.bin}:{self.env['PATH']}", FAKE_SYS=str(self.sys), FAKE_LOG=str(self.log))

    def go(self, *args):
        self.log.write_text("")
        p = subprocess.run(["bash", str(SCRIPT), *args], env=self.env, cwd="/", stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.out = p.stdout
        self.calls = [json.loads(x) for x in self.log.read_text().splitlines()]
        return p.returncode

    def state(self):
        return json.loads(self.sys.read_text())

    def mutations(self):
        return [c for c in self.calls if c[0] == "sudo" and any(m in c for m in MUTATING)]

    def done(self):
        shutil.rmtree(self.tmp)


def mutate(state, fn):
    s = json.loads(json.dumps(state))
    fn(s)
    return s


# H1
box = Box(FRESH)
rc = box.go()
check(rc == 0, f"H1 rc {rc}")
check(box.out == DRY, f"H1 dry-run output differs:\n{box.out}")
check(box.mutations() == [], f"H1 mutated: {box.mutations()}")
check(box.state() == FRESH, "H1 fake system changed")
box.done()

# H2, H3
box = Box(FRESH)
rc = box.go("--apply")
plus = [x[2:] for x in DRY.splitlines() if x.startswith("+ ")]
did = [" ".join(c[0:1] + c[1:]) if c[:3] != ["sudo", "-u", "hermes"] or c[3] != "tee"
       else "printf '%s\\n' 'umask 027' | " + " ".join(c) for c in box.mutations()]
check(rc == 0, f"H2 rc {rc}: {box.out}")
check(did == plus, f"H2 applied {did} expected {plus}")
check(box.out == DRY.replace("mode DRY RUN", "mode APPLY").replace(
    "RESULT: DRY RUN, nothing changed. Re-run with --apply to execute.", "RESULT: OK, hermes created and matches D43"),
    f"H2 output:\n{box.out}")
st = box.state()
check(st["users"]["hermes"] == GOOD["users"]["hermes"], f"H2 user {st['users'].get('hermes')}")
check(st["stat"] == GOOD["stat"] and st["files"] == GOOD["files"], f"H2 paths {st['stat']} {st['files']}")
for args in ((), ("--apply",)):
    rc = box.go(*args)
    mode = "APPLY" if args else "DRY RUN"
    check(rc == 0 and box.out == f"create_user.sh: user hermes (D43), mode {mode}\n"
          "check: user hermes exists, verifying (never fixed here)\n"
          "RESULT: OK, hermes exists and matches D43, nothing to do\n" + NEXT, f"H3 {args} output:\n{box.out}")
    check(box.mutations() == [], f"H3 {args} mutated: {box.mutations()}")
box.done()

# H4
WRONG = (
    ("extra groups", lambda s: s["users"]["hermes"].update(groups=["hermes", "sudo", "docker", "forge"]),
     ["FAIL groups are 'hermes sudo docker forge', expected only hermes"]),
    ("unlocked", lambda s: s["users"]["hermes"].update(pw="P"),
     ["FAIL password status 'P', expected L (locked)"]),
    ("home 755", lambda s: s["stat"].update({"/home/hermes": "755 hermes hermes"}),
     ["FAIL /home/hermes is '755 hermes hermes', expected '750 hermes hermes'"]),
    ("no umask", lambda s: s["files"].update({"/home/hermes/.profile": ["# skel", "umask 022"]}),
     ["FAIL no 'umask 027' line in /home/hermes/.profile"]),
    ("out 2770", lambda s: s["stat"].update({"/srv/hermes-out": "2770 hermes forge"}),
     ["FAIL /srv/hermes-out is '2770 hermes forge', expected '2750 hermes forge'"]),
    ("out missing", lambda s: s["stat"].pop("/srv/hermes-out"),
     ["FAIL /srv/hermes-out is '', expected '2750 hermes forge'"]),
    ("uid 0 elsewhere", lambda s: s["users"]["hermes"].update(uid=0, home="/root"),
     ["FAIL hermes has uid 0", "FAIL home is '/root', expected /home/hermes"]),
)
for tag, fn, want in WRONG:
    st0 = mutate(GOOD, fn)
    for args in ((), ("--apply",)):
        box = Box(st0)
        rc = box.go(*args)
        lines = box.out.splitlines()
        check(rc == 1, f"H4 {tag} {args} rc {rc}")
        check([x for x in lines if x.startswith("FAIL ")] == want, f"H4 {tag} {args} reasons: {box.out}")
        check(lines[-1] == "RESULT: FAIL, hermes exists but does not match D43, nothing changed",
              f"H4 {tag} {args} last line: {box.out}")
        check(box.mutations() == [] and box.state() == st0, f"H4 {tag} {args} fixed silently: {box.mutations()}")
        check("NEXT:" not in box.out, f"H4 {tag} {args} printed NEXT")
        box.done()

# H5
LEFT = (
    ("group", lambda s: s["groups"].append("hermes"), "FAIL group hermes exists without user hermes"),
    ("home", lambda s: s["stat"].update({"/home/hermes": "755 root root"}), "FAIL /home/hermes exists without user hermes"),
    ("out", lambda s: s["stat"].update({"/srv/hermes-out": "755 root root"}),
     "FAIL /srv/hermes-out exists without user hermes"),
    ("no forge", lambda s: s["groups"].remove("forge"), "FAIL group forge missing"),
)
for tag, fn, want in LEFT:
    st0 = mutate(FRESH, fn)
    for args in ((), ("--apply",)):
        box = Box(st0)
        rc = box.go(*args)
        lines = box.out.splitlines()
        check(rc == 1 and [x for x in lines if x.startswith("FAIL ")] == [want]
              and lines[-1] == "RESULT: FAIL, leftovers found, nothing changed", f"H5 {tag} {args}: {box.out}")
        check(not any(x.startswith("+ ") for x in lines), f"H5 {tag} {args} printed commands")
        check(box.mutations() == [] and box.state() == st0, f"H5 {tag} {args} mutated")
        box.done()

# H6
for args in (("--force",), ("--apply", "x")):
    box = Box(FRESH)
    rc = box.go(*args)
    check(rc == 2 and box.out == "usage: create_user.sh [--apply]\n" and box.calls == [], f"H6 {args}: {rc} {box.out!r}")
    box.done()

# H7
code = [x for x in SCRIPT.read_text().splitlines() if not x.lstrip().startswith("#")]
check(not any("bash -x" in x or "set -x" in x for x in code), "H7 xtrace")
check(not any(w in x for x in code for w in (".env", "_KEY", "TOKEN", "SECRET", "sudo -E", "--preserve-env")),
      "H7 key or env file named in code")

if failures:
    for f in failures:
        print("  " + f)
    print(f"FAIL: {len(failures)} failed of {n} assertions")
    raise SystemExit(1)
print(f"PASS: test_hermes_user dry run + apply + idempotent + wrong user + leftovers, {n} assertions")
