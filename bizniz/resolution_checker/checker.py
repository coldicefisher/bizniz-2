"""``ResolutionChecker`` — v5 iter-2+ structured-output reviewer.

Takes a frozen CanonicalReport + current code files and emits a
ResolutionReport: for each known finding, the LLM judges
``resolved | still_present | regressed``. Constrained schema; the
agent CANNOT add new findings.

Per the v5 spec, runs per-source (QE flavor + CR flavor) in
parallel — same fan-out as v3.1's parallel review. Both flavors
share this Checker class; the prompt differs slightly.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, List, Optional

from bizniz.canonical_findings.types import (
    CanonicalFinding,
    CanonicalReport,
    FindingResolution,
    ResolutionReport,
    ResolutionStatus,
)
from bizniz.clients.base_ai_client import BaseAIClient
from bizniz.clients.chatgpt.messages import Message
from bizniz.clients.chatgpt.types.response_format import ResponseFormat
from bizniz.lib.llm_utils import call_with_retry


# Constrained output: only statuses for findings we already have.
# No new findings. JSON schema enforces this server-side; downstream
# code also drops any unknown ids when applying the resolution.
_RESOLUTION_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "resolution_check_output",
        "schema": {
            "type": "object",
            "properties": {
                "resolutions": {
                    "type": "array",
                    "description": (
                        "One entry per finding in the input report. "
                        "Status is constrained — do NOT invent new findings."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_id": {
                                "type": "string",
                                "description": "Echo the canonical finding id verbatim.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["resolved", "still_present", "regressed"],
                                "description": (
                                    "resolved = defect no longer in code; "
                                    "still_present = defect still there; "
                                    "regressed = was fixed but broke again."
                                ),
                            },
                            "evidence": {
                                "type": "string",
                                "description": "One-line reason for the verdict.",
                            },
                        },
                        "required": ["finding_id", "status", "evidence"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["resolutions"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


_SYSTEM_PROMPT = """You are a RESOLUTION CHECKER for the v5 review
loop. You are NOT a fresh reviewer.

Your input is:
  - A frozen list of canonical findings (with stable ids) from iter 1
    of review/repair.
  - The current source + test files on disk (after the last repair
    iter).

Your job is to examine the CURRENT code and judge each finding:
  - `resolved` — the defect is no longer present
  - `still_present` — the defect is still in the code
  - `regressed` — the code now has the defect again after being fixed
    (rare; only relevant when iter > 2)

HARD CONSTRAINTS:

1. **No new findings.** You MUST emit one resolution per existing
   canonical finding and NOTHING ELSE. Do not invent new defects
   even if you see them. (Future iter 1 reviews will catch new
   defects in their fresh pass.)

2. **Echo finding_id exactly.** Your `finding_id` field is the
   canonical id from the input — copy it verbatim. Don't paraphrase
   or shorten.

3. **Evidence is one line.** A brief reason — "function exists at
   app/me.py:14" or "still throws NotImplementedError at line 22".
   Not a paragraph.

4. **Be conservative on `resolved`.** If you're not sure the defect
   is gone, prefer `still_present`. False-positive `resolved`
   verdicts make the loop falsely declare success.

5. **Stay structured.** Emit ONE JSON object matching the schema.
   No prose around it.
"""


class ResolutionCheckerError(Exception):
    """LLM call failed or returned schema-invalid output."""


class ResolutionChecker:
    """Per-source resolution checker. Same class for QE + CR flavors
    — the source distinction lives in the canonical findings, not the
    checker itself. (Future: split if the prompts need to diverge.)
    """

    def __init__(
        self,
        client: BaseAIClient,
        on_status: Optional[Callable[[str], None]] = None,
        max_retries: int = 3,
    ):
        self._client = client
        self._on_status = on_status
        self._max_retries = max_retries

    def check(
        self,
        *,
        canonical: CanonicalReport,
        iter_idx: int,
        current_files: Dict[str, str],
        source_filter: Optional[str] = None,
    ) -> ResolutionReport:
        """Check resolution for findings in ``canonical``.

        ``source_filter``: when set (e.g., "quality_engineer"), only
        check findings from that source. None = all findings.
        ``current_files``: dict of relative_path → file_content.
        """
        findings_to_check = [
            f for f in canonical.findings
            if (source_filter is None or f.source == source_filter)
            and f.status not in ("resolved", "wont_fix")
        ]
        if not findings_to_check:
            self._log(
                f"ResolutionChecker[{source_filter or 'all'}]: "
                f"nothing to check (all resolved/wontfix)"
            )
            return ResolutionReport(
                milestone_name=canonical.milestone_name,
                iter_idx=iter_idx,
            )

        self._log(
            f"ResolutionChecker[{source_filter or 'all'}]: checking "
            f"{len(findings_to_check)} finding(s) at iter {iter_idx}"
        )

        prompt = self._build_prompt(
            findings_to_check=findings_to_check,
            current_files=current_files,
        )

        raw = call_with_retry(
            client=self._client,
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=prompt),
            ],
            response_format=ResponseFormat.JSON_SCHEMA,
            schema=_RESOLUTION_SCHEMA,
            max_attempts=self._max_retries,
            on_status=self._on_status,
            label=f"ResolutionChecker[{source_filter or 'all'}]:iter{iter_idx}",
        )

        items = raw.get("resolutions") or []
        resolutions: List[FindingResolution] = []
        for it in items:
            try:
                resolutions.append(FindingResolution(**it))
            except Exception as e:
                self._log(
                    f"ResolutionChecker[{source_filter or 'all'}]: "
                    f"skipping malformed resolution {it}: "
                    f"{type(e).__name__}: {e}"
                )

        # Discard resolutions for findings outside our scope (defense
        # in depth — the schema doesn't actually prevent the agent
        # from echoing a wrong id).
        valid_ids = {f.id for f in findings_to_check}
        filtered = [r for r in resolutions if r.finding_id in valid_ids]
        dropped = len(resolutions) - len(filtered)
        if dropped:
            self._log(
                f"ResolutionChecker[{source_filter or 'all'}]: "
                f"dropped {dropped} resolution(s) with unknown ids"
            )

        return ResolutionReport(
            milestone_name=canonical.milestone_name,
            iter_idx=iter_idx,
            resolutions=filtered,
        )

    def _build_prompt(
        self,
        *,
        findings_to_check: List[CanonicalFinding],
        current_files: Dict[str, str],
    ) -> str:
        sections: List[str] = []
        sections.append("## Canonical findings to check\n")
        sections.append(
            "For each finding below, examine the CURRENT code and emit "
            "a resolution status. Echo the `id` exactly."
        )
        sections.append("")
        for f in findings_to_check:
            sections.append(f"### `{f.id}`")
            sections.append(f"- source: {f.source}")
            sections.append(f"- priority: {f.priority}")
            if f.capability_id:
                sections.append(f"- capability: `{f.capability_id}`")
            if f.file_hint:
                sections.append(f"- file_hint: `{f.file_hint}`")
            sections.append(f"- summary: {f.summary}")
            if f.detail:
                # Cap detail at 500 chars — the canonical detail is
                # often verbose.
                d = f.detail[:500]
                sections.append(f"- detail: {d}")
            sections.append(f"- current_status: {f.status}")
            sections.append("")

        sections.append("## Current code on disk\n")
        if not current_files:
            sections.append("(no files supplied — base verdict on file_hints + structure)")
        else:
            for path, content in current_files.items():
                # Cap each file at 4000 chars. The agent should focus
                # on the file_hint paths anyway.
                if len(content) > 4000:
                    head = content[:2000]
                    tail = content[-2000:]
                    rendered = head + f"\n\n...[truncated {len(content) - 4000} chars]...\n\n" + tail
                else:
                    rendered = content
                sections.append(f"### `{path}`")
                sections.append("```")
                sections.append(rendered)
                sections.append("```")
                sections.append("")

        sections.append(
            "## Your job\n\n"
            "Emit `{resolutions: [{finding_id, status, evidence}, ...]}`. "
            "One entry per finding above. Be conservative on `resolved` "
            "— if unsure, return `still_present`."
        )
        return "\n".join(sections)

    def _log(self, msg: str) -> None:
        if self._on_status:
            try:
                self._on_status(msg)
            except Exception:
                pass


# ── Parallel fan-out helper ───────────────────────────────────────


def check_both_sources_parallel(
    *,
    qe_checker: ResolutionChecker,
    cr_checker: ResolutionChecker,
    canonical: CanonicalReport,
    iter_idx: int,
    current_files: Dict[str, str],
    on_status: Optional[Callable[[str], None]] = None,
) -> ResolutionReport:
    """Run QE-flavor + CR-flavor checks concurrently (matches v3.1's
    parallel review). Returns a merged ResolutionReport."""
    def _log(msg: str) -> None:
        if on_status:
            try:
                on_status(msg)
            except Exception:
                pass

    _log(f"ResolutionChecker: starting iter {iter_idx} (QE + CR in parallel)")

    with ThreadPoolExecutor(max_workers=2) as ex:
        qe_fut = ex.submit(
            qe_checker.check,
            canonical=canonical,
            iter_idx=iter_idx,
            current_files=current_files,
            source_filter="quality_engineer",
        )
        cr_fut = ex.submit(
            cr_checker.check,
            canonical=canonical,
            iter_idx=iter_idx,
            current_files=current_files,
            source_filter="code_reviewer",
        )
        qe_report = qe_fut.result()
        cr_report = cr_fut.result()

    merged = ResolutionReport(
        milestone_name=canonical.milestone_name,
        iter_idx=iter_idx,
        resolutions=list(qe_report.resolutions) + list(cr_report.resolutions),
    )
    _log(
        f"ResolutionChecker: iter {iter_idx} merged "
        f"{len(merged.resolutions)} resolution(s) "
        f"(QE={len(qe_report.resolutions)}, "
        f"CR={len(cr_report.resolutions)})"
    )
    return merged
