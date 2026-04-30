# Orchestrator Internals

`bizniz/orchestrator/`. Internal helpers that support `CodingOrchestrator` (documented separately at [agents/coding_orchestrator.md](../agents/coding_orchestrator.md)).

## Files

| File | Class / contents | Purpose |
|------|------------------|---------|
| `coding_orchestrator.py` | `CodingOrchestrator` | The repair loop |
| `model_progression.py` | `ModelProgression`, `DEFAULT_PROGRESSION` | Cheapest → most-capable escalation list |
| `stall_detector.py` | `StallDetector` | Detects when the loop is stuck |
| `strategy.py` | `CodingStrategy` enum | `TDD` vs `CODE_FIRST` |
| `types.py` | `OrchestratorResult`, `TestRunResult`, errors | Result and error types |

## `ModelProgression`

`bizniz/orchestrator/model_progression.py`. An ordered list of model names plus a current pointer.

```python
DEFAULT_PROGRESSION = [
    "gpt-4o-mini", "gpt-4o", "gpt-5",
    "claude-sonnet", "claude-opus",
]
```

| Member | Returns | Notes |
|--------|---------|-------|
| `__init__(models=None)` | — | Empty list raises `ValueError` |
| `current_model` | `str` | The model at the current index |
| `is_at_max` | `bool` | True when at the last index |
| `escalate()` | `Optional[str]` | Returns the new model or None if already at max |
| `reset()` | None | Back to index 0 |
| `set_start(name)` | None | Set the index to the given model name; silently no-op if not present |

The orchestrator builds three of these (coder, tester, repair) plus an optional shared one. `BiznizConfig.make_*_progression()` constructs them from the YAML.

## `StallDetector`

`bizniz/orchestrator/stall_detector.py`. Detects loop stalls via three signals.

```python
StallDetector(
    code_hash_threshold=2,         # same code hash this many times → stall
    error_sig_threshold=3,         # same failing-tests+error-types signature N times → stall
    consecutive_fail_threshold=3,  # N consecutive failures → stall
)
```

| Method | Purpose |
|--------|---------|
| `record_failure(code_hash, failure_output)` | Updates all three counters and appends to `repair_history` |
| `record_success()` | Resets `consecutive_failures` and clears the error signature counters |
| `is_stalled` | True if any threshold is hit |
| `stall_reason` | Human-readable reason (or `"not stalled"`) |
| `repair_history` | List of one-line failure summaries (`"Attempt N: ..."`), preserved across `reset_counters` |
| `reset_counters()` | Reset stall counters but KEEP `repair_history` for deep diagnosis context |

Internal:

- `_compute_error_signature(output)` — SHA-256 of sorted failing test names + sorted error/exception type names from pytest output.
- `_summarize_failure(output)` — finds the last `FAILED` / `ERROR` line (≤200 chars) for the history entry.

The orchestrator's stall-recovery flow:

```
record_failure(...)
if is_stalled:
    log(stall_reason)
    next_model = progression.escalate()
    if next_model:
        client.set_model(next_model)         # or rebuild via client_factory
        reset_counters()                     # keep repair_history!
        # next iteration uses the new model
    else:
        # already at max model — let max_iterations / wall-clock handle the exit
```

## `CodingStrategy`

`bizniz/orchestrator/strategy.py`. Just an `Enum`:

```python
class CodingStrategy(str, Enum):
    TDD = "tdd"             # tests first, fix code only
    CODE_FIRST = "code_first"  # code first, fix either
```

Used by the engineer to retry an issue with a different strategy (CODE_FIRST → TDD) and by callers to override the default in `run_multi(strategy=...)`.

## `OrchestratorResult` and friends

`bizniz/orchestrator/types.py`:

```python
class TestRunResult(BaseModel):
    all_passed: bool
    total_tests: int = 0
    passed: int = 0
    failed: int = 0
    failing_test_files: List[str] = []
    regression_files: List[str] = []  # tests passing before, failing now
    stdout: str = ""

class OrchestratorResult(BaseModel):
    success: bool
    changes: List[FileChange] = []
    test_files: List[GeneratedTestFile] = []
    iterations: int = 0
    error: Optional[str] = None
    failure_context: Optional[str] = None        # last failure output for retry strategies
    strategy_used: Optional[str] = None          # "tdd" or "code_first"
    architecture_drift_detected: bool = False
    drift_files: List[str] = []                   # coder-created files not in plan

class OrchestratorStalledError(Exception): ...
class OrchestratorMaxIterationsError(Exception): ...
```

`OrchestratorMaxIterationsError` is what bubbles up when the loop hits `max_iterations` without success — the engineer's `_run_orchestrator` catches it and produces a failed `OrchestratorResult` so the retry chain (`dispatch`) can keep going.

## Example: building progressions from config

```python
from bizniz.config.bizniz_config import BiznizConfig
from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator

cfg = BiznizConfig.find_and_load()

orchestrator = CodingOrchestrator(
    coder=...,
    tester=...,
    test_environment=...,
    workspace=...,
    quick_debugger=...,
    client=...,
    coder_progression=cfg.make_autocoder_progression(),
    tester_progression=cfg.make_autotester_progression(),
    repair_progression=cfg.make_repair_progression(),
    stall_threshold=cfg.stall_threshold,
    agentic_debug_threshold=cfg.agentic_debug_threshold,
    max_iterations=cfg.max_iterations,
    enable_agentic_debug=cfg.enable_agentic_debug,
    stall_recovery=cfg.stall_recovery,
)
```

## Interactions

- **Used by:** `CodingOrchestrator`, `Engineer._run_orchestrator`, `BiznizConfig.make_*_progression`.
- **Calls into:** nothing external — these are pure utility classes.

## Gotchas

- **`set_start` is silent on miss.** If the engineer suggests a model not in the progression, escalation still starts from index 0. Match progression lists to your engineer's `available_models`.
- **`StallDetector.repair_history` survives `reset_counters`.** This is on purpose — the deep diagnosis pass needs the history. If you want a clean slate, build a new detector.
- **`is_stalled` is OR over three signals.** Even one error-signature repeat above `error_sig_threshold` triggers a stall. Set the thresholds based on how aggressive you want escalation to be.
- **`OrchestratorResult.success` and the result type are NOT mutually exclusive with `OrchestratorMaxIterationsError`.** When the orchestrator throws max-iterations, the engineer constructs a failed result manually. So if you call `run_multi` directly, you can get either an exception or `success=False`. Both shapes need handling.
- **`drift_files` is set by the orchestrator, but governance review runs in the engineer.** The orchestrator only flags drift; the engineer's `review_drift` decides what to do.
