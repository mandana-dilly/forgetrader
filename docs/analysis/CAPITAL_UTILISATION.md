# Capital utilisation at $5,000 per name

Brief wheel-prep-1125 T5. Analysis only: no code, policy or watchlist change. Every repo
file:line is at base c20f6522225ee39308ffbd587560baec44be7012.

## The code's numbers

- **Strike ceiling** (screener.py:136-147, called by the scan at wheel_dryrun.py:260-262):
  `available = cash x (1 - reserve_pct)`, `max_strike = floor(available / 100)`,
  `max_underlying = max_strike / (1 - otm_target_pct / 100)`. A: reserve_pct 0.0
  (policy.toml:2), otm_target_pct 10 (policy.toml:5). At cash $5,000: available 5,000,
  max_strike 50, max_underlying 55.5556. A name priced above max_underlying is rejected
  before its chain is read (wheel_dryrun.py:337-343).
- **10% OTM** is a floor, not a target: a put passes when
  `(spot - strike) / spot x 100 >= otm_target_pct` (gates.py:40-45), so the deepest
  eligible strike is the largest listed strike at or below 0.9 x spot.
- **Collateral** = strike x 100, gated `<= available` (gates.py:113-116).
- **One contract, one position:** `CSP_QTY = 1` (submit.py:26); the Stage 1 scan runs only
  at exactly `noop:FLAT stable` (run.py:363), so at most one wheel position exists.
  `[risk] max_positions = 1` (policy.toml:29) is not read by any code (findings F10).
- **Ranking:** every passing row across all names sorts by open_interest desc, yield
  tiebreak (wheel_dryrun.py:145-152, 451-452); run.py proposes `passing_rows[0]`
  (run.py:378-379).

## Table (approved names, 1-contract cap, $5,000 per name)

Approved names: the seven entries of watchlist.json (NOK, BAC, CMCSA, HAL, PCG, PFE, T;
watchlist.json:14, 19, 28, 37, 46, 55, 64 are their `own_100_note` lines). Strike =
largest $0.50 strike at or below 0.9 x price; the $0.50 grid is an assumption (strike
spacing per name is UNVERIFIED). Utilisation = collateral / 5,000.

| Name | Price UNVERIFIED | Source of price | 0.9 x price | Strike at 10% OTM | Collateral (cap 1) | Utilisation (cap 1) | Contracts if the cap were lifted | Utilisation then |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BAC | 49.76 | 5 Nov close, aggregator (A) | 44.78 | 44.50 | 4,450 | 89.0% | 1 | 89.0% |
| HAL | 31.92 | 5 Nov close, aggregator (A) | 28.73 | 28.50 | 2,850 | 57.0% | 1 | 57.0% |
| PFE | 29.88 | 5 Nov close, aggregator (A) | 26.89 | 26.50 | 2,650 | 53.0% | 1 | 53.0% |
| CMCSA | 24.95 | 5 Nov close, aggregator (A) | 22.46 | 22.00 | 2,200 | 44.0% | 2 | 88.0% |
| T | 21.82 | 5 Nov close, aggregator (A) | 19.64 | 19.50 | 1,950 | 39.0% | 2 | 78.0% |
| PCG | 13.23 | 5 Nov close, aggregator (A) | 11.91 | 11.50 | 1,150 | 23.0% | 4 | 92.0% |
| NOK | 8.34 | lower bound (B) | 7.51 | 7.50 | 750 | 15.0% | 6 | 90.0% |

(A) /srv/forgedrop/from-dev/watchlist_proposals.md lines 64, 126, 80, 141, 156, 95 (outside
the repo, stockanalysis.com / finance.yahoo.com closes of 2026-11-05, three weeks old).
(B) not in that file; docs/HANDOVER.md:10 records a passing NOK 7.50P at 10% OTM on 4 Nov,
which needs spot >= 7.50 / 0.9 = 8.33. Every name is below max_underlying 55.56.

Read with the same file's caveat (watchlist_proposals.md:56): on the 5 Nov quotes, low-IV
large caps at 10% OTM bid 0.04 to 0.16 and mostly fail `bid_too_low` / `spread_*`; which
name passes on a given run is quote-dependent. The table is what each name would use IF it
passed, not what the scan will pick.

## Three tie-break options

### 1. Highest collateral use that passes all gates

- **Change:** the sort key at wheel_dryrun.py:451-452 becomes collateral desc first (the
  row already carries `collateral`, gates.py:155), OI and yield after it; run.py:379's
  "already OI-desc sorted" comment and the ordering tests that pin OI-first ranking would
  change. No state, submit or reconcile change.
- **Consequence:** when BAC passes it wins (89%); otherwise HAL, PFE, CMCSA, T, PCG, NOK in
  that order. Capital is deployed hardest on the most expensive name, so a single
  assignment ties up most of the account in one stock (100 shares x price) and the next CSP
  waits for that cycle. Liquidity becomes a pass/fail floor only (OI >= 100), no longer a
  preference.

### 2. Multi-position up to N

- **Change (Tier 1, large):** the state holds ONE wheel (forgetrader_state.py:454-464) and
  reconcile walks one stage (forgetrader_state.py:247-372); build_snapshot raises on any
  position that is not the wheel's ticker or contract (forgetrader_state.py:686-687) and on
  any untracked open order (forgetrader_state.py:703-712); the scan only runs at FLAT
  (run.py:363); `CSP_QTY = 1` is hardcoded (submit.py:26). N positions means a list of
  wheels in state, per-wheel snapshot / reconcile / journal, collateral reserved across open
  positions in the ceiling (screener.py:139-142 reads cash only), and new HALT classes and
  runbook sections (docs/HALT_RUNBOOK.md, tests/test_halt_runbook.py).
- **Consequence:** utilisation could approach the "cap lifted" column (78-92% for CMCSA, T,
  PCG, NOK) or combine names; D19's "one position, every position covered" premise
  (docs/INSTRUCTIONS.md:47) and funding criterion 1's single cycle
  (docs/INSTRUCTIONS.md:271) would need restating. Lifting the 1-contract cap on one name is
  a different change again (submit.py:26, docs/INSTRUCTIONS.md:154).

### 3. Status quo: open-interest sort

- **Change:** none.
- **Consequence:** the most liquid passing contract wins regardless of size: a passing NOK
  7.50P (OI 655 on 4 Nov, docs/HANDOVER.md:10) beats a passing BAC 44.50P with less OI,
  using 15% of the account instead of 89%. Fills are the most reliable; idle cash is the
  cost. Funding criterion 6 (docs/INSTRUCTIONS.md:276) asks for names under the new ceiling
  before money moves: with OI-first ranking, cheap liquid names can keep most of the
  capital idle even when such names exist.
