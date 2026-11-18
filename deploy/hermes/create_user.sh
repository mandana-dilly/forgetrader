#!/usr/bin/env bash
# create_user.sh [--apply] - creates Unix user hermes per D43: own group only (never sudo,
# docker or forge), password locked, home 750, umask 027 in its profile, output dir
# /srv/hermes-out owned hermes, group forge, mode 2750 (hermes writes, forge reads).
# Default is a dry run: checks (read-only, passwd -S needs sudo) and prints every command.
# --apply executes them. Run as forge: cd / && sudo -v && bash ~/forgetrader/deploy/hermes/create_user.sh
# Idempotent: an existing correct hermes is reported; an existing wrong one is a FAIL with the
# reason and is never fixed here. Never creates or reads any API key. NEVER run with bash -x.
set -u
U=hermes
H=/home/hermes
OUT=/srv/hermes-out
PROFILE_LINE="umask 027"
APPLY=0
case "${1:-}" in
  "") ;;
  --apply) APPLY=1 ;;
  *) echo "usage: create_user.sh [--apply]"; exit 2 ;;
esac
[ $# -le 1 ] || { echo "usage: create_user.sh [--apply]"; exit 2; }
MODE="DRY RUN"; [ $APPLY -eq 1 ] && MODE=APPLY
echo "create_user.sh: user $U (D43), mode $MODE"
next() {
  echo "NEXT: Baxman, as forge: sudo -u $U -i, put the LLM key in $U's own environment with an"
  echo "      editor (never on a command line, never in forge's files), exit, then:"
  echo "      cd / && sudo -v && bash ~/forgetrader/deploy/keyprobe.sh $U"
}
# Prints one FAIL line per deviation from D43; returns the count.
verify() {
  local bad=0 pw g st h
  pw=$(getent passwd "$U")
  h=$(echo "$pw" | cut -d: -f6)
  [ "$(echo "$pw" | cut -d: -f3)" != 0 ] || { echo "FAIL $U has uid 0"; bad=$((bad+1)); }
  [ "$h" = "$H" ] || { echo "FAIL home is '$h', expected $H"; bad=$((bad+1)); }
  g=$(id -nG "$U" 2>/dev/null)
  [ "$g" = "$U" ] || { echo "FAIL groups are '$g', expected only $U"; bad=$((bad+1)); }
  st=$(sudo passwd -S "$U" 2>/dev/null | cut -d' ' -f2)
  [ "$st" = L ] || { echo "FAIL password status '$st', expected L (locked)"; bad=$((bad+1)); }
  st=$(stat -c '%a %U %G' "$H" 2>/dev/null)
  [ "$st" = "750 $U $U" ] || { echo "FAIL $H is '$st', expected '750 $U $U'"; bad=$((bad+1)); }
  sudo -u "$U" grep -qxF "$PROFILE_LINE" "$H/.profile" 2>/dev/null \
    || { echo "FAIL no '$PROFILE_LINE' line in $H/.profile"; bad=$((bad+1)); }
  st=$(stat -c '%a %U %G' "$OUT" 2>/dev/null)
  [ "$st" = "2750 $U forge" ] || { echo "FAIL $OUT is '$st', expected '2750 $U forge'"; bad=$((bad+1)); }
  return $bad
}
if getent passwd "$U" >/dev/null; then
  echo "check: user $U exists, verifying (never fixed here)"
  if verify; then echo "RESULT: OK, $U exists and matches D43, nothing to do"; next; exit 0; fi
  echo "RESULT: FAIL, $U exists but does not match D43, nothing changed"
  exit 1
fi
pre=0
getent group "$U" >/dev/null && { echo "FAIL group $U exists without user $U"; pre=1; }
getent group forge >/dev/null || { echo "FAIL group forge missing"; pre=1; }
stat "$H" >/dev/null 2>&1 && { echo "FAIL $H exists without user $U"; pre=1; }
stat "$OUT" >/dev/null 2>&1 && { echo "FAIL $OUT exists without user $U"; pre=1; }
[ $pre -eq 0 ] || { echo "RESULT: FAIL, leftovers found, nothing changed"; exit 1; }
echo "check: user $U absent, group $U absent, $H absent, $OUT absent, group forge present"
run() {
  echo "+ $*"
  [ $APPLY -eq 1 ] || return 0
  "$@" || { echo "RESULT: FAIL, command failed: $*"; exit 1; }
}
run sudo useradd --create-home --user-group --shell /bin/bash "$U"
run sudo passwd --lock "$U"
run sudo chmod 750 "$H"
echo "+ printf '%s\n' '$PROFILE_LINE' | sudo -u $U tee -a $H/.profile"
if [ $APPLY -eq 1 ]; then
  printf '%s\n' "$PROFILE_LINE" | sudo -u "$U" tee -a "$H/.profile" >/dev/null \
    || { echo "RESULT: FAIL, profile append failed"; exit 1; }
fi
run sudo install -d -o "$U" -g forge -m 2750 "$OUT"
if [ $APPLY -eq 0 ]; then
  echo "RESULT: DRY RUN, nothing changed. Re-run with --apply to execute."
  next; exit 0
fi
if verify; then echo "RESULT: OK, $U created and matches D43"; next; exit 0; fi
echo "RESULT: FAIL, $U created but does not match D43"
exit 1
