FORGETRADER HANDOVER - 5 Nov 2026 (session 6 start) - supersedes all 4 Nov handovers
HEAD c480285 (pushed). Instructions v6.8 in force. Tree: only ?? canary.secret, ?? deploy/.
9 suites green as forge (gates 420, place_csp 145, probe 154, propose_csp 14,
reconcile_handle 39, run_stages 82, snapshot 22, state_machine 411, watchlist 68).
Wheel FLAT, broker FLAT. Paper account "forge trade dev": $5,000, options enabled,
ceiling $50 strike / $55.56 underlying. Old $1,300 account keys cycled (confirmed).
SOURCE FREEZE LIFTED (D26: probe dropped as a gate).

FIRST MARKET-HOURS RESULT (timer, 5 Nov AEDT = 4 Nov ET)
- 02:30 run: SCAN propose_csp NOK 7.50P exp 2026-12-04 bid 0.16 oi 655 yield 25.9556%
  collateral 750 market_open=True. Yield checks: 0.16/7.50*365/30 (ET date 4 Nov, D13).
- 05:00 run: all_rejected, market_open=True, top: otm_below_target 120, spread_too_wide 104,
  null_oi 76, dte 60, oi_too_low 38. stale_underlying_price absent in market hours.
- Passing is intermittent (quote-dependent). Premium is tiny ($16/contract): NOK proves the
  pipeline, it cannot make money under the 1-contract cap. Money needs higher-priced
  human-stamped names under the $50 ceiling (scout agent, later).

ISOLATION STATE (all proven 4 Nov)
- Claude Code removed from forge. Hermes gateway disabled (forge user unit).
- User forgedev (uid 1002, own group only, locked, home 750). Clone at
  /home/forgedev/forgetrader (c480285, own .venv, pins match). Drops:
  /srv/forgedrop/to-dev (forge writes), /srv/forgedrop/from-dev (forgedev writes).
- keyprobe PASSED against current keys. Known artifact: section 7 lists keyprobe.sh itself
  (literal canary marker). Must fix before commit or forgedev's copy false-FAILs section 8.

NEXT (in order)
1. keyprobe marker fix (build at runtime, e.g. MARK="CANARY-NOT-""A-KEY"), add
   canary.secret to .gitignore, copy the two unit files into deploy/, re-run keyprobe
   (section 7 must list only .env and canary.secret), commit deploy/ + .gitignore, push.
2. Install Claude Code as forgedev (native installer, its own login). Confirm with keyprobe
   that nothing changed. Never install as forge.
3. docs/: v6.8 + this handover into the repo; CLAUDE.md pointing at docs/ and stating the
   forgedev rules (no keys, no broker, branch only, bundle out). Commit, bundle to-dev,
   forgedev pulls.
4. Brief 1 (Tier 2 shakedown): test_watchlist.py crashes in a clean clone with
   FileNotFoundError on state.json (depends on a live file). Fix the test. Exercises the
   full loop end to end.
5. Brief 2 (Tier 1): 13f + 13b.3 combined. scan_watchlist(clock=None) with run.py passing
   its clock (D13; option b violates D13); run.py --live: paper check before any broker
   read (new exit 3, D7 amendment), submit only on noop:FLAT stable + passing proposal +
   run clock is_open; place_csp gets money-refreshed state; HALT:submit persists state_out;
   print + journal broker status; seam test (real gates row into submit._validate);
   dry-run tripwire (fake submit_order raises on every dry scenario); unified live:/dryrun:
   prefix incl. state_missing; dead dry_run param; docstrings + argparse help.
6. First live paper CSP: Baxman runs run.py --live in a market session.
7. Telegram outbound run summary (bot token in forge .env only; extend keyprobe).
8. --set-state fix, then timer --live (Phase X decision). Stage 2 during the CSP's DTE window.
9. End of build: sudo userdel -r forgedev, rm -r /srv/forgedrop. Hermes to its own user.
10. ~1-3 Dec: re-stamp NOK from the Nokia 2027 calendar (BOUND expires).

THE LOOP (D21, per feature)
forge:    git bundle create /srv/forgedrop/to-dev/forgetrader.bundle HEAD main
forgedev: git -C ~/forgetrader pull /srv/forgedrop/to-dev/forgetrader.bundle main
Claude Code (as forgedev): branch <b>, build, suites, FI script,
          git bundle create /srv/forgedrop/from-dev/<b>.bundle main..<b>
Baxman:   suites + FI AS forgedev: sudo -u forgedev -H bash -c '...'  (never sudo -iu)
forge:    git fetch /srv/forgedrop/from-dev/<b>.bundle <b>:refs/dev/<b>
          git --no-pager diff main...refs/dev/<b>   (Architect reads)
          git merge --ff-only <verified SHA>; health check; git push origin main
Never execute forgedev-authored code as forge before the diff is verified.

TIDY BATCH (D25): earnings_lapsed token (test_run_stages); "N failed of M" back-port;
stale docstrings; TICKER_RE fullmatch (decide if money hazard); chmod 600 journal.db
(still 644); /tmp cleanup; PROJECT_STATE.md generated vs hand-maintained.

OPEN DECISIONS: forced-assignment test; 13b.3 action-string design; per-HALT-class recovery
steps; D13/D6 carve-out for pre-clock rows; whether forge keeps docker membership.

LESSONS (5 Nov)
- The first real proposal came from scheduling an existing stage, not from new code.
- A test gate that needs a human awake in a 1-hour window blocks more than it protects.
