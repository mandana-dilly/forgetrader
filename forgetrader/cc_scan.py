"""Brief B5 - Stage 2 producer: covered-call scan, dry and read-only.

scan_calls() evaluates the CALL chain of one held ticker against every
covered-call gate (gates.evaluate_call) and ranks the survivors by
open_interest desc, yield tiebreak. It does no printing, no file writing and
no sys.exit, and never reads a clock: the caller passes the run clock (D13).

The standalone CLI

    .venv/bin/python -m forgetrader.cc_scan --ticker T --basis 25.00

reads the broker clock ONCE, makes read-only data calls, prints the ranked
candidates and top_rejects, and writes NOTHING (no state, no journal, no
scans/). This module never imports forgetrader.submit and has no order path.

Exit codes: 0 ok (candidates or a correct empty result), 3 bad args or clock
invalid, 4 watchlist structure (D7 HALT class; nothing is journalled because
this CLI writes nothing), 1 uncaught (auth / network).
"""
import argparse
import math
import sys
from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

import credentials
import screener
import watchlist
from gates import FAILS_DELIM, evaluate_call

WATCHLIST_PATH = Path(__file__).resolve().parent.parent / "watchlist.json"
ET = ZoneInfo("America/New_York")


class ClockInvalid(ValueError):
    """The run clock is missing or its timestamp is tz-naive."""


class CCScanResult:
    """Everything one scan_calls() pass produced. No behaviour - just fields.

    candidates - all_pass rows, ranked open_interest desc, yield tiebreak.
    all_rows   - every evaluate_call row.
    reject_tally - Counter of reject tokens over every failing row (D9), plus
                 one "name:<token>" for a ticker-level rejection.
    rejection  - None, or the ticker-level token when no chain was evaluated:
                 not_on_watchlist / earnings_lapsed / no_snapshot / null_price.
    """

    def __init__(self, *, ticker, cost_basis, run_date, earnings_date,
                 underlying_price, underlying_price_ts, candidates, all_rows,
                 reject_tally, rejection, wl_rejected, wl_warnings,
                 chain_fetch_errors):
        self.ticker = ticker
        self.cost_basis = cost_basis
        self.run_date = run_date
        self.earnings_date = earnings_date
        self.underlying_price = underlying_price
        self.underlying_price_ts = underlying_price_ts
        self.candidates = candidates
        self.all_rows = all_rows
        self.reject_tally = reject_tally
        self.rejection = rejection
        self.wl_rejected = wl_rejected
        self.wl_warnings = wl_warnings
        self.chain_fetch_errors = chain_fetch_errors


def run_clock_ts(clock):
    """The run clock's tz-aware timestamp, else ClockInvalid."""
    ts = getattr(clock, "timestamp", None)
    if ts is None or ts.tzinfo is None or ts.utcoffset() is None:
        raise ClockInvalid("clock timestamp missing or tz-naive")
    return ts


def _oi_key(row):
    return row["open_interest"] if row["open_interest"] != "" else -1


def _yield_key(row):
    y = row["annualized_yield_pct"]
    return y if y != "" else -1.0


def scan_calls(ticker, cost_basis, trading_client, stock_client, option_client,
               policy, clock, watchlist_path=None):
    """Covered-call scan for one held ticker. Assumes the throttle is already
    initialized. The watchlist is validated here against the run clock's ET
    time (watchlist.load_watchlist); a structural failure raises
    WatchlistStructureError. A ticker that is not approved, or whose earnings
    stamp has lapsed, yields zero candidates and a named rejection, no HALT.
    Raises ClockInvalid on a missing or tz-naive clock timestamp.
    """
    run_ts = run_clock_ts(clock)
    now_et = run_ts.astimezone(ET)
    run_date = now_et.date()

    entry = policy["entry"]
    dte_min = entry["dte_min"]
    dte_max = entry["dte_max"]

    if watchlist_path is None:
        watchlist_path = WATCHLIST_PATH
    approved, wl_rejected, wl_warnings = watchlist.load_watchlist(
        str(watchlist_path), now_et)

    reject_tally = Counter()

    def result(rejection=None, earnings_date=None, price=None, price_ts=None,
               candidates=(), all_rows=(), chain_fetch_errors=()):
        if rejection is not None:
            reject_tally[f"name:{rejection}"] += 1
        return CCScanResult(
            ticker=ticker, cost_basis=cost_basis, run_date=run_date,
            earnings_date=earnings_date, underlying_price=price,
            underlying_price_ts=price_ts, candidates=list(candidates),
            all_rows=list(all_rows), reject_tally=reject_tally,
            rejection=rejection, wl_rejected=wl_rejected,
            wl_warnings=wl_warnings,
            chain_fetch_errors=list(chain_fetch_errors))

    entry_wl = next((e for e in approved if e["ticker"] == ticker), None)
    if entry_wl is None:
        lapsed = any(t == ticker and r == "earnings_date_in_past"
                     for t, r in wl_rejected)
        return result("earnings_lapsed" if lapsed else "not_on_watchlist")
    earnings_date = watchlist._parse_date(entry_wl["earnings_date"])

    feed = DataFeed(policy["runtime"]["feed"])
    screener.throttle(f"get_stock_snapshot {ticker}")
    snap_resp = stock_client.get_stock_snapshot(
        StockSnapshotRequest(symbol_or_symbols=[ticker], feed=feed))
    snap = snap_resp.get(ticker) if snap_resp is not None else None
    if snap is None:
        return result("no_snapshot", earnings_date)
    price, _src, price_ts = screener.price_from_chain(snap)
    if price is None:
        return result("null_price", earnings_date)

    exp_gte = run_date + timedelta(days=dte_min)
    exp_lte = run_date + timedelta(days=dte_max + 10)
    contracts = []
    page_token = None
    while True:
        screener.throttle(f"get_option_contracts {ticker}")
        resp = trading_client.get_option_contracts(
            GetOptionContractsRequest(
                underlying_symbols=[ticker],
                status=AssetStatus.ACTIVE,
                type=ContractType.CALL,
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
                limit=10000,
                page_token=page_token,
            )
        )
        contracts.extend(resp.option_contracts or [])
        page_token = resp.next_page_token
        if not page_token:
            break

    chain_fetch_errors = []
    screener.throttle(f"get_option_chain {ticker}")
    try:
        snapshots = option_client.get_option_chain(
            OptionChainRequest(
                underlying_symbol=ticker,
                type=ContractType.CALL,
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
            )
        )
    except Exception as ex:
        chain_fetch_errors.append(
            f"get_option_chain FAILED for {ticker}: {type(ex).__name__}: {ex}")
        snapshots = {}

    all_rows = []
    candidates = []
    for c in contracts:
        row = evaluate_call(
            c, snapshots.get(c.symbol), cost_basis, price, price_ts, run_ts,
            run_date, earnings_date, dte_min, dte_max,
            policy["calls"]["otm_above_basis_pct"], entry["min_bid"],
            entry["min_open_interest"], entry["max_spread_abs"],
            entry["max_spread_to_bid_ratio"],
            entry["min_annualized_yield_pct"],
            policy["universe"]["max_price_staleness_min"])
        all_rows.append(row)
        if row["all_pass"]:
            candidates.append(row)
        else:
            for tok in row["rejected_by"].split(FAILS_DELIM):
                if tok:
                    reject_tally[tok] += 1

    candidates.sort(key=lambda r: (_oi_key(r), _yield_key(r)), reverse=True)
    return result(None, earnings_date, price, price_ts, candidates, all_rows,
                  chain_fetch_errors)


# --------------------------------------------------------------------------- #
# Standalone dry CLI                                                         #
# --------------------------------------------------------------------------- #

class _BadArgs(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # bad args exit 3, not argparse's 2
        raise _BadArgs(message)


def _build_parser():
    p = _Parser(prog="forgetrader.cc_scan",
                description="Dry covered-call scan for one held ticker. "
                            "Read-only, writes nothing, places nothing.")
    p.add_argument("--ticker", required=True, help="held underlying, e.g. NOK")
    p.add_argument("--basis", required=True,
                   help="cost basis per share, e.g. 25.00")
    return p


def _parse_args(argv):
    args = _build_parser().parse_args(argv)
    if not watchlist.TICKER_RE.match(args.ticker):
        raise _BadArgs(f"--ticker {args.ticker!r} is not 1-5 capital letters")
    try:
        basis = Decimal(args.basis)
    except InvalidOperation:
        raise _BadArgs(f"--basis {args.basis!r} is not a number")
    if not basis.is_finite() or basis <= 0:
        raise _BadArgs(f"--basis {args.basis!r} must be a finite number above 0")
    basis_f = float(basis)
    if not math.isfinite(basis_f) or basis_f <= 0:
        raise _BadArgs(f"--basis {args.basis!r} out of range")
    return args.ticker, basis_f


def _top_rejects(tally):
    return ", ".join(f"{tok}={n}" for tok, n in tally.most_common(5))


def _render(res, clock):
    print("CC SCAN - DRY, READ-ONLY. PLACES NO ORDER, WRITES NO FILE.")
    print(f"ticker={res.ticker} cost_basis={res.cost_basis} "
          f"run_date={res.run_date} clock={clock.timestamp.isoformat()} "
          f"is_open={getattr(clock, 'is_open', None)}")
    print(f"earnings_date={res.earnings_date} "
          f"underlying_price={res.underlying_price} "
          f"price_ts={res.underlying_price_ts.isoformat() if res.underlying_price_ts else None}")
    for msg in res.chain_fetch_errors:
        print(f"WARNING: {msg}", file=sys.stderr)
    if res.rejection is not None:
        print(f"REJECTED {res.ticker}: {res.rejection}")
    print(f"evaluated={len(res.all_rows)} candidates={len(res.candidates)}")
    if res.candidates:
        print("CANDIDATES (open_interest desc, yield tiebreak)")
        for r in res.candidates:
            print(f"  {r['contract_symbol']} strike={r['strike']:.2f} "
                  f"floor={r['strike_floor']:.2f} expiry={r['expiration_date']} "
                  f"dte={r['dte']} bid={r['bid']} ask={r['ask']} "
                  f"oi={r['open_interest']} "
                  f"annualized_yield_pct={r['annualized_yield_pct']}")
    else:
        print("CANDIDATES (none)")
    print(f"top_rejects=[{_top_rejects(res.reject_tally)}]")


def main(argv=None):
    try:
        ticker, basis = _parse_args(argv)
    except _BadArgs as e:
        print(f"BAD ARGS: {e}", file=sys.stderr)
        return 3

    key, secret, paper = credentials.load_credentials()
    trading_client = TradingClient(key, secret, paper=paper)
    stock_client = StockHistoricalDataClient(key, secret)
    option_client = OptionHistoricalDataClient(key, secret)
    policy = screener.load_policy()
    screener.init_throttle(policy["runtime"]["rate_limit_per_min"])

    # One broker clock for the whole run (D13); the scan never reads its own.
    screener.throttle("get_clock")
    clock = trading_client.get_clock()
    try:
        run_clock_ts(clock)
    except ClockInvalid as e:
        print(f"CLOCK INVALID: {e}", file=sys.stderr)
        return 3

    try:
        res = scan_calls(ticker, basis, trading_client, stock_client,
                         option_client, policy, clock)
    except watchlist.WatchlistStructureError as e:
        print(f"HALT watchlist_structure {e}", file=sys.stderr)
        return 4
    _render(res, clock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
