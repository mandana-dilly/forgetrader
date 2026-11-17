#!/usr/bin/env bash
# Brief instance-config Tier 1 fault injection (forge_instance.py table and
# lookup, alerts.send prefix, run.py instance check order).
# Run AS forgedev from anywhere:  bash tests/fi/fi_instance.sh
# Everything runs in a temp COPY of this checkout's tracked files (plus a
# .venv symlink); the checkout itself is never mutated (its sources are
# sha256-checked at the end). No .env, state.json or journal.db is copied.
# Per mutation: pristine copy of the source, ONE mutation, print
# mutated_lines, run the named suite (must exit non-zero with a FAIL: line,
# a traceback is a crash, not a bite), cp restore from pristine, sha256sum -c,
# then ALL suites must pass again. Prints "FI Mn bit" or "FI Mn DID NOT BITE".
# Exit 0 only if every mutation bit and every restore passed.
# Pre-registered (brief): every mutation -> test_instance fails.
#   M1 table maps "a" to "policy-b.toml"
#   M2 missing FORGE_INSTANCE returns the "a" row
#   M3 alerts.send uses the literal "[forgetrader] "
#   M4 run.py resolves the instance after the broker read: a MOVE of the
#      2-line check block (if _instance_halted(...): / return 4) to just after
#      the read_broker_financials call, so mutated_lines expect 2. The check
#      is a 2-line if/return; moving one line alone leaves a dangling return.
#   M5 lookup strips and lowercases the value before the match
set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d /tmp/fi_instance.XXXXXX)
PRISTINE=$(mktemp -d /tmp/fi_instance_pristine.XXXXXX)
(cd "$SRC" && git ls-files -z | xargs -0 cp --parents -p -t "$WORK") || exit 1
ln -s "$(readlink -f "$SRC/.venv")" "$WORK/.venv" || exit 1
for f in .env state.json journal.db; do
    [ -e "$WORK/$f" ] && { echo "REFUSED: $f copied into $WORK"; exit 1; }
done
echo "source checkout: $SRC"
echo "work copy:       $WORK"
SOURCES="forge_instance.py alerts.py forgetrader/run.py"
(cd "$SRC" && sha256sum $SOURCES) > "$PRISTINE/src.sha256"
cd "$WORK" || exit 1
for f in $SOURCES; do cp -p "$f" "$PRISTINE/$(basename "$f")"; done
sha256sum $SOURCES > "$PRISTINE/work.sha256"
cat "$PRISTINE/work.sha256"
PY=.venv/bin/python
SUITES=$(ls tests/test_*.py)
FAILED=0
SUMMARY=""

lineno() {  # $1 file, $2 fixed string -> the one line number, else empty
    local hits
    hits=$(grep -nF -- "$2" "$1" | cut -d: -f1)
    [ "$(echo "$hits" | grep -c .)" = 1 ] && echo "$hits"
}

all_pass() {  # every suite exit 0 with a PASS: last line
    local t out rc ok=0
    for t in $SUITES; do
        out=$($PY "$t" 2>&1); rc=$?
        if [ $rc -ne 0 ] || ! echo "$out" | tail -1 | grep -q '^PASS:'; then
            echo "    after restore: $t exit=$rc | $(echo "$out" | tail -1)"
            ok=1
        fi
    done
    return $ok
}

# finish ID FILE EXPECT_LINES SUITE: count, run, restore, verify, report.
finish() {
    local id=$1 file=$2 expect=$3 suite=$4 base nlines out rc last word=bit
    base=$(basename "$file")
    nlines=$(diff "$PRISTINE/$base" "$file" | grep -c '^>')
    if [ "$nlines" = "$expect" ]; then
        echo "    mutated_lines=$nlines (expect $expect) match"
    else
        echo "    mutated_lines=$nlines (expect $expect) MISMATCH"
        word="DID NOT BITE (mutation not applied as registered)"
    fi
    diff "$PRISTINE/$base" "$file" | sed 's/^/    | /'
    out=$($PY "tests/$suite.py" 2>&1); rc=$?
    last=$(echo "$out" | tail -1)
    echo "    $suite exit=$rc | $last"
    echo "$out" | grep '^  ' | sed 's/^/    /'
    if echo "$out" | grep -q 'Traceback (most recent call last)'; then
        word="DID NOT BITE (crash, not a bite)"
    elif [ $rc -eq 0 ] || ! echo "$last" | grep -q '^FAIL: '; then
        word="DID NOT BITE"
    fi
    cp -p "$PRISTINE/$base" "$file"
    echo "    restore: $(sha256sum -c "$PRISTINE/work.sha256" 2>&1 | grep "^$file:")"
    if all_pass; then
        echo "    after restore: all suites PASS"
    else
        word="$word, RESTORE FAILED"
    fi
    [ "$word" = bit ] || FAILED=1
    echo "FI $id $word"
    SUMMARY="$SUMMARY
FI $id $word"
}

# mutate ID FILE "FIXED ORIGINAL TEXT" SED_SUBST_EXPR DESCRIPTION
mutate() {
    local id=$1 file=$2 orig=$3 expr=$4 desc=$5 line
    echo
    echo "=== $id $desc"
    line=$(lineno "$file" "$orig")
    if [ -z "$line" ]; then
        echo "    original text not found exactly once in $file"
        FAILED=1; SUMMARY="$SUMMARY
FI $id DID NOT BITE (mutation target missing)"; echo "FI $id DID NOT BITE"; return
    fi
    echo "    $file:$line  sed -i '${line}${expr}'"
    sed -i "${line}${expr}" "$file"
    finish "$id" "$file" 1 test_instance
}

mutate M1 forge_instance.py '"a": Instance("a", "policy.toml", "[forgetrader] "),' \
    's/"a", "policy.toml"/"a", "policy-b.toml"/' \
    'table maps "a" to "policy-b.toml"'

mutate M2 forge_instance.py 'raise InstanceError(f"{ENV_VAR} is not set")' \
    's/raise InstanceError(f"{ENV_VAR} is not set")/return _TABLE["a"]/' \
    'missing FORGE_INSTANCE returns the "a" row'

mutate M3 alerts.py 'prefix = forge_instance.current().prefix' \
    's/prefix = forge_instance.current().prefix/prefix = "[forgetrader] "/' \
    'alerts.send uses the literal "[forgetrader] "'

# M4: move the 2-line instance check block below the broker read.
echo
echo "=== M4 run.py resolves the instance after the broker read"
F=forgetrader/run.py
CHK=$(lineno "$F" 'if _instance_halted(conn, run_id, state, mode):')
READ=$(lineno "$F" 'trading_client=trading_client, tracked_client_order_id=tracked)')
if [ -z "$CHK" ] || [ -z "$READ" ] || [ "$CHK" -ge "$READ" ]; then
    echo "    targets not found exactly once / in order (check=$CHK read=$READ)"
    FAILED=1; SUMMARY="$SUMMARY
FI M4 DID NOT BITE (mutation target missing)"; echo "FI M4 DID NOT BITE"
else
    sed -n "${CHK},$((CHK + 1))p" "$F" > "$PRISTINE/m4_block"
    echo "    $F: lines $CHK-$((CHK + 1)) moved to after line $READ"
    sed -i -e "${READ}r $PRISTINE/m4_block" -e "${CHK},$((CHK + 1))d" "$F"
    finish M4 "$F" 2 test_instance
fi

mutate M5 forge_instance.py 'row = _TABLE.get(value)' \
    's/_TABLE.get(value)/_TABLE.get(value.strip().lower())/' \
    'lookup strips and lowercases the value before the match'

echo
echo "=== SUMMARY"
echo "$SUMMARY" | sed '/^$/d'
echo
echo "=== FINAL sha256sum -c (work copy restored)"
sha256sum -c "$PRISTINE/work.sha256"; W_RC=$?
echo "=== FINAL sha256sum -c (source checkout never mutated)"
(cd "$SRC" && sha256sum -c "$PRISTINE/src.sha256"); S_RC=$?
for f in state.json journal.db; do
    [ -e "$SRC/$f" ] && echo "real $f: present (untouched: the FI never writes outside $WORK)" \
        || echo "real $f: absent"
done
if [ $FAILED -eq 0 ] && [ $W_RC -eq 0 ] && [ $S_RC -eq 0 ]; then
    echo "FI_INSTANCE: every mutation bit, every restore passed"
    exit 0
fi
echo "FI_INSTANCE: FINDING - see DID NOT BITE / RESTORE / sha256 lines above"
exit 1
