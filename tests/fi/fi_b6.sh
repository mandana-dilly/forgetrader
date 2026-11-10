#!/usr/bin/env bash
# Brief B6 Tier 1 fault injection (Stage 2 consumer: forgetrader/submit.py
# place_cc, forgetrader/run.py HOLDING branch + shared _submit_gate,
# forgetrader_state.py _put_strike_basis hardening).
# Run AS forgedev from anywhere:  bash tests/fi/fi_b6.sh
# Per mutation: pristine copy in /tmp, sed -i ONE line-addressed mutation,
# print mutated_lines (must be 1: anything else is a MISMATCH), run ALL
# suites, compare each suite's failure count with the PREDICTED count
# registered below, cp restore from pristine, sha256sum -c. Ends with
# sha256sum -c on every mutated source (plus the real state.json / journal.db
# if they exist; the forgedev clone has neither).
# Predictions were committed before the first run and are never edited after.
# A prediction of "guard" registers an expected NO_BITE (every suite 0): the
# mutated line is a regression guard that no single-line mutation can bite.
set -u
cd "$(dirname "$0")/../.." || exit 1
PY=.venv/bin/python
PRISTINE=$(mktemp -d /tmp/fi_b6.XXXXXX)
SOURCES="forgetrader/submit.py forgetrader/run.py forgetrader_state.py"
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

# mutate ID FILE LINE SED_EXPR "suite=N ..." | guard  DESCRIPTION
# Suites not named in the prediction are predicted 0.
mutate() {
    local id=$1 file=$2 line=$3 expr=$4 pred=$5 desc=$6
    local base t name want got rc out bit=0 ok=1 nlines guard=0
    [ "$pred" = guard ] && guard=1
    base=$(basename "$file")
    echo
    echo "=== $id $desc"
    echo "    $file:$line  sed -i '${line}${expr}'"
    echo "    original line: $(sed -n "${line}p" "$file" | sed 's/^ *//')"
    echo "    predicted: $pred (every other suite 0)"
    sed -i "${line}${expr}" "$file"
    nlines=$(diff "$PRISTINE/$base" "$file" | grep -c '^>')
    if [ "$nlines" = 1 ]; then
        echo "    mutated_lines=$nlines (expect 1) match"
    else
        echo "    mutated_lines=$nlines (expect 1) MISMATCH"
        ok=0
    fi
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
    if [ $guard -eq 1 ]; then
        [ $bit -eq 0 ] && bitword="NO_BITE (registered REGRESSION_GUARD)" || ok=0
    elif [ $bit -eq 0 ]; then
        ok=0
    fi
    [ $ok -eq 1 ] && matchword=all_predictions_match || { matchword=PREDICTION_MISMATCH; MISMATCH=1; }
    echo "    $id RESULT: $bitword $matchword"
    SUMMARY="$SUMMARY
$id $bitword $matchword"
}

# ---- M1: place_cc writes CC_OPEN instead of CC_PENDING ---------------------
# The (already None) strike reset line in place_cc's build becomes a stage
# overwrite, so the write-ahead state and the returned state are CC_OPEN.
mutate M1 forgetrader/submit.py 216 's/wheel\["strike"\] = None/wheel["stage"] = fs.CC_OPEN/' \
    "place_cc=8 run_stages=5" \
    "place_cc write-ahead stage CC_OPEN (PC P1 disk 1, P2 call-time disk 1, P3 returned 1, P4 disk 1, P5 disk x2 2, P6 returned 1, P9 disk 1; RS H2 stage, write-ahead, stage_to, alert 4, H11 stage 1)"

# ---- M2: limit = ask -------------------------------------------------------
mutate M2 forgetrader/submit.py 223 's/limit_price=limit_price,/limit_price=proposal["ask"],/' \
    "place_cc=2 run_stages=1" \
    "place_cc request limit is the ask (PC P1 request limit 1, P9 seam limit 1; RS H2 limit 1)"

# ---- M3: live gate drops the is_open check ---------------------------------
mutate M3 forgetrader/run.py 156 's/if is_open is not True:/if False:/' \
    "live_submit=4 run_stages=8" \
    "_submit_gate ignores is_open (LS C is_open False/None/1 3 + HOLDING is_open False 1; RS L6 submit, runs, stdout, wheel 4 + H3 submit, runs, stdout, wheel 4)"

# ---- M4: HOLDING scan trigger startswith instead of == ---------------------
mutate M4 forgetrader/run.py 359 's/elif action == "noop:HOLDING stable":/elif action.startswith("noop:HOLDING"):/' \
    "run_stages=4" \
    "near-miss action scans (RS H10 scan called, decisions 2; H10L scan called, runs 2)"

# ---- M5: right check accepts P ---------------------------------------------
mutate M5 forgetrader/submit.py 168 's/if sym\[-9\] != "C":/if sym[-9] not in ("C", "P"):/' \
    "place_cc=6" \
    "a put passes place_cc (PC P7 right P: outcome, detail, submit count, state.json, orders, input state 6)"

# ---- M6: isdigit check removed from _put_strike_basis ----------------------
# REGRESSION GUARD: _contract_expiry (forgetrader_state.py, called on the
# next line) also refuses a non-digit strike field with the same message, so
# NOK261218P0001e+03 still HALTs "cannot parse strike". Predicted NO_BITE.
mutate M6 forgetrader_state.py 143 's/ or not contract\[-8:\]\.isdigit()//' \
    "guard" \
    "_put_strike_basis without its own isdigit check (double-guarded by _contract_expiry: no suite can bite)"

# ---- M7: chain fetch error event not recorded ------------------------------
mutate M7 forgetrader/run.py 377 's/fs.record_event(conn, run_id, "chain_fetch_error", symbol=ticker, detail=errs)/pass/' \
    "run_stages=1" \
    "chain_fetch_error event dropped (RS H6 event 1)"

# ---- M8: qty 2 -------------------------------------------------------------
mutate M8 forgetrader/submit.py 141 's/^CC_QTY = 1 /CC_QTY = 2 /' \
    "place_cc=2 run_stages=1" \
    "place_cc qty 2 (PC P1 orders rows qty 1, request qty 1; RS H2 request qty 1)"

echo
echo "=== SUMMARY"
echo "$SUMMARY" | sed '/^$/d'
echo
echo "=== FINAL sha256sum -c (every mutated source restored)"
sha256sum -c "$PRISTINE/sources.sha256"; SHA_RC=$?
echo "sha256sum exit=$SHA_RC"
if [ $MISMATCH -eq 0 ] && [ $SHA_RC -eq 0 ]; then
    echo "FI_B6: every mutation bit as predicted (M6 registered guard), every source restored"
    exit 0
fi
echo "FI_B6: FINDING - see MISMATCH / NO_BITE / sha256 lines above"
exit 1
