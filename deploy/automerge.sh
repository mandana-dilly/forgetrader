#!/usr/bin/env bash
# automerge.sh BRANCH - D36: merge a verified SHA without Baxman when it touches only tests/** or docs/**.
# Run as forge: bash ~/forgetrader/deploy/automerge.sh BRANCH
# Refuses (exit 10), changing nothing, unless ALL hold: .git/verified-BRANCH exists and equals
# refs/dev/BRANCH; main is an ancestor of that SHA; git diff --no-renames main...SHA is non-empty
# and EVERY path starts with tests/ or docs/. On pass: merge.sh BRANCH SHA (all merge logic and
# suites live there), then one notify.py ping. Never pushes. Never run with bash -x.
# FT_FORGE_REPO is for tests only; production never sets it.
set -euo pipefail
B=${1:?usage: automerge.sh BRANCH}
refuse() { echo "AUTOMERGE REFUSED $1"; exit 10; }
[[ $B =~ ^[A-Za-z0-9._-]+$ ]] || refuse "bad branch name"
REPO=${FT_FORGE_REPO:-/home/forge/forgetrader}
cd "$REPO"
[ -f ".git/verified-$B" ] || refuse "no .git/verified-$B"
V=$(cat ".git/verified-$B")
R=$(git rev-parse -q --verify "refs/dev/$B^{commit}" || echo none)
echo "refs/dev $R"; echo "verified $V"
[ "$V" = "$R" ] || refuse "verified $V is not refs/dev $R"
[[ $R =~ ^[0-9a-f]{40}$ ]] || refuse "refs/dev/$B is not a SHA"
git merge-base --is-ancestor main "$R" || refuse "main $(git rev-parse main) is not an ancestor of $R"
n=0; bad=0
while IFS= read -r -d '' p; do
  n=$((n+1))
  case $p in
    tests/*|docs/*) ;;
    *) echo "AUTOMERGE REFUSED path=$p"; bad=$((bad+1)) ;;
  esac
done < <(git diff -z --no-renames --name-only "main...$R")
[ $n -gt 0 ] || refuse "empty diff main...$R"
[ $bad -eq 0 ] || exit 10
read -r ins del < <(git diff --no-renames --numstat "main...$R" | awk '{i+=$1; d+=$2} END {print i+0, d+0}')
MSG="[forge] AUTOMERGE sha=${R:0:12} files=$n +$ins/-$del"
echo "automerge: every path under tests/ or docs/ ($n files), calling merge.sh"
set +e
bash "$REPO/deploy/merge.sh" "$B" "$R"
rc=$?
set -e
[ $rc -eq 0 ] || { echo "AUTOMERGE merge.sh exit=$rc, no ping sent"; exit $rc; }
echo "$MSG"
/usr/bin/python3 -I "$REPO/deploy/ops/notify.py" "$MSG" || echo "AUTOMERGE notify failed"
exit 0
