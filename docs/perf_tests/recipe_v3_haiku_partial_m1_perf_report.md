# Build Performance Report

**Source log:** `/tmp/claude-1000/-home-jamey-bizniz/753088a7-f437-4dd8-9110-cc6e57f04557/tasks/bdufeadat.output`  
**Events parsed:** 37  
**Wall-clock span:** 1h13m

## Headline

- **Unit dispatch:** 19 run + 0 skipped via resume (0% saved)

## Coder unit dispatch

| | calls | total | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| All units | 19 | 57m3s | 2m59s | 7m28s | 7m28s |

**Exit codes:** exit 0: 19

## Per-agent timing

| agent | calls | total | median | p95 | max |
|---|---:|---:|---:|---:|---:|
| ServicePlanner.repair | 6 | 4m40s | 40s | 1m26s | 1m26s |
| ServicePlanner | 2 | 3m35s | 2m2s | 2m2s | 2m2s |
| QualityEngineer.review | 3 | 2m17s | 50s | 57s | 57s |
| QualityEngineer.enrich | 1 | 2m2s | 2m2s | 2m2s | 2m2s |
| CodeReviewer | 2 | 1m33s | 1m4s | 1m4s | 1m4s |
| Planner | 1 | 1m12s | 1m12s | 1m12s | 1m12s |
| AuthOperator.code_examples | 1 | 35s | 35s | 35s | 35s |
| Architect.decompose | 1 | 24s | 24s | 24s | 24s |
| AuthPlanner | 1 | 14s | 14s | 14s | 14s |

