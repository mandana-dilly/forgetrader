#!/usr/bin/env python3
"""
ForgeTrader universe screener - read-only.

Answers: how many optionable US equities sit under the current price ceiling,
and what does their put-chain liquidity actually look like right now.

Never submits, cancels, or modifies an order.
"""
import argparse
import csv
import os
import sys
import time
import tomllib
import traceback
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from math import floor
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from alpaca.data.enums import DataFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockSnapshotRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus, ContractType
from alpaca.trading.requests import GetAssetsRequest, GetOptionContractsRequest

from gates import FAILS_DELIM, evaluate_contract

SCRIPT_DIR = Path(__file__).resolve().parent
POLICY_PATH = SCRIPT_DIR / "policy.toml"
SCANS_DIR = SCRIPT_DIR / "scans"

# ---------------------------------------------------------------------------
# rate limiter - single shared throttle across every client call in the script
# ---------------------------------------------------------------------------
_call_times = deque()
_call_count = 0
_rate_limit_per_min = None


def init_throttle(rate_limit_per_min):
    global _rate_limit_per_min
    _rate_limit_per_min = rate_limit_per_min


def throttle(label=""):
    global _call_count
    if _rate_limit_per_min is None:
        raise RuntimeError("throttle used before init_throttle()")
    now = time.monotonic()
    while _call_times and now - _call_times[0] > 60:
        _call_times.popleft()
    if len(_call_times) >= _rate_limit_per_min:
        sleep_for = 60 - (now - _call_times[0]) + 0.01
        print(f"[rate] window full ({len(_call_times)}/{_rate_limit_per_min}) - sleeping {sleep_for:.2f}s")
        time.sleep(sleep_for)
        now = time.monotonic()
        while _call_times and now - _call_times[0] > 60:
            _call_times.popleft()
    _call_times.append(now)
    _call_count += 1
    print(f"[rate] call #{_call_count} {label}")


def header(title):
    print()
    print(f"===== {title} =====")


def load_policy():
    with open(POLICY_PATH, "rb") as f:
        return tomllib.load(f)


def price_from_chain(snap):
    """
    Walk the price-source precedence chain in order:
    latest_trade.price -> minute_bar.close -> daily_bar.close -> previous_daily_bar.close
    A field whose value is exactly 0 is skipped, not treated as a valid price.
    Returns (price, source_tag, timestamp) or (None, None, None) if all sources are
    missing or zero.
    """
    candidates = [
        ("trade", snap.latest_trade.price if snap.latest_trade else None,
         snap.latest_trade.timestamp if snap.latest_trade else None),
        ("minute_bar", snap.minute_bar.close if snap.minute_bar else None,
         snap.minute_bar.timestamp if snap.minute_bar else None),
        ("daily_bar", snap.daily_bar.close if snap.daily_bar else None,
         snap.daily_bar.timestamp if snap.daily_bar else None),
        ("prev_daily_bar", snap.previous_daily_bar.close if snap.previous_daily_bar else None,
         snap.previous_daily_bar.timestamp if snap.previous_daily_bar else None),
    ]
    for tag, price, ts in candidates:
        if price is not None and price != 0:
            return price, tag, ts
    return None, None, None


# ---------------------------------------------------------------------------
# Stage A - asset enumeration
# ---------------------------------------------------------------------------
def stage_a(trading_client, limit):
    header("STAGE A - ASSET ENUMERATION")
    throttle("get_all_assets(status=ACTIVE, asset_class=US_EQUITY)")
    req = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
    all_assets = trading_client.get_all_assets(req)
    print(f"active US equity assets (raw)        : {len(all_assets)}")

    tradable = [a for a in all_assets if a.tradable]
    print(f"after tradable == True               : {len(tradable)}")

    optionable = [a for a in tradable if "has_options" in (a.attributes or [])]
    print(f"after 'has_options' in attributes     : {len(optionable)}")

    if limit and limit > 0:
        optionable = optionable[:limit]
        print(f"after --limit {limit}                    : {len(optionable)}")

    print(f"STAGE A SURVIVORS                     : {len(optionable)}")
    return optionable


# ---------------------------------------------------------------------------
# Stage B - price fetch
# ---------------------------------------------------------------------------
def compute_collateral_numbers(trading_client, policy):
    throttle("get_account")
    acct = trading_client.get_account()
    cash = float(acct.cash)
    reserve_pct = policy["capital"]["reserve_pct"]
    available = cash * (1 - reserve_pct)
    max_strike = floor(available / 100)
    otm_target_pct = policy["entry"]["otm_target_pct"]
    # Derived affordability ceiling, not a config value: recomputed each run from
    # available collateral. Do NOT move to policy.toml - it is a function of cash, not a
    # tunable. The binding affordability check is the Stage C collateral gate.
    max_underlying = max_strike / (1 - otm_target_pct / 100)

    print(f"cash                 : {cash:.2f}")
    print(f"available            : {available:.2f}")
    print(f"max_strike           : {max_strike}")
    print(f"max_underlying       : {max_underlying:.4f}")

    return {"cash": cash, "available": available, "max_strike": max_strike, "max_underlying": max_underlying}


def stage_b(stock_client, trading_client, policy, survivors, feed, run_ts_str):
    header("STAGE B - PRICE FETCH")
    coll = compute_collateral_numbers(trading_client, policy)

    min_underlying_price = policy["universe"]["min_underlying_price"]
    batch_size = policy["runtime"]["snapshot_batch_size"]

    chunks = [survivors[i:i + batch_size] for i in range(0, len(survivors), batch_size)]
    print(f"survivors to price   : {len(survivors)}")
    print(f"batch size           : {batch_size}")
    print(f"chunk count          : {len(chunks)}")

    now_utc = datetime.now(timezone.utc)
    rows = []
    total_requested = 0
    total_returned = 0
    absent_symbols = []
    source_tally = Counter()

    for idx, chunk in enumerate(chunks):
        symbols = [a.symbol for a in chunk]
        throttle(f"get_stock_snapshot chunk {idx} ({len(symbols)} symbols)")
        req = StockSnapshotRequest(symbol_or_symbols=symbols, feed=feed)
        result = stock_client.get_stock_snapshot(req)

        total_requested += len(symbols)
        total_returned += len(result)
        missing = sorted(set(symbols) - set(result.keys()))
        absent_symbols.extend(missing)

        if len(result) < 0.90 * len(symbols):
            print(
                f"WARNING: chunk {idx} truncation suspicion - requested {len(symbols)}, "
                f"returned {len(result)}",
                file=sys.stderr,
            )

        for asset in chunk:
            sym = asset.symbol
            snap = result.get(sym)

            if snap is None:
                rows.append({
                    "symbol": sym, "name": asset.name, "exchange": str(asset.exchange),
                    "price": "", "price_source": "", "price_ts": "",
                    "staleness_min": "", "passed": False, "reject_reason": "no_snapshot",
                })
                continue

            price, source, ts = price_from_chain(snap)
            if price is None:
                rows.append({
                    "symbol": sym, "name": asset.name, "exchange": str(asset.exchange),
                    "price": "", "price_source": "", "price_ts": "",
                    "staleness_min": "", "passed": False, "reject_reason": "null_price",
                })
                continue

            source_tally[source] += 1
            staleness_min = (now_utc - ts).total_seconds() / 60

            passed = True
            reject_reason = ""
            if price > coll["max_underlying"]:
                passed = False
                reject_reason = "above_max_underlying"
            elif price < min_underlying_price:
                passed = False
                reject_reason = "below_min_underlying_price"

            rows.append({
                "symbol": sym, "name": asset.name, "exchange": str(asset.exchange),
                "price": price, "price_source": source, "price_ts": ts.isoformat(),
                "staleness_min": round(staleness_min, 2), "passed": passed,
                "reject_reason": reject_reason,
            })

    print(f"total requested (sum of chunks) : {total_requested}")
    print(f"total returned  (sum of chunks) : {total_returned}")
    print(f"total absent symbols            : {len(absent_symbols)}")

    print()
    print("price_source tally:")
    if source_tally:
        for tag, cnt in source_tally.most_common():
            print(f"  {tag:14s}: {cnt}")
    else:
        print("  (no symbol produced a usable price)")

    SCANS_DIR.mkdir(exist_ok=True)
    prices_path = SCANS_DIR / f"prices_{run_ts_str}.csv"
    fieldnames = [
        "symbol", "name", "exchange", "price", "price_source", "price_ts",
        "staleness_min", "passed", "reject_reason",
    ]
    with open(prices_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"wrote {prices_path}")

    survivors_b = [r for r in rows if r["passed"]]
    print()
    print(f"STAGE B SURVIVOR COUNT: {len(survivors_b)}")

    return {
        "rows": rows,
        "survivors": survivors_b,
        "collateral": coll,
        "absent_symbols": absent_symbols,
        "total_requested": total_requested,
        "total_returned": total_returned,
        "source_tally": source_tally,
        "prices_path": prices_path,
    }


# ---------------------------------------------------------------------------
# Stage C - chain qualification
# ---------------------------------------------------------------------------
def stage_c(trading_client, option_client, policy, survivors_b, max_chains, available, run_date, run_ts_str):
    header("STAGE C - CHAIN QUALIFICATION")
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

    # widen the fetch window past dte_max, per the pattern established in smoke.py's
    # chain read path, then filter to [dte_min, dte_max] in Python (dte_pass column)
    fetch_max_days = dte_max + 10

    targets = survivors_b if not max_chains else survivors_b[:max_chains]
    print(f"Stage B survivors available for chain fetch : {len(survivors_b)}")
    print(f"chains to fetch (after --max-chains)         : {len(targets)}")

    today = run_date
    exp_gte = today + timedelta(days=dte_min)
    exp_lte = today + timedelta(days=fetch_max_days)
    print(f"anchor date (US/Eastern)                     : {today}")
    print(f"fetch expiration window                      : [{exp_gte}, {exp_lte}]  (dte gate applied at {dte_min}-{dte_max})")

    all_rows = []
    gate_fail_tally = Counter()
    all_pass_count = 0

    for rec in targets:
        symbol = rec["symbol"]
        underlying_price = float(rec["price"])

        # staleness of the Stage B price is recomputed here, at chain-evaluation time,
        # not carried over from Stage B - the two stages can be many minutes apart
        underlying_price_ts = datetime.fromisoformat(rec["price_ts"])
        underlying_staleness_min = (datetime.now(timezone.utc) - underlying_price_ts).total_seconds() / 60

        print(f"--- {symbol} (underlying price {underlying_price}, "
              f"price age {underlying_staleness_min:.2f} min) ---")

        contracts = []
        page_token = None
        while True:
            throttle(f"get_option_contracts {symbol}")
            req = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                status=AssetStatus.ACTIVE,
                type=ContractType.PUT,
                expiration_date_gte=exp_gte,
                expiration_date_lte=exp_lte,
                limit=10000,
                page_token=page_token,
            )
            resp = trading_client.get_option_contracts(req)
            page_contracts = resp.option_contracts or []
            contracts.extend(page_contracts)
            page_token = resp.next_page_token
            if not page_token:
                break
        print(f"  contracts fetched: {len(contracts)}")

        throttle(f"get_option_chain {symbol}")
        chain_req = OptionChainRequest(
            underlying_symbol=symbol,
            type=ContractType.PUT,
            expiration_date_gte=exp_gte,
            expiration_date_lte=exp_lte,
        )
        try:
            snapshots = option_client.get_option_chain(chain_req)
        except Exception as e:
            print(f"  get_option_chain FAILED for {symbol}: {type(e).__name__}: {e}", file=sys.stderr)
            snapshots = {}

        for c in contracts:
            row = evaluate_contract(
                c, snapshots.get(c.symbol), underlying_price,
                underlying_price_ts.isoformat(), underlying_staleness_min, today,
                dte_min, dte_max, otm_target_pct, min_bid, min_oi,
                max_spread_abs, max_spread_to_bid_ratio, min_yield_pct, available, max_staleness_min,
            )
            row["underlying_symbol"] = symbol
            all_rows.append(row)
            if row["all_pass"]:
                all_pass_count += 1
            for token in row["rejected_by"].split(FAILS_DELIM):
                if token:
                    gate_fail_tally[token] += 1

    print()
    print(f"total contracts evaluated : {len(all_rows)}")
    print(f"all_pass contracts        : {all_pass_count}")

    print()
    print("per-gate rejection tally:")
    if gate_fail_tally:
        for gate, cnt in gate_fail_tally.most_common():
            print(f"  {gate:28s}: {cnt}")
    else:
        print("  (no contracts evaluated)")
    fail_count = len(all_rows) - all_pass_count
    print(f"reject tokens: {sum(gate_fail_tally.values())} across {fail_count} failing contracts")

    SCANS_DIR.mkdir(exist_ok=True)
    chains_path = SCANS_DIR / f"chains_{run_ts_str}.csv"
    fieldnames = [
        "underlying_symbol", "root_symbol", "underlying_price", "underlying_price_ts", "underlying_staleness_min",
        "contract_symbol", "expiration_date", "dte", "dte_pass",
        "strike", "otm_pct", "otm_pass", "bid", "ask", "mid", "bid_pass",
        "open_interest", "oi_pass", "spread_abs", "spread_pct", "spread_pass",
        "collateral", "collateral_pass", "annualized_yield_pct", "yield_pass",
        "adjusted_pass", "staleness_pass", "all_pass", "rejected_by",
    ]
    with open(chains_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"wrote {chains_path}")

    return {
        "rows": all_rows,
        "gate_fail_tally": gate_fail_tally,
        "all_pass_count": all_pass_count,
        "chains_path": chains_path,
    }


# ---------------------------------------------------------------------------
# Stage D - summary
# ---------------------------------------------------------------------------
EARNINGS_GATE_BANNER = "NO EARNINGS GATE APPLIED - THIS IS CALIBRATION DATA, NOT A TRADE CANDIDATE LIST"


def stage_d(trading_client, coll, stage_a_count, stage_b_result, stage_c_result, feed_str, run_ts_str):
    header("STAGE D - SUMMARY")
    print(EARNINGS_GATE_BANNER)
    throttle("get_clock")
    clock = trading_client.get_clock()

    lines = []
    lines.append(EARNINGS_GATE_BANNER)
    lines.append("")
    lines.append(f"ForgeTrader universe screener run: {run_ts_str}")
    lines.append(f"feed in use: {feed_str}")
    lines.append(
        f"market clock: timestamp={clock.timestamp} is_open={clock.is_open} "
        f"next_open={clock.next_open} next_close={clock.next_close}"
    )
    lines.append("")
    lines.append("collateral numbers:")
    lines.append(f"  cash            : {coll['cash']:.2f}")
    lines.append(f"  available       : {coll['available']:.2f}")
    lines.append(f"  max_strike      : {coll['max_strike']}")
    lines.append(f"  max_underlying  : {coll['max_underlying']:.4f}")
    lines.append("")
    lines.append("filter step counts:")
    lines.append(f"  Stage A survivors                          : {stage_a_count}")
    lines.append(f"  Stage B total requested (sum of chunks)    : {stage_b_result['total_requested']}")
    lines.append(f"  Stage B absent from snapshot response      : {len(stage_b_result['absent_symbols'])}")
    lines.append(f"  Stage B survivors                          : {len(stage_b_result['survivors'])}")
    lines.append("")
    lines.append("price_source tally:")
    if stage_b_result["source_tally"]:
        for tag, cnt in stage_b_result["source_tally"].most_common():
            lines.append(f"  {tag:14s}: {cnt}")
    else:
        lines.append("  (no symbol produced a usable price)")
    lines.append("")
    lines.append(f"absent symbols (Stage B, {len(stage_b_result['absent_symbols'])} total):")
    lines.append(", ".join(stage_b_result["absent_symbols"]) if stage_b_result["absent_symbols"] else "  (none)")

    if stage_c_result is not None:
        lines.append("")
        lines.append(f"Stage C contracts evaluated : {len(stage_c_result['rows'])}")
        lines.append(f"Stage C all_pass contracts  : {stage_c_result['all_pass_count']}")
        lines.append("")
        lines.append("per-gate rejection tally:")
        if stage_c_result["gate_fail_tally"]:
            for gate, cnt in stage_c_result["gate_fail_tally"].most_common():
                lines.append(f"  {gate:28s}: {cnt}")
        else:
            lines.append("  (no contracts evaluated)")
        fail_count = len(stage_c_result["rows"]) - stage_c_result["all_pass_count"]
        lines.append(
            f"reject tokens: {sum(stage_c_result['gate_fail_tally'].values())} "
            f"across {fail_count} failing contracts"
        )
        lines.append("")
        lines.append("all_pass contracts ranked by open_interest descending:")
        passers = [r for r in stage_c_result["rows"] if r["all_pass"]]
        passers.sort(
            key=lambda r: (
                int(r["open_interest"]) if str(r["open_interest"]).strip() != "" else -1,
                r["annualized_yield_pct"] if r["annualized_yield_pct"] != "" else -1,
            ),
            reverse=True,
        )
        if passers:
            lines.append(f"  {'symbol':<8} {'contract':<24} {'strike':>8} {'dte':>4} {'bid':>8} {'oi':>8} {'yield%':>8}")
            for r in passers:
                lines.append(
                    f"  {r['underlying_symbol']:<8} {r['contract_symbol']:<24} "
                    f"{r['strike']:>8.2f} {r['dte']:>4} {r['bid']:>8.2f} {str(r['open_interest']):>8} {r['annualized_yield_pct']:>8.2f}"
                )
        else:
            lines.append("  (none)")

    text = "\n".join(lines) + "\n"
    SCANS_DIR.mkdir(exist_ok=True)
    summary_path = SCANS_DIR / f"summary_{run_ts_str}.txt"
    summary_path.write_text(text)
    print(text)
    print(f"wrote {summary_path}")
    return summary_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["a", "b", "c", "all"], default="all")
    parser.add_argument("--max-chains", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    load_dotenv(dotenv_path=SCRIPT_DIR / ".env")
    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    if not api_key or not api_secret:
        print("FATAL: ALPACA_API_KEY or ALPACA_API_SECRET missing from .env", file=sys.stderr)
        sys.exit(1)

    policy = load_policy()
    init_throttle(policy["runtime"]["rate_limit_per_min"])
    feed_str = policy["runtime"]["feed"]
    feed = DataFeed(feed_str)

    trading_client = TradingClient(api_key, api_secret, paper=paper)
    stock_client = StockHistoricalDataClient(api_key, api_secret)
    option_client = OptionHistoricalDataClient(api_key, api_secret)

    # single timestamp shared by every artifact this run writes, so prices/chains/summary
    # can be paired reliably instead of drifting apart across a long run
    run_dt = datetime.now()
    run_ts_str = run_dt.strftime("%Y%m%d_%H%M%S")

    # DTE math anchors to the US/Eastern calendar date, not the VPS's local date -
    # a UTC-hosted VPS mid-session would otherwise be a day ahead
    vps_now = datetime.now().astimezone()
    run_date = datetime.now(ZoneInfo("America/New_York")).date()

    header("FORGETRADER UNIVERSE SCREENER")
    print(f"stage          : {args.stage}")
    print(f"feed in use    : {feed_str}")
    print(f"rate limit     : {policy['runtime']['rate_limit_per_min']}/min")
    print(f"snapshot batch : {policy['runtime']['snapshot_batch_size']}")
    print(f"--limit        : {args.limit or 'unlimited'}")
    print(f"--max-chains   : {args.max_chains or 'unlimited'}")
    print(f"VPS local time : {vps_now.isoformat()} (tzname={vps_now.tzname()})")
    print(f"DTE anchor date: {run_date} (US/Eastern)")

    survivors_a = stage_a(trading_client, args.limit)
    if args.stage == "a":
        print()
        print(f"[stage=a] stopping after Stage A. total calls made: {_call_count}")
        sys.exit(0)

    stage_b_result = stage_b(stock_client, trading_client, policy, survivors_a, feed, run_ts_str)
    if args.stage == "b":
        print()
        print(f"[stage=b] stopping after Stage B. total calls made: {_call_count}")
        sys.exit(0)

    stage_c_result = stage_c(
        trading_client, option_client, policy, stage_b_result["survivors"],
        args.max_chains, stage_b_result["collateral"]["available"],
        run_date, run_ts_str,
    )
    if args.stage == "c":
        print()
        print(f"[stage=c] stopping after Stage C. total calls made: {_call_count}")
        sys.exit(0)

    stage_d(trading_client, stage_b_result["collateral"], len(survivors_a),
            stage_b_result, stage_c_result, feed_str, run_ts_str)

    print()
    print(f"total API calls made this run: {_call_count}")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
