# ITM covered calls under D28 S1

Brief wheel-prep-1125 T3 (open decision, due before A's 11 Dec expiry). Analysis only: no
policy or code change. Every file:line is at base 4b04c86169e59efe29e1bb4a6bcca8a856c34617 (written at c20f652, re-verified).
The D28 and D30 decision texts are not in the repo (`git grep -n "D28\|D30" c20f652` finds
only run.py:32, "D28 S4"); "S1" below means the strike rule as the code implements it.

## What S1 does today

- **Strike floor, basis only.** `gates.evaluate_call` passes a call's strike when
  `strike >= cost_basis x (1 + otm_above_basis_pct / 100)`, compared in Decimal
  (gates.py:214-223). The percentage is `[calls] otm_above_basis_pct`: 10 on A
  (policy.toml:32-33), 2 on B (policy-b.toml:33-34). The brief says `[cc]` keys; the table is
  `[calls]` (findings F4). cc_scan passes it in at cc_scan.py:199.
- **No spot-relative gate.** The underlying price is used for staleness only
  (gates.py:202-207) and echoed on the row (gates.py:303). Nothing compares the strike with
  spot, so an in-the-money call passes whenever its strike clears the basis floor.
  tests/test_itm_cc.py G1 pins this: the same 8.00 call passes at spots 5.00, 10.50 and 50.00.
- **Yield counts intrinsic value.** `annualized_yield_pct = (bid*100)/(cost_basis*100) x
  365/dte x 100` (gates.py:282-286). A deep ITM bid is mostly intrinsic, so its "yield" is
  the largest in the chain and the 7% yield floor (policy.toml:15) never binds on it.
- **Ranking.** Candidates sort by open_interest descending, yield as tiebreak
  (cc_scan.py:216); run.py proposes `candidates[0]` (run.py:465) and place_cc rechecks only
  the basis floor (submit.py:177-183), never spot.
- **Who runs it.** Only on exactly `noop:HOLDING stable` (run.py:435), so the first CC scan
  after an assignment is the run after the HOLDING transition (run.py:432-434).

## Worked outcomes (tests/test_itm_cc.py, pinned at base)

Fixture: basis 7.00, A's policy (floor 7.70), run 14 Dec 2026 10:30 ET, one expiry Fri
8 Jan 2027 (dte 25), strikes 6.00-12.00 step 0.50, bid = intrinsic + time value
`max(0.20, 0.45 - 0.08 |K - spot|)`, ask = bid + 0.05, two OI shapes (FLAT 500 everywhere;
PEAKED, most OI nearest the money). Every strike from 8.00 up passes every gate (9
candidates); 6.00-7.50 fail `strike_below_basis_floor` only. The bid model is synthetic: the
selection logic is the code's, the premiums are the fixture's.

P&L per contract if called away at expiry = 100 x (strike - basis + bid). Break-even at
expiry = basis - bid. Days to next FLAT: the call expires Fri 8 Jan; called_away is read by
the first run after the broker removes the shares (fs:359-360), earliest the A timer's
10:30 ET run Mon 11 Jan 2027 (deploy/forgetrader-run.timer:5), 28 days after the scan, if
the assignment has posted by then. An OTM call that expires with spot below the strike goes
back to HOLDING (`cc_expired_worthless`, fs:361-366): no FLAT that cycle.

| Spot | OI shape | Selected (run.py:465) | Strike vs spot | Premium (bid x 100) | Code yield % | P&L if called | Break-even | Next FLAT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 7.00 | FLAT | NOK270108C00008000 | 8.00 OTM by 1.00 | 37 | 77.1714 | 137 | 6.63 | 28 d only if spot >= 8.00 on 8 Jan |
| 7.00 | PEAKED | NOK270108C00008000 | 8.00 OTM by 1.00 | 37 | 77.1714 | 137 | 6.63 | as above |
| 7.35 | FLAT | NOK270108C00008000 | 8.00 OTM by 0.65 | 40 | 83.4286 | 140 | 6.60 | 28 d only if spot >= 8.00 |
| 7.35 | PEAKED | NOK270108C00008000 | 8.00 OTM by 0.65 | 40 | 83.4286 | 140 | 6.60 | as above |
| 8.40 | FLAT | NOK270108C00008000 | 8.00 **ITM by 0.40** | 82 | 171.0286 | 182 | 6.18 | 28 d unless spot falls below 8.00 |
| 8.40 | PEAKED | NOK270108C00008500 | 8.50 OTM by 0.10 | 44 | 91.7714 | 194 | 6.56 | 28 d only if spot >= 8.50 |
| 10.50 | FLAT | NOK270108C00008000 | 8.00 **ITM by 2.50** | 275 | 573.5714 | 375 | 4.25 | 28 d, near certain |
| 10.50 | PEAKED | NOK270108C00010500 | 10.50 ATM | 45 | 93.8571 | 395 | 6.55 | 28 d only if spot >= 10.50 |

Reading it: with OI flat across strikes the yield tiebreak picks the lowest eligible strike,
which is ITM as soon as spot is above the floor (8.40, 10.50). With OI peaked near the money
(the usual real shape) OI decides and the pick is near the money. Which one B or A gets on a
real chain depends on the chain's OI, not on any rule about moneyness. At spot 10.50 the ITM
pick locks in 375 with a 4.25 break-even; the ATM pick would make 395 if called but only
cushions to 6.55.

## Early-assignment exposure (D19, D14; D30 not in the repo)

- An ITM short call can be assigned before expiry (American style). The engine treats it as
  `CC_OPEN->FLAT (called_away)`, which has no expiry guard by design (fs:359-360; D14,
  docs/INSTRUCTIONS.md:42 "assigned / called_away unguarded (early assignment is legal)").
  No HALT; the next run after that scans for a CSP (run.py:363).
- D19's premise (docs/INSTRUCTIONS.md:47: every position cash-secured or covered) holds: the
  call is covered by the 100 shares, so early assignment delivers shares the account owns.
  The cap is one contract (submit.py:145), so there is no partial lot.
- The one HALT path: the broker shows the shares gone while the short call still shows open
  (assignment posted to the stock before the option) -> `HALT:CC_OPEN call still open but
  broker shows shares=0` (fs:357), a D19a act-before-open class
  (docs/HALT_RUNBOOK.md:72-73), but here a timing artefact. Unproven on paper:
  "Paper assignment happens only at expiry" (docs/INSTRUCTIONS.md:244), so paper never
  exercises early assignment at all. Real money would.
- Exposure grows with depth ITM: the 8.00 call at spot 10.50 (time value 0.25 in the fixture)
  is the likeliest early assignment of the table.

## Options

### 1. Keep S1 (Architect's current recommendation)

- **Code:** none.
- **Consequences:** behaviour as the table. ITM calls are sold whenever spot is above the
  basis floor and OI does not favour a nearer strike. Upside above the strike is given up;
  premium is mostly intrinsic, which raises the downside cushion and makes called_away near
  certain, so the cycle reaches FLAT fastest (funding criterion 1 wants one full cycle,
  docs/INSTRUCTIONS.md:271). The yield number on journal lines and
  Telegram overstates income for ITM picks (intrinsic counted, gates.py:283). Early
  assignment risk is highest here (live money only; paper does not model it).

### 2. Floor = max(basis x 1.02, spot)

- **Code:** gates.py:218-223 (floor needs `underlying_price`, already an argument of
  evaluate_call, gates.py:175-176); the recheck in place_cc (submit.py:177-183) has no spot
  today, so it would read the row's `underlying_price` (gates.py:303) or keep checking the
  basis only; tests/test_cc_gates.py fixtures at the floor and tests/test_itm_cc.py would be
  re-pinned. Policy: 1.02 is B's percentage (policy-b.toml:34); on A it lowers the basis part
  of the floor from 10% (7.70) to 2% (7.14) unless it is read as
  `max(basis x (1 + pct/100), spot)`.
- **Consequences on the fixture** (same bid model, computed by hand from the test's
  `bid_for`, not by code): floor 7.14 / 7.35 / 8.40 / 10.50 for the four spots; the lowest
  eligible strike is 7.50 / 7.50 / 8.50 / 10.50 (PEAKED picks the same), premium 41 / 44 / 44
  / 45, P&L if called 91 / 94 / 194 / 395. Never ITM at selection; at low spots A would sell
  a NEARER strike than today (7.50 vs 8.00) because 2% is below A's 10%. A stale spot (up to
  60 minutes, policy.toml:23) could still put an ITM strike through. Called away is no longer
  near certain, so time to FLAT gets longer and less predictable.

### 3. Skip the CC when spot > basis x (1 + X)

- **Code:** a ticker-level rejection before or after the chain is read, e.g. in
  cc_scan.scan_calls next to `no_snapshot` / `null_price` (cc_scan.py:149-153), reusing the
  `cc_rejected:` path in run.py:457-463 (decision row, stderr WARNING, alert text, exit 0,
  no HALT); a new policy key for X under `[calls]`; tests for the new token.
- **Consequences:** while spot is high the wheel sits at HOLDING with uncovered-upside shares
  and no premium; each run logs `cc_rejected:...` and nothing else. With X = 10 the spots 8.40
  (+20%) and 10.50 (+50%) skip, 7.00 and 7.35 sell the 8.00 call as today. The wheel only
  resumes if spot falls back under the threshold, so a stock that keeps rising never reaches
  FLAT through called_away (funding criterion 1 stalls). D19 is unaffected (long stock, no
  short option). This option does not stop ITM selection when spot sits between the floor and
  the threshold (spot 8.40 with X = 25 still sells the ITM 8.00 under FLAT OI).
