# Logging

`bizniz/logging/pipeline_logger.py`. Structured JSON event logging for pipeline runs.

## Purpose

Each pipeline run produces a JSONL file at `<workspace>/.bizniz/logs/run_<id>.jsonl`. Each line is a structured event (`run_start`, `issue_start`, `model_escalation`, `error`, etc.). The log file can be summarized after the fact via `PipelineLogger.load_summary(path)`.

## API

```python
class PipelineLogger:
    def __init__(self, log_dir: str, run_id: Optional[str] = None):
        ...
    log_path: Path           # property
    run_id: str              # property

    def log(self, event_type: str, **kwargs) -> None
    def get_summary(self) -> dict
    @classmethod
    def load_summary(cls, log_path: str) -> dict
```

| Convenience method | Event type emitted |
|--------------------|--------------------|
| `log_run_start(problem_statement)` | `run_start` |
| `log_run_end(success, total_issues, resolved, failed)` | `run_end` |
| `log_issue_start(issue_id, title, suggested_model=None)` | `issue_start` |
| `log_issue_end(issue_id, success, iterations)` | `issue_end` |
| `log_model_escalation(issue_id, from_model, to_model, reason="")` | `model_escalation` |
| `log_stall_detected(issue_id, reason)` | `stall_detected` |
| `log_deep_diagnosis(issue_id, root_cause_category, fix_target, confidence, root_cause="")` | `deep_diagnosis` |
| `log_error(issue_id, error_type, message)` | `error` |
| `log_package_install(package)` | `package_install` |

Every event line is:

```json
{
  "timestamp": "2026-04-29T18:30:42+00:00",
  "event": "<event_type>",
  ... user kwargs ...
}
```

## `get_summary()` and `load_summary()`

Both return a dict keyed:

```python
{
    "run_id": str,
    "total_issues": int,
    "resolved": int,
    "failed": int,
    "total_iterations": int,
    "escalations": int,
    "stalls": int,
    "deep_diagnoses": int,
    "errors": int,
    "log_path": str,
}
```

`get_summary` works on the in-memory event list of a live `PipelineLogger`. `load_summary(path)` rebuilds an `_events` list from a saved JSONL file and computes the same dict.

## Example

```python
from bizniz.logging.pipeline_logger import PipelineLogger

logger = PipelineLogger(log_dir="/tmp/.bizniz/logs", run_id="20260429_test")
logger.log_run_start("Build a TODO app")
logger.log_issue_start(issue_id=1, title="Create User model", suggested_model="gpt-4o-mini")
logger.log_model_escalation(issue_id=1, from_model="gpt-4o-mini", to_model="gpt-4o", reason="stall")
logger.log_issue_end(issue_id=1, success=True, iterations=5)
logger.log_run_end(success=True, total_issues=1, resolved=1, failed=0)

print(logger.get_summary())

# Later:
summary = PipelineLogger.load_summary(str(logger.log_path))
```

## Interactions

- **Used by:** the pipeline entrypoint (the script that wires up architect → engineer → orchestrator) — currently optional. `BaseAIAgent` doesn't reach into the pipeline logger; orchestration code that does want auditing constructs one explicitly.
- **Calls into:** stdlib `json`, `pathlib`, `datetime`.

## Gotchas

- **Each event is appended one line at a time.** `f.write(json.dumps(entry) + "\n")` per call — no buffering. That's safe under crashes but slower for high-frequency events.
- **`run_id` is auto-generated from UTC timestamp** if not supplied: `YYYYMMDD_HHMMSS`. Two runs starting in the same second will collide; pass an explicit `run_id` for parallel pipelines.
- **`load_summary` does NOT replay events.** It just rebuilds the counters. Detailed forensics requires reading the JSONL by hand or with `jq`.
- **The logger is purely additive.** No event TTL, no rotation. Old `.jsonl` files accumulate under `.bizniz/logs/` until you delete them.
