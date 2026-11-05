#!/usr/bin/env bash
# Brief 2 Tier 1 fault injection (run.py --live + one broker clock into the scan).
# Run AS forgedev from anywhere:  bash tests/fi/fi_b2.sh
# Per mutation: pristine copy in /tmp, sed -i ONE line-addressed mutation,
# print mutated_lines (expect 1), run ALL suites, compare each suite's failure
# count with the PREDICTED count registered below, cp restore from pristine,
# sha256sum -c. Ends with sha256sum -c on every mutated source (plus the real
# state.json / journal.db if they exist; the forgedev clone has neither).
# Predictions were committed before the first run and are never edited after.
set -u
cd "$(dirname "$0")/../.." || exit 1
PY=.venv/bin/python
PRISTINE=$(mktemp -d /tmp/fi_b2.XXXXXX)
SOURCES="forgetrader/run.py wheel_dryrun.py"
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

# ---- B2: --live paper check before any broker read ------------------------
mutate M1 forgetrader/run.py 163 's/if live is True and paper is not True:/if False:/' \
    "run_stages=8" \
    "B2 not_paper refusal removed (L3: clock read + exit 0; L7: clock, exit, broker calls, state write, stderr, scan)"

# ---- B3: each submit-gate condition ----------------------------------------
mutate M2 forgetrader/run.py 102 's/if paper is not True:/if False:/' \
    "live_submit=2" \
    "B3 gate paper condition removed (gate unit: paper False, paper 'True'; run level is caught earlier by B2)"
mutate M3 forgetrader/run.py 100 's/if live is not True:/if False:/' \
    "live_submit=2 propose_csp=1 run_stages=4" \
    "B3 gate live condition removed (gate unit 2; propose_csp extra decision row; L2 tripwire, exit, outcome, wheel)"
mutate M4 forgetrader/run.py 104 's/if action != "noop:FLAT stable":/if False:/' \
    "live_submit=2" \
    "B3 gate action condition removed (gate unit: transition, trailing space; run level scans only on FLAT stable)"
mutate M5 forgetrader/run.py 106 's/if is_open is not True:/if False:/' \
    "live_submit=3 run_stages=4" \
    "B3 gate is_open condition removed (gate unit False/None/1; L6 submits: calls, outcome, stdout, wheel)"
mutate M6 forgetrader/run.py 108 's/is not True:/is None:/' \
    "live_submit=2 run_stages=2" \
    "B3 gate final_pass test weakened to 'is None' (gate unit False/1; L5a/L5b reach place_csp, refused there)"

# ---- B5: the state place_csp returns is the state persisted ----------------
mutate M7 forgetrader/run.py 274 's/new_state = state_out  # persisted on every outcome/pass/' \
    "run_stages=4" \
    "B5 state_out dropped (L1 wheel, opened_at, decision stage_to; L8 wheel)"
mutate M8 forgetrader/run.py 290 's/(conn, run_id, new_state, token, mode)/(conn, run_id, state, token, mode)/' \
    "run_stages=1" \
    "B5 HALT:submit persists the loaded state (L8 wheel; money still refreshed because reconcile noop returns the same object)"

# ---- B6: broker status printed and journalled ------------------------------
mutate M9 forgetrader/run.py 277 's/ status=/ stat=/' \
    "run_stages=4" \
    "B6 status token renamed (L1 runs, last_run, stdout, decision action)"

# ---- B1: one clock --------------------------------------------------------
mutate M10 wheel_dryrun.py 265 's/if clock is None:/if True:/' \
    "live_submit=2" \
    "B1 scan re-reads get_clock when given a clock (scan clock count, ScanResult.clock identity)"
mutate M11 forgetrader/run.py 234 's/clock=clock)/clock=None)/' \
    "run_stages=13" \
    "B1 run.py does not pass its clock (T6 check in all 13 scenarios that reach the scan)"

# ---- T2: dry-run tripwire --------------------------------------------------
mutate M12 forgetrader/run.py 269 's/_submit_gate(live, /_submit_gate(True, /' \
    "propose_csp=1 run_stages=4" \
    "T2 dry-run treated as live at the gate (L2 tripwire, exit, outcome, wheel; propose_csp extra decision row)"

# ---- B7: mode prefix incl. state_missing ----------------------------------
mutate M13 forgetrader/run.py 137 's/f"{mode}:HALT:state_missing"/"HALT:state_missing"/' \
    "run_stages=5" \
    "B7 state_missing loses its prefix (S6 and S6L: T7 + runs each; T7 aggregate)"

echo
echo "=== SUMMARY"
echo "$SUMMARY" | sed '/^$/d'
echo
echo "=== FINAL sha256sum -c (every mutated source restored)"
sha256sum -c "$PRISTINE/sources.sha256"; SHA_RC=$?
echo "sha256sum exit=$SHA_RC"
if [ $MISMATCH -eq 0 ] && [ $SHA_RC -eq 0 ]; then
    echo "FI_B2: every mutation bit as predicted, every source restored"
    exit 0
fi
echo "FI_B2: FINDING - see MISMATCH / NO_BITE / sha256 lines above"
exit 1
