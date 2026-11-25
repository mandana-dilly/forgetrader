"""ITM covered-call characterisation (brief wheel-prep-1125 T3, D28 S1 open
decision). Pins CURRENT behaviour; it changes nothing and judges nothing.
Never the network, never a key, never a live file.

The REAL forgetrader.cc_scan.scan_calls and gates.evaluate_call run against
tests/test_cc_gates.py's fake clients (FakeTrading / FakeStock / FakeOption,
FakeContract / FakeSnap / FakeClock) with account A's REAL policy.toml
(FORGE_INSTANCE=a: calls.otm_above_basis_pct 10, DTE 14..35, min_bid 0.10,
min OI 100, spread 0.10 abs and 0.5 of bid, yield floor 7).

Fixture (pinned, synthetic):
  cost_basis 7.00 -> strike floor 7.70 (7.00 x 1.10).
  run clock 2026-12-14 15:30 UTC (Mon 10:30 ET), one expiry 2027-01-08 (dte 25),
  earnings stamp 2027-01-29 (after expiry).
  13 NOK calls, strikes 6.00 to 12.00 step 0.50.
  bid = intrinsic max(spot - K, 0) + time value max(0.20, 0.45 - 0.08 |K - spot|),
  rounded to 0.01; ask = bid + 0.05 (every bid and spread passes).
  Two open-interest shapes, both above the OI floor:
    FLAT    every strike OI 500 (rank falls to the yield tiebreak)
    PEAKED  OI 2000 - 400 |K - spot|, floor 150 (most OI nearest the money)
  spots 7.00, 7.35, 8.40, 10.50.

Per (spot, shape) it pins the contract scan_calls ranks first (the one
run.py:465 proposes), its strike vs spot (ITM / ATM / OTM), bid, the code's
annualized_yield_pct, and the strikes rejected by strike_below_basis_floor.
G1 pins that evaluate_call's pass/fail never depends on the underlying
price: the same contract passes at spots 5.00 and 50.00.
"""
import contextlib
import io
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
sys.path.insert(0, ROOT)
sys.path.insert(0, TESTS)

import test_cc_gates as tcg  # noqa: E402  (sets FORGE_INSTANCE=a; main() not run)

os.environ["FORGE_INSTANCE"] = "a"

import gates  # noqa: E402
import screener  # noqa: E402
from forgetrader import cc_scan  # noqa: E402

UTC = timezone.utc
RUN_TS = datetime(2026, 12, 14, 15, 30, tzinfo=UTC)
EXPIRY = date(2027, 1, 8)
EARNINGS = "2027-01-29"
BASIS = 7.0
STRIKES = [6.0 + 0.5 * i for i in range(13)]
SPOTS = (7.0, 7.35, 8.4, 10.5)

failures = []
n = 0


def check(cond, msg):
    global n
    n += 1
    if not cond:
        failures.append(msg)


def sym(k):
    return f"NOK{EXPIRY.strftime('%y%m%d')}C{int(round(k * 1000)):08d}"


def bid_for(k, spot):
    return round(max(spot - k, 0.0) + max(0.20, 0.45 - 0.08 * abs(k - spot)), 2)


def oi_for(k, spot, shape):
    if shape == "FLAT":
        return 500
    return max(150, int(round(2000 - 400 * abs(k - spot))))


def chain(spot, shape):
    contracts, snaps = [], {}
    for k in STRIKES:
        c = tcg.FakeContract(symbol=sym(k), strike=k, expiry=EXPIRY,
                             oi=str(oi_for(k, spot, shape)))
        contracts.append(c)
        b = bid_for(k, spot)
        snaps[c.symbol] = tcg.FakeSnap(b, round(b + 0.05, 2))
    return contracts, snaps


def scan(spot, shape, policy):
    contracts, snaps = chain(spot, shape)
    tc = tcg.FakeTrading([tcg.FakeResp(contracts)], clock=tcg.FakeClock(RUN_TS))
    sc = tcg.FakeStock({"NOK": tcg.FakeStockSnap(spot, RUN_TS - timedelta(minutes=5))})
    oc = tcg.FakeOption(snaps)
    with tempfile.TemporaryDirectory(prefix="itm_cc_") as tmp:
        wl = tcg._write_wl(tmp, [tcg._wl_entry("NOK", earnings=EARNINGS)])
        with contextlib.redirect_stdout(io.StringIO()):
            res = cc_scan.scan_calls("NOK", BASIS, tc, sc, oc, policy,
                                     tcg.FakeClock(RUN_TS), watchlist_path=wl)
    return res, tc


def moneyness(k, spot):
    return "ATM" if k == spot else ("ITM" if k < spot else "OTM")


# (spot, shape) -> (selected strike, ITM/ATM/OTM, bid, annualized_yield_pct, OI)
# Values are what the code produced at base c20f652 (characterisation).
EXPECTED = {
    (7.0, "FLAT"): (8.0, "OTM", 0.37, 77.1714, 500),
    (7.0, "PEAKED"): (8.0, "OTM", 0.37, 77.1714, 1600),
    (7.35, "FLAT"): (8.0, "OTM", 0.40, 83.4286, 500),
    (7.35, "PEAKED"): (8.0, "OTM", 0.40, 83.4286, 1740),
    (8.4, "FLAT"): (8.0, "ITM", 0.82, 171.0286, 500),
    (8.4, "PEAKED"): (8.5, "OTM", 0.44, 91.7714, 1960),
    (10.5, "FLAT"): (8.0, "ITM", 2.75, 573.5714, 500),
    (10.5, "PEAKED"): (10.5, "ATM", 0.45, 93.8571, 2000),
}
BELOW_FLOOR = [6.0, 6.5, 7.0, 7.5]  # 7.70 floor: 8.00 is the lowest eligible strike


def main():
    policy = screener.load_policy()
    screener.init_throttle(policy["runtime"]["rate_limit_per_min"])
    check(policy["calls"]["otm_above_basis_pct"] == 10, f"policy calls {policy['calls']!r}")
    check((policy["entry"]["dte_min"], policy["entry"]["dte_max"]) == (14, 35),
          f"policy dte {policy['entry']!r}")
    check((EXPIRY - RUN_TS.date()).days == 25, "fixture dte")
    scenarios = 0
    print("spot  shape   selected               strike  vs_spot  bid   yield_pct  oi    "
          "candidates  P&L_if_called")
    for spot in SPOTS:
        for shape in ("FLAT", "PEAKED"):
            scenarios += 1
            tag = f"spot {spot} {shape}"
            res, tc = scan(spot, shape, policy)
            check(res.rejection is None and res.chain_fetch_errors == [],
                  f"{tag} rejection {res.rejection!r} {res.chain_fetch_errors!r}")
            check(tc.order_calls == 0, f"{tag} order call")
            check(len(res.all_rows) == 13, f"{tag} rows {len(res.all_rows)}")
            below = sorted(r["strike"] for r in res.all_rows if not r["strike_pass"])
            check(below == BELOW_FLOOR, f"{tag} below floor {below!r}")
            other = [r["rejected_by"] for r in res.all_rows
                     if r["strike_pass"] and not r["all_pass"]]
            check(other == [], f"{tag} eligible strikes failing another gate {other!r}")
            check(all(r["rejected_by"] == "strike_below_basis_floor"
                      for r in res.all_rows if not r["strike_pass"]),
                  f"{tag} floor rejections carry one token")
            check(len(res.candidates) == 9, f"{tag} candidates {len(res.candidates)}")
            top = res.candidates[0] if res.candidates else {}
            k = top.get("strike")
            got = (k, moneyness(k, spot) if k is not None else None, top.get("bid"),
                   top.get("annualized_yield_pct"), top.get("open_interest"))
            check(got == EXPECTED[(spot, shape)], f"{tag} selected {got!r}")
            check(top.get("contract_symbol") == sym(k) if k else False, f"{tag} symbol")
            check(top.get("strike_floor") == 7.7, f"{tag} floor {top.get('strike_floor')!r}")
            # the yield the code computes is bid / basis x 365 / dte, intrinsic included
            if k is not None:
                y = round(top["bid"] / BASIS * 365 / 25 * 100, 4)
                check(y == top["annualized_yield_pct"], f"{tag} yield formula {y}")
                pnl = round((k - BASIS + top["bid"]) * 100, 2)
                print(f"{spot:<5} {shape:<7} {top['contract_symbol']}  {k:<6.2f}  "
                      f"{moneyness(k, spot):<7}  {top['bid']:<4}  {top['annualized_yield_pct']:<9}  "
                      f"{top['open_interest']:<5} {len(res.candidates):<10}  {pnl}")

    # G1: no spot-relative gate in evaluate_call: the same deep-ITM row passes
    # whatever the underlying price.
    e = policy["entry"]
    c = tcg.FakeContract(symbol=sym(8.0), strike=8.0, expiry=EXPIRY, oi="500")
    rows = [gates.evaluate_call(
        c, tcg.FakeSnap(2.75, 2.80), BASIS, px, RUN_TS - timedelta(minutes=5), RUN_TS,
        RUN_TS.date(), date(2027, 1, 29), e["dte_min"], e["dte_max"],
        policy["calls"]["otm_above_basis_pct"], e["min_bid"], e["min_open_interest"],
        e["max_spread_abs"], e["max_spread_to_bid_ratio"], e["min_annualized_yield_pct"],
        policy["universe"]["max_price_staleness_min"]) for px in (5.0, 10.5, 50.0)]
    check([r["all_pass"] for r in rows] == [True, True, True],
          f"G1 pass by spot {[r['rejected_by'] for r in rows]!r}")
    check(len({r["annualized_yield_pct"] for r in rows}) == 1, "G1 yield depends on spot")
    scenarios += 1

    if failures:
        for f in failures:
            print("  " + f)
        print(f"FAIL: {len(failures)} failed of {n} assertions")
        return 1
    print(f"PASS: test_itm_cc {scenarios} scenarios, {n} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
