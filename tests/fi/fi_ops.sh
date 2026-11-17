#!/usr/bin/env bash
# Brief ops-tooling fault injection (pre-registered M1-M5 against deploy/automerge.sh
# and deploy/suites.sh). Run AS forgedev from anywhere:  bash tests/fi/fi_ops.sh
# Works on a temp copy of this worktree (never the tree it lives in): per mutation,
# sed -i ONE line in the copy, print mutated_lines (must be 1), run tests/test_ops.py
# (must exit non-zero with a FAIL line, not a traceback), cp restore from the pristine
# copy, sha256sum -c, then require every suite to pass again.
# Prints "FI Mn bit" or "FI Mn DID NOT BITE"; exits 0 only if every mutation bit and
# every restore is clean. Predictions were committed before the first run.
set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
W=$(mktemp -d /tmp/fi_ops.XXXXXX)
trap 'rm -rf "$W"' EXIT
cp -a "$SRC/." "$W/copy"; rm -rf "$W/copy/.git"
cp -a "$W/copy" "$W/pristine"
cd "$W/copy" || exit 1
PY=.venv/bin/python
SOURCES="deploy/automerge.sh deploy/suites.sh"
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
all_suites || { echo "FI_OPS: baseline not green, nothing injected"; exit 1; }
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
    $PY tests/test_ops.py </dev/null >| "$W/ops.out" 2>&1; rc=$?
    last=$(tail -1 "$W/ops.out")
    echo "    test_ops exit=$rc | $last"
    grep '^  ' "$W/ops.out" | head -5 | sed 's/^/    /'
    if [ "$nlines" = 1 ] && [ $rc -ne 0 ] && echo "$last" | grep -q '^FAIL: ' \
       && ! grep -q 'Traceback (most recent call last)' "$W/ops.out"; then
        echo "FI $id bit"
    else
        echo "FI $id DID NOT BITE"; ALL_BIT=0
    fi
    cp -p "$W/pristine/$file" "$file"
    sha256sum -c --quiet "$W/sources.sha256" && echo "    restored: sha256 ok" || { echo "    RESTORE FAILED"; ALL_BIT=0; }
    all_suites && echo "    all suites pass after restore" || { echo "    SUITES NOT GREEN after restore"; ALL_BIT=0; }
}

# M1 allowlist also accepts deploy/  (predicted: test_ops fails, A1/A3/A5)
mutate M1 deploy/automerge.sh 's#^    tests/\*|docs/\*) ;;$#    tests/*|docs/*|deploy/*) ;;#' \
    "allowlist also accepts deploy/"
# M2 every-path check becomes any-path  (predicted: test_ops fails, A3/A4)
mutate M2 deploy/automerge.sh 's/^\[ \$bad -eq 0 \] || exit 10$/[ $bad -lt $n ] || exit 10/' \
    "every-path check becomes any-path"
# M3 verified-SHA comparison skipped  (predicted: test_ops fails, A7)
mutate M3 deploy/automerge.sh 's/^\[ "\$V" = "\$R" \] || refuse/true || refuse/' \
    "verified-SHA comparison skipped"
# M4 suites.sh skips the HEAD == SHA check  (predicted: test_ops fails, S2)
mutate M4 deploy/suites.sh 's/^\[ "\$HEAD" = "\$SHA" \] || {/true || {/' \
    "suites.sh skips the HEAD == SHA check"
# M5 suites.sh exits 0 when a suite fails  (predicted: test_ops fails, S4/S5/M4)
mutate M5 deploy/suites.sh '$s/^exit 1$/exit 0/' \
    "suites.sh exits 0 when a suite fails"

echo
echo "=== FINAL sha256sum -c"
sha256sum -c "$W/sources.sha256" || ALL_BIT=0
if [ $ALL_BIT -eq 1 ]; then echo "FI_OPS: every mutation bit, every source restored, suites green"; exit 0; fi
echo "FI_OPS: FINDING - see DID NOT BITE / restore lines above"
exit 1
