#!/usr/bin/env bash
# keyprobe.sh [TARGET_USER] - proves TARGET_USER (default forgedev) cannot read or find the
# Alpaca keys and Telegram token of account A and, when /home/forge/forgetrader-b/.env exists,
# of account B (D23).
# Run as forge: cd / && sudo -v && bash ~/forgetrader/deploy/keyprobe.sh [TARGET_USER]
# Refuses (exit 2) root, forge and any user in the sudo or docker group.
# Key values live only in two 600 pattern files in a 700 temp dir, written by python and read
# by grep (-f FILE, or stdin redirected from the file). Never in a shell variable, an argument
# or the output: only counts, verdicts and filenames are printed.
# One PASS/FAIL line per check, last line KEYPROBE target=U result=PASS|FAIL (exit 0|1).
# FT_FORGE_HOME / FT_PY / FT_DECOY / FT_SEARCH_ROOT are for tests only; production never sets them.
# NEVER run with bash -x.
set -u
umask 077
T=${1:-forgedev}
refuse() { echo "REFUSED: $1"; echo "KEYPROBE target=$T result=FAIL"; exit 2; }
[ $# -le 1 ] || { T=invalid; refuse "usage: keyprobe.sh [TARGET_USER]"; }
[[ $T =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || { T=invalid; refuse "target is not a valid user name"; }
[ "$T" != root ] && [ "$T" != forge ] || refuse "target $T is never probed"
PW=$(getent passwd "$T") || refuse "no user $T"
TUID=$(echo "$PW" | cut -d: -f3)
TH=$(echo "$PW" | cut -d: -f6)
[ "$TUID" != 0 ] || refuse "$T has uid 0"
TG=$(id -nG "$T") || refuse "cannot list groups of $T"
for g in $TG; do
  case $g in sudo|docker) refuse "$T is in group $g";; esac
done
FH=${FT_FORGE_HOME:-/home/forge}
A=$FH/forgetrader
B=$FH/forgetrader-b
PY=${FT_PY:-$A/.venv/bin/python}
DECOY=${FT_DECOY:-/srv/forgedrop/to-dev/decoy.txt}
HDECOY=$TH/keyprobe-decoy.txt
SROOT=${FT_SEARCH_ROOT:-/}
AS=(sudo -u "$T" -H)
# Marker built at runtime so this file never contains it literally (else section 8 false-FAILs on clones).
MARK="CANARY-NOT-""A-KEY"
W=$(mktemp -d) || { echo "FAIL mktemp"; echo "KEYPROBE target=$T result=FAIL"; exit 1; }
trap 'rm -rf "$W"' EXIT
KEYS=$W/keys
ALL=$W/all
fails=0
res() { if [ "$1" -eq 0 ]; then echo "PASS $2"; else echo "FAIL $2"; fails=$((fails+1)); fi; }
finish() { if [ $fails -eq 0 ]; then echo "KEYPROBE target=$T result=PASS"; exit 0; fi
  echo "KEYPROBE target=$T result=FAIL"; exit 1; }
# Appends the 3 key values of env file $1 to $KEYS; prints only how many were usable (>= 16 chars).
extract() { "$PY" -c '
import sys
from dotenv import dotenv_values
v = dotenv_values(sys.argv[1])
vals = [v.get(k) or "" for k in ("ALPACA_API_KEY", "ALPACA_API_SECRET", "TELEGRAM_BOT_TOKEN")]
good = [x for x in vals if len(x) >= 16 and "\n" not in x]
with open(sys.argv[2], "a") as f:
    f.write("".join(x + "\n" for x in good))
print(len(good))
' "$1" "$KEYS" 2>/dev/null; }

echo "--- keyprobe target=$T home=$TH groups=$TG search_root=$SROOT"
LABELS=(A); DIRS=("$A")
if [ -f "$B/.env" ]; then LABELS+=(B); DIRS+=("$B")
else echo "account B: $B/.env absent, B not covered"; fi
for i in "${!LABELS[@]}"; do echo "coverage: ${LABELS[$i]} ${DIRS[$i]}/.env"; done

echo "--- 0 key patterns (counts only, expect 3 per account)"
: >| "$KEYS"
for i in "${!LABELS[@]}"; do
  c=$(extract "${DIRS[$i]}/.env")
  [ "$c" = 3 ]; res $? "patterns ${LABELS[$i]}: ${c:-0} of 3 keys loaded"
done
[ $fails -eq 0 ] || { echo "ABORT: key patterns incomplete, nothing probed"; finish; }
cp "$KEYS" "$ALL"
printf '%s\n' "$MARK" >> "$ALL"

echo "--- 1 canary beside each .env (same owner and mode), decoys"
for i in "${!LABELS[@]}"; do
  L=${LABELS[$i]}; E=${DIRS[$i]}/.env; C=${DIRS[$i]}/canary.secret
  [ -f "$C" ] || { printf '%s\n' "$MARK" >| "$C"; chmod 600 "$C"; }
  stat -c '%a %U %n' "$E" "$C"
  [ "$(stat -c '%a %U' "$E")" = "$(stat -c '%a %U' "$C")" ]; res $? "canary $L: same owner and mode as .env"
  ! "${AS[@]}" cat "$C" >/dev/null 2>&1; res $? "canary $L: read by $T BLOCKED"
done
[ -f "$DECOY" ] || { printf '%s\n' "$MARK" >| "$DECOY"; chmod 644 "$DECOY"; }
"${AS[@]}" test -f "$HDECOY" || printf '%s\n' "$MARK" | "${AS[@]}" tee "$HDECOY" >/dev/null
EXPECT=()
for d in "$DECOY" "$HDECOY"; do "${AS[@]}" test -r "$d" && EXPECT+=("$d"); done
echo "decoys readable by $T: ${EXPECT[*]:-none}"
[ ${#EXPECT[@]} -ge 1 ]; res $? "decoy control: $T reads ${#EXPECT[@]} decoy(s)"

echo "--- 2 .env"
for i in "${!LABELS[@]}"; do
  ! "${AS[@]}" test -r "${DIRS[$i]}/.env"; res $? ".env ${LABELS[$i]}: read by $T BLOCKED"
done

echo "--- 3 forge process environments"
n=0; r=0
for p in $(pgrep -u forge); do n=$((n+1)); "${AS[@]}" test -r "/proc/$p/environ" && r=$((r+1)); done
[ $r -eq 0 ]; res $? "forge processes: $n, environ readable by $T: $r"

echo "--- 4 $T environment (plain and login shell)"
envs() { "${AS[@]}" env </dev/null 2>/dev/null; "${AS[@]}" bash -lc env </dev/null 2>/dev/null; }
nn=$(envs | grep -cE '^(ALPACA_|TELEGRAM_)')
nv=$(envs | grep -cF -f "$KEYS")
[ "$nn" -eq 0 ] && [ "$nv" -eq 0 ]; res $? "env of $T: ALPACA_/TELEGRAM_ names $nn, key values $nv"

echo "--- 5 privilege"
! "${AS[@]}" sudo -n true >/dev/null 2>&1; res $? "sudo for $T BLOCKED"
! "${AS[@]}" test -w /var/run/docker.sock; res $? "docker socket for $T BLOCKED"
[[ " $TG " != *" forge "* ]]; res $? "$T not in group forge"

echo "--- 6 git history (expect 0)"
for i in "${!LABELS[@]}"; do
  R=${DIRS[$i]}
  if git -C "$R" rev-parse --git-dir >/dev/null 2>&1; then
    c=$(git -C "$R" log -p --all 2>/dev/null | grep -cF -f "$KEYS")
    [ "$c" -eq 0 ]; res $? "git history ${LABELS[$i]}: $c key strings"
  else res 1 "git history ${LABELS[$i]}: $R is not a git repo"; fi
done

echo "--- 7 positive control: forge searches its own trees (expect each .env and canary.secret)"
for i in "${!LABELS[@]}"; do
  D=${DIRS[$i]}
  grep -rlF -f "$ALL" "$D" --exclude-dir=.venv --exclude-dir=.git 2>/dev/null >| "$W/pc"
  sed 's/^/  /' "$W/pc"
  grep -qxF "$D/.env" "$W/pc" && grep -qxF "$D/canary.secret" "$W/pc"
  res $? "positive control ${LABELS[$i]}: .env and canary.secret found"
done

echo "--- 8 $T searches $SROOT (expect ONLY the decoys it can read)"
"${AS[@]}" grep -rlF -f - "$SROOT" --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev \
  < "$ALL" 2>/dev/null | sed 's#//*#/#g' | sort >| "$W/hits"
sed 's/^/  /' "$W/hits"
for d in "${EXPECT[@]}"; do echo "$d"; done | sed 's#//*#/#g' | sort >| "$W/want"
cmp -s "$W/hits" "$W/want"; res $? "filesystem search as $T: $(grep -c . "$W/hits") file(s), only decoys"
finish
