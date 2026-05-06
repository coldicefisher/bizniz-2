"""AuthAgent result + audit types."""
from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel


AuthAgentMode = Literal["configure", "audit"]


class AuditCheck(BaseModel):
    """One item in the audit battery's report."""
    name: str
    passed: bool
    detail: str = ""


class AuditReport(BaseModel):
    """Output of the deterministic audit battery. Always run after the
    agent's tool loop finishes (both modes). Six categories of check:
    token validation matrix, role enforcement, JWT signing, test
    credential exposure, idempotency, contract drift."""
    checks: List[AuditCheck] = []

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed(self) -> List[AuditCheck]:
        return [c for c in self.checks if not c.passed]


class AuthAgentResult(BaseModel):
    """Terminal payload from an AuthAgent run.

    The agent emits ``submit_contract`` with the markdown body it wants
    written to AUTH_CONTRACT.md. After the loop finishes, a
    deterministic audit battery runs and its findings get attached.
    """
    mode: AuthAgentMode
    contract_markdown: str
    contract_path: Optional[str] = None  # set by the agent after writing
    audit: AuditReport = AuditReport()
    summary: str = ""  # one-paragraph narrative of what the agent did/found
    applied_changes: List[str] = []  # things the agent mutated (configure mode only)


class AuthAgentError(Exception):
    pass
