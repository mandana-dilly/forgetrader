"""Offline net for the Brief 2 live-submit seams (13f + 13b.3).

  A  T1 seam: the REAL producer (wheel_dryrun.scan_watchlist -> the shared
     gates.evaluate_contract) yields a passing row from alpaca-py model
     objects; that row, unmodified, passes submit._validate. Mutating one
     field at a time is refused, naming its check.
  B  T6 scan clock (D13): given a clock, scan_watchlist never calls
     get_clock and ScanResult.clock IS that object. Given None (the
     standalone wheel_dryrun.main() caller only), it reads exactly one clock.
  C  T3 gate unit: run._submit_gate returns None only when all five
     conditions hold; each condition false alone (and near-miss values such
     as 1 / "True" for True) returns the reason, so nothing is submitted.

Fakes only: no credentials, no TradingClient, no network. The scan's
host-clock staleness reads are pinned by patching wheel_dryrun.datetime;
the watchlist and policy are pinned fixtures written to a mkdtemp dir, so
the real watchlist.json / policy.toml are never read. A guard hashes the
real state.json / journal.db before vs after.
"""
import contextlib
import copy
import datetime
import hashlib
import io
import json
import os
import sys
import tempfile
import types
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from alpaca.data.models.snapshots import OptionsSnapshot, Snapshot
from alpaca.trading.models import OptionContract

import forgetrader_state as fs
import screener
import wheel_dryrun
from forgetrader import submit
from forgetrader.run import _submit_gate

REAL_FILES = [Path(ROOT) / "state.json", Path(ROOT) / "journal.db"]
ET = datetime.timezone(datetime.timedelta(hours=-5))
PINNED_NOW = datetime.datetime(2026, 11, 4, 15, 30, tzinfo=datetime.timezone.utc)
NOW_ET = PINNED_NOW.astimezone(ET)
RUN_DATE = datetime.date(2026, 11, 4)
SYM = "NOK261204P00007500"
AS_OF = PINNED_NOW.isoformat()

# Pinned copy of the policy keys scan_watchlist reads (values as policy.toml
# on 5 Nov 2026). Pinned so a policy edit cannot silently change this seam.
POLICY = {
    "capital": {"reserve_pct": 0.0},
    "entry": {"otm_target_pct": 10, "dte_min": 14, "dte_max": 35,
              "min_bid": 0.10, "min_open_interest": 100,
              "max_spread_abs": 0.10, "max_spread_to_bid_ratio": 0.5,
              "min_annualized_yield_pct": 7},
    "universe": {"min_underlying_price": 5.00, "max_price_staleness_min": 60},
    "runtime": {"rate_limit_per_min": 150, "feed": "iex"},
}

WATCHLIST = {"version": 2, "entries": [{
    "ticker": "NOK", "added": "2026-08-24", "earnings_date": "2027-01-01",
    "earnings_source": "fixture", "earnings_stamped": "2026-11-02",
    "own_100_note": "fixture: willing to own 100 NOK", "review_by": "2026-12-03"}]}

n = 0
failures = []


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)
    return bool(cond)


def sha(p):
    p = Path(p)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


class PinnedDT(datetime.datetime):
    """wheel_dryrun's staleness reads datetime.now(timezone.utc)."""

    @classmethod
    def now(cls, tz=None):
        return PINNED_NOW.astimezone(tz) if tz else PINNED_NOW.replace(tzinfo=None)


def _contract():
    return OptionContract(
        id=str(uuid.uuid4()), symbol=SYM, name="NOK Dec 4 2026 7.5 Put",
        status="active", tradable=True, expiration_date="2026-12-04",
        root_symbol="NOK", underlying_symbol="NOK",
        underlying_asset_id=str(uuid.uuid4()), type="put", style="american",
        strike_price=7.5, size="100", open_interest="655")


def _ts(minutes_before):
    return (PINNED_NOW - datetime.timedelta(minutes=minutes_before)).isoformat()


class FakeTrading:
    def __init__(self):
        self.clock_calls = 0
        self.clock = types.SimpleNamespace(timestamp=PINNED_NOW, is_open=True)

    def get_clock(self):
        self.clock_calls += 1
        return self.clock

    def get_account(self):
        return types.SimpleNamespace(cash="5000")

    def get_option_contracts(self, req):
        return types.SimpleNamespace(option_contracts=[_contract()],
                                     next_page_token=None)

    def submit_order(self, req):
        raise AssertionError("TRIPWIRE: the scan must never submit")


class FakeStock:
    def get_stock_snapshot(self, req):
        return {"NOK": Snapshot("NOK", {"latestTrade": {
            "t": _ts(2), "p": 8.5, "s": 100, "x": "V", "i": 1, "c": ["@"], "z": "A"}})}


class FakeOption:
    def get_option_chain(self, req):
        return {SYM: OptionsSnapshot(SYM, {"latestQuote": {
            "t": _ts(1), "bp": 0.16, "bs": 10, "bx": "C",
            "ap": 0.20, "as": 10, "ax": "C", "c": "A"}})}


def _scan(clock):
    """Run the real scan_watchlist against the fakes; returns (result, tc)."""
    tmp = Path(tempfile.mkdtemp(prefix="live_submit_"))
    wl = tmp / "watchlist.json"
    wl.write_text(json.dumps(WATCHLIST))
    tc = FakeTrading()
    orig_wl, orig_dt = wheel_dryrun.WATCHLIST_PATH, wheel_dryrun.datetime
    try:
        wheel_dryrun.WATCHLIST_PATH = wl
        wheel_dryrun.datetime = PinnedDT
        screener.init_throttle(POLICY["runtime"]["rate_limit_per_min"])
        with contextlib.redirect_stdout(io.StringIO()):
            res = wheel_dryrun.scan_watchlist(
                tc, FakeStock(), FakeOption(), POLICY, NOW_ET, RUN_DATE,
                clock=clock)
        return res, tc
    finally:
        wheel_dryrun.WATCHLIST_PATH, wheel_dryrun.datetime = orig_wl, orig_dt


def part_a():
    run_clock = types.SimpleNamespace(timestamp=PINNED_NOW, is_open=True)
    res, _tc = _scan(run_clock)
    check(len(res.passing_rows) == 1, f"A producer passing rows {len(res.passing_rows)} "
          f"rejects={dict(res.reject_tally)!r}")
    if not res.passing_rows:
        return
    row = res.passing_rows[0]
    check(row["final_pass"] is True and type(row["bid"]) is float,
          f"A row final_pass={row['final_pass']!r} bid type={type(row['bid']).__name__}")
    flat = fs.fresh_flat_state()
    lp, failed = submit._validate(flat, row, AS_OF)
    check(failed is None and lp == Decimal("0.16"),
          f"A real row refused by _validate: {failed!r} lp={lp!r}")

    # One field at a time; each refusal names its check.
    muts = [
        ("final_pass False", {"final_pass": False}, "final_pass_not_true"),
        ("final_pass str", {"final_pass": "True"}, "final_pass_not_true"),
        ("final_pass missing", {"final_pass": None}, "final_pass_not_true"),
        ("contract_symbol short", {"contract_symbol": "NOK"}, "contract_symbol_invalid"),
        ("contract_symbol empty str", {"contract_symbol": ""}, "contract_symbol_invalid"),
        ("underlying_symbol other", {"underlying_symbol": "F"}, "underlying_mismatch"),
        ("underlying_symbol empty", {"underlying_symbol": ""}, "underlying_mismatch"),
        ("contract right C", {"contract_symbol": SYM.replace("P0", "C0")}, "right_not_put"),
        ("bid str", {"bid": "0.16"}, "bid_not_float"),
        ("bid empty (gates missing value)", {"bid": ""}, "bid_not_float"),
        ("bid Decimal", {"bid": Decimal("0.16")}, "bid_not_float"),
        ("bid 3dp", {"bid": 0.155}, "bid_not_2dp"),
        ("bid zero", {"bid": 0.0}, "bid_not_2dp"),
    ]
    for label, patch, want in muts:
        r = copy.deepcopy(row)
        for k, v in patch.items():
            if v is None:
                r.pop(k, None)
            else:
                r[k] = v
        lp, failed = submit._validate(flat, r, AS_OF)
        check(lp is None and failed == want, f"A mutation {label}: got {failed!r} want {want!r}")
    not_flat = fs.fresh_flat_state()
    not_flat["wheel"]["stage"] = fs.CSP_PENDING
    check(submit._validate(not_flat, row, AS_OF) == (None, "stage_not_flat"),
          "A mutation stage: not refused as stage_not_flat")
    check(submit._validate(flat, row, "") == (None, "as_of_ts_missing"),
          "A mutation as_of_ts: not refused as as_of_ts_missing")


def part_b():
    run_clock = types.SimpleNamespace(timestamp=PINNED_NOW, is_open=True)
    res, tc = _scan(run_clock)
    check(tc.clock_calls == 0, f"B clock given: scan called get_clock {tc.clock_calls}x")
    check(res.clock is run_clock, "B clock given: ScanResult.clock is not the passed object")
    res, tc = _scan(None)
    check(tc.clock_calls == 1, f"B clock None: get_clock calls {tc.clock_calls} != 1")
    check(res.clock is tc.clock, "B clock None: ScanResult.clock is not the scan's own read")
    print(f"B clock=None behaviour: scan reads its own clock, get_clock calls={tc.clock_calls}")


def part_c():
    good = {"final_pass": True, "contract_symbol": SYM}
    ok = dict(live=True, paper=True, action="noop:FLAT stable", proposal=good, is_open=True)
    check(_submit_gate(**ok) is None, f"C all true: {_submit_gate(**ok)!r}")
    cases = [
        ("live False", {"live": False}, "dry_run"),
        ("live 1", {"live": 1}, "dry_run"),
        ("paper False", {"paper": False}, "refused:not_paper"),
        ("paper 'True'", {"paper": "True"}, "refused:not_paper"),
        ("action transition", {"action": "CSP_PENDING->FLAT (cancel_or_expire)"},
         "skip:not_flat_stable"),
        ("action trailing space", {"action": "noop:FLAT stable "}, "skip:not_flat_stable"),
        ("is_open False", {"is_open": False}, "skip:market_closed"),
        ("is_open None", {"is_open": None}, "skip:market_closed"),
        ("is_open 1", {"is_open": 1}, "skip:market_closed"),
        ("proposal None", {"proposal": None}, "skip:no_passing_proposal"),
        ("final_pass False", {"proposal": dict(good, final_pass=False)},
         "skip:no_passing_proposal"),
        ("final_pass 1", {"proposal": dict(good, final_pass=1)}, "skip:no_passing_proposal"),
        ("final_pass absent", {"proposal": {"contract_symbol": SYM}},
         "skip:no_passing_proposal"),
    ]
    for label, patch, want in cases:
        got = _submit_gate(**dict(ok, **patch))
        check(got == want, f"C {label}: got {got!r} want {want!r}")

    # B6: the same gate for place_cc (HOLDING stable, candidate all_pass).
    cc = {"all_pass": True, "contract_symbol": "NOK261218C00028000"}
    hok = dict(ok, action="noop:HOLDING stable", proposal=cc,
               stable_action="noop:HOLDING stable", pass_key="all_pass")
    check(_submit_gate(**hok) is None, f"C HOLDING all true: {_submit_gate(**hok)!r}")
    hcases = [
        ("HOLDING action FLAT", {"action": "noop:FLAT stable"}, "skip:not_holding_stable"),
        ("HOLDING transition", {"action": "CSP_OPEN->HOLDING (assigned)"},
         "skip:not_holding_stable"),
        ("HOLDING is_open False", {"is_open": False}, "skip:market_closed"),
        ("HOLDING final_pass only", {"proposal": {"final_pass": True}},
         "skip:no_passing_proposal"),
        ("HOLDING all_pass 'True'", {"proposal": dict(cc, all_pass="True")},
         "skip:no_passing_proposal"),
        ("HOLDING live False", {"live": False}, "dry_run"),
        ("HOLDING paper False", {"paper": False}, "refused:not_paper"),
    ]
    for label, patch, want in hcases:
        got = _submit_gate(**dict(hok, **patch))
        check(got == want, f"C {label}: got {got!r} want {want!r}")


def main():
    before = [sha(p) for p in REAL_FILES]
    for part in (part_a, part_b, part_c):
        try:
            part()
        except Exception as e:  # a crash is reported, never a silent pass
            check(False, f"{part.__name__} crashed: {type(e).__name__}: {e}")
    after = [sha(p) for p in REAL_FILES]
    print(f"isolation state.json/journal.db sha256 before={before} after={after}")
    check(before == after, "ISOLATION: real state.json / journal.db changed")
    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_live_submit seam + scan clock + gate, {n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
