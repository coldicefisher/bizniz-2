"""Sticky repair log.

Every error and every attempted fix in the pipeline accumulates here,
keyed by service. Readable by every debugger agent (QuickDebugger,
AgenticDebugger across all escalation tiers) so no later attempt
repeats a fix the previous one already tried.

Persisted to ``<workspace>/.bizniz_repair_log.json`` so the log
survives container teardowns, sidecar dispatches, and process
restarts within a milestone.

Schema:

    [
      {
        "agent": "agenticdebugger",       # which agent recorded this
        "tier": "gemini-flash-top",       # model tier (None for non-tiered)
        "attempt": 1,                     # attempt number at this tier
        "timestamp": "2026-05-04T...",
        "trigger": "test_landlord_login_and_profile failed: ...",
        "diagnosis": "Maybe the role table isn't seeded",
        "fixes": [
          {"file": "app/api/routes/auth.py", "summary": "added role lookup"}
        ],
        "outcome": "still_failing | passed | skipped"
      },
      ...
    ]

The agent's prompt context for any new attempt includes the full log
formatted as ``PRIOR REPAIR ATTEMPTS — DO NOT REPEAT THESE``.
"""
from bizniz.repair_log.log import (
    RepairLogEntry,
    append_entry,
    read_log,
    format_for_prompt,
    log_path,
)

__all__ = [
    "RepairLogEntry",
    "append_entry",
    "read_log",
    "format_for_prompt",
    "log_path",
]
