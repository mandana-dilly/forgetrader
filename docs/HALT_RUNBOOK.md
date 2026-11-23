# HALT runbook - DRAFT for Architect and Baxman review

Brief halt-runbook, 23 Nov 2026, drafted from main ba8299a (critical path item 10, D19).
DRAFT: nothing here is a banked decision. Every "Decision (Baxman)" line names a choice this
doc does not make. Every file:line below is ba8299a; re-read the code if HEAD has moved.

## How to read this

**Class.** A HALT class is the token after `HALT:` up to the first space in the text the
engine writes to `runs.outcome`. `tests/test_halt_runbook.py` extracts the classes from the
source (ast, never a typed list) and fails if a class has no section here, has two, or if a
section names a class the code cannot produce. The token rule is literal, so some headings
are awkward (`HALT:no`, `HALT:unknown`, two `broker_snapshot` classes); renaming them is a
code change, not a doc change.

**Outcome shape.** Every HALT row is `MODE:HALT:CLASS DETAIL`, MODE = `dryrun` or `live`
(run.py:240, 209, 211). The timers run `--live` (deploy/forgetrader-run.service ExecStart), so
timer HALTs read `live:HALT:...`; Baxman's `--dry-run` HALTs read `dryrun:HALT:...`.

**Common HALT tail** (`_halt_exit`, run.py:205-213), used by every class except
`state_missing` and `instance`: sets `last_run = {ts, outcome}` on the state it is handed,
writes state.json (atomic, fs:477-493), writes ONE `runs` row, sends ONE Telegram summary
`OUTCOME stage=S ticker=T` with the instance prefix (alerts.py:54-66), returns exit 4. It
never refreshes money fields. Separately `fs.alert_stub` prints `ALERT: HALT ...` to stdout
(fs:605-607), i.e. the unit's journalctl log.

**Dump.** Only the snapshot class and the reconcile classes (everything raised inside
`reconcile`, fs:218-372) write `scans/state_halt_YYYYMMDD_HHMMSSZ.txt` (fs:610-625): reason,
the state as loaded, and the broker data that triggered it. The stamp is the run instant
(the broker clock, UTC). Path: `~/forgetrader/scans/` (A), `~/forgetrader-b/scans/` (B).

**Crash alert.** Exit 4 is a non-zero exit from a `Type=oneshot` unit with
`OnFailure=forgetrader-crash-alert@%n.service` and no `SuccessExitStatus`, so a timer HALT
should also send `[forgetrader] CRASH unit=... exit=4` (deploy/crash_alert.py). (Baxman to
confirm from a live HALT: two Telegram messages per timer HALT.)

**Accounts.** Commands are run as forge. A = `~/forgetrader`, timer
`forgetrader-run.timer` (10:30, 13:00 ET). B = `~/forgetrader-b`, timer
`forgetrader-b-run.timer` (10:45, 13:15 ET, deploy/b/install_b.py:10).

**Diagnosis rule.** Only read-only commands appear under Diagnosis: `sqlite3 -readonly`
SELECTs, `jq` on state.json / watchlist.json, `cat` / `ls` of the dump. `instr()` is used
rather than `LIKE` because `_` is a LIKE wildcard and LIKE is case-insensitive. Broker truth
beyond what the dump shows is Baxman's read (INSTRUCTIONS "State recovery runbook" step 4, or
the Alpaca paper dashboard); it touches Alpaca and is not reproduced here.

**Recovery rule.** Before any state edit stop that account's timer
(`systemctl --user stop forgetrader-run.timer` or `forgetrader-b-run.timer`). State edits
follow the INSTRUCTIONS "State recovery runbook" step 6 procedure (jq to a temp file in the
same directory, `chmod --reference`, `mv`). `--set-state` does not exist in the code
(forgetrader_state.py:841-850 has only `--show` / `--init`). Broker-side actions (cancel an
order, close a position) are Baxman's, made outside the engine.

**Confirmation rule.** Baxman runs a dry-run from the account directory, outside a timer
minute, then restarts the timer:
`cd ~/forgetrader && .venv/bin/python -m forgetrader.run --dry-run; echo exit=$?` (A),
`cd ~/forgetrader-b && .venv/bin/python -m forgetrader.run --dry-run; echo exit=$?` (B).
A dry-run is a full run that never submits (run.py:403, 482): it reads the broker, writes
state.json and journals. Expect exit 0 and a `dryrun:noop:...` or legal-transition outcome.

## HALT:state_missing

- **Outcome:** `MODE:HALT:state_missing` (run.py:253). No detail.
- **Trigger:** `fs.load_state()` raises FileNotFoundError: state.json absent (run.py:247-249,
  fs:471-474). Corrupt or empty JSON is NOT this class: it is an uncaught exit 1 with no row.
- **Engine did:** NOT the common tail. No state written, no last_run, no decision row, no
  credentials or broker read, no dump. One `runs` row stamped with host UTC (pre-clock,
  run.py:250, 88). `ALERT: HALT state_missing | PATH absent; run --init deliberately` on
  stdout, the same on stderr, Telegram `MODE:HALT:state_missing stage=None ticker=None`.
  Exit 4.
- **Diagnosis:**
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs ORDER BY rowid DESC LIMIT 10;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT rowid, ts, outcome FROM runs ORDER BY rowid DESC LIMIT 10;"`
    (the last non-HALT row is the last stage the engine knew).
  - A: `ls -l ~/forgetrader/state.json ~/forgetrader/state.json.tmp`
  - B: `ls -l ~/forgetrader-b/state.json ~/forgetrader-b/state.json.tmp`
    (a leftover `state.json.tmp` means a write died between fs:482 and fs:487).
  - Last stage journalled, A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions ORDER BY rowid DESC LIMIT 5;"`
  - Last stage journalled, B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions ORDER BY rowid DESC LIMIT 5;"`
- **Recovery:** INSTRUCTIONS "State recovery runbook" in full (find the destroyer first).
  Decision (Baxman): restore from backup, or `--init` plus a hand-written wheel matched to
  broker truth.
- **Confirmed by:** Baxman `--dry-run`: exit 0, `dryrun:noop:STAGE ...` for the stage broker
  truth shows. Any HALT means the restored state is wrong: fix the state, never force past it.
- Seen live: (Baxman fills)

## HALT:instance

- **Outcome:** `MODE:HALT:instance FORGE_INSTANCE is not set` or
  `MODE:HALT:instance FORGE_INSTANCE='X' is not one of a, b` (run.py:225,
  forge_instance.py:57, 60; `<` and `>` replaced by `_`, run.py:180-183).
- **Trigger:** `forge_instance.current()` raises InstanceError: after loading the account's
  .env, FORGE_INSTANCE is missing or not exactly `a` or `b` (forge_instance.py:52-62). An
  already-set environment variable wins over .env.
- **Engine did:** NOT the common tail, same effect plus last_run: state as loaded with only
  `last_run` changed is written, one `runs` row (host UTC), no decision row, no credentials,
  no broker read, no dump (run.py:216-234). stdout `ALERT: HALT:instance ...`, stderr line,
  Telegram with prefix `[forgetrader-?] ` (forge_instance.py:29). Exit 4.
- **Diagnosis:**
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:instance') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:instance') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - A: `jq '{wheel, last_run}' ~/forgetrader/state.json`
  - B: `jq '{wheel, last_run}' ~/forgetrader-b/state.json`
  - The FORGE_INSTANCE line only, no other .env line printed. A: `grep -n '^FORGE_INSTANCE' ~/forgetrader/.env`
    B: `grep -n '^FORGE_INSTANCE' ~/forgetrader-b/.env` (B must read exactly `FORGE_INSTANCE=b`,
    deploy/b/install_b.py:26-27, 54).
- **Recovery:** Decision (Baxman): which instance this checkout is, then set exactly one
  `FORGE_INSTANCE=a` (A) / `FORGE_INSTANCE=b` (B) line in that account's .env and check no
  unit sets the variable differently. The wheel is untouched; no state edit.
- **Confirmed by:** Baxman `--dry-run`: exit 0, no `HALT:instance`, and the Telegram summary
  arrives with the account's own prefix (`[forgetrader] ` / `[forgetrader-b] `).
- Seen live: (Baxman fills)

## HALT:clock

- **Outcome:** `MODE:HALT:clock timestamp missing or tz-naive` (run.py:316, 320).
- **Trigger:** `trading_client.get_clock()` returned None, or a clock whose `timestamp` is None
  or tz-naive (run.py:311-314). A get_clock exception is NOT this class: uncaught, exit 1, no
  row (run.py:84-88).
- **Engine did:** common tail on the state as loaded; one decision row
  `stage_from = stage_to = STAGE`, action `HALT:clock ...` (run.py:318-319); stdout
  `ALERT: HALT clock ...`; host-UTC stamps (pre-clock, run.py:315). No financials read, no
  dump. Exit 4.
- **Diagnosis:**
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:clock') > 0 ORDER BY rowid DESC LIMIT 10;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:clock') > 0 ORDER BY rowid DESC LIMIT 10;"`
    (one row = transient; every run since a date = the SDK or the broker changed).
  - A: `jq '{wheel, last_run}' ~/forgetrader/state.json`
  - B: `jq '{wheel, last_run}' ~/forgetrader-b/state.json`
- **Recovery:** nothing in state to fix. Decision (Baxman): wait for the next timer run, or,
  if it repeats, freeze the timer and open a brief (alpaca-py pin vs installed, broker clock
  payload). An undocumented clock shape stays a HALT; it is never coerced.
- **Confirmed by:** next timer run outcome is not `HALT:clock`; Baxman `--dry-run` reads the
  clock the same way and is the immediate check.
- Seen live: (Baxman fills)

## HALT:snapshot

- **Outcome:** `MODE:HALT:snapshot MESSAGE` (run.py:337, 342). MESSAGE is one of
  (fs:659-712): `financials missing positions or open_orders`;
  `pending stage STAGE without client_order_id`;
  `pending stage STAGE tracked_order_status missing`; `position SYM qty Q is not an int`;
  `tracked contract SYM qty Q, expected -1`; `unexpected position SYM qty Q`;
  `malformed contract symbol 'C'` (optionally `: right 'X'`);
  `unexpected open order SYM COID STATUS`.
- **Trigger:** `fs.build_snapshot(wheel, financials)` raised SnapshotError: the broker shows
  something the wheel does not track. Note `_safe_int` truncates fractional qty
  (fs:421-425), so `is not an int` means an unparseable qty (None).
- **Engine did:** dump with the FULL financials (positions, open_orders, tracked status)
  (run.py:338); stdout `ALERT: HALT snapshot ... | dump PATH`; one decision row
  `HALT:snapshot ...`; common tail on the state as loaded (no money refresh). Exit 4.
- **Diagnosis:**
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:snapshot') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:snapshot') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - A: `ls -1t ~/forgetrader/scans/state_halt_*.txt | head -3` then `cat` the newest; its
    `reason :` line must equal the runs row after `HALT:`.
  - B: `ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -3` then `cat` the newest.
  - A: `jq '.wheel' ~/forgetrader/state.json`
  - B: `jq '.wheel' ~/forgetrader-b/state.json`
  - Orders this engine placed, A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, symbol, side, qty, limit_price, status, client_order_id FROM orders ORDER BY rowid DESC LIMIT 10;"`
  - Orders this engine placed, B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, symbol, side, qty, limit_price, status, client_order_id FROM orders ORDER BY rowid DESC LIMIT 10;"`
    (a dump position or open order whose coid is not here was not placed by this account's
    engine: manual trade, probe on another account, or the wrong account's keys).
- **Recovery** (Decision (Baxman), per message):
  - `unexpected position` / `unexpected open order`: close or cancel it at the broker, or
    decide the wheel should be tracking it and edit state to the matching stage
    (INSTRUCTIONS recovery step 5).
  - `tracked contract ... expected -1`: the 1-contract cap is broken at the broker. Stop both
    timers, Architect informed before any action.
  - `pending stage ... without client_order_id` / `tracked_order_status missing`: the
    state was hand-edited without a coid; set it from broker open orders, or set the stage
    to what the broker shows.
  - `malformed contract symbol`: wheel.contract_symbol in state is wrong; correct it from the
    broker position.
  - `financials missing ...`: code defect, open a brief.
- **Confirmed by:** Baxman `--dry-run`: exit 0, `dryrun:noop:STAGE ...` or a legal
  transition, and no new `state_halt_*` dump.
- Seen live: (Baxman fills)

## HALT:watchlist_structure

- **Outcome:** `MODE:HALT:watchlist_structure MESSAGE` (run.py:369, 374 at FLAT; 443, 448 at
  HOLDING). MESSAGE (watchlist.py:98, 135, 140, 147): `entry N is not an object`,
  `watchlist file not found: PATH`, `invalid JSON in PATH: ...`,
  `top-level shape invalid in PATH: expected {version: 2, entries: [...]}`.
- **Trigger:** the scan's `watchlist.load_watchlist` raised WatchlistStructureError. Only
  reached on `noop:FLAT stable` (wheel_dryrun.scan_watchlist, run.py:363-367) or
  `noop:HOLDING stable` (cc_scan.scan_calls, run.py:435-441). Watchlist path:
  ACCOUNT/watchlist.json (wheel_dryrun.py:75, cc_scan.py:45). A single bad entry is a named
  rejection, not this class.
- **Engine did:** reconcile already passed and money fields were already refreshed
  (run.py:353-356), so the state written is the refreshed state plus last_run. The run has
  TWO decision rows: the reconcile `noop:... stable` and `HALT:watchlist_structure ...`.
  stdout `ALERT: HALT watchlist_structure ...`. No scan result, no submit, no dump. Exit 4.
- **Diagnosis:**
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:watchlist_structure') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:watchlist_structure') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - A: `jq '.version, (.entries | length), [.entries[] | type]' ~/forgetrader/watchlist.json`
  - B: `jq '.version, (.entries | length), [.entries[] | type]' ~/forgetrader-b/watchlist.json`
    (jq itself erroring = invalid JSON; version must be 2; every type must be "object").
- **Recovery:** Decision (Baxman): restore watchlist.json from git (`git show` of the last
  good commit, written by Baxman) or fix it by hand. Stamps are human decisions; Claude Code
  never writes watchlist.json.
- **Confirmed by:** Baxman `--dry-run`: exit 0 with `SCAN propose_csp` / `SCAN all_rejected`
  (FLAT) or `SCAN propose_cc` / `cc_rejected:...` / `cc_all_rejected` (HOLDING) printed.
- Seen live: (Baxman fills)

## HALT:submit

- **Outcome:** `live:HALT:submit EXCTYPE: MESSAGE` (run.py:416, 499; detail from
  submit.py:137, 267). Only ever `live:`: place_csp / place_cc are reached only when
  `_submit_gate` passes, which needs `--live` (run.py:192-193).
- **Trigger:** any exception after the order was built (submit.py:104-138 CSP, 234-268 CC):
  write-ahead `write_state` or intent `record_order` failed (`broker_reached=False`), or
  `submit_order`, reading its status/id, or the result `record_order` failed
  (`broker_reached=True`: an order may exist).
- **Engine did:** state.json is the write-ahead state, CSP_PENDING (from FLAT) or CC_PENDING
  (from HOLDING) with a new `client_order_id`, persisted again with last_run by the common
  tail (run.py:408, 491, 424, 507). Journal: `orders` intent row (status NULL) if it got that
  far; `events` row `submit_exception` with `broker_reached=... TYPE: MSG`; decision row
  `HALT:submit ...` whose detail is the result JSON (coid, broker_order_id, broker_reached).
  stdout `ALERT: HALT:submit broker_reached=...`, stderr `HALT submit ... coid=...`, Telegram.
  No retry, no rollback, no dump. Exit 4.
- **Diagnosis:**
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT d.ts, d.action, d.detail FROM decisions d WHERE instr(d.action, 'HALT:submit') > 0 ORDER BY d.rowid DESC LIMIT 3;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT d.ts, d.action, d.detail FROM decisions d WHERE instr(d.action, 'HALT:submit') > 0 ORDER BY d.rowid DESC LIMIT 3;"`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, event_type, symbol, detail FROM events WHERE event_type = 'submit_exception' ORDER BY rowid DESC LIMIT 3;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, event_type, symbol, detail FROM events WHERE event_type = 'submit_exception' ORDER BY rowid DESC LIMIT 3;"`
  - A: `jq '.wheel | {stage, ticker, contract_symbol, client_order_id, opened_at}' ~/forgetrader/state.json`
  - B: `jq '.wheel | {stage, ticker, contract_symbol, client_order_id, opened_at}' ~/forgetrader-b/state.json`
  - Rows for that coid, A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, symbol, qty, limit_price, status, broker_order_id FROM orders WHERE client_order_id = '$(jq -r .wheel.client_order_id ~/forgetrader/state.json)';"`
  - Rows for that coid, B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, symbol, qty, limit_price, status, broker_order_id FROM orders WHERE client_order_id = '$(jq -r .wheel.client_order_id ~/forgetrader-b/state.json)';"`
- **Recovery:** normally none: D19 and D15 say the next run resolves it from the tracked
  status. The next run reads the coid's status at CSP_PENDING / CC_PENDING: order live ->
  `noop:..._PENDING awaiting fill`; filled -> `..._PENDING->..._OPEN (fill)`; canceled or
  expired -> back to FLAT / HOLDING; never reached the broker -> `HALT:CSP_PENDING` /
  `HALT:CC_PENDING tracked order not_found at broker` (see those sections). Decision
  (Baxman): let the next timer run resolve it, or dry-run now to see which. If
  `broker_reached=False` with a disk error, check disk before the next run.
- **Confirmed by:** the next run's outcome is one of the four above. Baxman `--dry-run` is
  the safe check (it cannot submit). The D19 morning review must still read this row: it
  can self-heal and leave the alert and journal as the only trace.
- Seen live: (Baxman fills)

## HALT:holding_basis_invalid

- **Outcome:** `MODE:HALT:holding_basis_invalid cost_basis=VALUE` (fs:163).
- **Trigger:** `wheel.cost_basis` is None, non-numeric, bool, NaN/inf or `<= 0`
  (fs:156-164), checked at HOLDING stable (fs:314-318) and after every transition INTO
  HOLDING (fs:167-176): `assigned` at CSP_OPEN (fs:302; basis is the put strike set by
  apply_trigger, fs:197-204), `cancel_or_expire` at CC_PENDING (fs:347),
  `cc_expired_worthless` at CC_OPEN (fs:366).
- **Engine did:** reconcile path: the input state is returned UNCHANGED, so the stage does
  NOT advance (e.g. stays CSP_OPEN while the broker holds 100 shares). Dump with the 4-key
  snapshot; stdout `ALERT: HALT holding_basis_invalid ... | dump PATH`; decision row with
  the snapshot JSON as detail (fs:628-644); common tail. Exit 4.
- **Diagnosis:**
  - A: `jq '.wheel | {stage, ticker, cost_basis, contract_symbol}' ~/forgetrader/state.json`
  - B: `jq '.wheel | {stage, ticker, cost_basis, contract_symbol}' ~/forgetrader-b/state.json`
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
  - The put that was sold (its strike is the basis), A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, symbol, limit_price, status FROM orders WHERE side = 'sell' ORDER BY rowid DESC LIMIT 5;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, symbol, limit_price, status FROM orders WHERE side = 'sell' ORDER BY rowid DESC LIMIT 5;"`
- **Recovery:** Decision (Baxman, Architect if the basis is not the put strike): the correct
  cost_basis, then a state edit of `wheel.cost_basis` (and the stage, if the broker has
  already moved, per recovery step 5).
- **Confirmed by:** Baxman `--dry-run`: exit 0 with `noop:HOLDING stable` or the transition
  into HOLDING that was blocked.
- Seen live: (Baxman fills)

## HALT:no

- **Outcome:** `MODE:HALT:no legal transition from STAGE on trigger 'T'` (fs:194).
- **Trigger:** `apply_trigger` given a (stage, trigger) pair not in LEGAL_TRANSITIONS
  (fs:57-68, 191-194).
- **Engine did:** not reachable as a runs.outcome in ba8299a. `reconcile` calls apply_trigger
  only with pairs legal for the stage it has just matched (fs:280, 289, 302, 308, 338, 347,
  360, 366). place_csp / place_cc validate the stage first (submit.py:43, 155) and turn any
  unexpected apply_trigger result into `submit:refused:apply_trigger_unexpected: ...`, exit 0
  (submit.py:80-83, 211-214). Had it been raised in reconcile it would get the reconcile
  treatment (dump, decision, common tail, exit 4).
- **Diagnosis:**
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:no ') > 0 OR instr(outcome, 'apply_trigger_unexpected') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:no ') > 0 OR instr(outcome, 'apply_trigger_unexpected') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - A: `jq '.wheel.stage' ~/forgetrader/state.json`  B: `jq '.wheel.stage' ~/forgetrader-b/state.json`
- **Recovery:** if ever seen, a code invariant broke. Decision (Baxman): stop that account's
  timer and open a brief; no state edit until the code path is understood.
- **Confirmed by:** the fixing brief's suites, then Baxman `--dry-run` exit 0.
- Seen live: (Baxman fills)

## HALT:broker_snapshot

- **Outcome:** `MODE:HALT:broker_snapshot missing required key 'shares'` (fs:250).
- **Trigger:** the snapshot handed to reconcile has no `shares` key (fs:249).
- **Engine did:** not reachable from run.py in ba8299a: build_snapshot always returns
  `shares` (fs:718-723) and a SnapshotError stops the run first. If raised: reconcile
  treatment (dump, decision row, common tail, exit 4).
- **Diagnosis:**
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:broker_snapshot missing') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:broker_snapshot missing') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
- **Recovery:** if ever seen, a code defect. Decision (Baxman): stop the timer, open a brief.
- **Confirmed by:** the fixing brief's suites, then Baxman `--dry-run` exit 0.
- Seen live: (Baxman fills)

## HALT:broker_snapshot['shares']

- **Outcome:** `MODE:HALT:broker_snapshot['shares'] not a non-negative int: VALUE` (fs:253).
- **Trigger:** `shares` is not a non-negative int (fs:252). From run.py the only reachable
  case is a NEGATIVE qty: build_snapshot sets shares from the wheel ticker's position qty
  (fs:679-680) after checking it is an int (fs:677) but not its sign. Negative = the account
  is SHORT the wheel ticker's stock.
- **Engine did:** reconcile treatment: state unchanged, dump, stdout
  `ALERT: HALT broker_snapshot['shares'] ... | dump PATH`, decision row, common tail. Exit 4.
- **Diagnosis:**
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:broker_snapshot[''shares'']') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'HALT:broker_snapshot[''shares'']') > 0 ORDER BY rowid DESC LIMIT 5;"`
  - A: `jq '.wheel | {stage, ticker, contract_symbol}' ~/forgetrader/state.json`
  - B: `jq '.wheel | {stage, ticker, contract_symbol}' ~/forgetrader-b/state.json`
- **Recovery:** a short stock position is neither cash-secured nor covered, so the D19
  premise ("a HALT costs staleness, not loss") does not hold for this class. Decision
  (Architect): whether this class needs same-day action. Decision (Baxman): close the short
  at the broker; then edit state to the stage the broker shows.
- **Confirmed by:** Baxman `--dry-run`: exit 0 with the expected noop for the stage.
- Seen live: (Baxman fills)

## HALT:FLAT

- **Outcome:** `MODE:HALT:FLAT but broker shows shares=N put_open=B call_open=B`
  (fs:262-265).
- **Trigger:** stage FLAT and the snapshot shows shares, an open put or an open call
  (fs:259-265). With a fresh FLAT wheel (ticker and contract_symbol None) any broker
  position is `HALT:snapshot unexpected position` first (fs:679-687), so this class needs a
  FLAT state that still carries `ticker` or `contract_symbol` (a hand edit; apply_trigger
  always resets the wheel on entry to FLAT, fs:208-209).
- **Engine did:** reconcile treatment: state unchanged, dump, decision row, common tail.
  Exit 4.
- **Diagnosis:**
  - A: `jq '.wheel' ~/forgetrader/state.json`  B: `jq '.wheel' ~/forgetrader-b/state.json`
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions ORDER BY rowid DESC LIMIT 10;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions ORDER BY rowid DESC LIMIT 10;"`
- **Recovery:** Decision (Baxman): the broker holds a wheel position the state calls FLAT.
  Edit state to the stage the broker shows (recovery step 5), or close the position at the
  broker and reset the wheel to `fresh_flat_state()["wheel"]`.
- **Confirmed by:** Baxman `--dry-run`: exit 0, `noop:FLAT stable` (and a scan) or the noop
  of the stage now in state.
- Seen live: (Baxman fills)

## HALT:CSP_PENDING

- **Outcome** (fs:267-293), `MODE:HALT:CSP_PENDING ` followed by one of:
  `tracked order not_found at broker`; `order status 'S' requires review` (S classed HALT:
  rejected, partially_filled, replaced, pending_replace, suspended, held, pending_review,
  fs:84-90); `unknown pending_order_status 'S'`; `filling but broker shows shares=N`;
  `order S but broker shows shares=N put_open=B`; `unclassifiable order status ...`
  (unreachable: classify_order_status returns only the four classes, fs:94-98).
- **Trigger:** at CSP_PENDING the tracked coid's status (fs:757-766, `not_found` on a 404)
  does not resolve to a legal move.
- **Engine did:** reconcile treatment: stage stays CSP_PENDING, dump (snapshot shows
  `pending_order_status`), decision row, common tail. Exit 4.
- **Diagnosis:**
  - A: `jq '.wheel | {stage, ticker, contract_symbol, client_order_id, opened_at}' ~/forgetrader/state.json`
  - B: `jq '.wheel | {stage, ticker, contract_symbol, client_order_id, opened_at}' ~/forgetrader-b/state.json`
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, symbol, limit_price, status, broker_order_id FROM orders WHERE client_order_id = '$(jq -r .wheel.client_order_id ~/forgetrader/state.json)';"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, symbol, limit_price, status, broker_order_id FROM orders WHERE client_order_id = '$(jq -r .wheel.client_order_id ~/forgetrader-b/state.json)';"`
  - Was it a HALT:submit first? A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, detail FROM events WHERE event_type = 'submit_exception' ORDER BY rowid DESC LIMIT 3;"`
    B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, detail FROM events WHERE event_type = 'submit_exception' ORDER BY rowid DESC LIMIT 3;"`
- **Recovery** (Decision (Baxman), per detail):
  - `not_found`: the order never reached the broker (crash before submit, or HALT:submit
    with `broker_reached=False`). Baxman confirms at the broker that no order or put exists
    under any coid, then resets the wheel to FLAT (`fresh_flat_state()["wheel"]`).
  - `rejected`: D11 says a gate bug. Reset to FLAT after confirming no position, and a
    brief on why the broker rejected (tick, collar).
  - `partially_filled`: impossible under the 1-contract cap; stop both timers, Architect.
  - `held` / `pending_review` / `suspended` / `replaced` / `pending_replace` /
    unknown string: undocumented or review states. Wait for the broker, or cancel at the
    broker; then the next run resolves (canceled -> FLAT).
  - `filling but ... shares` / `order S but ...`: stock or a put the pending order does not
    explain; reconstruct broker truth before any edit.
- **Confirmed by:** Baxman `--dry-run`: exit 0 with `noop:CSP_PENDING awaiting fill`,
  `CSP_PENDING->CSP_OPEN (fill)`, `CSP_PENDING->FLAT (cancel_or_expire)`, or after a reset
  `noop:FLAT stable`.
- Seen live: (Baxman fills)

## HALT:CSP_OPEN

- **Outcome**, `MODE:HALT:CSP_OPEN ` followed by one of:
  `put still open but broker shows shares=N` (fs:299);
  `put gone and shares=N is neither a 100-share assignment lot nor flat` (fs:309-312);
  expiry guard on put gone with 0 shares (fs:304-307, 121-135):
  `contract gone before expiry D (as_of D)`, `cannot parse expiry from contract_symbol 'C'`,
  `contract_symbol 'C' right does not match stage`, `expiry check needs as_of_date`
  (unreachable from run.py, which passes the run date, run.py:347);
  assignment with a bad symbol (fs:197-200): `assigned cannot parse strike from
  contract_symbol 'C'`, `assigned contract_symbol 'C' right is not P`.
- **Trigger:** at CSP_OPEN the broker's put / share combination is not `put open, 0 shares`,
  `put gone, 100 shares` (assigned) or `put gone, 0 shares, on or after expiry`.
- **Engine did:** reconcile treatment: stage stays CSP_OPEN, dump, decision row, common tail.
  Exit 4.
- **Diagnosis:**
  - A: `jq '.wheel | {stage, ticker, contract_symbol, opened_at}' ~/forgetrader/state.json`
  - B: `jq '.wheel | {stage, ticker, contract_symbol, opened_at}' ~/forgetrader-b/state.json`
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions ORDER BY rowid DESC LIMIT 10;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions ORDER BY rowid DESC LIMIT 10;"`
- **Recovery** (Decision (Baxman), per detail):
  - `before expiry`: the put vanished early (bought back, closed by the broker). Confirm at
    the broker; if gone with no shares, reset the wheel to FLAT.
  - `cannot parse` / `right does not match` / `assigned ... right is not P`:
    wheel.contract_symbol is wrong; correct it from the broker (or from the orders journal)
    and let the next run decide.
  - `put still open but shares` / `neither a 100-share lot`: shares the wheel does not
    explain (partial lot, extra lot, manual buy); reconstruct broker truth first.
- **Confirmed by:** Baxman `--dry-run`: exit 0 with `noop:CSP_OPEN stable`,
  `CSP_OPEN->HOLDING (assigned)`, `CSP_OPEN->FLAT (expired_worthless)`, or after an edit the
  noop of the new stage.
- Seen live: (Baxman fills)

## HALT:HOLDING

- **Outcome:** `MODE:HALT:HOLDING but broker shows shares=N put_open=B call_open=B`
  (fs:320-323).
- **Trigger:** at HOLDING the broker does not show exactly 100 shares with no open put or
  call (fs:315). The HOLDING wheel keeps only stage, ticker, cost_basis (fs:210-214), so
  any option position is `HALT:snapshot unexpected position` first; in practice this class
  means shares != 100 (sold, part-sold, extra shares).
- **Engine did:** reconcile treatment: stage unchanged, dump, decision row, common tail.
  Exit 4.
- **Diagnosis:**
  - A: `jq '.wheel' ~/forgetrader/state.json`  B: `jq '.wheel' ~/forgetrader-b/state.json`
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, symbol, side, qty, status FROM orders ORDER BY rowid DESC LIMIT 10;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, symbol, side, qty, status FROM orders ORDER BY rowid DESC LIMIT 10;"`
- **Recovery:** Decision (Baxman): shares 0 -> the lot was sold outside the engine; confirm
  and reset the wheel to FLAT. Other counts -> restore exactly 100 at the broker, or
  Architect decides how the wheel treats the extra / missing shares.
- **Confirmed by:** Baxman `--dry-run`: exit 0 with `noop:HOLDING stable` (and a CC scan) or
  `noop:FLAT stable` after a reset.
- Seen live: (Baxman fills)

## HALT:CC_PENDING

- **Outcome** (fs:325-351), `MODE:HALT:CC_PENDING ` followed by one of:
  `tracked order not_found at broker`; `order status 'S' requires review` (S classed HALT,
  fs:84-90); `unknown pending_order_status 'S'`; `filling but broker shows shares=N`
  (shares != 100); `order S but broker shows shares=N call_open=B`;
  `unclassifiable order status ...` (unreachable, fs:94-98).
- **Trigger:** at CC_PENDING the tracked coid's status, with the share count, does not
  resolve to a legal move.
- **Engine did:** reconcile treatment: stage stays CC_PENDING, dump, decision row, common
  tail. Exit 4.
- **Diagnosis:**
  - A: `jq '.wheel | {stage, ticker, cost_basis, contract_symbol, client_order_id, opened_at}' ~/forgetrader/state.json`
  - B: `jq '.wheel | {stage, ticker, cost_basis, contract_symbol, client_order_id, opened_at}' ~/forgetrader-b/state.json`
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, symbol, limit_price, status, broker_order_id FROM orders WHERE client_order_id = '$(jq -r .wheel.client_order_id ~/forgetrader/state.json)';"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, symbol, limit_price, status, broker_order_id FROM orders WHERE client_order_id = '$(jq -r .wheel.client_order_id ~/forgetrader-b/state.json)';"`
- **Recovery:** as HALT:CSP_PENDING, except the safe reset target is HOLDING (wheel keeps
  stage, ticker, cost_basis; `client_order_id` and `contract_symbol` cleared, fs:206-214),
  and only once Baxman confirms 100 shares and no call at the broker. Decision (Baxman):
  wait, cancel at the broker, or reset to HOLDING.
- **Confirmed by:** Baxman `--dry-run`: exit 0 with `noop:CC_PENDING awaiting fill`,
  `CC_PENDING->CC_OPEN (fill)`, `CC_PENDING->HOLDING (cancel_or_expire)`, or after a reset
  `noop:HOLDING stable`.
- Seen live: (Baxman fills)

## HALT:CC_OPEN

- **Outcome**, `MODE:HALT:CC_OPEN ` followed by one of:
  `call still open but broker shows shares=N` (fs:357);
  `call gone and shares=N is neither flat (called away) nor a full 100-share lot (expiry)`
  (fs:367-370); expiry guard on call gone with 100 shares (fs:362-365, 121-135):
  `contract gone before expiry D (as_of D)`, `cannot parse expiry from contract_symbol 'C'`,
  `contract_symbol 'C' right does not match stage`, `expiry check needs as_of_date`
  (unreachable from run.py).
- **Trigger:** at CC_OPEN the broker's call / share combination is not `call open, 100
  shares`, `call gone, 0 shares` (called away) or `call gone, 100 shares, on or after
  expiry`.
- **Engine did:** reconcile treatment: stage stays CC_OPEN, dump, decision row, common tail.
  Exit 4.
- **Diagnosis:**
  - A: `jq '.wheel | {stage, ticker, cost_basis, contract_symbol, opened_at}' ~/forgetrader/state.json`
  - B: `jq '.wheel | {stage, ticker, cost_basis, contract_symbol, opened_at}' ~/forgetrader-b/state.json`
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions ORDER BY rowid DESC LIMIT 10;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions ORDER BY rowid DESC LIMIT 10;"`
- **Recovery** (Decision (Baxman), per detail):
  - `call still open but shares=N` with N below 100: the short call is NOT fully covered,
    so the D19 premise does not hold. Decision (Architect): same-day action or not.
    Decision (Baxman): restore cover or buy back the call at the broker.
  - `before expiry`: the call vanished early with the shares still held (bought back);
    confirm at the broker, then reset to HOLDING.
  - `cannot parse` / `right does not match`: correct wheel.contract_symbol from the broker.
  - `neither flat nor a full lot`: partial assignment or a manual trade; reconstruct broker
    truth first.
- **Confirmed by:** Baxman `--dry-run`: exit 0 with `noop:CC_OPEN stable`,
  `CC_OPEN->FLAT (called_away)`, `CC_OPEN->HOLDING (cc_expired_worthless)`, or after an
  edit the noop of the new stage.
- Seen live: (Baxman fills)

## HALT:unknown

- **Outcome:** `MODE:HALT:unknown stage 'S'` (fs:372).
- **Trigger:** `wheel.stage` is none of the six STAGES (fs:53). load_state does not
  validate the stage (fs:471-474) and build_snapshot does not reject it, so a hand-edited or
  corrupted stage reaches reconcile.
- **Engine did:** reconcile treatment: state unchanged, dump, decision row, common tail.
  Exit 4.
- **Diagnosis:**
  - A: `jq '.wheel' ~/forgetrader/state.json`  B: `jq '.wheel' ~/forgetrader-b/state.json`
  - A: `sqlite3 -readonly ~/forgetrader/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions WHERE instr(action, 'HALT:unknown') = 0 ORDER BY rowid DESC LIMIT 5;"`
  - B: `sqlite3 -readonly ~/forgetrader-b/journal.db "SELECT ts, stage_from, stage_to, action FROM decisions WHERE instr(action, 'HALT:unknown') = 0 ORDER BY rowid DESC LIMIT 5;"`
    (the last stage the engine itself recorded).
  - A: `cat "$(ls -1t ~/forgetrader/scans/state_halt_*.txt | head -1)"`
  - B: `cat "$(ls -1t ~/forgetrader-b/scans/state_halt_*.txt | head -1)"`
- **Recovery:** Decision (Baxman): which stage broker truth shows (recovery step 5); edit
  `wheel.stage` (and the keys that stage needs). Find who wrote the bad stage first.
- **Confirmed by:** Baxman `--dry-run`: exit 0 with `noop:STAGE ...` for that stage.
- Seen live: (Baxman fills)

## Refusals and other non-HALT stops

Not HALT classes (no `HALT:` token), listed so the morning review does not mistake them.

- **`live:refused:not_paper`** (run.py:286-294): `--live` and `load_credentials()` returned
  paper not True. One decision row and one runs row, NO state write, no client built, no
  broker read, Telegram summary, exit 3. Diagnosis A/B:
  `sqlite3 -readonly ~/forgetrader/journal.db "SELECT rowid, ts, outcome FROM runs WHERE instr(outcome, 'refused:not_paper') > 0 ORDER BY rowid DESC LIMIT 5;"`
  (B: `~/forgetrader-b/journal.db`). Decision (Baxman): which keys that account's .env holds;
  live keys are Phase W only. Confirmed by the next timer outcome not being refused.
- **`live:submit:refused:CHECK`** (run.py:414, 497): the proposal failed a submit check
  (submit.py:40-68, 148-195). State untouched, exit 0.
- **`live:skip:market_closed` / `live:skip:no_passing_proposal` / `live:skip:not_...`**
  (run.py:196-201): exit 0.
- **probe** (forgetrader/probe.py:193, 214, 222-225): runs rows `probe:refused:CHECK`
  (exit 3) and `probe:halt` (exit 4). `probe:halt` has no `HALT:` token, but report.py's
  digest counts it: `outcome LIKE '%HALT%'` (report.py:177) is case-insensitive in SQLite.
  Recovery as HALT:submit (probe calls the same place_csp).
- **Uncaught, exit 1, no runs row:** corrupt or empty state.json, credentials or client
  build failure, get_clock exception, broker read failure (run.py:66-68, 84-88). Visible
  only in journalctl and the CRASH alert.
- **Standalone CLIs:** `python -m forgetrader.cc_scan` prints `HALT watchlist_structure`
  and exits 4 (cc_scan.py:321-323); wheel_dryrun.py main exits 1 on the same error
  (wheel_dryrun.py:527-529). Neither writes a runs row.
