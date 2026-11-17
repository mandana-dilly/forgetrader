#!/usr/bin/env bash
# launch.sh BRANCH        as forge: launch Claude Code on brief-BRANCH.md from forge's main,
#                         then arm a waiter that runs verify.sh and pings Telegram with the result.
# launch.sh --wait BRANCH the waiter itself (tmux session w-BRANCH). Architect-authored. Never bash -x.
#                         After a verify RESULT PASS it runs automerge.sh (D36). The waiter never pushes.
# FT_TODEV / FT_FORGE_REPO / FT_SUDO are for tests only; production never sets them.
set -euo pipefail
TODEV=${FT_TODEV:-/srv/forgedrop/to-dev}
REPO=${FT_FORGE_REPO:-/home/forge/forgetrader}
SUDO=${FT_SUDO:-sudo -u forgedev -H}
MODEL=${MODEL:-claude-opus-5-5}
if [ "${1:-}" = "--wait" ]; then
  B=${2:?usage: launch.sh --wait BRANCH}
  while $SUDO tmux has-session -t "$B" 2>/dev/null; do sleep 60; done
  set +e
  bash "$REPO/deploy/verify.sh" "$B" >| "/tmp/verify_$B.out" 2>&1
  echo "verify exit=$?" >> "/tmp/verify_$B.out"
  R=$(grep -o '=== RESULT [A-Z]*' "/tmp/verify_$B.out" | tail -1)
  MSG="${R:-verify did not finish} - paste: cat /tmp/verify_$B.out"
  if [ "$R" = "=== RESULT PASS" ]; then
    bash "$REPO/deploy/automerge.sh" "$B" >| "/tmp/automerge_$B.out" 2>&1
    A=$?
    echo "automerge exit=$A" >> "/tmp/automerge_$B.out"
    if [ $A -eq 0 ]; then MSG="RESULT PASS, AUTOMERGED $(cut -c1-12 "$REPO/.git/verified-$B")"
    else MSG="RESULT PASS, manual merge ($(grep -m1 'REFUSED' "/tmp/automerge_$B.out" || echo "automerge exit=$A"))"; fi
    MSG="$MSG - paste: cat /tmp/verify_$B.out /tmp/automerge_$B.out"
  fi
  /usr/bin/python3 -I "$TODEV/ops/notify.py" "[forgetrader] BUILD $B: $MSG"
  exit 0
fi
B=${1:?usage: launch.sh BRANCH}
[[ $B =~ ^[A-Za-z0-9._-]+$ ]] || { echo "REFUSED: bad branch name"; exit 2; }
BRIEF=$TODEV/brief-$B.md
[ -s "$BRIEF" ] || { echo "REFUSED: $BRIEF missing"; exit 2; }
grep -q "^Branch: $B\$" "$BRIEF" || { echo "REFUSED: brief does not say Branch: $B"; exit 2; }
cd "$REPO"
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "REFUSED: forge tree dirty"; exit 2; }
sudo -u forgedev -H tmux has-session -t "$B" 2>/dev/null && { echo "REFUSED: session $B already running"; exit 2; }
BASE=$(git rev-parse main)
git bundle create "$TODEV/main.bundle" main 2>/dev/null
[ "$(git bundle list-heads "$TODEV/main.bundle" refs/heads/main | cut -d' ' -f1)" = "$BASE" ] || { echo "REFUSED: main.bundle is not $BASE"; exit 2; }
sudo -iu forgedev tmux new-session -d -s "$B" "bash -l $TODEV/ops/cc-run.sh $B $BASE $MODEL"
tmux new-session -d -s "w-$B" "bash $TODEV/ops/launch.sh --wait $B"
echo "LAUNCHED $B base $BASE model $MODEL. Telegram pings when verify finishes."
