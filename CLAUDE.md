# CLAUDE.md - rules for Claude Code in this repo

You run as Unix user forgedev in /home/forgedev/forgetrader, a dev clone.
Before any work read docs/INSTRUCTIONS.md (spec, banked decisions) and docs/HANDOVER.md
(latest status snapshot, may be stale). Where either disagrees with the code, the code is
what exists; report the disagreement, do not "fix" the doc.

## Hard rules
- No keys, no broker. This clone has no .env, state.json or journal.db. Never create them,
  never look for keys, never call Alpaca (no load_credentials, no read_broker_financials,
  no run.py or probe.py without a fake client). Baxman runs anything that touches Alpaca.
- Never write state.json, journal.db, watchlist.json, policy.toml or .env, anywhere.
- Work only on the branch named in the brief. Never commit to main. Never push.
  Branch commits are drafts: only Baxman's --ff-only merge of a verified SHA reaches main.
- Finish by bundling: git bundle create /srv/forgedrop/from-dev/BRANCH.bundle main..BRANCH
- Never bash -x. Never git checkout / stash / reset with uncommitted work in the tree.
- Commit messages contain no redirect-shaped tokens (less-than, greater-than, dash-arrow).
- A test that needs a live file (state.json etc.) is a finding: report it, never create the file.

## Recon
Read every signature a brief depends on (nl -ba) before writing code. Never restate from
memory or from the docs. Undocumented API values are a HALT or are not sent.

## Tests
All suites: for t in tests/test_*.py; do .venv/bin/python "$t"; echo "$t exit=$?"; done
Each ends PASS: ... or FAIL: N failed of M assertions. A traceback is a crash, not a bite.
Tier 1 fault injection: pristine copy of the source to /tmp, sed -i ONE mutation, print
mutated_lines (expect 1), run all suites, cp restore from pristine, sha256sum -c.
Expected failure counts are pre-registered in the brief; a mismatch is a finding.

## Reporting
Your summaries are not evidence; Baxman verifies from his own terminal. Report only:
branch, SHA, files changed, and the exact verification commands. Never claim a result
you did not print.
