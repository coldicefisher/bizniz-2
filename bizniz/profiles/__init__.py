"""Service-type profiles.

Centralizes the per-(service_type, framework) decisions that were
previously spread across multiple files: which documenter extracts
contracts, which validator type-checks the source, which skeleton
the provisioner seeds from, and what contract format the service
exposes to other services.

Adding a new (service_type, framework) combination is one entry
in ``SERVICE_PROFILES``. The architect, engineer, integration
runner, and documenter persistence layer all look up by
``profile_for(service)`` rather than branching on framework
strings.

Guardrail: a service the planner emits with a combination that's
not in the registry raises ``UnknownServiceTypeError`` rather
than silently working with the wrong defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Type


@dataclass(frozen=True)
class ServiceProfile:
    """Per-(service_type, framework) configuration.

    Fields are intentionally optional so a profile can grow
    incrementally — Phase 5 lays down the structure, Phase 6 will
    actually consume ``validator``, etc.
    """
    service_type: str
    framework: str
    language: str

    # Phase 1+2+4: which documenter class to dispatch
    documenter: Optional[Type] = None

    # Phase 6: validator (run as post-flight type check) and the
    # sidecar/runner that executes it. The validator command is a
    # list of args; the orchestrator runs it in a sidecar based
    # on ``validator_runner``.
    validator: Optional[List[str]] = None
    validator_runner: Optional[str] = None  # e.g. "node-sidecar", "python", "docker:image"

    # Cross-service contract format the service exposes to others
    # (advisory; consumed by the contract-capture step).
    contract_format: Optional[str] = None  # "openapi", "typescript-d-ts", "event-schema", etc.

    # Provisioner skeleton repo identifier.
    skeleton: Optional[str] = None

    # Test runner used by the engineer for unit tests.
    test_runner: Optional[str] = None

    # Open metadata bag for profile-specific notes.
    extras: dict = field(default_factory=dict)


# ── lazy imports for the registry values ───────────────────────────

def _python_documenter():
    from bizniz.documenters.python_ast import PythonAstDocumenter
    return PythonAstDocumenter


def _typescript_documenter():
    from bizniz.documenters.typescript_ast import TypeScriptAstDocumenter
    return TypeScriptAstDocumenter


# ── the registry ───────────────────────────────────────────────────

# Profiles are keyed by ``(service_type, framework)``. Both are
# normalized to lowercase before lookup. Frameworks should match
# what the architect emits in ``ServiceDefinition.framework``.
_PROFILE_TABLE = [
    ServiceProfile(
        service_type="backend",
        framework="fastapi",
        language="python",
        documenter=_python_documenter,
        validator=["python", "-m", "pyright", "app/"],
        validator_runner="python",
        contract_format="openapi",
        skeleton="fastapi",
        test_runner="pytest",
    ),
    ServiceProfile(
        service_type="frontend",
        framework="react",
        language="typescript",
        documenter=_typescript_documenter,
        validator=["npx", "tsc", "--noEmit"],
        validator_runner="node-sidecar",
        contract_format="typescript-d-ts",
        skeleton="react",
        test_runner="jest",
    ),
    ServiceProfile(
        service_type="frontend",
        framework="angular",
        language="typescript",
        documenter=_typescript_documenter,
        validator=["npx", "tsc", "--noEmit"],
        validator_runner="node-sidecar",
        contract_format="typescript-d-ts",
        skeleton="angular",
        test_runner="jest",
    ),
    # Worker / consumer / queue / etc. get added here as we land them.
]


SERVICE_PROFILES = {
    (p.service_type.lower(), p.framework.lower()): p
    for p in _PROFILE_TABLE
}


class UnknownServiceTypeError(ValueError):
    """Raised when a (service_type, framework) pair has no profile.

    The intent is to fail loudly at planning time rather than
    silently misbehaving downstream. If you hit this, either the
    planner hallucinated a framework we don't actually support, or
    we need to add a profile entry.
    """


def profile_for(service) -> ServiceProfile:
    """Look up the profile for a ``ServiceDefinition``-like object.

    Accepts anything with ``.service_type`` and ``.framework``
    attributes (so callers can pass either a real
    ``ServiceDefinition`` or a test stub).

    Raises ``UnknownServiceTypeError`` for combinations not in the
    registry.
    """
    st = (getattr(service, "service_type", "") or "").lower()
    fw = (getattr(service, "framework", "") or "").lower()
    key = (st, fw)
    if key not in SERVICE_PROFILES:
        raise UnknownServiceTypeError(
            f"No profile for (service_type={st!r}, framework={fw!r}). "
            f"Known: {sorted(SERVICE_PROFILES.keys())}. "
            f"Add a ServiceProfile entry to bizniz/profiles/__init__.py "
            f"to support this combination."
        )
    return SERVICE_PROFILES[key]


def has_profile(service_type: str, framework: str) -> bool:
    """Check whether a given (service_type, framework) is supported."""
    return (service_type.lower(), framework.lower()) in SERVICE_PROFILES


def documenter_for(service) -> Optional[Type]:
    """Return the documenter CLASS for a service, or None if the
    profile has no documenter (e.g. infrastructure services).

    Soft variant of ``profile_for(s).documenter`` that returns None
    instead of raising — most callers want to skip extraction
    silently for unknown combinations rather than crash.
    """
    try:
        prof = profile_for(service)
    except UnknownServiceTypeError:
        return None
    if prof.documenter is None:
        return None
    # Profiles store lazy callables to avoid import cycles; resolve here.
    if callable(prof.documenter) and not isinstance(prof.documenter, type):
        return prof.documenter()
    return prof.documenter
