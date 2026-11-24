# Forced paths - funding criterion 4

Brief forced-paths (Tier 1-grade evidence). Funding criterion 4 (INSTRUCTIONS.md,
"Funding criteria"): `partially_filled`, `pending_cancel`, a crash between
write-ahead and submit, and a submit timeout, each forced by fault injection, not
waited for. Suite: `tests/test_forced_paths.py`. FI script: `tests/fi/fi_forced_paths.sh`.

Line references are to base 550d23e. Expected behaviour below was written from the
code and the banked decisions BEFORE the tests were written.

## Harness (what is real, what is faked)

Real: `run.run(live=True)` end to end, so the run.py stage branch and submit branch
(run.py:345-525), `fs.read_broker_financials` (forgetrader_state.py:730-792, incl. the
404 -> `not_found` mapping at :759-766), `fs.build_snapshot` (:655-723),
`fs.reconcile_and_handle` (:628-644), `submit.place_csp` (submit.py:71-138),
`submit.place_cc` (submit.py:198-268), `fs.write_state` (:477-493), the journal.

Faked (patched module attributes, restored after each run):
- `alpaca.trading.client.TradingClient` -> `FakeBroker`: a TradingClient-shaped fake
  whose whole broker (money, positions, orders by client_order_id, every
  `submit_order` call, clock) lives in one JSON file in the scenario's mkdtemp dir, so
  a child process and the parent share one broker. `get_order_by_client_id` for an
  unknown id raises the SDK's own `APIError` built exactly as alpaca/common/rest.py:199-207
  builds it (requests `HTTPError` with a 404 response). `get_orders(OPEN)` returns
  every order whose status is not terminal (filled, canceled, expired, rejected,
  replaced): an assumption of the fake, not a documented Alpaca list. The two data
  clients are a fake that raises on any call.
- `credentials.load_credentials` -> ("k", "s", True): placeholders, no keys read.
- `wheel_dryrun.scan_watchlist` / `cc_scan.scan_calls` -> one passing row, reused by
  name from tests/test_run_stages.py (`LIVE_ROW` NOK put, `CC_ROW` F call over the F
  HOLDING fixture, `_scan_result`, `_cc_result`). The real producers feeding the real
  validators are proven in test_live_submit.py and test_place_cc.py.
- `alerts.send`, `fs.alert_stub` -> files in the scenario dir. `fs.STATE_PATH`,
  `JOURNAL_PATH`, `SCANS_DIR` -> the scenario dir.

Clocks are pinned (2026-11-04 15:30, 16:30, 17:30 UTC, `is_open` True, one per run);
the seed state is written under a pinned instant. No network, no real client.

Why a new fake: the existing fakes (test_run_stages `FakeTC`, test_place_csp /
test_place_cc `FakeClient`, test_probe `FakeTC`) are single-run and feed canned
financials; none holds broker state across runs or across a process boundary, and
none exercises the real `read_broker_financials`.

Every scenario starts at the leg's home stage (CSP leg: FLAT, empty broker; CC leg:
HOLDING F, cost_basis 12.0, broker 100 F) and its first run submits through the real
gate (`--live`, paper, stable noop, passing proposal, clock open). Broker money changes
every run (M1, M2, M3), so a money refresh is visible.

Per run the suite asserts: exit code; exact `runs.outcome` token (this run's rows);
`last_run.outcome`; wheel stage; wheel `client_order_id`; this run's decision actions;
this run's orders rows `(status, client_order_id)`; this run's event types; the fake's
total `submit_order` calls so far in the scenario; the state's money fields; whether a
scan ran. On every broker-mismatch HALT run it also asserts the state file is
unchanged apart from `last_run` and `generated_at` (see "Doc disagreement" below).
Per scenario: total `submit_order` calls <= 1.

## Expected behaviour

P = `CSP_PENDING` (CSP leg) / `CC_PENDING` (CC leg). Run 1 of P1, P2, P4 is common:

Run 1 accept (P1, P2): exit 0, `live:submit:submitted status=accepted coid=C`,
stage P, coid C, decisions `[noop:FLAT stable | noop:HOLDING stable, propose_csp |
propose_cc, submit:submitted status=accepted coid=C]`, orders `[(None, C), (accepted, C)]`,
events `[live_complete]`, submits 1, money M1, scan yes. Code: run.py:403-426 /
482-509, submit.py:105-117 / 235-247. Decisions D15 (write-ahead, submit exactly once),
D5 (scan only on the stable noop).

### P1 partially_filled (both legs)

Unreachable at the hardcoded 1-contract cap (submit.py:26 `CSP_QTY = 1`, :145
`CC_QTY = 1`); this guards a size-up. Forced: run 2 sees the tracked order
`partially_filled`, still open, and the short contract position at -1.

Expected run 2: exit 4, `live:HALT:P order status 'partially_filled' requires review`,
stage P, coid C, decisions `[HALT:P order status 'partially_filled' requires review]`,
no orders rows, no events, submits 1, money M1 (no refresh), no scan, state unchanged
but `last_run`. Code: forgetrader_state.py:85 (`"partially_filled": "HALT"`),
:268-275 (CSP) / :326-333 (CC): status is classified BEFORE position flags, so the
-1 position cannot turn it into a fill; run.py:348-349 -> `_halt_exit` (run.py:205-213,
no money refresh). Decisions D10 (explicit map, classify first), D11 (partially_filled
-> HALT), D4.

### P2 pending_cancel (both legs)

Forced: run 2 sees the tracked order `pending_cancel`, still open, no position; run 3
sees it `canceled`.

Expected run 2: exit 0, `live:noop:P awaiting fill`, stage P, coid C, decisions
`[noop:P awaiting fill]`, events `[live_complete]`, submits 1, money M2, no scan.
Expected run 3: exit 0, `live:CSP_PENDING->FLAT (cancel_or_expire)` /
`live:CC_PENDING->HOLDING (cancel_or_expire)`, stage FLAT / HOLDING, coid None,
events `[live_complete]`, submits 1, money M3, NO scan that run.
Code: forgetrader_state.py:78 (`"pending_cancel": "LIVE"`), :281-282 / :339-340
(LIVE without the contract position -> noop), :283-289 / :341-347 (DEAD -> exit),
:206-207 (exit clears client_order_id); run.py:363 / :435 (scan only on the exact
stable noop). Decisions D10 (every status classed), D12 (pending_cancel limbo is a
known waiting state: a cancel in flight can still fill), D15 (next run resolves via
tracked status), D5 (a transition into FLAT does not scan), D3 (exit allowlist).
Fail-closed holds: no submit is possible at P (only a stable noop at home scans), and
the stage moves only on the broker's terminal status.

### P3 crash between write-ahead and submit (both legs)

Forced: run 1 is a child process (`python tests/test_forced_paths.py --child DIR
--kill`) whose `fs.write_state` is wrapped: after the REAL atomic write returns with a
P-stage state (the first such write is place_csp / place_cc's write-ahead,
submit.py:105 / :235), it writes a marker to fd 1 and calls `os._exit(77)`, before the
intent row (submit.py:106 / :236) and before `submit_order` (submit.py:110 / :240).
Runs 2 and 3 are fresh child processes (no kill) against a broker holding NO order
with that client_order_id.

Expected run 1: exit 77 with the marker, state.json parses as JSON, no
`state.json.tmp` left, stage P, coid a uuid4 string, `last_run` still the seed's
(`init`), decisions `[stable noop, propose]`, no orders rows, no events, no runs row,
submits 0, money M1 (the write-ahead carries the refreshed money), scan yes.
Expected runs 2 and 3: exit 4, `live:HALT:P tracked order not_found at broker`,
stage P, coid unchanged, decisions `[HALT:P tracked order not_found at broker]`, no
orders rows, no events, submits 0, money M1, no scan, state unchanged but `last_run`.
Code: forgetrader_state.py:477-493 (temp file, fsync, `os.replace`, dir fsync),
:759-766 (APIError 404 -> `not_found`), :270-271 / :328-329 (`not_found` -> HALT);
run.py:259-260 (tracked id passed only at P). Decisions D3 (crash pre-submit -> 404 ->
not_found -> HALT), D6 (atomic state), D4.

### P4 submit timeout (both legs)

What alpaca-py 0.43.5 actually raises (recon of the installed package):
- `RESTClient._request` passes NO `timeout` to requests (alpaca/common/rest.py:113-125,
  :195), so the client never times out by itself (see Gaps G3).
- A gateway timeout arrives as HTTP 504. 504 is in `DEFAULT_RETRY_EXCEPTION_CODES`
  (alpaca/common/constants.py:11-13: 3 retries, 3 s apart, codes [429, 504]), so
  `_request` re-sends (rest.py:127-135, :201-202) and, when retries run out, raises
  `APIError(response.text, http_error)` (rest.py:204-207) with `status_code` 504
  (alpaca/common/exceptions.py:25-29). That `APIError` is the exception the test
  raises from `submit_order`, built the same way, body `504 Gateway Time-out`.
  A connection-level failure would surface as a requests `ConnectionError`; place_csp /
  place_cc catch every exception the same way (submit.py:118 / :248), so it is not
  forced separately.

Expected run 1 (both variants): exit 4, `live:HALT:submit APIError: 504 Gateway
Time-out`, stage P, coid C (the request's), decisions `[stable noop, propose,
HALT:submit APIError: 504 Gateway Time-out]`, orders `[(None, C)]` (intent only),
events `[submit_exception]`, submits 1, money M1, scan yes. Code: submit.py:118-138 /
248-268 (no retry, no rollback, returns the P state), run.py:408 / :491 (the returned
state is the one persisted), :421-424 / :504-507 -> `_halt_exit`. Decision D15.

Variant a (broker never got it). Expected runs 2 and 3: exit 4,
`live:HALT:P tracked order not_found at broker`, stage P, coid C, submits 1, money M1,
no scan, state unchanged but `last_run`. Decisions D3, D15 ("next run resolves via
tracked status"), D4.

Variant b (broker holds it under C, status `new`). Expected run 2: exit 0,
`live:noop:P awaiting fill`, stage P, coid C, money M2, submits 1. Expected run 3
(broker: `filled`, contract position -1): exit 0, `live:CSP_PENDING->CSP_OPEN (fill)` /
`live:CC_PENDING->CC_OPEN (fill)`, stage CSP_OPEN / CC_OPEN, coid None, money M3,
submits 1. Decisions D15, D19 (`HALT:submit` can self-heal on the next run; the HALT
row stays the trace).

## Gaps

### G1 - alpaca-py re-POSTs an order on HTTP 504 (wire-level double submit)

Parked 24 Nov 2026 (Architect): Alpaca documents client_order_id as a unique identifier
and a duplicate is reportedly rejected; G2 HALTs on any duplicate regardless; SDK
private-attribute patch rejected as fragile.

- Where: alpaca/common/rest.py:127-135 and :201-202 with constants.py:13
  (`DEFAULT_RETRY_EXCEPTION_CODES = [429, 504]`), reached from run.py:296
  (`TradingClient(api_key, api_secret, paper=paper)`, default retry config) and
  submit.py:110 / :240. Also probe.py:91.
- Scenario: P4. A 504 from Alpaca's gateway means the request may have reached the
  order service. The SDK sleeps 3 s and POSTs the SAME order body (same
  client_order_id) again, up to 3 more times, inside the one `submit_order` call the
  engine counts. Whether Alpaca rejects a repeated client_order_id is not documented
  in anything recon'd here, so by the documentation rule it cannot be relied on. If it
  is not rejected, two live orders can exist under one client_order_id, which breaks
  the 1-contract cap (see G2 for why the snapshot does not catch it before a fill).
- Not testable in this suite: proving it needs a real `TradingClient`, which this brief
  forbids constructing. The suite's P4 counts `submit_order` calls (engine level), not
  POSTs.
- Proposed minimal fix: `TradingClient.__init__` (alpaca/trading/client.py:53-86) does
  not expose the retry settings, and `RESTClient` ignores an empty or zero override
  (rest.py:76-83). One line after run.py:296 (and probe.py:91):
  `trading_client._retry_codes = [429]`, plus a test that pins that private attribute
  on the installed SDK, so an upgrade that renames it fails loudly. 429 stays: a rate
  limit refusal never created an order.

### G2 - build_snapshot tolerates any number of open orders under the tracked id

Fixed in g2-g3-hardening. See "G2 / G3 fix (g2-g3-hardening)" below.

- Where: forgetrader_state.py:703-712. Every open order whose client_order_id equals
  the tracked one is tolerated; the count is never checked.
- Scenario: P4 after G1 (two orders under C). Snapshot passes, reconcile reads the
  status of whichever order `get_order_by_client_id` returns, and the wheel can sit at
  P or advance on one fill while a second order is live. D4 ("unexpected open order =
  HALT") is not met for the second order. The snapshot only catches it after both
  fill (contract qty -2 -> SnapshotError at :681-684), by which time 2 contracts are
  sold.
- Proposed minimal fix: in build_snapshot, count the tolerated orders and raise
  `SnapshotError("more than one open order with tracked client_order_id ...")` when
  the count exceeds 1. No test written here (it would assert the fix).

### G3 - a stalled submit hangs the run forever: no exception, no HALT

Fixed in g2-g3-hardening, effective once Baxman installs the units. See "G2 / G3 fix
(g2-g3-hardening)" below.

- Where: alpaca/common/rest.py:113-125 / :195 (no `timeout` passed to requests), and
  deploy/forgetrader-run.service (`Type=oneshot`, no `TimeoutStartSec=`; for oneshot
  systemd disables the start timeout by default, `man systemd.service`, systemd 255).
- Scenario: P4 when the connection stalls instead of returning a status. place_csp
  never returns, no HALT row is written, no alert is sent, and every later timer
  firing is skipped while the unit is still active. That is a silent noop where a
  HALT is owed (D19 morning review reads HALT rows; there is none).
- Proposed minimal fix: one line in deploy/forgetrader-run.service,
  `TimeoutStartSec=10min`. systemd then SIGTERMs the run; that is the P3 path (state
  already written ahead, the next run resolves by tracked status) and `OnFailure=`
  fires the crash alert. Out of scope here (no deploy/** edits); Baxman's call.

### Observations (not gaps: fail-closed holds)

- A P3 kill leaves no `runs` row for the killed run (run.py:520 never runs). The trace
  is the next run's HALT row, so D19's morning query still sees it.
- `pending_cancel` has no age bound: a cancel stuck at the broker is a quiet noop each
  run. No submit is possible at P and the stage does not move, and DAY TIF (D12) bounds
  the order's life, so this is staleness, not exposure.

### Doc disagreement (reported, not fixed)

Resolved 24 Nov 2026: brief wording was loose; code and spec agree (last_run only).

The brief asks for "no state write on a broker-mismatch HALT". The code writes state
on every HALT: `_halt_exit` sets `last_run` and calls `fs.write_state`
(run.py:209-210). INSTRUCTIONS.md run sequence step 4 / 5 says "`last_run` only" and
step 6 says the money refresh never happens on a broker-mismatch HALT. The suite
asserts that reading: on a broker-mismatch HALT the state file equals the previous
state apart from `last_run` and `generated_at` (money and wheel untouched).

## Regression guards

- P3 "state.json parses as JSON" and "no state.json.tmp left": the kill fires after
  `write_state` has returned, so the atomic write is never interrupted mid-write. These
  two assertions hold on any single-line change that still lets `write_state` return.
  They are regression guards, not proofs of atomicity (test_place_csp S8 pins the
  fsync / replace / dir-fsync order).
- P3 "marker printed": harness integrity (proves the kill hook ran), not engine logic.

## Fault injection (pre-registered)

Script: tests/fi/fi_forced_paths.sh (convention of fi_b8.sh / fi_journal_report.sh:
temp copy of the tracked tree with the .venv linked in, the worktree itself never
mutated). Per mutation: pristine copy, `sed -i` ONE line (line-addressed, original
text checked first), `mutated_lines` must be 1, ALL suites run, each suite's failure
count compared with the prediction below (suites not named: 0), `cp` restore from
pristine, `sha256sum -c`. A traceback in any suite is a CRASH, never a match.

| ID | Path / leg | File:line | Mutation | Predicted failing assertions |
| --- | --- | --- | --- | --- |
| M1 | P1 both | forgetrader_state.py:85 | `"partially_filled": "HALT"` -> `"FILLED"` | state_machine 6, forced_paths 18 |
| M2 | P1 CSP | forgetrader_state.py:268 | `cls = "FILLED" if order_status == "partially_filled" else classify_order_status(order_status)` | state_machine 2, forced_paths 9 |
| M3 | P1 CC | forgetrader_state.py:326 | same as M2 | state_machine 2, forced_paths 9 |
| M4 | P2 both | forgetrader_state.py:78 | `"pending_cancel": "LIVE"` -> `"DEAD"` | state_machine 6, forced_paths 28 |
| M5 | P2 CSP | forgetrader_state.py:268 | `cls = "DEAD" if order_status == "pending_cancel" else ...` | state_machine 2, forced_paths 14 |
| M6 | P2 CC | forgetrader_state.py:326 | same as M5 | state_machine 2, forced_paths 14 |
| M7 | P3 + P4a both | forgetrader_state.py:764 | 404 read as `"canceled"` instead of `"not_found"` | forced_paths 82 |
| M8 | P3 CSP | submit.py:105 | place_csp write-ahead removed (`pass`) | place_csp 14, run_stages 2, probe 2, forced_paths 20 |
| M9 | P3 CC | submit.py:235 | place_cc write-ahead removed (`pass`) | place_cc 17, run_stages 2, forced_paths 20 |
| M10 | P4 CSP | submit.py:136 | place_csp HALT returns the input state (`return state, _result(`) | place_csp 3, run_stages 1, alerts 1, forced_paths 40 |
| M11 | P4 CC | submit.py:266 | place_cc HALT returns the input state | place_cc 4, run_stages 2, forced_paths 40 |
| M12 | P4 CSP | run.py:408 | returned state not persisted (`pass`) | run_stages 4, clock 2, alerts 1, forced_paths 63 |
| M13 | P4 CC | run.py:491 | returned state not persisted (`pass`) | run_stages 7, clock 1, forced_paths 63 |

How the forced_paths counts arise (per run, failing assertions):
- M1: P1 run 2 on each leg becomes a fill (rc, runs, last_run, stage, coid,
  decisions, events, money, unchanged) 9 x 2 legs = 18.
- M2 / M3: the same 9 on one leg.
- M4: P2 run 2 becomes `cancel_or_expire` (runs, last_run, stage, coid, decisions) 5;
  run 3 is at home, scans and SUBMITS AGAIN (runs, last_run, stage, coid, decisions,
  orders, submits, scan) 8; scenario submits <= 1 fails 1; 14 x 2 legs = 28.
- M5 / M6: the same 14 on one leg.
- M7: P3 run 2 exits to home (9), run 3 scans and submits (11) = 20 per leg; P4a run 2
  exits to home (9), run 3 submits a SECOND order (11), scenario <= 1 (1) = 21 per leg;
  (20 + 21) x 2 = 82.
- M8 / M9: P3 run 1 now submits before any write-ahead and dies at the end-of-run
  write (last_run, decisions, orders, submits) 4; runs 2 and 3 find the order and
  noop (rc, runs, last_run, decisions, events, submits, money, unchanged) 8 + 8 = 20.
- M10 / M11: P4a run 1 persists home (stage, coid) 2, run 2 submits again (11), run 3
  waits on the second order (9), scenario (1) = 23; P4b run 1 (2), run 2 HALT:snapshot
  on the unexpected open order (8), run 3 HALT:snapshot on the unexpected position (7)
  = 17; 40.
- M12 / M13: P1 run 1 (2) + run 2 HALT:snapshot (5) = 7; P2 run 1 (2) + run 2 (8) +
  run 3 submits again (6) + scenario (1) = 17; P4a run 1 (2) + run 2 submits again
  (12) + run 3 (7) + scenario (1) = 22; P4b 2 + 8 + 7 = 17; 63.

Other suites' counts come from reading their assertions against each mutation
(test_state_machine 13b / 13c; test_place_csp S1, S2, S3, S4, S5, S6; test_place_cc
P1-P6, P9; test_run_stages L1, L8, H2, H11; test_probe S2, S3; test_clock K2, K3,
K7b; test_alerts R5).

No mutation in this table is registered as a regression guard: each is predicted to
bite.

## G2 / G3 fix (g2-g3-hardening)

Base c20f652. Line references below are to this branch.

G2: `build_snapshot` counts the open orders it tolerates under the tracked
client_order_id (forgetrader_state.py:703-714) and raises
`SnapshotError("more than one open order with tracked client_order_id COID: N")` when
N exceeds 1 (:715-718). Exactly one stays tolerated. Every later line of the module
moves down by 6 (e.g. read_broker_financials's 404 mapping is now :765-772).
Tests: test_snapshot fixtures 22 (CSP_PENDING) and 23 (CC_PENDING), message pinned
exactly; test_forced_paths P5 below.

### P5 second open order under the tracked id (both legs)

Forced: run 1 accepted as in P1 (common run 1 above). The fake broker then lists a
second OPEN order (status `new`) under the same coid C in `get_orders(OPEN)`;
`get_order_by_client_id(C)` still returns the first. FakeBroker gained an optional
`dup_orders` list that only P5 sets; P1-P4 never set it, so their behaviour is
unchanged.

Expected run 2: exit 4, `live:HALT:snapshot more than one open order with tracked
client_order_id C: 2`, stage P, coid C, decisions `[HALT:snapshot more than one open
order with tracked client_order_id C: 2]`, no orders rows, no events, submits 1,
money M1 (no refresh), no scan, state unchanged but `last_run`. Code:
forgetrader_state.py:715-718, run.py:334-342 -> `_halt_exit` (run.py:205-213). Decision
D4 (an unexpected open order is a HALT).

G3: `TimeoutStartSec=10min` is the last line of the [Service] section of
deploy/forgetrader-run.service (line 10). Account B's run unit has no committed file:
deploy/b/install_b.py renders forgetrader-b-run.service from A's INSTALLED
forgetrader-run.service, so B inherits the line once A's unit is installed, and
install_b.py now refuses to render a B run service without the exact line
(`timeout-missing`, `TIMEOUT_LINE` at install_b.py:56, check at :189-190). Tests:
test_install_b R6 (fixture equals the committed unit), R7 (the line is in [Service] of
the committed A unit and of the rendered B unit), R2 / D5 / A5 (literal B text), C12
(refusal when the A unit lacks the line). Not effective until Baxman installs the A
unit and re-runs install_b.py --apply.

### Fault injection (pre-registered, g2-g3-hardening)

Script: tests/fi/fi_g2_g3.sh, same convention as fi_forced_paths.sh (temp copy of the
tracked tree with the .venv linked in, worktree never mutated; per mutation: original
line checked, `sed -i` ONE line, `mutated_lines` must be 1, ALL suites, failure count per
suite vs the prediction, `cp` restore, `sha256sum -c`). A traceback is a CRASH.

| ID | Gap / leg | File:line | Mutation | Predicted failing assertions |
| --- | --- | --- | --- | --- |
| G2a | G2 both | forgetrader_state.py:715 | `if n_tracked > 1:` -> `if False:` | snapshot 4, forced_paths 14 |
| G3a | G3 A | deploy/forgetrader-run.service:10 | `TimeoutStartSec=10min` -> `# TimeoutStartSec=10min removed by FI` | install_b 2 |
| G3b | G3 B | deploy/b/install_b.py:191 | `return b_name, t` -> `return b_name, t.replace(TIMEOUT_LINE + chr(10), "")` | install_b 4 |
| G3c | G3 B guard | deploy/b/install_b.py:189 | `if b_name == ... and TIMEOUT_LINE not in ...:` -> `if False:` | install_b 7 |

How the counts arise:
- G2a: test_snapshot fixtures 22 and 23 each fail the "expected SnapshotError" check and
  the exact-message check (2 x 2 = 4). test_forced_paths P5 run 2 on each leg becomes
  `noop:P awaiting fill` (rc, runs, last_run, decisions, events, money, unchanged) 7 x 2
  legs = 14; stage, coid, orders, submits and scan are the same either way.
- G3a: the comment removes the directive (systemd ignores `#` lines). test_install_b R6
  (fixture no longer equals the committed run.service) 1, R7 for the committed unit 1.
  The render tests use the fixture texts, so nothing else moves.
- G3b: B's rendered run service loses the line after the guard passed. test_install_b
  R2 for forgetrader-b-run.service 1, R7 for the rendered unit 1, D5 (dry-run prints it
  in full) 1, A5 (written text) 1 = 4.
- G3c: the guard never refuses. C12 dry-run: exit 2 and the named reason fail (2), nothing
  is written either way; C12 --apply: exit 2, named reason, nothing written, runner
  unused and no B unit all fail (5); 7.

No mutation in this table is registered as a regression guard: each is predicted to bite.
