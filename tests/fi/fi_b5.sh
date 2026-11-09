#!/usr/bin/env bash
# Brief B5 Tier 1 fault injection (Stage 2 producer: forgetrader_state.py
# cost_basis on assignment + gates.py evaluate_call).
# Run AS forgedev from anywhere:  bash tests/fi/fi_b5.sh
# Per mutation: pristine copy in /tmp, sed -i ONE line-addressed mutation,
# print mutated_lines (expect 1), run ALL suites, compare each suite's failure
# count with the PREDICTED count registered below, cp restore from pristine,
# sha256sum -c. Ends with sha256sum -c on every mutated source (plus the real
# state.json / journal.db if they exist; the forgedev clone has neither).
# Predictions were committed before the first run and are never edited after.
set -u
cd "$(dirname "$0")/../.." || exit 1
PY=.venv/bin/python
PRISTINE=$(mktemp -d /tmp/fi_b5.XXXXXX)
SOURCES="forgetrader_state.py gates.py"
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
    # test_snapshot prints a bare "FAIL" then one indented line per failure
    # and no count line: count the indented lines after "FAIL".
    [ -z "$n" ] && grep -qx 'FAIL' "$out" && \
        n=$(sed -n '/^FAIL$/,$p' "$out" | grep -c '^  ')
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

# ---- M1: drop the basis set on assignment ---------------------------------
# The assigned transition keeps whatever cost_basis the wheel already had
# (None in every fresh fixture), so reconcile HALTs holding_basis_invalid.
mutate M1 forgetrader_state.py 202 's/= basis$/= new_state["wheel"].get("cost_basis")/' \
    "state_machine=4 reconcile_handle=4 run_stages=5 snapshot=2 cc_gates=6" \
    "assigned no longer sets cost_basis (SM B-tr-assigned 3 + 13a2-assigned-no-date 1; RH S3 dump, alert, stage_to, action 4; RS S1 exit, stage, decisions, WARNING, money 5; SN 16 action, stage 2; CC S1 action, basis, wheel 3 + S1b 1 + S1c 1 + S4 zero-strike 1)"

# ---- M2: strike floor >= to > ---------------------------------------------
mutate M2 gates.py 221 's/>= floor_d/> floor_d/' \
    "cc_gates=2" \
    "strike exactly at the floor rejected (G strike-at-floor rejected_by + all_pass 2)"

# ---- M3: yield denominator basis to strike --------------------------------
mutate M3 gates.py 283 's/(cost_basis \* 100)/(strike * 100)/' \
    "cc_gates=6" \
    "yield on strike (G spread-wide-vs-bid 1, spread-both 1, yield-denominator-basis 2, baseline yield value 1, yield-basis value 1)"

# ---- M4: earnings < to <= -------------------------------------------------
mutate M4 gates.py 293 's/expiration_date < earnings_date)/expiration_date <= earnings_date)/' \
    "cc_gates=3" \
    "expiry on the earnings date passes (G earnings-equal-expiry 2; C8 1)"

# ---- M5: staleness off by sign --------------------------------------------
mutate M5 gates.py 204 's/(run_ts - underlying_price_ts)/(underlying_price_ts - run_ts)/' \
    "cc_gates=6" \
    "staleness sign flipped, every past price looks fresh (G stale-61 2, baseline staleness value 1, run-clock staleness 1, future staleness value 1; C7 stale 1)"

echo
echo "=== SUMMARY"
echo "$SUMMARY" | sed '/^$/d'
echo
echo "=== FINAL sha256sum -c (every mutated source restored)"
sha256sum -c "$PRISTINE/sources.sha256"; SHA_RC=$?
echo "sha256sum exit=$SHA_RC"
if [ $MISMATCH -eq 0 ] && [ $SHA_RC -eq 0 ]; then
    echo "FI_B5: every mutation bit as predicted, every source restored"
    exit 0
fi
echo "FI_B5: FINDING - see MISMATCH / NO_BITE / sha256 lines above"
exit 1
