#!/usr/bin/env bash
# verify.sh BRANCH - one-command verification of a forgedev branch bundle (D21).
# Run as forge: bash ~/forgetrader/deploy/verify.sh BRANCH
# Fetches the bundle into refs/dev/BRANCH (data only), then AS FORGEDEV in a throwaway
# worktree at that exact SHA runs all suites and the FI scripts this branch adds or changes,
# then prints log, stat and full diff of main...SHA. Records the SHA only on PASS.
# Never executes branch code as forge. Never run with bash -x.
set -euo pipefail
B=${1:?usage: verify.sh BRANCH}
[[ $B =~ ^[A-Za-z0-9._-]+$ ]] || { echo "REFUSED: bad branch name"; exit 2; }
BUNDLE=/srv/forgedrop/from-dev/$B.bundle
cd /home/forge/forgetrader
git bundle verify -q "$BUNDLE"
git fetch -q --no-tags "$BUNDLE" "+$B:refs/dev/$B"
SHA=$(git rev-parse "refs/dev/$B")
rm -f ".git/verified-$B"
echo "=== branch $B sha $SHA"
echo "=== commits"; git --no-pager log --oneline "main..$SHA"
git merge-base --is-ancestor main "$SHA" && echo "ff-able onto main: yes" || echo "ff-able onto main: NO"
FI=$(git diff --name-only --diff-filter=AM "main...$SHA" -- 'tests/fi/*.sh' | tr '\n' ' ')
echo "=== FI scripts in branch: ${FI:-none}"
echo "=== as forgedev"
set +e
sudo -u forgedev -H bash -c '
set -u
S=$1; shift
cd ~/forgetrader || exit 9
git cat-file -e "$S^{commit}" || { echo "SHA not in forgedev repo"; exit 9; }
git worktree remove --force ~/verify >/dev/null 2>&1; rm -rf ~/verify; git worktree prune
git worktree add -q --detach ~/verify "$S" || exit 9
ln -s ~/forgetrader/.venv ~/verify/.venv
cd ~/verify
echo "worktree HEAD: $(git rev-parse HEAD)"
fail=0
for t in tests/test_*.py; do
  .venv/bin/python "$t" >| /tmp/fd_t.out 2>&1; rc=$?
  echo "$t exit=$rc | $(tail -1 /tmp/fd_t.out)"; [ $rc -eq 0 ] || fail=1
done
for f in "$@"; do
  if grep -nE "(/home/forgedev|~)/forgetrader" "$f"; then echo "FI $f: points at real clone, REFUSED"; fail=1; continue; fi
  echo "--- FI $f"; bash "$f"; rc=$?; echo "FI $f exit=$rc"; [ $rc -eq 0 ] || fail=1
done
echo "tracked files changed after FI: $(git status --short --untracked-files=no | wc -l)"
exit $fail
' _ "$SHA" $FI
RC=$?
set -e
echo "=== diff stat"; git --no-pager diff --stat "main...$SHA"
echo "=== new files"; git --no-pager diff --name-only --diff-filter=A "main...$SHA"
echo "=== full diff"; git --no-pager diff "main...$SHA"
if [ $RC -eq 0 ]; then printf '%s\n' "$SHA" >| ".git/verified-$B"; echo "=== RESULT PASS recorded $SHA"
else echo "=== RESULT FAIL rc=$RC, nothing recorded"; fi
