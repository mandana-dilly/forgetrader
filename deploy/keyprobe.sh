#!/usr/bin/env bash
# keyprobe.sh - proves user forgedev cannot read or find the Alpaca keys.
# Run as forge: cd / && sudo -v && bash ~/forgetrader/deploy/keyprobe.sh
# Reads .env into a pipe only. Prints lengths, verdicts and filenames, never a key.
# NEVER run with bash -x.
set -u
ENV=/home/forge/forgetrader/.env
CANARY=/home/forge/forgetrader/canary.secret
DECOY=/srv/forgedrop/to-dev/decoy.txt
# Marker built at runtime so this file never contains it literally (else section 8 false-FAILs on clones).
MARK="CANARY-NOT-""A-KEY"
keys() { /home/forge/forgetrader/.venv/bin/python -c "
from dotenv import dotenv_values
v = dotenv_values('$ENV')
for k in ('ALPACA_API_KEY', 'ALPACA_API_SECRET'):
    print(v.get(k) or '')
"; }
echo "--- 0 key patterns (lengths only, expect 2 non-zero)"
keys | awk '{print "pattern " NR " length " length($0)}'
np=$(keys | grep -c .)
[ "$np" -eq 2 ] || { echo "ABORT: $np non-empty key patterns, expected 2 (FAIL)"; exit 1; }
echo "--- 1 canary beside .env (same owner and mode), decoy in drop folder"
[ -f "$CANARY" ] || { printf '%s\n' "$MARK" >| "$CANARY"; chmod 600 "$CANARY"; }
[ -f "$DECOY" ] || { printf '%s\n' "$MARK" >| "$DECOY"; chmod 644 "$DECOY"; }
stat -c '%a %U %n' "$ENV" "$CANARY" "$DECOY"
sudo -u forgedev cat "$CANARY" >/dev/null 2>&1 && echo "canary read: ALLOWED (FAIL)" || echo "canary read: BLOCKED"
sudo -u forgedev cat "$DECOY" >/dev/null 2>&1 && echo "decoy read: ALLOWED (control ok)" || echo "decoy read: BLOCKED (control broken)"
echo "--- 2 .env"
sudo -u forgedev test -r "$ENV" && echo ".env readable: YES (FAIL)" || echo ".env readable: BLOCKED"
echo "--- 3 forge process environments"
n=0; r=0
for p in $(pgrep -u forge); do n=$((n+1)); sudo -u forgedev test -r "/proc/$p/environ" && r=$((r+1)); done
echo "forge processes: $n, environ readable by forgedev: $r"
echo "--- 4 forgedev environment"
echo "ALPACA_ vars in forgedev env: $(sudo -u forgedev -H env | grep -c '^ALPACA_')"
echo "--- 5 privilege"
sudo -u forgedev sudo -n true >/dev/null 2>&1 && echo "sudo: ALLOWED (FAIL)" || echo "sudo: BLOCKED"
sudo -u forgedev test -w /var/run/docker.sock && echo "docker socket: WRITABLE (FAIL)" || echo "docker socket: BLOCKED"
echo "--- 6 git history (expect 0)"
echo "key strings in git history: $(git -C /home/forge/forgetrader log -p --all | grep -cF -f <(keys | grep -v '^$'))"
echo "--- 7 positive control: forge searches its own tree (expect .env and canary.secret)"
{ keys; echo "$MARK"; } | grep -v '^$' | grep -rlF -f - /home/forge/forgetrader --exclude-dir=.venv --exclude-dir=.git 2>/dev/null
echo "--- 8 forgedev searches the whole filesystem (expect ONLY $DECOY)"
{ keys; echo "$MARK"; } | grep -v '^$' | sudo -u forgedev grep -rlF -f - / --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev 2>/dev/null
echo "--- done"
