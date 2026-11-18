#!/usr/bin/env bash
# Brief B8 Tier 1 fault injection (D13 one broker clock, D13a pre-clock
# carve-out, D31 quote ts / ask on gate rows).
# Run AS forgedev from anywhere:  bash tests/fi/fi_b8.sh
# Everything runs in a temp copy of this worktree (tracked files only); the
# worktree itself is never mutated. Per mutation: pristine copy of the
# source, sed -i ONE line-addressed change, print mutated_lines (expect 1),
# run tests/test_clock.py (must exit non-zero), cp restore from pristine,
# sha256sum -c, run ALL suites (each must exit 0).
# Prints "FI Mn bit" or "FI Mn DID NOT BITE"; exits 0 only if all five bite
# and every restore verifies with all suites green.
set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
PY="$SRC/.venv/bin/python"
WORK=$(mktemp -d /tmp/fi_b8.XXXXXX)
PRISTINE="$WORK/pristine"
COPY="$WORK/tree"
mkdir -p "$PRISTINE" "$COPY"
(cd "$SRC" && git ls-files -z | tar --null -cf - -T -) | tar -xf - -C "$COPY"
ln -s "$(readlink -f "$SRC/.venv")" "$COPY/.venv"  # test_ops uses ROOT/.venv
cd "$COPY" || exit 1
echo "source: $SRC"
echo "temp copy: $COPY"
echo "HEAD: $(git -C "$SRC" rev-parse HEAD)"

SOURCES="forgetrader/run.py wheel_dryrun.py"
for f in $SOURCES; do cp -p "$f" "$PRISTINE/$(basename "$f")"; done
sha256sum $SOURCES > "$PRISTINE/sources.sha256"
cat "$PRISTINE/sources.sha256"

all_suites() {  # prints one line per suite; returns the number that failed
    local t rc bad=0 out
    for t in tests/test_*.py; do
        out=$("$PY" "$t" 2>&1); rc=$?
        echo "    $t exit=$rc | $(printf '%s\n' "$out" | tail -1)"
        [ "$rc" -ne 0 ] && bad=$((bad + 1))
    done
    return "$bad"
}

echo
echo "=== baseline (unmutated temp copy)"
all_suites; BASE=$?
if [ "$BASE" -ne 0 ]; then
    echo "BASELINE NOT GREEN ($BASE suites failed): no mutation run"
    exit 1
fi

ALL_BIT=1

# mutate ID FILE GREP_PATTERN SED_EXPR DESCRIPTION
# GREP_PATTERN (fixed string) must match exactly one line of FILE, except
# where FIRST is given as $6 (then the first match is the target).
mutate() {
    local id=$1 file=$2 pat=$3 expr=$4 desc=$5 first=${6:-}
    local base hits line nlines rc out ok=1
    base=$(basename "$file")
    echo
    echo "=== $id $desc"
    hits=$(grep -nF -- "$pat" "$file" | cut -d: -f1)
    if [ -z "$hits" ]; then
        echo "    target not found: $pat"
        echo "FI $id DID NOT BITE"; ALL_BIT=0; return
    fi
    if [ "$(echo "$hits" | wc -l)" -ne 1 ] && [ -z "$first" ]; then
        echo "    target not unique: $pat (lines $(echo $hits))"
        echo "FI $id DID NOT BITE"; ALL_BIT=0; return
    fi
    line=$(echo "$hits" | head -1)
    echo "    $file:$line  sed -i '${line}${expr}'"
    echo "    original line: $(sed -n "${line}p" "$file" | sed 's/^ *//')"
    sed -i "${line}${expr}" "$file"
    nlines=$(diff "$PRISTINE/$base" "$file" | grep -c '^<')
    if [ "$nlines" = 1 ]; then
        echo "    mutated_lines=$nlines (expect 1)"
    else
        echo "    mutated_lines=$nlines (expect 1) MISMATCH"; ok=0
    fi
    echo "    mutated line: $(sed -n "${line}p" "$file" | sed 's/^ *//')"
    out=$("$PY" tests/test_clock.py 2>&1); rc=$?
    printf '%s\n' "$out" | grep '^  FAIL' | sed 's/^/    /'
    echo "    test_clock exit=$rc | $(printf '%s\n' "$out" | tail -1)"
    cp -p "$PRISTINE/$base" "$file"
    echo "    restore: $(sha256sum -c "$PRISTINE/sources.sha256" 2>&1 | grep "^$file:")"
    if ! sha256sum -c --quiet "$PRISTINE/sources.sha256"; then
        echo "    RESTORE FAILED"; ok=0
    fi
    echo "    all suites after restore:"
    if ! all_suites; then echo "    SUITES NOT GREEN AFTER RESTORE"; ok=0; fi
    if [ "$rc" -ne 0 ] && [ "$ok" = 1 ]; then
        echo "FI $id bit"
    else
        echo "FI $id DID NOT BITE"; ALL_BIT=0
    fi
}

mutate M1 forgetrader/run.py 'fs.pin_now(ts)' \
    's/fs\.pin_now(ts)/fs.pin_now(datetime.now(timezone.utc))/' \
    'run.py stamps the run ts (and so opened_at, last_run.ts, journal ts) from the host clock'

mutate M2 forgetrader/run.py 'ts.isoformat(), fs.STATE_PATH)' \
    's/ts\.isoformat(), fs\.STATE_PATH)/datetime.now(timezone.utc).isoformat(), fs.STATE_PATH)/' \
    'the submit path (place_csp, first match) receives a host-clock as_of' first

mutate M3 forgetrader/run.py 'PRECLOCK_OUTCOMES = frozenset({' \
    's/"clock"})/"clock", "snapshot"})/' \
    'the pre-clock set gains a post-clock outcome (snapshot)'

mutate M4 forgetrader/run.py 'if outcome not in PRECLOCK_OUTCOMES:' \
    's/if outcome not in PRECLOCK_OUTCOMES:/if False:/' \
    'preclock_now accepts any outcome (set check removed)'

mutate M5 wheel_dryrun.py 'row["quote_ask"] = quote_ask' \
    'd' \
    'the CSP gate row omits the quote ask'

echo
echo "final sha256sum -c:"
sha256sum -c "$PRISTINE/sources.sha256"
if [ "$ALL_BIT" = 1 ]; then
    echo "FI b8: all 5 mutations bit"
    exit 0
fi
echo "FI b8: NOT ALL MUTATIONS BIT"
exit 1
