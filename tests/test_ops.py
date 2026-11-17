"""Offline net for the ops scripts (Brief ops-tooling). Stdlib only, temp git repos only:
every path root and the sudo prefix come from FT_* variables pointed into a temp dir,
and the sudo prefix is replaced by `env HOME=<fake forgedev home> SPY_AS=forgedev`.
Never the network, never forge's home, never a real bundle drop.

  A  deploy/automerge.sh refuses (exit 10, the named check, merge.sh and notify never
     called, main and the verified file unchanged): a deploy/ path, a top-level new
     path, tests/ plus deploy/ mixed, a tests_ prefix that is not tests/, a rename out
     of deploy/, missing verified file, verified SHA != refs/dev, empty diff, non-ff,
     bad branch name. Accepts tests-only, docs-only, tests plus docs: merge.sh called
     exactly once with BRANCH SHA, one notify with the exact AUTOMERGE text. A failing
     merge.sh is passed through and sends no ping.
  S  deploy/suites.sh: refuses a wrong SHA and a non-hex SHA (exit 2, no suite run);
     PASS line counts suites; one failing suite gives the FAIL line and a non-zero
     exit; every suite reads an empty stdin (the script arrives on stdin); the temp
     clone is removed; the script names nothing under forge's home.
  V  deploy/verify.sh: per-branch forgedev worktree ~/verify-BRANCH, two branches keep
     two worktrees, no ~/verify.
  M  deploy/merge.sh: every suite runs through the sudo prefix (spy), none as the
     invoking user; main.bundle and forgedev main at the new HEAD; B absent, B
     fast-forwarded, B not ff-able refused loudly; a failing suite sets
     suites_failed=1; SHA mismatch still refused before anything runs.
  L  deploy/ops/launch.sh --wait: the four ping texts; automerge runs only after PASS.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
VENV = ROOT / ".venv"

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


def base_env():
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("FT_", "SPY_", "AM_", "LW_", "GIT_"))}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL="/dev/null")
    return env


def run(cmd, cwd=None, env=None, stdin=None):
    with open(stdin, "rb") if stdin else open(os.devnull, "rb") as f:
        p = subprocess.run(cmd, cwd=cwd, env=env or base_env(), stdin=f,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return p.returncode, p.stdout


def git(repo, *args):
    rc, out = run(["git", "-C", str(repo), *args])
    if rc != 0:
        raise RuntimeError(f"git {args} failed: {out}")
    return out.strip()


def write(repo, files):
    for rel, text in files.items():
        p = Path(repo) / rel
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)


def commit(repo, files, msg="c"):
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def new_repo(path, files):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    return commit(path, files, "base")


def lines(out):
    return out.splitlines()


OK_SUITE = 'print("PASS: ok")\n'
BAD_SUITE = 'import sys\nprint("FAIL: 1 failed of 1 assertions")\nsys.exit(1)\n'
STDIN_SUITE = ('import sys\ndata = sys.stdin.read()\n'
               'print("PASS: stdin empty" if data == "" else "FAIL: stdin not empty")\n'
               'sys.exit(0 if data == "" else 1)\n')
SPY_SUITE = ('import os\n'
             'with open(os.environ["SPY_LOG"], "a") as f:\n'
             '    f.write(os.environ.get("SPY_AS", "INVOKER") + " " + os.path.basename(__file__) + "\\n")\n'
             'print("PASS: spy")\n')


# ---------------------------------------------------------------- A automerge
SPY_MERGE = ('#!/usr/bin/env bash\necho "$@" >> "$AM_LOG/merge.log"\n'
             'echo "spy merge.sh $*"\nexit "${AM_MERGE_RC:-0}"\n')
SPY_NOTIFY = ('import os, sys\n'
              'with open(os.path.join(os.environ["AM_LOG"], "notify.log"), "a") as f:\n'
              '    f.write(sys.argv[1] + "\\n")\n')
AM_BASE = {"tests/test_ok.py": OK_SUITE, "docs/a.md": "a\nb\n", "deploy/x.sh": "x\n",
           "deploy/merge.sh": SPY_MERGE, "deploy/ops/notify.py": SPY_NOTIFY, "README.md": "r\n"}


def am_case(name, changes, verified="same", advance_main=False, empty=False,
            branch="feat", merge_rc=0):
    tmp = Path(tempfile.mkdtemp(prefix="test_ops_am."))
    repo = tmp / "forge"
    new_repo(repo, AM_BASE)
    log = tmp / "log"
    log.mkdir()
    if empty:
        sha = git(repo, "rev-parse", "main")
    else:
        git(repo, "checkout", "-q", "-b", "dev")
        for i, ch in enumerate(changes):
            if ch.get("__mv__"):
                a, b = ch["__mv__"]
                git(repo, "mv", a, b)
                git(repo, "commit", "-q", "-m", f"mv{i}")
            else:
                commit(repo, ch, f"c{i}")
        sha = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "main")
    if advance_main:
        commit(repo, {"docs/later.md": "later\n"}, "main moves on")
    git(repo, "update-ref", f"refs/dev/{branch}", sha)
    vf = repo / ".git" / f"verified-{branch}"
    if verified == "same":
        vf.write_text(sha + "\n")
    elif verified == "other":
        vf.write_text(git(repo, "rev-parse", "main") + "\n")
    main_before = git(repo, "rev-parse", "main")
    vf_before = vf.read_text() if vf.exists() else None
    env = base_env()
    env.update(FT_FORGE_REPO=str(repo), AM_LOG=str(log), AM_MERGE_RC=str(merge_rc))
    rc, out = run(["bash", str(DEPLOY / "automerge.sh"), branch], cwd=tmp, env=env)
    ml = log / "merge.log"
    nl = log / "notify.log"
    res = {"rc": rc, "out": out, "sha": sha,
           "merge": ml.read_text().splitlines() if ml.exists() else [],
           "notify": nl.read_text().splitlines() if nl.exists() else [],
           "main_same": git(repo, "rev-parse", "main") == main_before,
           "vf_same": (vf.read_text() if vf.exists() else None) == vf_before,
           "clean": git(repo, "status", "--porcelain") == ""}
    shutil.rmtree(tmp)
    return res


def am_refused(tag, r, want_line):
    check(r["rc"] == 10, f"{tag} exit {r['rc']} want 10: {r['out']!r}")
    check(want_line in lines(r["out"]), f"{tag} missing refusal line {want_line!r}: {r['out']!r}")
    check(r["merge"] == [], f"{tag} merge.sh called {r['merge']}")
    check(r["notify"] == [], f"{tag} notify called {r['notify']}")
    check(r["main_same"] and r["vf_same"] and r["clean"], f"{tag} changed something")


r = am_case("deploy", [{"deploy/x.sh": "x2\n"}])
am_refused("A1 deploy/", r, "AUTOMERGE REFUSED path=deploy/x.sh")

r = am_case("toplevel", [{"NEWFILE.md": "n\n"}])
am_refused("A2 top-level new", r, "AUTOMERGE REFUSED path=NEWFILE.md")

r = am_case("mixed", [{"tests/test_new.py": OK_SUITE, "deploy/y.sh": "y\n"}])
am_refused("A3 mixed", r, "AUTOMERGE REFUSED path=deploy/y.sh")
check("AUTOMERGE REFUSED path=tests/test_new.py" not in lines(r["out"]),
      "A3 tests/ path wrongly refused")
check(sum(1 for x in lines(r["out"]) if x.startswith("AUTOMERGE REFUSED")) == 1,
      f"A3 want exactly one refusal line: {r['out']!r}")

r = am_case("prefix", [{"tests_evil/a.py": "a\n", "docsx": "d\n"}])
am_refused("A4 tests_ prefix", r, "AUTOMERGE REFUSED path=tests_evil/a.py")
check("AUTOMERGE REFUSED path=docsx" in lines(r["out"]), "A4 docsx not refused")

r = am_case("rename", [{"__mv__": ("deploy/x.sh", "tests/x.sh")}])
am_refused("A5 rename out of deploy/", r, "AUTOMERGE REFUSED path=deploy/x.sh")

r = am_case("noverified", [{"tests/test_new.py": OK_SUITE}], verified="missing")
am_refused("A6 missing verified", r, "AUTOMERGE REFUSED no .git/verified-feat")

r = am_case("other", [{"tests/test_new.py": OK_SUITE}], verified="other")
am_refused("A7 verified != refs/dev", r,
           next((x for x in lines(r["out"]) if x.startswith("AUTOMERGE REFUSED verified ")),
                "AUTOMERGE REFUSED verified <missing>"))
check(any(x.startswith("AUTOMERGE REFUSED verified ") and x.endswith(r["sha"])
          for x in lines(r["out"])), f"A7 refusal does not name refs/dev sha: {r['out']!r}")

r = am_case("empty", [], empty=True)
am_refused("A8 empty diff", r,
           next((x for x in lines(r["out"]) if x.startswith("AUTOMERGE REFUSED empty diff")),
                "AUTOMERGE REFUSED empty diff <missing>"))

r = am_case("nonff", [{"tests/test_new.py": OK_SUITE}], advance_main=True)
check(any(x.startswith("AUTOMERGE REFUSED main ") and "is not an ancestor of" in x
          for x in lines(r["out"])), f"A9 non-ff refusal line missing: {r['out']!r}")
am_refused("A9 non-ff", r, next((x for x in lines(r["out"]) if "not an ancestor" in x), "?"))

tmp = Path(tempfile.mkdtemp(prefix="test_ops_am."))
env = base_env()
env.update(FT_FORGE_REPO=str(tmp))
rc, out = run(["bash", str(DEPLOY / "automerge.sh"), "bad/name"], cwd=tmp, env=env)
check(rc == 10 and "AUTOMERGE REFUSED bad branch name" in lines(out), f"A10 bad name: {rc} {out!r}")
shutil.rmtree(tmp)


def am_accepted(tag, r, files, ins, dele):
    check(r["rc"] == 0, f"{tag} exit {r['rc']}: {r['out']!r}")
    check(r["merge"] == [f"feat {r['sha']}"], f"{tag} merge.sh calls {r['merge']}")
    want = f"[forge] AUTOMERGE sha={r['sha'][:12]} files={files} +{ins}/-{dele}"
    check(r["notify"] == [want], f"{tag} notify {r['notify']} want {[want]}")
    check(not any("REFUSED" in x for x in lines(r["out"])), f"{tag} refusal printed: {r['out']!r}")


r = am_case("tests", [{"tests/test_new.py": "a\nb\nc\n"}])
am_accepted("A11 tests-only", r, 1, 3, 0)
r = am_case("docs", [{"docs/a.md": "a\nB\n"}])
am_accepted("A12 docs-only", r, 1, 1, 1)
r = am_case("both", [{"tests/test_new.py": "a\n"}, {"docs/new.md": "x\ny\n", "docs/a.md": None}])
am_accepted("A13 tests plus docs", r, 3, 3, 2)

r = am_case("mergefail", [{"tests/test_new.py": "a\n"}], merge_rc=1)
check(r["rc"] == 1, f"A14 merge.sh failure exit {r['rc']}: {r['out']!r}")
check(len(r["merge"]) == 1 and r["notify"] == [], f"A14 merge {r['merge']} notify {r['notify']}")


# ---------------------------------------------------------------- S suites.sh
def suites_case(files, sha=None):
    tmp = Path(tempfile.mkdtemp(prefix="test_ops_s."))
    repo = tmp / "src"
    head = new_repo(repo, files)
    todev = tmp / "todev"
    todev.mkdir()
    git(repo, "bundle", "create", "-q", str(todev / "main.bundle"), "main")
    tmpd = tmp / "tmpd"
    tmpd.mkdir()
    env = base_env()
    env.update(FT_TODEV=str(todev), FT_VENV=str(VENV), TMPDIR=str(tmpd))
    use = head if sha is None else sha
    rc, out = run(["bash", "-s", "--", use], cwd=tmp, env=env, stdin=DEPLOY / "suites.sh")
    left = os.listdir(tmpd)
    shutil.rmtree(tmp)
    return rc, out, head, left


GOOD = {"tests/test_ok.py": OK_SUITE, "tests/test_stdin.py": STDIN_SUITE, "tests/helper.py": "x = 1\n"}
rc, out, head, left = suites_case(GOOD)
L = lines(out)
check(rc == 0, f"S1 exit {rc}: {out!r}")
check(len(L) == 4, f"S1 want 4 lines: {L}")
check(L and L[0].startswith(f"=== suites SHA {head} 20"), f"S1 header: {L[:1]}")
check("tests/test_ok.py exit=0 | PASS: ok" in L, f"S1 ok line: {L}")
check("tests/test_stdin.py exit=0 | PASS: stdin empty" in L, f"S1 stdin line: {L}")
check(L[-1:] == ["=== SUITES PASS n=2"], f"S1 PASS line: {L[-1:]}")
check(left == [], f"S1 temp clone left behind: {left}")

rc, out, head, left = suites_case(GOOD, sha="0123456789abcdef0123456789abcdef01234567")
check(rc == 2, f"S2 wrong SHA exit {rc}: {out!r}")
check(lines(out) == [f"REFUSED: clone HEAD {head} is not 0123456789abcdef0123456789abcdef01234567"],
      f"S2 wrong SHA output: {out!r}")
check(left == [], f"S2 temp clone left behind: {left}")

for tag, bad in (("S3 non-hex", "g" * 40), ("S3 short", "abc123"), ("S3 upper", "A" * 40), ("S3 empty", "")):
    rc, out, head, left = suites_case(GOOD, sha=bad)
    check(rc == 2 and lines(out) == ["REFUSED: SHA is not 40 hex"], f"{tag}: {rc} {out!r}")

rc, out, head, left = suites_case(dict(GOOD, **{"tests/test_bad.py": BAD_SUITE}))
L = lines(out)
check(rc != 0, f"S4 failing suite exit {rc}")
check("tests/test_bad.py exit=1 | FAIL: 1 failed of 1 assertions" in L, f"S4 bad line: {L}")
check(L[-1:] == ["=== SUITES FAIL n=3 failed=1"], f"S4 FAIL line: {L[-1:]}")
check(not any(x.startswith("=== SUITES PASS") for x in L), "S4 PASS line printed")

rc, out, head, left = suites_case({"README.md": "r\n"})
check(rc != 0 and lines(out)[-1:] == ["=== SUITES FAIL n=0 failed=0"], f"S5 no suites: {rc} {out!r}")

src = (DEPLOY / "suites.sh").read_text()
check("/home/forge/" not in src and "/home/forge\n" not in src and "~forge" not in src,
      "S6 suites.sh names a path under forge's home")
py = [x for x in src.splitlines() if "python" in x and not x.lstrip().startswith("#")]
check(py and all("</dev/null" in x for x in py), f"S6 python call without </dev/null: {py}")


# ---------------------------------------------------------------- V verify.sh
def verify_setup(tmp):
    forge = tmp / "forge"
    new_repo(forge, {"tests/test_ok.py": OK_SUITE})
    home = tmp / "fdhome"
    home.mkdir()
    clone = home / "forgetrader"
    git(tmp, "clone", "-q", str(forge), str(clone))
    os.symlink(VENV, clone / ".venv")
    (clone / ".git" / "info" / "exclude").write_text(".venv\n")
    fromdev = tmp / "fromdev"
    fromdev.mkdir()
    return forge, home, clone, fromdev


def verify_branch(tmp, forge, home, clone, fromdev, b):
    git(clone, "checkout", "-q", "-b", b, "main")
    sha = commit(clone, {f"tests/test_{b.replace('-', '_')}.py": OK_SUITE}, b)
    git(clone, "checkout", "-q", "main")
    git(clone, "bundle", "create", "-q", str(fromdev / f"{b}.bundle"), f"main..{b}")
    env = base_env()
    env.update(FT_FORGE_REPO=str(forge), FT_FROMDEV=str(fromdev), FT_SUDO=f"env HOME={home}")
    rc, out = run(["bash", str(DEPLOY / "verify.sh"), b], cwd=tmp, env=env)
    return rc, out, sha


tmp = Path(tempfile.mkdtemp(prefix="test_ops_v."))
forge, home, clone, fromdev = verify_setup(tmp)
rc1, out1, sha1 = verify_branch(tmp, forge, home, clone, fromdev, "b-one")
check(rc1 == 0 and f"=== RESULT PASS recorded {sha1}" in lines(out1), f"V1 verify b-one: {rc1} {out1!r}")
check(f"worktree HEAD: {sha1}" in lines(out1), f"V1 worktree HEAD line: {out1!r}")
check((home / "verify-b-one").is_dir(), "V1 ~/verify-b-one missing")
check(not (home / "verify").exists(), "V1 shared ~/verify created")
rc2, out2, sha2 = verify_branch(tmp, forge, home, clone, fromdev, "b-two")
check(rc2 == 0 and f"=== RESULT PASS recorded {sha2}" in lines(out2), f"V2 verify b-two: {rc2} {out2!r}")
check((home / "verify-b-two").is_dir() and (home / "verify-b-one").is_dir(),
      "V2 two branches do not keep two worktrees")
check(git(home / "verify-b-one", "rev-parse", "HEAD") == sha1
      and git(home / "verify-b-two", "rev-parse", "HEAD") == sha2, "V2 worktree HEADs wrong")
check(not (home / "verify").exists(), "V2 shared ~/verify created")
check((forge / ".git" / "verified-b-two").read_text().strip() == sha2, "V2 verified file")
shutil.rmtree(tmp)


# ---------------------------------------------------------------- M merge.sh
def merge_case(branch_files, b_mode="absent", mismatch=False):
    tmp = Path(tempfile.mkdtemp(prefix="test_ops_m."))
    forge = tmp / "forge"
    base = new_repo(forge, {"tests/test_spy.py": SPY_SUITE,
                            "deploy/suites.sh": (DEPLOY / "suites.sh").read_text()})
    home = tmp / "fdhome"
    home.mkdir()
    git(tmp, "clone", "-q", str(forge), str(home / "forgetrader"))
    repo_b = tmp / "forge-b"
    if b_mode != "absent":
        git(tmp, "clone", "-q", str(forge), str(repo_b))
        if b_mode == "diverged":
            commit(repo_b, {"local.txt": "l\n"}, "local")
    b_before = git(repo_b, "rev-parse", "HEAD") if b_mode != "absent" else None
    git(forge, "checkout", "-q", "-b", "dev")
    sha = commit(forge, branch_files, "branch")
    git(forge, "checkout", "-q", "main")
    git(forge, "update-ref", "refs/dev/feat", sha)
    (forge / ".git" / "verified-feat").write_text((base if mismatch else sha) + "\n")
    todev = tmp / "todev"
    todev.mkdir()
    spy = tmp / "spy.log"
    env = base_env()
    env.update(FT_FORGE_REPO=str(forge), FT_FORGE_B=str(repo_b), FT_TODEV=str(todev),
               FT_VENV=str(VENV), FT_SUDO=f"env SPY_AS=forgedev HOME={home}", SPY_LOG=str(spy))
    rc, out = run(["bash", str(DEPLOY / "merge.sh"), "feat", sha], cwd=tmp, env=env)
    res = {"rc": rc, "out": out, "sha": sha, "base": base,
           "spy": spy.read_text().splitlines() if spy.exists() else [],
           "main": git(forge, "rev-parse", "main"),
           "fd_main": git(home / "forgetrader", "rev-parse", "main"),
           "bundle": (run(["git", "bundle", "list-heads", str(todev / "main.bundle"), "refs/heads/main"])[1].split()
                      or [""])[0] if (todev / "main.bundle").exists() else None,
           "b_before": b_before,
           "b_head": git(repo_b, "rev-parse", "HEAD") if b_mode != "absent" else None}
    shutil.rmtree(tmp)
    return res


r = merge_case({"tests/test_spy2.py": SPY_SUITE})
L = lines(r["out"])
check(r["rc"] == 0, f"M1 exit {r['rc']}: {r['out']!r}")
check(sorted(r["spy"]) == ["forgedev test_spy.py", "forgedev test_spy2.py"],
      f"M1 suites not run (only) through the sudo prefix: {r['spy']}")
check(not any(x.startswith("INVOKER") for x in r["spy"]), f"M1 a suite ran as the invoking user: {r['spy']}")
check("=== SUITES PASS n=2" in L, f"M1 SUITES PASS line: {L}")
check(f"main now {r['sha']}" in L and r["main"] == r["sha"], "M1 main not at SHA")
check(r["bundle"] == r["sha"], f"M1 main.bundle head {r['bundle']}")
check(r["fd_main"] == r["sha"] and f"forgedev main {r['sha']}" in L, "M1 forgedev main not refreshed")
check(L[-3:] == ["suites_failed=0", "NEXT: health check, then git push origin main", "B: not present"],
      f"M1 tail: {L[-3:]}")

r = merge_case({"tests/test_spy2.py": SPY_SUITE}, b_mode="clean")
L = lines(r["out"])
check(r["rc"] == 0 and r["b_head"] == r["sha"], f"M2 B not fast-forwarded: {r['rc']} {r['out']!r}")
check(L[-1:] == [f"B: HEAD {r['sha']}"], f"M2 B line: {L[-1:]}")

r = merge_case({"tests/test_spy2.py": SPY_SUITE}, b_mode="diverged")
L = lines(r["out"])
check(r["rc"] == 4, f"M3 B not ff-able exit {r['rc']}: {r['out']!r}")
check(L and L[-1].startswith("REFUSED: B ") and "is not an ancestor of main" in L[-1], f"M3 B refusal: {L[-1:]}")
check(r["b_head"] == r["b_before"], "M3 B changed")

r = merge_case({"tests/test_bad.py": BAD_SUITE})
L = lines(r["out"])
check(r["rc"] != 0, f"M4 failing suite exit {r['rc']}")
check("=== SUITES FAIL n=2 failed=1" in L and "suites_failed=1" in L, f"M4 fail lines: {L}")
check(r["spy"] == ["forgedev test_spy.py"], f"M4 spy {r['spy']}")

r = merge_case({"tests/test_spy2.py": SPY_SUITE}, mismatch=True)
check(r["rc"] == 3 and "REFUSED: SHA mismatch" in lines(r["out"]), f"M5 mismatch: {r['rc']} {r['out']!r}")
check(r["spy"] == [] and r["main"] == r["base"], "M5 something ran after a mismatch")

msrc = (DEPLOY / "merge.sh").read_text()
mcode = "\n".join(x for x in msrc.splitlines() if not x.lstrip().startswith("#"))
check("tests/" not in mcode and "python" not in mcode, "M6 merge.sh code names tests/ or a python")
check('$SUDO bash -s -- "$H" < "$REPO/deploy/suites.sh"' in msrc, "M6 merge.sh does not use suites.sh form")


# ---------------------------------------------------------------- L launch.sh --wait
def launch_case(result, am_rc=0, am_line=""):
    tmp = Path(tempfile.mkdtemp(prefix="test_ops_l."))
    b = f"test-ops-{os.getpid()}"
    repo = tmp / "forge"
    (repo / "deploy").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".git" / f"verified-{b}").write_text("abcdef0123456789abcdef0123456789abcdef01\n")
    (repo / "deploy" / "verify.sh").write_text(f'echo "verify $1"\n{result}\n')
    (repo / "deploy" / "automerge.sh").write_text(
        f'echo "$1" >> "{tmp}/am.log"\necho "{am_line}"\nexit {am_rc}\n')
    (tmp / "ops").mkdir()
    (tmp / "ops" / "notify.py").write_text(
        f'import sys\nopen("{tmp}/notify.log", "a").write(sys.argv[1] + "\\n")\n')
    env = base_env()
    env.update(FT_FORGE_REPO=str(repo), FT_TODEV=str(tmp), FT_SUDO="env")
    rc, out = run(["bash", str(DEPLOY / "ops" / "launch.sh"), "--wait", b], cwd=tmp, env=env)
    am = (tmp / "am.log").read_text().splitlines() if (tmp / "am.log").exists() else []
    no = (tmp / "notify.log").read_text().splitlines() if (tmp / "notify.log").exists() else []
    shutil.rmtree(tmp)
    for f in (f"/tmp/verify_{b}.out", f"/tmp/automerge_{b}.out"):
        if os.path.exists(f):
            os.unlink(f)
    return rc, out, am, no, b


rc, out, am, no, b = launch_case('echo "=== RESULT PASS recorded x"')
check(rc == 0 and am == [b], f"L1 automerge not run once after PASS: {am} {out!r}")
check(no == [f"[forgetrader] BUILD {b}: RESULT PASS, AUTOMERGED abcdef012345 - paste: "
             f"cat /tmp/verify_{b}.out /tmp/automerge_{b}.out"], f"L1 ping {no}")
rc, out, am, no, b = launch_case('echo "=== RESULT PASS recorded x"', 10, "AUTOMERGE REFUSED path=deploy/x.sh")
check(no == [f"[forgetrader] BUILD {b}: RESULT PASS, manual merge (AUTOMERGE REFUSED path=deploy/x.sh) - paste: "
             f"cat /tmp/verify_{b}.out /tmp/automerge_{b}.out"], f"L2 ping {no}")
rc, out, am, no, b = launch_case('echo "=== RESULT FAIL rc=1, nothing recorded"')
check(am == [] and no == [f"[forgetrader] BUILD {b}: === RESULT FAIL - paste: cat /tmp/verify_{b}.out"],
      f"L3 FAIL: am {am} ping {no}")
rc, out, am, no, b = launch_case("exit 1")
check(am == [] and no == [f"[forgetrader] BUILD {b}: verify did not finish - paste: cat /tmp/verify_{b}.out"],
      f"L4 no result: am {am} ping {no}")
for f in ("automerge.sh", "ops/launch.sh"):
    code = [x for x in (DEPLOY / f).read_text().splitlines() if not x.lstrip().startswith("#")]
    check(not any("push" in x for x in code), f"L5 {f} has a push")


if failures:
    for f in failures:
        print("  " + f)
    print(f"FAIL: {len(failures)} failed of {n} assertions")
    raise SystemExit(1)
print(f"PASS: test_ops automerge + suites + verify + merge + launch, {n} assertions")
