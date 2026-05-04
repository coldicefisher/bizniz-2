"""Persistent repair log read/write helpers.

Concurrency: each entry append is an atomic read-modify-write on
the JSON file. Workers within a single milestone are dispatched
sequentially per service for engineering, so concurrent writes to
the same service's log are rare. Failures degrade soft (the log
write fails silently rather than crashing the agent).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_LOG_FILENAME = ".bizniz_repair_log.json"


@dataclass
class RepairLogEntry:
    """One entry in the repair log.

    Each agent that performs a fix appends one entry per attempt.
    Future readers (later debug attempts, post-milestone analyzers)
    consume the full log to understand what's been tried.
    """
    agent: str                              # "quickdebugger" / "agenticdebugger" / "coder"
    trigger: str                            # what failed (test name + assertion / exception)
    diagnosis: str = ""                     # the agent's hypothesis
    fixes: List[Dict[str, str]] = field(default_factory=list)
    outcome: str = "unknown"                # "still_failing" / "passed" / "skipped"
    tier: Optional[str] = None              # model tier label (e.g. "gemini-flash-top")
    attempt: Optional[int] = None           # nth attempt at this tier
    timestamp: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


def log_path(workspace_root: Path) -> Path:
    return Path(workspace_root) / _LOG_FILENAME


def read_log(workspace_root: Path) -> List[RepairLogEntry]:
    """Return every entry in the log. Empty list when the file
    doesn't exist or can't be parsed."""
    path = log_path(workspace_root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    out: List[RepairLogEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            out.append(RepairLogEntry(**{
                k: item.get(k) for k in (
                    "agent", "trigger", "diagnosis", "fixes",
                    "outcome", "tier", "attempt", "timestamp",
                ) if k in item
            }))
        except TypeError:
            continue
    return out


def append_entry(workspace_root: Path, entry: RepairLogEntry) -> None:
    """Atomically append an entry. Soft-fails on I/O errors —
    the agent's repair flow shouldn't depend on the log existing."""
    try:
        existing = read_log(workspace_root)
        existing.append(entry)
        path = log_path(workspace_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([asdict(e) for e in existing], indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Soft fail — we want the agent to keep working even if
        # the log can't be written.
        pass


# ── prompt formatting ──────────────────────────────────────────────


def format_for_prompt(workspace_root: Path, max_chars: int = 6000) -> str:
    """Render the repair log as a prompt section.

    Returns empty string when the log is empty. The output tells
    the next debugger: "here's everything that's been tried — don't
    repeat these, you'll waste turns."
    """
    entries = read_log(workspace_root)
    if not entries:
        return ""

    lines = [
        "PRIOR REPAIR ATTEMPTS (DO NOT REPEAT — these have already "
        "been tried and either failed or were superseded; pick a "
        "different angle):",
        "",
    ]
    used = sum(len(line) + 1 for line in lines)

    for i, e in enumerate(entries):
        chunk = _format_one(i + 1, e)
        if used + len(chunk) > max_chars:
            lines.append(
                f"  ... ({len(entries) - i} more entries truncated for length)"
            )
            break
        lines.append(chunk)
        used += len(chunk) + 1

    return "\n".join(lines).rstrip() + "\n"


def _format_one(idx: int, entry: RepairLogEntry) -> str:
    tier_part = f" [{entry.tier}]" if entry.tier else ""
    attempt_part = f" attempt={entry.attempt}" if entry.attempt is not None else ""
    diag = (entry.diagnosis or "(no diagnosis recorded)")[:300]
    out_lines = [
        f"  #{idx} {entry.agent}{tier_part}{attempt_part} "
        f"→ outcome={entry.outcome}",
        f"     trigger: {(entry.trigger or '')[:200]}",
        f"     diagnosis: {diag}",
    ]
    if entry.fixes:
        for fix in entry.fixes[:5]:
            f_path = fix.get("file", "?")
            f_summary = (fix.get("summary") or "")[:120]
            out_lines.append(f"     fix: {f_path} — {f_summary}")
    return "\n".join(out_lines)
