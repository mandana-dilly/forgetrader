#!/bin/bash -l
# cc-run.sh BRANCH BASE MODEL - forgedev side of launch.sh. Architect-authored.
# Each branch builds in its own worktree /home/forgedev/wt/BRANCH, so builds run in parallel.
B=$1; BASE=$2; MODEL=$3
L=/home/forgedev/$B.log
BRIEF=/srv/forgedrop/to-dev/brief-$B.md
W=/home/forgedev/wt/$B
cd /home/forgedev/forgetrader || exit 1
{
  echo "start $(date -Is)"
  echo "model $MODEL | $(claude --version)"
  git fetch -q /srv/forgedrop/to-dev/main.bundle "+main:refs/base/$B" || { echo "ABORT: bundle fetch failed"; exit 1; }
  [ "$(git rev-parse "refs/base/$B")" = "$BASE" ] || { echo "ABORT: bundle main is not $BASE"; exit 1; }
  git show-ref -q --verify "refs/heads/$B" && { echo "ABORT: branch $B already exists"; exit 1; }
  [ -e "$W" ] && { echo "ABORT: $W already exists"; exit 1; }
  grep -qx '.venv' .git/info/exclude 2>/dev/null || echo '.venv' >> .git/info/exclude
  mkdir -p /home/forgedev/wt
  git worktree add -q -b "$B" "$W" "$BASE" || { echo "ABORT: worktree add failed"; exit 1; }
  ln -s /home/forgedev/forgetrader/.venv "$W/.venv"
  echo "worktree $W on $(git -C "$W" rev-parse --abbrev-ref HEAD) at $(git -C "$W" rev-parse HEAD)"
} >| "$L" 2>&1
cd "$W" || exit 1
claude -p "$(printf 'Base SHA: %s. Your branch %s is checked out in your working directory %s (a git worktree of your clone). Work only there. Never commit the .venv symlink.\n\n' "$BASE" "$B" "$W"; cat "$BRIEF")" --model "$MODEL" --dangerously-skip-permissions >> "$L" 2>&1
rc=$?
[ -s "/srv/forgedrop/from-dev/$B.bundle" ] && echo "run-script: bundle present" >> "$L" || echo "run-script: BUNDLE MISSING" >> "$L"
echo "claude exit=$rc" >> "$L"
