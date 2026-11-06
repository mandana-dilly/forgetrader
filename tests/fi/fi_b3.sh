#!/usr/bin/env bash
# Brief 3 Tier 1 fault injection (--set-state removed from forgetrader_state.py).
# Run AS forgedev from anywhere:  bash tests/fi/fi_b3.sh
# Per mutation: pristine copy in /tmp, sed -i ONE line-addressed mutation,
# print mutated_lines (expect 1), run ALL suites, compare each suite's failure
# count with the PREDICTED count registered below, cp restore from pristine,
# sha256sum -c. Ends with sha256sum -c on every mutated source (plus the real
# state.json / journal.db if they exist; the forgedev clone has neither).
# Predictions were committed before the first run and are never edited after.
# The --set-state removal itself is covered by test_state_cli G1/G2, a
# REGRESSION GUARD: no single-line mutation can restore a deleted option.
set -u
cd "$(dirname "$0")/../.." || exit 1
PY=.venv/bin/python
PRISTINE=$(mktemp -d /tmp/fi_b3.XXXXXX)
SOURCES="forgetrader_state.py"
for f in $SOURCES; do cp -p "$f" "$PRISTINE/$(basename "$f")"; done
sha256sum $SOURCES > "$PRISTINE/sources.sha256"
for f in state.json journal.db; do
    if [ -e "$f" ]; then sha256sum "$f" >> "$PRISTINE/sources.sha256"; else echo "real $f: absent"; fi
done
echo "pristine dir: $PRISTINE"
cat "$PRISTINE/sources.sha256"

SUITES=$(ls tests/test_*.py)
MISMATCH=0
SUMMARY=""

suite_failures() {  # $1 = output file, $2 = exit code -> prints the failure count
    local out=$1 rc=$2 n
    if grep -q 'Traceback (most recent call last)' "$out"; then echo CRASH; return; fi
    if [ "$rc" -eq 0 ] && tail -1 "$out" | grep -q '^PASS:'; then echo 0; return; fi
    n=$(tail -1 "$out" | grep -oE '[0-9]+ failed' | grep -oE '[0-9]+' | head -1)
    [ -z "$n" ] && n=$(grep -m1 -oE '^[0-9]+ failed' "$out" | grep -oE '[0-9]+')
    [ -z "$n" ] && n="unparsed_rc$rc"
    echo "$n"
}

# mutate ID FILE LINE SED_EXPR "suite=N ..." DESCRIPTION
# Suites not named in the prediction are predicted 0.
mutate() {
    local id=$1 file=$2 line=$3 expr=$4 pred=$5 desc=$6
    local base t name want got rc out bit=0 ok=1
    base=$(basename "$file")
    echo
    echo "=== $id $desc"
    echo "    $file:$line  sed -i '${line}${expr}'"
    echo "    predicted: $pred (every other suite 0)"
    sed -i "${line}${expr}" "$file"
    echo "    mutated_lines=$(diff "$PRISTINE/$base" "$file" | grep -c '^>') (expect 1)"
    echo "    mutated line: $(sed -n "${line}p" "$file" | sed 's/^ *//')"
    for t in $SUITES; do
        name=$(basename "$t" .py); name=${name#test_}
        out=$(mktemp "$PRISTINE/out.XXXXXX")
        $PY "$t" > "$out" 2>&1; rc=$?
        got=$(suite_failures "$out" "$rc")
        want=$(echo " $pred " | grep -oE " $name=[0-9]+ " | grep -oE '[0-9]+')
        [ -z "$want" ] && want=0
        if [ "$got" = "$want" ]; then flag=match; else flag=MISMATCH; ok=0; fi
        [ "$got" != 0 ] && bit=1
        printf '    %-18s exit=%s failed=%-5s predicted=%-3s %s | %s\n' \
            "$name" "$rc" "$got" "$want" "$flag" "$(tail -1 "$out")"
    done
    cp -p "$PRISTINE/$base" "$file"
    echo "    restore: $(sha256sum -c "$PRISTINE/sources.sha256" 2>&1 | grep "^$file:")"
    [ $bit -eq 1 ] && bitword=BIT || bitword=NO_BITE
    [ $ok -eq 1 ] && matchword=all_predictions_match || { matchword=PREDICTION_MISMATCH; MISMATCH=1; }
    [ $bit -eq 0 ] && MISMATCH=1
    echo "    $id RESULT: $bitword $matchword"
    SUMMARY="$SUMMARY
$id $bitword $matchword"
}

# ---- --init refusal (D7 exit 3), exercised only by test_state_cli ----------
mutate M1 forgetrader_state.py 729 's/if STATE_PATH.exists():/if not STATE_PATH.exists():/' \
    "state_cli=14" \
    "--init refuse-if-exists inverted (I1 exit, sha, REFUSE, journal, stub 5; I2 exit, sha, REFUSE 3; I3 exit, written, FLAT, coid, stub, journal 6)"
mutate M2 forgetrader_state.py 732 's/return 3/return 0/' \
    "state_cli=2" \
    "--init refusal exit 3 becomes 0 (I1 exit, I2 exit; file, REFUSE, journal and stub checks still hold)"

echo
echo "=== SUMMARY"
echo "$SUMMARY" | sed '/^$/d'
echo
echo "=== FINAL sha256sum -c (every mutated source restored)"
sha256sum -c "$PRISTINE/sources.sha256"; SHA_RC=$?
echo "sha256sum exit=$SHA_RC"
if [ $MISMATCH -eq 0 ] && [ $SHA_RC -eq 0 ]; then
    echo "FI_B3: every mutation bit as predicted, every source restored"
    exit 0
fi
echo "FI_B3: FINDING - see MISMATCH / NO_BITE / sha256 lines above"
exit 1
