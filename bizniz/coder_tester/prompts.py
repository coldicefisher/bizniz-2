"""System + user prompts for CoderTesterAgent (v4).

Per-issue scope: agent sees ONE issue at a time, with seeded scaffold
for that issue's files plus the capability spec, plus a directory
listing of the workspace so the agent knows the surrounding shape.

The system prompt is deliberately stricter than CoderAgentV3's:
- the agent ALSO writes tests (no separate Tester downstream)
- the issue's target_files + test_files define the legal output set
- the agent is encouraged to encode edge cases from its own
  implementation into its tests (the whole reason we merged the
  roles)
"""
from __future__ import annotations

from typing import List, Optional

from bizniz.architect.types import ServiceDefinition
from bizniz.coder.types import Issue
from bizniz.coder_tester.types import FilledFile
from bizniz.quality_engineer.types import CapabilitySpec


CODER_TESTER_SYSTEM_PROMPT = """You are a senior software engineer
implementing ONE atomic issue end-to-end: code AND tests, in one
pass. There is no separate Tester downstream; the tests you write
are the ones that will run.

HARD CONSTRAINTS:

1. **One issue, one envelope.** You see exactly one issue's spec.
   Fill EVERY file in `target_files` and EVERY file in `test_files`.
   Do not invent paths outside that set.

2. **Code and tests stay aligned BECAUSE YOU WROTE BOTH.** This is
   the whole reason the roles are merged. Encode the edge cases
   your implementation handles INTO the tests. If your code's error
   path returns `{"detail": "..."}` for duplicate email, your test
   asserts exactly that shape. No drift, no "tester misread the spec".

3. **Respect the seeded scaffold.** Imports, function signatures,
   class field types, route registrations, decorators — all already
   set. Fill bodies. Do NOT rename or change signatures. Other
   issues in this milestone consume your symbols by exact name.

4. **Replace every `raise NotImplementedError`.** The seed used it
   as a stub marker. Your filled output must have ZERO remaining
   `NotImplementedError` instances.

5. **Tests must be REAL.** Real fixtures, real assertions, exercise
   the system. Do NOT write `assert True` or "smoke tests" that pass
   trivially. If you can't write a real assertion because the
   dependency isn't ready yet, that's a `notes` line — do NOT ship a
   fake-passing test.

6. **Imports must resolve.** Use only:
   - Python stdlib (or TS stdlib for typescript issues)
   - The skeleton's declared dependencies (requirements.txt / package.json)
   - Symbols defined in other files in this milestone's seeded scaffold
     (cross-issue references)
   - Symbols shipped by the skeleton itself

7. **Honor the auth contract.** Auth is delegated to the configured
   provider (FusionAuth by default). No local password hashing, no
   local JWT minting — only JWT VALIDATION via the skeleton's
   `get_current_user` dependency.

8. **Forbidden patterns.** No `except Exception:` blocks that
   swallow errors and re-raise as generic 500s. No assertion-less
   tests. No TODO-only test bodies. No `print()` debug stubs left in.

OUTPUT:

Return ONE JSON object matching the provided schema. The
`filled_files` array MUST contain exactly the paths listed in the
issue's `target_files` + `test_files`. Each entry's `role` is
"code" for a target_file or "test" for a test_file (informational).
"""


def build_user_prompt(
    *,
    issue: Issue,
    service: ServiceDefinition,
    seeded_files: List[FilledFile],
    capabilities: List[CapabilitySpec],
    skeleton_md: Optional[str] = None,
    auth_contract: Optional[str] = None,
    sibling_issue_summaries: Optional[List[str]] = None,
) -> str:
    """Build the per-issue user prompt.

    ``sibling_issue_summaries`` is a 1-line description of every
    other issue in this milestone (id + title + target_files). The
    agent uses this to know what cross-issue symbols it can reference
    by name, without needing the full text of every other issue.
    """
    sections: List[str] = []

    sections.append(f"## Target service\n")
    sections.append(f"- name: `{service.name}`")
    sections.append(f"- framework: {service.framework} / {service.language}")
    sections.append(f"- workspace: `{service.workspace_name}/`")

    sections.append(f"\n## Issue to deliver (ONE issue, all-at-once)\n")
    sections.append(f"### `{issue.id}` — {issue.title}")
    sections.append(issue.description)
    sections.append(f"\n- target_files: {issue.target_files}")
    sections.append(f"- test_files: {issue.test_files}")
    if issue.success_criteria:
        sections.append("- success criteria:")
        for sc in issue.success_criteria:
            sections.append(f"  - {sc}")
    if issue.spec_refs:
        sections.append(f"- capability refs: {issue.spec_refs}")
    if issue.depends_on:
        sections.append(f"- depends_on (already coded): {issue.depends_on}")

    # Capabilities relevant to THIS issue (filtered by spec_refs).
    relevant_caps = [c for c in capabilities if c.id in (issue.spec_refs or [])]
    if relevant_caps:
        sections.append(f"\n## Capability specs for this issue\n")
        for c in relevant_caps:
            sections.append(f"### `{c.id}` — {c.name}")
            if c.description:
                sections.append(c.description)
            if c.inputs:
                sections.append("**Inputs:**")
                for f in c.inputs:
                    req = "required" if f.required else "optional"
                    sections.append(f"  - `{f.name}` ({f.type}, {req}): {f.description}")
            if c.outputs:
                sections.append("**Outputs:**")
                for f in c.outputs:
                    sections.append(f"  - `{f.name}` ({f.type}): {f.description}")
            if c.validation_rules:
                sections.append("**Validation rules:**")
                for r in c.validation_rules:
                    sections.append(f"  - {r}")
            if c.error_cases:
                sections.append("**Error cases:**")
                for e in c.error_cases:
                    sections.append(f"  - {e}")
            if c.test_scenarios:
                sections.append("**Test scenarios (your tests MUST cover these):**")
                for ts in c.test_scenarios:
                    sections.append(f"  - {ts}")
            sections.append("")

    # Seeded scaffold for THIS issue's files only.
    sections.append("\n## Seeded scaffold for this issue\n")
    if seeded_files:
        sections.append(
            "These are the CURRENT scaffolds for your target/test files. "
            "Imports + signatures + types are the contract. Fill the bodies."
        )
        sections.append("")
        for sf in seeded_files:
            sections.append(f"### `{sf.path}`")
            sections.append("```")
            sections.append(sf.content)
            sections.append("```")
            sections.append("")
    else:
        sections.append(
            "(No seeded scaffold supplied for this issue's paths — write "
            "files from scratch but respect existing module shape in the "
            "workspace.)"
        )

    if sibling_issue_summaries:
        sections.append("\n## Sibling issues in this milestone (for cross-issue references)\n")
        sections.append(
            "Symbols defined in these issues are available by exact name. "
            "Their files are not shown here in full — reference by name only."
        )
        for s in sibling_issue_summaries:
            sections.append(f"- {s}")

    if skeleton_md:
        sections.append(f"\n## Skeleton contract\n\n{skeleton_md}")
    if auth_contract:
        sections.append(f"\n## Auth contract\n\n{auth_contract}")

    sections.append(
        "\n## Your job\n\n"
        "Emit a JSON object with two fields: `issue_id` (echo the id "
        "above) and `filled_files` (one entry per path in target_files "
        "+ test_files, with `path`, `content`, and `role`). Match the "
        "contract, replace every `NotImplementedError`, write real "
        "tests that encode YOUR implementation's edge cases."
    )

    return "\n".join(sections)
