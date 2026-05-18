"""HTTPApiTester — agent that authors integration tests for HTTP APIs.

Reads the problem statement + service definition + the service's live
OpenAPI contract, emits a complete pytest+httpx module that exercises
domain behavior. The runner separately executes those tests against
the running stack.

Framework-blind by design: the same agent works for FastAPI, Express,
Flask, Spring, any backend that speaks HTTP. The only contract the
runner enforces is "one Python file the runner can pytest".
"""
from __future__ import annotations

import json
import time
from typing import Optional

from bizniz.core.agent import BaseAIAgent
from bizniz.architect.types import ServiceDefinition
from bizniz.integration.prompts import HTTP_API_TESTER_SYSTEM_PROMPT


class HTTPApiTester(BaseAIAgent):
    """Generate integration test files from a problem statement +
    OpenAPI contract. One instance per backend service per run."""

    @property
    def _process_system_prompt(self) -> str:
        return HTTP_API_TESTER_SYSTEM_PROMPT

    def generate_test_file(
        self,
        problem_statement: str,
        service: ServiceDefinition,
        openapi_doc: dict,
        target_filepath: str = "tests/integration/test_api.py",
        auth_contract: Optional[str] = None,
    ) -> str:
        """Returns a complete Python file as a string, ready to write
        to ``target_filepath`` in the service workspace.

        ``auth_contract`` is the verbatim text of the project's
        AUTH_CONTRACT.md when one exists. When provided, the tester
        MUST drive real auth flows and test protected endpoints with
        real tokens — no skipping, no mocking.
        """
        prompt = self._build_prompt(
            problem_statement=problem_statement,
            service=service,
            openapi_doc=openapi_doc,
            target_filepath=target_filepath,
            auth_contract=auth_contract,
        )
        self.add_messages_to_history([{"role": "user", "content": prompt}])
        # Emit a perf_log-friendly timing line so the integration phase
        # shows up in the report. Without this the testers were invisible
        # to perf_log and we couldn't attribute the "everything else"
        # bucket of wall time.
        t0 = time.time()
        text, _, _ = self._ai_client.get_text(
            messages=self.message_history,
        )
        elapsed = time.time() - t0
        print(
            f"HTTPApiTester({service.name}): completed in {elapsed:.1f}s",
            flush=True,
        )
        source = self._strip_code_block(text or "")
        return source

    @staticmethod
    def _build_prompt(
        problem_statement: str,
        service: ServiceDefinition,
        openapi_doc: dict,
        target_filepath: str,
        auth_contract: Optional[str] = None,
    ) -> str:
        # Trim the openapi doc — full schemas can balloon prompt cost.
        # Keep paths + methods + summaries + parameter/body schemas;
        # drop component-level schemas referenced only by examples.
        slim_paths = {}
        for path, ops in (openapi_doc.get("paths") or {}).items():
            slim_ops = {}
            for method, op in (ops or {}).items():
                if not isinstance(op, dict):
                    continue
                slim_ops[method] = {
                    "summary": op.get("summary"),
                    "parameters": op.get("parameters"),
                    "requestBody": op.get("requestBody"),
                    "responses": {
                        code: {"description": (resp or {}).get("description")}
                        for code, resp in (op.get("responses") or {}).items()
                    },
                }
            if slim_ops:
                slim_paths[path] = slim_ops

        components = openapi_doc.get("components", {}).get("schemas", {})

        if auth_contract:
            auth_section = (
                f"AUTH CONTRACT (the project has FusionAuth-backed authentication; "
                f"you MUST exercise auth end-to-end — no skipping protected "
                f"endpoints, no mocking, no faking tokens):\n\n"
                f"{auth_contract}\n"
            )
        else:
            auth_section = (
                "AUTH CONTRACT: none. This service has no authentication. "
                "Do not invent auth headers."
            )

        return (
            f"PROBLEM STATEMENT:\n{problem_statement}\n\n"
            f"SERVICE:\n"
            f"- name: {service.name}\n"
            f"- type: {service.service_type}\n"
            f"- framework: {service.framework}\n"
            f"- language: {service.language}\n"
            f"- port: {service.port}\n"
            f"- description: {service.description}\n\n"
            f"{auth_section}\n\n"
            f"OPENAPI PATHS (slimmed):\n"
            f"{json.dumps(slim_paths, indent=2)}\n\n"
            f"OPENAPI SCHEMAS (request/response shapes):\n"
            f"{json.dumps(components, indent=2)}\n\n"
            f"Write the integration test file. Target path: "
            f"{target_filepath}. Return ONLY the Python source — no "
            f"markdown, no fences, no prose."
        )
