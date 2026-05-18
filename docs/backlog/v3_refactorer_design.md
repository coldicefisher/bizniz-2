# v3 Refactorer — design

Filed 2026-05-17. Roadmap item 6, take 3.

**Status:** Design. Implementation queued behind this doc landing.

## The two refactor signals

A real refactorer cares about TWO classes of problem, and they
need different detectors:

### Signal 1 — known anti-patterns (deterministic)

Code patterns that are wrong on inspection regardless of context.
Hardcoded credentials, copy-pasted boilerplate, SQL strings glued
together with `+`, etc. Detected by regex / AST walk; no LLM needed
to *find* them.

**Reused as-is from v2:**

- `bizniz/refactorer/anti_patterns.py:scan_files` — AST-based Python
  anti-pattern detector. Already battle-tested. Returns
  `AntiPatternFinding` per match (path, line, severity).
- `bizniz/refactorer/cpd.py:detect_duplicates` — shingle-matching +
  MinHash for verbatim and near-duplicate code blocks. Returns
  `DuplicateBlock` per duplicate. Battle-tested via MUSE.

### Signal 2 — misplaced business logic (agent-driven)

Code that's in the wrong layer: business logic embedded in an
API route handler, in a Celery worker task body, in a Click CLI
function. The code itself isn't *bad*; it's in the wrong place —
should be a service-layer function in `core/python/` that the
route/worker/CLI thin-wraps.

**NEW for v3:** Walk every file under `app/api/routes/`,
`app/workers/`, `app/cli/` (and equivalent paths per skeleton).
For each one, dispatch an agent to scan it with a focused prompt:

> Look at this route handler. Identify any logic that ISN'T HTTP
> handling: data transformation, business rules, validation
> beyond Pydantic, side-effect coordination, etc. List candidates
> as JSON. For each candidate: line range, function name, why it
> belongs in core, suggested core module name.

Output is a `MisplacedLogicFinding` per candidate.

Why agent-driven not deterministic: business-logic identification
requires semantic judgment. A deterministic scanner can't tell
"this for-loop computes a tax" from "this for-loop concatenates
HTTP response headers." The agent reads the code and decides.

## The per-candidate pipeline

Every candidate (from Signal 1 or Signal 2) flows through the same
4-step pipeline:

```
                  ┌──────────────┐
candidate ───────▶│ Decision     │── NO ──▶ skip, log rationale
                  │ Gate (agent) │
                  └──────────────┘
                          │ YES
                          ▼
                  ┌──────────────┐
                  │ Plan         │── core path + import wiring
                  │ (agent)      │
                  └──────────────┘
                          │
                          ▼
                  ┌──────────────┐
                  │ Execute      │── file edits via Claude session
                  │ (agent)      │
                  └──────────────┘
                          │
                          ▼
                  ┌──────────────┐
                  │ Verify       │── run tests (deterministic)
                  │ (deterministic) │
                  └──────────────┘
                       /        \
                    pass         fail
                     │            │
                git commit    git revert
```

### Step 1 — Decision gate

For each candidate, ONE agent micro-call:

> Given this finding (anti-pattern / duplicate / misplaced
> logic), should we refactor it?
>
> Consider:
> - Risk: would extraction break other consumers?
> - Value: how many consumers benefit?
> - Cost: is the extracted abstraction healthier than the original?
> - Is the existing code already idiomatic enough?
>
> Return JSON: `{"refactor": true|false, "rationale": "..."}`

Cheap call (no tools, just analysis). Stops 30-50% of low-value
candidates before they touch the codebase.

### Step 2 — Plan

For each `refactor=true` candidate, ONE agent call WITH discovery
tools:

> Scan `core/python/` to find where this should live. Either:
>
> 1. An existing module — return its path. (Preferred.)
> 2. A new module — propose path + filename consistent with
>    existing naming conventions in core/python/.
>
> Then plan the extraction:
> - What functions/classes to move
> - What signature changes are needed (e.g., dependency injection
>   for things the route had via FastAPI deps)
> - What imports the consumer needs after extraction

Output: `ExtractionPlan` with `destination_path`,
`destination_kind` ("existing" | "new"), `functions_to_move`,
`signature_changes`, `consumer_import`.

### Step 3 — Execute

Claude CLI session with Read/Edit/Write/Bash, given the
`ExtractionPlan`:

> Apply this extraction. Move the code to `<destination_path>`,
> rewrite the consumer to import + call the new function, preserve
> behavior. Do not change the consumer's external interface.

Returns a structured `ExtractionResult` (status, files_edited,
git_diff_summary).

### Step 4 — Verify

Deterministic — no agent. Two checks in order:

1. **Import-resolution check.** Walk every file edited, parse
   imports, confirm they resolve. Catches the obvious failure mode
   (import path wrong) before paying for a full test run.
2. **Test run.** Invoke the project's test runner inside the
   service container. If passes → `git_ops.commit(message)`. If
   fails → `git_ops.revert_to(pre_rev)` and mark `status=reverted`.

The Verify step has NO agent in the loop. Once the agent ships the
code, deterministic guards arbitrate.

## What's NOT in scope

- **Frontend refactoring** (component extraction, redux state).
  Filed as future "v4 frontend refactorer." Needs different
  scan-2 (visual + DOM-shape) and different destination
  (shared component lib).
- **Multi-language refactor** in one pass. v3 is Python-only —
  TypeScript scanning + extraction comes later when there's a
  parallel `core/typescript/` ready to receive.
- **Cross-milestone state.** v3 runs per refactor phase invocation;
  it doesn't try to track "we refactored X in M2, don't re-flag
  the same finding in M3." (Future: a per-project `refactored.json`
  ledger.)
- **AntiPatterns scan for TS** — v2's `scan_typescript_file` is
  there but disabled for v3's pipeline. Python-only acceptance
  criterion.

## Module layout

```
bizniz/refactorer/
  __init__.py
  cpd.py                          # REUSED from v2
  anti_patterns.py                # REUSED from v2
  tokenizers.py                   # REUSED from v2
  misplacement_scanner.py         # NEW — Signal 2 (agent-driven)
  decision_gate.py                # NEW — Step 1
  extraction_planner.py           # REWRITTEN — Step 2 (agent + core walk)
  extraction_executor.py          # REWRITTEN — Step 3 (just file edits)
  import_verifier.py              # NEW — Step 4a (deterministic)
  agent.py                        # REWRITTEN — orchestrates the pipeline
```

The v2 modules above (anti_patterns, cpd, tokenizers) are reused.
The v1 `refactorer.py` (single-shot) stays in place as a fallback
selectable via `BIZNIZ_REFACTORER=v1`. Default switches to v3
once it ships and passes one live build.

## Acceptance

1. **Two scans run.** `RefactorerAgent.run()` invokes scan 1
   (anti-patterns + CPD) and scan 2 (misplacement) on every
   non-test Python file in the project.
2. **Decision gate runs per candidate.** Each finding gets a
   yes/no with rationale; rationale survives into the
   `RefactorerRunResult.notes`.
3. **Extraction Plan includes destination_path.** The plan is
   computed by an agent that read `core/python/`, not by a
   heuristic.
4. **Per-extraction git discipline.** Each accepted extraction is
   committed independently. Failed tests revert ONLY that
   extraction; others stay.
5. **Python-only.** TypeScript / frontend extractions are
   explicitly skipped in v3.
6. **Telemetry.** End-of-run report shows: candidates found per
   scan, decisions per candidate, extractions applied vs reverted,
   destination paths used.

## Estimated effort

| Component | Hours |
|---|---|
| Misplacement scanner (scan 2) | 2.5 |
| Decision gate | 1.5 |
| Extraction planner rewrite | 2.5 |
| Extraction executor adjustments | 1 |
| Import verifier | 1 |
| Agent orchestration | 1.5 |
| Tests | 3 |
| **Total** | **~13 hours** |

Realistic: two sessions.

## Sequencing relative to today's work

- **D14 (generalized phase recovery base)** — landed. Useful
  for any future per-phase recovery (smoke + docs + post-refactor
  if needed).
- **D16 (refactor recovery loop)** — depends on the refactorer
  semantically. Land it AFTER v3, wrapping v3's per-extraction
  failures rather than v1's whole-pass failures. (Skipping now.)
- **D17 (docs critical-docs gate + recovery)** — orthogonal to
  refactor. Ship today using the D14 base.

## Open questions

- **Decision-gate model tier:** flash-lite (cheap, fast) or pro
  (smarter)? Probably flash-lite to start; if rejection rate is
  noisy, escalate.
- **Misplacement scanner output limits:** how many candidates to
  surface per file before truncating? Probably 5 per file with a
  "more available" marker, to keep the planner's queue tractable.
- **Single-extraction commit messages:** auto-generate from the
  finding shape, or have the agent write them? Auto for v3 to
  keep it deterministic.
