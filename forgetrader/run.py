#!/usr/bin/env python3
"""forgetrader run path - dry-run, run-to-completion, single pass, no loop.

Run sequence:
  1. LOAD state.json first. Missing -> HALT:state_missing (exit 4); run.py
     never creates state (only `forgetrader_state.py --init` does). Corrupt
     state is an uncaught loud failure.
  2. Build the Alpaca clients, load policy, init the throttle.
  3. READ broker truth via read_broker_financials; the wheel's tracked
     client_order_id is passed only at CSP_PENDING / CC_PENDING.
  4. build_snapshot(wheel, financials) -> the 4-key snapshot; SnapshotError
     -> HALT:snapshot (dump + alert + journal, exit 4).
  5. reconcile_and_handle at ANY stage (dumps + alerts + journals on HALT).
     HALT -> exit 4 with no money refresh.
  6. Non-HALT: refresh the money fields on the new state.
  7. Scan the watchlist ONLY on exactly "noop:FLAT stable". At HOLDING,
     journal hold:stage2_not_built and warn (Stage 2 is not built).
  8. Write state.json atomically, journal the run, exit 0.

Invoked as `python -m forgetrader.run` from ~/forgetrader, where cwd is on
sys.path so the flat sibling module imports below resolve. No order is ever
submitted.

Exit codes (morning-review contract):
  0  ok / noop / legal transition
  1  uncaught exception (auth / network / corrupt state)
  2  argparse error
  3  init / set-state refused
  4  HALT (state_missing | snapshot | reconcile)
"""
import argparse
import sys

import forgetrader_state as fs


def _proposal_line(row, is_open):
    """One deterministic line: the proposed CSP's identity + the numbers that
    matter, plus the market-open flag so morning-review can read it without
    run.py branching on the clock."""
    return (
        f"propose_csp {row['underlying_symbol']} "
        f"strike={row['strike']:.2f} right=P expiry={row['expiration_date']} "
        f"bid={row['bid']} oi={row['open_interest']} "
        f"annualized_yield_pct={row['annualized_yield_pct']} "
        f"collateral={row['collateral']:.2f} market_open={is_open}"
    )


def _all_rejected_line(approved_n, reject_tally, is_open):
    """One deterministic line for an empty scan: how many names were approved,
    the market-open flag, and the most-common reject tokens - enough for
    morning-review to tell a genuine all-rejected from a closed-market empty."""
    top = ", ".join(f"{tok}={cnt}" for tok, cnt in reject_tally.most_common(5))
    return (
        f"all_rejected approved={approved_n} market_open={is_open} "
        f"top_rejects=[{top}]"
    )


def _halt_exit(conn, run_id, state, action):
    """Common HALT tail: last_run only (NO money refresh), write, journal, 4."""
    state["last_run"] = {"ts": fs._now_iso(), "outcome": f"dryrun:{action}"}
    fs.write_state(state)
    fs.record_run(conn, run_id, f"dryrun:{action}")
    return 4


def run(dry_run=True):
    run_id = fs.new_run_id()
    conn = fs.journal_connect()
    try:
        # 1. LOAD state FIRST. Missing state is a HALT, never a creation path:
        #    write_state is deliberately NOT called here. Corrupt JSON is not
        #    caught - it stays a loud uncaught failure.
        try:
            state = fs.load_state()
        except FileNotFoundError:
            fs.alert_stub(f"HALT state_missing | {fs.STATE_PATH} absent; "
                          f"run --init deliberately")
            fs.record_run(conn, run_id, "HALT:state_missing")
            print(f"HALT state_missing: {fs.STATE_PATH} absent; run --init "
                  f"deliberately", file=sys.stderr)
            return 4
        stage = state["wheel"]["stage"]
        tracked = (state["wheel"].get("client_order_id")
                   if stage in (fs.CSP_PENDING, fs.CC_PENDING) else None)

        # 2. Build the SDK clients ONCE for this process. All imports are local
        #    so `import forgetrader.run` still needs no alpaca-py.
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        import screener
        import wheel_dryrun
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from credentials import load_credentials

        api_key, api_secret, paper = load_credentials()

        trading_client = TradingClient(api_key, api_secret, paper=paper)
        stock_client = StockHistoricalDataClient(api_key, api_secret)
        option_client = OptionHistoricalDataClient(api_key, api_secret)

        # Policy + throttle must be ready before any scan (scan_watchlist's
        # docstring requires the shared throttle pre-initialized).
        policy = screener.load_policy()
        screener.init_throttle(policy["runtime"]["rate_limit_per_min"])

        # Mirror wheel_dryrun.main(): DTE / earnings anchor to the US/Eastern
        # calendar date, not the host's local date.
        now_et = datetime.now(ZoneInfo("America/New_York"))
        run_date = now_et.date()

        # 3. READ broker truth (raises on auth/network failure - let it).
        financials = fs.read_broker_financials(
            trading_client=trading_client, tracked_client_order_id=tracked)

        # 4. BUILD the snapshot; broker/wheel mismatch fails closed.
        try:
            snapshot = fs.build_snapshot(state["wheel"], financials)
        except fs.SnapshotError as e:
            reason = f"snapshot {e}"
            path = fs.dump_halt(financials, state, reason)
            fs.alert_stub(f"HALT {reason} | dump {path}")
            fs.record_decision(conn, run_id, stage_from=stage, stage_to=stage,
                               action=f"HALT:{reason}", detail="")
            return _halt_exit(conn, run_id, state, f"HALT:{reason}")

        # 5. RECONCILE at any stage (dumps + alerts + journals on HALT itself).
        new_state, action = fs.reconcile_and_handle(
            state, snapshot, conn=conn, run_id=run_id)
        if action.startswith("HALT:"):
            return _halt_exit(conn, run_id, new_state, action)

        # 6. Non-HALT: refresh the money fields from this read.
        new_state["equity"] = financials["equity"]
        new_state["cash"] = financials["cash"]
        new_state["options_buying_power"] = financials["options_buying_power"]
        new_state["positions"] = financials["positions"]

        # 7. Scan only on exactly "noop:FLAT stable" (a transition INTO FLAT
        #    this run does not scan; the next run does).
        if action == "noop:FLAT stable":
            result = wheel_dryrun.scan_watchlist(
                trading_client, stock_client, option_client, policy,
                now_et, run_date)
            is_open = getattr(result.clock, "is_open", None)
            if result.passing_rows:
                proposal = result.passing_rows[0]  # already OI-desc sorted
                scan_action = "propose_csp"
                detail = _proposal_line(proposal, is_open)
            else:
                scan_action = "all_rejected"
                detail = _all_rejected_line(
                    len(result.approved), result.reject_tally, is_open)
            # Exactly one decisions row for the scan outcome. No order path.
            fs.record_decision(conn, run_id, stage_from=fs.FLAT,
                               stage_to=fs.FLAT, action=scan_action, detail=detail)
            # Compact stdout summary only - no gate trace, no scans/*.txt/*.csv
            # (that stays wheel_dryrun.main()'s job). run.py is a consumer.
            print(f"SCAN {scan_action}")
            print(f"  {detail}")
        elif new_state["wheel"]["stage"] == fs.HOLDING:
            fs.record_decision(conn, run_id, stage_from=fs.HOLDING,
                               stage_to=fs.HOLDING,
                               action="hold:stage2_not_built", detail=action)
            print("WARNING: HOLDING - Stage 2 covered-call scan not built; "
                  "wheel stalled", file=sys.stderr)

        # 8. WRITE state (atomic), journal the run outcome
        new_state["last_run"] = {"ts": fs._now_iso(),
                                 "outcome": f"dryrun:{action}"}
        fs.write_state(new_state)
        fs.record_run(conn, run_id, f"dryrun:{action}")
        fs.record_event(conn, run_id, "dryrun_complete", detail=action)
        print(f"RUN OK  stage={new_state['wheel']['stage']}  action={action}  "
              f"state->{fs.STATE_PATH}")
        return 0
    finally:
        conn.close()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="forgetrader.run",
        description="Dry-run, run-to-completion wheel path (no order submission).",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                   help="read account, reconcile, write state "
                        "(default; the only mode implemented)")
    g.add_argument("--live", dest="live", action="store_true",
                   help="NOT implemented")
    args = p.parse_args(argv)
    if getattr(args, "live", False):
        p.error("live submission is not built (dry-run only)")
    return run(dry_run=True)


if __name__ == "__main__":
    sys.exit(main())
