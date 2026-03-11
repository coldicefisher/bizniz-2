"""Types for pre-flight validation results."""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ImportIssue:
    """A single import resolution problem."""
    filepath: str
    import_name: str
    issue: str  # "missing_module", "missing_init", "missing_dependency"
    detail: str

    def __str__(self):
        return f"{self.filepath}: {self.import_name} — {self.detail}"


@dataclass
class AutoStub:
    """A file that was auto-generated to fix an import issue."""
    filepath: str
    content: str
    reason: str


@dataclass
class ImportRewrite:
    """A relative import that was rewritten to absolute."""
    filepath: str
    old_import: str
    new_import: str


@dataclass
class PreflightResult:
    """Result of a pre-flight validation pass."""
    language: str
    issues: List[ImportIssue] = field(default_factory=list)
    stubs_created: List[AutoStub] = field(default_factory=list)
    import_rewrites: List["ImportRewrite"] = field(default_factory=list)
    files_checked: int = 0

    @property
    def passed(self) -> bool:
        return len(self.issues) == 0

    @property
    def issues_fixed(self) -> int:
        return len(self.stubs_created)

    def summary(self) -> str:
        lines = [f"Preflight ({self.language}): {self.files_checked} files checked"]
        if self.import_rewrites:
            lines.append(f"  Rewrote {len(self.import_rewrites)} relative import(s) to absolute:")
            for rw in self.import_rewrites:
                lines.append(f"    ~ {rw.filepath}: {rw.old_import} → {rw.new_import}")
        if self.stubs_created:
            lines.append(f"  Auto-fixed {len(self.stubs_created)} issue(s):")
            for stub in self.stubs_created:
                lines.append(f"    + {stub.filepath} ({stub.reason})")
        if self.issues:
            lines.append(f"  {len(self.issues)} unresolved issue(s):")
            for issue in self.issues:
                lines.append(f"    ! {issue}")
        elif not self.stubs_created and not self.import_rewrites:
            lines.append("  All imports resolved.")
        return "\n".join(lines)
