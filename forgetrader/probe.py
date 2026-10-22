"""Brief 13b.2 - isolated paper probe through place_csp.

Proves live: write-ahead -> submit -> CSP_PENDING -> (later run.py dry-runs)
day expiry -> cancel_or_expire -> FLAT, using a non-marketable whole-dollar
limit. Does NOT prove fill or assignment. place_csp is not modified here.

Run as `.venv/bin/python -m forgetrader.probe --contract <OCC> [--submit]`
from ~/forgetrader. Without --submit nothing is ever submitted.

Every refusal prints "PROBE REFUSED <check>", journals a decision row and a run
row "probe:refused:<check>", submits nothing, writes no state, and exits 3.
probe.py never calls write_state: the only state write is place_csp's
write-ahead. No host clock is read; every date comes from the broker clock.

Exit codes: 0 dry / submitted, 3 refused, 4 halt (submit failed after the
write-ahead; reconcile sorts out what the broker did).
"""
import argparse
import json
import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.data.enums import OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest
from alpaca.trading.client import TradingClient

import credentials
import forgetrader_state as fs
import screener
import watchlist
from forgetrader.submit import place_csp

WATCHLIST_PATH = fs.SCRIPT_DIR / "watchlist.json"

PROBE_MAX_QUOTE_AGE_MIN = 30  # PROVISIONAL
PROBE_MAX_LIMIT_TO_BID = 1.15

_OCC_TAIL = 15  # YYMMDD + C/P + 8-digit strike
GATES_SKIPPED = ["bid", "oi", "spread", "yield"]


class _Refused(Exception):
    def __init__(self, check):
        super().__init__(check)
        self.check = check


def _build_parser():
    p = argparse.ArgumentParser(prog="forgetrader.probe")
    p.add_argument("--contract", required=True, help="OCC put symbol")
    p.add_argument("--submit", action="store_true",
                   help="actually submit; without it the probe is a dry run")
    return p


def _checks(args, conn, run_id, stage):
    """Run the refusal sequence. Returns (tc, state, ts, sym, root, detail,
    limit) on success; raises _Refused(check) on the first failing check."""
    sym = args.contract

    # 1. paper only, before any client exists.
    _key, _secret, paper = credentials.load_credentials()
    if paper is not True:
        raise _Refused("not_paper")

    # 2. state must already exist; the probe never creates it.
    if not Path(fs.STATE_PATH).exists():
        raise _Refused("state_missing")
    state = fs.load_state(fs.STATE_PATH)
    stage[0] = state["wheel"]["stage"]

    # 3. clients, policy, throttle.
    tc = TradingClient(_key, _secret, paper=True)
    oc = OptionHistoricalDataClient(_key, _secret)
    policy = screener.load_policy()
    screener.init_throttle(policy["runtime"]["rate_limit_per_min"])

    # 4. broker clock is the only clock.
    screener.throttle("get_clock")
    clock = tc.get_clock()
    ts = getattr(clock, "timestamp", None)
    if ts is None or ts.tzinfo is None or ts.utcoffset() is None:
        raise _Refused("clock_invalid")
    if getattr(clock, "is_open", None) is not True:
        raise _Refused("market_closed")
    now_et = ts.astimezone(ZoneInfo("America/New_York"))
    run_date = now_et.date()

    # 5. must be FLAT and stable at the broker.
    fin = fs.read_broker_financials(tc, None)
    try:
        snap = fs.build_snapshot(state["wheel"], fin)
    except fs.SnapshotError as e:
        raise _Refused(f"not_flat_stable:{e}")
    _new, action = fs.reconcile(state, snap, run_date)
    if action != "noop:FLAT stable":
        raise _Refused(f"not_flat_stable:{action}")

    # 6. contract shape.
    expiry = fs._contract_expiry(sym)
    if expiry is None or sym[-9] != "P":
        raise _Refused("contract_invalid")
    root = sym[:-_OCC_TAIL]
    if expiry <= run_date:
        raise _Refused("contract_expired")

    # 7. watchlist + earnings.
    approved, _rejected, _warnings = watchlist.load_watchlist(
        str(WATCHLIST_PATH), now_et)
    entry = next((e for e in approved if e["ticker"] == root), None)
    if entry is None:
        raise _Refused("not_on_watchlist")
    earnings = watchlist._parse_date(entry["earnings_date"])
    if earnings is None or expiry >= earnings:
        raise _Refused("earnings_inside_option")

    # 8. collateral.
    strike = int(sym[-8:]) / 1000
    coll = screener.compute_collateral_numbers(tc, policy)
    if strike * 100 > coll["available"]:
        raise _Refused("collateral")

    # 9. quote.
    screener.throttle("get_option_latest_quote")
    quotes = oc.get_option_latest_quote(OptionLatestQuoteRequest(
        symbol_or_symbols=sym, feed=OptionsFeed.INDICATIVE))
    q = quotes.get(sym) if quotes else None
    if q is None:
        raise _Refused("quote_missing")
    bid, ask = q.bid_price, q.ask_price
    if bid is None or ask is None:
        raise _Refused("quote_missing")
    if not (math.isfinite(bid) and math.isfinite(ask)):
        raise _Refused("quote_invalid")
    if bid <= 0 or ask <= 0:
        raise _Refused("quote_missing")
    if bid > ask:
        raise _Refused("quote_crossed")
    qts = q.timestamp
    if qts is None or qts.tzinfo is None or qts.utcoffset() is None:
        raise _Refused("quote_stale")
    age_min = (ts - qts).total_seconds() / 60
    if age_min < 0 or age_min > PROBE_MAX_QUOTE_AGE_MIN:
        raise _Refused("quote_stale")

    # 10. whole-dollar limit strictly above the ask (non-marketable).
    limit = float(math.floor(ask) + 1)
    if limit > bid * PROBE_MAX_LIMIT_TO_BID:
        raise _Refused("collar_risk")

    detail = {
        "contract": sym, "root": root, "strike": strike,
        "expiry": expiry.isoformat(), "earnings": earnings.isoformat(),
        "bid": float(bid), "ask": float(ask), "quote_ts": qts.isoformat(),
        "age_min": age_min, "limit": limit, "available": coll["available"],
        "gates_skipped": GATES_SKIPPED,
    }
    return tc, state, ts, sym, root, detail, limit


def main(argv=None):
    args = _build_parser().parse_args(argv)
    run_id = fs.new_run_id()
    conn = fs.journal_connect(fs.JOURNAL_PATH)
    stage = ["UNKNOWN"]
    try:
        try:
            tc, state, ts, sym, root, detail, limit = _checks(
                args, conn, run_id, stage)
        except _Refused as r:
            print(f"PROBE REFUSED {r.check}")
            fs.record_decision(conn, run_id, stage_from=stage[0],
                               stage_to=stage[0],
                               action=f"probe:refused:{r.check}", detail="")
            fs.record_run(conn, run_id, f"probe:refused:{r.check}")
            return 3

        text = json.dumps(detail, sort_keys=True)
        fs.record_decision(conn, run_id, stage_from=fs.FLAT, stage_to=fs.FLAT,
                           action="probe:propose", detail=text)
        print(f"PROBE PROPOSE {text}")

        if not args.submit:
            fs.record_run(conn, run_id, "probe:dry")
            return 0

        proposal = {"final_pass": True, "contract_symbol": sym,
                    "underlying_symbol": root, "bid": limit, "probe": True}
        state_out, res = place_csp(tc, state, proposal, conn, run_id,
                                   ts.isoformat(), fs.STATE_PATH)
        outcome = res["outcome"]
        fs.record_decision(conn, run_id, stage_from=fs.FLAT,
                           stage_to=state_out["wheel"]["stage"],
                           action=f"probe:{outcome}",
                           detail=json.dumps(res, sort_keys=True))
        fs.record_run(conn, run_id, f"probe:{outcome}")
        if outcome == "submitted":
            print(f"PROBE SUBMITTED coid={res['client_order_id']} "
                  f"status={res['status']}")
            return 0
        if outcome == "refused":
            print(f"PROBE REFUSED {res['detail']}")
            return 3
        fs.alert_stub(f"HALT:probe {res['detail']} coid={res['client_order_id']}")
        print(f"PROBE HALT {res['detail']}", file=sys.stderr)
        return 4
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
