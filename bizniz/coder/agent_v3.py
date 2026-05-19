"""``CoderAgentV3`` — single-dispatch coder for the v3 pipeline spec.

Takes (seeded scaffold + N issue specs + supporting context) and emits,
via structured output in ONE LLM call, the filled-bodies version of
every target_file across all issues.

Differs from production ``ClaudeCliCoder`` in two important ways:
1. **One call, N issues** — collapses today's per-issue dispatch
   pattern. The agent sees the whole milestone scope + the contract.
2. **Pure structured output** — no tool loop, no Read/Edit/Write/Bash.
   The agent emits JSON: ``{filled_files: [{path, content}, …]}``.
   Bizniz writes the files to disk. Deterministic + inspectable.

Pure single-shot is the strongest version of the v3 spec — proving
this works at milestone scale is the load-bearing claim. If it
ships clean quality, parallel CoderAgent + TesterAgent against the
same scaffold is feasible.

Lives next to ``claude_cli_coder.py`` (production) so this test
variant doesn't disturb live builds.
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.coder.types import Issue
from bizniz.lib.llm_utils import call_with_retry
from bizniz.quality_engineer.types import EnrichedSpec


# ── Output schema ────────────────────────────────────────────────


CODER_AGENT_V3_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "coder_agent_v3_output",
        "schema": {
            "type": "object",
            "properties": {
                "filled_files": {
                    "type": "array",
                    "description": (
                        "Every file in the seeded scaffold (or referenced "
                        "by an issue's target_files) with all bodies filled "
                        "in. Function bodies are real implementations; tests "
                        "have real assertions. Imports, signatures, and "
                        "type declarations match the seed (no drift)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": (
                                    "Workspace-relative path, same as the "
                                    "seeded file's path."
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": (
                                    "COMPLETE filled file. Imports + classes "
                                    "+ signatures preserved from the seed; "
                                    "bodies are real implementations. NO "
                                    "remaining ``raise NotImplementedError``."
                                ),
                            },
                        },
                        "required": ["path", "content"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["filled_files"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


# ── Prompt ───────────────────────────────────────────────────────


CODER_AGENT_V3_SYSTEM_PROMPT = """You are a senior software engineer
implementing a milestone's worth of work in one pass.

A ServicePlanner emitted N issue specs PLUS a seeded scaffold for
every file those issues will fill. The scaffold is the binding
contract: imports, function signatures, type declarations, route
registrations, and Pydantic class field lists are ALREADY DEFINED.
Your job is to fill in the bodies for every stubbed function and
add any missing test assertions / data setup.

HARD CONSTRAINTS:

1. **Respect the seeded contract.** Do not change function names,
   parameter types, return types, decorators, or class field types.
   The TesterAgent will (in production) write tests against this
   contract independently — if you drift, the tests break. New
   *helper* functions you add are OK; renames of existing
   signatures are not.

2. **Replace every ``raise NotImplementedError``.** The seed used
   this as a stub marker. Your filled output must have ZERO
   remaining ``NotImplementedError`` instances. If a body genuinely
   needs upstream work, leave a clearly-commented ``TODO`` instead
   — but every test issue should have real assertions, not TODOs.

3. **Test issues get REAL test bodies.** When an issue says
   "Integration tests for X", the corresponding test_*.py file's
   functions must have real assertions, fixture setup, and
   exercise the system end-to-end. Tests must be runnable by
   ``pytest`` against the live stack.

4. **Code issues get REAL implementations.** When an issue says
   "Add /me endpoint and response schema", the route handler's
   body must do the actual work (validate auth, build response,
   return). Pydantic schemas must define every field listed in
   the capability spec.

5. **All imports must resolve.** Use only:
   - Python stdlib
   - The skeleton's declared dependencies (in requirements.txt)
   - Symbols defined in OTHER files in this milestone's seeded
     scaffold (cross-file references)
   - Symbols shipped by the skeleton itself

6. **Honor the auth contract.** The pipeline delegates identity to
   the auth provider (FusionAuth by default). No local password
   hashing, no JWT minting — only JWT VALIDATION via the
   skeleton's ``get_current_user`` dependency.

OUTPUT FORMAT:

Return ONE valid JSON object matching the provided schema. The
``filled_files`` array MUST include every path from the seeded
scaffold AND every unique path referenced by any issue's
``target_files``. No markdown fences around the outer JSON.
Inside ``content`` strings, the value is raw file content
(backslashes + newlines escaped per JSON rules).
"""


# ── Agent ────────────────────────────────────────────────────────


class FilledFile(BaseModel):
    path: str
    content: str


class CoderAgentV3Result(BaseModel):
    filled_files: List[FilledFile] = Field(default_factory=list)


class CoderAgentV3Error(Exception):
    """LLM output failed validation or returned an empty result."""


class CoderAgentV3:
    """Single-dispatch coder. Fills all milestone target_files in
    one call against a seeded scaffold."""

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._on_status = on_status
        self._max_retries = max_retries

    def fill_milestone(
        self,
        *,
        architecture: SystemArchitecture,
        enriched_spec: EnrichedSpec,
        service: ServiceDefinition,
        issues: List[Issue],
        seeded_files: List[FilledFile],
        skeleton_md: Optional[str] = None,
        auth_contract: Optional[str] = None,
    ) -> CoderAgentV3Result:
        """One LLM call fills the bodies for every issue in ``issues``."""
        self._log(
            f"CoderAgentV3: {service.name} — "
            f"{len(issues)} issue(s), {len(seeded_files)} seeded file(s)"
        )

        user_prompt = _build_prompt(
            architecture=architecture,
            enriched_spec=enriched_spec,
            service=service,
            issues=issues,
            seeded_files=seeded_files,
            skeleton_md=skeleton_md,
            auth_contract=auth_contract,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=CODER_AGENT_V3_SYSTEM_PROMPT),
                Message(role="user", content=user_prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=CODER_AGENT_V3_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label=f"CoderAgentV3({service.name})",
        )

        items = raw.get("filled_files") or []
        if not items:
            raise CoderAgentV3Error(
                f"CoderAgentV3({service.name}): empty filled_files. "
                f"Refusing to ship a no-op result."
            )

        filled: List[FilledFile] = []
        for it in items:
            try:
                filled.append(FilledFile(**it))
            except Exception as e:
                raise CoderAgentV3Error(
                    f"filled_files entry failed validation: "
                    f"{type(e).__name__}: {e}; item: {it}"
                )

        self._log(
            f"CoderAgentV3: {service.name} → "
            f"{len(filled)} file(s) filled"
        )
        return CoderAgentV3Result(filled_files=filled)

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass


# ── Prompt builder ───────────────────────────────────────────────


def _build_prompt(
    *,
    architecture: SystemArchitecture,
    enriched_spec: EnrichedSpec,
    service: ServiceDefinition,
    issues: List[Issue],
    seeded_files: List[FilledFile],
    skeleton_md: Optional[str] = None,
    auth_contract: Optional[str] = None,
) -> str:
    sections: List[str] = []

    sections.append(f"## Target service\n\n- name: `{service.name}`")
    sections.append(f"- framework: {service.framework} / {service.language}")
    sections.append(f"- workspace: `{service.workspace_name}/`")

    sections.append("\n## Milestone capabilities (EnrichedSpec)\n")
    if enriched_spec.capabilities:
        for c in enriched_spec.capabilities:
            sections.append(f"### `{c.id}` — {c.name}")
            if c.description:
                sections.append(c.description)
            if c.test_scenarios:
                sections.append("**Test scenarios:**")
                for ts in c.test_scenarios:
                    sections.append(f"  - {ts}")
            sections.append("")

    sections.append("\n## Issues to fill (single dispatch, all at once)\n")
    for i in issues:
        sections.append(f"### `{i.id}` — {i.title}")
        sections.append(i.description)
        sections.append(f"- target_files: {i.target_files}")
        if i.success_criteria:
            sections.append("- success criteria:")
            for sc in i.success_criteria:
                sections.append(f"  - {sc}")
        if i.depends_on:
            sections.append(f"- depends_on: {i.depends_on}")
        sections.append("")

    sections.append("\n## Seeded scaffold (the binding contract)\n")
    sections.append(
        "Every file below is the CURRENT scaffold for this milestone. "
        "Imports + signatures + types are the contract. Fill the bodies; "
        "do not change the signatures."
    )
    sections.append("")
    for sf in seeded_files:
        sections.append(f"### `{sf.path}`")
        sections.append("```")
        sections.append(sf.content)
        sections.append("```")
        sections.append("")

    if skeleton_md:
        sections.append(f"\n## Skeleton contract\n\n{skeleton_md}")
    if auth_contract:
        sections.append(f"\n## Auth contract\n\n{auth_contract}")

    sections.append(
        "\n## Your job\n\n"
        "Emit a JSON object with ONE field, `filled_files`. Every "
        "seeded file above must appear there with its bodies filled in. "
        "Match the contract. Replace every `raise NotImplementedError`. "
        "Tests get real assertions, code gets real implementations."
    )

    return "\n".join(sections)
