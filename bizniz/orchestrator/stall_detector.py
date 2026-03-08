import re
import hashlib
from typing import Optional, Dict, List


class StallDetector:
    """Detects repair loop stalls via multiple signals."""

    def __init__(
        self,
        code_hash_threshold: int = 2,
        error_sig_threshold: int = 3,
        consecutive_fail_threshold: int = 5,
    ):
        self._code_hash_threshold = code_hash_threshold
        self._error_sig_threshold = error_sig_threshold
        self._consecutive_fail_threshold = consecutive_fail_threshold

        self._previous_code_hash: Optional[str] = None
        self._code_hash_repeat_count: int = 0
        self._error_signature_counts: Dict[str, int] = {}
        self._consecutive_failures: int = 0
        self._repair_history: List[str] = []

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

    def reset_counters(self) -> None:
        """Reset stall counters after escalation. Keeps repair_history for deep diagnosis context."""
        self._previous_code_hash = None
        self._code_hash_repeat_count = 0
        self._error_signature_counts.clear()
        self._consecutive_failures = 0

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
