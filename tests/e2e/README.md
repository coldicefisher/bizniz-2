# End-to-End Pipeline Tests

Full lifecycle tests that exercise the bizniz pipeline from problem
statement to working application. These are not unit tests — they
call real AI APIs, build real Docker images, and produce real
projects in `~/bizniz_projects/`.

## Tests

### Property Manager (`property_manager/`)

The first full-lifecycle test. A property management app for small
landlords with Postgres, JWT auth, two roles, and real business logic.

**What it exercises:**
- Planner → milestone decomposition (3-5 milestones)
- M1: greenfield build via `architect.evolve()` with empty architecture
- M2+: incremental evolve — new/extended services, preserved workspaces
- Integration tests per milestone (HTTPApiTester + AgenticDebugger)
- Real database (Postgres via docker-compose)
- JWT authentication with role-based access

**Run commands:**

```bash
# Always from repo root with env loaded:
cd ~/bizniz && set -a && source .env && set +a

# Step 1: Plan only — see milestones before committing API cost
./tests/e2e/property_manager/run.sh plan

# Step 2: Execute milestone 1 (greenfield)
./tests/e2e/property_manager/run.sh m1

# Step 3: Verify M1, then execute milestone 2 (evolve)
./tests/e2e/property_manager/run.sh m2

# Execute a range of milestones
./tests/e2e/property_manager/run.sh m1-3

# Run integration tests against the built project
./tests/e2e/property_manager/run.sh integration

# Stand up the app for manual inspection
./tests/e2e/property_manager/run.sh up

# Tear down
./tests/e2e/property_manager/run.sh down
```

## Adding a New Test

1. Create `tests/e2e/<name>/`
2. Add `problem_statement.txt` — the natural-language prompt
3. Add `run.sh` — convenience wrapper
4. Add `README.md` — what this test exercises and expected outcomes
5. The test reuses `examples/milestone_build.py` under the hood

## Cost Expectations

| Phase | Approximate Cost |
|---|---|
| Planning | $0.01 (1 Gemini Pro call) |
| M1 engineering (2-3 services) | $1-3 |
| M1 integration tests + debugger | $0.05-0.50 |
| M2 evolve + engineering | $1-2 |
| Full lifecycle (all milestones) | $5-15 |

Costs depend on model config in `bizniz.yaml`, number of services,
and how many repair iterations the debugger needs.
