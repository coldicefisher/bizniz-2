"""Builds the project's .env file from the architecture + template env vars."""
from __future__ import annotations

from typing import Dict

from bizniz.architect.types import SystemArchitecture


def build_env_file(
    architecture: SystemArchitecture,
    template_env_vars: Dict[str, str],
) -> str:
    """Compose a .env string with project metadata and template-contributed
    env vars (postgres credentials, FusionAuth keys, etc.).
    """
    lines = [
        f"# {architecture.project_name} — development environment",
        f"PROJECT_NAME={architecture.project_slug}",
        "",
    ]

    # Group template-contributed vars
    grouped: Dict[str, list] = {}
    for key, value in template_env_vars.items():
        prefix = key.split("_", 1)[0]
        grouped.setdefault(prefix, []).append((key, value))

    for prefix in sorted(grouped):
        lines.append(f"# {prefix}")
        for key, value in sorted(grouped[prefix]):
            lines.append(f"{key}={value}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
