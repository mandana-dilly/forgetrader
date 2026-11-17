"""Barrier: the orchestrator runs only when every scout input is in and valid (D43).

  python scouts/barrier.py RUN_DIR --roles a,b,c --deadline HH:MM --tz Australia/Sydney [--now ISO]

A role is ready only if RUN_DIR/ROLE.ok holds the sha256 of the current ROLE.json, so a
file edited after validation counts as missing. The deadline is HH:MM on the local date
of now in --tz.

  READY    exit 0  every role ready
  WAITING  exit 5  before the deadline; names missing (and rejected) roles
  MISSED   exit 6  at or after the deadline; names missing and rejected roles
  usage    exit 2
"""
import argparse
import hashlib
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scouts import schemas  # noqa: E402

READY, WAITING, MISSED = 0, 5, 6


def role_state(run_dir, role):
    """"ready", "rejected" or "missing" for one role."""
    run_dir = Path(run_dir)
    try:
        sha = hashlib.sha256((run_dir / f"{role}.json").read_bytes()).hexdigest()
        ok = (run_dir / f"{role}.ok").read_text().strip()
    except OSError:
        ok = sha = None
    if ok is not None and ok == sha:
        return "ready"
    if (run_dir / f"{role}.rejected").exists():
        return "rejected"
    return "missing"


def evaluate(run_dir, roles, deadline_hm, tz, now):
    """(exit_code, line)."""
    states = {r: role_state(run_dir, r) for r in roles}
    missing = [r for r in roles if states[r] == "missing"]
    rejected = [r for r in roles if states[r] == "rejected"]
    if not missing and not rejected:
        return READY, f"READY roles={','.join(roles)}"
    local = now.astimezone(tz)
    hh, mm = deadline_hm
    deadline = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    detail = f"missing={','.join(missing) or '-'} rejected={','.join(rejected) or '-'} " \
             f"deadline={deadline.isoformat()}"
    if local < deadline:
        return WAITING, f"WAITING {detail}"
    return MISSED, f"MISSED {detail}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="wait for every scout role to validate")
    ap.add_argument("run_dir")
    ap.add_argument("--roles", required=True)
    ap.add_argument("--deadline", required=True)
    ap.add_argument("--tz", required=True)
    ap.add_argument("--now")
    a = ap.parse_args(argv)
    roles = a.roles.split(",")
    if not roles or any(r not in schemas.ROLES for r in roles) or len(set(roles)) != len(roles):
        print(f"barrier: --roles must be distinct names from {','.join(schemas.ROLES)}", file=sys.stderr)
        return 2
    m = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", a.deadline)
    if not m:
        print("barrier: --deadline must be HH:MM", file=sys.stderr)
        return 2
    try:
        tz = ZoneInfo(a.tz)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"barrier: unknown --tz {a.tz}", file=sys.stderr)
        return 2
    if a.now is None:
        now = datetime.now(timezone.utc)
    else:
        now = schemas.parse_ts(a.now)
        if now is None:
            print("barrier: --now must be ISO-8601 with a timezone", file=sys.stderr)
            return 2
    code, line = evaluate(a.run_dir, roles, (int(m.group(1)), int(m.group(2))), tz, now)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
