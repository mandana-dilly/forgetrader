#!/usr/bin/env python3
"""Brief 8 - wheel_dryrun.py

Read the approved watchlist, and for each approved name fetch a price and a
PUT chain, run the shared gate chain plus two engine-owned context gates
(watchlist membership and earnings proximity), then propose exactly one
cash-secured put - or emit a correct all-rejected report.

Read-only by construction. This script imports no order request type and
calls no order method; it cannot place, cancel, or modify an order. The
only path out is a proposal printed to stdout and written to
scans/wheel_dryrun_<ts>.txt.

Reuse (imported, never reimplemented):
  gates.evaluate_contract, gates.FAILS_DELIM         - the shared gate chain
  screener.price_from_chain                           - price-source precedence
  screener.compute_collateral_numbers                 - affordability ceiling
  screener.load_policy                                - policy.toml
  screener.init_throttle / screener.throttle          - one shared rate limiter
  watchlist.load_watchlist / WatchlistStructureError  - the human approval gate
  watchlist._parse_date                               - earnings_date reparse,
                                                        zero divergence

Gate order per name:
  watchlist member  ->  price/ceiling  ->  earnings  ->  shared evaluate_contract
  final_pass = all_pass AND earnings_pass

The earnings reject token "earnings_inside_option" is appended onto the shared
rejected_by string here, so gates.py stays untouched.
"""
import argparse
import csv
import os
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.data.enums import DataFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus, ContractType
from alpaca.trading.requests import GetOptionContractsRequest

from gates import FAILS_DELIM, evaluate_contract
from screener import (
    compute_collateral_numbers,
    init_throttle,
    load_policy,
    price_from_chain,
    throttle,
)
from watchlist import WatchlistStructureError, _parse_date, load_watchlist

SCRIPT_DIR = Path(__file__).resolve().parent
SCANS_DIR = SCRIPT_DIR / "scans"
WATCHLIST_PATH = SCRIPT_DIR / "watchlist.json"

# Appended onto the shared rejected_by string when the contract's expiry is not
# strictly before the name's earnings_date (or the date is unparseable).
EARNINGS_REJECT_TOKEN = "earnings_inside_option"

# Per-contract CSV columns. The list up to and including "rejected_by" is copied
# verbatim from screener.stage_c's fieldnames (rejected_by is the shared string,
# already augmented here with EARNINGS_REJECT_TOKEN). The trailing two are the
# engine-owned fields this script appends onto every evaluate_contract row.
CSV_FIELDNAMES = [
    "underlying_symbol", "root_symbol", "underlying_price", "underlying_price_ts", "underlying_staleness_min",
    "contract_symbol", "expiration_date", "dte", "dte_pass",
    "strike", "otm_pct", "otm_pass", "bid", "ask", "mid", "bid_pass",
    "open_interest", "oi_pass", "spread_abs", "spread_pct", "spread_pass",
    "collateral", "collateral_pass", "annualized_yield_pct", "yield_pass",
    "adjusted_pass", "staleness_pass", "all_pass", "rejected_by",
    "earnings_pass", "final_pass",
]


def _et_now_str():
    return datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")


def wait_for_open(trading_client, max_poll_hours):
    """ftnight pattern: poll get_clock() until the market is open, then return
    so the caller runs exactly once. Total wait is capped at max_poll_hours;
    if the deadline passes with the market still closed, exit non-zero.
    """
    deadline = time.time() + max_poll_hours * 3600
    while True:
        if time.time() > deadline:
            print(
                f"{_et_now_str()} DEADLINE EXCEEDED after {max_poll_hours}h - "
                f"market never opened",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(3)
        try:
            throttle("get_clock (wait-for-open poll)")
            clock = trading_client.get_clock()
        except Exception as e:
            print(
                f"{_et_now_str()} clock error {type(e).__name__}: {e} - retry in 300s",
                flush=True,
            )
            time.sleep(300)
            continue

        if clock.is_open:
            print(
                f"{_et_now_str()} market OPEN (next_close={clock.next_close.isoformat()}) "
                f"- running once",
                flush=True,
            )
            return

        wait_s = (
            clock.next_open.astimezone(timezone.utc) - datetime.now(timezone.utc)
        ).total_seconds()
        print(
            f"{_et_now_str()} market closed - next_open={clock.next_open.isoformat()} "
            f"({wait_s / 60:.1f} min) - sleeping",
            flush=True,
        )
        time.sleep(max(60, min(wait_s + 5, 900)))


def _oi_sort_key(row):
    oi = row["open_interest"]
    return int(oi) if str(oi).strip() != "" else -1


def _yield_sort_key(row):
    y = row["annualized_yield_pct"]
    return y if y != "" else -1.0


def _write_report(lines, run_ts_str):
    SCANS_DIR.mkdir(exist_ok=True)
    path = SCANS_DIR / f"wheel_dryrun_{run_ts_str}.txt"
    path.write_text("\n".join(lines) + "\n")
    print(f"\nwrote {path}")
    return path


def _write_csv(rows, run_ts_str):
    """One per-contract CSV per run, same timestamp as the .txt. DictWriter
    pattern copied from screener.stage_c."""
    SCANS_DIR.mkdir(exist_ok=True)
    path = SCANS_DIR / f"wheel_dryrun_{run_ts_str}.csv"
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDNAMES})
    print(f"wrote {path}")
    return path


def main():
    parser = argparse.ArgumentParser(
        description="Brief 8 wheel dry-run - read-only CSP proposer (no order path)"
    )
    parser.add_argument(
        "--wait-for-open",
        action="store_true",
        help="poll get_clock() until the market opens, then run once (ftnight pattern)",
    )
    parser.add_argument(
        "--max-poll-hours",
        type=float,
        default=20,
        help="cap on --wait-for-open polling before giving up (default 20)",
    )
    args = parser.parse_args()

    load_dotenv(dotenv_path=SCRIPT_DIR / ".env")
    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    if not api_key or not api_secret:
        print(
            "FATAL: ALPACA_API_KEY or ALPACA_API_SECRET missing from .env",
            file=sys.stderr,
        )
        sys.exit(1)

    policy = load_policy()
    init_throttle(policy["runtime"]["rate_limit_per_min"])
    feed_str = policy["runtime"]["feed"]
    feed = DataFeed(feed_str)

    trading_client = TradingClient(api_key, api_secret, paper=paper)
    stock_client = StockHistoricalDataClient(api_key, api_secret)
    option_client = OptionHistoricalDataClient(api_key, api_secret)

    if args.wait_for_open:
        wait_for_open(trading_client, args.max_poll_hours)

    # One timestamp for every artifact this run writes.
    run_ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    # DTE and the earnings comparison both anchor to the US/Eastern calendar
    # date, not the host's local date.
    now_et = datetime.now(ZoneInfo("America/New_York"))
    run_date = now_et.date()

    entry = policy["entry"]
    dte_min = entry["dte_min"]
    dte_max = entry["dte_max"]
    otm_target_pct = entry["otm_target_pct"]
    min_bid = entry["min_bid"]
    min_oi = entry["min_open_interest"]
    max_spread_abs = entry["max_spread_abs"]
    max_spread_to_bid_ratio = entry["max_spread_to_bid_ratio"]
    min_yield_pct = entry["min_annualized_yield_pct"]
    max_staleness_min = policy["universe"]["max_price_staleness_min"]
    min_underlying_price = policy["universe"]["min_underlying_price"]

    # Mirror screener Stage C: widen the fetch window past dte_max, then let
    # evaluate_contract apply the real [dte_min, dte_max] gate in Python.
    fetch_max_days = dte_max + 10
    exp_gte = run_date + timedelta(days=dte_min)
    exp_lte = run_date + timedelta(days=fetch_max_days)

    coll = compute_collateral_numbers(trading_client, policy)
    available = coll["available"]
    max_underlying = coll["max_underlying"]

    try:
        approved, wl_rejected, wl_warnings = load_watchlist(str(WATCHLIST_PATH), now_et)
    except WatchlistStructureError as e:
        print(f"FATAL: watchlist unreadable - {e}", file=sys.stderr)
        sys.exit(1)

    throttle("get_clock")
    clock = trading_client.get_clock()

    lines = []

    def emit(text=""):
        print(text)
        lines.append(text)

    emit("WHEEL DRY-RUN - READ-ONLY. THIS SCRIPT PLACES NO ORDER, CANCELS NONE, MODIFIES NONE.")
    emit("")
    emit(f"run                  : {run_ts_str}")
    emit(f"feed in use          : {feed_str}")
    emit(f"DTE anchor (US/East) : {run_date}")
    emit(
        f"market clock         : timestamp={clock.timestamp} is_open={clock.is_open} "
        f"next_open={clock.next_open} next_close={clock.next_close}"
    )
    emit("")
    emit("collateral numbers:")
    emit(f"  cash               : {coll['cash']:.2f}")
    emit(f"  available          : {coll['available']:.2f}")
    emit(f"  max_strike         : {coll['max_strike']}")
    emit(f"  max_underlying     : {coll['max_underlying']:.4f}")
    emit(f"  price band         : [{min_underlying_price:.2f}, {max_underlying:.4f}]")
    emit(f"  fetch expiry window: [{exp_gte}, {exp_lte}]  (dte gate at {dte_min}-{dte_max})")
    emit("")
    emit(f"watchlist            : {WATCHLIST_PATH.name}")
    emit(
        f"  approved names      : {len(approved)}  "
        f"({', '.join(e['ticker'] for e in approved) or '(none)'})"
    )
    emit(
        f"  watchlist-rejected  : {len(wl_rejected)}  "
        f"({', '.join(f'{t}:{r}' for t, r in wl_rejected) or '(none)'})"
    )
    emit(
        f"  advisory warnings   : {len(wl_warnings)}  "
        f"({', '.join(f'{t}:{w}' for t, w in wl_warnings) or '(none)'})"
    )

    reject_tally = Counter()
    passing_rows = []  # final_pass rows across every approved name
    all_rows = []  # every evaluate_contract row, for the per-contract CSV
    per_name = []  # (ticker, one-line outcome)
    earnings_by_ticker = {}

    for e in approved:
        ticker = e["ticker"]
        emit("")
        emit(f"===== {ticker} =====")
        emit("  gate: watchlist_member  -> PASS (on the approved list)")

        earnings_raw = e.get("earnings_date")
        earnings_date = _parse_date(earnings_raw)
        earnings_by_ticker[ticker] = earnings_date
        if earnings_date is None:
            emit(
                f"  earnings_date reparse   -> UNPARSEABLE ({earnings_raw!r}) - "
                f"fail-closed, every contract fails the earnings gate"
            )
        else:
            emit(
                f"  earnings_date reparse   -> {earnings_date}  "
                f"(expiration_date must be strictly before this)"
            )

        # --- price fetch: screener Stage B, single-symbol snapshot ---
        throttle(f"get_stock_snapshot {ticker}")
        snap_resp = stock_client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=[ticker], feed=feed)
        )
        snap = snap_resp.get(ticker) if snap_resp is not None else None
        if snap is None:
            emit("  gate: price/ceiling     -> FAIL (no_snapshot) - name rejected, no chain fetch")
            per_name.append((ticker, "REJECTED name-level: no_snapshot"))
            reject_tally["name:no_snapshot"] += 1
            continue

        price, price_src, price_ts = price_from_chain(snap)
        if price is None:
            emit("  gate: price/ceiling     -> FAIL (null_price) - name rejected, no chain fetch")
            per_name.append((ticker, "REJECTED name-level: null_price"))
            reject_tally["name:null_price"] += 1
            continue

        staleness_min = (
            datetime.now(timezone.utc) - price_ts
        ).total_seconds() / 60
        emit(
            f"  price                  -> {price} "
            f"(source={price_src}, age={staleness_min:.2f} min)"
        )

        if price < min_underlying_price:
            emit(
                f"  gate: price/ceiling     -> FAIL (below_min_underlying_price: "
                f"{price} < {min_underlying_price}) - name rejected, no chain fetch"
            )
            per_name.append((ticker, "REJECTED name-level: below_min_underlying_price"))
            reject_tally["name:below_min_underlying_price"] += 1
            continue
        if price > max_underlying:
            emit(
                f"  gate: price/ceiling     -> FAIL (above_max_underlying: "
                f"{price} > {max_underlying:.4f}) - name rejected, no chain fetch"
            )
            per_name.append((ticker, "REJECTED name-level: above_max_underlying"))
            reject_tally["name:above_max_underlying"] += 1
            continue
        emit("  gate: price/ceiling     -> PASS")

        # --- chain fetch: screener Stage C (paginated contracts + chain snap) ---
        contracts = []
        page_token = None
        while True:
            throttle(f"get_option_contracts {ticker}")
            resp = trading_client.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbols=[ticker],
                    status=AssetStatus.ACTIVE,
                    type=ContractType.PUT,
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

        throttle(f"get_option_chain {ticker}")
        try:
            snapshots = option_client.get_option_chain(
                OptionChainRequest(
                    underlying_symbol=ticker,
                    type=ContractType.PUT,
                    expiration_date_gte=exp_gte,
                    expiration_date_lte=exp_lte,
                )
            )
        except Exception as ex:
            print(
                f"  get_option_chain FAILED for {ticker}: {type(ex).__name__}: {ex}",
                file=sys.stderr,
            )
            snapshots = {}

        price_ts_iso = price_ts.isoformat()
        name_eval = 0
        name_pass = 0
        for c in contracts:
            # Recompute staleness at chain-evaluation time, per screener Stage C -
            # the price fetch and this loop can be minutes apart.
            chain_staleness_min = (
                datetime.now(timezone.utc) - price_ts
            ).total_seconds() / 60

            row = evaluate_contract(
                c,
                snapshots.get(c.symbol),
                price,
                price_ts_iso,
                chain_staleness_min,
                run_date,
                dte_min,
                dte_max,
                otm_target_pct,
                min_bid,
                min_oi,
                max_spread_abs,
                max_spread_to_bid_ratio,
                min_yield_pct,
                available,
                max_staleness_min,
            )
            row["underlying_symbol"] = ticker

            # Engine-owned earnings gate: expiry strictly before earnings_date,
            # fail-closed when the date did not parse.
            earnings_pass = (
                earnings_date is not None and c.expiration_date < earnings_date
            )
            if not earnings_pass:
                rb = row["rejected_by"]
                row["rejected_by"] = (
                    f"{rb}{FAILS_DELIM}{EARNINGS_REJECT_TOKEN}"
                    if rb
                    else EARNINGS_REJECT_TOKEN
                )
            row["earnings_pass"] = earnings_pass
            row["final_pass"] = row["all_pass"] and earnings_pass
            all_rows.append(row)

            name_eval += 1
            if row["final_pass"]:
                name_pass += 1
                passing_rows.append(row)
            else:
                for tok in row["rejected_by"].split(FAILS_DELIM):
                    if tok:
                        reject_tally[tok] += 1

        emit(
            f"  chain: contracts fetched={len(contracts)}  evaluated={name_eval}  "
            f"final_pass={name_pass}"
        )
        per_name.append(
            (ticker, f"{name_pass}/{name_eval} contract(s) pass all shared gates + earnings")
        )

    emit("")
    emit("===== PER-NAME OUTCOME =====")
    if per_name:
        for t, v in per_name:
            emit(f"  {t:<8} {v}")
    else:
        emit("  (no approved names to examine)")

    emit("")
    emit("===== REJECT TALLY (per token, all names) =====")
    if reject_tally:
        for tok, cnt in reject_tally.most_common():
            emit(f"  {tok:34s}: {cnt}")
    else:
        emit("  (nothing rejected)")

    passing_rows.sort(
        key=lambda r: (_oi_sort_key(r), _yield_sort_key(r)), reverse=True
    )

    emit("")
    emit("===== CANDIDATES (final_pass) - ranked by open_interest desc, yield tiebreak =====")
    if passing_rows:
        emit(
            f"  {'symbol':<8} {'contract':<24} {'strike':>8} {'dte':>4} "
            f"{'bid':>7} {'mid':>8} {'oi':>8} {'yield%':>9} {'collat':>10}"
        )
        for r in passing_rows:
            emit(
                f"  {r['underlying_symbol']:<8} {r['contract_symbol']:<24} "
                f"{r['strike']:>8.2f} {r['dte']:>4} {r['bid']:>7.2f} "
                f"{str(r['mid']):>8} {str(r['open_interest']):>8} "
                f"{r['annualized_yield_pct']:>9.2f} {r['collateral']:>10.2f}"
            )
    else:
        emit("  (none)")

    emit("")
    if passing_rows:
        top = passing_rows[0]
        emit("===== PROPOSAL - ONE CASH-SECURED PUT (NOT PLACED) =====")
        emit(f"  underlying           : {top['underlying_symbol']} @ {top['underlying_price']}")
        emit("  action               : SELL 1 PUT, cash-secured")
        emit(f"  contract             : {top['contract_symbol']}")
        emit(f"  expiration           : {top['expiration_date']}  (dte={top['dte']})")
        emit(f"  strike               : {top['strike']:.2f}")
        emit(f"  otm_pct              : {top['otm_pct']}")
        emit(f"  bid / mid / ask      : {top['bid']} / {top['mid']} / {top['ask']}")
        emit(f"  open_interest        : {top['open_interest']}")
        emit(f"  annualized_yield_pct : {top['annualized_yield_pct']}")
        emit(
            f"  collateral (strike*100): {top['collateral']:.2f}  "
            f"(available={available:.2f})"
        )
        emit(
            f"  earnings_date        : {earnings_by_ticker.get(top['underlying_symbol'])}  "
            f"(expiry is strictly before it)"
        )
        emit("")
        emit("  This script has no order path. Acting on this proposal is a separate, manual step.")
    else:
        emit("===== ALL-REJECTED REPORT =====")
        emit("  No contract cleared every shared gate plus the earnings gate.")
        emit(f"  approved names examined : {len(approved)}")
        if per_name:
            emit("  per-name outcome:")
            for t, v in per_name:
                emit(f"    {t:<8} {v}")
        emit("  Nothing to propose. This is a correct empty result, not an error.")

    _write_report(lines, run_ts_str)
    _write_csv(all_rows, run_ts_str)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
