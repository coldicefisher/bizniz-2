# Performance tests — bizniz component microbenchmarks

This directory documents per-component perf tests of the bizniz
pipeline. Each test isolates ONE component (Coder, ServicePlanner,
Decomposer, QualityEngineer.review, …) so we can attribute wall-
clock cost cleanly instead of guessing from full-pipeline runs.

## Why

A full recipe-class build now takes 24+ hours (was 40 minutes per
the bookshelf_claude baseline in CLAUDE.md). Full-pipeline timing
data (`bizniz.perf_log`) gives aggregate signal but doesn't answer
*which component scales badly under what conditions*. These
microbenchmarks isolate one component at a time, against fixed
inputs and a known-good ground truth, so each variable can be
measured independently.

## Layout

```
~/bizniz_perf_tests/                       # outside the repo (gitignored)
  <test-slug>/
    fixtures/                              # pre-seeded inputs (frozen)
    .runs/<test-slug>/<run-id>/
      result.json                          # timings + outcome + diff vs expected
      log.txt                              # full stdout/stderr
      workspace/                           # post-test workspace state

bizniz/perf_tests/                         # in-repo (committed)
  __init__.py
  runner.py                                # CLI: python -m bizniz.perf_tests run <test>
  fixtures/                                # input fixtures (issue specs, prompts, etc.)
  tests/                                   # one .py per test scenario

docs/perf_tests/                           # in-repo (committed)
  README.md                                # this file
  <test-slug>.md                           # results doc per test
```

## Versioning + rollback

Perf data is only as trustworthy as our knowledge of what code +
deps + binary were in effect at run time. The harness records four
things on every run:

1. **Git tag at HEAD** — `perf/<test-slug>/run-<N>` (annotated). Lets
   us `git diff perf/coder/run-1 perf/coder/run-2` to see what
   changed, and `git checkout perf/coder/run-1` + re-run to verify a
   rollback reproduces the original numbers.
2. **Dirty-tree gate** — `python -m bizniz.perf_tests run …` refuses
   to start when the working tree is dirty (would record a misleading
   `git_rev`). `--allow-dirty` overrides and captures the full
   `git diff HEAD` into `result.json` so the run stays traceable.
3. **Env fingerprint** in `result.json` under `env`:
   - `bizniz_git_rev` + `bizniz_git_status.dirty`
   - `claude_cli_version` — catches binary upgrades
   - `pip_freeze.sha256` — catches dep drift without a 50KB freeze dump
   - `fixture_sha256` — catches fixture edits that change what's
     actually being measured
   - `python_version`, `platform`
4. **A row in `docs/perf_tests/<slug>.md`** for every change that
   affects the numbers. The doc is the audit log; the git tag is the
   pointer; the env fingerprint is the "was this comparable" check.

### Experiment branches

Knob tweaks (prompt versions, thresholds, model tiers) land on
`experiment/<name>` branches first. Run perf tests on the branch,
compare to `main`, only merge if you'd ship the change. Keep losing
experiments as branches for archaeology — `git branch -D` only after
the result is documented.

### Library upgrades

If `pip_freeze.sha256` changes between two runs, treat the numeric
delta as untrusted until a same-deps re-baseline is captured. The
runner doesn't enforce this — it's a discipline check at compare
time.

## Workflow

1. **Pick a component to test.** Start at the bottleneck per the
   build-log timing data, not where intuition says.
2. **Build a fixture.** Find a real example from a past build,
   freeze it under `bizniz/perf_tests/fixtures/<test-slug>/`.
   Include both the input (e.g. CoderIssue + workspace seed) and
   the *expected* result (what the component produced when it ran
   successfully in production).
3. **Write the test scenario.** A Python file under
   `bizniz/perf_tests/tests/<test-slug>.py` that uses the runner
   to dispatch the component, measure, and compare.
4. **Run it.** `python -m bizniz.perf_tests run <test-slug>`.
   Runner writes `~/bizniz_perf_tests/<test-slug>/.runs/<run-id>/`
   and tags the repo `perf/<test-slug>/run-<N>`.
5. **Document.** Update `docs/perf_tests/<test-slug>.md` with the
   numbers + interpretation.
6. **Iterate.** Change ONE variable (Decomposer on/off, prompt
   tweak, model tier), rerun, compare via
   `python -m bizniz.perf_tests compare <test>/run-N <test>/run-M`.

## What a test measures

Every test records:

- `wall_clock_s` — total runtime
- `subprocess_calls` — count + per-call duration distribution
- `result_status` — pass/fail vs the expected outcome
- `git_rev` — bizniz HEAD at test time (so the tag is meaningful)
- `env_snapshot` — model versions, key env vars

## Tests planned

| Test | Component | Question | Status |
|---|---|---|---|
| `coder_single_issue` | Coder | What's the baseline wall-clock for ONE Coder dispatch on a known-good issue? | planned |
| `coder_decompose_ab` | Coder + Decomposer | Does Decomposer reduce or increase total Coder time per issue? | planned (after baseline) |
| `service_planner_single` | ServicePlanner | Is the 4-min ServicePlanner call duration intrinsic to the prompt or fixable? | planned |
| `review_repair_per_iter` | QualityEngineer + Engineer.repair | What's the cost of one repair iter? Does it scale with project size? | planned |
