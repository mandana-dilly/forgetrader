#!/usr/bin/env bash
# merge.sh BRANCH SHA - ff-only merge of the SHA verify.sh passed, suites AS FORGEDEV via
# deploy/suites.sh against the new main (D22a: no tests/ file runs as forge), refresh
# forgedev's main, then fast-forward forgetrader-b if present. Never pushes:
# git push origin main stays its own line (D8).
# FT_FORGE_REPO / FT_FORGE_B / FT_TODEV / FT_SUDO are for tests only; production never sets them.
set -euo pipefail
B=${1:?usage: merge.sh BRANCH SHA}; S=${2:?usage: merge.sh BRANCH SHA}
[[ $B =~ ^[A-Za-z0-9._-]+$ && $S =~ ^[0-9a-f]{40}$ ]] || { echo "REFUSED: bad args"; exit 2; }
REPO=${FT_FORGE_REPO:-/home/forge/forgetrader}
REPO_B=${FT_FORGE_B:-/home/forge/forgetrader-b}
TODEV=${FT_TODEV:-/srv/forgedrop/to-dev}
SUDO=${FT_SUDO:-sudo -u forgedev -H}
cd "$REPO"
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "REFUSED: not on main"; exit 3; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "REFUSED: tracked changes"; exit 3; }
R=$(git rev-parse "refs/dev/$B"); V=$(cat ".git/verified-$B" 2>/dev/null || echo none)
echo "arg      $S"; echo "refs/dev $R"; echo "verified $V"
[ "$S" = "$R" ] && [ "$S" = "$V" ] || { echo "REFUSED: SHA mismatch"; exit 3; }
git merge -q --ff-only "$S"
H=$(git rev-parse HEAD)
echo "main now $H"
git bundle create -q "$TODEV/main.bundle" main
fail=0
$SUDO bash -s -- "$H" < "$REPO/deploy/suites.sh" || fail=1
git bundle create -q "$TODEV/forgetrader.bundle" HEAD main
$SUDO bash -c '
cd ~/forgetrader || exit 9
if [ "$(git rev-parse --abbrev-ref HEAD)" = main ]; then git pull -q --ff-only "$1/forgetrader.bundle" main
else git fetch -q "$1/forgetrader.bundle" main:main; fi
echo "forgedev main $(git rev-parse main)"' _ "$TODEV"
echo "suites_failed=$fail"; echo "NEXT: health check, then git push origin main"
if [ -d "$REPO_B" ]; then
  git -C "$REPO_B" fetch -q "$REPO" main
  if ! git -C "$REPO_B" merge-base --is-ancestor HEAD FETCH_HEAD; then
    echo "REFUSED: B $REPO_B HEAD $(git -C "$REPO_B" rev-parse HEAD) is not an ancestor of main $H, B left unchanged"; exit 4
  fi
  git -C "$REPO_B" merge -q --ff-only FETCH_HEAD || { echo "REFUSED: B $REPO_B ff-only merge failed"; exit 4; }
  echo "B: HEAD $(git -C "$REPO_B" rev-parse HEAD)"
else
  echo "B: not present"
fi
exit $fail
