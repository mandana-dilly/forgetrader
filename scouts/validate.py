"""Validate one scout role file (D42).

  python scouts/validate.py RUN_DIR ROLE [--now ISO]

Reads RUN_DIR/ROLE.json once and rejects on: schema errors (scouts/schemas.py),
fetched_at in the future (more than 5 min past now) or older than 36 h, run_date
not equal to the RUN_DIR name. Earnings get a status per ticker, derived here and
never taken from the agent: "agree" only if at least 2 sources on distinct hostnames
all give the same date; differing dates are "conflict"; otherwise "single_source"
(kept, flagged).

Success: exit 0, writes ROLE.derived.json (earnings only) then ROLE.ok = sha256 of
the exact bytes validated, removes any stale ROLE.rejected.
Failure: exit 1, removes any stale ROLE.ok and ROLE.derived.json, writes
ROLE.rejected with one reason per line. Usage errors exit 2.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scouts import schemas  # noqa: E402

MAX_AGE = timedelta(hours=36)
MAX_SKEW = timedelta(minutes=5)


def host_key(url):
    """Hostname used for source independence; www.x.com and x.com are one host."""
    h = schemas.https_host(url)
    return h[4:] if h and h.startswith("www.") else h


def earnings_status(sources):
    """(status, dates) for one ticker's sources; dates is the sorted distinct list."""
    dates = sorted({s["date"] for s in sources})
    hosts = {host_key(s["url"]) for s in sources}
    if len(dates) > 1:
        return "conflict", dates
    if len(hosts) >= 2:
        return "agree", dates
    return "single_source", dates


def derive_earnings(doc):
    out = {}
    for it in doc["items"]:
        status, dates = earnings_status(it["sources"])
        out[it["ticker"]] = {"status": status, "dates": dates,
                             "date": dates[0] if status == "agree" else None}
    return out


def check_file(run_dir, role, raw, now):
    """(errors, doc) for the bytes of RUN_DIR/ROLE.json."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        return [f"json: {e}"], None
    errs = schemas.check(doc, role)
    if errs:
        return errs, None
    if doc["run_date"] != Path(run_dir).resolve().name:
        errs.append(f"run_date {doc['run_date']} does not match RUN_DIR name {Path(run_dir).resolve().name!r}")
    for i, it in enumerate(doc["items"]):
        ts = schemas.parse_ts(it["fetched_at"])
        if ts > now + MAX_SKEW:
            errs.append(f"items[{i}].fetched_at in the future: {it['fetched_at']} (now {now.isoformat()})")
        if now - ts > MAX_AGE:
            errs.append(f"items[{i}].fetched_at older than 36 h: {it['fetched_at']} (now {now.isoformat()})")
    return errs, doc


def _write(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _rm(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def validate(run_dir, role, now):
    """Validate and write the marker files. Returns (ok, reasons)."""
    run_dir = Path(run_dir)
    src = run_dir / f"{role}.json"
    ok, rejected, derived = (run_dir / f"{role}.ok", run_dir / f"{role}.rejected",
                             run_dir / f"{role}.derived.json")
    try:
        raw = src.read_bytes()
    except OSError as e:
        errs, doc = [f"cannot read {src.name}: {e.strerror}"], None
    else:
        errs, doc = check_file(run_dir, role, raw, now)
    if errs:
        _rm(ok)
        _rm(derived)
        _write(rejected, "".join(e.replace("\n", " ") + "\n" for e in errs))
        return False, errs
    sha = hashlib.sha256(raw).hexdigest()
    if role == "earnings":
        _write(derived, json.dumps({"role": role, "run_date": doc["run_date"], "sha256": sha,
                                    "tickers": derive_earnings(doc)}, indent=2, sort_keys=True) + "\n")
    else:
        _rm(derived)
    _rm(rejected)
    _write(ok, sha + "\n")
    return True, []


def parse_now(s):
    if s is None:
        return datetime.now(timezone.utc)
    dt = schemas.parse_ts(s)
    if dt is None:
        raise SystemExit("validate: --now must be ISO-8601 with a timezone")
    return dt


def main(argv=None):
    ap = argparse.ArgumentParser(description="validate one scout role file")
    ap.add_argument("run_dir")
    ap.add_argument("role", choices=schemas.ROLES)
    ap.add_argument("--now")
    a = ap.parse_args(argv)
    if not Path(a.run_dir).is_dir():
        print(f"validate: no such RUN_DIR {a.run_dir}", file=sys.stderr)
        return 2
    try:
        now = parse_now(a.now)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    good, errs = validate(a.run_dir, a.role, now)
    if good:
        print(f"OK {a.role}")
        return 0
    print(f"REJECTED {a.role}")
    for e in errs:
        print("  " + e)
    return 1


if __name__ == "__main__":
    sys.exit(main())
