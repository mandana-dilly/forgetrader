"""Test suite for watchlist.py (Brief 13e).

Fixtures are evaluated at a PINNED now_et; field lists are hardcoded here
on purpose (not imported) so a mutation removing a field is caught.
"""

import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watchlist as wl

NY = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
FIX_DIR = Path(__file__).resolve().parent / "watchlist_fixtures"
FIX_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=NY)
TODAY = date(2030, 1, 15)

FIELDS = ["ticker", "added", "earnings_date", "earnings_source",
          "earnings_stamped", "own_100_note", "review_by"]
DATE_FIELDS = ["added", "earnings_date", "earnings_stamped", "review_by"]

FIXTURE_NAMES = sorted([
    "fixture_bad_date.json", "fixture_bad_ticker.json", "fixture_clean.json",
    "fixture_duplicate.json", "fixture_earnings_past.json",
    "fixture_empty_note.json", "fixture_missing_earnings.json",
    "fixture_review_overdue.json",
])


class T:
    def __init__(self):
        self.failures, self.n = [], 0

    def ok(self, cond, msg):
        self.n += 1
        if not cond:
            self.failures.append(msg)


def _sha(p):
    # Absent file hashes to None, so the guard also catches the test creating one.
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def base(**over):
    e = {
        "ticker": "ABC", "added": "2030-01-01", "earnings_date": "2099-01-01",
        "earnings_source": "https://example.com/ir",
        "earnings_stamped": "2030-01-01", "own_100_note": "test note",
        "review_by": "2099-01-01",
    }
    e.update(over)
    return e


def without(field):
    e = base()
    del e[field]
    return e


def raises(fn, exc, substr):
    try:
        fn()
    except exc as e:
        return substr in str(e), f"msg {str(e)!r}"
    except Exception as e:  # wrong type
        return False, f"wrong exception {type(e).__name__}: {e}"
    return False, "no exception"


def main():
    t = T()
    real = [ROOT / "watchlist.json", ROOT / "state.json", ROOT / "journal.db"]
    before = [_sha(p) for p in real]
    tmp = tempfile.mkdtemp()
    try:
        # F: fixtures ------------------------------------------------------
        names = sorted(p.name for p in FIX_DIR.glob("fixture_*.json"))
        t.ok(names == FIXTURE_NAMES, f"F fixture set {names!r}")
        expected = {
            "bad_date": ([], [("MSFT", "bad_date_format:added")], []),
            "bad_ticker": ([], [("aapl", "bad_ticker_format")], []),
            "clean": (["AAPL"], [], []),
            "duplicate": ([], [("MSFT", "duplicate_ticker"),
                               ("MSFT", "duplicate_ticker")], []),
            "earnings_past": ([], [("TSLA", "earnings_date_in_past")], []),
            "empty_note": ([], [("AMD", "empty_own_100_note")], []),
            "missing_earnings": ([], [("NOK", "missing_field:earnings_date")], []),
            "review_overdue": (["NVDA"], [], [("NVDA", "review_overdue")]),
        }
        for name, exp in expected.items():
            a, r, w = wl.load_watchlist(str(FIX_DIR / f"fixture_{name}.json"), FIX_NOW)
            got = (sorted(x["ticker"] for x in a), r, w)
            t.ok(got == exp, f"F {name} got {got!r} want {exp!r}")

        # E: _validate_entry -----------------------------------------------
        v = lambda e: wl._validate_entry(e, TODAY)
        t.ok(v(base()) is None, f"E0 baseline {v(base())!r}")
        for f in FIELDS:
            t.ok(v(without(f)) == f"missing_field:{f}", f"E1 absent {f}: {v(without(f))!r}")
            t.ok(v(base(**{f: None})) == f"missing_field:{f}", f"E1 None {f}")
        for bad in ["abc", "ABCDEF", "", 123, "AB1"]:
            t.ok(v(base(ticker=bad)) == "bad_ticker_format", f"E2 {bad!r}: {v(base(ticker=bad))!r}")
        for ok_t in ["A", "ABCDE"]:
            t.ok(v(base(ticker=ok_t)) is None, f"E2 boundary {ok_t!r}: {v(base(ticker=ok_t))!r}")
        for f in DATE_FIELDS:
            for bad in ["01-15-2030", "2030-02-30", "2030-1-15", 20300115]:
                r = v(base(**{f: bad}))
                t.ok(r == f"bad_date_format:{f}", f"E3 {f}={bad!r}: {r!r}")
        t.ok(v(base(earnings_date="2030-01-14")) == "earnings_date_in_past", "E4 day before")
        t.ok(v(base(earnings_date="2030-01-15")) == "earnings_date_in_past", "E4 == TODAY")
        t.ok(v(base(earnings_date="2030-01-16")) is None, "E4 day after")
        for bad in ["", "   ", 5]:
            r = v(base(own_100_note=bad))
            t.ok(r == "empty_own_100_note", f"E5 {bad!r}: {r!r}")

        # V: _evaluate -------------------------------------------------------
        ev = lambda es: wl._evaluate(es, TODAY)
        a, r, w, _ = ev([base(), base()])
        t.ok(a == [] and r == [("ABC", "duplicate_ticker")] * 2 and w == [], f"V1 {a!r} {r!r} {w!r}")
        a, r, w, _ = ev([base(), base(own_100_note="  ")])
        t.ok(a == [] and r == [("ABC", "duplicate_ticker"), ("ABC", "empty_own_100_note")],
             f"V2 {a!r} {r!r}")
        a, r, w, _ = ev([base(), base(ticker="abc")])
        t.ok([x["ticker"] for x in a] == ["ABC"] and r == [("abc", "bad_ticker_format")],
             f"V3 {a!r} {r!r}")
        a, r, w, _ = ev([base(ticker=123)])
        t.ok(a == [] and r == [("<entry#0>", "bad_ticker_format")], f"V4 {a!r} {r!r}")
        a, r, w, _ = ev([base(review_by="2030-01-14")])
        t.ok(len(a) == 1 and r == [] and w == [("ABC", "review_overdue")], f"V5 {a!r} {r!r} {w!r}")
        a, r, w, _ = ev([base(review_by="2030-01-15")])
        t.ok(len(a) == 1 and r == [] and w == [], f"V6 {a!r} {r!r} {w!r}")
        ok, info = raises(lambda: ev([base(), "not a dict"]),
                          wl.WatchlistStructureError, "is not an object")
        t.ok(ok, f"V7 {info}")

        # S: load_watchlist structure --------------------------------------
        now = datetime(2030, 1, 15, 12, 0, tzinfo=NY)

        def write(name, text):
            p = os.path.join(tmp, name)
            Path(p).write_text(text)
            return p

        cases = [
            ("S1", os.path.join(tmp, "nope.json"), "watchlist file not found"),
            ("S2", write("s2.json", "{"), "invalid JSON"),
            ("S3", write("s3.json", json.dumps({"version": 1, "entries": []})), "top-level shape invalid"),
            ("S4", write("s4.json", json.dumps({"version": "2", "entries": []})), "top-level shape invalid"),
            ("S5", write("s5.json", json.dumps({"version": 2, "entries": {}})), "top-level shape invalid"),
            ("S6", write("s6.json", "[]"), "top-level shape invalid"),
        ]
        for label, path, sub in cases:
            ok, info = raises(lambda p=path: wl.load_watchlist(p, now),
                              wl.WatchlistStructureError, sub)
            t.ok(ok, f"{label} {info}")
        p7 = write("s7.json", json.dumps(
            {"version": 2, "entries": [base(earnings_date="2030-01-15")]}))
        late = datetime(2030, 1, 15, 23, 59, tzinfo=NY)
        a, r, w = wl.load_watchlist(p7, late)
        t.ok(a == [] and r == [("ABC", "earnings_date_in_past")] and w == [],
             f"S7 {a!r} {r!r} {w!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    after = [_sha(p) for p in real]
    for p, b, a_ in zip(real, before, after):
        print(f"isolation {p.name} sha256 before={b} after={a_}")
    t.ok(before == after, "ISOLATION real watchlist.json/state.json/journal.db changed")

    if t.failures:
        print("FAIL")
        for f in t.failures:
            print(f"  {f}")
        print(f"FAIL: {len(t.failures)} failed of {t.n} assertions")
        return 1
    print(f"PASS: test_watchlist {t.n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
