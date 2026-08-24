"""Watchlist schema + fail-closed validator.

This is the human approval gate for ForgeTrader: one decision per ticker,
made off the market-hours path. watchlist.json membership is meant to be
the first gate in the future engine's gate chain, but no engine exists
yet -- this module only defines and validates the schema in isolation.

Alpaca has no earnings calendar endpoint, so earnings_date is a
human-stamped field. A missing or stale stamp is a HARD BLOCK: selling a
put whose expiry crosses an earnings event is a mechanical way to lose
money.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from zoneinfo import ZoneInfo

TICKER_RE = re.compile(r"^[A-Z]{1,5}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

REQUIRED_FIELDS = [
    "ticker",
    "added",
    "earnings_date",
    "earnings_source",
    "earnings_stamped",
    "own_100_note",
    "review_by",
]

DATE_FIELDS = ["added", "earnings_date", "earnings_stamped", "review_by"]


class WatchlistStructureError(Exception):
    """Raised when the file itself is unreadable: missing, invalid JSON,
    or top-level shape not matching {version: 2, entries: [...]}."""


def _parse_date(value) -> date | None:
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _validate_entry(entry: dict, now_et_date: date) -> str | None:
    """Return a hard-block reason string, or None if the entry passes
    every hard-block check (still may earn an advisory warning later)."""
    for field in REQUIRED_FIELDS:
        if field not in entry or entry[field] is None:
            return f"missing_field:{field}"

    ticker = entry["ticker"]
    if not isinstance(ticker, str) or not TICKER_RE.match(ticker):
        return "bad_ticker_format"

    parsed = {}
    for field in DATE_FIELDS:
        d = _parse_date(entry[field])
        if d is None:
            return f"bad_date_format:{field}"
        parsed[field] = d

    if parsed["earnings_date"] <= now_et_date:
        return "earnings_date_in_past"

    note = entry["own_100_note"]
    if not isinstance(note, str) or not note.strip():
        return "empty_own_100_note"

    return None


def _evaluate(entries: list, now_et_date: date):
    """Run hard-block + advisory checks over the entry list.

    Returns (approved, rejected, warnings, records) where records is the
    full per-entry decision log in original file order, used by the CLI
    for printing.
    """
    approved = []
    rejected = []
    warnings = []
    records = []  # (ticker_display, status, detail)

    local = []  # (entry, ticker_display, reason_or_None)
    ticker_counter = Counter()

    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise WatchlistStructureError(f"entry {idx} is not an object")

        ticker_val = entry.get("ticker")
        ticker_display = ticker_val if isinstance(ticker_val, str) else f"<entry#{idx}>"

        if isinstance(ticker_val, str) and TICKER_RE.match(ticker_val):
            ticker_counter[ticker_val] += 1

        reason = _validate_entry(entry, now_et_date)
        local.append((entry, ticker_display, reason))

    for entry, ticker_display, reason in local:
        if reason is None and ticker_counter[entry["ticker"]] > 1:
            reason = "duplicate_ticker"

        if reason is not None:
            rejected.append((ticker_display, reason))
            records.append((ticker_display, "REJECTED", reason))
            continue

        review_by = _parse_date(entry["review_by"])
        if review_by < now_et_date:
            warnings.append((ticker_display, "review_overdue"))
            approved.append(entry)
            records.append((ticker_display, "APPROVED_WARNING", "review_overdue"))
        else:
            approved.append(entry)
            records.append((ticker_display, "APPROVED", None))

    return approved, rejected, warnings, records


def _load_and_evaluate(path: str, now_et: datetime):
    try:
        with open(path, "r") as f:
            text = f.read()
    except FileNotFoundError as e:
        raise WatchlistStructureError(f"watchlist file not found: {path}") from e

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise WatchlistStructureError(f"invalid JSON in {path}: {e}") from e

    if (
        not isinstance(data, dict)
        or data.get("version") != 2
        or not isinstance(data.get("entries"), list)
    ):
        raise WatchlistStructureError(
            f"top-level shape invalid in {path}: expected {{version: 2, entries: [...]}}"
        )

    return _evaluate(data["entries"], now_et.date())


def load_watchlist(path: str, now_et: datetime):
    """Validate the watchlist at `path` against a fixed `now_et`
    (a tz-aware datetime in America/New_York).

    Returns (approved, rejected, warnings):
      approved: list of entry dicts that may trade.
      rejected: list of (ticker, reason) tuples -- hard blocked.
      warnings: list of (ticker, warning) tuples -- trades, advisory only.

    Raises WatchlistStructureError if the file itself is unreadable:
    missing, invalid JSON, or top-level shape mismatch. Never raises for
    a bad individual entry -- those are rejected or warned, not thrown.
    """
    approved, rejected, warnings, _records = _load_and_evaluate(path, now_et)
    return approved, rejected, warnings


def main(argv):
    path = argv[1] if len(argv) > 1 else "watchlist.json"
    now_et = datetime.now(ZoneInfo("America/New_York"))

    try:
        approved, rejected, warnings, records = _load_and_evaluate(path, now_et)
    except WatchlistStructureError as e:
        print(f"STRUCTURAL ERROR: {e}")
        sys.exit(1)

    for ticker_display, status, detail in records:
        if status == "APPROVED":
            print(f"APPROVED  {ticker_display}")
        elif status == "APPROVED_WARNING":
            print(f"APPROVED  {ticker_display}  (WARNING: {detail})")
        else:
            print(f"REJECTED  {ticker_display}  {detail}")

    print(
        f"SUMMARY approved={len(approved)} rejected={len(rejected)} "
        f"warnings={len(warnings)}"
    )
    sys.exit(1 if rejected else 0)


if __name__ == "__main__":
    main(sys.argv)
