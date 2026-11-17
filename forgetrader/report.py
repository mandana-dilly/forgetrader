#!/usr/bin/env python3
"""Brief D38 - state reporter and daily digest. Stdlib only, read-only.

For each account DIR it reads DIR/state.json, DIR/journal.db (sqlite URI
mode=ro), DIR/watchlist.json and `git -C DIR rev-parse --short HEAD`, and
never writes any of them. No broker read, no keys: alerts is imported only on
the digest --send path (alerts.send is the one sender; it reads the Telegram
pair from .env itself).

  python -m forgetrader.report state --out PATH [--as-of TS] DIR [DIR ...]
      PROJECT_STATE.md for all DIRs, written atomically to PATH.
  python -m forgetrader.report digest [--send] [--as-of TS] DIR [DIR ...]
      One message per DIR to stdout; with --send also alerts.send(text) once
      per DIR.

--as-of is ISO8601 and must be tz-aware; default now(UTC). That default is
the ONLY clock read in this module: every date below derives from as-of.

Account label = basename of DIR. ET = America/New_York.
  days to expiry  forgetrader_state._contract_expiry(contract_symbol) minus
                  the ET date of as-of; n/a with no contract.
  collateral      _put_strike_basis(contract_symbol) x 100 at CSP_PENDING /
                  CSP_OPEN, else 0. Utilisation only if state.json carries
                  cash; cash is never inferred.
  runs today      runs rows whose ts falls on the ET date of as-of; expected
                  2 Mon-Fri, 0 Sat/Sun (no holiday calendar).
  HALT window     runs rows with outcome LIKE '%HALT%' (the lifetime
                  count's filter), ts in
                  (16:30 ET on the previous weekday, as-of]. Up to 10 shown.
  fill record     orders rows acknowledged by the broker (broker_order_id
                  set) vs decisions rows recording a fill transition;
                  n/a when the journal has no such columns.
  stamps lapsing  approved watchlist names with review_by within 14 days of
                  the ET date of as-of; past due marked OVERDUE.

Exit codes:
  0  every input read
  1  uncaught exception, or a --send that alerts.send reported as not sent
  2  argparse error, or --as-of not tz-aware
  3  a missing or unreadable state.json, journal.db or watchlist.json (that
     account still gets its section, and its message is still sent)
"""
import argparse
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import forgetrader_state as fs
import watchlist

ET = ZoneInfo("America/New_York")
EXPECTED_RUNS_WEEKDAY = 2
HALT_SHOW = 10
LAST_RUNS = 5
STAMP_DAYS = 14
DIGEST_MAX = 3400  # under 3500 with the instance alert prefix added
OUTCOME_MAX = 200
FILL_ACTIONS = ("CSP_PENDING->CSP_OPEN (fill)", "CC_PENDING->CC_OPEN (fill)")


def _parse_as_of(text):
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"--as-of not ISO8601: {text!r}")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise argparse.ArgumentTypeError(f"--as-of must be tz-aware: {text!r}")
    return dt


def _parse_ts(text):
    """tz-aware datetime from a journal ts, or None (naive or unparseable)."""
    try:
        dt = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None or dt.utcoffset() is None:
        return None
    return dt


def _money(v):
    text = f"{v:.2f}"
    return text[:-3] if text.endswith(".00") else text


def _clip(text, n=OUTCOME_MAX):
    text = str(text)
    return text if len(text) <= n else text[:n - 3] + "..."


def window_start(as_of):
    """16:30 ET on the weekday before the ET date of as-of (Mon -> Fri)."""
    d = as_of.astimezone(ET).date() - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return datetime.combine(d, time(16, 30), tzinfo=ET)


def _git_head(d):
    try:
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        return f"unknown ({type(e).__name__})"
    head = r.stdout.strip()
    return head if r.returncode == 0 and head else "unknown (not a git HEAD)"


def _read_state(d, et_date):
    """Fills the state part of an account dict. Returns the missing marker
    or None."""
    try:
        state = fs.load_state(d / "state.json")
        wheel = state["wheel"]
        if not isinstance(wheel, dict):
            raise TypeError("wheel is not an object")
    except FileNotFoundError:
        return {"missing": "STATE MISSING (state.json not found)"}
    except Exception as e:
        return {"missing": f"STATE MISSING (unreadable: {type(e).__name__})"}

    stage = wheel.get("stage")
    contract = wheel.get("contract_symbol")
    out = {"missing": None, "stage": stage, "ticker": wheel.get("ticker"),
           "contract": contract}

    if contract is None:
        out["dte"] = "n/a (no contract)"
    else:
        expiry = fs._contract_expiry(contract)
        out["dte"] = ("n/a (unparseable contract_symbol)" if expiry is None
                      else f"{(expiry - et_date).days} (expiry {expiry})")

    collateral = 0.0
    if stage in (fs.CSP_PENDING, fs.CSP_OPEN):
        strike, why = fs._put_strike_basis(contract)
        if why:
            out["collateral"] = f"n/a ({why})"
            collateral = None
        else:
            collateral = strike * fs.LOT
            out["collateral"] = _money(collateral)
    else:
        out["collateral"] = "0"

    if "cash" not in state or state["cash"] is None:
        out["util"] = "n/a (cash not in state)"
    else:
        cash = state["cash"]
        if isinstance(cash, bool) or not isinstance(cash, (int, float)):
            out["util"] = "n/a (cash not numeric in state)"
        elif collateral is None:
            out["util"] = "n/a (collateral unknown)"
        elif cash <= 0:
            out["util"] = f"n/a (cash {_money(cash)} in state)"
        else:
            out["util"] = (f"{collateral / cash * 100:.1f}% "
                           f"({_money(collateral)} / cash {_money(cash)})")
    return out


def _read_journal(d, as_of):
    try:
        uri = (d / "journal.db").resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        return {"missing": f"JOURNAL MISSING (cannot open: {type(e).__name__})"}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT ts FROM runs ORDER BY rowid").fetchall()
        halt_rows = conn.execute(
            "SELECT ts, outcome FROM runs WHERE outcome LIKE '%HALT%' ORDER BY rowid"
        ).fetchall()
        lifetime = conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE outcome LIKE '%HALT%'"
        ).fetchone()["n"]
        last = conn.execute(
            f"SELECT ts, outcome FROM runs ORDER BY rowid DESC LIMIT {LAST_RUNS}"
        ).fetchall()
        order_cols = {r["name"] for r in conn.execute("PRAGMA table_info(orders)")}
        dec_cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)")}
        if "broker_order_id" in order_cols and "action" in dec_cols:
            submits = conn.execute(
                "SELECT COUNT(*) AS n FROM orders WHERE broker_order_id IS NOT NULL"
            ).fetchone()["n"]
            fills = conn.execute(
                "SELECT COUNT(*) AS n FROM decisions WHERE action IN (?, ?)",
                FILL_ACTIONS).fetchone()["n"]
            fill = (f"{submits} broker-acknowledged submits, {fills} fills "
                    f"(lifetime, journalled)")
        else:
            fill = "n/a (not journalled)"
    except sqlite3.Error as e:
        return {"missing": f"JOURNAL MISSING (unreadable: {type(e).__name__})"}
    finally:
        conn.close()

    et_date = as_of.astimezone(ET).date()
    start = window_start(as_of)
    today, halts, bad_ts = 0, [], 0
    for r in rows:
        ts = _parse_ts(r["ts"])
        if ts is None:
            bad_ts += 1
            continue
        if ts.astimezone(ET).date() == et_date:
            today += 1
    for r in halt_rows:
        ts = _parse_ts(r["ts"])
        if ts is not None and start < ts <= as_of:
            halts.append((ts, r["ts"], r["outcome"]))
    halts.sort(key=lambda h: h[0])
    return {"missing": None, "runs_today": today, "bad_ts": bad_ts,
            "halts": [(t, o) for _, t, o in halts], "halt_start": start,
            "halt_lifetime": lifetime, "fill": fill,
            "last_runs": [(r["ts"], r["outcome"]) for r in last]}


def _read_stamps(d, as_of):
    now_et = as_of.astimezone(ET)
    try:
        approved, _rejected, _warnings = watchlist.load_watchlist(
            str(d / "watchlist.json"), now_et)
    except Exception as e:
        return {"missing": f"WATCHLIST MISSING ({type(e).__name__})"}
    lapsing = []
    for entry in approved:
        review_by = watchlist._parse_date(entry["review_by"])
        days = (review_by - now_et.date()).days
        if days <= STAMP_DAYS:
            lapsing.append((days, entry["ticker"], review_by))
    lapsing.sort()
    return {"missing": None, "lapsing": lapsing}


def collect(dirs, as_of):
    """One read-only dict per DIR, in argument order."""
    et_date = as_of.astimezone(ET).date()
    accounts = []
    for raw in dirs:
        d = Path(raw)
        accounts.append({
            "label": d.resolve().name,
            "head": _git_head(d),
            "state": _read_state(d, et_date),
            "journal": _read_journal(d, as_of),
            "stamps": _read_stamps(d, as_of),
        })
    return accounts


def head_mismatch(accounts):
    return len(accounts) > 1 and len({a["head"] for a in accounts}) > 1


def any_missing(accounts):
    return any(a[k]["missing"] for a in accounts
               for k in ("state", "journal", "stamps"))


def _section(a, as_of, mismatch, full):
    """Lines for one account. full=True adds the last runs (state output)."""
    et = as_of.astimezone(ET)
    lines = [f"account {a['label']}  HEAD {a['head']}"]
    if mismatch:
        lines.append("HEAD MISMATCH")
    s = a["state"]
    if s["missing"]:
        lines.append(s["missing"])
    else:
        lines += [f"stage {s['stage']}  ticker {s['ticker']}  "
                  f"contract {s['contract']}",
                  f"days to expiry: {s['dte']}",
                  f"committed collateral: {s['collateral']}",
                  f"capital utilisation: {s['util']}"]
    j = a["journal"]
    if j["missing"]:
        lines.append(j["missing"])
    else:
        expected = EXPECTED_RUNS_WEEKDAY if et.weekday() < 5 else 0
        lines.append(f"runs today ({et.date()} ET): {j['runs_today']}/{expected}")
        if j["bad_ts"]:
            lines.append(f"runs rows with unreadable ts: {j['bad_ts']}")
        start = j["halt_start"].strftime("%Y-%m-%d %H:%M %Z")
        lines.append(f"HALT rows since {start}: {len(j['halts'])} "
                     f"(lifetime {j['halt_lifetime']})")
        for ts, outcome in j["halts"][:HALT_SHOW]:
            lines.append(f"  {ts} {_clip(outcome)}")
        if len(j["halts"]) > HALT_SHOW:
            lines.append(f"  +{len(j['halts']) - HALT_SHOW} more")
        lines.append(f"fill record: {j['fill']}")
    w = a["stamps"]
    if w["missing"]:
        lines.append(w["missing"])
    elif not w["lapsing"]:
        lines.append(f"stamps lapsing within {STAMP_DAYS}d: none")
    else:
        lines.append(f"stamps lapsing within {STAMP_DAYS}d:")
        for days, ticker, review_by in w["lapsing"]:
            tag = f"OVERDUE {-days}d" if days < 0 else f"{days}d"
            lines.append(f"  {ticker} review_by {review_by} {tag}")
    if full and not j["missing"]:
        lines.append(f"last {LAST_RUNS} runs:")
        for ts, outcome in j["last_runs"]:
            lines.append(f"  {ts} {_clip(outcome)}")
    return lines


def _as_of_utc(as_of):
    return as_of.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_state(accounts, as_of):
    mismatch = head_mismatch(accounts)
    out = [f"# PROJECT_STATE (GENERATED {_as_of_utc(as_of)}) - do not edit", ""]
    if mismatch:
        out += ["HEAD MISMATCH", ""]
    for a in accounts:
        out.append(f"## {a['label']}")
        out.append("")
        out += _section(a, as_of, mismatch, full=True)
        out.append("")
    return "\n".join(out)


def render_digest(a, as_of, mismatch):
    lines = [f"DIGEST {a['label']} as of {_as_of_utc(as_of)}"]
    lines += _section(a, as_of, mismatch, full=False)
    text = "\n".join(lines)
    if len(text) > DIGEST_MAX:
        text = text[:DIGEST_MAX - 12] + "\n[truncated]"
    return text


def atomic_write(path, text):
    """Temp file in the same dir, fsync, os.replace, fsync the dir."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "x") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as-of", type=_parse_as_of, default=None,
                        help="tz-aware ISO8601 instant; default now (UTC)")
    common.add_argument("dirs", nargs="+", metavar="DIR",
                        help="account directory (state.json, journal.db, "
                             "watchlist.json); label = basename")
    p = argparse.ArgumentParser(prog="forgetrader.report",
                                description="Read-only state report and daily digest.")
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("state", parents=[common], help="write PROJECT_STATE.md")
    st.add_argument("--out", required=True, help="output path (atomic write)")
    dg = sub.add_parser("digest", parents=[common], help="one message per DIR")
    dg.add_argument("--send", action="store_true",
                    help="also alerts.send(text) once per DIR")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    as_of = args.as_of or datetime.now(timezone.utc)  # the only clock read
    accounts = collect(args.dirs, as_of)
    rc = 3 if any_missing(accounts) else 0

    if args.cmd == "state":
        atomic_write(args.out, render_state(accounts, as_of))
        print(f"wrote {args.out}")
    else:
        mismatch = head_mismatch(accounts)
        send = None
        if args.send:
            import alerts  # only here: alerts loads the Telegram pair from .env
            send = alerts.send
        for a in accounts:
            text = render_digest(a, as_of, mismatch)
            print(text)
            print()
            if send is not None and send(text) is not True:
                print(f"WARNING: digest for {a['label']} not sent", file=sys.stderr)
                if rc == 0:
                    rc = 1
    if rc == 3:
        print("EXIT 3: missing or unreadable input", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
