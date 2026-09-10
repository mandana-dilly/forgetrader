#!/usr/bin/env python3
"""ftnight launcher - Brief 11b market-hours proof.
Polls Alpaca get_clock() (never local time), waits for the regular session,
runs `forgetrader.run --dry-run` exactly once, and captures run stdout/stderr,
exit code, fresh journal decisions rows, and state stage into a timestamped
scans/ file for morning review. Read-only clock polling; no orders; no --live.
"""
import os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
PY = str(REPO / ".venv" / "bin" / "python")
OUT = REPO / "scans" / f"ftnight_11b_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

def log(m):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {m}", flush=True)

def get_clock():
    from alpaca.trading.client import TradingClient
    from credentials import load_credentials
    k, s, paper = load_credentials()
    return TradingClient(k, s, paper=paper).get_clock()

def wait_for_open(tight=30, cap=1800):
    while True:
        clk = get_clock()
        if clk.is_open:
            log(f"market OPEN (next_close={clk.next_close})"); return
        now = datetime.now(timezone.utc)
        secs = (clk.next_open - now).total_seconds() if clk.next_open else cap
        nap = max(tight, min(secs - 60, cap))
        log(f"closed; next_open={clk.next_open}; sleeping {int(nap)}s")
        time.sleep(nap)

def main():
    OUT.parent.mkdir(exist_ok=True)
    log(f"ftnight_11b start; out={OUT}")
    try:
        wait_for_open()
    except Exception as e:
        OUT.write_text(f"FTNIGHT ABORT during clock wait: {type(e).__name__}: {e}\n")
        log(f"ABORT: {type(e).__name__}: {e}"); sys.exit(1)
    log("running forgetrader.run --dry-run once")
    proc = subprocess.run([PY, "-m", "forgetrader.run", "--dry-run"],
                          cwd=str(REPO), capture_output=True, text=True)
    dec = subprocess.run(["sqlite3", str(REPO / "journal.db"),
        "SELECT run_id,stage_from,stage_to,action,detail FROM decisions ORDER BY rowid DESC LIMIT 4;"],
        capture_output=True, text=True)
    st = subprocess.run(["jq", ".wheel.stage", str(REPO / "state.json")],
                        capture_output=True, text=True)
    with OUT.open("w") as f:
        f.write("===== FTNIGHT 11b MARKET-HOURS RUN =====\n")
        f.write(f"run_exit: {proc.returncode}\n")
        f.write("===== RUN STDOUT =====\n"); f.write(proc.stdout)
        f.write("===== RUN STDERR =====\n"); f.write(proc.stderr)
        f.write("===== DECISIONS_TAIL =====\n"); f.write(dec.stdout)
        if dec.stderr: f.write("sqlite stderr: " + dec.stderr)
        f.write("===== STATE_STAGE =====\n"); f.write(st.stdout)
        if st.stderr: f.write("jq stderr: " + st.stderr)
        f.write("===== FTNIGHT 11b DONE =====\n")
    log(f"complete exit={proc.returncode}; artifact {OUT}")

if __name__ == "__main__":
    main()
