#!/usr/bin/env bash
# Run auto_engineer 5 times and report results
set -o pipefail

RESULTS=()
NUM_RUNS=${1:-3}
for i in $(seq 1 $NUM_RUNS); do
    echo ""
    echo "================================================================"
    echo "  STABILITY TEST RUN $i / $NUM_RUNS"
    echo "================================================================"
    echo ""

    START=$(date +%s)
    python3 examples/auto_engineer.py 2>&1 | tee "/tmp/stability_run_${i}.log"
    EXIT_CODE=${PIPESTATUS[0]}
    END=$(date +%s)
    DURATION=$((END - START))

    if [ $EXIT_CODE -eq 0 ]; then
        # Check if all issues resolved by looking at the summary
        if grep -q "0 failed" "/tmp/stability_run_${i}.log" 2>/dev/null || grep -q "failed: 0" "/tmp/stability_run_${i}.log" 2>/dev/null; then
            RESULTS+=("Run $i: PASS (${DURATION}s)")
            echo ""
            echo ">>> Run $i: PASS (${DURATION}s)"
        else
            RESULTS+=("Run $i: PARTIAL (${DURATION}s, exit=0 but check log)")
            echo ""
            echo ">>> Run $i: PARTIAL (${DURATION}s)"
        fi
    else
        RESULTS+=("Run $i: FAIL (${DURATION}s, exit=$EXIT_CODE)")
        echo ""
        echo ">>> Run $i: FAIL (${DURATION}s, exit=$EXIT_CODE)"
    fi
done

echo ""
echo "================================================================"
echo "  STABILITY TEST SUMMARY"
echo "================================================================"
for r in "${RESULTS[@]}"; do
    echo "  $r"
done
echo "================================================================"
