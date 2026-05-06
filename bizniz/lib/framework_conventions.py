"""Per-framework convention catalog used by Engineer + CodeReviewer.

Single source of truth for framework-specific knowledge that the v2
agents need to be effective on a given stack:

  - Engineer needs build-mode conventions ("USE these patterns to ship
    a React frontend / FastAPI backend / Angular dashboard").
  - CodeReviewer needs reviewer-mode calibration ("these patterns are
    REAL — don't false-positive them as hallucinations").

Same underlying facts, different framing per consumer.

When adding a new framework here, add both ``for_engineer`` and
``for_reviewer`` lists. Keep entries terse — these go straight into
prompt context and prompt context is expensive.
"""
from __future__ import annotations

from typing import Dict, List


# Framework names match ``ServiceDefinition.framework`` (lowercased,
# ``ServiceDefinition`` normalizes for us). Add aliases here if a
# skeleton uses a different name.
_FRAMEWORK_FACTS: Dict[str, Dict[str, List[str]]] = {
    "react": {
        "for_engineer": [
            "Files: .tsx for components, .ts for utilities/types/hooks, "
            ".test.tsx for tests.",
            "Routing: skeleton auto-mounts src/routes/*.tsx whose default "
            "export is a `RouteEntry` (or `RouteEntry[]`). Add new routes "
            "as new files in src/routes/ — do NOT register manually.",
            "State: prefer hooks (useState/useEffect/useReducer) for local "
            "state; if the skeleton already wires Zustand, use it for "
            "cross-component state.",
            "Build: Vite (npm run dev for HMR, npm run build for production). "
            "vite.config.ts MUST keep `allowedHosts: true` — docker DNS "
            "hostnames are blocked otherwise.",
            "Styling: Tailwind CSS v4 (skeleton-shipped). Prefer Tailwind "
            "utility classnames over raw CSS files.",
            "Tests: vitest with jsdom + @testing-library/react. "
            "Filename convention `*.test.tsx` for component tests, "
            "`*.test.ts` for pure-logic tests.",
        ],
        "for_reviewer": [
            "src/routes/*.tsx whose default export is a `RouteEntry` "
            "(or `RouteEntry[]`) is auto-mounted by the skeleton — it's "
            "real even if not registered explicitly elsewhere.",
            "Hooks (useState, useEffect, useReducer, useCallback, useMemo, "
            "useContext) are real imports from 'react'.",
            "JSX expressions in .tsx files return ReactNode; this is real "
            "even when the body uses fragments (`<>...</>`) or arrays.",
            "Tailwind utility classnames (flex, gap-4, text-sm, "
            "rounded-md, bg-blue-500, etc.) are real even though they "
            "look like fake CSS — Tailwind generates them at build time.",
            "Vite-specific imports (import.meta.env, import.meta.glob, "
            "?raw / ?url suffixes on imports) are real Vite features.",
        ],
    },
    "angular": {
        "for_engineer": [
            "Files: components are TRIPLETS — Foo.component.ts (the class) "
            "+ foo.component.html (template) + foo.component.css (styles). "
            "Services: foo.service.ts. Modules: foo.module.ts. Tests: "
            "foo.component.spec.ts (Jasmine `describe`/`it` syntax).",
            "Routing: Angular Router with explicit registration — "
            "`RouterModule.forRoot(routes)` at app root, "
            "`RouterModule.forChild(routes)` per feature module. "
            "Routes are NOT auto-discovered.",
            "State: services with RxJS observables (BehaviorSubject + "
            "asObservable() for cross-component state); Angular signals "
            "(signal/computed/effect from '@angular/core') for fine-grained "
            "reactivity in Angular 17+.",
            "Build: Angular CLI — `ng serve` for dev, `ng build` for prod. "
            "`angular.json` controls build config; don't try to introduce "
            "Vite or webpack overrides unless the skeleton explicitly asked.",
            "Styling: Angular Material (skeleton-shipped). Use MatFormFieldModule, "
            "MatButtonModule, MatInputModule, MatIconModule etc.; import "
            "them in the relevant NgModule's `imports` array.",
            "Tests: Jasmine + Karma by default (`ng test`). Some skeletons "
            "swap in Jest via @angular-builders/jest — check angular.json "
            "before assuming.",
        ],
        "for_reviewer": [
            "Component classes decorated with @Component({selector, "
            "templateUrl, styleUrls}) are real Angular components. "
            "Properties/methods on them are accessed by the .html "
            "template via Angular binding ({{ value }}, [prop], (event)) "
            "— don't flag missing usages of the properties just because "
            "you can't see them in the .ts file.",
            "Service classes decorated with @Injectable({providedIn: "
            "'root'}) are auto-instantiated singletons — don't flag "
            "missing manual instantiation. Constructor injection via "
            "`constructor(private foo: FooService) {}` is the standard "
            "wiring.",
            "Module classes decorated with @NgModule({declarations, "
            "imports, exports, providers}) are container objects. A "
            "component must be in some module's `declarations` to be "
            "usable, but the registration may be in a different file.",
            "RxJS operators (pipe, map, filter, mergeMap, switchMap, "
            "tap, catchError, etc.) are real imports from 'rxjs/operators' "
            "or, in newer versions, from 'rxjs'.",
            "Angular signals (signal, computed, effect, untracked) "
            "imported from '@angular/core' are real (Angular 17+).",
            "Angular Material module imports (MatFormFieldModule, "
            "MatButtonModule, MatInputModule, MatTableModule, MatDialog, "
            "etc.) are real even when not visibly registered — they're "
            "imported via NgModule.imports.",
            "Standalone components (Angular 14+) skip NgModule entirely: "
            "@Component({standalone: true, imports: [...]}). The imports "
            "array on the component itself is the registration.",
        ],
    },
    "fastapi": {
        "for_engineer": [
            "Routing: skeleton auto-mounts app/api/routes/*.py whose "
            "module exports a `router: APIRouter`. Add new routes as "
            "new files in app/api/routes/ — do NOT manually register "
            "in main.py.",
            "Models: SQLAlchemy 2.0 declarative — `Mapped[X]` + "
            "`mapped_column(...)` typing. Pydantic v2 for request/"
            "response schemas (use `BaseModel`, `Field`, `model_validator`).",
            "Validation: Pydantic v2 `Field(constraints)` for "
            "field-level rules; `model_validator(mode='after')` for "
            "cross-field rules.",
            "Auth: FusionAuth-issued RS256 JWTs. Use the skeleton's "
            "`get_current_user` dependency to extract the user, "
            "`require_roles(['role_name'])` for authorization. NEVER "
            "mint tokens or hash passwords yourself.",
            "Tests: pytest with httpx.AsyncClient (or sync Client) "
            "for endpoint tests. Test files in tests/integration/.",
        ],
        "for_reviewer": [
            "Files in app/api/routes/ exporting `router: APIRouter` are "
            "auto-mounted by the skeleton at startup — don't flag the "
            "route as 'unregistered'.",
            "SQLAlchemy 2.0 fields declared with `Mapped[X]` or "
            "`mapped_column(...)` are real attributes accessible at "
            "runtime via instance.attribute_name.",
            "Pydantic v2 fields with `Field(alias='X')` are accessible "
            "BOTH as `model.python_name` and via `model.model_dump(by_alias=True)['X']`.",
            "FastAPI dependency injection (`Depends(...)`, `Query(...)`, "
            "`Body(...)`, `Header(...)`, `Path(...)`) is resolved at "
            "runtime — these are real even though they look like "
            "default values.",
            "`get_current_user` and `require_roles` are skeleton-shipped "
            "helpers (in app/core/auth.py) — they exist, don't flag.",
            "SQLAlchemy 2.0's `select(...)` returns a Select object "
            "with chained methods (.where, .join, .order_by) — these "
            "are real even though they don't look like 1.x query API.",
        ],
    },
}


def render_for_engineer(architecture) -> str:
    """Render framework conventions block for the Engineer's seed context.

    Walks ``architecture.services``, picks each unique framework with a
    known facts entry, and emits a block in the Engineer's voice
    ("USE these conventions"). Returns "" if none of the services'
    frameworks are in the catalog.
    """
    blocks: List[str] = []
    seen: set = set()
    for service in architecture.services:
        fw = (service.framework or "").lower()
        if fw in seen or fw not in _FRAMEWORK_FACTS:
            continue
        seen.add(fw)
        facts = _FRAMEWORK_FACTS[fw]["for_engineer"]
        block = [
            f"### {service.framework} ({service.language}) — service "
            f"`{service.name}`"
        ]
        for fact in facts:
            block.append(f"- {fact}")
        blocks.append("\n".join(block))
    if not blocks:
        return ""
    return (
        "## Framework conventions (use these patterns)\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def render_for_reviewer(architecture) -> str:
    """Render framework calibration block for the CodeReviewer's user prompt.

    Walks ``architecture.services``, picks each unique framework with a
    known facts entry, and emits a block in the reviewer's voice
    ("these are REAL, don't flag"). Returns "" if no relevant frameworks.
    """
    blocks: List[str] = []
    seen: set = set()
    for service in architecture.services:
        fw = (service.framework or "").lower()
        if fw in seen or fw not in _FRAMEWORK_FACTS:
            continue
        seen.add(fw)
        facts = _FRAMEWORK_FACTS[fw]["for_reviewer"]
        block = [
            f"### {service.framework} (in service `{service.name}`)"
        ]
        for fact in facts:
            block.append(f"- {fact}")
        blocks.append("\n".join(block))
    if not blocks:
        return ""
    return (
        "## Framework calibration (these patterns are REAL — DO NOT flag)\n\n"
        + "\n\n".join(blocks)
        + "\n"
    )


def known_frameworks() -> List[str]:
    """List the framework names this catalog has facts for."""
    return sorted(_FRAMEWORK_FACTS.keys())
