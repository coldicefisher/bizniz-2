# Deprecated examples

These scripts predate the v2.5 refactor (commit `e0fc7e7`, 2026-05-06)
which moved `bizniz.agents.coder` → `bizniz.coder` and removed
`bizniz.tester` entirely. They no longer import cleanly and have not
been updated.

Kept here (rather than deleted) for git-archaeology: each one
references a pre-v2.5 entry point shape that may inform future work.

## What to use instead

| Old | Current |
|---|---|
| `auto_architect.py` | `../v2_build.py` (positional prompt, `--project`) |
| `engineer.py` | `../v2_build.py --phase implement --milestone N` |
| `coding_orchestrator.py` | `../v2_build.py --phase implement` |
| `milestone_build.py` | `../v2_build.py --milestone N` |
| `codegen_backend*.py` / `codegen_frontend.py` / `codegen_blast.py` | `../v2_build.py` |
| `autocoder_standalone.py` / `autotester_standalone.py` | covered by `../v2_build.py`'s implement phase |
| `run_stability_test.py` | functional tests under `bizniz/*/tests/` |
| `simple_frontend.py` | `../frontend_iterate.py` (still current) |
| `coding_orchestrator.py` | `../v2_build.py --phase implement` |

The smoke test at `bizniz/tests/test_examples_smoke.py` catches new
examples that break in the same way before they ship.
