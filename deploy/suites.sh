#!/usr/bin/env bash
# suites.sh SHA - every tests/test_*.py AS FORGEDEV in a throwaway clone of main.bundle (D22a).
# Invoked as: sudo -u forgedev -H bash -s -- SHA < deploy/suites.sh
# The script itself arrives on stdin, so every python call reads </dev/null.
# Refuses (exit 2) unless SHA is 40 hex and the clone's HEAD is exactly SHA.
# Never reads anything under forge's home. FT_TODEV / FT_VENV are for tests only:
# production never sets them (and sudo resets the environment). Never run with bash -x.
set -u
SHA=${1:-}
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || { echo "REFUSED: SHA is not 40 hex"; exit 2; }
TODEV=${FT_TODEV:-/srv/forgedrop/to-dev}
VENV=${FT_VENV:-/home/forgedev/forgetrader/.venv}
D=$(mktemp -d) || { echo "REFUSED: mktemp failed"; exit 2; }
trap 'rm -rf "$D"' EXIT
cd "$D" || exit 2
git clone -q -b main "$TODEV/main.bundle" r </dev/null || { echo "REFUSED: clone of $TODEV/main.bundle failed"; exit 2; }
cd r || exit 2
HEAD=$(git rev-parse HEAD)
[ "$HEAD" = "$SHA" ] || { echo "REFUSED: clone HEAD $HEAD is not $SHA"; exit 2; }
ln -s "$VENV" .venv
echo "=== suites SHA $SHA $(date -Is)"
n=0; failed=0
for t in tests/test_*.py; do
  [ -e "$t" ] || continue
  n=$((n+1))
  .venv/bin/python "$t" </dev/null >| "$D/t.out" 2>&1; rc=$?
  echo "$t exit=$rc | $(tail -1 "$D/t.out")"
  [ $rc -eq 0 ] || failed=$((failed+1))
done
if [ $failed -eq 0 ] && [ $n -gt 0 ]; then echo "=== SUITES PASS n=$n"; exit 0; fi
echo "=== SUITES FAIL n=$n failed=$failed"
exit 1
