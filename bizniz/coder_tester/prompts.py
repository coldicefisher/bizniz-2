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

SELF-VALIDATE BEFORE EMITTING:

Before you write the JSON, trace through your output ONCE in your head:

a. **Every import line.** For each `import X` and `from X import Y`,
   confirm X is in stdlib, in declared dependencies (requirements.txt
   / package.json), in this milestone's other seeded scaffold files,
   or in the skeleton itself. If X is none of those, REMOVE the
   import or replace with something that does resolve.

b. **Every cross-issue symbol reference.** For each function or class
   you call from another file, confirm it's mentioned in the sibling
   issue summaries OR in the seeded scaffold's signatures. If not,
   you're hallucinating — either inline the logic or remove the call.

c. **Every test assertion.** Your tests call YOUR code. Confirm the
   function names + signatures match exactly between the code and
   test files in your output.

d. **No leftover `NotImplementedError`** in any filled file (skeleton
   used these as stubs; your bodies must replace them).

Self-validate quietly — the only thing you emit is the JSON envelope.
If you catch an issue during self-validate, fix it BEFORE writing the
JSON. Do not emit a "pre_validation" field or commentary; the
deterministic gates downstream don't read it. The point is to ship
clean code on the first try, saving the fix-pass round-trip.

OUTPUT:

Return ONE JSON object matching the provided schema. The
`filled_files` array MUST contain exactly the paths listed in the
issue's `target_files` + `test_files`. Each entry's `role` is
"code" for a target_file or "test" for a test_file (informational).
"""


# v4 fix B (2026-05-20): edit-mode system prompt for REPAIR. The
# agent emits surgical patches instead of whole-file content.
# Unchanged code in the existing file stays verbatim — eliminates
# the cross-fix-issue conflict pattern where whole-file overwrites
# erased adjacent fix-issues' work.
CODER_TESTER_EDIT_SYSTEM_PROMPT = """You are a senior software engineer
fixing ONE atomic issue's code + tests. Two output paths:

  - **edits**: for files that ALREADY EXIST (shown in the scaffold).
    Surgical find/replace patches. Preserve unchanged code.
  - **new_files**: for paths in target_files / test_files that DO
    NOT appear in the scaffold yet (no current content shown).
    Whole-file content for creation.

HARD CONSTRAINTS:

1. **One issue, multiple precise patches.** You see exactly one
   issue's spec. Emit ONE or MORE FileEdit entries with
   `path / old_text / new_text`. Each `old_text` must be a UNIQUE
   substring of the file's current content. Pad with surrounding
   lines until unique.

2. **Preserve unchanged code.** Do NOT regenerate or rewrite lines
   you aren't actively changing. The whole point of edit mode is
   that lines you don't touch stay verbatim — that's how we avoid
   the "fix-breaks-unrelated-code" regression class.

3. **Multiple edits to the same file are applied in order.** Each
   subsequent edit sees the prior edits' results. Order yours so
   the `old_text` matches what's on disk after earlier edits.

4. **Code and tests stay aligned BECAUSE YOU WROTE BOTH.** When you
   edit a function's signature, also edit the corresponding test
   assertion in the same call. No drift across patches.

5. **No whole-file edits.** If your `old_text` would equal (or
   nearly equal) the entire file content, you're doing it wrong —
   that's whole-file mode. Break into focused patches; leave a
   `notes` line if a section genuinely needs full rewrite.

6. **Replace every `raise NotImplementedError`.** A common edit
   target: stub markers from the original scaffold.

7. **Tests must be REAL.** Real fixtures, real assertions. No
   stubs. No `assert True`.

8. **Honor the auth contract.** No local password hashing, no JWT
   minting — only JWT validation via `get_current_user`.

9. **Forbidden patterns.** No `except Exception:` re-raising as
   generic 500s. No assertion-less tests.

OUTPUT:

Return ONE JSON object matching the provided edit-mode schema.
- ``edits`` array — one entry per surgical change to an EXISTING
  file. Runner applies via find/replace.
- ``new_files`` array — one entry per path that needs to be CREATED.
  Runner writes the whole-file content directly.

For each path in the issue's target_files + test_files, decide:
existing in the scaffold? → use edits. Not in the scaffold? → use
new_files. Don't put the same path in both.
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
    edit_mode: bool = False,
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

    if edit_mode:
        sections.append(
            "\n## Your job (EDIT MODE — REPAIR)\n\n"
            "Two output paths, both go into the same JSON envelope:\n\n"
            "**A. For files ALREADY in the scaffold above** → use "
            "`edits`. Surgical find/replace patches. Preserve unchanged "
            "code. Each edit: `{path, old_text, new_text, role}`. "
            "`old_text` must be a UNIQUE substring of the file's "
            "current content — pad with surrounding lines until unique. "
            "Multiple edits to the same file apply IN ORDER.\n\n"
            "**B. For files in target_files / test_files but NOT in "
            "the scaffold** (paths that don't exist yet) → use "
            "`new_files`. Whole-file content for creation. Each entry: "
            "`{path, content, role}`.\n\n"
            "Rules:\n"
            "- Same path NEVER goes in both `edits` and `new_files`.\n"
            "- For `edits`: do NOT regenerate unchanged code. Preserve "
            "everything your edit doesn't touch.\n"
            "- For `edits`: do NOT emit a single edit covering the "
            "whole file. Break into focused patches.\n"
            "- If you can't decide whether a file exists, look for it "
            "in the scaffold above. Present → edit. Absent → new_file.\n"
        )
    else:
        sections.append(
            "\n## Your job\n\n"
            "Emit a JSON object with two fields: `issue_id` (echo the id "
            "above) and `filled_files` (one entry per path in target_files "
            "+ test_files, with `path`, `content`, and `role`). Match the "
            "contract, replace every `NotImplementedError`, write real "
            "tests that encode YOUR implementation's edge cases."
        )

    return "\n".join(sections)
