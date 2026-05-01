"""WebUITester — agent that authors Playwright tests for web frontends.

Reads the problem statement + frontend service definition + optional
backend OpenAPI contract, emits a Playwright .spec.ts file that
exercises the live UI. The runner separately executes those tests
against the running stack via a Playwright sidecar container.

Framework-blind by design: the same agent works for React, Vue,
Angular, Svelte, Astro, anything that serves HTML+JS over HTTP.
Playwright treats the page as a black box.
"""
from __future__ import annotations

import json
from typing import Optional

from bizniz.core.agent import BaseAIAgent
from bizniz.architect.types import ServiceDefinition
from bizniz.integration.web_ui_prompts import WEB_UI_TESTER_SYSTEM_PROMPT


class WebUITester(BaseAIAgent):
    """Generate Playwright integration test files from a problem
    statement + service definition. One instance per frontend service
    per run."""

    @property
    def _process_system_prompt(self) -> str:
        return WEB_UI_TESTER_SYSTEM_PROMPT

    def generate_test_file(
        self,
        problem_statement: str,
        service: ServiceDefinition,
        backend_contracts: Optional[dict] = None,
        target_filepath: str = "tests/integration/ui.spec.cjs",
    ) -> str:
        """Returns a complete TypeScript file as a string, ready to
        write to ``target_filepath`` in the service workspace.

        ``backend_contracts`` is a dict of {service_name: openapi_doc}
        for any backend services that have been captured. Lets the
        UI tester write tests that know what API calls SHOULD happen
        when the user interacts with the page.
        """
        prompt = self._build_prompt(
            problem_statement=problem_statement,
            service=service,
            backend_contracts=backend_contracts or {},
            target_filepath=target_filepath,
        )
        self.add_messages_to_history([{"role": "user", "content": prompt}])
        text, _, _ = self._ai_client.get_text(
            messages=self.message_history,
        )
        return self._strip_code_block(text or "")

    @staticmethod
    def _build_prompt(
        problem_statement: str,
        service: ServiceDefinition,
        backend_contracts: dict,
        target_filepath: str,
    ) -> str:
        # Slim contracts to just paths+methods (full schemas would
        # bloat the prompt and the UI tester doesn't need them —
        # network assertions are coarse-grained).
        slim_contracts = {}
        for svc_name, doc in backend_contracts.items():
            paths = doc.get("paths", {})
            slim_contracts[svc_name] = sorted(paths.keys())

        return (
            f"PROBLEM STATEMENT:\n{problem_statement}\n\n"
            f"FRONTEND SERVICE:\n"
            f"- name: {service.name}\n"
            f"- framework: {service.framework}\n"
            f"- language: {service.language}\n"
            f"- port: {service.port}\n"
            f"- description: {service.description}\n\n"
            f"BACKEND ENDPOINTS (the frontend should be calling these):\n"
            f"{json.dumps(slim_contracts, indent=2)}\n\n"
            f"Write the Playwright integration test file. Target path: "
            f"{target_filepath}. Return ONLY the TypeScript source — no "
            f"markdown, no fences, no prose."
        )
