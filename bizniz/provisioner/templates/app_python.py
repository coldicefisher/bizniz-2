"""Generated boilerplate for a Python app service when no skeleton applies."""
from __future__ import annotations

from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)


_FRAMEWORK_DEFAULTS = {
    "fastapi": ["fastapi", "uvicorn", "pydantic", "httpx"],
    "flask": ["flask"],
    "django": ["django"],
}


def _generate_dockerfile(port: int) -> str:
    return (
        "FROM python:3.12-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY . .\n"
        "ENV PYTHONPATH=/app\n"
        f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{port}"]\n'
    )


def _generate_requirements(framework: str, declared: list) -> str:
    packages = list(declared) if declared else []
    base = ["pytest"]
    for pkg in base:
        if pkg not in packages:
            packages.append(pkg)
    for pkg in _FRAMEWORK_DEFAULTS.get(framework, []):
        if pkg not in packages:
            packages.insert(0, pkg)
    return "\n".join(packages) + "\n"


class PythonAppTemplate(InfraTemplate):
    """Emits a Dockerfile + requirements.txt for a Python app service.

    Used when the architect did NOT pick a skeleton for this service. The
    Provisioner calls this for every Python app whose skeleton is "none"
    or unset.
    """

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        port = ctx.service.port or 8000
        dockerfile = _generate_dockerfile(port)
        requirements = _generate_requirements(ctx.service.framework, ctx.service.requirements)

        return TemplateOutput(
            workspace_files={"requirements.txt": requirements},
            infra_files={"Dockerfile": dockerfile},
        )
