"""Offline net for scouts/ (Brief scouts-core, D42 / D43). Stdlib only, temp dirs only,
pinned clocks (--now), never the network, never an LLM, never a live file.

  V  scouts/validate.py: one rejection test per rule (unknown key incl. an agent-computed
     field, bad ticker, tz-naive / future / older-than-36h fetched_at, non-https URL in
     every role, run_date versus RUN_DIR, sentiment range and type, filing form, flag code,
     bad JSON, missing file, role mismatch, duplicate ticker); success writes ROLE.ok =
     sha256 and the derived file; failure removes stale ROLE.ok / derived.
  E  earnings status: agree / conflict / single_source, two URLs on one host and
     www.host + host are single_source.
  B  scouts/barrier.py: READY / WAITING / MISSED (deadline boundary, Sydney tz),
     rejected roles named, file edited after validation counts as missing.
  F  scouts/facts.py: refuses unvalidated and edited input (nothing stored), agree gives
     the date, conflict / single_source / unknown give None, latest run wins, derived.json
     is not trusted, red_flags from the latest run, append-only triggers.
  S  scouts/eval/score.py exact counts on the fixtures.
  I  scouts/ imports only stdlib and scouts; no engine module imports scouts.
"""
import ast
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCOUTS = ROOT / "scouts"
FIX = ROOT / "tests" / "fixtures" / "scouts"
sys.path.insert(0, str(ROOT))
from scouts import facts  # noqa: E402

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


RUN_DATE = "2026-11-16"
NOW = "2026-11-16T22:00:00+00:00"
FRESH = "2026-11-16T20:00:00+00:00"
TMP = Path(tempfile.mkdtemp(prefix="test_scouts."))


def py(*args):
    p = subprocess.run([sys.executable, *map(str, args)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=TMP)
    return p.returncode, p.stdout


def run_dir(name=RUN_DATE):
    d = Path(tempfile.mkdtemp(dir=TMP)) / name
    d.mkdir()
    return d


def src(url, date):
    return {"url": url, "date": date}


def earn_item(ticker, sources, fetched=FRESH):
    return {"ticker": ticker, "fetched_at": fetched, "sources": sources}


def good(role):
    item = {
        "earnings": earn_item("NOK", [src("https://www.reuters.com/a", "2026-01-29"),
                                      src("https://www.nasdaq.com/b", "2026-01-29")]),
        "news": {"ticker": "NOK", "fetched_at": FRESH, "sentiment": 1, "headlines": ["h"],
                 "catalysts": [], "sources": ["https://www.reuters.com/n"]},
        "filings": {"ticker": "NOK", "fetched_at": FRESH,
                    "filings": [{"form": "8-K", "filed": "2026-11-10", "url": "https://www.sec.gov/f"}]},
        "redflags": {"ticker": "NOK", "fetched_at": FRESH,
                     "flags": [{"code": "litigation", "note": "n", "url": "https://www.sec.gov/r"}]},
    }[role]
    return {"role": role, "run_date": RUN_DATE, "items": [item]}


def write(d, role, doc):
    p = d / f"{role}.json"
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return p


def validate(d, role, now=NOW):
    return py(SCOUTS / "validate.py", d, role, "--now", now)


def rejected_text(d, role):
    p = d / f"{role}.rejected"
    return p.read_text() if p.exists() else ""


def expect_reject(tag, role, doc, token, now=NOW, name=RUN_DATE):
    d = run_dir(name)
    write(d, role, doc)
    rc, out = validate(d, role, now)
    rej = rejected_text(d, role)
    check(rc == 1, f"{tag} exit {rc} (want 1): {out}")
    check(token in rej, f"{tag} reason {token!r} not in {rej!r}")
    check(not (d / f"{role}.ok").exists(), f"{tag} .ok written on rejection")
    return d


def expect_ok(tag, role, doc, now=NOW, name=RUN_DATE):
    d = run_dir(name)
    p = write(d, role, doc)
    rc, out = validate(d, role, now)
    check(rc == 0, f"{tag} exit {rc} (want 0): {out}")
    ok = (d / f"{role}.ok")
    check(ok.exists() and ok.read_text().strip() == hashlib.sha256(p.read_bytes()).hexdigest(),
          f"{tag} .ok missing or not the sha256")
    return d


# ---------------------------------------------------------------- V validator
for role in ("earnings", "news", "filings", "redflags"):
    d = expect_ok(f"V0 {role}", role, good(role))
    check(not (d / f"{role}.rejected").exists(), f"V0 {role} .rejected present")
    check((d / f"{role}.derived.json").exists() == (role == "earnings"), f"V0 {role} derived presence")

doc = good("earnings"); doc["extra"] = 1
expect_reject("V1 unknown top key", "earnings", doc, "unknown key 'extra'")
doc = good("earnings"); doc["items"][0]["status"] = "agree"
expect_reject("V1 agent-computed status", "earnings", doc, "unknown key 'status'")
doc = good("filings"); doc["items"][0]["filings"][0]["confidence"] = 0.9
expect_reject("V1 unknown nested key", "filings", doc, "unknown key 'confidence'")
doc = good("earnings"); del doc["items"][0]["fetched_at"]
expect_reject("V1 missing key", "earnings", doc, "missing key 'fetched_at'")
for bad in ("nok", "TOOLONGX", "1AB", ".A", ""):
    doc = good("news"); doc["items"][0]["ticker"] = bad
    expect_reject(f"V2 ticker {bad!r}", "news", doc, "bad ticker")
expect_ok("V2 ticker BRK.B", "news", {**good("news"), "items": [{**good("news")["items"][0], "ticker": "BRK.B"}]})

for bad in ("2026-11-16T20:00:00", "2026-11-16", "yesterday", 1763323200):
    doc = good("earnings"); doc["items"][0]["fetched_at"] = bad
    expect_reject(f"V3 tz-naive/bad {bad!r}", "earnings", doc, "not ISO-8601 tz-aware")
doc = good("earnings"); doc["items"][0]["fetched_at"] = "2026-11-16T22:06:00+00:00"
expect_reject("V4 future 6 min", "earnings", doc, "in the future")
doc = good("earnings"); doc["items"][0]["fetched_at"] = "2026-11-17T09:06:00+11:00"
expect_reject("V4 future 6 min, other offset", "earnings", doc, "in the future")
doc = good("earnings"); doc["items"][0]["fetched_at"] = "2026-11-16T22:04:00+00:00"
expect_ok("V4 4 min ahead accepted", "earnings", doc)
doc = good("earnings"); doc["items"][0]["fetched_at"] = "2026-11-15T09:59:00+00:00"
expect_reject("V5 older than 36 h", "earnings", doc, "older than 36 h")
doc = good("earnings"); doc["items"][0]["fetched_at"] = "2026-11-15T10:01:00Z"
expect_ok("V5 35h59m accepted", "earnings", doc)

doc = good("earnings"); doc["items"][0]["sources"][1]["url"] = "http://www.nasdaq.com/b"
expect_reject("V6 http earnings source", "earnings", doc, "url not https")
doc = good("news"); doc["items"][0]["sources"] = ["http://www.reuters.com/n"]
expect_reject("V6 http news source", "news", doc, "url not https")
doc = good("filings"); doc["items"][0]["filings"][0]["url"] = "http://www.sec.gov/f"
expect_reject("V6 http filing url", "filings", doc, "url not https")
doc = good("redflags"); doc["items"][0]["flags"][0]["url"] = "http://www.sec.gov/r"
expect_reject("V6 http flag url", "redflags", doc, "url not https")
for bad in ("ftp://x.com/a", "www.sec.gov/f", "https://", "https:///path"):
    doc = good("filings"); doc["items"][0]["filings"][0]["url"] = bad
    expect_reject(f"V6 bad url {bad!r}", "filings", doc, "url not https")

expect_reject("V7 run_date versus RUN_DIR", "news", good("news"), "does not match RUN_DIR", name="2026-11-15")
doc = good("news"); doc["run_date"] = "2026-13-01"
expect_reject("V7 run_date not a date", "news", doc, "run_date not YYYY-MM-DD", name="2026-13-01")

for bad in (3, -3, 1.0, True, "1"):
    doc = good("news"); doc["items"][0]["sentiment"] = bad
    expect_reject(f"V8 sentiment {bad!r}", "news", doc, "sentiment not an int in -2..2")
doc = good("news"); doc["items"][0]["sources"] = []
expect_reject("V8 news needs a source", "news", doc, "at least one url")
doc = good("news"); doc["items"][0]["headlines"] = ["ok", 3]
expect_reject("V8 headlines strings", "news", doc, "not a list of strings")
doc = good("earnings"); doc["items"][0]["sources"] = []
expect_reject("V8 earnings needs a source", "earnings", doc, "at least one source")
doc = good("earnings"); doc["items"][0]["sources"][0]["date"] = "29/01/2026"
expect_reject("V8 earnings source date", "earnings", doc, "date: not YYYY-MM-DD")

doc = good("filings"); doc["items"][0]["filings"][0]["form"] = "S-1"
expect_reject("V9 filing form", "filings", doc, "form: not one of")
doc = good("filings"); doc["items"][0]["filings"][0]["filed"] = "2026-02-30"
expect_reject("V9 filing date", "filings", doc, "filed: not YYYY-MM-DD")
doc = good("redflags"); doc["items"][0]["flags"][0]["code"] = "fraud"
expect_reject("V10 flag code", "redflags", doc, "code: not one of")
expect_reject("V11 bad json", "news", "{not json", "json:")
doc = good("news"); doc["role"] = "earnings"
expect_reject("V12 role mismatch", "news", doc, "is not 'news'")
doc = good("news"); doc["items"].append(dict(doc["items"][0]))
expect_reject("V13 duplicate ticker", "news", doc, "duplicate ticker 'NOK'")

d = expect_ok("V14 stale markers", "earnings", good("earnings"))
bad = good("earnings"); bad["items"][0]["fetched_at"] = "2026-11-14T00:00:00+00:00"
write(d, "earnings", bad)
rc, _ = validate(d, "earnings")
check(rc == 1 and not (d / "earnings.ok").exists() and not (d / "earnings.derived.json").exists()
      and "older than 36 h" in rejected_text(d, "earnings"), "V14 stale .ok / derived not removed")
write(d, "earnings", good("earnings"))
rc, _ = validate(d, "earnings")
check(rc == 0 and not (d / "earnings.rejected").exists(), "V14 stale .rejected not removed on success")
(d / "earnings.json").unlink()
rc, _ = validate(d, "earnings")
check(rc == 1 and not (d / "earnings.ok").exists() and "cannot read" in rejected_text(d, "earnings"),
      "V15 missing file not rejected with stale .ok removed")
rc, out = py(SCOUTS / "validate.py", d, "earnings", "--now", "2026-11-16T22:00:00")
check(rc == 2, f"V16 tz-naive --now exit {rc}")

# ---------------------------------------------------------------- E earnings status
CASES = {
    "AGR": ([src("https://www.reuters.com/a", "2026-01-29"), src("https://www.nasdaq.com/b", "2026-01-29")],
            "agree", "2026-01-29"),
    "CON": ([src("https://www.reuters.com/a", "2026-01-29"), src("https://www.nasdaq.com/b", "2026-01-30")],
            "conflict", None),
    "CON.B": ([src("https://a.com/1", "2026-01-29"), src("https://b.com/2", "2026-01-29"),
               src("https://c.com/3", "2026-02-05")], "conflict", None),
    "ONE": ([src("https://www.reuters.com/a", "2026-01-29")], "single_source", None),
    "SAME": ([src("https://www.reuters.com/a", "2026-01-29"), src("https://www.reuters.com/b", "2026-01-29")],
             "single_source", None),
    "WWW": ([src("https://www.zacks.com/a", "2026-01-29"), src("https://zacks.com/b", "2026-01-29")],
            "single_source", None),
    "CASE": ([src("https://Nasdaq.com/a", "2026-01-29"), src("https://nasdaq.COM/b", "2026-01-29")],
             "single_source", None),
}
edoc = {"role": "earnings", "run_date": RUN_DATE,
        "items": [earn_item(t, s) for t, (s, _, _) in CASES.items()]}
ed = expect_ok("E0 mixed earnings", "earnings", edoc)
derived = json.loads((ed / "earnings.derived.json").read_text())
check(derived["sha256"] == (ed / "earnings.ok").read_text().strip(), "E0 derived sha")
for t, (_, status, date) in CASES.items():
    got = derived["tickers"].get(t, {})
    check(got.get("status") == status, f"E {t} status {got.get('status')} want {status}")
    check(got.get("date") == date, f"E {t} date {got.get('date')} want {date}")

# ---------------------------------------------------------------- B barrier
SYD_BEFORE = "2026-11-16T22:59:00+00:00"   # 09:59 AEDT 17 Nov
SYD_AT = "2026-11-17T10:00:00+11:00"
SYD_AFTER = "2026-11-16T23:30:00+00:00"


def barrier(d, roles, now, deadline="10:00"):
    return py(SCOUTS / "barrier.py", d, "--roles", roles, "--deadline", deadline,
              "--tz", "Australia/Sydney", "--now", now)


bd = run_dir()
for role in ("earnings", "news", "filings"):
    write(bd, role, good(role))
    validate(bd, role)
rc, out = barrier(bd, "earnings,news,filings", SYD_BEFORE)
check(rc == 0 and out.startswith("READY"), f"B1 READY: {rc} {out}")
rc, out = barrier(bd, "earnings,news,filings", SYD_AFTER)
check(rc == 0 and out.startswith("READY"), f"B1 READY after deadline: {rc} {out}")
rc, out = barrier(bd, "earnings,news,filings,redflags", SYD_BEFORE)
check(rc == 5 and out.startswith("WAITING") and "missing=redflags " in out, f"B2 WAITING: {rc} {out}")
rc, out = barrier(bd, "earnings,news,filings,redflags", SYD_AT)
check(rc == 6 and out.startswith("MISSED") and "missing=redflags " in out, f"B3 MISSED at deadline: {rc} {out}")
rc, out = barrier(bd, "earnings,news,filings,redflags", SYD_AFTER)
check(rc == 6 and "missing=redflags " in out, f"B3 MISSED after: {rc} {out}")
bad = good("redflags"); bad["items"][0]["flags"][0]["code"] = "fraud"
write(bd, "redflags", bad)
validate(bd, "redflags")
rc, out = barrier(bd, "earnings,news,filings,redflags", SYD_BEFORE)
check(rc == 5 and "rejected=redflags" in out, f"B4 WAITING names rejected: {rc} {out}")
rc, out = barrier(bd, "earnings,news,filings,redflags", SYD_AFTER)
check(rc == 6 and "missing=- rejected=redflags" in out, f"B4 MISSED names rejected: {rc} {out}")
edited = good("news"); edited["items"][0]["sentiment"] = -2
write(bd, "news", edited)          # valid content, but edited after its .ok
rc, out = barrier(bd, "earnings,news", SYD_BEFORE)
check(rc == 5 and "missing=news " in out, f"B5 edited after validation not missing: {rc} {out}")
rc, out = barrier(bd, "earnings,news", SYD_AFTER)
check(rc == 6 and "missing=news " in out, f"B5 edited after validation MISSED: {rc} {out}")
validate(bd, "news")
rc, out = barrier(bd, "earnings,news", SYD_BEFORE)
check(rc == 0, f"B6 revalidated READY: {rc} {out}")
(bd / "filings.json").unlink()      # .ok left behind with no file
rc, out = barrier(bd, "filings", SYD_BEFORE)
check(rc == 5 and "missing=filings " in out, f"B7 .ok without file: {rc} {out}")
rc, out = barrier(bd, "news,bogus", SYD_BEFORE)
check(rc == 2, f"B8 unknown role exit {rc}")
rc, out = barrier(bd, "news", SYD_BEFORE, deadline="25:00")
check(rc == 2, f"B8 bad deadline exit {rc}")
rc, out = barrier(bd / "nope", "news", SYD_BEFORE)
check(rc == 5 and "missing=news " in out, f"B9 absent RUN_DIR waits: {rc} {out}")

# ---------------------------------------------------------------- F facts
db = TMP / "facts.db"
fd = run_dir("2026-11-15")
write(fd, "earnings", {**edoc, "run_date": "2026-11-15",
                       "items": [earn_item(t, s, "2026-11-15T20:00:00Z") for t, (s, _, _) in CASES.items()]})
write(fd, "redflags", {"role": "redflags", "run_date": "2026-11-15", "items": [
    {"ticker": "AGR", "fetched_at": "2026-11-15T20:00:00Z", "flags": [
        {"code": "dilution", "note": "atm", "url": "https://www.sec.gov/1"},
        {"code": "litigation", "note": "suit", "url": "https://www.sec.gov/2"}]},
    {"ticker": "ONE", "fetched_at": "2026-11-15T20:00:00Z", "flags": [
        {"code": "going_concern", "note": "gc", "url": "https://www.sec.gov/3"}]}]})
validate(fd, "earnings", "2026-11-15T22:00:00Z")
store = facts.Facts(db)
try:
    store.ingest(fd)
    check(False, "F1 ingest accepted a role with no .ok")
except facts.NotValidated as e:
    check("redflags" in str(e), f"F1 refusal names redflags: {e}")
check(store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0, "F1 partial ingest stored rows")
check(store.earnings_date("AGR") is None, "F1 date visible after refused ingest")
validate(fd, "redflags", "2026-11-15T22:00:00Z")
tampered = json.loads((fd / "earnings.derived.json").read_text())
tampered["tickers"]["CON"] = {"status": "agree", "date": "2026-01-29", "dates": ["2026-01-29"]}
(fd / "earnings.derived.json").write_text(json.dumps(tampered))
check(sorted(store.ingest(fd)) == ["earnings", "redflags"], "F2 ingest validated run")
check(store.earnings_date("AGR") == "2026-01-29", f"F2 agree date {store.earnings_date('AGR')}")
for t in ("CON", "CON.B", "ONE", "SAME", "WWW", "CASE", "NEVER"):
    check(store.earnings_date(t) is None, f"F3 {t} date {store.earnings_date(t)} want None")
check(store.earnings_status("CON") == ("conflict", ["2026-01-29", "2026-01-30"]), "F3 conflict recomputed, derived ignored")
check(store.red_flags("AGR") == ["dilution", "litigation"], f"F4 red_flags {store.red_flags('AGR')}")
check(store.red_flags("ONE") == ["going_concern"], "F4 red_flags ONE")
check(store.ingest(fd) == [], "F5 re-ingest same bytes is a no-op")

fd2 = run_dir(RUN_DATE)            # next day: AGR now conflicts, CON now agrees, ONE clean
write(fd2, "earnings", {"role": "earnings", "run_date": RUN_DATE, "items": [
    earn_item("AGR", CASES["CON"][0]), earn_item("CON", CASES["AGR"][0])]})
write(fd2, "redflags", {"role": "redflags", "run_date": RUN_DATE, "items": [
    {"ticker": "AGR", "fetched_at": FRESH, "flags": [{"code": "delisting", "note": "n", "url": "https://x.com/d"}]}]})
validate(fd2, "earnings")
validate(fd2, "redflags")
write(fd2, "redflags", {"role": "redflags", "run_date": RUN_DATE, "items": []})   # edited after .ok
runs_before = store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
try:
    store.ingest(fd2)
    check(False, "F6 ingest accepted a file edited after validation")
except facts.NotValidated as e:
    check("redflags" in str(e), f"F6 refusal names redflags: {e}")
check(store.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == runs_before, "F6 refused run partly stored")
validate(fd2, "redflags")
check(sorted(store.ingest(fd2)) == ["earnings", "redflags"], "F7 ingest second run")
check(store.earnings_date("AGR") is None, f"F7 latest conflict must hide older agree: {store.earnings_date('AGR')}")
check(store.earnings_date("CON") == "2026-01-29", "F7 latest agree wins")
check(store.red_flags("AGR") == [], f"F7 red_flags from latest run only: {store.red_flags('AGR')}")
check(store.red_flags("ONE") == [], "F7 ticker absent from latest run has no flags")
try:
    store.db.execute("UPDATE earnings SET status = 'agree'")
    check(False, "F8 UPDATE allowed")
except sqlite3.Error as e:
    check("append-only" in str(e), f"F8 UPDATE error {e}")
try:
    store.db.execute("DELETE FROM runs")
    check(False, "F8 DELETE allowed")
except sqlite3.Error as e:
    check("append-only" in str(e), f"F8 DELETE error {e}")
store.close()
rc, out = py(SCOUTS / "facts.py", db, "earnings-date", "CON")
check(rc == 0 and out.strip() == "2026-01-29", f"F9 cli earnings-date {rc} {out}")
rc, out = py(SCOUTS / "facts.py", db, "earnings-date", "AGR")
check(rc == 0 and out.strip() == "None", f"F9 cli conflict None {rc} {out}")
rc, out = py(SCOUTS / "facts.py", db, "ingest", run_dir())
check(rc == 1 and out.startswith("REFUSED"), f"F9 cli empty run refused {rc} {out}")

# ---------------------------------------------------------------- S scorer
rc, out = py(SCOUTS / "eval" / "score.py", FIX / "answer_key.json", FIX / "candidate_perfect.json")
lines = out.splitlines()
check(rc == 0 and lines == ["earnings_date: 4/4 100.0%", "filings: 5/5 100.0% extra=0", "misses=0"],
      f"S1 perfect: {rc} {lines}")
rc, out = py(SCOUTS / "eval" / "score.py", FIX / "answer_key.json", FIX / "candidate_cheap.json")
lines = out.splitlines()
check(rc == 0 and lines == [
    "earnings_date: 2/4 50.0%",
    "filings: 3/5 60.0% extra=2",
    "MISS earnings_date MSFT expected 2026-01-27 got 2026-01-28",
    "MISS earnings_date NOK expected 2026-01-29 got absent",
    "MISS filings AAPL expected 4 2026-11-03 not found",
    "MISS filings AAPL extra 4 2026-11-04 not in key",
    "MISS filings NOK expected 10-Q 2026-10-23 not found",
    "MISS filings NOK extra 10-K 2026-10-23 not in key",
    "misses=6"], f"S2 cheap: {rc} {lines}")
rc, out = py(SCOUTS / "eval" / "score.py", FIX / "answer_key.json", TMP / "absent.json")
check(rc == 2, f"S3 unreadable candidate exit {rc}")

# ---------------------------------------------------------------- I isolation
def imported(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


engine = {p.stem for p in ROOT.glob("*.py")} | {"forgetrader"}
check({"gates", "forgetrader_state", "forgetrader", "watchlist"} <= engine, f"I0 engine names {sorted(engine)}")
scout_files = sorted(SCOUTS.rglob("*.py"))
check(len(scout_files) >= 7, f"I1 scouts files {scout_files}")
for p in scout_files:
    names = imported(p)
    check(not (names & engine), f"I1 {p.relative_to(ROOT)} imports engine {sorted(names & engine)}")
    check(names <= set(sys.stdlib_module_names) | {"scouts"},
          f"I2 {p.relative_to(ROOT)} non-stdlib {sorted(names - set(sys.stdlib_module_names))}")
for p in sorted(ROOT.glob("*.py")) + sorted((ROOT / "forgetrader").rglob("*.py")):
    check("scouts" not in imported(p), f"I3 engine {p.relative_to(ROOT)} imports scouts")
check(facts.DEFAULT_DB in (ROOT / ".gitignore").read_text().splitlines(), "I4 facts db not gitignored")

shutil.rmtree(TMP, ignore_errors=True)
if failures:
    for f in failures:
        print("  " + f)
    print(f"FAIL: {len(failures)} failed of {n} assertions")
    raise SystemExit(1)
print(f"PASS: test_scouts validator + earnings + barrier + facts + scorer + isolation, {n} assertions")
