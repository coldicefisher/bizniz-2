# Per-run Efficiency Reports

Every successful (and most failed) `Architect.build()` calls leave behind a markdown report under `<project_root>/docs/runs/<job_id>.md` plus a JSON sidecar at `<project_root>/docs/runs/<job_id>.json`. The JSON is machine-readable and powers the **delta-since-last-run** section of the next run's report.

## What's in a report

| Section | Source |
|---|---|
| Header (job_id, slug, status, duration) | `CostTracker.start_job` + finally-block timestamp |
| Architecture | `SystemArchitecture` (services + framework + port + skeleton + depends_on) |
| Models | `BiznizConfig` snapshot at build time (architect/engineer/coder/etc. models) |
| Engineering results | One row per `ServiceResult` (success, issues passed/total, error) |
| Cost | `CostTracker.summary()` — calls, tokens, total cost, by-model and by-agent breakdowns |
| Delta since last run | Only present when a previous JSON sidecar exists in the same `docs/runs/` directory |

## Why both Markdown and JSON

The markdown is for humans (PR descriptions, project history, `cd docs/runs && ls -t`).

The JSON is what the **next** run reads when it computes `Δ`. We don't try to parse markdown — JSON sidecars are stable across format changes.

## Failure semantics

The report writer is wrapped in a `try/except` inside `Architect.build()`'s `finally` block. If anything goes wrong inside it (missing field, disk full, malformed CostSummary), the failure is logged but the architect run still returns the `ArchitectResult`. **The report writer never crashes the build.**

When the run fails before the project root is materialized (e.g. `decompose()` raises), no report is written — there's no project to write into yet.

## Reading the delta

```
| Metric        | Previous | This run | Δ            |
|---------------|---------:|---------:|-------------:|
| Duration (s)  |    120   |     90   | ↓ -30        |
| Calls         |     14   |     12   | ↓ -2         |
| Cost ($)      |  0.0750  |  0.0520  | ↓ $-0.0230   |
| Input tokens  |  18,000  |  14,000  | ↓ -4,000     |
| Output tokens |   5,500  |   4,200  | ↓ -1,300     |
```

Arrows: `↑` worse, `↓` better, `→` no change. Whether a metric being "up" or "down" is "good" depends on the metric — calls/cost/tokens/duration are all "down is better."

## Where it lives in code

- `bizniz/run_report/report.py` — `RunReport` dataclass, `render_markdown`, `write_run_report`, `load_previous_run`
- Wired from `bizniz/architect/architect.py:Architect.build()` in the `finally` block, after `tracker.finish_job()`
- Tests: `bizniz/run_report/tests/test_report.py` (11 unit tests)

## Discoverability

`docs/runs/` is the canonical place. Sort by `mtime` for chronological view:

```sh
ls -t docs/runs/*.md | head
```

For a "history at a glance" page, you can build a small index doc that lists `(run_id, status, duration, cost)` from the JSON sidecars — left as an exercise; not in the skeleton.
