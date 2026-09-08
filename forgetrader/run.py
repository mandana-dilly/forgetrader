#!/usr/bin/env python3
"""Brief 10 (B-minimal) - the first integrated, Alpaca-touching run path.

Run-to-completion, single pass, no loop, no daemon:
  1. read real account state via forgetrader_state.read_broker_financials
  2. load prior state.json if present, else build a fresh FLAT state
  3. refresh the money fields on that FLAT state from this read
  4. build a guarded zero-position FLAT broker snapshot
  5. run reconcile_and_handle FLAT-against-FLAT (journals the decision;
     dumps + alerts on HALT)
  6. write state.json atomically, journal the run, exit per the code contract

Scope: provable TODAY at position ZERO. It does NOT build the
facts->snapshot mapper (Brief A) and does NOT submit orders. Non-FLAT prior
state is out of scope (exit 3), not a failure.

Invoked as `python -m forgetrader.run` from ~/forgetrader, where cwd is on
sys.path so the flat sibling module imports below resolve.

Exit codes (morning-review contract):
  0  clean FLAT noop (expected happy path today)
  3  prior state stage != FLAT (out of scope for this brief)
  4  reconcile returned non-noop at FLAT (HALT / unexpected transition)
  non-zero via uncaught exception = auth / network / guard failure (loud)
"""
import argparse
import sys

import forgetrader_state as fs


def build_flat_snapshot(financials):
    """FLAT-only. Assert the broker confirms zero positions, then return the
    canonical FLAT snapshot. HALT (raise) if positions are non-empty - that is
    the signal the real facts->snapshot mapper (Brief A) is needed and this
    hardcoded path is no longer valid."""
    positions = financials.get("positions") or []
    if len(positions) != 0:
        raise RuntimeError(
            f"FLAT run path but broker shows {len(positions)} position(s): "
            f"{[p.get('symbol') for p in positions]}. Zero-position snapshot is "
            f"invalid here; the facts->snapshot mapper (Brief A) is required."
        )
    return {"shares": 0, "put_open": False, "call_open": False,
            "pending_order_status": None}


def run(dry_run=True):
    run_id = fs.new_run_id()
    conn = fs.journal_connect()
    try:
        # 1. READ broker truth (raises on auth/network failure - let it)
        financials = fs.read_broker_financials()
        # 2. LOAD prior state if present, else fresh FLAT; this run only supports FLAT
        try:
            state = fs.load_state()
        except FileNotFoundError:
            state = fs.fresh_flat_state(
                equity=financials["equity"], cash=financials["cash"],
                options_buying_power=financials["options_buying_power"],
                positions=financials["positions"], last_outcome="dryrun_init")
        stage = state["wheel"]["stage"]
        if stage != fs.FLAT:
            # B-minimal only handles FLAT. Non-FLAT is not an error in the code -
            # it is out of scope for this brief. Journal + exit non-zero loud.
            fs.record_run(conn, run_id, f"out_of_scope_stage:{stage}")
            print(f"BRIEF 10 SCOPE: state stage is {stage}, this path only handles "
                  f"FLAT. Later briefs handle the rest.", file=sys.stderr)
            return 3
        # 3. Refresh the money fields on the FLAT state from this read
        state["equity"] = financials["equity"]
        state["cash"] = financials["cash"]
        state["options_buying_power"] = financials["options_buying_power"]
        state["positions"] = financials["positions"]
        # 4. BUILD guarded FLAT snapshot (raises if positions non-empty)
        snapshot = build_flat_snapshot(financials)
        # 5. RECONCILE (fires dump+alert+journal on HALT; journals decision always)
        new_state, action = fs.reconcile_and_handle(
            state, snapshot, conn=conn, run_id=run_id)
        # 6. Expected at FLAT/zero-pos: "noop:FLAT stable". Anything else is notable.
        # 7. WRITE state (atomic), journal the run outcome
        new_state["last_run"] = {"ts": fs._now_iso(),
                                 "outcome": f"dryrun:{action}"}
        fs.write_state(new_state)
        fs.record_run(conn, run_id, f"dryrun:{action}")
        fs.record_event(conn, run_id, "dryrun_complete", detail=action)
        print(f"RUN OK  stage={new_state['wheel']['stage']}  action={action}  "
              f"state->{fs.STATE_PATH}")
        return 0 if action.startswith("noop") else 4  # non-noop at FLAT = look at it
    finally:
        conn.close()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="forgetrader.run",
        description="Brief 10 B-minimal: dry-run FLAT run-to-completion path.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                   help="read account, reconcile FLAT-against-FLAT, write state "
                        "(default; the only mode this brief implements)")
    g.add_argument("--live", dest="live", action="store_true",
                   help="NOT implemented in Brief 10")
    args = p.parse_args(argv)
    if getattr(args, "live", False):
        p.error("live submission is not built (Brief 10 is dry-run only)")
    return run(dry_run=True)


if __name__ == "__main__":
    sys.exit(main())
