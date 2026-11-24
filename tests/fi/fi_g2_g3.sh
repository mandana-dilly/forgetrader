#!/usr/bin/env bash
# Brief g2-g3-hardening Tier 1 fault injection (forced-paths gaps G2 and G3).
# G2: build_snapshot HALTs on a second open order under the tracked client_order_id
# (both legs). G3: TimeoutStartSec=10min in A's run unit and in B's rendered run unit.
# Run AS forgedev from anywhere:  bash tests/fi/fi_g2_g3.sh
# Everything runs in a temp copy of this tree (tracked files only, .venv linked in
# because test_ops uses ROOT/.venv); the tree it lives in is never mutated.
# Per mutation: check the target line still holds the expected original text,
# sed -i ONE line-addressed change, print mutated_lines (must be 1), run ALL suites,
# compare each suite's failure count with the count PREDICTED below (suites not
# named: 0; a traceback is CRASH, never a match), cp restore from the pristine copy,
# sha256sum -c. After the last mutation: sha256sum -c of every source, then ALL
# suites again (each must PASS). Exits 0 only if every mutation matched its
# prediction, every restore verified and the final run is green.
# Predictions are registered here and in docs/FORCED_PATHS.md ("G2 / G3 fix") before
# the first run
# and are never edited after it. No mutation here is registered as a guard.
set -u
SRC=$(cd "$(dirname "$0")/../.." && pwd)
W=$(mktemp -d /tmp/fi_g2_g3.XXXXXX)
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

SOURCES="forgetrader_state.py deploy/forgetrader-run.service deploy/b/install_b.py"
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
all_green || { echo "FI_G2_G3: baseline not green, nothing injected"; exit 1; }

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

# ---- G2 second open order under the tracked id ----------------------------
mutate G2a forgetrader_state.py 715 '    if n_tracked > 1:' \
    's/if n_tracked > 1:/if False:/' \
    "snapshot=4 forced_paths=14" \
    "G2 both legs: build_snapshot guard disabled (two tracked open orders tolerated)"

# ---- G3 run start timeout -----------------------------------------------------
mutate G3a deploy/forgetrader-run.service 10 'TimeoutStartSec=10min' \
    's/^TimeoutStartSec=10min$/# TimeoutStartSec=10min removed by FI/' \
    "install_b=2" \
    "G3 A: TimeoutStartSec line removed from the committed run unit"

mutate G3b deploy/b/install_b.py 191 '    return b_name, t' \
    's/return b_name, t$/return b_name, t.replace(TIMEOUT_LINE + chr(10), "")/' \
    "install_b=4" \
    "G3 B: TimeoutStartSec line removed from the rendered B run unit"

mutate G3c deploy/b/install_b.py 189 '    if b_name == "forgetrader-b-run.service" and TIMEOUT_LINE not in t.splitlines():' \
    's/if b_name == "forgetrader-b-run.service" and TIMEOUT_LINE not in t.splitlines():/if False:/' \
    "install_b=7" \
    "G3 B guard: install_b renders a B run unit without TimeoutStartSec"

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
    echo "FI_G2_G3: all 4 mutations bit as predicted, every source restored, suites green"
    exit 0
fi
echo "FI_G2_G3: FINDING - see MISMATCH / NO_BITE / sha256 / suite lines above"
exit 1
