_ANALYZE_PROMPT_PYTHON = """
Analyze the following problem statement and produce a complete engineering breakdown.

{skeleton_contract}

{auth_context}

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

{architecture_context}

Produce a JSON response with:
- "business_requirements": list of strings (business goals served)
- "use_cases": list of objects with "title" and "description"
- "functional_requirements": list of strings (concrete capabilities)
- "nonfunctional_requirements": list of strings (performance, security, etc.)
- "issues": list of objects, each with:
    - "title": action-phrase name for the coding task
    - "description": detailed description including which classes/functions to implement
    - "target_files": list of objects with "filepath" and "action" ("create" or "modify")
    - "test_files": list of test file paths (e.g. "tests/test_expense_manager.py")
    - "depends_on": list of issue titles this issue depends on (empty if none)
    - "suggested_model": the AI model to start with for this issue based on complexity.
      AVAILABLE MODELS (ordered cheapest to most capable): {available_models}
      Choose the cheapest model that can reliably solve the task. Use cheaper models for simple tasks (data classes, enums, basic CRUD). Use more capable models for complex tasks (multi-module, complex algorithms). Only pick from the list above.
    - "test_setup_hint": how to set up tests for this issue. CRITICAL for endpoint/route issues.

RULES FOR EXTRACTING SPEC DETAIL — ATTRIBUTE COMPLETENESS:
- The problem statement is the source of truth. Every concrete noun
  the spec associates with a domain entity (attributes, fields,
  configurable properties) MUST end up in the corresponding model
  or schema. Do not silently drop attributes because a subset feels
  enough — the spec listed them, so they're in scope.
  Example: "products have name, price, sku, weight" → all four
  fields appear; not just name and price.
- If an attribute is plausibly out-of-scope for the CURRENT
  milestone (e.g. file uploads, while milestone is text-only),
  call it out explicitly in the issue description with a TODO
  rather than silently omitting it.

RULES FOR ISSUES — SINGLE RESPONSIBILITY IS MANDATORY:
- Each issue MUST have exactly ONE focused responsibility.
- Each issue should touch 1-2 target files max (plus __init__.py if needed).
- Each issue MUST have its OWN dedicated test file — do NOT share test files between issues.
  Example: "Implement ServicesRepository" → tests/test_services_repository.py
           "Implement AppointmentsRepository" → tests/test_appointments_repository.py
  NEVER assign the same test file to multiple issues.
- If a task has multiple concerns, split it: "Create app factory and DI providers" → 2 issues.
- When a Skeleton directory contract is present, file paths MUST be
  inside the skeleton's declared extension points (e.g. for the
  FastAPI skeleton: app/api/routes/<feature>.py,
  app/models/<feature>.py, app/schemas/<feature>.py). NEVER place
  files in a parallel package outside the skeleton's root.
- Without a skeleton, all paths must be inside the package
  namespace or tests/ directory.
- Domain model issues come FIRST — they define shared types other issues depend on.
- An issue can create multiple files (e.g. a domain model file + its __init__.py update).
- test_files paths should start with "tests/".
- Order issues by dependency graph — if issue B depends on issue A, A comes first.
- depends_on references issues by their title string.
- Do NOT group related functionality — each class/module gets its own issue.

TEST SETUP HINTS — REQUIRED for endpoint/route/integration issues:
- For each issue, provide a "test_setup_hint" explaining how tests should be set up.
- For endpoint/route issues: explain how the app is constructed, how to import it for
  testing, and how to create a test client.
- When a skeleton contract is present, the test_setup_hint should
  reference the skeleton's app entrypoint (e.g. for FastAPI:
    "The FastAPI app is `app.main:app` from app/main.py. Tests:
     from app.main import app; from fastapi.testclient import TestClient;
     client = TestClient(app)").
- Without a skeleton, the test_setup_hint should reference the
  generated package's app factory.
- For issues that integrate with other components: explain import paths and required mocks.
- For standalone units (data classes, pure functions): use empty string "".
"""

_ANALYZE_PROMPT_TYPESCRIPT = """
Analyze the following problem statement and produce a complete engineering breakdown.
This is a TypeScript project.

{skeleton_contract}

{auth_context}

PROBLEM STATEMENT:
──────────────────────────────────────────────────────────────
{problem_statement}

{architecture_context}

Produce a JSON response with:
- "business_requirements": list of strings (business goals served)
- "use_cases": list of objects with "title" and "description"
- "functional_requirements": list of strings (concrete capabilities)
- "nonfunctional_requirements": list of strings (performance, security, etc.)
- "issues": list of objects, each with:
    - "title": action-phrase name for the coding task
    - "description": detailed description including which classes/functions to implement
    - "target_files": list of objects with "filepath" and "action" ("create" or "modify")
    - "test_files": list of test file paths (e.g. "src/__tests__/App.test.tsx", "src/__tests__/utils.test.ts")
    - "depends_on": list of issue titles this issue depends on (empty if none)
    - "suggested_model": the AI model to start with for this issue based on complexity.
      AVAILABLE MODELS (ordered cheapest to most capable): {available_models}
      Choose the cheapest model that can reliably solve the task. Use cheaper models for simple tasks (interfaces, enums, basic CRUD). Use more capable models for complex tasks (multi-module, complex algorithms). Only pick from the list above.
    - "test_setup_hint": how to set up tests for this issue. CRITICAL for endpoint/route issues.

RULES FOR EXTRACTING SPEC DETAIL — ATTRIBUTE COMPLETENESS:
- The problem statement is the source of truth. Every concrete noun
  the spec associates with a domain entity (attributes, fields,
  configurable properties) MUST end up in the corresponding model
  or schema. Do not silently drop attributes because a subset feels
  enough — the spec listed them, so they're in scope.
  Example: "products have name, price, sku, weight" → all four
  fields appear; not just name and price.
- If an attribute is plausibly out-of-scope for the CURRENT
  milestone (e.g. file uploads, while milestone is text-only),
  call it out explicitly in the issue description with a TODO
  rather than silently omitting it.

RULES FOR ISSUES — SINGLE RESPONSIBILITY IS MANDATORY:
- Each issue MUST have exactly ONE focused responsibility.
- Each issue should touch 1-2 target files max.
- Each issue MUST have its OWN dedicated test file — do NOT share test files between issues.
  NEVER assign the same test file to multiple issues.
- If a task has multiple concerns, split it into separate issues.
- All file paths must use .ts or .tsx extensions.
- Test files must end in .test.ts or .test.tsx (Jest convention).
- Domain model issues come FIRST — they define shared types other issues depend on.
- An issue can create multiple files (e.g. a component file + its test file).
- Order issues by dependency graph — if issue B depends on issue A, A comes first.
- depends_on references issues by their title string.
- Do NOT group related functionality — each class/module gets its own issue.

TEST SETUP HINTS — REQUIRED for endpoint/route/integration issues:
- For each issue, provide a "test_setup_hint" explaining how tests should be set up.
- For endpoint/route issues: explain how the app is constructed, how to import it for
  testing, and how to create a test client. Example for Express:
    "The Express app is exported from src/app.ts. Tests should:
     import app from '../app'; import request from 'supertest';
     const response = await request(app).get('/api/services')"
- For issues that integrate with other components: explain import paths and required mocks.
- For standalone units (interfaces, pure functions): use empty string "".
"""


def get_analyze_prompt(language: str = "python") -> str:
    if language == "typescript":
        return _ANALYZE_PROMPT_TYPESCRIPT
    return _ANALYZE_PROMPT_PYTHON


# Backward compatibility
ANALYZE_PROMPT_TEMPLATE = _ANALYZE_PROMPT_PYTHON
