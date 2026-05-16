# Agent Error-Path Audit — Item 5

**Status:** Roadmap item 5, in progress.

**Goal:** Every `raise` in every agent classified, every lenient
path pinned by a test. Triggered by three CRM v1 M5 crashes on
2026-05-15 / 2026-05-16, all the same class: a strict raise in
side-channel code halts the whole pipeline.

## Philosophy (the four classifications)

Each `raise` falls into one of four buckets:

| Category | Action | When to use |
|---|---|---|
| **fatal** | Keep raising. Build halts. | Config invalid, primary-path contract violation, truly unrecoverable. Greenfield Planner/Architect failure is fatal — without a plan, nothing downstream works. |
| **lenient** | Drop / repair / log + continue. | LLM-emitted bad data in side-channel code (repair iters, integration debug, UX fix dispatch). Losing one bad input is preferable to crashing the milestone. |
| **transient** | Retry-with-backoff, then surface. | Upstream infrastructure failure (Anthropic 5xx, rate limit, readonly DB, network blip). `call_with_retry` (commit `ee81331`) handles this for single-call agents. |
| **auto-fill** | Default value + log. | Empty/missing optional field. The `_validate_files_non_empty` reference impl in `service_planner/agent.py:266` is the canonical example. |

**Reference impl for the lenient pattern:** `ServicePlanner._validate_files_non_empty`
and `ServicePlanner._repair_dep_targets` (both in
`bizniz/service_planner/agent.py`). Canonical docstring: *"Repair
iterations are a side-channel. Losing one fix-issue is better than
crashing the milestone."*

**Reference impl for the transient pattern:** `bizniz/lib/llm_utils.py`'s
`call_with_retry` with separate `max_attempts` (permanent) and
`max_transient_attempts` (transient with `30/90/300/600/1800/3600s`
backoff schedule).

## Live crash log this informed the audit

- **Crash 1 (2026-05-15)** — `ProjectDB.mark_finished` raised
  `OperationalError: readonly database` from a stale sqlite
  connection. Fix: `_RetryingConnection` wrapper, commit `9258835`.
  Classification: **transient**. Audit action: complete.
- **Crash 2 (2026-05-16)** — `ServicePlanner.repair(backend, iter1)`
  LLM emitted `BA-fix1-3 depends_on=['BA-fix1-2']` without emitting
  `BA-fix1-2`. Fix: `_repair_dep_targets` drops bad edges with a
  warning, commit `f24b5d7`. Classification: **lenient**. Audit
  action: complete.
- **Crash 3 (2026-05-16)** — `ServicePlanner.repair(frontend, iter1)`
  hit Anthropic HTTP 500s on all 3 retry attempts in 70 seconds.
  Fix: separate transient retry budget in `call_with_retry` (7
  attempts, exponential backoff), commit `ee81331`. Classification:
  **transient**. Audit action: complete.

## Audit results — single-call agents (LLM-driven, JSON-output)

These all flow through `call_with_retry`, so transient infrastructure
errors are now handled centrally. The remaining classifications are
about what happens AFTER the retry budget is exhausted.

### Planner (`bizniz/planner/planner.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 138 | `PlannerBadAIResponseError` | After call_with_retry exhausted on bad JSON / empty response | **fatal** | OK — without a plan, no milestones exist. |

### Architect (`bizniz/architect/architect.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 105 | `ArchitectBadAIResponseError` | After retry exhausted | **fatal** | OK — without architecture, no services materialize. |

### ServicePlanner (`bizniz/service_planner/agent.py`)

Partially patched as of commit `f24b5d7`. Greenfield mode stays
strict; repair mode is lenient on dep-target failures.

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 98 | `ServicePlannerError` | Greenfield: 0 issues emitted | **fatal** | OK — service can't produce code with no issues. |
| 114 | `ServicePlannerError` | Greenfield: individual issue payload failed model validation | **fatal** | OK in greenfield. **Audit**: in repair mode (line 211, see below) the same case is also fatal — should auto-drop the bad issue with a warning. |
| 128 | `ServicePlannerError` | Greenfield: cyclic deps | **fatal** | OK in greenfield. Cycle = LLM contradicted itself. |
| 211 | `ServicePlannerError` | **Repair**: individual issue payload failed validation | **fatal** | ⚠️ **Should be lenient**. Mirror the fix-issue-dropping pattern from `_validate_files_non_empty` and `_repair_dep_targets`. |
| 223 | `ServicePlannerError` | **Repair**: cyclic deps | **fatal** | ⚠️ **Should be lenient** — drop the cycle-causing edge rather than crash repair. |
| 248 | `ServicePlannerError` | Duplicate issue ids (both modes) | **fatal** | OK in greenfield. In repair mode, could deduplicate with a warning. Marginal. |
| 261 | `ServicePlannerError` | Unknown dep target (greenfield only) | **fatal** | OK. Repair path uses `_repair_dep_targets` (lenient) at line 217. |

**Follow-up tickets:**
- `service_planner_repair_lenient_payload_validation` — line 211 should drop bad issues, not raise.
- `service_planner_repair_lenient_cycles` — line 223 should drop the offending edge, not raise.

### Decomposer (`bizniz/decomposer/agent.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 97 | `DecomposerError` | LLM output failed schema validation | **lenient (via dispatcher)** | OK — `MilestoneCodeDispatcher._decompose_issues` catches and falls back to single-unit dispatch. The Decomposer raises; the dispatcher absorbs. |
| 103 | `DecomposerError` | Empty ordered_units | **lenient (via dispatcher)** | OK — same dispatcher fallback. |
| 118 | `DecomposerError` | Duplicate unit ids | **lenient (via dispatcher)** | OK — same dispatcher fallback. |

**Tests required:** verify the dispatcher fallback is exercised
by an integration test that monkey-patches Decomposer to raise.

### QualityEngineer (`bizniz/quality_engineer/agent.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 103 | `QualityEngineerError` | Enrich: schema validation | **fatal** | OK in enrich (required for confidence gating per item 1). |
| 108 | `QualityEngineerError` | Enrich: zero capabilities | **fatal** | OK — milestone is undefined without capabilities. |
| 122 | `QualityEngineerError` | Enrich: zero scenarios across all capabilities | **fatal** | OK. |
| 184 | `QualityEngineerError` | Re-enrich: schema validation | **fatal** | ⚠️ Re-enrich is a side-channel (called only at confidence 0.4-0.6). On schema failure, **should fall back to the original spec** rather than crash the milestone. |
| 188 | `QualityEngineerError` | Re-enrich: empty | **fatal** | ⚠️ Same — fall back to original spec. |
| 252 | `QualityEngineerError` | Review: schema validation | **fatal** | ⚠️ Review-mode is side-channel (drives REPAIR iters). On schema failure, should default to "needs review" verdict rather than crash. |

**Follow-up tickets:**
- `qe_reenrich_lenient_fallback` — lines 184, 188 should return the original spec on failure.
- `qe_review_lenient_fallback` — line 252 should default to "approved=False with no findings" instead of crashing — repair iter can re-trigger.

### CodeReviewer (`bizniz/code_reviewer/agent.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 103 | `CodeReviewError` | Schema validation after retry | **fatal** | ⚠️ Review is side-channel. **Should be lenient**: default to "approved=False with no findings" so the milestone proceeds. The repair iter will re-trigger. |

**Follow-up ticket:** `code_reviewer_lenient_fallback`.

### Refactorer (`bizniz/refactorer/refactorer.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 91 | `RefactorerError` | Constructor: `claude` binary not on PATH | **fatal-at-init** | OK — agent literally can't function. Raised at construction, not during run. The driver catches at construction in `refactor_phase.py` and disables the phase. |

## Audit results — tool-loop agents

Tool-loop agents (Coder, Tester, ClaudeCliCoder, ClaudeCliDebugger,
AgenticDebugger) are wrapped by `MilestoneCodeDispatcher` / integration
debug loops that catch agent-level exceptions and mark the unit/issue
as `errored` or `deferred`. The repair iter then targets those.

This is *already* the lenient pattern at one layer up. The raises
within the agents themselves are fine because they're absorbed by
the dispatcher.

### ClaudeCliCoder (`bizniz/coder/claude_cli_coder.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 135 | `CoderError` | Subprocess exit != 0 | **lenient (via dispatcher)** | OK — unit marked errored, repair iter retries. |
| 227, 231, 242, 250, 256 | `CoderError` | Output parsing failures (JSON shape, expected fields) | **lenient (via dispatcher)** | OK — same path. |

### Coder (`bizniz/coder/agent.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 124 | `CoderError` | `submit_code` called without an active issue | **fatal-impossible** | OK — invariant violation. Should never fire in correct code. |
| 135 | `TerminalActionRejected` | Forced-final-action rejection | **stall** (special) | OK — `tool_loop_agent` converts to stall signal which the dispatcher escalates to next model tier. Documented in CLAUDE.md "What NOT to do". |

### ClaudeCliDebugger (`bizniz/agents/debugger/claude_cli_debugger.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 149 | `AgenticDebuggerError` | Subprocess crash | **lenient (via debug_loop)** | OK — integration debug loop catches, marks iter failed, escalates tier. |
| 234 | `AgenticDebuggerTimeoutError` | Timeout exceeded | **lenient** | OK — same path. |
| 239, 250, 258, 264 | `AgenticDebuggerError` | Output parsing failures | **lenient** | OK — same path. |

### AgenticDebugger (`bizniz/agents/debugger/agentic.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 165, 173, 184 | `AgenticDebuggerBadResponseError` | LLM response shape failures | **lenient (via debug_loop)** | OK. |

## Audit results — orchestration / driver

### MilestoneCodeDispatcher (`bizniz/driver/milestone_code_dispatcher.py`)

Mixed. Some raises are legitimately fatal (state corruption,
unrecoverable plan failures); others should soft-fail.

**Status:** Not yet deep-audited. Open follow-up:
`milestone_code_dispatcher_raise_audit`.

### Gates (`bizniz/driver/gates.py`)

Gates are designed to be strict — they're the explicit halt-control
mechanism. Raises here are by design.

**Status:** No action — gates are the intentional strict layer.

## Audit results — auth path

### AuthPlanner (`bizniz/auth_planner/agent.py`)

| Line | Exception | Trigger | Class | Status |
|---|---|---|---|---|
| 78, 134, 139, 152 | `AuthPlannerError` | Schema / contract validation failures | **fatal** | OK — auth contract is identity backbone, can't soft-fail without dangerous defaults. |

## Audit results — infrastructure / persistence

### ProjectDB (`bizniz/project/project_db.py`)

**Status:** Partially patched. `_RetryingConnection` wraps sqlite
ops and retries once on `OperationalError: readonly database`.

**Open follow-up:** audit other `OperationalError` shapes (database
is locked, disk I/O error). All should follow the same retry-once-
then-surface pattern.

### ProjectGit (`bizniz/driver/project_git.py`)

Designed as best-effort by item 3. Every git op is in a try/except
that logs and continues — no raises propagate.

**Status:** Verify by reading once. No expected work.

### ClaudeCliClient (`bizniz/clients/claude_cli/claude_cli_client.py`)

Has 429 backoff + Max-plan usage cap wait already. The transient
budget at `call_with_retry` complements these.

**Status:** OK. Watch for 5xx-specific handling at the client layer
(currently relies on `call_with_retry` upstream).

## Tests required (per lenient path)

Mirror `test_repair_drops_unknown_dep_instead_of_raising` (the test
that pinned the live crash 2 fix). Each test:

1. Deliberately injects the bad input (mocked client returning bad
   JSON, or raising a transient-shaped exception)
2. Asserts the agent returns a sensible default
3. Asserts a log warning was emitted
4. Asserts the milestone proceeds (i.e. no exception escapes)

Lenient paths without tests rot — a future "make it strict again"
refactor silently removes the leniency. This is the
non-negotiable item 5 done-when.

## Summary table — follow-up tickets

| Ticket | File | Lines | Estimated effort |
|---|---|---|---|
| `service_planner_repair_lenient_payload_validation` | service_planner/agent.py | 211 | 30 min + test |
| `service_planner_repair_lenient_cycles` | service_planner/agent.py | 223 | 30 min + test |
| `qe_reenrich_lenient_fallback` | quality_engineer/agent.py | 184, 188 | 1 hr + tests |
| `qe_review_lenient_fallback` | quality_engineer/agent.py | 252 | 1 hr + test |
| `code_reviewer_lenient_fallback` | code_reviewer/agent.py | 103 | 1 hr + test |
| `milestone_code_dispatcher_raise_audit` | driver/milestone_code_dispatcher.py | TBD | 2 hr (deep-audit) |
| `projectdb_other_operational_errors` | project/project_db.py | TBD | 1 hr (mirror existing pattern) |

**Total estimated effort:** ~8 hours of focused work to ship the
remaining lenient-path patches + tests.

## What's already shipped under item 5

- ✅ `ProjectDB._RetryingConnection` (commit `9258835`)
- ✅ `ServicePlanner._repair_dep_targets` (commit `f24b5d7`)
- ✅ `call_with_retry` separate transient + permanent budgets with
  exponential backoff and env-var override (commit `ee81331`)
- ✅ This audit doc

## Related

- `docs/roadmap.md` — full roadmap with item 5 detail.
- `bizniz/service_planner/agent.py:266` — `_validate_files_non_empty`
  canonical docstring on the lenient philosophy.
- `bizniz/service_planner/tests/test_service_planner.py:test_repair_drops_unknown_dep_instead_of_raising`
  — reference test pinning the lenient pattern.
- `bizniz/lib/tests/test_llm_utils.py` — reference tests for the
  transient retry pattern.
