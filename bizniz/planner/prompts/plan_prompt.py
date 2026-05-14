PLAN_PROMPT_TEMPLATE = """\
Problem statement:
{problem_statement}

Project name: {project_name}
Project slug: {project_slug}

Decompose this project into an ordered sequence of milestones.

For each milestone, produce:
  - sequence_index: 0-based position in the build order.
  - name: short label (3–6 words).
  - problem_slice: SELF-CONTAINED problem statement for just this
    milestone. The Architect will read this in isolation and decompose
    it into services. Include enough context that the Architect doesn't
    need to know about other milestones.
  - use_cases: list of user stories this milestone delivers, in
    "user can <do thing>" form.
  - success_criteria: list of testable outcomes from a user's
    perspective.
  - depends_on_names: list of other milestone names (from THIS plan)
    that must ship before this one.
  - estimated_effort: rough sizing — "S" (a few days), "M" (about a
    week), or "L" (1–2 weeks). Use "L" if you're unsure.

Also produce:
  - project_name (echo back).
  - project_slug (echo back).
  - description: 1–2 sentence overview of the whole plan.
  - milestones: the ordered list, sequence_index ascending from 0.
"""


def build_plan_prompt(
    problem_statement: str,
    project_name: str,
    project_slug: str,
) -> str:
    return PLAN_PROMPT_TEMPLATE.format(
        problem_statement=problem_statement,
        project_name=project_name,
        project_slug=project_slug,
    )
