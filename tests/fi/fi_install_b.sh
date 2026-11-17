#!/usr/bin/env bash
# Brief b-standup fault injection (pre-registered M1-M4 against deploy/b/install_b.py).
# Run AS forgedev from anywhere:  bash tests/fi/fi_install_b.sh
# Works on a temp copy of this worktree (never the tree it lives in): per mutation,
# sed -i ONE line in the copy, print mutated_lines (must be 1), run tests/test_install_b.py
# (must exit non-zero with a FAIL line, not a traceback), cp restore from the pristine
# copy, sha256sum -c, then require every suite to pass again.
# Prints "FI Mn bit" or "FI Mn DID NOT BITE"; exits 0 only if every mutation bit and
# every restore is clean. Predictions were committed before the first run.
set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
W=$(mktemp -d /tmp/fi_install_b.XXXXXX)
trap 'rm -rf "$W"' EXIT
cp -a "$SRC/." "$W/copy"; rm -rf "$W/copy/.git"
cp -a "$W/copy" "$W/pristine"
cd "$W/copy" || exit 1
PY=.venv/bin/python
SOURCES="deploy/b/install_b.py"
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
all_suites || { echo "FI_INSTALL_B: baseline not green, nothing injected"; exit 1; }
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
    $PY tests/test_install_b.py </dev/null >| "$W/ib.out" 2>&1; rc=$?
    last=$(tail -1 "$W/ib.out")
    echo "    test_install_b exit=$rc | $last"
    grep '^  FAIL' "$W/ib.out" | head -5 | sed 's/^/    /'
    if [ "$nlines" = 1 ] && [ $rc -ne 0 ] && echo "$last" | grep -q '^FAIL: ' \
       && ! grep -q 'Traceback (most recent call last)' "$W/ib.out"; then
        echo "FI $id bit"
    else
        echo "FI $id DID NOT BITE"; ALL_BIT=0
    fi
    cp -p "$W/pristine/$file" "$file"
    sha256sum -c --quiet "$W/sources.sha256" && echo "    restored: sha256 ok" || { echo "    RESTORE FAILED"; ALL_BIT=0; }
    all_suites && echo "    all suites pass after restore" || { echo "    SUITES NOT GREEN after restore"; ALL_BIT=0; }
}

# M1 substitution-count check skipped  (predicted: test_install_b fails, C1-C5, C7, C11)
mutate M1 deploy/b/install_b.py 's/^    if got != want:  # substitution count check$/    if False:/' \
    "substitution-count check skipped"
# M2 dry-run writes the units  (predicted: test_install_b fails, D2/D3)
mutate M2 deploy/b/install_b.py 's/^    if not args.apply:$/    if not args.apply and write_units(unit_dir, rendered) is None:/' \
    "dry-run writes the units"
# M3 FORGE_INSTANCE check accepts any value  (predicted: test_install_b fails, P9-P11, P15)
mutate M3 deploy/b/install_b.py 's/^    return line == ENV_LINE$/    return line.startswith(b"FORGE_INSTANCE=")/' \
    "FORGE_INSTANCE check accepts any value"
# M4 OnCalendar minute 10:45 rendered as 10:30  (predicted: test_install_b fails, R2/D1/A1)
mutate M4 deploy/b/install_b.py 's/(("10:30", "10:45", 1),/(("10:30", "10:30", 1),/' \
    "OnCalendar minute 10:45 rendered as 10:30"

echo
echo "=== FINAL sha256sum -c"
sha256sum -c "$W/sources.sha256" || ALL_BIT=0
if [ $ALL_BIT -eq 1 ]; then echo "FI_INSTALL_B: every mutation bit, every source restored, suites green"; exit 0; fi
echo "FI_INSTALL_B: FINDING - see DID NOT BITE / restore lines above"
exit 1
