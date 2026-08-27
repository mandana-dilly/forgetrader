"""Golden-fixture regression harness for gates.py (Brief 7b).

gates.py holds the sole gate chain for ForgeTrader's screener and future
engine. This harness freezes its current, human-verified behaviour so any
later edit that changes a gate's output is caught.

`evaluate_contract` is a pure function: it only reads attributes off its
`contract` and `snap` arguments and returns a dict of CSV-ready primitives.
No network, no methods called on those objects. So the test inputs are
plain Python stand-in objects with the right attributes (FakeContract /
FakeSnap / FakeQuote below) -- NOT live SDK objects, NOT JSON files.

Exact type contract the fixtures honour (from the real call site,
screener.py, and the function body):

    contract.expiration_date  -> datetime.date   (date arithmetic vs `today`)
    today                     -> datetime.date
    contract.strike_price     -> float
    contract.root_symbol / underlying_symbol / symbol -> str
    contract.open_interest    -> str of digits, or None  (parse_open_interest
                                 does int(raw_oi); the live API returns a
                                 string or None)
    snap                      -> None, or an object whose .latest_quote is
                                 None or has .bid_price / .ask_price
                                 (each float or None)
    underlying_price          -> float
    underlying_price_ts       -> str  (screener passes .isoformat())
    underlying_staleness_min  -> float
    thresholds                -> numeric, current policy.toml values

Pinned harness date
-------------------
`today` is date(2026, 1, 15), hard-pinned. Fixtures must NOT call
date.today(): the watchlist fixtures anchor to the wall clock and rot as
real time moves past their chosen dates. A pinned harness date never
expires.

Baseline arithmetic (all nine gates pass against current policy.toml)
--------------------------------------------------------------------
    today            = 2026-01-15
    expiration_date  = 2026-02-12       -> dte = 28        (14 <= 28 <= 35)
    underlying_price = 30.0
    strike_price     = 26.0             -> otm_pct = (30-26)/30*100
                                                   = 13.33%  (>= 10)
    collateral       = 26.0 * 100 = 2600.0
    available        = 3000.0           -> 2600 <= 3000
    bid / ask        = 0.50 / 0.55      -> spread_abs = 0.05
                                          (<= 0.10 and <= 0.5*0.50 = 0.25)
    open_interest    = "150"            -> 150            (>= 100)
    staleness_min    = 10.0             -> 10 <= 60
    yield            = (0.50*100 / 2600) * (365/28) * 100
                                        = 25.07%          (>= 7)

`available` is not a policy.toml key -- in screener.py it is a runtime
figure derived from account cash (compute_collateral_numbers). The harness
pins it to 3000.0, above the baseline collateral, so the baseline clears
the collateral gate and the `collateral` fixture can drop it to isolate
that gate.

Every fixture is the baseline with exactly one thing overridden, so a
regression names itself. Where the function structurally cascades (a
missing bid also leaves spread and yield undefined; a sub-floor bid also
sinks yield) the extra tokens are noted inline -- they are the real
behaviour and are frozen as-is.

GOLDENS are empty here on purpose. Phase 2 (run by a human, through the
terminal) captures and freezes them: freezing a golden is a verification
act, which Claude Code cannot perform.

Style follows watchlist.py: plain asserts via comparison, a main(argv),
__main__ guard, no pytest (not installed).
"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gates import evaluate_contract


class FakeQuote:
    def __init__(self, bid_price, ask_price):
        self.bid_price = bid_price
        self.ask_price = ask_price


class FakeSnap:
    def __init__(self, latest_quote):
        self.latest_quote = latest_quote


class FakeContract:
    def __init__(self, *, symbol, root_symbol, underlying_symbol,
                 strike_price, expiration_date, open_interest):
        self.symbol = symbol
        self.root_symbol = root_symbol
        self.underlying_symbol = underlying_symbol
        self.strike_price = strike_price
        self.expiration_date = expiration_date
        self.open_interest = open_interest


# --- baseline -------------------------------------------------------------
BASE_CONTRACT = dict(
    symbol="ABC260212P00026000",
    root_symbol="ABC",
    underlying_symbol="ABC",
    strike_price=26.0,
    expiration_date=date(2026, 2, 12),
    open_interest="150",
)

BASE_KWARGS = dict(
    underlying_price=30.0,
    underlying_price_ts="2026-01-15T14:30:00+00:00",
    underlying_staleness_min=10.0,
    today=date(2026, 1, 15),
    dte_min=14,
    dte_max=35,
    otm_target_pct=10,
    min_bid=0.10,
    min_oi=100,
    max_spread_abs=0.10,
    max_spread_to_bid_ratio=0.5,
    min_yield_pct=7,
    available=3000.0,
    max_staleness_min=60,
)

BASE_BID, BASE_ASK = 0.50, 0.55


def make_baseline():
    """Return (contract_kwargs, call_kwargs, (bid, ask)) for a contract that
    passes every gate. Each fixture is this with one field overridden."""
    return dict(BASE_CONTRACT), dict(BASE_KWARGS), (BASE_BID, BASE_ASK)


def _mk(*, contract_over=None, kwargs_over=None,
        quote=(BASE_BID, BASE_ASK), snap_mode="quote"):
    """Build one fixture tuple (contract, snap, call_kwargs).

    snap_mode: "quote"      -> FakeSnap(FakeQuote(*quote))
               "none"       -> snap is None
               "null_quote" -> FakeSnap(None)
    """
    c_kw, k_kw, _ = make_baseline()
    if contract_over:
        c_kw.update(contract_over)
    if kwargs_over:
        k_kw.update(kwargs_over)
    contract = FakeContract(**c_kw)
    if snap_mode == "none":
        snap = None
    elif snap_mode == "null_quote":
        snap = FakeSnap(None)
    else:
        snap = FakeSnap(FakeQuote(quote[0], quote[1]))
    return contract, snap, k_kw


# --- fixtures: baseline + one override, one isolated reject each ---------
#
# spread_ratio (the subtle one): bid 0.17, ask 0.26 -> spread_abs = 0.09.
#   absolute gate: 0.09 <= max_spread_abs (0.10)                 -> PASSES
#   ratio gate:    0.09 >  max_spread_to_bid_ratio * bid
#                       =  0.5 * 0.17 = 0.085                     -> FAILS
#                                                        (spread_wide_vs_bid)
#   bid gate:      0.17 >= min_bid (0.10)                        -> PASSES
#   yield:         (0.17*100 / 2600) * (365/28) * 100 = 8.52%
#                       >= min_yield_pct (7)                     -> PASSES
#   Isolated: only the ratio gate trips.
#
# yield_low: bid 0.12 (>= min_bid, bid gate passes), ask 0.15
#   (spread_abs 0.03 <= 0.10 and <= 0.5*0.12 = 0.06, both spread gates pass).
#   yield = (0.12*100 / 2600) * (365/28) * 100 = 6.02% < 7  -> yield_too_low.
#
# bid_low: bid 0.05 (< min_bid) -> bid_too_low. strike lowered to 5.0
#   (collateral 500) so the sub-floor bid still clears the yield gate --
#   at the baseline strike (collateral 2600) any bid under 0.10 also sinks
#   yield below 7%, so the two are arithmetically inseparable there.
#   yield = (0.05*100 / 500) * (365/28) * 100 = 13.04% >= 7  -> PASSES.
#   ask 0.07 keeps both spread gates passing (spread_abs 0.02 <= 0.10 and
#   <= 0.5*0.05 = 0.025). strike 5.0 vs underlying 30 is ~83% OTM, so the
#   otm gate still passes. Isolated: bid_too_low is the only trip.
#
# no_snap / null_quote / null_bid: with bid unavailable, the spread and yield
#   branches both guard on `bid is not None`, so each fixture also emits
#   undefined_metric_spread and undefined_metric_yield. That cascade is the
#   function's real behaviour and is frozen as-is in Phase 2.
#
# otm_low: strike 29.4 vs underlying 30 -> otm_pct = 2.0% < 10. collateral
#   29.4*100 = 2940 <= available 3000, so the collateral gate still passes.
#
# collateral: override `available` down to 2500.0 (< baseline collateral
#   2600). Raising strike instead would push it toward the money and also
#   trip otm_below_target; lowering available isolates the collateral gate.
FIXTURES = {
    "clean":        _mk(),
    "adjusted":     _mk(contract_over={"root_symbol": "XYZ1"}),
    "stale":        _mk(kwargs_over={"underlying_staleness_min": 90.0}),
    "dte_short":    _mk(contract_over={"expiration_date": date(2026, 1, 20)}),
    "otm_low":      _mk(contract_over={"strike_price": 29.4}),
    "no_snap":      _mk(snap_mode="none"),
    "null_quote":   _mk(snap_mode="null_quote"),
    "null_bid":     _mk(quote=(None, 0.55)),
    "bid_low":      _mk(contract_over={"strike_price": 5.0}, quote=(0.05, 0.07)),
    "null_oi":      _mk(contract_over={"open_interest": None}),
    "oi_low":       _mk(contract_over={"open_interest": "50"}),
    "spread_abs":   _mk(quote=(0.50, 0.65)),
    "spread_ratio": _mk(quote=(0.17, 0.26)),
    "yield_low":    _mk(quote=(0.12, 0.15)),
    "collateral":   _mk(kwargs_over={"available": 2500.0}),
}


GOLDENS = {'adjusted': {'adjusted_pass': False, 'all_pass': False, 'annualized_yield_pct': 25.0687, 'ask': 0.55, 'bid': 0.5, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.525, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'adjusted_contract', 'root_symbol': 'XYZ1', 'spread_abs': 0.05, 'spread_pass': True, 'spread_pct': 9.5238, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'bid_low': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 13.0357, 'ask': 0.07, 'bid': 0.05, 'bid_pass': False, 'collateral': 500.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.06, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 83.3333, 'rejected_by': 'bid_too_low', 'root_symbol': 'ABC', 'spread_abs': 0.02, 'spread_pass': True, 'spread_pct': 33.3333, 'staleness_pass': True, 'strike': 5.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'clean': {'adjusted_pass': True, 'all_pass': True, 'annualized_yield_pct': 25.0687, 'ask': 0.55, 'bid': 0.5, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.525, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': '', 'root_symbol': 'ABC', 'spread_abs': 0.05, 'spread_pass': True, 'spread_pct': 9.5238, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'collateral': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 25.0687, 'ask': 0.55, 'bid': 0.5, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': False, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.525, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'collateral_exceeds_available', 'root_symbol': 'ABC', 'spread_abs': 0.05, 'spread_pass': True, 'spread_pct': 9.5238, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'dte_short': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 140.3846, 'ask': 0.55, 'bid': 0.5, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 5, 'dte_pass': False, 'expiration_date': '2026-01-20', 'mid': 0.525, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'dte', 'root_symbol': 'ABC', 'spread_abs': 0.05, 'spread_pass': True, 'spread_pct': 9.5238, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'no_snap': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': '', 'ask': '', 'bid': '', 'bid_pass': False, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': '', 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'no_snapshot;undefined_metric_spread;undefined_metric_yield', 'root_symbol': 'ABC', 'spread_abs': '', 'spread_pass': False, 'spread_pct': '', 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': False}, 'null_bid': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': '', 'ask': 0.55, 'bid': '', 'bid_pass': False, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': '', 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'null_bid;undefined_metric_spread;undefined_metric_yield', 'root_symbol': 'ABC', 'spread_abs': '', 'spread_pass': False, 'spread_pct': '', 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': False}, 'null_oi': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 25.0687, 'ask': 0.55, 'bid': 0.5, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.525, 'oi_pass': False, 'open_interest': '', 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'null_oi', 'root_symbol': 'ABC', 'spread_abs': 0.05, 'spread_pass': True, 'spread_pct': 9.5238, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'null_quote': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': '', 'ask': '', 'bid': '', 'bid_pass': False, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': '', 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'null_quote;undefined_metric_spread;undefined_metric_yield', 'root_symbol': 'ABC', 'spread_abs': '', 'spread_pass': False, 'spread_pct': '', 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': False}, 'oi_low': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 25.0687, 'ask': 0.55, 'bid': 0.5, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.525, 'oi_pass': False, 'open_interest': 50, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'oi_too_low', 'root_symbol': 'ABC', 'spread_abs': 0.05, 'spread_pass': True, 'spread_pct': 9.5238, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'otm_low': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 22.1696, 'ask': 0.55, 'bid': 0.5, 'bid_pass': True, 'collateral': 2940.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.525, 'oi_pass': True, 'open_interest': 150, 'otm_pass': False, 'otm_pct': 2.0, 'rejected_by': 'otm_below_target', 'root_symbol': 'ABC', 'spread_abs': 0.05, 'spread_pass': True, 'spread_pct': 9.5238, 'staleness_pass': True, 'strike': 29.4, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'spread_abs': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 25.0687, 'ask': 0.65, 'bid': 0.5, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.575, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'spread_too_wide', 'root_symbol': 'ABC', 'spread_abs': 0.15, 'spread_pass': False, 'spread_pct': 26.087, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'spread_ratio': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 8.5234, 'ask': 0.26, 'bid': 0.17, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.215, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'spread_wide_vs_bid', 'root_symbol': 'ABC', 'spread_abs': 0.09, 'spread_pass': False, 'spread_pct': 41.8605, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': True}, 'stale': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 25.0687, 'ask': 0.55, 'bid': 0.5, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.525, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'stale_underlying_price', 'root_symbol': 'ABC', 'spread_abs': 0.05, 'spread_pass': True, 'spread_pct': 9.5238, 'staleness_pass': False, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 90.0, 'yield_pass': True}, 'yield_low': {'adjusted_pass': True, 'all_pass': False, 'annualized_yield_pct': 6.0165, 'ask': 0.15, 'bid': 0.12, 'bid_pass': True, 'collateral': 2600.0, 'collateral_pass': True, 'contract_symbol': 'ABC260212P00026000', 'dte': 28, 'dte_pass': True, 'expiration_date': '2026-02-12', 'mid': 0.135, 'oi_pass': True, 'open_interest': 150, 'otm_pass': True, 'otm_pct': 13.3333, 'rejected_by': 'yield_too_low', 'root_symbol': 'ABC', 'spread_abs': 0.03, 'spread_pass': True, 'spread_pct': 22.2222, 'staleness_pass': True, 'strike': 26.0, 'underlying_price': 30.0, 'underlying_price_ts': '2026-01-15T14:30:00+00:00', 'underlying_staleness_min': 10.0, 'yield_pass': False}}


def _jsonable(v):
    """Coerce any non-JSON type. The dict is already CSV-ready primitives
    (str / int / float / bool), so this is a belt-and-braces pass-through."""
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return repr(v)


def run_all():
    results = {}
    for name, (contract, snap, kwargs) in FIXTURES.items():
        results[name] = evaluate_contract(contract, snap, **kwargs)
    return results


def main(argv):
    results = run_all()

    if "--capture" in argv:
        blob = {
            k: {kk: _jsonable(vv) for kk, vv in v.items()}
            for k, v in results.items()
        }
        print(json.dumps(blob, indent=2, sort_keys=True))
        return 0

    # assert mode
    failures = []
    for name, expected in GOLDENS.items():
        got = results[name]
        for k, exp in expected.items():
            if got.get(k) != exp:
                failures.append(f"{name}.{k}: expected {exp!r} got {got.get(k)!r}")

    if failures:
        print("FAIL")
        for f in failures:
            print("  " + f)
        return 1

    print(f"PASS: {len(GOLDENS)} fixtures, "
          f"{sum(len(e) for e in GOLDENS.values())} field assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
