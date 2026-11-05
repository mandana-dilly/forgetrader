#!/usr/bin/env bash
# merge.sh BRANCH SHA - ff-only merge of the SHA verify.sh passed, suites as forge,
# then refresh forgedev's main. Never pushes: git push origin main stays its own line (D8).
set -euo pipefail
B=${1:?usage: merge.sh BRANCH SHA}; S=${2:?usage: merge.sh BRANCH SHA}
[[ $B =~ ^[A-Za-z0-9._-]+$ && $S =~ ^[0-9a-f]{40}$ ]] || { echo "REFUSED: bad args"; exit 2; }
cd /home/forge/forgetrader
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "REFUSED: not on main"; exit 3; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "REFUSED: tracked changes"; exit 3; }
R=$(git rev-parse "refs/dev/$B"); V=$(cat ".git/verified-$B" 2>/dev/null || echo none)
echo "arg      $S"; echo "refs/dev $R"; echo "verified $V"
[ "$S" = "$R" ] && [ "$S" = "$V" ] || { echo "REFUSED: SHA mismatch"; exit 3; }
git merge -q --ff-only "$S"
echo "main now $(git rev-parse HEAD)"
fail=0
for t in tests/test_*.py; do
  rc=0; .venv/bin/python "$t" >| /tmp/t.out 2>&1 || rc=$?
  echo "$t exit=$rc | $(tail -1 /tmp/t.out)"; [ $rc -eq 0 ] || fail=1
done
git bundle create -q /srv/forgedrop/to-dev/forgetrader.bundle HEAD main
sudo -u forgedev -H bash -c '
cd ~/forgetrader || exit 9
if [ "$(git rev-parse --abbrev-ref HEAD)" = main ]; then git pull -q --ff-only /srv/forgedrop/to-dev/forgetrader.bundle main
else git fetch -q /srv/forgedrop/to-dev/forgetrader.bundle main:main; fi
echo "forgedev main $(git rev-parse main)"'
echo "suites_failed=$fail"; echo "NEXT: health check, then git push origin main"
exit $fail
