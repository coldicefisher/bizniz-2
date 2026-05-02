#!/usr/bin/env bash
# Property Manager E2E test runner
#
# Usage:
#   ./tests/e2e/property_manager/run.sh plan        # Plan only
#   ./tests/e2e/property_manager/run.sh m1           # Execute milestone 1
#   ./tests/e2e/property_manager/run.sh m2           # Execute milestone 2
#   ./tests/e2e/property_manager/run.sh m1-3         # Execute milestones 1-3
#   ./tests/e2e/property_manager/run.sh integration  # Run integration tests
#   ./tests/e2e/property_manager/run.sh up           # Stand up the app
#   ./tests/e2e/property_manager/run.sh down         # Tear down

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
set -a && source .env && set +a

PROJECT_DIR="$HOME/bizniz_projects/property_manager_v1"
COMPOSE="$PROJECT_DIR/infra/development/docker-compose.yml"

case "${1:-help}" in
    plan)
        PYTHONPATH=. .venv/bin/python -u examples/milestone_build.py --plan-only
        ;;
    m[0-9]*)
        # Extract milestone spec: m1 -> 1, m1-3 -> 1-3
        SPEC="${1#m}"
        PYTHONPATH=. .venv/bin/python -u examples/milestone_build.py \
            --resume --milestone "$SPEC"
        ;;
    integration)
        PYTHONPATH=. .venv/bin/python -u examples/debug_integration.py "$PROJECT_DIR"
        ;;
    up)
        docker compose -f "$COMPOSE" up -d
        echo "Backend: http://localhost:8000"
        echo "Frontend: http://localhost:5173"
        echo "API docs: http://localhost:8000/docs"
        ;;
    down)
        docker compose -f "$COMPOSE" down
        ;;
    *)
        echo "Usage: $0 {plan|m1|m2|m1-3|integration|up|down}"
        exit 1
        ;;
esac
