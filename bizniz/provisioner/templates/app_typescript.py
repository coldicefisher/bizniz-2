"""Generated boilerplate for a TypeScript app service when no skeleton applies."""
from __future__ import annotations

import json

from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)


def _generate_dockerfile(service_type: str) -> str:
    """Dev image — workspace is bind-mounted by the test environment.

    For frontends, run a vite dev server so ``docker compose up``
    serves the app at the declared port. For non-frontends (e.g. a
    standalone TypeScript service), default to running the test
    suite — the deployed container is rarely the goal for those.
    """
    base = (
        "FROM node:20-slim\n"
        "WORKDIR /workspace\n"
        "COPY package*.json ./\n"
        "RUN npm install\n"
        "COPY . .\n"
    )
    if service_type == "frontend":
        # vite dev server is what `npm run dev` runs in the package.json below.
        return base + 'CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]\n'
    return base + 'CMD ["npx", "jest"]\n'


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


def _generate_skeleton_md(service_type: str) -> str:
    """Minimal SKELETON.md for the no-skeleton TypeScript fallback so
    the engineer aligns its code to the Dockerfile + package.json
    conventions the Provisioner just emitted.
    """
    if service_type == "frontend":
        return """# Minimal TypeScript Frontend Contract (no skeleton)

This service has no skeleton — you are building it from scratch.
The Provisioner has emitted a minimal ``Dockerfile`` and
``package.json``; everything else is yours.

## Hard contract: the entrypoint

The Dockerfile runs ``npm run dev`` which (per ``package.json``)
runs ``vite``. Vite expects:
- ``index.html`` at the workspace root, with a script tag pointing
  at your entry module (typically ``/src/main.tsx`` or ``/src/main.ts``)
- ``src/main.tsx`` (or .ts) — mounts your app to ``#root`` in
  ``index.html``

## Where files go

- ``index.html`` — Vite's HTML entrypoint
- ``src/main.tsx`` — bootstrap; renders your root component
- ``src/<feature>.tsx`` — your components, organized however you
  like (or under ``src/components/``, ``src/pages/``, etc.)
- ``src/__tests__/<feature>.test.ts`` — Jest test files
- ``vite.config.ts`` — minimal Vite config (you'll need to create
  this: import the React plugin, set the dev server port to the
  one your service was assigned)
- ``tsconfig.json`` — TypeScript config

## What you may NOT do

- ❌ Skip ``index.html`` or ``src/main.tsx``. Vite needs both.
- ❌ Hardcode the dev server port in your code; it's set in
  ``vite.config.ts``.
- ❌ Put files outside ``src/`` and expect Vite to find them.

## The contract in one sentence

``index.html`` + ``src/main.tsx`` are the entrypoint contract. Vite
runs ``npm run dev``; your code mounts to ``#root``.
"""
    return """# Minimal TypeScript Service Contract (no skeleton)

This service has no skeleton — you are building it from scratch.
The Provisioner has emitted a minimal ``Dockerfile`` and
``package.json``; everything else is yours.

## Hard contract

The Dockerfile runs ``npx jest`` (your test suite). For a
deployable service, you must define your own entry script and
update the Dockerfile's CMD.

## Where files go

- ``src/<feature>.ts`` — your code, organized however you like
- ``src/__tests__/<feature>.test.ts`` — Jest test files
- ``tsconfig.json`` — TypeScript config (create as needed)

## The contract in one sentence

Code goes in ``src/``; tests go in ``src/__tests__/``; the rest is
your call.
"""


class TypeScriptAppTemplate(InfraTemplate):
    """Emits a Dockerfile + package.json + SKELETON.md for a
    TypeScript app service when no skeleton applies. SKELETON.md
    declares what the Dockerfile + package.json expect (vite dev
    server for frontends, jest for non-frontends) so the engineer
    aligns its code accordingly.
    """

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        dockerfile = _generate_dockerfile(ctx.service.service_type)
        pkg_json = _generate_package_json(
            ctx.service.name, ctx.project_slug, ctx.service.service_type,
        )
        skeleton_md = _generate_skeleton_md(ctx.service.service_type)
        return TemplateOutput(
            workspace_files={
                "package.json": pkg_json,
                "SKELETON.md": skeleton_md,
            },
            infra_files={"Dockerfile": dockerfile},
        )
