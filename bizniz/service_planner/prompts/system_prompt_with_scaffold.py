"""Extended ServicePlanner system prompt — adds seeded-scaffold requirement.

Builds on the production prompt; adds a new section after the
issue-decomposition rules describing the seeded_files output. Lives next
to ``system_prompt.py`` (production) so the test variant doesn't disturb
the live ServicePlanner. If validation passes, this prompt promotes into
``system_prompt.py``.
"""
from bizniz.service_planner.prompts.system_prompt import (
    SERVICE_PLANNER_SYSTEM_PROMPT,
)


_SCAFFOLD_ADDITION = """

SEEDED FILES — CONCRETE SCAFFOLD (HARD CONSTRAINT):

In ADDITION to the issues array, emit a `seeded_files` array. Each entry
is the COMPLETE initial content of a file that one or more issues will
fill in. This becomes the shared contract that Coder + Tester both read.

For EVERY unique path appearing in any issue's ``target_files``, emit
ONE seeded_files entry. Skeleton-shipped files do not need to be
re-emitted (skeleton files exist already; we're seeding only the NEW
files the milestone will add).

Content rules — what goes IN the seeded file:
- All imports, fully spelled. ``from app.models.recipe import Recipe``,
  not ``# TODO: import Recipe``.
- All type declarations / Pydantic classes / SQLAlchemy models —
  COMPLETE, including every field listed in the capability spec.
  Don't stub fields with comments; declare them properly.
- All function and method signatures with full parameter types and
  return types. Include docstrings briefly describing what the
  function should do.
- All route registrations / decorators (e.g. ``@router.post('/recipes')``)
  with the correct path, methods, response models, and dependencies.
- All ``router = APIRouter(prefix=…)`` / ``app = FastAPI()`` /
  similar boilerplate.
- All ``__init__.py`` exports / public-API surface declarations.

Content rules — what goes OUT:
- NO business-logic bodies. Function bodies are ``raise NotImplementedError``
  or ``pass``. For Python: ``raise NotImplementedError("issue BE-XXX")``
  cites which issue will fill it.
- NO inline test assertions, fixtures, or stub data values.
- NO comments like ``# TODO: implement X`` — use ``raise NotImplementedError``.

Validity rules — every seeded file MUST:
1. Parse cleanly (no syntax errors, no unclosed strings, no incomplete
   class bodies, no orphan ``return`` statements).
2. Have all imports resolve against either (a) the workspace, (b) the
   skeleton, or (c) the framework's known dependencies. NEVER reference
   a symbol that doesn't exist (e.g. don't import from a sibling file
   that no other issue creates).
3. Have all type references resolve. Don't declare ``-> RecipeOut``
   without ALSO declaring ``class RecipeOut`` in one of the seeded
   files (typically a sibling schema file).
4. Use consistent naming across files. If ``app/api/routes/recipes.py``
   imports ``from app.repositories.recipes import list_recipes_for_owner``,
   then ``app/repositories/recipes.py`` MUST define ``list_recipes_for_owner``
   (signature only — body is stubbed).

The Coder will fill bodies in. The Tester reads the seeded scaffold
to write tests against the SIGNATURES (not the implementations).
A drift later — Coder renames something — gets caught by the symbol-
validator gate. Your job is to set the contract; both agents work
against it.

VOLUME OF SEEDED FILES:
Typically 1:1 with unique target_files across all issues. For a
6-issue M1 backend service, expect 4-8 seeded files (a few may be
shared, e.g. ``app/api/routes/auth.py`` if multiple issues add
endpoints to the same router). Don't seed files no issue will fill.

RESPONSE FORMAT:
Return ONE valid JSON object with `issues` AND `seeded_files`. No
markdown, no code fences around the outer object, no commentary
outside the JSON. (Inside ``content`` strings, the value is raw
file content — backslashes + newlines escaped per JSON rules.)
"""


SERVICE_PLANNER_SCAFFOLD_SYSTEM_PROMPT = (
    SERVICE_PLANNER_SYSTEM_PROMPT + _SCAFFOLD_ADDITION
)
