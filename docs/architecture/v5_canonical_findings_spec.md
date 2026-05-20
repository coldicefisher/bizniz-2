# v5 Pipeline Spec — Canonical Findings convergence

**Status:** spec — implementation not yet started.
**Anchor data:** recipe_v4_v8 M1 run (2026-05-19) — killed at
iter-3 regression. v4 architecture (parallel coder/tester/validator/
debugger) all worked correctly; convergence failed at the OUTER
review/repair loop.
**Predecessor:** [[v4_pipeline_spec.md]] (v4 shipped end-to-end).

## Why we're changing

v4 solved IMPLEMENT. v3.1 solved review parallelism + V2 approval
semantics. What's left unsolved: **the review/repair loop doesn't
converge.**

Tonight's recipe_v4_v8 evidence:

| Iter | Defects | Coverage | Verdict |
|---|---|---|---|
| Review iter 1 | 61 | 0/7 | initial |
| Review iter 2 (after 36-min repair) | 32 | 4/7 | progress |
| Review iter 3 (after 20-min repair) | **34** | **3/7** | **REGRESSION** |

Coverage went 4/7 → 3/7. The reviewer LOST credit for a capability
it had previously approved — on essentially the same code. This is
pure LLM-judgment drift: same code, different verdict.

Multiple contributing causes ranked:

1. **QE/CR re-review is non-deterministic.** Same code, different
   verdict. The reviewer is the noise source.
2. **PerIssueDebugger timeout left partial work.** Bumped 600s →
   3000s (separate commit) but doesn't solve the structural
   problem.
3. **Cross-fix-issue conflicts via whole-file rewrites.** Parallel
   fix-issues each emit full file content; they can erase each
   other's work.

(1) is the dominant factor. (2) is mitigated. (3) needs structural
work — but is downstream of (1): if the reviewer were stable, (3)'s
damage would surface as actual test failures, not phantom defect
counts.

## What v5 is

Make the **reviewer stateful across iters**. The full QE+CR review
runs ONCE per milestone. Every subsequent iter is a *resolution
check* against the frozen list, not a fresh review.

```
Iter 1:
  PHASE_FULL_REVIEW (QE + CR parallel — today's path)
    → CanonicalReport: List[CanonicalFinding] (frozen, persisted)
  PHASE_REPAIR (target the canonical list)

Iter 2+:
  PHASE_RESOLUTION_CHECK (NEW — replaces fresh review):
    For each CanonicalFinding fi:
      examine current code →
      status = resolved | still_present | regressed
  if all resolved → APPROVED
  if any still_present → REPAIR those specific ones (next iter)
  if any regressed → ROLLBACK the iter's repair, retry
```

The reviewer at iter 2+ is doing a **structured check against
specific named items**, not making a fresh creative judgment. Its
output universe is constrained: for each known finding, mark it
resolved / still / regressed. **The defect count is mathematically
monotone non-increasing.** It cannot go up.

## Architecture diagram

```
                  ┌──────────────────────────────────┐
                  │ MilestoneLoop (review/repair)    │
                  └──────────────┬───────────────────┘
                                 │
                  ┌──────────────v───────────────────┐
                  │ ITER 1 — FULL REVIEW             │
                  │  parallel QE.review + CR.review  │
                  │  → freeze as CanonicalReport     │
                  │  → persist to milestone state    │
                  └──────────────┬───────────────────┘
                                 │
                  ┌──────────────v───────────────────┐
                  │ REPAIR (v4 dispatcher)           │
                  │  target CanonicalFindings        │
                  └──────────────┬───────────────────┘
                                 │
                  ┌──────────────v───────────────────┐
                  │ ITER 2+ — RESOLUTION CHECK       │
                  │  for each Fi in CanonicalReport: │
                  │   resolved | still | regressed   │
                  │  NEVER invent new findings       │
                  └──────────────┬───────────────────┘
                                 │
                  ┌──────────────v───────────────────┐
                  │ all resolved?                    │
                  │   yes → APPROVED                 │
                  │   any regressed? → ROLLBACK iter │
                  │   else → REPAIR remaining        │
                  └──────────────────────────────────┘
```

## Concrete decisions

| Decision | Value | Reasoning |
|---|---|---|
| Reviewer at iter 1 | full v3.1 parallel QE+CR | unchanged — still the source of truth |
| Reviewer at iter 2+ | NEW ResolutionChecker (per source) | LLM call but constrained output schema |
| CanonicalReport scope | per-milestone | reset for next milestone (next spec, next gaps) |
| Persistence | JSON artifact alongside milestone state | survives process restart, enables resume |
| Finding ID stability | `<source>:<capability_id>:<short_hash>` | reproducible across iters, debuggable |
| Regression handling | git reset to pre-repair snapshot | use existing ProjectGit tags |
| Approval criterion | all critical resolved + all important resolved | nice-to-have can remain |
| Stall criterion | 0 findings resolved across N consecutive iters | replaces v3.1 ProgressTracker for this loop |
| Hard cap | unchanged (20 iters) | belt-and-suspenders |
| New-defect handling | trust per-issue validator + integration tests | reviewer can NEVER invent new findings |

## Implementation surface

```
new modules:
  bizniz/canonical_findings/
    types.py              — CanonicalFinding, CanonicalReport,
                            ResolutionStatus, ResolutionReport
    persistence.py        — write/read canonical report alongside
                            milestone state JSON
    fingerprint.py        — stable Finding ID generator
                            (source + capability + short hash of
                            issue scope)
    tests/

  bizniz/resolution_checker/
    agent.py              — ResolutionChecker (per source: QE flavor,
                            CR flavor). Takes CanonicalReport +
                            current code, returns ResolutionReport.
    prompts.py            — system + user template
    types.py              — ResolutionReport, FindingResolution
    tests/

  bizniz/driver/review_repair_v5.py
    ReviewRepairV5Loop — wraps v3.1's parallel review at iter 1,
    swaps to ResolutionChecker at iter 2+, handles rollback on
    regression.

modified:
  bizniz/driver/milestone_loop.py
    + use_v5_canonical: bool = False flag
    + _phase_review_repair_loop branch: when use_v5_canonical,
      delegate to ReviewRepairV5Loop
    + _phase_review_repair_loop_v5 helper (mirrors v3.1's shape but
      uses CanonicalFindings semantics)

  bizniz/quality_engineer/
    + agent.py — new check_resolution() method (alongside review())
                 that emits ResolutionReport instead of CoverageReport
    + or split into a thin ResolutionChecker module that calls QE.

  bizniz/code_reviewer/
    + agent.py — symmetric check_resolution()
    + emits per-finding resolution status

  bizniz/driver/project_git.py
    + snapshot_for_repair_iter() — git stash or tag before each
      repair iter so we can ROLLBACK on regression
    + rollback_repair_iter() — git reset to that snapshot

  bizniz/config/bizniz_config.py
    + use_v5_canonical: bool = False  (or env-toggle)
    + resolution_check_stall_threshold: int = 3

  examples/v2_build.py
    + --use-v5 flag (implies --use-v4 IMPLEMENT + canonical review)
```

## Algorithm — ReviewRepairV5Loop

```python
def run(initial_result) -> ReviewRepairV5Result:
    # Step 1: full review (only at iter 1).
    coverage, code_review = phase_review_parallel(...)
    if approved(coverage, code_review):
        return clean_result()

    canonical = freeze_to_canonical_findings(coverage, code_review)
    persist_canonical_report(canonical)

    project_git.snapshot_for_repair_iter(0)

    iteration = 0
    stall_counter = 0
    while iteration < self._hard_cap:
        iteration += 1

        # Step 2: REPAIR (v4 dispatcher targeting canonical findings).
        project_git.snapshot_for_repair_iter(iteration)
        repair_result = code_dispatcher.repair(
            canonical_findings=canonical.unresolved(),
            ...
        )

        # Step 3: RESOLUTION_CHECK (NOT a fresh review).
        resolution = resolution_checker.check(
            canonical=canonical,
            current_code=read_workspace(),
        )

        # Update canonical findings' statuses based on resolution.
        canonical.apply_resolution(resolution)

        # Approval check.
        if canonical.all_blockers_resolved():
            log("v5: approved after N iter(s)")
            return approved_result(canonical, repair_result)

        # Regression check — defect count can ONLY decrease in v5.
        # If any finding flipped from resolved → still/regressed,
        # roll back this iter's repair.
        regressed = canonical.flipped_back_findings(prior_state)
        if regressed:
            log(f"v5: rollback iter {iteration} — {len(regressed)} regression(s)")
            project_git.rollback_repair_iter(iteration)
            stall_counter += 1
            if stall_counter >= self._stall_threshold:
                return stall_result(canonical)
            continue

        # Progress check.
        resolved_this_iter = canonical.newly_resolved_count(prior_state)
        if resolved_this_iter == 0:
            stall_counter += 1
            if stall_counter >= self._stall_threshold:
                return stall_result(canonical)
        else:
            stall_counter = 0

    return hard_cap_result(canonical)
```

## What we trade away

The current loop can discover NEW defects that the repair
introduced or that the iter-1 review missed. v5 forbids that — the
reviewer at iter 2+ can only mark resolved / still / regressed.
New defects are caught instead by:

- **Per-issue validator** (AST + symbol + pytest collect) catches
  import-level regressions
- **PerIssueDebugger** runs in-container `pytest -x` and sees real
  test failures
- **Integration phase** catches stack-level regressions
- **Next milestone's enrich** surfaces new gaps against next spec

Net loss: small — defects that don't break tests AND aren't in
the canonical list aren't actually defects worth iterating on.
That's noise.

## Anchor projections

From tonight's recipe_v4_v8 numbers:

| Phase | v4 measured | v5 projected | Why |
|---|---|---|---|
| IMPLEMENT | 13 min | 13 min | unchanged |
| Iter 1 full review | 3 min | 3 min | unchanged |
| Repair iter 1 | 36 min | 36 min | unchanged |
| Iter 2 review | 3 min | **~1 min** | resolution check ≠ full review |
| Repair iter 2 | 20 min | 20 min | unchanged |
| Iter 3 review | 3 min | **~1 min** | resolution check |
| Regression rollbacks | 0 (but caused harm) | catches + recovers | net win |
| Iters to converge | did NOT converge | **2-4 iter(s)** projected | monotone progress |
| **M1 total projection** | did NOT land | **~1h 15m – 1h 30m** | landing M1 |

The big projection: **M1 actually lands** instead of regressing
forever.

## Risks + mitigations

1. **Reviewer misses real defects** because it can only check the
   frozen list. **Mitigation**: iter-1 review is the canonical
   pass; trust it to be comprehensive. Downstream gates (per-issue
   validator, integration tests) catch what the reviewer doesn't.

2. **Canonical fingerprint instability.** Two iters generate slightly
   different IDs for the same logical defect → can't track
   resolution. **Mitigation**: fingerprint includes
   `(source, capability_id, target_file?)` — stable as long as
   capability ids are stable. Test the fingerprint generator
   against known QE outputs.

3. **Rollback breaks resume.** If a repair iter is rolled back, the
   project's git state goes back but the bizniz state file might
   not. **Mitigation**: snapshot + rollback BOTH the workspace files
   AND the milestone state JSON. Use a single atomic operation.

4. **ResolutionChecker is also an LLM** — still some non-determinism.
   **Mitigation**: structured output (`{finding_id: status}` map)
   constrains the universe. Much smaller output space than fresh
   review = much less drift. If still noisy, add a "majority of N
   checks" pattern — run check 3x, take majority.

5. **Approval criterion too strict.** "All critical + all important
   resolved" might never be met if QE keeps emitting important-priority
   findings the agent can't fix. **Mitigation**: keep nice-to-have
   findings non-blocking (already the case in v3.1's `_approved`).
   Add an escape hatch: after N iters with no progress on a specific
   finding, mark it "won't fix" and continue.

## Validation plan

**Phase 1 — Unit tests for CanonicalReport machinery**
- Fingerprint stability across re-fingerprintings of identical findings
- Status transitions (still → resolved, resolved → regressed, etc)
- Persistence round-trip

**Phase 2 — ResolutionChecker stub against known fixture**
- Take an existing CoverageReport from a past run
- Feed it as CanonicalReport
- Feed unchanged workspace content
- Assert all findings come back as `still_present` (sanity check)
- Then mutate the workspace to fix one finding → assert it comes
  back as `resolved`

**Phase 3 — Live M1 on recipe_v4_v9**
- Run with `--use-v5 --use-v4` end-to-end
- Anchor: recipe_v4_v8's halt at iter-3 regression
- Pass criteria: M1 APPROVES within ≤4 iters; no regression rollbacks
  needed in the happy path; if rollback fires, the loop recovers
  not collapses.

## Open questions

1. **Should ResolutionChecker be per-source (QE flavor + CR flavor)
   or unified?** Per-source matches the current architecture's
   structure but duplicates LLM calls. Unified is cheaper but means
   one prompt covers two distinct concern types (coverage gaps vs
   code-quality findings).
   - **Recommend**: per-source, run in parallel (same as iter-1 review).
     Maintains the v3.1 fan-out pattern.

2. **Rollback granularity: per repair iter, or per fix-issue?** Per-iter
   is simpler — git stash the whole thing. Per-fix-issue is more
   surgical but needs per-issue snapshots.
   - **Recommend**: per-iter for v5 MVP. Per-fix-issue is a future
     refinement if data shows we need it.

3. **What if iter-1's full review is itself noisy and emits
   spurious findings?** v5 freezes them and the agent burns time
   trying to "fix" things that aren't broken.
   - **Recommend**: trust iter-1's review (same as v3.1 does).
     Add a future escape hatch: if a finding stays `still_present`
     for 3+ iters with no progress, mark it "won't fix" and remove
     from the active list.

## Out of scope (deferred to later)

- **Cross-iter QE/CR re-running** — strictly v5 says no, but a future
  "v5.1" could do a "fresh review" check every 5 iters as a sanity
  pass.
- **Per-fix-issue rollback** (instead of per-iter) — wait for data.
- **Reviewer ensemble** (run check 3x, take majority) — only if
  the constrained ResolutionChecker still shows drift.
- **Cross-milestone canonical reports** — keep per-milestone for now.

## Implementation order (commit chain)

1. `CanonicalFindings` types + fingerprint + persistence + unit tests
2. `ResolutionChecker` (per-source, parallel) + tests
3. `ProjectGit.snapshot_for_repair_iter` + `rollback_repair_iter` + tests
4. `ReviewRepairV5Loop` + MilestoneLoop wiring + tests
5. `--use-v5` CLI flag in v2_build
6. Live M1 run on `recipe_v4_v9 --use-v5 --use-v4`
