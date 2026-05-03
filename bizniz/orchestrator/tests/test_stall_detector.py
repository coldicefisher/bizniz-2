import hashlib
import re

from bizniz.orchestrator.stall_detector import StallDetector


PYTEST_OUTPUT_A = """\
FAILED tests/test_math.py::test_add - AssertionError: assert 3 == 4
FAILED tests/test_math.py::test_sub - ValueError: invalid literal
= 2 failed in 0.05s =
"""

PYTEST_OUTPUT_B = """\
FAILED tests/test_io.py::test_read - FileNotFoundError: no such file
= 1 failed in 0.02s =
"""

PYTEST_OUTPUT_ERROR_LINE = """\
some setup output
ERROR collecting tests/test_broken.py
"""

PYTEST_OUTPUT_NO_MARKERS = """\
some generic output
all good here
final line
"""


def test_no_stall_initially():
    sd = StallDetector()
    assert sd.is_stalled is False
    assert sd.stall_reason == "not stalled"
    assert sd.repair_history == []


def test_code_hash_stall():
    sd = StallDetector(code_hash_threshold=2)
    # First call sets the hash; repeat count stays 0
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    assert sd.is_stalled is False

    # Second call with same hash: repeat count = 1
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    assert sd.is_stalled is False

    # Third call with same hash: repeat count = 2, hits threshold
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    assert sd.is_stalled is True
    assert "code unchanged" in sd.stall_reason


def test_code_hash_reset_on_different():
    sd = StallDetector(code_hash_threshold=2)
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    # repeat count is 1; now a different hash resets it
    sd.record_failure("def", PYTEST_OUTPUT_B)
    assert sd._code_hash_repeat_count == 0
    # Should not be stalled from code hash signal alone
    # (may still be stalled from consecutive failures if threshold is low)
    sd2 = StallDetector(code_hash_threshold=2, consecutive_fail_threshold=100)
    sd2.record_failure("abc", PYTEST_OUTPUT_A)
    sd2.record_failure("abc", PYTEST_OUTPUT_A)
    sd2.record_failure("def", PYTEST_OUTPUT_B)
    assert sd2.is_stalled is False


def test_error_signature_stall():
    sd = StallDetector(error_sig_threshold=3, consecutive_fail_threshold=100)
    # Record failures with different code hashes but same error output
    for i in range(3):
        sd.record_failure(f"hash_{i}", PYTEST_OUTPUT_A)
    assert sd.is_stalled is True
    assert "same error pattern repeated" in sd.stall_reason


def test_consecutive_failure_stall():
    sd = StallDetector(consecutive_fail_threshold=3, code_hash_threshold=100, error_sig_threshold=100)
    sd.record_failure("a", PYTEST_OUTPUT_A)
    sd.record_failure("b", PYTEST_OUTPUT_B)
    assert sd.is_stalled is False
    sd.record_failure("c", PYTEST_OUTPUT_A)
    assert sd.is_stalled is True
    assert "3 consecutive failures" in sd.stall_reason


def test_record_success_resets_counters():
    sd = StallDetector(code_hash_threshold=2, error_sig_threshold=3, consecutive_fail_threshold=5)
    # Accumulate some state
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    assert sd._consecutive_failures == 2
    assert sd._code_hash_repeat_count == 1

    sd.record_success()

    assert sd._consecutive_failures == 0
    assert sd._code_hash_repeat_count == 0
    assert sd._error_signature_counts == {}
    assert sd.is_stalled is False
    # repair_history is NOT cleared
    assert len(sd.repair_history) == 2


def test_reset_counters_keeps_repair_history():
    sd = StallDetector()
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    sd.record_failure("abc", PYTEST_OUTPUT_A)
    assert len(sd.repair_history) == 3

    sd.reset_counters()

    assert sd._code_hash_repeat_count == 0
    assert sd._consecutive_failures == 0
    assert sd._error_signature_counts == {}
    assert sd._previous_code_hash is None
    assert sd.is_stalled is False
    # History preserved
    assert len(sd.repair_history) == 3


def test_reset_counters_keep_error_signatures():
    sd = StallDetector(error_sig_threshold=3, consecutive_fail_threshold=100)
    # Accumulate 2 error sigs (not yet at threshold of 3)
    sd.record_failure("h1", PYTEST_OUTPUT_A)
    sd.record_failure("h2", PYTEST_OUTPUT_A)
    assert not sd.is_stalled

    # Reset but keep error signatures
    sd.reset_counters(keep_error_signatures=True)
    assert sd._consecutive_failures == 0
    assert len(sd._error_signature_counts) > 0  # preserved

    # One more same-error failure should now hit threshold
    sd.record_failure("h3", PYTEST_OUTPUT_A)
    assert sd.is_stalled
    assert "same error pattern repeated" in sd.stall_reason


def test_reset_counters_clear_error_signatures():
    sd = StallDetector(error_sig_threshold=3, consecutive_fail_threshold=100)
    sd.record_failure("h1", PYTEST_OUTPUT_A)
    sd.record_failure("h2", PYTEST_OUTPUT_A)

    # Full reset clears error signatures
    sd.reset_counters(keep_error_signatures=False)
    assert sd._error_signature_counts == {}

    # Need 3 fresh failures to trigger
    sd.record_failure("h3", PYTEST_OUTPUT_A)
    assert not sd.is_stalled


def test_stall_reason_multiple_signals():
    # Use thresholds that will all trigger at the same time
    sd = StallDetector(code_hash_threshold=2, error_sig_threshold=3, consecutive_fail_threshold=3)
    for _ in range(3):
        sd.record_failure("same_hash", PYTEST_OUTPUT_A)

    reason = sd.stall_reason
    assert "code unchanged" in reason
    assert "same error pattern repeated" in reason
    assert "consecutive failures" in reason


def test_repair_history_accumulates():
    sd = StallDetector()
    sd.record_failure("h1", PYTEST_OUTPUT_A)
    sd.record_failure("h2", PYTEST_OUTPUT_B)
    sd.record_failure("h3", PYTEST_OUTPUT_ERROR_LINE)

    history = sd.repair_history
    assert len(history) == 3
    assert history[0].startswith("Attempt 1:")
    assert history[1].startswith("Attempt 2:")
    assert history[2].startswith("Attempt 3:")
    # Verify summaries pick up FAILED/ERROR lines
    assert "FAILED" in history[0]
    assert "FAILED" in history[1]
    assert "ERROR" in history[2]


def test_compute_error_signature():
    sig = StallDetector._compute_error_signature(PYTEST_OUTPUT_A)
    # Should be a hex string (sha256)
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)

    # Same output -> same signature
    assert StallDetector._compute_error_signature(PYTEST_OUTPUT_A) == sig

    # Different output -> different signature
    sig_b = StallDetector._compute_error_signature(PYTEST_OUTPUT_B)
    assert sig_b != sig

    # Verify it extracts the right components
    test_names = sorted(re.findall(r'FAILED\s+([\w/.:]+)', PYTEST_OUTPUT_A))
    error_types = sorted(set(re.findall(r'(\w+Error|\w+Exception)', PYTEST_OUTPUT_A)))
    expected_str = "|".join(test_names) + "||" + "|".join(error_types)
    expected_sig = hashlib.sha256(expected_str.encode()).hexdigest()
    assert sig == expected_sig


def test_record_diagnosis_adds_to_history():
    from bizniz.agents.debugger.types import AgenticDiagnosis, CodeFix
    sd = StallDetector()

    diag = AgenticDiagnosis(
        diagnosis="Import path is wrong",
        root_cause_category="import_error",
        fix_target="code",
        confidence="high",
        fix_plan=["Fix the import in rbac_demo.py"],
        code_fixes=[CodeFix(filepath="app/routes/rbac.py", new_content="from app.core import auth")],
    )
    sd.record_diagnosis(diag)

    assert len(sd.repair_history) == 1
    assert "DEBUGGER DIAGNOSIS" in sd.repair_history[0]
    assert "import_error" in sd.repair_history[0]
    assert "app/routes/rbac.py" in sd.repair_history[0]


def test_is_duplicate_fix_detects_same_fix():
    from bizniz.agents.debugger.types import AgenticDiagnosis, CodeFix
    sd = StallDetector()

    fix = [CodeFix(filepath="app/routes/rbac.py", new_content="from app.core import auth")]
    diag1 = AgenticDiagnosis(
        root_cause_category="import_error", confidence="high", code_fixes=fix,
    )
    diag2 = AgenticDiagnosis(
        root_cause_category="import_error", confidence="high", code_fixes=fix,
    )

    sd.record_diagnosis(diag1)
    assert sd.is_duplicate_fix() is False  # first time

    sd.record_diagnosis(diag2)
    assert sd.is_duplicate_fix() is True  # same fix proposed twice


def test_is_duplicate_fix_allows_different_fixes():
    from bizniz.agents.debugger.types import AgenticDiagnosis, CodeFix
    sd = StallDetector()

    diag1 = AgenticDiagnosis(
        root_cause_category="import_error", confidence="high",
        code_fixes=[CodeFix(filepath="app/routes/rbac.py", new_content="fix A")],
    )
    diag2 = AgenticDiagnosis(
        root_cause_category="import_error", confidence="high",
        code_fixes=[CodeFix(filepath="app/routes/rbac.py", new_content="fix B")],
    )

    sd.record_diagnosis(diag1)
    sd.record_diagnosis(diag2)
    assert sd.is_duplicate_fix() is False  # different content


def test_is_duplicate_fix_no_code_fixes_hashes_diagnosis():
    from bizniz.agents.debugger.types import AgenticDiagnosis
    sd = StallDetector()

    diag = AgenticDiagnosis(
        root_cause_category="import_error",
        diagnosis="same analysis",
        fix_plan=["same plan"],
        confidence="high",
    )
    sd.record_diagnosis(diag)
    sd.record_diagnosis(diag)
    assert sd.is_duplicate_fix() is True


def test_summarize_failure():
    # Output with FAILED line
    summary = StallDetector._summarize_failure(PYTEST_OUTPUT_A)
    assert "FAILED" in summary

    # Output with ERROR line
    summary = StallDetector._summarize_failure(PYTEST_OUTPUT_ERROR_LINE)
    assert "ERROR" in summary

    # Output with no markers - falls back to last non-empty line
    summary = StallDetector._summarize_failure(PYTEST_OUTPUT_NO_MARKERS)
    assert summary == "final line"

    # Empty output
    summary = StallDetector._summarize_failure("")
    assert summary == "(no output)"
