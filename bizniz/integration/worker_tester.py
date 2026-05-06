"""WorkerTester — integration test author for background workers.

Mirrors HTTPApiTester / WebUITester structure but targets workers
(queue consumers, stream processors, schedulers, WebSocket servers).
Single LLM call producing a pytest module that exercises the worker
through its real event surface (queue/stream/websocket), not by
importing its source.
"""
from __future__ import annotations

import json
from typing import Dict, Optional

from bizniz.architect.types import ServiceDefinition
from bizniz.core.agent import BaseAIAgent
from bizniz.integration.worker_prompts import WORKER_TESTER_SYSTEM_PROMPT


class WorkerTester(BaseAIAgent):
    """Generate integration test files for worker services."""

    @property
    def _process_system_prompt(self) -> str:
        return WORKER_TESTER_SYSTEM_PROMPT

    def generate_test_file(
        self,
        problem_statement: str,
        service: ServiceDefinition,
        backend_contracts: Optional[Dict[str, dict]] = None,
        depends_on_services: Optional[Dict[str, ServiceDefinition]] = None,
        target_filepath: str = "tests/integration/test_worker.py",
        auth_contract: Optional[str] = None,
    ) -> str:
        """Returns a complete pytest file as a string.

        ``backend_contracts`` is the backends' OpenAPI dicts, keyed by
        service name. The worker tester uses these to set up
        prerequisite state (create users, resources) via the REST
        surface before enqueuing the worker job.

        ``depends_on_services`` is a name→ServiceDefinition map for
        the worker's declared dependencies (queue, cache, db). Lets
        the prompt cite real hostnames + ports.
        """
        prompt = self._build_prompt(
            problem_statement=problem_statement,
            service=service,
            backend_contracts=backend_contracts or {},
            depends_on_services=depends_on_services or {},
            target_filepath=target_filepath,
            auth_contract=auth_contract,
        )
        self.add_messages_to_history([{"role": "user", "content": prompt}])
        text, _, _ = self._ai_client.get_text(messages=self.message_history)
        source = self._strip_code_block(text or "")
        return source

    @staticmethod
    def _build_prompt(
        problem_statement: str,
        service: ServiceDefinition,
        backend_contracts: Dict[str, dict],
        depends_on_services: Dict[str, ServiceDefinition],
        target_filepath: str,
        auth_contract: Optional[str] = None,
    ) -> str:
        depends_block = "\n".join(
            f"  - {name}: {svc.service_type}/{svc.framework} "
            f"(language={svc.language}, port={svc.port})"
            for name, svc in (depends_on_services or {}).items()
        ) or "  (no dependencies declared)"

        # Slim backend contracts to the path list — full schemas are
        # rarely needed for worker setup steps.
        slim_backends: Dict[str, list] = {}
        for name, doc in (backend_contracts or {}).items():
            slim_backends[name] = sorted(list((doc.get("paths") or {}).keys()))

        auth_block = ""
        if auth_contract:
            auth_block = (
                "\n\nAUTH_CONTRACT.md (use these credentials to drive "
                "real auth flows for backend setup steps):\n"
                f"```markdown\n{auth_contract.strip()}\n```\n"
            )

        return (
            f"# Worker integration tests for `{service.name}`\n"
            f"\n"
            f"## Problem slice\n{problem_statement.strip()}\n"
            f"\n"
            f"## Worker service\n"
            f"  - name: {service.name}\n"
            f"  - framework: {service.framework}\n"
            f"  - language: {service.language}\n"
            f"  - port: {service.port}\n"
            f"  - depends_on: {', '.join(service.depends_on) or '(none)'}\n"
            f"\n"
            f"## Worker dependency services (already up in the compose stack)\n"
            f"{depends_block}\n"
            f"\n"
            f"## Backend services + their available paths\n"
            f"```json\n{json.dumps(slim_backends, indent=2)}\n```\n"
            f"{auth_block}\n"
            f"## Output target\n"
            f"`{target_filepath}` — a single pytest module, no prose, "
            f"no markdown fences.\n"
        )

    @staticmethod
    def _strip_code_block(text: str) -> str:
        text = text.strip()
        for fence in ("```python", "```py", "```"):
            if text.startswith(fence):
                text = text[len(fence):].lstrip()
                break
        if text.endswith("```"):
            text = text[:-3].rstrip()
        return text
