"""Brief 13b.1 - write-ahead CSP submission.

place_csp() submits exactly one cash-secured put. Ordering is the contract:
validate and build with zero side effects, then persist intent (state.json,
then the journal intent row) BEFORE the broker is touched. Once anything past
the build step fails, the function never rolls back, rewrites state or
retries: it returns the CSP_PENDING state with outcome "halt" so the caller
persists from it and reconciliation sorts out what the broker actually did.

fs.* is always reached through the module so tests can monkeypatch it.
"""
import uuid
from decimal import Decimal, InvalidOperation

from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

import forgetrader_state as fs

CSP_QTY = 1  # hardcoded cap, never a parameter

_OCC_TAIL = 15  # YYMMDD + C/P + 8-digit strike


def _result(outcome, detail, coid=None, broker_order_id=None, status=None,
            broker_reached=False):
    return {
        "outcome": outcome, "detail": detail, "client_order_id": coid,
        "broker_order_id": broker_order_id, "status": status,
        "broker_reached": broker_reached,
    }


def _validate(state, proposal, as_of_ts):
    """Return (limit_price Decimal, None) on success, (None, failed_check) else."""
    try:
        if state["wheel"]["stage"] != fs.FLAT:
            return None, "stage_not_flat"
    except (KeyError, TypeError):
        return None, "stage_not_flat"
    if proposal.get("final_pass") is not True:
        return None, "final_pass_not_true"
    sym = proposal.get("contract_symbol")
    if not isinstance(sym, str) or len(sym) <= _OCC_TAIL:
        return None, "contract_symbol_invalid"
    und = proposal.get("underlying_symbol")
    if not isinstance(und, str) or not und or sym[:-_OCC_TAIL] != und:
        return None, "underlying_mismatch"
    if sym[-9] != "P":
        return None, "right_not_put"
    bid = proposal.get("bid")
    if type(bid) is not float:
        return None, "bid_not_float"
    try:
        lp = Decimal(repr(bid))
        if not (lp > 0 and lp == lp.quantize(Decimal("0.01"))):
            return None, "bid_not_2dp"
    except InvalidOperation:
        return None, "bid_not_2dp"
    if not isinstance(as_of_ts, str) or not as_of_ts:
        return None, "as_of_ts_missing"
    return lp, None


def place_csp(trading_client, state, proposal, conn, run_id, as_of_ts,
              state_path=fs.STATE_PATH):
    lp, failed = _validate(state, proposal, as_of_ts)
    if failed:
        return state, _result("refused", failed)

    sym = proposal["contract_symbol"]
    und = proposal["underlying_symbol"]
    try:
        new, t = fs.apply_trigger(state, "submit_csp")
        if (t != "FLAT->CSP_PENDING (submit_csp)" or new is state
                or new["wheel"]["stage"] != fs.CSP_PENDING):
            return state, _result("refused", f"apply_trigger_unexpected: {t}")
        coid = str(uuid.uuid4())
        wheel = new["wheel"]
        wheel["ticker"] = und
        wheel["contract_symbol"] = sym
        wheel["client_order_id"] = coid
        wheel["opened_at"] = as_of_ts
        wheel["strike"] = None
        wheel["expiry"] = None
        limit_price = float(lp)
        if float(str(lp)) != limit_price:
            return state, _result("refused", "limit_price_float_mismatch")
        req = LimitOrderRequest(
            symbol=sym, qty=CSP_QTY, side=OrderSide.SELL, type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY, limit_price=limit_price,
            client_order_id=coid)
    except Exception as e:
        return state, _result("refused", f"build_failed: {type(e).__name__}: {e}")

    broker_reached = False
    order = None
    try:
        fs.write_state(new, state_path)  # write-ahead
        fs.record_order(conn, run_id, sym, "sell", CSP_QTY, limit_price,
                        status=None, client_order_id=coid, broker_order_id=None)
        broker_reached = True
        order = trading_client.submit_order(req)  # exactly once
        status = order.status.value
        broker_order_id = str(order.id)
        fs.record_order(conn, run_id, sym, "sell", CSP_QTY, limit_price,
                        status=status, client_order_id=coid,
                        broker_order_id=broker_order_id)
        return new, _result("submitted", "submitted", coid, broker_order_id,
                            status, True)
    except Exception as e:
        try:
            fs.record_event(
                conn, run_id, "submit_exception", symbol=sym,
                detail=f"broker_reached={broker_reached} {type(e).__name__}: {e}")
        except Exception:
            pass
        try:
            fs.alert_stub(
                f"HALT:submit broker_reached={broker_reached} {type(e).__name__}")
        except Exception:
            pass
        seen_id = None
        try:
            seen_id = str(order.id) if order is not None else None
        except Exception:
            pass
        return new, _result(
            "halt", f"{type(e).__name__}: {e}", coid, seen_id, None,
            broker_reached)
