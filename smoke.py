#!/usr/bin/env python3
"""
ForgeTrader Week 1 smoke test - proves the option-chain read path and the
order submit/cancel round trip. Plumbing test, not a strategy test.
"""
import argparse
import os
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

from alpaca.common.exceptions import APIError
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderSide,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrdersRequest,
    LimitOrderRequest,
)

UNDERLYING = "F"
COLLATERAL_CEILING_STRIKE = 13.00
DAYS_OUT_MIN = 14
DAYS_OUT_MAX = 35
ORDER_LIMIT_MULTIPLIER = 5


def header(title: str) -> None:
    print()
    print(f"===== {title} =====")


def bid_absent(quote) -> bool:
    if quote is None:
        return True
    if quote.bid_price is None or quote.bid_price <= 0:
        return True
    if quote.bid_size is None or quote.bid_size <= 0:
        return True
    return False


def ask_absent(quote) -> bool:
    if quote is None:
        return True
    if quote.ask_price is None or quote.ask_price <= 0:
        return True
    if quote.ask_size is None or quote.ask_size <= 0:
        return True
    return False


def parse_open_interest(raw_oi):
    if raw_oi is None:
        return None
    try:
        return int(raw_oi)
    except (ValueError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

    if not api_key or not api_secret:
        print("FATAL: ALPACA_API_KEY or ALPACA_API_SECRET missing from .env", file=sys.stderr)
        sys.exit(1)

    trading_client = TradingClient(api_key, api_secret, paper=paper)
    stock_data_client = StockHistoricalDataClient(api_key, api_secret)
    option_data_client = OptionHistoricalDataClient(api_key, api_secret)

    # ---------------------------------------------------------------
    header("STEP 1 - ACCOUNT")
    # ---------------------------------------------------------------
    acct = trading_client.get_account()
    positions = trading_client.get_all_positions()
    open_orders = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))

    print(f"account number      : {acct.account_number}")
    print(f"equity               : {acct.equity}")
    print(f"cash                 : {acct.cash}")
    print(f"options buying power : {acct.options_buying_power}")
    print(f"options approved lvl : {acct.options_approved_level}")
    print(f"options trading lvl  : {acct.options_trading_level}")
    print(f"open position count  : {len(positions)}")
    print(f"open order count     : {len(open_orders)}")

    cash_available = float(acct.cash)

    # ---------------------------------------------------------------
    header("STEP 2 - UNDERLYING PRICE")
    # ---------------------------------------------------------------
    trade_resp = stock_data_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=UNDERLYING)
    )
    trade = trade_resp.get(UNDERLYING) if trade_resp is not None else None

    if trade is None or trade.price is None:
        print(f"REJECT: no latest trade price available for {UNDERLYING}")
        sys.exit(1)

    underlying_price = trade.price
    now_utc = datetime.now(timezone.utc)
    age_minutes = (now_utc - trade.timestamp).total_seconds() / 60

    print(f"underlying           : {UNDERLYING}")
    print(f"price                : {underlying_price}")
    print(f"quote timestamp      : {trade.timestamp}")
    print(f"now (UTC)            : {now_utc}")
    print(f"age (minutes)        : {age_minutes:.2f}")

    # ---------------------------------------------------------------
    header("STEP 3 - CONTRACT METADATA")
    # ---------------------------------------------------------------
    today = date.today()
    exp_gte = today + timedelta(days=DAYS_OUT_MIN)
    exp_lte = today + timedelta(days=DAYS_OUT_MAX)

    print(f"filter: underlying={UNDERLYING} type=PUT status=ACTIVE "
          f"expiration=[{exp_gte}, {exp_lte}] strike<={COLLATERAL_CEILING_STRIKE}")

    all_contracts = []
    page_token = None
    page_num = 0
    while True:
        page_num += 1
        req = GetOptionContractsRequest(
            underlying_symbols=[UNDERLYING],
            status=AssetStatus.ACTIVE,
            type=ContractType.PUT,
            expiration_date_gte=exp_gte,
            expiration_date_lte=exp_lte,
            strike_price_lte=str(COLLATERAL_CEILING_STRIKE),
            limit=10000,
            page_token=page_token,
        )
        resp = trading_client.get_option_contracts(req)
        page_contracts = resp.option_contracts or []
        print(f"page {page_num}: returned {len(page_contracts)} contracts, "
              f"next_page_token={resp.next_page_token!r}")
        all_contracts.extend(page_contracts)
        page_token = resp.next_page_token
        if not page_token:
            break

    print(f"pagination complete  : {page_num} page(s) fetched, "
          f"final next_page_token=None confirms no truncation")
    print(f"total contracts      : {len(all_contracts)}")

    if len(all_contracts) == 0:
        print("REJECT: zero contracts returned for the given filter")
        sys.exit(1)

    print(f"{'OCC symbol':<24}  {'strike':>8}  {'expiration':>12}  {'DTE':>4}  "
          f"{'OI (raw)':>10}  {'OI (parsed)':>15}")
    contract_meta_by_symbol = {}
    for c in all_contracts:
        dte = (c.expiration_date - today).days
        oi_parsed = parse_open_interest(c.open_interest)
        oi_parsed_str = str(oi_parsed) if oi_parsed is not None else "OI: UNAVAILABLE"
        print(f"{c.symbol:<24}  {c.strike_price:>8.2f}  {str(c.expiration_date):>12}  {dte:>4}  "
              f"{str(c.open_interest):>10}  {oi_parsed_str:>15}")
        contract_meta_by_symbol[c.symbol] = c

    # ---------------------------------------------------------------
    header("STEP 4 - QUOTE SNAPSHOT")
    # ---------------------------------------------------------------
    chain_req = OptionChainRequest(
        underlying_symbol=UNDERLYING,
        type=ContractType.PUT,
        strike_price_lte=COLLATERAL_CEILING_STRIKE,
        expiration_date_gte=exp_gte,
        expiration_date_lte=exp_lte,
    )
    snapshots = option_data_client.get_option_chain(chain_req)
    print(f"snapshot response contains {len(snapshots)} symbols")

    print(f"{'OCC symbol':<24}  {'bid':>8}  {'ask':>8}  {'mid':>8}  "
          f"{'bid_sz':>8}  {'ask_sz':>8}  {'quote ts':<32}")

    no_snapshot_count = 0
    bid_absent_count = 0
    ask_absent_count = 0
    quotable_contracts = {}  # symbol -> (contract, quote, mid)

    for symbol, contract in contract_meta_by_symbol.items():
        snap = snapshots.get(symbol)
        if snap is None:
            no_snapshot_count += 1
            print(f"{symbol:<24}  {'NO SNAPSHOT':>8}")
            continue

        quote = snap.latest_quote
        b_absent = bid_absent(quote)
        a_absent = ask_absent(quote)
        if b_absent:
            bid_absent_count += 1
        if a_absent:
            ask_absent_count += 1

        raw_bid = quote.bid_price if quote is not None else None
        raw_ask = quote.ask_price if quote is not None else None
        raw_bid_sz = quote.bid_size if quote is not None else None
        raw_ask_sz = quote.ask_size if quote is not None else None
        ts = quote.timestamp if quote is not None else None

        if b_absent or a_absent:
            mid_str = "REJECT"
            reasons = []
            if b_absent:
                reasons.append("bid absent")
            if a_absent:
                reasons.append("ask absent")
            print(f"{symbol:<24}  {str(raw_bid):>8}  {str(raw_ask):>8}  {mid_str:>8}  "
                  f"{str(raw_bid_sz):>8}  {str(raw_ask_sz):>8}  {str(ts):<32}  ({', '.join(reasons)})")
        else:
            mid = (raw_bid + raw_ask) / 2
            print(f"{symbol:<24}  {raw_bid:>8.2f}  {raw_ask:>8.2f}  {mid:>8.2f}  "
                  f"{raw_bid_sz:>8.0f}  {raw_ask_sz:>8.0f}  {str(ts):<32}")
            quotable_contracts[symbol] = (contract, quote, mid)

    print(f"STEP 3 contracts with NO snapshot entry at all : {no_snapshot_count}")
    print(f"STEP 3 contracts with bid absent (incl. zero)  : {bid_absent_count}")
    print(f"STEP 3 contracts with ask absent (incl. zero)  : {ask_absent_count}")

    # ---------------------------------------------------------------
    header("STEP 5 - SELECT ONE CONTRACT")
    # ---------------------------------------------------------------
    if not quotable_contracts:
        print("REJECT: no contracts have both metadata and a usable bid")
        sys.exit(1)

    target_strike = underlying_price * 0.90
    print(f"underlying price     : {underlying_price}")
    print(f"target strike (10% below): {target_strike:.4f}")

    best_symbol = None
    best_variance = None
    for symbol, (contract, quote, mid) in quotable_contracts.items():
        variance = abs(contract.strike_price - target_strike)
        if best_variance is None or variance < best_variance:
            best_variance = variance
            best_symbol = symbol

    sel_contract, sel_quote, sel_mid = quotable_contracts[best_symbol]
    collateral_required = sel_contract.strike_price * 100
    collateral_ok = collateral_required <= cash_available

    print(f"chosen symbol        : {sel_contract.symbol}")
    print(f"chosen strike        : {sel_contract.strike_price}")
    print(f"variance from target : {best_variance:.4f}")
    print(f"collateral required  : {collateral_required:.2f}  (strike * 100)")
    print(f"cash available       : {cash_available:.2f}")
    print(f"collateral <= cash   : {collateral_ok}")

    # ---------------------------------------------------------------
    header("STEP 6 - SUBMIT AND CANCEL")
    # ---------------------------------------------------------------
    limit_price = round(sel_quote.ask_price * ORDER_LIMIT_MULTIPLIER, 2)
    print(f"intended order       : SELL 1 {sel_contract.symbol} LIMIT {limit_price} TIF=DAY")
    print(f"ask price            : {sel_quote.ask_price}")
    print(f"limit price = ask * {ORDER_LIMIT_MULTIPLIER} = {limit_price}")
    print(f"unfillable by design : a sell limit {ORDER_LIMIT_MULTIPLIER}x above market ask "
          f"cannot execute, so this proves submit/cancel without risking a fill")

    if not args.submit:
        print()
        print("DRY RUN: no order submitted. Pass --submit to place and cancel a real order.")
        sys.exit(0)

    order_req = LimitOrderRequest(
        symbol=sel_contract.symbol,
        qty=1,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )

    try:
        order = trading_client.submit_order(order_req)
    except APIError as e:
        print(f"ORDER REJECTED: {type(e).__name__}: {e}")
        sys.exit(0)

    print(f"order id             : {order.id}")
    print(f"status                : {order.status}")
    print(f"symbol                : {order.symbol}")
    print(f"qty                   : {order.qty}")
    print(f"limit price           : {order.limit_price}")

    time.sleep(2)
    refetched = trading_client.get_order_by_id(order.id)
    print(f"status after 2s       : {refetched.status}")

    cancellable = {"new", "accepted", "pending_new", "partially_filled", "held"}
    status_str = str(refetched.status).split(".")[-1].lower()
    if status_str in cancellable:
        try:
            trading_client.cancel_order_by_id(order.id)
        except APIError as e:
            print(f"CANCEL FAILED: {type(e).__name__}: {e}")
    else:
        print(f"SKIP CANCEL: order status {refetched.status} is terminal")
    time.sleep(2)
    final = trading_client.get_order_by_id(order.id)
    print(f"status after cancel   : {final.status}")

    remaining_open = trading_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    print(f"open orders remaining : {len(remaining_open)}")
    for o in remaining_open:
        print(f"  {o.id} {o.symbol} {o.status}")

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
