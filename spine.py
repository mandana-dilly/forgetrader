#!/usr/bin/env python3
"""
ForgeTrader spine - proves alpaca-py can authenticate and read account state.
Does nothing but read. No orders, no writes, no side effects.
"""
import os
import sys
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient

load_dotenv()

API_KEY = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_API_SECRET")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

if not API_KEY or not API_SECRET:
    print("FATAL: ALPACA_API_KEY or ALPACA_API_SECRET missing from .env", file=sys.stderr)
    sys.exit(1)

client = TradingClient(API_KEY, API_SECRET, paper=PAPER)

try:
    acct = client.get_account()
except Exception as e:
    print(f"FATAL: get_account() failed - {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)

print("===== ALPACA ACCOUNT STATE =====")
print(f"paper mode        : {PAPER}")
print(f"account number    : {acct.account_number}")
print(f"status            : {acct.status}")
print(f"currency          : {acct.currency}")
print(f"equity            : {acct.equity}")
print(f"cash              : {acct.cash}")
print(f"buying power      : {acct.buying_power}")
print(f"options BP        : {getattr(acct, 'options_buying_power', 'n/a')}")
print(f"trading blocked   : {acct.trading_blocked}")
print(f"account blocked   : {acct.account_blocked}")
print(f"pattern day trader: {acct.pattern_day_trader}")
print("================================")

try:
    positions = client.get_all_positions()
except Exception as e:
    print(f"FATAL: get_all_positions() failed - {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)

print(f"open positions    : {len(positions)}")
for p in positions:
    print(f"  {p.symbol:12s} qty={p.qty} avg_entry={p.avg_entry_price} mkt_val={p.market_value}")
print("================================")
print("SPINE OK - auth works, account readable, positions readable")
