# Build Performance Report

**Source log:** `/tmp/claude-1000/-home-jamey-bizniz/753088a7-f437-4dd8-9110-cc6e57f04557/tasks/bnkl609eu.output`  
**Events parsed:** 45  
**Wall-clock span:** 3h28m

## Headline

- **Milestones DONE:** 1 — M1
- **Unit dispatch:** 24 run + 0 skipped via resume (0% saved)

## Coder unit dispatch

| | calls | total | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| All units | 24 | 1h35m | 3m19s | 9m20s | 14m58s |

**Exit codes:** exit 0: 24

## Per-agent timing

| agent | calls | total | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| ClaudeCliDebugger | 3 | 14m14s | 5m | 5m45s | 5m45s |
| ServicePlanner.repair | 4 | 5m | 1m21s | 1m40s | 1m40s |
| QualityEngineer.review | 3 | 3m27s | 1m15s | 1m33s | 1m33s |
| QualityEngineer.enrich | 1 | 3m16s | 3m16s | 3m16s | 3m16s |
| ServicePlanner | 2 | 2m47s | 1m24s | 1m24s | 1m24s |
| CodeReviewer | 2 | 1m58s | 1m14s | 1m14s | 1m14s |
| Planner | 1 | 1m4s | 1m4s | 1m4s | 1m4s |
| AuthOperator.code_examples | 1 | 39s | 39s | 39s | 39s |
| Architect.decompose | 1 | 14s | 14s | 14s | 14s |
| AuthPlanner | 1 | 6s | 6s | 6s | 6s |

## Milestones DONE

| milestone | name | repair iters |
|---|---|---:|
| M1 | Auth and authenticated shell | 2 |

## ProUXDesigner (last milestone observed)

- Total: **32m14s**
  - global_design: 12m9s
  - verify_css: 5m3s
  - capture: 3m35s
  - fix: 3m12s
  - build_chain_fix: 2m51s
  - code_review: 2m34s
  - eval: 2m1s
  - pre_capture: 46s
  - storybook: 0s

