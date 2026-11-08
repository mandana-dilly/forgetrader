#!/usr/bin/env python3
"""forgetrader run path - run-to-completion, single pass, no loop.

Dry-run is the default and never submits. --live may submit ONE cash-secured
put through submit.place_csp, and only when every _submit_gate condition holds.

Run sequence:
  1. LOAD state.json first. Missing -> HALT:state_missing (exit 4); run.py
     never creates state (only `forgetrader_state.py --init` does). Corrupt
     state is an uncaught loud failure.
  2. Load credentials. With --live, refuse unless paper is True BEFORE any
     client is built or any broker read: live:refused:not_paper, run row
     journalled, no state write, exit 3.
  3. Build the Alpaca clients, load policy, init the throttle. Read the broker
     clock ONCE; every consumer (incl. the scan) receives that object.
     Missing clock or tz-naive timestamp -> HALT exit 4, no financials read.
  4. READ broker truth via read_broker_financials; the wheel's tracked
     client_order_id is passed only at CSP_PENDING / CC_PENDING.
  5. build_snapshot(wheel, financials) -> the 4-key snapshot; SnapshotError
     -> HALT:snapshot (dump + alert + journal, exit 4).
  6. reconcile_and_handle at ANY stage (dumps + alerts + journals on HALT).
     HALT -> exit 4 with no money refresh.
  7. Non-HALT: refresh the money fields on the new state.
  8. Scan the watchlist ONLY on exactly "noop:FLAT stable". At HOLDING,
     journal hold:stage2_not_built and warn (Stage 2 is not built).
  9. Submit ONLY when _submit_gate passes: --live, paper True, action exactly
     "noop:FLAT stable", a proposal with final_pass True, run clock is_open
     True. place_csp gets the money-refreshed state; the state it returns is
     the state persisted (submitted and HALT:submit alike).
 10. Write state.json atomically, journal the run, then send ONE Telegram
     summary (alerts.send: outcome, stage, ticker, top_rejects if any), exit.
     Every HALT sends its HALT outcome instead, after its dump and journal
     row; refused:not_paper sends its outcome after its run row. One message
     per run. An alert failure never changes the exit code, state or journal.

Every runs.outcome and last_run.outcome carries the mode prefix "dryrun:" or
"live:" (state_missing included). Live FLAT-stable outcomes:
  live:submit:submitted status=STATUS coid=COID     exit 0
  live:submit:refused:CHECK                         exit 0, state untouched
  live:HALT:submit DETAIL                           exit 4
  live:skip:market_closed                           exit 0
  live:skip:no_passing_proposal                     exit 0

Invoked as `python -m forgetrader.run` from ~/forgetrader, where cwd is on
sys.path so the flat sibling module imports below resolve.

Exit codes (morning-review contract):
  0  ok / noop / legal transition / submitted / refused proposal / skip
  1  uncaught exception (auth / network / corrupt state)
  2  argparse error
  3  --live refused: not paper
  4  HALT (state_missing | clock | snapshot | reconcile | watchlist_structure
     | submit)
"""
import argparse
import json
import sys

import alerts
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


def _render_wl_rejected(wl_rejected):
    """Render watchlist rejects as "[TICKER:reason, ...]". ONE renderer shared
    by the all_rejected decision/stdout line, the watchlist_rejected event and
    the stderr warning, so the three cannot drift."""
    return "[" + ", ".join(f"{t}:{r}" for t, r in wl_rejected) + "]"


def _all_rejected_line(approved_n, reject_tally, is_open, wl_rejected):
    """One deterministic line for an empty scan: how many names were approved,
    the market-open flag, the most-common reject tokens, and the watchlist
    entries dropped before the scan (wl_rejected) - enough for morning-review
    to tell a genuine all-rejected from a closed-market empty."""
    return (
        f"all_rejected approved={approved_n} market_open={is_open} "
        f"top_rejects=[{_top_rejects(reject_tally)}] "
        f"wl_rejected={_render_wl_rejected(wl_rejected)}"
    )


def _top_rejects(reject_tally):
    """The five most common reject tokens as "tok=n, ...". ONE renderer shared
    by the all_rejected line and the Telegram summary."""
    return ", ".join(f"{tok}={cnt}" for tok, cnt in reject_tally.most_common(5))


def _summary(outcome, state, top_rejects=None):
    """One-line Telegram text for a run: the journalled outcome, the wheel
    stage and ticker of the state persisted (None when there is no state),
    and top_rejects when the scan rejected everything."""
    wheel = state["wheel"] if state is not None else {}
    text = (f"{outcome} stage={wheel.get('stage')} "
            f"ticker={wheel.get('ticker')}")
    if top_rejects:
        text += f" top_rejects=[{top_rejects}]"
    return text


def _no_redirect(text):
    """Broker / exception text reaches stdout, the journal and git-visible
    logs: drop the characters that make a redirect-shaped token."""
    return str(text).replace("<", "_").replace(">", "_")


def _submit_gate(live, paper, action, proposal, is_open):
    """Pure. None when place_csp may be called, else the reason it may not.
    Every condition is an identity / equality test on exactly the value
    required; anything else submits nothing."""
    if live is not True:
        return "dry_run"
    if paper is not True:
        return "refused:not_paper"
    if action != "noop:FLAT stable":
        return "skip:not_flat_stable"
    if is_open is not True:
        return "skip:market_closed"
    if not isinstance(proposal, dict) or proposal.get("final_pass") is not True:
        return "skip:no_passing_proposal"
    return None


def _halt_exit(conn, run_id, state, action, mode):
    """Common HALT tail: sets last_run only (never refreshes money itself),
    writes the state it is given, journals the run row, sends the HALT
    outcome as the run's one alert, returns 4."""
    state["last_run"] = {"ts": fs._now_iso(), "outcome": f"{mode}:{action}"}
    fs.write_state(state)
    fs.record_run(conn, run_id, f"{mode}:{action}")
    alerts.send(_summary(f"{mode}:{action}", state))
    return 4


def run(live=False):
    """One run. live=False (default) never submits; live=True may submit one
    CSP via place_csp when _submit_gate passes. Returns the exit code."""
    mode = "live" if live is True else "dryrun"
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
            fs.record_run(conn, run_id, f"{mode}:HALT:state_missing")
            print(f"HALT state_missing: {fs.STATE_PATH} absent; run --init "
                  f"deliberately", file=sys.stderr)
            alerts.send(_summary(f"{mode}:HALT:state_missing", None))
            return 4
        stage = state["wheel"]["stage"]
        tracked = (state["wheel"].get("client_order_id")
                   if stage in (fs.CSP_PENDING, fs.CC_PENDING) else None)

        # 2-3. Credentials, then the SDK clients ONCE for this process. All
        #    imports are local so `import forgetrader.run` still needs no
        #    alpaca-py.
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        import screener
        import wheel_dryrun
        from watchlist import WatchlistStructureError
        from zoneinfo import ZoneInfo

        from credentials import load_credentials
        from forgetrader.submit import place_csp

        api_key, api_secret, paper = load_credentials()

        # --live is paper-only until Phase W: refuse BEFORE any client exists
        # or any broker read. No state write.
        if live is True and paper is not True:
            fs.record_decision(conn, run_id, stage_from=stage, stage_to=stage,
                               action="refused:not_paper", detail="")
            fs.record_run(conn, run_id, f"{mode}:refused:not_paper")
            print("REFUSED not_paper: --live requires a paper account",
                  file=sys.stderr)
            alerts.send(_summary(f"{mode}:refused:not_paper", state))
            return 3

        trading_client = TradingClient(api_key, api_secret, paper=paper)
        stock_client = StockHistoricalDataClient(api_key, api_secret)
        option_client = OptionHistoricalDataClient(api_key, api_secret)

        # Policy + throttle must be ready before any scan (scan_watchlist's
        # docstring requires the shared throttle pre-initialized).
        policy = screener.load_policy()
        screener.init_throttle(policy["runtime"]["rate_limit_per_min"])

        # One broker clock for the whole run (D13): DTE / earnings / expiry
        # anchor to the US/Eastern calendar date of the broker's timestamp,
        # never the host clock. This is the ONLY get_clock call; the scan and
        # the submit gate receive this object. A None clock HALTs here, so
        # no consumer below can be handed None.
        screener.throttle("get_clock")
        clock = trading_client.get_clock()
        ts = getattr(clock, "timestamp", None)
        if (clock is None or ts is None or ts.tzinfo is None
                or ts.utcoffset() is None):
            reason = "clock timestamp missing or tz-naive"
            fs.alert_stub(f"HALT {reason}")
            fs.record_decision(conn, run_id, stage_from=stage, stage_to=stage,
                               action=f"HALT:{reason}", detail="")
            return _halt_exit(conn, run_id, state, f"HALT:{reason}", mode)
        now_et = ts.astimezone(ZoneInfo("America/New_York"))
        run_date = now_et.date()

        # 4. READ broker truth (raises on auth/network failure - let it).
        financials = fs.read_broker_financials(
            trading_client=trading_client, tracked_client_order_id=tracked)

        # 5. BUILD the snapshot; broker/wheel mismatch fails closed.
        try:
            snapshot = fs.build_snapshot(state["wheel"], financials)
        except fs.SnapshotError as e:
            reason = f"snapshot {e}"
            path = fs.dump_halt(financials, state, reason)
            fs.alert_stub(f"HALT {reason} | dump {path}")
            fs.record_decision(conn, run_id, stage_from=stage, stage_to=stage,
                               action=f"HALT:{reason}", detail="")
            return _halt_exit(conn, run_id, state, f"HALT:{reason}", mode)

        # 6. RECONCILE at any stage (dumps + alerts + journals on HALT itself).
        new_state, action = fs.reconcile_and_handle(
            state, snapshot, conn=conn, run_id=run_id,
            as_of_date=run_date)
        if action.startswith("HALT:"):
            return _halt_exit(conn, run_id, new_state, action, mode)

        # 7. Non-HALT: refresh the money fields from this read. Runs before
        #    the submit branch, so place_csp receives the refreshed state.
        new_state["equity"] = financials["equity"]
        new_state["cash"] = financials["cash"]
        new_state["options_buying_power"] = financials["options_buying_power"]
        new_state["positions"] = financials["positions"]

        # 8. Scan only on exactly "noop:FLAT stable" (a transition INTO FLAT
        #    this run does not scan; the next run does).
        outcome = f"{mode}:{action}"
        top_rejects = None
        if action == "noop:FLAT stable":
            try:
                result = wheel_dryrun.scan_watchlist(
                    trading_client, stock_client, option_client, policy,
                    now_et, run_date, clock=clock)
            except WatchlistStructureError as e:
                reason = f"watchlist_structure {e}"
                fs.alert_stub(f"HALT {reason}")
                fs.record_decision(conn, run_id, stage_from=fs.FLAT,
                                   stage_to=fs.FLAT, action=f"HALT:{reason}",
                                   detail="")
                return _halt_exit(conn, run_id, new_state, f"HALT:{reason}",
                                  mode)
            is_open = getattr(clock, "is_open", None)
            proposal = None
            if result.passing_rows:
                proposal = result.passing_rows[0]  # already OI-desc sorted
                scan_action = "propose_csp"
                detail = _proposal_line(proposal, is_open)
            else:
                scan_action = "all_rejected"
                top_rejects = _top_rejects(result.reject_tally)
                detail = _all_rejected_line(
                    len(result.approved), result.reject_tally, is_open,
                    result.wl_rejected)
            # Exactly one decisions row for the scan outcome.
            fs.record_decision(conn, run_id, stage_from=fs.FLAT,
                               stage_to=fs.FLAT, action=scan_action, detail=detail)
            if result.wl_rejected:
                rendered = _render_wl_rejected(result.wl_rejected)
                fs.record_event(conn, run_id, "watchlist_rejected",
                                detail=rendered)
                print(f"WARNING: watchlist_rejected {rendered}",
                      file=sys.stderr)
            # Compact stdout summary only - no gate trace, no scans/*.txt/*.csv
            # (that stays wheel_dryrun.main()'s job). run.py is a consumer.
            print(f"SCAN {scan_action}")
            print(f"  {detail}")

            # 9. Submit gate. Dry-run (the default) stops here every time.
            gate = _submit_gate(live, paper, action, proposal, is_open)
            if gate is None:
                state_out, res = place_csp(
                    trading_client, new_state, proposal, conn, run_id,
                    ts.isoformat(), fs.STATE_PATH)
                new_state = state_out  # persisted on every outcome
                coid = res["client_order_id"]
                if res["outcome"] == "submitted":
                    token = (f"submit:submitted status={res['status']} "
                             f"coid={coid}")
                elif res["outcome"] == "refused":
                    token = f"submit:refused:{_no_redirect(res['detail'])}"
                else:
                    token = f"HALT:submit {_no_redirect(res['detail'])}"
                fs.record_decision(
                    conn, run_id, stage_from=fs.FLAT,
                    stage_to=new_state["wheel"]["stage"], action=token,
                    detail=_no_redirect(json.dumps(res, sort_keys=True)))
                if res["outcome"] == "halt":
                    print(f"HALT submit {_no_redirect(res['detail'])} "
                          f"coid={coid}", file=sys.stderr)
                    return _halt_exit(conn, run_id, new_state, token, mode)
                print(f"LIVE {token} broker_order_id={res['broker_order_id']}")
                outcome = f"{mode}:{token}"
            elif gate != "dry_run":
                fs.record_decision(conn, run_id, stage_from=fs.FLAT,
                                   stage_to=fs.FLAT, action=gate, detail="")
                print(f"LIVE {gate}")
                outcome = f"{mode}:{gate}"
        elif new_state["wheel"]["stage"] == fs.HOLDING:
            fs.record_decision(conn, run_id, stage_from=fs.HOLDING,
                               stage_to=fs.HOLDING,
                               action="hold:stage2_not_built", detail=action)
            print("WARNING: HOLDING - Stage 2 covered-call scan not built; "
                  "wheel stalled", file=sys.stderr)

        # 10. WRITE state (atomic), journal the run outcome, then alert.
        new_state["last_run"] = {"ts": fs._now_iso(), "outcome": outcome}
        summary = _summary(outcome, new_state, top_rejects)
        fs.write_state(new_state)
        fs.record_run(conn, run_id, outcome)
        fs.record_event(conn, run_id, f"{mode}_complete", detail=action)
        print(f"RUN OK  stage={new_state['wheel']['stage']}  action={action}  "
              f"state: {fs.STATE_PATH}")
        alerts.send(summary)
        return 0
    finally:
        conn.close()


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="forgetrader.run",
        description="Run-to-completion wheel path. Dry-run by default; "
                    "--live may submit one paper CSP.",
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", dest="live", action="store_false",
                   help="read account, reconcile, scan, write state; never "
                        "submits (default)")
    g.add_argument("--live", dest="live", action="store_true",
                   help="as --dry-run, then submit one CSP via place_csp only "
                        "if paper is True, action is noop:FLAT stable, a "
                        "proposal has final_pass True and the broker clock "
                        "is_open is True; refuses (exit 3) unless paper")
    p.set_defaults(live=False)
    args = p.parse_args(argv)
    return run(live=args.live)


if __name__ == "__main__":
    sys.exit(main())
