"""
Architect.evolve prompt — re-decompose a project for one milestone,
preserving services that already exist.

Used in evolve-mode (one Architect call per Milestone walked by the
top-level Director loop). The Architect sees the existing architecture
and the milestone's problem-slice, and returns an updated architecture
where each service is tagged NEW (added for this milestone), EXTENDED
(already existed but the milestone adds work to it), or UNCHANGED.
"""
from bizniz.architect.skeletons import skeletons_summary_for_prompt


EVOLVE_PROMPT_TEMPLATE = """\
You are evolving an existing project to deliver one milestone of work.

PROJECT: {project_name}  (slug: {project_slug})

OVERALL PROBLEM STATEMENT (already partially built):
{problem_statement}

EXISTING ARCHITECTURE (services already in the project):
{existing_services}

{workspace_state_block}
MILESTONE TO DELIVER:
  Name:           {milestone_name}
  Effort:         {milestone_effort}
  Problem slice:  {milestone_problem_slice}

  Use cases (what the user gets after this milestone ships):
{use_cases_block}

  Success criteria (testable outcomes):
{success_criteria_block}

YOUR JOB:

Return an updated architecture that delivers the milestone above.
Every service from the existing architecture MUST appear in your
response (don't drop services). For each service set evolve_state:

  - "new"        — this service is being added for THIS milestone.
                   Doesn't exist in the existing architecture above.
  - "extended"   — this service existed before, but this milestone
                   adds new endpoints/components/code to it.
  - "unchanged"  — this service existed and this milestone does not
                   touch it.

Tag a service "extended" only if the milestone genuinely needs new
code in it (new endpoints, new domain models, new UI pages). If the
milestone delivers entirely-new functionality in a separate service,
add a NEW service rather than extending an existing one — but prefer
extending an existing service over duplicating it.

EXISTING SERVICES MUST KEEP their original name, framework,
language, port, and skeleton choice. Only add to depends_on or
requirements; don't rewrite them.

You do NOT generate docker-compose.yml. The Provisioner builds compose
deterministically from your service list and the registered
infrastructure templates.

For NEW services, follow the same rules as a fresh decompose:

IMPORTANT framework rules:
- **EXPLICIT USER CONSTRAINTS WIN.** If the problem statement names a
  specific framework (e.g., "Frontend: React", "use FastAPI"), honor
  it — defaults below apply ONLY when the problem is silent.
- Backend: Python with FastAPI by default.
- Frontend: React with TypeScript by default; Angular only when the
  problem is silent AND the UI is dashboard-heavy.
- Never Node.js for backends; never C#/.NET for new projects.

Authentication is REQUIRED whenever the milestone involves user
accounts or anything user-scoped. If the existing architecture
already includes a fusionauth service, reuse it (keep evolve_state
"unchanged" or "extended"). If not and the milestone needs it, ADD a
fusionauth auth service AND a postgres database.

Available skeletons:
{skeletons}

Skeleton selection rules (for NEW services only — existing services
keep their original skeleton):
- "fastapi" for Python/FastAPI backends.
- "react" is the default frontend WHEN the problem is silent on framework.
- "angular" only when the problem is silent AND the UI is dashboard-heavy.
- "teams-*" only when the system needs realtime fan-out feeds.
- Infrastructure (database/cache/proxy/auth): always "none" — the
  Provisioner has dedicated templates.

Container-port reference (set ``service.port`` to this — it's the port
the framework's dev server listens on inside its container; Provisioner
handles host-side mapping + collision remap):
- fastapi → 8000   |   teams-backend → 8000
- react → 5173
- angular → 4200   |   teams-frontend → 4200
- teams-consumer → no port
- fusionauth → 9011  |  postgres → 5432  |  redis → 6379

Return a JSON object with project_name, project_slug, description,
and the FULL updated services list (existing + new + extended).
""".replace("{skeletons}", skeletons_summary_for_prompt())


def build_evolve_prompt(
    *,
    project_name: str,
    project_slug: str,
    problem_statement: str,
    existing_services: str,
    milestone_name: str,
    milestone_effort: str,
    milestone_problem_slice: str,
    use_cases_block: str,
    success_criteria_block: str,
    workspace_state_block: str = "",
) -> str:
    return EVOLVE_PROMPT_TEMPLATE.format(
        project_name=project_name,
        project_slug=project_slug,
        problem_statement=problem_statement,
        existing_services=existing_services,
        workspace_state_block=workspace_state_block,
        milestone_name=milestone_name,
        milestone_effort=milestone_effort or "(unspecified)",
        milestone_problem_slice=milestone_problem_slice,
        use_cases_block=use_cases_block,
        success_criteria_block=success_criteria_block,
    )
