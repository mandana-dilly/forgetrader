#!/usr/bin/env bash
# Brief scouts-core fault injection (pre-registered M1-M5 against scouts/).
# Run AS forgedev from anywhere:  bash tests/fi/fi_scouts.sh
# Works on a temp copy of this worktree (never the tree it lives in): per mutation,
# sed -i ONE line in the copy, print mutated_lines (must be 1), run tests/test_scouts.py
# (must exit non-zero with a FAIL line, not a traceback), cp restore from the pristine
# copy, sha256sum -c, then require every suite to pass again.
# Prints "FI Mn bit" or "FI Mn DID NOT BITE"; exits 0 only if every mutation bit and
# every restore is clean. Predictions were committed before the first run.
set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
W=$(mktemp -d /tmp/fi_scouts.XXXXXX)
trap 'rm -rf "$W"' EXIT
cp -a "$SRC/." "$W/copy"; rm -rf "$W/copy/.git"
cp -a "$W/copy" "$W/pristine"
cd "$W/copy" || exit 1
PY=.venv/bin/python
SOURCES="scouts/schemas.py scouts/validate.py scouts/barrier.py scouts/facts.py"
sha256sum $SOURCES > "$W/sources.sha256"
echo "temp copy: $W/copy"
cat "$W/sources.sha256"

all_suites() {  # every suite in the copy must exit 0 with a PASS last line
    local t rc bad=0
    for t in tests/test_*.py; do
        $PY "$t" </dev/null >| "$W/t.out" 2>&1; rc=$?
        if [ $rc -ne 0 ] || ! tail -1 "$W/t.out" | grep -q '^PASS'; then
            echo "    $t exit=$rc | $(tail -1 "$W/t.out")"; bad=1
        fi
    done
    return $bad
}

echo "=== baseline: all suites in the copy"
all_suites || { echo "FI_SCOUTS: baseline not green, nothing injected"; exit 1; }
echo "baseline green"

ALL_BIT=1
# mutate ID FILE SED_EXPR DESCRIPTION
mutate() {
    local id=$1 file=$2 expr=$3 desc=$4 nlines rc last
    echo
    echo "=== $id $desc"
    echo "    $file  sed -i '$expr'"
    sed -i "$expr" "$file"
    nlines=$(diff "$W/pristine/$file" "$file" | grep -c '^>')
    echo "    mutated_lines=$nlines"
    diff "$W/pristine/$file" "$file" | sed 's/^/    /'
    $PY tests/test_scouts.py </dev/null >| "$W/sc.out" 2>&1; rc=$?
    last=$(tail -1 "$W/sc.out")
    echo "    test_scouts exit=$rc | $last"
    grep '^  ' "$W/sc.out" | head -5 | sed 's/^/    /'
    if [ "$nlines" = 1 ] && [ $rc -ne 0 ] && echo "$last" | grep -q '^FAIL: ' \
       && ! grep -q 'Traceback (most recent call last)' "$W/sc.out"; then
        echo "FI $id bit"
    else
        echo "FI $id DID NOT BITE"; ALL_BIT=0
    fi
    cp -p "$W/pristine/$file" "$file"
    sha256sum -c --quiet "$W/sources.sha256" && echo "    restored: sha256 ok" || { echo "    RESTORE FAILED"; ALL_BIT=0; }
    all_suites && echo "    all suites pass after restore" || { echo "    SUITES NOT GREEN after restore"; ALL_BIT=0; }
}

# M1 earnings "agree" accepts two sources on the same host  (predicted: test_scouts fails, E SAME/WWW/CASE)
mutate M1 scouts/validate.py 's/^    if len(hosts) >= 2:$/    if len(sources) >= 2:/' \
    "earnings agree accepts two sources on the same host"
# M2 http:// URLs accepted  (predicted: test_scouts fails, V6 http in every role)
mutate M2 scouts/schemas.py 's/^    if parts.scheme != "https" or not host:$/    if parts.scheme not in ("https", "http") or not host:/' \
    "http URLs accepted"
# M3 barrier ignores the sha match  (predicted: test_scouts fails, B5)
mutate M3 scouts/barrier.py 's/^    if ok is not None and ok == sha:$/    if ok is not None:/' \
    "barrier ignores the sha match (file edited after .ok)"
# M4 earnings_date returns the date on conflict  (predicted: test_scouts fails, F3 CON/CON.B, F7)
mutate M4 scouts/facts.py 's/^        if status != "agree":$/        if status not in ("agree", "conflict"):/' \
    "earnings_date returns the date on conflict"
# M5 fetched_at age limit not enforced  (predicted: test_scouts fails, V5, V14)
mutate M5 scouts/validate.py 's/^        if now - ts > MAX_AGE:$/        if False:/' \
    "fetched_at age limit not enforced"

echo
echo "=== FINAL sha256sum -c"
sha256sum -c "$W/sources.sha256" || ALL_BIT=0
if [ $ALL_BIT -eq 1 ]; then echo "FI_SCOUTS: every mutation bit, every source restored, suites green"; exit 0; fi
echo "FI_SCOUTS: FINDING - see DID NOT BITE / restore lines above"
exit 1
