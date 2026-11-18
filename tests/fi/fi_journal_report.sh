#!/usr/bin/env bash
# Brief journal-report fault injection (pre-registered M1-M3 against
# forgetrader/journal_report.py). Run AS forgedev from anywhere:
#   bash tests/fi/fi_journal_report.sh
# Works on a temp copy of this worktree (never the tree it lives in): per mutation,
# sed -i ONE line in the copy, print mutated_lines (must be 1), run ALL suites,
# require test_journal_report to exit non-zero with a FAIL line (not a traceback)
# and every other suite to pass (the prediction), cp restore from the pristine
# copy, sha256sum -c, then require every suite to pass again.
# Prints "FI Mn bit" or "FI Mn DID NOT BITE"; exits 0 only if every mutation bit as
# predicted and every restore is clean. Predictions were committed before the first run.
set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
W=$(mktemp -d /tmp/fi_journal_report.XXXXXX)
trap 'rm -rf "$W"' EXIT
cp -a "$SRC/." "$W/copy"; rm -rf "$W/copy/.git"
cp -a "$W/copy" "$W/pristine"
cd "$W/copy" || exit 1
PY=.venv/bin/python
SOURCES="forgetrader/journal_report.py"
sha256sum $SOURCES > "$W/sources.sha256"
echo "temp copy: $W/copy"
cat "$W/sources.sha256"

FAILED=""
all_suites() {  # sets FAILED to the suites without exit 0 + a PASS last line
    local t rc
    FAILED=""
    for t in tests/test_*.py; do
        $PY "$t" </dev/null >| "$W/t.out" 2>&1; rc=$?
        if [ $rc -ne 0 ] || ! tail -1 "$W/t.out" | grep -q '^PASS'; then
            echo "    $t exit=$rc | $(tail -1 "$W/t.out")"
            FAILED="$FAILED $t"
        fi
    done
    [ -z "$FAILED" ]
}

echo "=== baseline: all suites in the copy"
all_suites || { echo "FI_JOURNAL_REPORT: baseline not green, nothing injected"; exit 1; }
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
    $PY tests/test_journal_report.py </dev/null >| "$W/jr.out" 2>&1; rc=$?
    last=$(tail -1 "$W/jr.out")
    echo "    test_journal_report exit=$rc | $last"
    grep '^  ' "$W/jr.out" | head -5 | sed 's/^/    /'
    all_suites
    echo "    failing suites:${FAILED:- none}"
    if [ "$nlines" = 1 ] && [ $rc -ne 0 ] && echo "$last" | grep -q '^FAIL: ' \
       && ! grep -q 'Traceback (most recent call last)' "$W/jr.out" \
       && [ "$FAILED" = " tests/test_journal_report.py" ]; then
        echo "FI $id bit"
    else
        echo "FI $id DID NOT BITE"; ALL_BIT=0
    fi
    cp -p "$W/pristine/$file" "$file"
    sha256sum -c --quiet "$W/sources.sha256" && echo "    restored: sha256 ok" || { echo "    RESTORE FAILED"; ALL_BIT=0; }
    all_suites && echo "    all suites pass after restore" || { echo "    SUITES NOT GREEN after restore"; ALL_BIT=0; }
}

# M1 matcher ignores a status mismatch  (predicted: only test_journal_report fails, M dec/res/intent/dec_live, X)
mutate M1 forgetrader/journal_report.py 's/^            if status_contradicts(j\["status"\], row\["broker_status"\]):$/            if False:/' \
    "matcher ignores a status mismatch"
# M2 BROKER_ONLY orders are dropped  (predicted: only test_journal_report fails, B)
mutate M2 forgetrader/journal_report.py 's/^    for coid in sorted(set(jmap) | set(bmap)):$/    for coid in sorted(set(jmap)):/' \
    "BROKER_ONLY orders are dropped"
# M3 exit 0 when discrepancies > 0  (predicted: only test_journal_report fails, J, B, M, X exit codes)
mutate M3 forgetrader/journal_report.py 's/^    return 3 if discrepancies else 0$/    return 0/' \
    "exit 0 when discrepancies are found"

echo
echo "=== FINAL sha256sum -c"
sha256sum -c "$W/sources.sha256" || ALL_BIT=0
if [ $ALL_BIT -eq 1 ]; then echo "FI_JOURNAL_REPORT: every mutation bit, every source restored, suites green"; exit 0; fi
echo "FI_JOURNAL_REPORT: FINDING - see DID NOT BITE / restore lines above"
exit 1
