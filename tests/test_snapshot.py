"""Fixture harness for forgetrader_state.build_snapshot (Brief 12a).

build_snapshot is pure: (wheel dict, financials dict) -> the 4-key snapshot
reconcile() consumes, or SnapshotError. Inputs are plain dicts; expected
values are hardcoded literals. No alpaca-py needed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import forgetrader_state as fs

PUT = "NOK260116P00005000"
CALL = "NOK260116C00006000"
COID = "fgt-test-coid-1"


def wheel(stage, contract=None, coid=None, ticker="NOK"):
    return {"stage": stage, "ticker": ticker,
            "contract_symbol": contract, "client_order_id": coid}


def pos(sym, qty):
    return {"symbol": sym, "qty": qty, "avg_entry_price": 1.0,
            "market_value": 1.0}


def order(coid, sym="NOK", status="new"):
    return {"id": "oid-1", "client_order_id": coid, "symbol": sym,
            "status": status}


def fin(positions=(), orders=(), tracked=None):
    return {"positions": list(positions), "open_orders": list(orders),
            "tracked_order_status": tracked}


def snap(shares, put_open, call_open, status):
    return {"shares": shares, "put_open": put_open, "call_open": call_open,
            "pending_order_status": status}


# (name, wheel, financials, expected) -- expected is a dict (snapshot) or a
# str (SnapshotError message prefix).
FIXTURES = [
    ("01-flat-empty", wheel(fs.FLAT), fin(), snap(0, False, False, None)),
    ("02-flat-stray-shares", wheel(fs.FLAT, ticker=None),
     fin([pos("NOK", 100)]), "unexpected position"),
    ("03-flat-stray-order", wheel(fs.FLAT), fin(orders=[order("other")]),
     "unexpected open order"),
    ("04-csp-pending-new", wheel(fs.CSP_PENDING, PUT, COID),
     fin(orders=[order(COID)], tracked="new"), snap(0, False, False, "new")),
    ("05-csp-pending-not-found", wheel(fs.CSP_PENDING, PUT, COID),
     fin(tracked="not_found"), snap(0, False, False, "not_found")),
    ("06-csp-open-put-short", wheel(fs.CSP_OPEN, PUT, COID),
     fin([pos(PUT, -1)]), snap(0, True, False, None)),
    ("07-csp-open-put-long", wheel(fs.CSP_OPEN, PUT, COID),
     fin([pos(PUT, 1)]), "tracked contract"),
    ("08-csp-open-assigned-ford", wheel(fs.CSP_OPEN, PUT, COID),
     fin([pos("NOK", 100)]), snap(100, False, False, None)),
    ("09-csp-open-put-gone-50-shares", wheel(fs.CSP_OPEN, PUT, COID),
     fin([pos("NOK", 50)]), snap(50, False, False, None)),
    ("10-holding-100", wheel(fs.HOLDING, PUT, COID),
     fin([pos("NOK", 100)]), snap(100, False, False, None)),
    ("11-cc-open-call-short", wheel(fs.CC_OPEN, CALL, COID),
     fin([pos("NOK", 100), pos(CALL, -1)]), snap(100, False, True, None)),
    ("12-bad-right-char", wheel(fs.CSP_OPEN, "NOK260116X00005000", COID),
     fin([pos("NOK260116X00005000", -1)]), "malformed contract symbol"),
    ("13-missing-open-orders", wheel(fs.FLAT),
     {"positions": []}, "financials missing"),
    ("14-qty-none", wheel(fs.HOLDING, PUT, COID),
     fin([pos("NOK", None)]), "position NOK qty None"),
    ("15-tracked-plus-untracked-order", wheel(fs.CSP_PENDING, PUT, COID),
     fin(orders=[order(COID), order("stranger", "AAPL")], tracked="new"),
     "unexpected open order"),
    ("17-flat-stale-coid-open-order", wheel(fs.FLAT, coid=COID),
     fin(orders=[order(COID)]), "unexpected open order"),
    ("18-csp-open-tracked-order-still-open", wheel(fs.CSP_OPEN, PUT, COID),
     fin([pos(PUT, -1)], orders=[order(COID)]), "unexpected open order"),
    ("19-csp-pending-no-coid", wheel(fs.CSP_PENDING, PUT, None), fin(),
     "pending stage CSP_PENDING without client_order_id"),
    ("20-cc-pending-tracked-none", wheel(fs.CC_PENDING, CALL, COID),
     fin([pos("NOK", 100)], tracked=None),
     "pending stage CC_PENDING tracked_order_status missing"),
    ("21-csp-pending-not-found-ok", wheel(fs.CSP_PENDING, PUT, COID),
     fin(tracked="not_found"), snap(0, False, False, "not_found")),
]

def main():
    failures = []
    assertions = 0
    n_fixtures = 0

    for name, w, f, expected in FIXTURES:
        n_fixtures += 1
        try:
            got = fs.build_snapshot(w, f)
            raised = None
        except fs.SnapshotError as e:
            got, raised = None, str(e)
        assertions += 1
        if isinstance(expected, dict):
            if raised is not None or got != expected:
                failures.append(f"{name}: expected {expected!r} got "
                                f"{got!r} raised {raised!r}")
        else:
            if raised is None:
                failures.append(f"{name}: expected SnapshotError "
                                f"{expected!r}, got {got!r}")
            elif not raised.startswith(expected):
                failures.append(f"{name}: message {raised!r} does not start "
                                f"with {expected!r}")

    # 16 CHAIN: fixture 8 snapshot -> reconcile at CSP_OPEN
    n_fixtures += 1
    name = "16-chain-ford-reconcile"
    w = wheel(fs.CSP_OPEN, PUT, COID)
    s = fs.fresh_flat_state()
    s["wheel"].update(w)
    snapshot = fs.build_snapshot(w, fin([pos("NOK", 100)]))
    new_state, action = fs.reconcile(s, snapshot)
    for label, got, exp in (
        ("action", action, "CSP_OPEN->HOLDING (assigned)"),
        ("stage", new_state["wheel"]["stage"], "HOLDING"),
    ):
        assertions += 1
        if got != exp:
            failures.append(f"{name}.{label}: expected {exp!r} got {got!r}")

    if failures:
        print("FAIL")
        for line in failures:
            print("  " + line)
        return 1
    print(f"PASS: {n_fixtures} fixtures, {assertions} assertions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
