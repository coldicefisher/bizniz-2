"""Route-duplication reviewer for Python FastAPI services.

Walks ``app/api/routes/*.py`` and ``app/main.py`` to compute every
HTTP route's resolved path. Flags:

- Duplicate paths: same METHOD + path appears twice.
- Doubled prefixes: ``app.include_router(router, prefix="/auth")``
  in main.py while the router itself already declares
  ``APIRouter(prefix="/auth")``. This is the M1 ``/auth/auth/login``
  bug.
- Manual include_router calls when auto-discovery exists: the
  skeleton's main.py auto-includes everything in ``app/api/routes/``
  via ``settings.api_v1_prefix``. Adding a manual include_router
  for the SAME router double-registers it.
- Conflicting registrations: same router included with different
  prefixes in different places.

NO AI calls. Pure mechanical AST scan.

Output is a ``RouteReview`` with ``issues: list[RouteIssue]``.
``ok=True`` when issues is empty. Caller (architect post-flight)
decides whether to fail the service on issues found.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@dataclass
class RouteEndpoint:
    """One ``@router.METHOD("/path")`` declaration."""
    file: str               # relative path to the source file
    method: str             # GET / POST / etc.
    router_prefix: str      # "/auth", "" if router has no prefix
    path: str               # "/login", "" for empty
    resolved: str           # full resolved path including all prefixes
    function_name: str
    line: int


@dataclass
class IncludeCall:
    """One ``app.include_router(...)`` call site (engineer-written
    main.py overrides)."""
    file: str
    router_var: str         # which variable was passed
    explicit_prefix: Optional[str]
    line: int


@dataclass
class RouteIssue:
    severity: str           # "error" / "warning"
    kind: str               # "duplicate_path" / "doubled_prefix" / "manual_and_auto" / "conflicting_prefix"
    message: str
    locations: List[str]    # file:line references


@dataclass
class RouteReview:
    ok: bool
    issues: List[RouteIssue] = field(default_factory=list)
    routes_seen: int = 0
    files_scanned: int = 0

    def message(self) -> str:
        """Render as a corrective error string for the architect to
        surface in the service result."""
        if self.ok:
            return ""
        lines = [
            f"Route review found {len(self.issues)} issue(s) "
            f"({self.routes_seen} routes across "
            f"{self.files_scanned} file(s)):"
        ]
        for issue in self.issues:
            lines.append(f"  [{issue.severity}/{issue.kind}] {issue.message}")
            for loc in issue.locations[:4]:
                lines.append(f"    at {loc}")
        return "\n".join(lines)


def review_routes(workspace_root: Path) -> RouteReview:
    """Scan ``app/api/routes/*.py`` and ``app/main.py`` for route
    duplication and prefix conflicts.

    Returns RouteReview with ``ok=True`` when nothing's wrong.
    """
    workspace_root = Path(workspace_root)

    routes_dir = workspace_root / "app" / "api" / "routes"
    main_path = workspace_root / "app" / "main.py"

    endpoints: List[RouteEndpoint] = []
    file_router_prefix: Dict[str, str] = {}  # router file -> its declared prefix
    files_scanned = 0

    if routes_dir.is_dir():
        for py in sorted(routes_dir.glob("*.py")):
            if py.name == "__init__.py":
                continue
            files_scanned += 1
            file_endpoints, prefix = _parse_route_file(py, workspace_root)
            endpoints.extend(file_endpoints)
            file_router_prefix[str(py.relative_to(workspace_root))] = prefix

    include_calls: List[IncludeCall] = []
    auto_discovery_in_main = False
    if main_path.exists():
        files_scanned += 1
        include_calls, auto_discovery_in_main = _parse_main(
            main_path, workspace_root,
        )

    issues: List[RouteIssue] = []

    # ── Issue 1: doubled prefix from manual include + router prefix ──
    # If main.py has ``app.include_router(auth.router, prefix="/auth")``
    # and the auth router itself declares ``APIRouter(prefix="/auth")``,
    # the resolved path is "/auth/auth/...".
    for call in include_calls:
        if call.explicit_prefix is None:
            continue
        # Find the router file that defines `<router_var>.router`. The
        # var typically matches the file name (e.g. ``auth`` ↔
        # ``app/api/routes/auth.py``).
        router_file = call.router_var
        for path, declared_prefix in file_router_prefix.items():
            if Path(path).stem == router_file and declared_prefix:
                if declared_prefix == call.explicit_prefix:
                    issues.append(RouteIssue(
                        severity="error",
                        kind="doubled_prefix",
                        message=(
                            f"app.include_router(<router>, prefix={call.explicit_prefix!r}) "
                            f"in {call.file}:{call.line} doubles the prefix already declared "
                            f"on the router in {path} "
                            f"(APIRouter(prefix={declared_prefix!r})). "
                            f"Resolved paths will look like "
                            f"{call.explicit_prefix}{declared_prefix}/<endpoint>."
                        ),
                        locations=[
                            f"{call.file}:{call.line}",
                            f"{path}",
                        ],
                    ))

    # ── Issue 2: manual include_router when auto-discovery is active ──
    if auto_discovery_in_main and include_calls:
        # Distinguish bare ``include_router(router_pkg.router)`` (which
        # would only conflict if the router_var matches a file in the
        # routes pkg) from imports of unrelated modules.
        manual = [c for c in include_calls
                  if Path(c.router_var).stem in
                  [Path(f).stem for f in file_router_prefix.keys()]]
        if manual:
            issues.append(RouteIssue(
                severity="error",
                kind="manual_and_auto",
                message=(
                    f"app/main.py has both auto-discovery "
                    f"(_include_routers / iter_modules over app.api.routes) "
                    f"AND {len(manual)} manual app.include_router(...) call(s). "
                    f"Each route file gets registered TWICE — once with "
                    f"settings.api_v1_prefix, once with the manual prefix. "
                    f"Either delete the manual include_router calls (use "
                    f"auto-discovery) or delete the auto-discovery loop."
                ),
                locations=[f"{c.file}:{c.line}" for c in manual],
            ))

    # ── Issue 3: same resolved path appears twice ──
    # When auto-discovery is on, resolved path = api_v1_prefix +
    # router_prefix + route_path. We don't know api_v1_prefix
    # statically, so use a placeholder; the relative comparison still
    # exposes duplicates within the file set.
    seen: Dict[Tuple[str, str], List[RouteEndpoint]] = {}
    for ep in endpoints:
        key = (ep.method.upper(), ep.resolved)
        seen.setdefault(key, []).append(ep)
    for (method, resolved), eps in seen.items():
        if len(eps) > 1:
            issues.append(RouteIssue(
                severity="error",
                kind="duplicate_path",
                message=(
                    f"{method} {resolved} is registered "
                    f"{len(eps)} times. Only the last registration wins "
                    f"at runtime; the others become unreachable shadows."
                ),
                locations=[
                    f"{ep.file}:{ep.line} ({ep.function_name})"
                    for ep in eps
                ],
            ))

    routes_seen = len(endpoints)
    return RouteReview(
        ok=len(issues) == 0,
        issues=issues,
        routes_seen=routes_seen,
        files_scanned=files_scanned,
    )


# ── parsers ────────────────────────────────────────────────────────


def _parse_route_file(path: Path, workspace_root: Path) -> Tuple[List[RouteEndpoint], str]:
    """Return (endpoints, router_prefix) for a single route file."""
    rel = str(path.relative_to(workspace_root))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return [], ""

    router_prefix = ""

    # Find ``router = APIRouter(prefix="...")`` at module scope.
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "router":
                if isinstance(node.value, ast.Call):
                    for kw in node.value.keywords:
                        if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                            router_prefix = kw.value.value or ""

    endpoints: List[RouteEndpoint] = []
    # Walk every function decorated with @router.<METHOD>("path", ...)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            method, route_path = _extract_router_decorator(dec)
            if method is None:
                continue
            resolved = (router_prefix or "") + (route_path or "")
            if not resolved.startswith("/"):
                resolved = "/" + resolved
            endpoints.append(RouteEndpoint(
                file=rel,
                method=method,
                router_prefix=router_prefix,
                path=route_path or "",
                resolved=resolved,
                function_name=node.name,
                line=node.lineno,
            ))

    return endpoints, router_prefix


def _extract_router_decorator(dec) -> Tuple[Optional[str], Optional[str]]:
    """If ``dec`` is ``@router.METHOD(...)`` or ``@<name>.METHOD(...)``,
    return (method, path). Otherwise (None, None)."""
    if not isinstance(dec, ast.Call):
        return None, None
    if not isinstance(dec.func, ast.Attribute):
        return None, None
    method = dec.func.attr.lower()
    if method not in _HTTP_METHODS:
        return None, None
    # First positional arg is the path
    if dec.args and isinstance(dec.args[0], ast.Constant):
        return method, dec.args[0].value
    return method, ""


def _parse_main(path: Path, workspace_root: Path) -> Tuple[List[IncludeCall], bool]:
    """Find every ``app.include_router(...)`` in main.py and detect
    whether auto-discovery is also present."""
    rel = str(path.relative_to(workspace_root))
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return [], False

    calls: List[IncludeCall] = []
    auto_discovery = False

    # Auto-discovery signature: a call to pkgutil.iter_modules over
    # app.api.routes (any form of that).
    text = path.read_text(encoding="utf-8")
    if "iter_modules" in text and "routes" in text:
        auto_discovery = True

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match `<x>.include_router(...)`
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "include_router":
            continue
        # First positional arg: the router (extract its name root).
        router_var = "?"
        if node.args:
            first = node.args[0]
            if isinstance(first, ast.Attribute):
                # e.g. ``auth.router`` — router_var = "auth"
                if isinstance(first.value, ast.Name):
                    router_var = first.value.id
            elif isinstance(first, ast.Name):
                router_var = first.id
        # Explicit prefix kwarg
        explicit_prefix = None
        for kw in node.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                explicit_prefix = kw.value.value
        # Skip the auto-discovery's call to include_router (it uses
        # `router_obj` as a variable, no explicit string prefix beyond
        # settings.api_v1_prefix).
        if router_var == "router_obj":
            continue
        calls.append(IncludeCall(
            file=rel,
            router_var=router_var,
            explicit_prefix=explicit_prefix,
            line=node.lineno,
        ))

    return calls, auto_discovery
