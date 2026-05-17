"""LLM-driven hypothesis-with-confidence classifier — Phase D.

For each ``AntiPatternFinding`` from the deterministic scanner
(Phase C), this module asks an LLM: "Why does this code exist?
What's the author's intent, and what's the right action?" The LLM
returns a hypothesis + a confidence score (0-1) + a recommended
action (``rewrite``, ``surface``, or ``ignore``).

The Refactorer agent (Phase G) uses the confidence score to decide
whether to auto-apply the rewrite or surface to a human:

- **confidence ≥ 0.7 + recommended_action="rewrite"** → auto-apply
- **confidence 0.4-0.7** → surface with the hypothesis attached
- **confidence < 0.4 or "ignore"** → leave alone

The LLM invocation is injectable for tests. Production uses Claude
CLI subprocess; tests pass a fake that returns canned JSON.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Literal, Optional

from pydantic import BaseModel, Field

from bizniz.refactorer.anti_patterns import AntiPatternFinding


# ── Output schema ────────────────────────────────────────────────


Action = Literal["rewrite", "surface", "ignore"]


class WhyVerdict(BaseModel):
    """LLM's classification of one finding."""
    finding: AntiPatternFinding
    hypothesis: str = Field(
        description="One-sentence guess at the author's intent.",
    )
    confidence: float = Field(
        default=0.0, ge=0.0, le=1.0,
        description=(
            "0-1 confidence in the hypothesis. Drives auto-apply "
            "decision: ≥0.7 with action=rewrite → apply; otherwise "
            "surface or ignore."
        ),
    )
    recommended_action: Action = "surface"
    rationale: str = Field(
        default="",
        description="Short justification for the recommended action.",
    )


class WhyReport(BaseModel):
    """All verdicts across the scan."""
    verdicts: List[WhyVerdict] = Field(default_factory=list)

    def auto_fix_candidates(
        self, min_confidence: float = 0.7,
    ) -> List[WhyVerdict]:
        return [
            v for v in self.verdicts
            if v.recommended_action == "rewrite"
            and v.confidence >= min_confidence
        ]

    def surface_candidates(self) -> List[WhyVerdict]:
        return [
            v for v in self.verdicts
            if v.recommended_action == "surface"
            or (v.recommended_action == "rewrite" and v.confidence < 0.7)
        ]


# ── Prompts ──────────────────────────────────────────────────────


_WHY_SYSTEM_PROMPT = """You are a senior code reviewer evaluating ONE anti-pattern finding from a static analysis pass. Your job is to:

1. Look at the surrounding source-code context (lines around the finding)
2. Hypothesize WHY this code is there (intent of the author)
3. Score your confidence in that hypothesis (0.0-1.0)
4. Recommend an action:
   - **rewrite** — confident this is a bug or anti-pattern AND there's a known better pattern. The Refactorer will apply the rewrite automatically if your confidence is ≥0.7.
   - **surface** — finding looks suspect but you're not certain enough to auto-apply, or the right fix isn't obvious. A human should review.
   - **ignore** — finding is a false positive (e.g. credential pattern matched a test placeholder, or the suspect code is actually correct given the surrounding context).

Output STRICT JSON, no commentary outside:

```json
{
  "hypothesis": "one-sentence guess at why this code is here",
  "confidence": 0.0-1.0,
  "recommended_action": "rewrite|surface|ignore",
  "rationale": "short justification for the action"
}
```

**Bias toward "surface" when uncertain.** The cost of a wrong auto-rewrite (broken build, lost work) far exceeds the cost of a human reviewing one extra finding.
"""


_WHY_USER_TEMPLATE = """Anti-pattern finding to classify:

**Pattern:** {pattern}
**Severity:** {severity}
**File:** {path}:{line}
**Snippet:** ``{snippet}``
**Description:** {description}
**Suggested fix (from scanner):** {suggested_fix}

**Surrounding context (lines {context_start}-{context_end}):**

```
{context}
```

Classify per the system prompt.
"""


def _build_user_prompt(
    finding: AntiPatternFinding,
    context: str,
    context_start: int,
    context_end: int,
) -> str:
    return _WHY_USER_TEMPLATE.format(
        pattern=finding.pattern,
        severity=finding.severity,
        path=finding.path,
        line=finding.line,
        snippet=finding.snippet,
        description=finding.description,
        suggested_fix=finding.suggested_fix or "(none)",
        context_start=context_start,
        context_end=context_end,
        context=context,
    )


def _gather_context(
    finding: AntiPatternFinding,
    file_text: str,
    radius: int = 8,
) -> "tuple[str, int, int]":
    """Return (context, start_line, end_line) — a window of ``radius``
    lines on either side of the finding's line."""
    lines = file_text.splitlines()
    start = max(1, finding.line - radius)
    end = min(len(lines), finding.line + radius)
    window = lines[start - 1:end]
    return "\n".join(window), start, end


# ── Result parsing ───────────────────────────────────────────────


def _parse_verdict(
    raw: dict, finding: AntiPatternFinding,
) -> WhyVerdict:
    """Coerce LLM JSON into a WhyVerdict with sane defaults."""
    confidence = raw.get("confidence", 0.0)
    try:
        confidence_f = float(confidence)
    except (TypeError, ValueError):
        confidence_f = 0.0
    confidence_f = max(0.0, min(1.0, confidence_f))

    action = raw.get("recommended_action", "surface")
    if action not in ("rewrite", "surface", "ignore"):
        action = "surface"

    return WhyVerdict(
        finding=finding,
        hypothesis=str(raw.get("hypothesis") or ""),
        confidence=confidence_f,
        recommended_action=action,
        rationale=str(raw.get("rationale") or ""),
    )


# ── Classifier ───────────────────────────────────────────────────


class WhyClassifier:
    """LLM-driven classifier. ``llm_invoker`` is injectable for tests."""

    def __init__(
        self,
        command: str = "claude",
        on_status: Optional[Callable[[str], None]] = None,
        llm_invoker: Optional[Callable[[AntiPatternFinding, str], Optional[dict]]] = None,
        file_reader: Optional[Callable[[str], str]] = None,
        context_radius: int = 8,
        additional_args: Optional[List[str]] = None,
    ) -> None:
        self._command = command
        self._on_status = on_status
        self._llm_invoker = llm_invoker or self._default_llm_invoker
        self._file_reader = file_reader
        self._context_radius = context_radius
        self._additional_args = list(additional_args or [])

    def _log(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def _read_file(self, path: str) -> str:
        if self._file_reader is not None:
            return self._file_reader(path)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    def classify(self, finding: AntiPatternFinding) -> WhyVerdict:
        """Classify one finding. Returns a verdict even on failure —
        never raises. LLM failure defaults to ``surface`` with low
        confidence so a human reviews."""
        file_text = self._read_file(finding.path)
        if not file_text:
            self._log(
                f"WhyClassifier: {finding.pattern} at {finding.path} "
                f"— file unreadable, defaulting to surface"
            )
            return WhyVerdict(
                finding=finding,
                hypothesis="(file unreadable)",
                confidence=0.0,
                recommended_action="surface",
                rationale="couldn't read file to gather context",
            )
        context, start, end = _gather_context(
            finding, file_text, self._context_radius,
        )
        user_prompt = _build_user_prompt(finding, context, start, end)
        self._log(
            f"WhyClassifier: classifying {finding.pattern} at "
            f"{finding.path}:{finding.line}..."
        )
        parsed = self._llm_invoker(finding, user_prompt)
        if parsed is None:
            self._log(
                f"WhyClassifier: LLM returned no parseable JSON for "
                f"{finding.pattern} — defaulting to surface"
            )
            return WhyVerdict(
                finding=finding,
                hypothesis="(LLM call failed)",
                confidence=0.0,
                recommended_action="surface",
                rationale="LLM did not return parseable JSON",
            )
        verdict = _parse_verdict(parsed, finding)
        self._log(
            f"WhyClassifier: {finding.pattern} → "
            f"{verdict.recommended_action} (conf={verdict.confidence:.2f})"
        )
        return verdict

    def classify_all(
        self, findings: List[AntiPatternFinding],
    ) -> WhyReport:
        """Classify every finding sequentially. Returns a ``WhyReport``."""
        verdicts: List[WhyVerdict] = []
        for finding in findings:
            verdicts.append(self.classify(finding))
        return WhyReport(verdicts=verdicts)

    # ── Default Claude CLI invoker ───────────────────────────────

    def _default_llm_invoker(
        self,
        finding: AntiPatternFinding,
        user_prompt: str,
    ) -> Optional[dict]:
        if shutil.which(self._command) is None:
            self._log(
                f"WhyClassifier: {self._command!r} not on PATH — "
                f"can't classify"
            )
            return None
        cmd = [
            self._command, "--print",
            "--output-format=json",
            "--append-system-prompt", _WHY_SYSTEM_PROMPT,
            "--permission-mode", "bypassPermissions",
        ] + self._additional_args
        try:
            proc = subprocess.run(
                cmd, input=user_prompt, capture_output=True,
                text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            return None
        if proc.returncode != 0:
            return None
        try:
            envelope = json.loads(proc.stdout)
        except Exception:
            return None
        inner = envelope.get("result")
        if not isinstance(inner, str):
            return None
        start = inner.find("{")
        end = inner.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(inner[start:end + 1])
        except Exception:
            return None
