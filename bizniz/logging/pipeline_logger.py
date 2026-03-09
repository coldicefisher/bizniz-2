"""
PipelineLogger

Structured JSON logging for the bizniz pipeline. Each run produces a log file
that can be reviewed to diagnose failures, track model usage, and understand
pipeline performance.

Logs are written to {workspace_root}/.bizniz/logs/ as JSON files.
"""

import json
import datetime
from pathlib import Path
from typing import Optional, List


class PipelineLogger:
    """Writes structured JSON events to a per-run log file."""

    def __init__(self, log_dir: str, run_id: Optional[str] = None):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._run_id = run_id or datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y%m%d_%H%M%S")
        self._log_path = self._log_dir / f"run_{self._run_id}.jsonl"
        self._events: List[dict] = []

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def run_id(self) -> str:
        return self._run_id

    def log(self, event_type: str, **kwargs):
        """Log a structured event."""
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "event": event_type,
            **kwargs,
        }
        self._events.append(entry)
        with open(self._log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

    def log_run_start(self, problem_statement: str):
        self.log("run_start", problem_statement=problem_statement)

    def log_run_end(self, success: bool, total_issues: int, resolved: int, failed: int):
        self.log(
            "run_end",
            success=success,
            total_issues=total_issues,
            resolved=resolved,
            failed=failed,
        )

    def log_issue_start(self, issue_id: int, title: str, suggested_model: Optional[str] = None):
        self.log("issue_start", issue_id=issue_id, title=title, suggested_model=suggested_model)

    def log_issue_end(self, issue_id: int, success: bool, iterations: int):
        self.log("issue_end", issue_id=issue_id, success=success, iterations=iterations)

    def log_model_escalation(self, issue_id: int, from_model: str, to_model: str, reason: str = ""):
        self.log("model_escalation", issue_id=issue_id, from_model=from_model, to_model=to_model, reason=reason)

    def log_stall_detected(self, issue_id: int, reason: str):
        self.log("stall_detected", issue_id=issue_id, reason=reason)

    def log_deep_diagnosis(self, issue_id: int, root_cause_category: str, fix_target: str, confidence: str, root_cause: str = ""):
        self.log(
            "deep_diagnosis",
            issue_id=issue_id,
            root_cause_category=root_cause_category,
            fix_target=fix_target,
            confidence=confidence,
            root_cause=root_cause,
        )

    def log_error(self, issue_id: Optional[int], error_type: str, message: str):
        self.log("error", issue_id=issue_id, error_type=error_type, message=message)

    def log_package_install(self, package: str):
        self.log("package_install", package=package)

    def get_summary(self) -> dict:
        """Return a summary of the run from logged events."""
        issues_started = [e for e in self._events if e["event"] == "issue_start"]
        issues_ended = [e for e in self._events if e["event"] == "issue_end"]
        errors = [e for e in self._events if e["event"] == "error"]
        escalations = [e for e in self._events if e["event"] == "model_escalation"]
        stalls = [e for e in self._events if e["event"] == "stall_detected"]
        diagnoses = [e for e in self._events if e["event"] == "deep_diagnosis"]

        resolved = sum(1 for e in issues_ended if e.get("success"))
        failed = sum(1 for e in issues_ended if not e.get("success"))
        total_iterations = sum(e.get("iterations", 0) for e in issues_ended)

        return {
            "run_id": self._run_id,
            "total_issues": len(issues_started),
            "resolved": resolved,
            "failed": failed,
            "total_iterations": total_iterations,
            "escalations": len(escalations),
            "stalls": len(stalls),
            "deep_diagnoses": len(diagnoses),
            "errors": len(errors),
            "log_path": str(self._log_path),
        }

    @classmethod
    def load_summary(cls, log_path: str) -> dict:
        """Load and summarize a previous run's log file."""
        logger = cls.__new__(cls)
        logger._events = []
        logger._log_path = Path(log_path)
        logger._run_id = Path(log_path).stem.replace("run_", "")
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    logger._events.append(json.loads(line))
        return logger.get_summary()
