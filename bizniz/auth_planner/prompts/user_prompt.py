"""User-facing prompt for one AuthPlanner call."""
from __future__ import annotations

from bizniz.architect.types import SystemArchitecture


def build_auth_planner_prompt(
    *,
    problem_slice: str,
    architecture: SystemArchitecture,
) -> str:
    services = "\n".join(
        f"  - {s.name} ({s.service_type}/{s.framework}, {s.language})"
        for s in architecture.services
    )
    return (
        "## Project\n\n"
        f"**{architecture.project_name}** (`{architecture.project_slug}`)\n"
        f"{architecture.description}\n\n"
        f"## Services\n\n{services}\n\n"
        f"## Milestone problem slice\n\n{problem_slice}\n\n"
        "## Your job\n\n"
        "Read the problem slice. Extract the user-facing roles + "
        "test users this milestone needs. Emit ONE AuthSpec JSON "
        "object matching the schema. Do not include super_admin in "
        "test_users (the seeded admin is implicit). Use snake_case "
        "for role names. One test user per non-admin role minimum."
    )
