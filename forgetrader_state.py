#!/usr/bin/env python3
"""Brief 9 - persistence layer foundation.

Contract (the one rule everything hangs off):
    broker owns facts, state owns intent, reconciliation runs every cycle and
    halts loud on any unreconcilable gap.

This module is the foundation only. It defines:
  * the on-disk state schema (state.json)         -> fresh_flat_state / load_state / write_state
  * the wheel state machine                        -> STAGES / LEGAL_TRANSITIONS / apply_trigger
  * the reconciliation function (pure, SDK-free)   -> reconcile
  * the append-only journal (journal.db)           -> journal_connect / record_*
  * a HALT diagnostic path                         -> dump_halt / alert_stub
  * a manual test path                             -> CLI --show / --set-state / --init

It does NOT detect live assignments, place orders, or wire Telegram. Those sit
on top later. `reconcile` takes a plain dict of broker facts and never touches
the SDK, so the whole state machine is provable off-hours with mock data.

The one place the Alpaca SDK is touched is read_broker_financials(), which
mirrors spine.py's auth pattern to populate the broker-facts portion of a real
--init write. That import is local to the function on purpose: importing this
module for unit tests must never require alpaca-py.
"""
import argparse
import copy
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PATH = SCRIPT_DIR / "state.json"
JOURNAL_PATH = SCRIPT_DIR / "journal.db"
SCANS_DIR = SCRIPT_DIR / "scans"

# --------------------------------------------------------------------------- #
# State machine                                                              #
# --------------------------------------------------------------------------- #

FLAT = "FLAT"
CSP_PENDING = "CSP_PENDING"
CSP_OPEN = "CSP_OPEN"
HOLDING = "HOLDING"
CC_PENDING = "CC_PENDING"
CC_OPEN = "CC_OPEN"

# The 6 stages, in wheel order. Nothing outside this list is a valid stage.
STAGES = [FLAT, CSP_PENDING, CSP_OPEN, HOLDING, CC_PENDING, CC_OPEN]

# Explicit (from_stage, trigger) -> to_stage. Exactly these ten, no others.
# Anything not keyed here is illegal and must HALT, never mutate.
LEGAL_TRANSITIONS = {
    (FLAT, "submit_csp"): CSP_PENDING,
    (CSP_PENDING, "fill"): CSP_OPEN,
    (CSP_PENDING, "reject_or_cancel"): FLAT,
    (CSP_OPEN, "assigned"): HOLDING,            # put gone AND shares present
    (CSP_OPEN, "expired_worthless"): FLAT,      # put gone AND no shares
    (HOLDING, "submit_cc"): CC_PENDING,
    (CC_PENDING, "fill"): CC_OPEN,
    (CC_PENDING, "reject_or_cancel"): HOLDING,
    (CC_OPEN, "called_away"): FLAT,             # shares gone AND call gone
    (CC_OPEN, "cc_expired_worthless"): HOLDING, # shares present, call gone
}

# Order statuses that mean "still in flight, do nothing yet".
_ORDER_LIVE = frozenset(
    {None, "", "pending", "pending_new", "new", "accepted", "held",
     "accepted_for_bidding", "partially_filled", "pending_replace",
     "replaced", "calculated"}
)
# Order statuses that mean "this order is dead, no fill".
_ORDER_DEAD = frozenset(
    {"canceled", "cancelled", "rejected", "expired", "done_for_day",
     "pending_cancel", "stopped", "suspended"}
)
_ORDER_FILLED = frozenset({"filled"})

# A share lot for the wheel is exactly one round lot.
LOT = 100


def apply_trigger(state, trigger):
    """Pure. Apply `trigger` to the current stage via LEGAL_TRANSITIONS.

    Returns (new_state, action). On a legal pair, new_state is a deep copy with
    wheel.stage advanced and action "FROM->TO (trigger)". On an illegal pair the
    input state is returned UNCHANGED and action is "HALT:<reason>".
    """
    stage = state["wheel"]["stage"]
    key = (stage, trigger)
    if key not in LEGAL_TRANSITIONS:
        return state, f"HALT:no legal transition from {stage} on trigger {trigger!r}"
    to_stage = LEGAL_TRANSITIONS[key]
    new_state = copy.deepcopy(state)
    new_state["wheel"]["stage"] = to_stage
    return new_state, f"{stage}->{to_stage} ({trigger})"


def reconcile(state, broker_snapshot):
    """Pure function. No I/O, no SDK calls.

    Inputs
      state           - the current state dict (see fresh_flat_state()).
      broker_snapshot - a plain dict of broker facts:
          shares               (int, REQUIRED) shares of the wheel ticker held.
          put_open             (bool) the tracked CSP still shows as open.
          call_open            (bool) the tracked CC still shows as open.
          pending_order_status (str|None) status of the in-flight order while
                               stage is *_PENDING (alpaca order-status strings).

    Returns (new_state, action):
      * a legal broker-driven transition -> transitioned state + "FROM->TO (trig)"
      * nothing to do                     -> unchanged state + "noop:<why>"
      * any (stage, broker_reality) that matches no legal transition
                                          -> unchanged state + "HALT:<reason>"
        (stage is NEVER mutated on HALT).

    Critical rule: at CSP_OPEN with the put absent, the choice between HOLDING
    and FLAT is made by INSPECTING broker_snapshot["shares"] - never inferred
    from the put's absence alone. Same for CC_OPEN and called_away vs expiry.
    """
    stage = state["wheel"]["stage"]

    if "shares" not in broker_snapshot:
        return state, "HALT:broker_snapshot missing required key 'shares'"
    shares = broker_snapshot["shares"]
    if not isinstance(shares, int) or isinstance(shares, bool) or shares < 0:
        return state, f"HALT:broker_snapshot['shares'] not a non-negative int: {shares!r}"

    put_open = bool(broker_snapshot.get("put_open"))
    call_open = bool(broker_snapshot.get("call_open"))
    order_status = broker_snapshot.get("pending_order_status")

    if stage == FLAT:
        if shares == 0 and not put_open and not call_open:
            return state, "noop:FLAT stable"
        return state, (
            f"HALT:FLAT but broker shows shares={shares} put_open={put_open} "
            f"call_open={call_open}"
        )

    if stage == CSP_PENDING:
        if order_status in _ORDER_FILLED or put_open:
            if shares != 0:
                return state, f"HALT:CSP_PENDING filling but broker shows shares={shares}"
            return apply_trigger(state, "fill")
        if order_status in _ORDER_DEAD:
            if shares != 0 or put_open:
                return state, (
                    f"HALT:CSP_PENDING order {order_status} but broker shows "
                    f"shares={shares} put_open={put_open}"
                )
            return apply_trigger(state, "reject_or_cancel")
        if order_status in _ORDER_LIVE:
            return state, "noop:CSP_PENDING awaiting fill"
        return state, f"HALT:CSP_PENDING unknown pending_order_status {order_status!r}"

    if stage == CSP_OPEN:
        if put_open:
            if shares == 0:
                return state, "noop:CSP_OPEN stable"
            return state, f"HALT:CSP_OPEN put still open but broker shows shares={shares}"
        # Put is absent. Decide by shares, never by the absence itself.
        if shares == LOT:
            return apply_trigger(state, "assigned")
        if shares == 0:
            return apply_trigger(state, "expired_worthless")
        return state, (
            f"HALT:CSP_OPEN put gone and shares={shares} is neither a {LOT}-share "
            f"assignment lot nor flat"
        )

    if stage == HOLDING:
        if shares == LOT and not put_open and not call_open:
            return state, "noop:HOLDING stable"
        return state, (
            f"HALT:HOLDING but broker shows shares={shares} put_open={put_open} "
            f"call_open={call_open}"
        )

    if stage == CC_PENDING:
        if order_status in _ORDER_FILLED or call_open:
            if shares != LOT:
                return state, f"HALT:CC_PENDING filling but broker shows shares={shares}"
            return apply_trigger(state, "fill")
        if order_status in _ORDER_DEAD:
            if shares != LOT or call_open:
                return state, (
                    f"HALT:CC_PENDING order {order_status} but broker shows "
                    f"shares={shares} call_open={call_open}"
                )
            return apply_trigger(state, "reject_or_cancel")
        if order_status in _ORDER_LIVE:
            return state, "noop:CC_PENDING awaiting fill"
        return state, f"HALT:CC_PENDING unknown pending_order_status {order_status!r}"

    if stage == CC_OPEN:
        if call_open:
            if shares == LOT:
                return state, "noop:CC_OPEN stable"
            return state, f"HALT:CC_OPEN call still open but broker shows shares={shares}"
        # Call is absent. Decide by shares, never by the absence itself.
        if shares == 0:
            return apply_trigger(state, "called_away")
        if shares == LOT:
            return apply_trigger(state, "cc_expired_worthless")
        return state, (
            f"HALT:CC_OPEN call gone and shares={shares} is neither flat (called "
            f"away) nor a full {LOT}-share lot (expiry)"
        )

    return state, f"HALT:unknown stage {stage!r}"


# --------------------------------------------------------------------------- #
# State schema + read/write                                                  #
# --------------------------------------------------------------------------- #

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value):
    """spine.py reads options_buying_power as getattr(acct, ..., 'n/a').

    Coerce anything that is not a real finite number to None. Never returns
    float('n/a') / NaN. Downstream must fail-closed on None, never float(None).
    """
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return f


def _safe_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def require_options_buying_power(state):
    """Fail-closed accessor for downstream sizing code.

    Returns a float, or raises ValueError when options_buying_power is null.
    Callers must NEVER do float(state["options_buying_power"]) directly.
    """
    obp = state.get("options_buying_power")
    if obp is None:
        raise ValueError("options_buying_power is null - fail closed, do not size against None")
    return float(obp)


def fresh_flat_state(equity=0.0, cash=0.0, options_buying_power=None,
                     high_water_mark=None, positions=None, open_orders=None,
                     last_outcome="init"):
    """Build a schema-complete state dict at stage FLAT with an empty wheel."""
    equity = _safe_float(equity) or 0.0
    if high_water_mark is None:
        high_water_mark = equity
    now = _now_iso()
    return {
        "generated_at": now,
        "equity": equity,
        "cash": _safe_float(cash) or 0.0,
        "options_buying_power": _safe_float(options_buying_power),
        "high_water_mark": _safe_float(high_water_mark) or 0.0,
        "wheel": {
            "stage": FLAT,
            "ticker": None,
            "cost_basis": None,
            "intent": None,
            "contract_symbol": None,
            "strike": None,
            "expiry": None,
            "opened_at": None,
        },
        "positions": list(positions) if positions else [],
        "open_orders": list(open_orders) if open_orders else [],
        "last_run": {"ts": now, "outcome": last_outcome},
    }


def load_state(path=STATE_PATH):
    """Read state.json. Raises FileNotFoundError if it does not exist."""
    with open(path, "r") as f:
        return json.load(f)


def write_state(state, path=STATE_PATH):
    """Atomic write: temp file in the same dir, then os.replace()."""
    path = Path(path)
    state["generated_at"] = _now_iso()
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- #
# Journal (sqlite, append-only, explicit columns, no positional access)      #
# --------------------------------------------------------------------------- #

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id  TEXT NOT NULL,
    ts      TEXT NOT NULL,
    outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    run_id     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    stage_from TEXT,
    stage_to   TEXT,
    action     TEXT NOT NULL,
    detail     TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    run_id      TEXT NOT NULL,
    ts          TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         INTEGER,
    limit_price REAL,
    status      TEXT
);
CREATE TABLE IF NOT EXISTS events (
    run_id     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    event_type TEXT NOT NULL,
    symbol     TEXT,
    detail     TEXT
);
"""


def journal_connect(path=JOURNAL_PATH):
    """Open journal.db, ensure schema, return a Row-factory connection."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row  # named access only
    conn.executescript(_DDL)
    conn.commit()
    return conn


def new_run_id():
    return uuid.uuid4().hex


def record_run(conn, run_id, outcome, ts=None):
    conn.execute(
        "INSERT INTO runs (run_id, ts, outcome) VALUES (:run_id, :ts, :outcome)",
        {"run_id": run_id, "ts": ts or _now_iso(), "outcome": outcome},
    )
    conn.commit()


def record_decision(conn, run_id, stage_from, stage_to, action, detail="", ts=None):
    conn.execute(
        "INSERT INTO decisions (run_id, ts, stage_from, stage_to, action, detail) "
        "VALUES (:run_id, :ts, :stage_from, :stage_to, :action, :detail)",
        {
            "run_id": run_id, "ts": ts or _now_iso(),
            "stage_from": stage_from, "stage_to": stage_to,
            "action": action, "detail": detail,
        },
    )
    conn.commit()


def record_order(conn, run_id, symbol, side, qty, limit_price, status, ts=None):
    conn.execute(
        "INSERT INTO orders (run_id, ts, symbol, side, qty, limit_price, status) "
        "VALUES (:run_id, :ts, :symbol, :side, :qty, :limit_price, :status)",
        {
            "run_id": run_id, "ts": ts or _now_iso(), "symbol": symbol,
            "side": side, "qty": qty, "limit_price": limit_price, "status": status,
        },
    )
    conn.commit()


def record_event(conn, run_id, event_type, symbol=None, detail="", ts=None):
    conn.execute(
        "INSERT INTO events (run_id, ts, event_type, symbol, detail) "
        "VALUES (:run_id, :ts, :event_type, :symbol, :detail)",
        {
            "run_id": run_id, "ts": ts or _now_iso(),
            "event_type": event_type, "symbol": symbol, "detail": detail,
        },
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# HALT diagnostics + alerting                                                #
# --------------------------------------------------------------------------- #

def alert_stub(msg):
    """Placeholder alert sink. Telegram wiring is a later one-line swap."""
    print(f"ALERT: {msg}")


def dump_halt(broker_snapshot, state, reason):
    """Write scans/state_halt_<ts>.txt with the reason, current state, and the
    broker snapshot that triggered the HALT. Returns the path written."""
    SCANS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    path = SCANS_DIR / f"state_halt_{ts}.txt"
    with open(path, "w") as f:
        f.write("FORGETRADER STATE HALT\n")
        f.write(f"written_at : {_now_iso()}\n")
        f.write(f"reason     : {reason}\n\n")
        f.write("----- current state -----\n")
        f.write(json.dumps(state, indent=2, default=str))
        f.write("\n\n----- broker snapshot -----\n")
        f.write(json.dumps(broker_snapshot, indent=2, default=str))
        f.write("\n")
    return path


def reconcile_and_handle(state, broker_snapshot, conn=None, run_id=None):
    """Non-pure wrapper around reconcile(): on a HALT action it writes the
    diagnostic dump and fires alert_stub; on any action it journals a decision
    row when a connection + run_id are supplied. Returns (new_state, action)."""
    stage_from = state["wheel"]["stage"]
    new_state, action = reconcile(state, broker_snapshot)
    if action.startswith("HALT:"):
        reason = action[len("HALT:"):]
        path = dump_halt(broker_snapshot, state, reason)
        alert_stub(f"HALT {reason} | dump {path}")
    if conn is not None and run_id is not None:
        record_decision(
            conn, run_id, stage_from, new_state["wheel"]["stage"], action,
            detail=json.dumps(broker_snapshot, default=str),
        )
    return new_state, action


# --------------------------------------------------------------------------- #
# SDK-touching helper (isolated; mirrors spine.py)                           #
# --------------------------------------------------------------------------- #

def read_broker_financials():
    """Reuse spine.py's auth pattern to read account + positions for a real
    state write. Import is local so unit tests never need alpaca-py. Raises on
    any auth / network failure - the caller decides whether to fail closed."""
    from dotenv import load_dotenv
    from alpaca.trading.client import TradingClient

    load_dotenv()
    api_key = os.environ.get("ALPACA_API_KEY")
    api_secret = os.environ.get("ALPACA_API_SECRET")
    paper = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
    if not api_key or not api_secret:
        raise RuntimeError("ALPACA_API_KEY or ALPACA_API_SECRET missing from .env")

    client = TradingClient(api_key, api_secret, paper=paper)
    acct = client.get_account()
    positions = client.get_all_positions()

    raw_obp = getattr(acct, "options_buying_power", "n/a")
    return {
        "equity": _safe_float(acct.equity) or 0.0,
        "cash": _safe_float(acct.cash) or 0.0,
        "options_buying_power": _safe_float(raw_obp),  # 'n/a' -> None
        "positions": [
            {
                "symbol": p.symbol,
                "qty": _safe_int(p.qty),
                "avg_entry_price": _safe_float(p.avg_entry_price),
                "market_value": _safe_float(p.market_value),
            }
            for p in positions
        ],
    }


# --------------------------------------------------------------------------- #
# CLI                                                                        #
# --------------------------------------------------------------------------- #

def cmd_show(_args):
    try:
        state = load_state()
    except FileNotFoundError:
        print(f"no state at {STATE_PATH} - run --init first", file=sys.stderr)
        return 1
    print(json.dumps(state, indent=2))
    return 0


def cmd_init(_args):
    financials = {}
    try:
        financials = read_broker_financials()
        print("broker facts read OK (equity/cash/options_bp/positions populated)")
    except Exception as e:  # noqa: BLE001 - off-hours / no-network is expected
        print(f"WARN: broker read failed ({type(e).__name__}: {e}); "
              f"writing FLAT state with null financials", file=sys.stderr)

    state = fresh_flat_state(
        equity=financials.get("equity", 0.0),
        cash=financials.get("cash", 0.0),
        options_buying_power=financials.get("options_buying_power"),
        positions=financials.get("positions"),
        last_outcome="init",
    )
    write_state(state)

    conn = journal_connect()
    run_id = new_run_id()
    record_run(conn, run_id, "init")
    record_decision(conn, run_id, None, FLAT, "init", detail="fresh FLAT state written")
    conn.close()

    print(f"wrote fresh FLAT state -> {STATE_PATH}")
    return 0


def cmd_set_state(args):
    stage = args.set_state
    if stage not in STAGES:
        print(f"invalid stage {stage!r}; one of {STAGES}", file=sys.stderr)
        return 2
    try:
        state = load_state()
    except FileNotFoundError:
        print(f"no state at {STATE_PATH} - run --init first", file=sys.stderr)
        return 1

    stage_from = state["wheel"]["stage"]
    state["wheel"]["stage"] = stage
    state["last_run"] = {"ts": _now_iso(), "outcome": f"manual_set_state:{stage}"}
    write_state(state)

    conn = journal_connect()
    run_id = new_run_id()
    record_run(conn, run_id, "manual_set_state")
    record_decision(conn, run_id, stage_from, stage, "manual_set_state",
                    detail="CLI --set-state (testing only)")
    conn.close()

    print(f"stage {stage_from} -> {stage} (manual)")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="forgetrader_state",
        description="ForgeTrader persistence layer foundation (Brief 9).",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="print current state.json")
    g.add_argument("--init", action="store_true",
                   help="write a fresh FLAT state (broker facts if reachable)")
    g.add_argument("--set-state", metavar="STAGE", choices=STAGES,
                   help="manually set wheel.stage for testing")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.show:
        return cmd_show(args)
    if args.init:
        return cmd_init(args)
    if args.set_state:
        return cmd_set_state(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
