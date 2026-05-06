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
from bizniz.integration.contract_guard import validate_form_field_contract


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
        auth_contract: Optional[str] = None,
    ) -> str:
        """Returns a complete TypeScript file as a string, ready to
        write to ``target_filepath`` in the service workspace.

        ``backend_contracts`` is a dict of {service_name: openapi_doc}
        for any backend services that have been captured. Lets the
        UI tester write tests that know what API calls SHOULD happen
        when the user interacts with the page.

        ``auth_contract`` is the verbatim text of the project's
        AUTH_CONTRACT.md when one exists. When provided, the tester
        MUST drive real login flows in the UI — no skipping, no
        mocking, no DOM-presence-only checks for auth.
        """
        prompt = self._build_prompt(
            problem_statement=problem_statement,
            service=service,
            backend_contracts=backend_contracts or {},
            target_filepath=target_filepath,
            auth_contract=auth_contract,
        )
        self.add_messages_to_history([{"role": "user", "content": prompt}])
        text, _, _ = self._ai_client.get_text(
            messages=self.message_history,
        )
        source = self._strip_code_block(text or "")

        # Contract-shape validation only. Hallucination detection moved
        # to the v2 ``CodeReviewer`` which runs as a post-flight pass
        # over all changed files (catches fabricated symbols/types
        # across the whole milestone, not just per-file domain leakage).
        c = validate_form_field_contract(source, backend_contracts or {})
        if not c.ok:
            self.add_messages_to_history([
                {"role": "assistant", "content": source},
                {"role": "user", "content": c.message()},
            ])
            text2, _, _ = self._ai_client.get_text(messages=self.message_history)
            source = self._strip_code_block(text2 or source)
            c2 = validate_form_field_contract(source, backend_contracts or {})
            if not c2.ok:
                raise ValueError(
                    f"WebUITester output failed contract-shape validation "
                    f"after one corrective retry. Refusing to write a "
                    f"contaminated test file. Reason: {c2.message()[:400]}"
                )

        return source

    @staticmethod
    def _build_prompt(
        problem_statement: str,
        service: ServiceDefinition,
        backend_contracts: dict,
        target_filepath: str,
        auth_contract: Optional[str] = None,
    ) -> str:
        # Slim contracts down but KEEP request body schemas — the UI
        # tester drives forms that submit to these endpoints, so
        # field-name drift (`username` vs `email`) silently invents
        # bugs. Earlier this stripped to paths only; that masked the
        # contract from the AI and the tests submitted wrong fields.
        slim_contracts = {}
        for svc_name, doc in backend_contracts.items():
            slim_paths = {}
            for path, ops in (doc.get("paths") or {}).items():
                if not isinstance(ops, dict):
                    continue
                slim_ops = {}
                for method, op in ops.items():
                    if not isinstance(op, dict):
                        continue
                    rb = op.get("requestBody") or {}
                    rb_schema = (
                        rb.get("content", {}).get("application/json", {}).get("schema")
                    )
                    slim_ops[method] = {
                        "summary": op.get("summary"),
                        "requestBody": rb_schema,
                    }
                if slim_ops:
                    slim_paths[path] = slim_ops
            slim_contracts[svc_name] = slim_paths

        if auth_contract:
            auth_section = (
                "AUTH CONTRACT (the project has FusionAuth-backed authentication; "
                "you MUST drive the real login UI flow, NOT fake authentication "
                "in test code):\n\n"
                f"{auth_contract}\n"
            )
        else:
            auth_section = (
                "AUTH CONTRACT: none. This frontend has no authentication. "
                "Do not invent login flows."
            )

        return (
            f"PROBLEM STATEMENT:\n{problem_statement}\n\n"
            f"FRONTEND SERVICE:\n"
            f"- name: {service.name}\n"
            f"- framework: {service.framework}\n"
            f"- language: {service.language}\n"
            f"- port: {service.port}\n"
            f"- description: {service.description}\n\n"
            f"{auth_section}\n\n"
            f"BACKEND ENDPOINTS (the frontend should be calling these):\n"
            f"{json.dumps(slim_contracts, indent=2)}\n\n"
            f"Write the Playwright integration test file. Target path: "
            f"{target_filepath}. Return ONLY the TypeScript source — no "
            f"markdown, no fences, no prose."
        )
