"""Shared gate logic for ForgeTrader.

Relocated UNTOUCHED from screener.py (Brief 7, Option A). Do not edit the
moved functions without updating the golden-fixture regression test (Brief 7b).
"""

FAILS_DELIM = ";"


def parse_open_interest(raw_oi):
    if raw_oi is None:
        return None
    try:
        return int(raw_oi)
    except (ValueError, TypeError):
        return None


def evaluate_contract(contract, snap, underlying_price, underlying_price_ts,
                       underlying_staleness_min, today, dte_min, dte_max,
                       otm_target_pct, min_bid, min_oi, max_spread_abs, max_spread_to_bid_ratio,
                       min_yield_pct, available, max_staleness_min):
    fails = []

    adjusted_pass = contract.root_symbol == contract.underlying_symbol
    if not adjusted_pass:
        fails.append("adjusted_contract")

    staleness_pass = underlying_staleness_min is not None and underlying_staleness_min <= max_staleness_min
    if not staleness_pass:
        fails.append("stale_underlying_price")

    dte = (contract.expiration_date - today).days
    dte_pass = dte_min <= dte <= dte_max
    if not dte_pass:
        fails.append("dte")

    strike = contract.strike_price
    if underlying_price and underlying_price != 0:
        otm_pct = (underlying_price - strike) / underlying_price * 100
        otm_pass = otm_pct >= otm_target_pct
        if not otm_pass:
            fails.append("otm_below_target")
    else:
        otm_pct = None
        otm_pass = False
        fails.append("undefined_metric_otm")

    if snap is None:
        bid = ask = mid = None
        bid_pass = False
        fails.append("no_snapshot")
    elif snap.latest_quote is None:
        bid = ask = mid = None
        bid_pass = False
        fails.append("null_quote")
    else:
        q = snap.latest_quote
        bid = q.bid_price
        ask = q.ask_price

        if bid is None:
            bid_pass = False
            fails.append("null_bid")
        elif bid == 0:
            bid_pass = False
            fails.append("zero_bid")
        elif bid < min_bid:
            bid_pass = False
            fails.append("bid_too_low")
        else:
            bid_pass = True

        if bid is not None and ask is not None and (bid + ask) != 0:
            mid = (bid + ask) / 2
        else:
            mid = None

    oi = parse_open_interest(contract.open_interest)
    if oi is None:
        oi_pass = False
        fails.append("null_oi")
    elif oi < min_oi:
        oi_pass = False
        fails.append("oi_too_low")
    else:
        oi_pass = True

    if bid is not None and ask is not None and mid not in (None, 0):
        spread_abs = ask - bid
        spread_pct = (spread_abs / mid) * 100
    else:
        spread_abs = None
        spread_pct = None

    if spread_abs is None:
        spread_pass = False
        fails.append("undefined_metric_spread")
    elif spread_abs > max_spread_abs:
        spread_pass = False
        fails.append("spread_too_wide")
    elif bid is None or bid <= 0:
        spread_pass = False
        fails.append("spread_ratio_undefined_no_bid")
    elif spread_abs > max_spread_to_bid_ratio * bid:
        spread_pass = False
        fails.append("spread_wide_vs_bid")
    else:
        spread_pass = True

    collateral = strike * 100
    collateral_pass = collateral <= available
    if not collateral_pass:
        fails.append("collateral_exceeds_available")

    if bid is not None and collateral not in (None, 0) and dte > 0:
        premium_total = bid * 100
        annualized_yield_pct = (premium_total / collateral) * (365 / dte) * 100
        yield_pass = annualized_yield_pct >= min_yield_pct
        if not yield_pass:
            fails.append("yield_too_low")
    else:
        annualized_yield_pct = None
        yield_pass = False
        fails.append("undefined_metric_yield")

    all_pass = (adjusted_pass and staleness_pass and dte_pass and otm_pass
                and bid_pass and oi_pass and spread_pass and collateral_pass
                and yield_pass)

    return {
        "underlying_price": underlying_price,
        "underlying_price_ts": underlying_price_ts,
        "underlying_staleness_min": round(underlying_staleness_min, 2) if underlying_staleness_min is not None else "",
        "contract_symbol": contract.symbol,
        "root_symbol": contract.root_symbol,
        "expiration_date": str(contract.expiration_date),
        "adjusted_pass": adjusted_pass,
        "staleness_pass": staleness_pass,
        "dte": dte, "dte_pass": dte_pass,
        "strike": strike,
        "otm_pct": round(otm_pct, 4) if otm_pct is not None else "",
        "otm_pass": otm_pass,
        "bid": bid if bid is not None else "",
        "ask": ask if ask is not None else "",
        "mid": round(mid, 4) if mid is not None else "",
        "bid_pass": bid_pass,
        "open_interest": oi if oi is not None else "",
        "oi_pass": oi_pass,
        "spread_abs": round(spread_abs, 4) if spread_abs is not None else "",
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else "",
        "spread_pass": spread_pass,
        "collateral": collateral, "collateral_pass": collateral_pass,
        "annualized_yield_pct": round(annualized_yield_pct, 4) if annualized_yield_pct is not None else "",
        "yield_pass": yield_pass,
        "all_pass": all_pass,
        "rejected_by": FAILS_DELIM.join(fails),
    }
