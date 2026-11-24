#!/usr/bin/env bash
# Brief forced-paths Tier 1 fault injection (funding criterion 4: partially_filled,
# pending_cancel, crash between write-ahead and submit, submit timeout; both legs).
# Run AS forgedev from anywhere:  bash tests/fi/fi_forced_paths.sh
# Everything runs in a temp copy of this tree (tracked files only, .venv linked in
# because test_ops uses ROOT/.venv); the tree it lives in is never mutated.
# Per mutation: check the target line still holds the expected original text,
# sed -i ONE line-addressed change, print mutated_lines (must be 1), run ALL suites,
# compare each suite's failure count with the count PREDICTED below (suites not
# named: 0; a traceback is CRASH, never a match), cp restore from the pristine copy,
# sha256sum -c. After the last mutation: sha256sum -c of every source, then ALL
# suites again (each must PASS). Exits 0 only if every mutation matched its
# prediction, every restore verified and the final run is green.
# Predictions are registered here and in docs/FORCED_PATHS.md before the first run
# and are never edited after it. No mutation here is registered as a guard.
set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
W=$(mktemp -d /tmp/fi_forced_paths.XXXXXX)
trap 'rm -rf "$W"' EXIT
COPY="$W/tree"
PRISTINE="$W/pristine"
mkdir -p "$COPY" "$PRISTINE"
(cd "$SRC" && git ls-files -z | tar --null -cf - -T -) | tar -xf - -C "$COPY"
ln -s "$(readlink -f "$SRC/.venv")" "$COPY/.venv"
cd "$COPY" || exit 1
PY=.venv/bin/python
echo "source: $SRC"
echo "HEAD: $(git -C "$SRC" rev-parse HEAD)"
echo "temp copy: $COPY"

SOURCES="forgetrader_state.py forgetrader/submit.py forgetrader/run.py"
for f in $SOURCES; do
    mkdir -p "$PRISTINE/$(dirname "$f")"
    cp -p "$f" "$PRISTINE/$f"
done
sha256sum $SOURCES > "$W/sources.sha256"
cat "$W/sources.sha256"
(cd "$SRC" && sha256sum $SOURCES) > "$W/src_tree.sha256"
for f in state.json journal.db; do
    if [ -e "$SRC/$f" ]; then (cd "$SRC" && sha256sum "$f") >> "$W/src_tree.sha256"
    else echo "real $f: absent"; fi
done

suite_failures() {  # $1 = output file, $2 = exit code -> prints the failure count
    local out=$1 rc=$2 n
    if grep -q 'Traceback (most recent call last)' "$out"; then echo CRASH; return; fi
    if [ "$rc" -eq 0 ] && tail -1 "$out" | grep -q '^PASS'; then echo 0; return; fi
    n=$(tail -1 "$out" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' | head -1)
    [ -z "$n" ] && n=$(grep -m1 -oE '^[0-9]+ failed' "$out" | grep -oE '[0-9]+')
    # test_snapshot prints a bare "FAIL" then one indented line per failure.
    [ -z "$n" ] && grep -qx 'FAIL' "$out" && \
        n=$(sed -n '/^FAIL$/,$p' "$out" | grep -c '^  ')
    [ -z "$n" ] && n="unparsed_rc$rc"
    echo "$n"
}

all_green() {  # every suite exits 0 with a PASS last line; prints the failures
    local t rc bad=0
    for t in tests/test_*.py; do
        $PY "$t" </dev/null >| "$W/t.out" 2>&1; rc=$?
        if [ $rc -ne 0 ] || ! tail -1 "$W/t.out" | grep -q '^PASS'; then
            echo "    $t exit=$rc | $(tail -1 "$W/t.out")"; bad=$((bad + 1))
        fi
    done
    echo "    suites run: $(ls tests/test_*.py | wc -l), not green: $bad"
    [ $bad -eq 0 ]
}

echo
echo "=== baseline (unmutated copy)"
all_green || { echo "FI_FORCED_PATHS: baseline not green, nothing injected"; exit 1; }

MISMATCH=0
SUMMARY=""

# mutate ID FILE LINE ORIGINAL SED_EXPR "suite=N ..." DESCRIPTION
mutate() {
    local id=$1 file=$2 line=$3 orig=$4 expr=$5 pred=$6 desc=$7
    local t name want got rc out ok=1 bit=0 nplus nminus
    echo
    echo "=== $id $desc"
    echo "    $file:$line  sed -i '${line}${expr}'"
    if [ "$(sed -n "${line}p" "$file")" != "$orig" ]; then
        echo "    target line moved: line $line is not: $orig"
        echo "    $id RESULT: NOT_RUN PREDICTION_MISMATCH"
        MISMATCH=1; SUMMARY="$SUMMARY
$id NOT_RUN PREDICTION_MISMATCH"
        return
    fi
    echo "    original line: $(echo "$orig" | sed 's/^ *//')"
    echo "    predicted: $pred (every other suite 0)"
    sed -i "${line}${expr}" "$file"
    nplus=$(diff "$PRISTINE/$file" "$file" | grep -c '^>')
    nminus=$(diff "$PRISTINE/$file" "$file" | grep -c '^<')
    if [ "$nplus" = 1 ] && [ "$nminus" = 1 ]; then
        echo "    mutated_lines=$nplus (expect 1) match"
    else
        echo "    mutated_lines=$nplus removed=$nminus (expect 1) MISMATCH"; ok=0
    fi
    echo "    mutated line: $(sed -n "${line}p" "$file" | sed 's/^ *//')"
    for t in tests/test_*.py; do
        name=$(basename "$t" .py); name=${name#test_}
        out="$W/out.$name"
        $PY "$t" </dev/null >| "$out" 2>&1; rc=$?
        got=$(suite_failures "$out" "$rc")
        want=$(echo " $pred " | grep -oE " $name=[0-9]+ " | grep -oE '[0-9]+')
        [ -z "$want" ] && want=0
        if [ "$got" = "$want" ]; then flag=match; else flag=MISMATCH; ok=0; fi
        [ "$got" != 0 ] && bit=1
        if [ "$got" != 0 ] || [ "$want" != 0 ]; then
            printf '    %-18s exit=%s failed=%-6s predicted=%-4s %s | %s\n' \
                "$name" "$rc" "$got" "$want" "$flag" "$(tail -1 "$out")"
        fi
    done
    cp -p "$PRISTINE/$file" "$file"
    echo "    restore: $(sha256sum -c "$W/sources.sha256" 2>&1 | grep "^$file:")"
    [ $bit -eq 1 ] && bitword=BIT || { bitword=NO_BITE; ok=0; }
    [ $ok -eq 1 ] && matchword=all_predictions_match || { matchword=PREDICTION_MISMATCH; MISMATCH=1; }
    echo "    $id RESULT: $bitword $matchword"
    SUMMARY="$SUMMARY
$id $bitword $matchword"
}

# ---- P1 partially_filled ----------------------------------------------------
mutate M1 forgetrader_state.py 85 '    "partially_filled": "HALT",' \
    's/"partially_filled": "HALT",/"partially_filled": "FILLED",/' \
    "state_machine=6 forced_paths=18" \
    "P1 both legs: status map partially_filled -> FILLED (D10/D11 line)"

mutate M2 forgetrader_state.py 268 '        cls = classify_order_status(order_status)' \
    's/cls = classify_order_status(order_status)/cls = "FILLED" if order_status == "partially_filled" else classify_order_status(order_status)/' \
    "state_machine=2 forced_paths=9" \
    "P1 CSP leg: reconcile CSP_PENDING reads partially_filled as FILLED"

mutate M3 forgetrader_state.py 326 '        cls = classify_order_status(order_status)' \
    's/cls = classify_order_status(order_status)/cls = "FILLED" if order_status == "partially_filled" else classify_order_status(order_status)/' \
    "state_machine=2 forced_paths=9" \
    "P1 CC leg: reconcile CC_PENDING reads partially_filled as FILLED"

# ---- P2 pending_cancel ------------------------------------------------------
mutate M4 forgetrader_state.py 78 '    "pending_cancel": "LIVE",' \
    's/"pending_cancel": "LIVE",/"pending_cancel": "DEAD",/' \
    "state_machine=6 forced_paths=28" \
    "P2 both legs: status map pending_cancel -> DEAD (exit before the broker confirms)"

mutate M5 forgetrader_state.py 268 '        cls = classify_order_status(order_status)' \
    's/cls = classify_order_status(order_status)/cls = "DEAD" if order_status == "pending_cancel" else classify_order_status(order_status)/' \
    "state_machine=2 forced_paths=14" \
    "P2 CSP leg: reconcile CSP_PENDING reads pending_cancel as DEAD"

mutate M6 forgetrader_state.py 326 '        cls = classify_order_status(order_status)' \
    's/cls = classify_order_status(order_status)/cls = "DEAD" if order_status == "pending_cancel" else classify_order_status(order_status)/' \
    "state_machine=2 forced_paths=14" \
    "P2 CC leg: reconcile CC_PENDING reads pending_cancel as DEAD"

# ---- P3 crash between write-ahead and submit (+ P4a) ------------------------
mutate M7 forgetrader_state.py 764 '                tracked_order_status = "not_found"' \
    's/tracked_order_status = "not_found"/tracked_order_status = "canceled"/' \
    "forced_paths=82" \
    "P3 + P4a both legs: broker 404 read as canceled (exit to home, then resubmit)"

mutate M8 forgetrader/submit.py 105 '        fs.write_state(new, state_path)  # write-ahead' \
    's/fs.write_state(new, state_path)  # write-ahead/pass  # write-ahead removed by FI/' \
    "place_csp=14 run_stages=2 probe=2 forced_paths=20" \
    "P3 CSP leg: place_csp submits with no write-ahead"

mutate M9 forgetrader/submit.py 235 '        fs.write_state(new, state_path)  # write-ahead' \
    's/fs.write_state(new, state_path)  # write-ahead/pass  # write-ahead removed by FI/' \
    "place_cc=17 run_stages=2 forced_paths=20" \
    "P3 CC leg: place_cc submits with no write-ahead"

# ---- P4 submit timeout ------------------------------------------------------
mutate M10 forgetrader/submit.py 136 '        return new, _result(' \
    's/return new, _result(/return state, _result(/' \
    "place_csp=3 run_stages=1 alerts=1 forced_paths=40" \
    "P4 CSP leg: place_csp HALT returns the input FLAT state (rollback)"

mutate M11 forgetrader/submit.py 266 '        return new, _result(' \
    's/return new, _result(/return state, _result(/' \
    "place_cc=4 run_stages=2 forced_paths=40" \
    "P4 CC leg: place_cc HALT returns the input HOLDING state (rollback)"

mutate M12 forgetrader/run.py 408 '                new_state = state_out  # persisted on every outcome' \
    's/new_state = state_out  # persisted on every outcome/pass  # state_out dropped by FI/' \
    "run_stages=4 clock=2 alerts=1 forced_paths=63" \
    "P4 CSP leg: run.py does not persist the state place_csp returned"

mutate M13 forgetrader/run.py 491 '                new_state = state_out  # persisted on every outcome' \
    's/new_state = state_out  # persisted on every outcome/pass  # state_out dropped by FI/' \
    "run_stages=7 clock=1 forced_paths=63" \
    "P4 CC leg: run.py does not persist the state place_cc returned"

echo
echo "=== SUMMARY"
echo "$SUMMARY" | sed '/^$/d'
echo
echo "=== FINAL sha256sum -c (every mutated source restored in the copy)"
sha256sum -c "$W/sources.sha256"; SHA_RC=$?
echo "=== source tree untouched (sha256sum -c in $SRC)"
(cd "$SRC" && sha256sum -c "$W/src_tree.sha256"); SRC_RC=$?
echo "=== all suites after the last restore"
all_green; GREEN_RC=$?
echo "sha256 copy exit=$SHA_RC source exit=$SRC_RC final suites exit=$GREEN_RC"
if [ $MISMATCH -eq 0 ] && [ $SHA_RC -eq 0 ] && [ $SRC_RC -eq 0 ] && [ $GREEN_RC -eq 0 ]; then
    echo "FI_FORCED_PATHS: all 13 mutations bit as predicted, every source restored, suites green"
    exit 0
fi
echo "FI_FORCED_PATHS: FINDING - see MISMATCH / NO_BITE / sha256 / suite lines above"
exit 1
