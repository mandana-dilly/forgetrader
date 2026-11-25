# Recovery drill, account B (funding criterion 5)

Brief wheel-prep-1125 T4 (milestone 14). Funding criterion 5: "One recovery drill: delete
state, run the runbook, `--dry-run` returns `noop:<STAGE>`" (docs/INSTRUCTIONS.md:275). Every
file:line is at base 4b04c86169e59efe29e1bb4a6bcca8a856c34617 (written at c20f652, re-verified). Simulated end to end, both
stages and the rollback, by `tests/test_recovery_drill.py` (fake broker, tempdir).

## Rules for this drill

- Run as **forge**, in `~/forgetrader-b` (`/home/forge/forgetrader-b`). Every block touches
  only B. Steps 4-6 read Alpaca through B's own `.env`: `credentials._ENV_PATH` is the `.env`
  beside the code that runs (credentials.py:7), so every python command starts with
  `cd ~/forgetrader-b`. Never run them from `~/forgetrader` (A's keys).
- **When:** AFTER the 1 Dec cycle has resolved (docs/prereg/B_CYCLE_2026-12-01.md) and B's
  wheel is FLAT or HOLDING. Never inside B's 10:45 or 13:15 ET timer windows
  (deploy/b/install_b.py:70-73), and not 16:30-16:40 ET (B digest at 16:35,
  deploy/b/install_b.py:74-77, reads state.json). A weekday after 14:00 ET or any weekend
  time is clear.
- One block per paste. Bracketed paste is on: read the block at the prompt, then Enter.
  Output goes to chat only, never back into the shell (docs/INSTRUCTIONS.md:92). Overwrites
  use `>|` (noclobber). Each block checks before it writes and chains the write with `&&`.
- **Do not paste step 5b's output back into a shell**: `--init` prints
  `wrote fresh FLAT state -> /home/forge/forgetrader-b/state.json` (forgetrader_state.py:843),
  the 2 Nov redirect shape (docs/INSTRUCTIONS.md:304; findings F7).
- Any output that differs from "Expect" is a deviation: stop and run the **Rollback** block.

## Step 0: preconditions (read-only)

```bash
cd ~/forgetrader-b && TZ=America/New_York date && jq -r '.wheel.stage' state.json && jq '{wheel, last_run}' state.json && systemctl --user --no-pager list-timers forgetrader-b-run.timer
```

Expect: ET time outside the windows above; stage `FLAT` or `HOLDING` (any other stage: do
not run the drill today); `last_run.outcome` a `live:` noop or transition (no `HALT`); the
timer listed with its next run.

## Step 1: stop B's timer

```bash
systemctl --user stop forgetrader-b-run.timer; systemctl --user is-active forgetrader-b-run.timer forgetrader-b-run.service; systemctl --user --no-pager list-timers forgetrader-b-run.timer
```

Expect: `inactive` twice (timer stopped, no run in progress); list-timers shows no
`forgetrader-b-run.timer` row (without `--all` it lists active timers only: systemd
behaviour, UNVERIFIED on this host). INSTRUCTIONS step 1 names
`forgetrader-dryrun.timer` (docs/INSTRUCTIONS.md:207); B's unit is `forgetrader-b-run.timer`
(deploy/b/install_b.py:10, docs/HALT_RUNBOOK.md:42-44) (findings F8).

## Step 2: timestamped copies of state.json and journal.db

```bash
cd ~/forgetrader-b && D=~/drill-b-$(date -u +%Y%m%dT%H%M%SZ) && mkdir "$D" && cp -p state.json journal.db "$D"/ && jq -r '.wheel.stage' "$D"/state.json >| "$D"/STAGE && sha256sum state.json journal.db && (cd "$D" && sha256sum state.json journal.db) && cat "$D"/STAGE && echo "drill dir $D"
```

Expect: two pairs of identical sha256 lines (live file vs copy), the stage (`FLAT` or
`HOLDING`), the drill dir path. Nothing writes journal.db while the timer is stopped, so the
copy is consistent. Every later block finds the copy with `ls -1d ~/drill-b-* | tail -1`.

## Step 3: delete state.json

```bash
cd ~/forgetrader-b && D=$(ls -1d ~/drill-b-* | tail -1) && cmp state.json "$D"/state.json && rm state.json; ls -l state.json; echo "ls exit=$?"
```

Expect: `ls: cannot access 'state.json': No such file or directory` and `ls exit=2`. (`cmp`
prints nothing when the copy matches; on a mismatch nothing is deleted.)

## Step 4: the dry-run must HALT:state_missing (exit 4)

```bash
cd ~/forgetrader-b && .venv/bin/python -m forgetrader.run --dry-run; echo exit=$?
```

Expect, exactly (forgetrader_state.py:36-37 puts state.json beside the module):

```text
ALERT: HALT state_missing | /home/forge/forgetrader-b/state.json absent; run --init deliberately
HALT state_missing: /home/forge/forgetrader-b/state.json absent; run --init deliberately
exit=4
```

- Line 1 is stdout from `fs.alert_stub` (run.py:251-252, forgetrader_state.py:605-607);
  line 2 is stderr (run.py:254-255); exit 4 (run.py:257).
- State is loaded first (run.py:247-249), so nothing else runs: no instance check, no
  credentials, no broker read, no state written (run.py:244-246). One Telegram message
  `[forgetrader-b] dryrun:HALT:state_missing stage=None ticker=None` (run.py:256, 149-161;
  alerts.py:58-59, 66).

```bash
cd ~/forgetrader-b && ls -l state.json; echo "ls exit=$?"; sqlite3 -readonly journal.db "SELECT ts, outcome FROM runs ORDER BY rowid DESC LIMIT 1;"
```

Expect: `ls exit=2` (no state created) and one row `TS|dryrun:HALT:state_missing`, TS in
host UTC, not the broker clock (pre-clock outcome, run.py:88, 250, 253).

## Step 5: rebuild by the runbook (docs/INSTRUCTIONS.md:205-213)

Runbook step 2 (find the destroyer) is known here: the drill deleted it. Step 3 (prefer a
backup) is deliberately skipped: the drill proves the hand rebuild. The copy in `$D` is the
rollback, and the reference the rebuild is checked against.

### 5a: broker truth first (runbook step 4, docs/INSTRUCTIONS.md:210, verbatim)

```bash
cd ~/forgetrader-b && .venv/bin/python -c "import forgetrader_state as fs, json; f=fs.read_broker_financials(); print(json.dumps({k:f[k] for k in ('positions','open_orders')}, indent=2, default=str))"; cat "$(ls -1d ~/drill-b-* | tail -1)"/STAGE
```

Expect (keys from forgetrader_state.py:779-798): `"open_orders": []` in both cases, and
- FLAT: `"positions": []`
- HOLDING: `"positions"` one entry with `"symbol": "BAC"`, `"qty": 100`.

Map it with runbook step 5 (docs/INSTRUCTIONS.md:211): nothing -> FLAT; 100 shares, no call ->
HOLDING. The mapped stage must equal the STAGE file printed last. Anything else: Rollback.

### 5b: --init (both stages)

```bash
cd ~/forgetrader-b && .venv/bin/python forgetrader_state.py --init; echo exit=$?
```

Expect, exactly (forgetrader_state.py:815-844):

```text
broker facts read OK (equity/cash/options_bp/positions populated)
wrote fresh FLAT state -> /home/forge/forgetrader-b/state.json
exit=0
```

`REFUSE: ... exists` with exit 3 means state.json is back (forgetrader_state.py:816-819):
stop. `WARN: broker read failed` (forgetrader_state.py:824-826) means a FLAT state with null
money was written: Rollback. `--init` journals one runs row `init` and one decision `init`
(forgetrader_state.py:837-841). The state is FLAT with the broker's money and positions
(forgetrader_state.py:828-835); for FLAT, the rebuild is done: go to step 6.

### 5c: HOLDING only, set the wheel (runbook step 6, jq to a temp file in the same dir)

The HOLDING wheel keeps only stage, ticker and cost_basis (forgetrader_state.py:210-214, the
allowlist every transition into HOLDING applies). cost_basis is the assigned put's strike
(forgetrader_state.py:197-204): the block takes it from the copy and refuses unless it equals
the strike of the last put B sold, from B's own journal.

```bash
cd ~/forgetrader-b && D=$(ls -1d ~/drill-b-* | tail -1) && [ "$(cat "$D"/STAGE)" = HOLDING ] && [ "$(jq -r '.wheel.stage' state.json)" = FLAT ] && B=$(jq '.wheel.cost_basis' "$D"/state.json) && T=$(jq -r '.wheel.ticker' "$D"/state.json) && P=$(sqlite3 -readonly journal.db "SELECT symbol FROM orders WHERE side = 'sell' AND substr(symbol, -9, 1) = 'P' ORDER BY rowid DESC LIMIT 1;") && echo "copy ticker=$T cost_basis=$B last put=$P" && [ "$T" = BAC ] && [ "$(printf '%.2f' "$B")" = "$(printf '%s' "$P" | awk '{printf "%.2f", substr($0, length($0) - 7) / 1000}')" ] && jq --arg t "$T" --argjson b "$B" '.wheel.stage = "HOLDING" | .wheel.ticker = $t | .wheel.cost_basis = $b' state.json >| state.json.drill.tmp && chmod --reference=state.json state.json.drill.tmp && mv state.json.drill.tmp state.json && jq '.wheel' state.json
```

Expect: the `copy ticker=BAC cost_basis=N last put=BAC...P...` line, then the wheel:
`"stage": "HOLDING"`, `"ticker": "BAC"`, `"cost_basis": N`, every other key (`intent`,
`contract_symbol`, `strike`, `expiry`, `opened_at`, `client_order_id`) `null`
(forgetrader_state.py:454-464). No wheel printed = a check failed and nothing was written:
Rollback. A whole-number basis may print as `48` or `48.0`; both pass the basis check
(forgetrader_state.py:156-164).

## Step 6: the dry-run must return noop:STAGE (exit 0)

```bash
cd ~/forgetrader-b && .venv/bin/python -m forgetrader.run --dry-run; echo exit=$?
```

Expect: exit 0 and, as the last stdout lines (run.py:399-400, 478-479, 522-523):

- FLAT: `SCAN propose_csp` or `SCAN all_rejected`, a detail line, then
  `RUN OK  stage=FLAT  action=noop:FLAT stable  state: /home/forge/forgetrader-b/state.json`
- HOLDING: `SCAN propose_cc`, `SCAN cc_rejected:REASON` or `SCAN cc_all_rejected`, a detail
  line, then
  `RUN OK  stage=HOLDING  action=noop:HOLDING stable  state: /home/forge/forgetrader-b/state.json`

A dry-run never submits (run.py:192-193, 403, 482). Any `HALT` = the rebuilt state is wrong
(runbook step 7, docs/INSTRUCTIONS.md:213): Rollback, never force past it.

```bash
cd ~/forgetrader-b && D=$(ls -1d ~/drill-b-* | tail -1) && jq -r '.last_run.outcome' state.json && diff <(jq -S '.wheel' "$D"/state.json) <(jq -S '.wheel' state.json) && echo WHEEL_MATCHES_COPY
```

Expect: `dryrun:noop:FLAT stable` or `dryrun:noop:HOLDING stable` (run.py:360, 517), then
`WHEEL_MATCHES_COPY` (the rebuild reproduced the wheel the engine had written). This is the
funding-5 pass line.

## Step 7: restore the timer

```bash
systemctl --user start forgetrader-b-run.timer && systemctl --user is-active forgetrader-b-run.timer && systemctl --user --no-pager list-timers forgetrader-b-run.timer
```

Expect: `active` and the next run at B's next weekday 10:45 or 13:15 ET
(deploy/b/install_b.py:70-73).

## Rollback (any deviation, at any step)

Leave the timer stopped. Put the copy back through a temp file in the same dir, prove it is
byte-identical, then confirm with a dry-run:

```bash
cd ~/forgetrader-b && D=$(ls -1d ~/drill-b-* | tail -1) && cp -p "$D"/state.json state.json.rollback.tmp && mv state.json.rollback.tmp state.json && cmp state.json "$D"/state.json && echo RESTORED && .venv/bin/python -m forgetrader.run --dry-run; echo exit=$?
```

Expect: `RESTORED`, then exit 0 with `RUN OK  stage=STAGE  action=noop:STAGE stable ...` for
the stage in `$D/STAGE`. Then Step 7. If this dry-run HALTs too, the broker moved during the
drill: keep the timer stopped and use docs/HALT_RUNBOOK.md for that class.

journal.db is NOT restored: it is append-only and never recreated (docs/INSTRUCTIONS.md:195);
the drill's rows (`dryrun:HALT:state_missing`, `init`, the dry-runs) stay as the drill's
evidence, and the copy in `$D` stays as a backup. The D19 morning review will list the
drill's `dryrun:HALT:state_missing` row; it was deliberately injected (funding criterion 2,
docs/INSTRUCTIONS.md:272).

## What the simulation proves (tests/test_recovery_drill.py)

For FLAT and HOLDING, in a tempdir named `forgetrader-b` with the FakeBroker: copies equal;
`--init` refuses over an existing state (exit 3, file untouched); after deletion the dry-run
exits 4 with the exact stderr line above, one `dryrun:HALT:state_missing` row, no state file,
no broker call, alert text `dryrun:HALT:state_missing stage=None ticker=None`; the real
`--init` exits 0 with the exact two stdout lines above, the broker's money and positions, and
`init` journal rows; the step 5c edit; the dry-run exits 0 with `dryrun:noop:STAGE stable`,
the RUN OK line, the stage's scan, one clock read and no submit; the rebuilt wheel equals the
copy. Rollback case: a rebuild left at FLAT while the broker holds 100 BAC HALTs with
`dryrun:HALT:snapshot unexpected position BAC qty 100` (exit 4); restoring the copy gives
`dryrun:noop:HOLDING stable`. Not simulated: systemctl, jq, sqlite3 and the shell checks
(they run only on Baxman's terminal).
