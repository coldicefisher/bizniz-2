# Confidence Signals — Load-bearing or Drop the Pretense

Filed 2026-05-15. Surfaced when checking whether QE's
"`confidence < 0.6` means Engineer should ask follow-up questions"
prompt language actually drives behavior. **It doesn't.**

## The honesty gap

Several agents self-rate their own output with a `confidence` field
(0-1) and the prompts describe what *should* happen at low scores
("treat as draft," "ask follow-ups," etc.). In code, the score is
logged and persisted but **nothing acts on it**.

Audit as of 2026-05-15:

| Agent | Has `confidence`? | Read by harness? |
|---|---|---|
| `QualityEngineer.enrich` | ✅ 0-1 self-rated | ❌ logged only |
| `QualityEngineer.review` (coverage) | ✅ 0-1 self-rated | ⚠️ merged with code_review for repair-decision summary only |
| `CodeReviewer.review` | ✅ 0-1 self-rated | ⚠️ merged with coverage for repair-decision summary only |
| `Architect.decompose` | ❌ no self-rating | n/a |
| `Planner` (milestone decomp) | ❌ no self-rating | n/a |
| `Coder.code_issue` | ❌ no self-rating | n/a |
| `Tester` | ❌ no self-rating | n/a |
| `UX vision eval` | ⚠️ via `overall_score` 1-10 | ✅ gates repair iteration |

The right column tells the story: only one signal is **load-bearing**
(UX vision score, which gates `stop_recommendation`). Every other
`confidence` field is descriptive telemetry the harness ignores.

## Ticket scope

Make `QualityEngineer.enrich.confidence` load-bearing.

### Behavior

- **`confidence >= 0.6`**: implement normally (current path).
- **`0.4 <= confidence < 0.6`**: trigger ONE re-enrich pass. Prompt
  variant explicitly asks the model: "your prior pass scored {score}
  — list the ambiguities you struggled with and either resolve them
  from re-reading the workspace OR write them as TODOs for the
  Engineer to surface." Take the higher-confidence result.
- **`confidence < 0.4`**: halt at a new soft gate (`enrich_low_confidence`).
  `--auto` pushes through with a logged warning; `--interactive`
  halts for human review.

### Why the bands

- `0.6` is what the QE prompt itself names as "draft territory."
- `0.4` is "the model itself doesn't trust this enough to retry" —
  halt rather than burn Coder cycles on a spec that's likely wrong.

### Acceptance

1. New `confidence_low_threshold` and `confidence_halt_threshold`
   fields on `MilestoneLoop` (defaults 0.6 / 0.4).
2. New `_maybe_re_enrich(spec)` helper on `MilestoneLoop` that
   dispatches QE once more with the augmented prompt when in the
   middle band.
3. New `enrich_low_confidence` soft gate. Wire into `GatePolicy`.
4. Tests: re-enrich is triggered at 0.5, not at 0.7. Halt fires at
   0.3 in `--interactive`. `--auto` logs and continues at 0.3.

## The meta-pattern: where else does this apply?

Once enrich-confidence is load-bearing, the same shape generalizes:

1. **CodeReviewer.review.confidence** — already self-rated; could
   gate "this critical review was low-confidence, escalate to a
   second-pass reviewer with `gemini-pro`."
2. **Coder.code_issue** — add a self-rated `confidence_in_fix` to
   its return shape. Low-confidence completions get a forced
   test-coverage re-check OR the next-tier Coder picks them up
   without waiting for failing tests.
3. **Tester** — similar; tests it's not confident about get an
   independent re-pass.
4. **Architect.decompose** — flag low-confidence service splits
   for human review BEFORE the Provisioner materializes them.
5. **Planner** — same; flag low-confidence milestone decompositions.

The unifying principle: **structured self-rating only earns its
keep when the harness acts on it.** Today most ratings are
descriptive telemetry. Roadmap item 8 (diagnostic + perf logging)
is the right place to systematize this — define a single
`AgentConfidence` shape, attach to every agent's output, drive
universal harness behavior off it.

## Order

1. **Roadmap item 1 (this ticket)**: enrich-confidence load-bearing
   as the reference implementation. SHIPPED 2026-05-15 (`5de1059`).
2. **During roadmap item 8**: define the `AgentConfidence` shape,
   audit every agent, retrofit the missing ones.
3. **During roadmap item 6 (test/debug)**: wire the universal
   action policy (low → retry, very-low → halt) into `GatePolicy`.

## Related

- `bizniz/quality_engineer/prompts/enrich_prompt.py` (the prompt
  language that suggests behavior that doesn't exist).
- `bizniz/quality_engineer/types.py:85` (the field definition).
- `bizniz/driver/milestone_loop.py:1168` (the one place confidence
  IS read — for review-merge summary, not gating).
