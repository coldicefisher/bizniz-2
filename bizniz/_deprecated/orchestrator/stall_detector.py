import re
import hashlib
from typing import Optional, Dict, List


class StallDetector:
    """Detects repair loop stalls via multiple signals."""

    def __init__(
        self,
        code_hash_threshold: int = 2,
        error_sig_threshold: int = 3,
        consecutive_fail_threshold: int = 3,
    ):
        self._code_hash_threshold = code_hash_threshold
        self._error_sig_threshold = error_sig_threshold
        self._consecutive_fail_threshold = consecutive_fail_threshold

        self._previous_code_hash: Optional[str] = None
        self._code_hash_repeat_count: int = 0
        self._error_signature_counts: Dict[str, int] = {}
        self._consecutive_failures: int = 0
        self._repair_history: List[str] = []
        self._debugger_fix_hashes: List[str] = []

    def record_failure(self, code_hash: str, failure_output: str) -> None:
        """Record a failed repair attempt. Updates all tracking state."""
        # Code hash repeats
        if code_hash == self._previous_code_hash:
            self._code_hash_repeat_count += 1
        else:
            self._code_hash_repeat_count = 0
        self._previous_code_hash = code_hash

        # Error signature
        sig = self._compute_error_signature(failure_output)
        self._error_signature_counts[sig] = self._error_signature_counts.get(sig, 0) + 1

        # Consecutive failures
        self._consecutive_failures += 1

        # Repair history (keep summary)
        summary = self._summarize_failure(failure_output)
        self._repair_history.append(f"Attempt {len(self._repair_history) + 1}: {summary}")

    def record_success(self) -> None:
        """Reset counters on success."""
        self._consecutive_failures = 0
        self._code_hash_repeat_count = 0
        self._error_signature_counts.clear()

    @property
    def is_stalled(self) -> bool:
        """Returns True if any stall signal has hit its threshold."""
        return (
            self._code_hash_repeat_count >= self._code_hash_threshold
            or any(c >= self._error_sig_threshold for c in self._error_signature_counts.values())
            or self._consecutive_failures >= self._consecutive_fail_threshold
        )

    @property
    def stall_reason(self) -> str:
        """Human-readable reason for the stall."""
        reasons = []
        if self._code_hash_repeat_count >= self._code_hash_threshold:
            reasons.append(f"code unchanged {self._code_hash_repeat_count} times")
        for sig, count in self._error_signature_counts.items():
            if count >= self._error_sig_threshold:
                reasons.append(f"same error pattern repeated {count} times")
        if self._consecutive_failures >= self._consecutive_fail_threshold:
            reasons.append(f"{self._consecutive_failures} consecutive failures")
        return "; ".join(reasons) or "not stalled"

    @property
    def repair_history(self) -> List[str]:
        """Return list of repair attempt summaries."""
        return list(self._repair_history)

    def record_diagnosis(self, diagnosis) -> None:
        """Append a debugger diagnosis + fixes to the repair history
        so the next debugger invocation sees what was already tried.

        Also records a hash of the code_fixes for duplicate detection.
        """
        parts = [f"DEBUGGER DIAGNOSIS: {diagnosis.root_cause_category} "
                 f"(confidence: {diagnosis.confidence})"]
        if diagnosis.diagnosis:
            parts.append(f"  Analysis: {diagnosis.diagnosis[:300]}")
        if diagnosis.fix_plan:
            parts.append("  Fix plan: " + "; ".join(diagnosis.fix_plan[:5]))
        if diagnosis.code_fixes:
            fix_summary = []
            for cf in diagnosis.code_fixes:
                preview = cf.new_content[:100].replace("\n", " ")
                fix_summary.append(f"    {cf.filepath}: {preview}...")
            parts.append("  Code fixes applied:\n" + "\n".join(fix_summary))

        self._repair_history.append("\n".join(parts))

        # Hash the code_fixes for duplicate detection
        if diagnosis.code_fixes:
            fix_sig = "|".join(
                f"{cf.filepath}:{hashlib.sha256(cf.new_content.encode()).hexdigest()}"
                for cf in sorted(diagnosis.code_fixes, key=lambda f: f.filepath)
            )
        else:
            # No code fixes — hash the diagnosis text + fix plan
            fix_sig = f"{diagnosis.root_cause_category}|{diagnosis.diagnosis}|{'|'.join(diagnosis.fix_plan)}"
        self._debugger_fix_hashes.append(
            hashlib.sha256(fix_sig.encode()).hexdigest()
        )

    def is_duplicate_fix(self) -> bool:
        """True if the most recent debugger fix matches any prior one."""
        if len(self._debugger_fix_hashes) < 2:
            return False
        latest = self._debugger_fix_hashes[-1]
        return latest in self._debugger_fix_hashes[:-1]

    def reset_counters(self, keep_error_signatures: bool = False) -> None:
        """Reset stall counters. Keeps repair_history and debugger_fix_hashes always.

        Parameters
        ----------
        keep_error_signatures:
            If True, preserve error signature counts so the detector
            fires immediately if the same error recurs. Use this after
            the agentic debugger runs (same model, same problem — don't
            waste 3 more iterations re-confirming). Pass False (default)
            on model escalation where the new model deserves a clean slate.
        """
        self._previous_code_hash = None
        self._code_hash_repeat_count = 0
        self._consecutive_failures = 0
        if not keep_error_signatures:
            self._error_signature_counts.clear()

    @staticmethod
    def _compute_error_signature(failure_output: str) -> str:
        """Hash of failing test names + error types from pytest output."""
        test_names = sorted(re.findall(r'FAILED\s+([\w/.:]+)', failure_output))
        error_types = sorted(set(re.findall(r'(\w+Error|\w+Exception)', failure_output)))
        sig_str = "|".join(test_names) + "||" + "|".join(error_types)
        return hashlib.sha256(sig_str.encode()).hexdigest()

    @staticmethod
    def _summarize_failure(failure_output: str) -> str:
        """Extract a short summary from pytest failure output."""
        # Try to find the short test summary line
        lines = failure_output.strip().splitlines()
        for line in reversed(lines):
            if "FAILED" in line or "ERROR" in line:
                return line.strip()[:200]
        # Fallback: last non-empty line
        for line in reversed(lines):
            if line.strip():
                return line.strip()[:200]
        return "(no output)"
