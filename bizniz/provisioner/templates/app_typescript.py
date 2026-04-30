"""Generated boilerplate for a TypeScript app service when no skeleton applies."""
from __future__ import annotations

import json

from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)


def _generate_dockerfile() -> str:
    """Dev image — workspace is bind-mounted by the test environment, so we
    only need the deps installed at image-build time."""
    return (
        "FROM node:20-slim\n"
        "WORKDIR /workspace\n"
        "COPY package*.json ./\n"
        "RUN npm install\n"
        'CMD ["npx", "jest"]\n'
    )


def _generate_package_json(service_name: str, project_slug: str, service_type: str) -> str:
    jest_config = {
        "preset": "ts-jest",
        "roots": ["<rootDir>/src", "<rootDir>/tests"],
        "testMatch": ["**/*.test.ts", "**/*.test.tsx"],
    }
    if service_type == "frontend":
        jest_config["testEnvironment"] = "jest-environment-jsdom"

    pkg = {
        "name": f"{project_slug}-{service_name}",
        "version": "0.1.0",
        "private": True,
        "scripts": {
            "build": "tsc" if service_type != "frontend" else "vite build",
            "dev": "vite" if service_type == "frontend" else "ts-node src/main.ts",
            "test": "jest",
        },
        "devDependencies": {
            "jest": "^29.7.0",
            "ts-jest": "^29.1.0",
            "typescript": "^5.3.0",
            "@types/jest": "^29.5.0",
        },
        "jest": jest_config,
    }
    if service_type == "frontend":
        pkg["devDependencies"].update({
            "@testing-library/jest-dom": "^6.1.0",
            "@testing-library/react": "^14.1.0",
            "react": "^18.2.0",
            "react-dom": "^18.2.0",
            "@types/react": "^18.2.0",
            "@types/react-dom": "^18.2.0",
            "jest-environment-jsdom": "^29.7.0",
        })
    return json.dumps(pkg, indent=2) + "\n"


class TypeScriptAppTemplate(InfraTemplate):
    """Emits a Dockerfile + package.json for a TypeScript app service when
    no skeleton applies."""

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        dockerfile = _generate_dockerfile()
        pkg_json = _generate_package_json(
            ctx.service.name, ctx.project_slug, ctx.service.service_type,
        )
        return TemplateOutput(
            workspace_files={"package.json": pkg_json},
            infra_files={"Dockerfile": dockerfile},
        )
