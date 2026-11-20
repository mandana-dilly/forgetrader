#!/usr/bin/env bash
# rebase.sh BRANCH - as forgedev: rebase a from-dev branch bundle onto the current main.bundle.
# Works in a fresh mktemp clone of main.bundle only (never ~/verify-* or ~/wt/*).
# Refuses unless the rebased diff has the same patch-id as the original and every
# tests/test_*.py passes in the clone (venv linked as verify.sh does). Only then is the old
# bundle kept as BRANCH.pre-rebase.bundle and the new one written and verified.
# Exit: 0 PASS or ALREADY_ON_MAIN, 1 not forgedev, 2 conflict, 3 patch-id mismatch,
# 4 failing suite, 5 setup or bundle error. On any non-PASS exit nothing is written.
# FT_TODEV / FT_FROMDEV / FT_VENV / FT_WHOAMI are for tests only; production never sets them.
# Never run with bash -x.
set -u
B=${1:-}
ME=${FT_WHOAMI:-$(id -un)}
[ "$ME" = forgedev ] || { echo "REFUSED: run as forgedev, not $ME"; exit 1; }
[[ $B =~ ^[A-Za-z0-9._-]+$ ]] || { echo "REFUSED: usage: rebase.sh BRANCH"; exit 5; }
TODEV=${FT_TODEV:-/srv/forgedrop/to-dev}
FROMDEV=${FT_FROMDEV:-/srv/forgedrop/from-dev}
VENV=${FT_VENV:-/home/forgedev/forgetrader/.venv}
BUNDLE=$FROMDEV/$B.bundle
[ -s "$BUNDLE" ] || { echo "REFUSED: $BUNDLE missing"; exit 5; }
D=$(mktemp -d) || { echo "REFUSED: mktemp failed"; exit 5; }
trap 'rm -rf "$D"' EXIT
OUT=$D/report
say() { printf '%s\n' "$*" | tee -a "$OUT"; }
fail() { say "RESULT FAIL $2 new_tip=${NEW_TIP:-none}"; exit "$1"; }
say "=== rebase.sh $B $(date -u +%Y-%m-%dT%H:%M:%SZ)"
NEW_BASE=$(git bundle list-heads "$TODEV/main.bundle" refs/heads/main | cut -d' ' -f1)
[ -n "$NEW_BASE" ] || fail 5 no_main_in_main.bundle
git clone -q -b main "$TODEV/main.bundle" "$D/r" </dev/null || fail 5 clone_failed
cd "$D/r" || fail 5 clone_failed
[ "$(git rev-parse HEAD)" = "$NEW_BASE" ] || fail 5 clone_head_not_new_base
git bundle verify -q "$BUNDLE" >/dev/null 2>&1 || fail 5 bundle_prerequisites_missing
git fetch -q --no-tags "$BUNDLE" "+$B:refs/heads/$B" || fail 5 fetch_failed
OLD_TIP=$(git rev-parse "refs/heads/$B")
OLD_BASE=$(git merge-base "$NEW_BASE" "$OLD_TIP") || fail 5 no_merge_base
say "old_base=$OLD_BASE"
say "new_base=$NEW_BASE"
say "old_tip=$OLD_TIP"
if [ "$OLD_BASE" = "$NEW_BASE" ]; then echo "ALREADY_ON_MAIN"; exit 0; fi
OLD_PID=$(git diff "$OLD_BASE" "$OLD_TIP" | git patch-id --stable | cut -d' ' -f1)
if ! git rebase -q --onto "$NEW_BASE" "$OLD_BASE" "$B" >"$D/rebase.log" 2>&1; then
  git diff --name-only --diff-filter=U | sed 's/^/conflict /' | tee -a "$OUT"
  git rebase --abort >/dev/null 2>&1
  fail 2 conflict
fi
NEW_TIP=$(git rev-parse HEAD)
NEW_PID=$(git diff "$NEW_BASE" "$NEW_TIP" | git patch-id --stable | cut -d' ' -f1)
say "new_tip=$NEW_TIP"
say "old_patch_id=$OLD_PID"
say "new_patch_id=$NEW_PID"
[ -n "$OLD_PID" ] && [ "$OLD_PID" = "$NEW_PID" ] || fail 3 patch_id_mismatch
ln -s "$VENV" .venv
n=0; failed=0
for t in tests/test_*.py; do
  [ -e "$t" ] || continue
  n=$((n+1))
  .venv/bin/python "$t" </dev/null >| "$D/t.out" 2>&1; rc=$?
  say "$t exit=$rc | $(tail -1 "$D/t.out")"
  [ $rc -eq 0 ] || failed=$((failed+1))
done
[ $failed -eq 0 ] && [ $n -gt 0 ] || fail 4 "suites_failed=$failed/$n"
git bundle create -q "$D/new.bundle" "main..$B" || fail 5 bundle_create_failed
git bundle verify -q "$D/new.bundle" >/dev/null 2>&1 || fail 5 bundle_verify_failed
mv -f "$BUNDLE" "$FROMDEV/$B.pre-rebase.bundle" || fail 5 move_failed
cp "$D/new.bundle" "$BUNDLE" || fail 5 bundle_write_failed
git bundle verify -q "$BUNDLE" >/dev/null 2>&1 || fail 5 bundle_verify_failed
say "RESULT PASS rebased new_tip=$NEW_TIP"
cp "$OUT" "$FROMDEV/rebase_$B.out"
