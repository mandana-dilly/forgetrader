# HALT coverage matrix

Brief wheel-prep-1125 T2 (milestone 10; D35 needs recovery steps for every HALT class).
Every file:line is at base 4b04c86169e59efe29e1bb4a6bcca8a856c34617 (written at c20f652,
re-verified after g2-g3-hardening moved forgetrader_state.py from line 703 on, deploy/b/install_b.py
and docs/HALT_RUNBOOK.md; the runbook re-verified its own moved refs, docs/HALT_RUNBOOK.md:7-8).
`fs` = forgetrader_state.py.

## Method

- **Tokens.** A token is the text after `HALT:` up to the first space, the class rule of
  docs/HALT_RUNBOOK.md:13-19. The list is the output of tests/test_halt_runbook.py's ast
  extractor (tests/test_halt_runbook.py:134-205) over run.py, submit.py, fs, wheel_dryrun.py,
  cc_scan.py and their repo imports: 17 classes. `grep -rn HALT` over forgetrader/ and the
  root modules adds two exit-4 paths outside that closure (probe.py, the cc_scan CLI).
- **Exit 4 paths in the package** (`grep -rn "return 4"`): run.py:213 (`_halt_exit`, every
  class except the two below), run.py:257 (state_missing), run.py:265 (instance, via
  run.py:216-234), forgetrader/probe.py:225 (`probe:halt`), forgetrader/cc_scan.py:323
  (standalone CLI). deploy/ scripts are ops tooling, not engine HALTs, and are out of scope.
- **Severity** is D19a (docs/HALT_RUNBOOK.md:66-77): three variants are "act before the next
  US open"; every other class is next-morning review.
- **Run-path test** = a test that drives `run.run()` to exit 4 and checks the journal. **Unit
  test** = pure `reconcile` / `apply_trigger` / `build_snapshot` / `reconcile_and_handle`
  only. A class whose run-path column was NONE at base and that the existing fakes can reach
  got a case in tests/test_halt_coverage.py (HC1-HC8).

## Matrix

| Token | Emitter file:line | Trigger condition | D19a severity | Runbook section | Run-path test | Unit test |
| --- | --- | --- | --- | --- | --- | --- |
| `HALT:state_missing` | run.py:251-256 (runs row run.py:253), exit run.py:257 | `fs.load_state` raises FileNotFoundError (run.py:247-249) | next-morning | HALT_RUNBOOK.md:79 | tests/test_run_stages.py::main S6, S6L (dry / live); tests/test_recovery_drill.py (drill step 4) | - |
| `HALT:instance` | run.py:225-232, exit run.py:265 | `forge_instance.current()` raises InstanceError (forge_instance.py:52-62) | next-morning | HALT_RUNBOOK.md:105 | tests/test_instance.py::part_r (R1, R2) | tests/test_instance.py::part_e |
| `HALT:clock` | run.py:316-320 | clock None, timestamp None or tz-naive (run.py:311-314) | next-morning | HALT_RUNBOOK.md:132 | tests/test_run_stages.py::main S8 | - |
| `HALT:snapshot` | run.py:337-342; messages fs:660, 668, 670, 678, 683-684, 687, 693, 700-701, 711-713 | `fs.build_snapshot` raises SnapshotError | **act before open** for `tracked contract ... expected -1` (fs:683-684); else next-morning | HALT_RUNBOOK.md:155 | tests/test_run_stages.py::main S3 (unexpected position); tests/test_halt_coverage.py HC8 (qty -2) | tests/test_snapshot.py |
| `HALT:watchlist_structure` | run.py:369-375 (FLAT), 443-449 (HOLDING) | scan raises WatchlistStructureError (watchlist.py:98, 135, 140, 147) | next-morning | HALT_RUNBOOK.md:202 | tests/test_run_stages.py::main S7, H8 | - |
| `HALT:submit` | run.py:416, 424 (CSP), 499, 507 (CC); detail submit.py:136-138, 266-268; alert stub submit.py:127-128, 257-258 | exception after the order is built (submit.py:104-138, 234-268) | next-morning | HALT_RUNBOOK.md:230 | tests/test_run_stages.py::main L8, H11; tests/test_forced_paths.py::p4a, p4b | tests/test_place_csp.py, tests/test_place_cc.py |
| `HALT:holding_basis_invalid` | fs:163 (via fs:316-318, 173-175) | cost_basis None, non-numeric, bool, NaN/inf or <= 0 at HOLDING stable or on entry to HOLDING | next-morning | HALT_RUNBOOK.md:267 | tests/test_halt_coverage.py HC1 (added) | tests/test_cc_gates.py::part_s S3, S4 |
| `HALT:no` | fs:194 | `apply_trigger` on a pair not in LEGAL_TRANSITIONS (fs:57-68) | next-morning | HALT_RUNBOOK.md:293 | NONE: not reachable from run.py (runbook 298-303); place_csp / place_cc turn it into `submit:refused:apply_trigger_unexpected` (submit.py:80-83, 211-214) | tests/test_state_machine.py:70, 109, 112 |
| `HALT:broker_snapshot` | fs:250 | snapshot without `shares` | next-morning | HALT_RUNBOOK.md:313 | NONE: not reachable from run.py, build_snapshot always sets `shares` (fs:724-729) | tests/test_state_machine.py:208 |
| `HALT:broker_snapshot['shares']` | fs:253 | `shares` not a non-negative int; from run.py only a negative wheel-ticker qty (fs:677-680) | **act before open** | HALT_RUNBOOK.md:329 | tests/test_halt_coverage.py HC2 (added) | tests/test_state_machine.py:211, 215, 219 |
| `HALT:FLAT` | fs:262-265 | FLAT and broker shows shares or an option (needs a FLAT wheel carrying ticker / contract) | next-morning | HALT_RUNBOOK.md:352 | tests/test_halt_coverage.py HC3 (added) | tests/test_state_machine.py:225; tests/test_reconcile_handle.py:122 |
| `HALT:CSP_PENDING` | fs:271, 273-274, 276, 279, 285-288, 290-293 | tracked status not_found / HALT-class / unknown, or shares or put contradict the status | next-morning | HALT_RUNBOOK.md:376 | tests/test_forced_paths.py::p1 (partially_filled), p3, p4a (not_found), CSP leg | tests/test_state_machine.py:151, 190, 229 |
| `HALT:CSP_OPEN` | fs:299, 304-307 (guard fs:125-134), 310-312, 200 | put open with shares; put gone with shares not 0/100; put gone before expiry; bad symbol on assignment | next-morning | HALT_RUNBOOK.md:414 | tests/test_run_stages.py::main S4, S9 | tests/test_cc_gates.py:98-105; tests/test_state_machine.py |
| `HALT:HOLDING` | fs:320-323 | HOLDING and broker is not exactly 100 shares, no option | next-morning | HALT_RUNBOOK.md:449 | tests/test_halt_coverage.py HC4 (added) | tests/test_cc_gates.py:136; tests/test_state_machine.py:251 |
| `HALT:CC_PENDING` | fs:329, 331-332, 334, 337, 343-346, 348-351 | as CSP_PENDING, with shares != 100 | next-morning | HALT_RUNBOOK.md:472 | tests/test_forced_paths.py::p1, p3, p4a, CC leg | tests/test_state_machine.py:167, 178, 255 |
| `HALT:CC_OPEN` | fs:357, 362-365 (guard fs:125-134), 368-370 | call open with shares != 100; call gone with shares not 0/100; call gone before expiry | **act before open** for `call still open but broker shows shares=N`, N below 100 (fs:357); else next-morning | HALT_RUNBOOK.md:499 | tests/test_halt_coverage.py HC5 (shares 50), HC6 (before expiry) (added) | tests/test_state_machine.py:268, 272 |
| `HALT:unknown` | fs:372 | wheel.stage not in STAGES (fs:53) | next-morning | HALT_RUNBOOK.md:534 | tests/test_halt_coverage.py HC7 (added) | tests/test_state_machine.py:277 |
| `HALT:probe` (alert text; runs row `probe:halt`) | forgetrader/probe.py:222-224, runs row probe.py:214, exit probe.py:225 | probe `--submit`: place_csp returned outcome halt | next-morning | MISSING as a `## HALT:` section; covered as a non-HALT stop at HALT_RUNBOOK.md:591-594 | tests/test_probe.py S3 (tests/test_probe.py:276-286) | - |
| `HALT watchlist_structure` (cc_scan CLI, no colon, no runs row) | forgetrader/cc_scan.py:321-323 | `python -m forgetrader.cc_scan` hits WatchlistStructureError | next-morning (no journal row, hand-run only) | HALT_RUNBOOK.md:598-600 (non-HALT stops) | tests/test_cc_gates.py L4 (tests/test_cc_gates.py:697-700) | - |

The ast extractor also attributes the expiry-guard and assignment f-strings (fs:125-133,
fs:200, `HALT:{stage} ...`) to every stage; in code they are reached only at CSP_OPEN
(right P) and CC_OPEN (right C) (fs:304-305, 362-363, 197-200).

## What tests/test_halt_coverage.py asserts per case (dry and live, 16 runs)

exit 4; exactly one `runs` row `MODE:TOKEN ...`; exactly one HALT decision row with the
exact action; `last_run.outcome` equal to the runs row; state.json equal to the seed apart
from `last_run` and `generated_at` (no money refresh, stage not moved: run.py:205-213 writes
the state reconcile returned unchanged, fs:218-235); one `scans/state_halt_*.txt` dump
(fs:610-625, run.py:338); one alert whose text starts with the outcome; zero submit calls;
one financials read. The harness (tests/test_run_stages.py:156-317) adds its own T2 / T6 / T7
checks.

## Remaining gaps

1. `HALT:no` and `HALT:broker_snapshot` cannot be reached through run.py at base (see their
   rows), so no run-path test exists or can exist with the current fakes; unit tests only.
2. `HALT:probe` has no `## HALT:probe` runbook section; it is documented as a non-HALT stop
   because its runs row is `probe:halt` (no `HALT:` prefix), and tests/test_halt_runbook.py's
   source closure (tests/test_halt_runbook.py:35-36) does not include probe.py.
3. Variant-level gaps inside covered classes (no run-path test for that exact detail):
   `HALT:snapshot` messages other than `unexpected position` and `tracked contract ... qty`;
   `HALT:CSP_PENDING` / `HALT:CC_PENDING` `unknown pending_order_status`,
   `filling but broker shows shares`, `order S but broker shows ...`; `HALT:CSP_OPEN`
   assignment-symbol variants; `HALT:CC_OPEN` partial lot (shares 50, call gone). Each has a
   unit test (columns above).
4. Unreachable detail lines kept in code: `unclassifiable order status` (fs:290-293,
   348-351), `expiry check needs as_of_date` from run.py (run.py:347 always passes a date).
5. "Seen live: none" for every class (HALT_RUNBOOK.md, each section's last line): every row
   above is proven by fakes only.
