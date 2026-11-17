"""Append-only sqlite store of validated scout facts (D42).

  python scouts/facts.py DB ingest RUN_DIR
  python scouts/facts.py DB earnings-date TICKER
  python scouts/facts.py DB red-flags TICKER

The db path is always an argument; DEFAULT_DB is the conventional name (gitignored).
ingest(RUN_DIR) takes every role file present and refuses the whole run (nothing
written) if any of them lacks a ROLE.ok matching its current sha256. Earnings status is
recomputed from the validated bytes, never read from ROLE.derived.json. Rows are only
ever inserted: triggers abort any UPDATE or DELETE. Re-ingesting the same bytes for a
role and run_date is a no-op.

"Latest" means highest run_date, then latest ingest within that run_date.
earnings_date(ticker) returns the date only if the ticker's latest status is "agree";
conflict, single_source and never-seen all give None (skip).
red_flags(ticker) returns the sorted distinct codes for the ticker in the latest
ingested redflags run ([] if that run does not name it).
"""
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scouts import schemas, validate  # noqa: E402

DEFAULT_DB = "scouts_facts.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT NOT NULL, role TEXT NOT NULL, sha256 TEXT NOT NULL,
    ingested_at TEXT NOT NULL, UNIQUE (run_date, role, sha256));
CREATE TABLE IF NOT EXISTS earnings (
    run_id INTEGER NOT NULL REFERENCES runs(id), ticker TEXT NOT NULL,
    status TEXT NOT NULL, dates TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS redflags (
    run_id INTEGER NOT NULL REFERENCES runs(id), ticker TEXT NOT NULL,
    code TEXT NOT NULL, note TEXT NOT NULL, url TEXT NOT NULL);
"""
APPEND_ONLY = "".join(
    f"CREATE TRIGGER IF NOT EXISTS no_{op.lower()}_{t} BEFORE {op} ON {t} "
    f"BEGIN SELECT RAISE(ABORT, 'facts store is append-only'); END;\n"
    for t in ("runs", "earnings", "redflags") for op in ("UPDATE", "DELETE"))


class NotValidated(Exception):
    pass


class Facts:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA + APPEND_ONLY)

    def close(self):
        self.db.close()

    def ingest(self, run_dir):
        """Ingest every validated role file in RUN_DIR; returns the roles newly stored."""
        run_dir = Path(run_dir)
        found = []
        for role in schemas.ROLES:
            src = run_dir / f"{role}.json"
            if not src.exists():
                continue
            raw = src.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            try:
                ok = (run_dir / f"{role}.ok").read_text().strip()
            except OSError:
                ok = None
            if ok != sha:
                raise NotValidated(f"{role}: no ROLE.ok matching the current {role}.json")
            found.append((role, sha, json.loads(raw)))
        if not found:
            raise NotValidated(f"no role files in {run_dir}")
        stamp = datetime.now(timezone.utc).isoformat()
        stored = []
        with self.db:
            for role, sha, doc in found:
                if self.db.execute("SELECT 1 FROM runs WHERE run_date=? AND role=? AND sha256=?",
                                   (doc["run_date"], role, sha)).fetchone():
                    continue
                run_id = self.db.execute(
                    "INSERT INTO runs (run_date, role, sha256, ingested_at) VALUES (?,?,?,?)",
                    (doc["run_date"], role, sha, stamp)).lastrowid
                if role == "earnings":
                    for it in doc["items"]:
                        status, dates = validate.earnings_status(it["sources"])
                        self.db.execute("INSERT INTO earnings VALUES (?,?,?,?)",
                                        (run_id, it["ticker"], status, ",".join(dates)))
                elif role == "redflags":
                    for it in doc["items"]:
                        for f in it["flags"]:
                            self.db.execute("INSERT INTO redflags VALUES (?,?,?,?,?)",
                                            (run_id, it["ticker"], f["code"], f["note"], f["url"]))
                stored.append(role)
        return stored

    def earnings_status(self, ticker):
        """Latest (status, dates list) for a ticker, or None if never seen."""
        row = self.db.execute(
            "SELECT e.status, e.dates FROM earnings e JOIN runs r ON r.id = e.run_id "
            "WHERE e.ticker = ? ORDER BY r.run_date DESC, r.id DESC LIMIT 1", (ticker,)).fetchone()
        return None if row is None else (row[0], row[1].split(","))

    def earnings_date(self, ticker):
        latest = self.earnings_status(ticker)
        if latest is None:
            return None
        status, dates = latest
        if status != "agree":
            return None
        return dates[0]

    def red_flags(self, ticker):
        run = self.db.execute("SELECT id FROM runs WHERE role = 'redflags' "
                              "ORDER BY run_date DESC, id DESC LIMIT 1").fetchone()
        if run is None:
            return []
        rows = self.db.execute("SELECT DISTINCT code FROM redflags WHERE run_id = ? AND ticker = ? "
                               "ORDER BY code", (run[0], ticker)).fetchall()
        return [r[0] for r in rows]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3 or argv[1] not in ("ingest", "earnings-date", "red-flags"):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    db, cmd, arg = argv
    f = Facts(db)
    try:
        if cmd == "ingest":
            try:
                print("INGESTED " + (",".join(f.ingest(arg)) or "nothing new"))
            except NotValidated as e:
                print(f"REFUSED {e}")
                return 1
        elif cmd == "earnings-date":
            print(f.earnings_date(arg) or "None")
        else:
            print(",".join(f.red_flags(arg)) or "-")
    finally:
        f.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
