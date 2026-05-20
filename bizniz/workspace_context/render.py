"""Render WorkspaceContext as a markdown section for agent prompts."""
from __future__ import annotations

from bizniz.workspace_context.types import WorkspaceContext


# Reasonable cap: don't dump every file's full content into the prompt
# if files are huge. Per-file limit; total prompt bounded.
_PER_FILE_CAP = 6000
_LIVE_FILE_TRUNCATE_NOTE = (
    "\n\n...[truncated middle; head + tail shown]...\n\n"
)


def _truncate(text: str, max_chars: int = _PER_FILE_CAP) -> str:
    if len(text) <= max_chars:
        return text
    head = max_chars // 2 - 50
    tail = max_chars - head - len(_LIVE_FILE_TRUNCATE_NOTE)
    return text[:head] + _LIVE_FILE_TRUNCATE_NOTE + text[-tail:]


def render_context_section(ctx: WorkspaceContext) -> str:
    """Render the whole context as a single markdown section the
    agent reads BEFORE writing code."""
    parts: list = []
    parts.append(
        "## WHAT'S ACTUALLY TRUE RIGHT NOW (preventive context)\n\n"
        "This section is built deterministically from the live "
        "workspace state. It's the source of truth for what's "
        "installed and what's on disk. Read it before writing code."
    )

    # ── Installed Python packages ────────────────────────────────
    if ctx.declared_python_packages:
        parts.append("\n### Installed Python packages")
        parts.append(
            "Your `import` lines MUST use the names in the 'Import as' "
            "column. The distribution name (left) is what's in "
            "requirements.txt; the import name (right) is what Python "
            "actually sees.\n"
        )
        parts.append("| Package | Version | Import as |")
        parts.append("|---------|---------|-----------|")
        for p in ctx.declared_python_packages:
            ver = p.version or "-"
            imp = p.import_name or p.name
            parts.append(f"| {p.name} | {ver} | `{imp}` |")
        parts.append("")

    # ── Installed Node packages ──────────────────────────────────
    if ctx.declared_node_packages:
        parts.append("\n### Installed npm packages")
        parts.append("| Package | Version | Import as |")
        parts.append("|---------|---------|-----------|")
        for p in ctx.declared_node_packages:
            ver = p.version or "-"
            imp = p.import_name or p.name
            parts.append(f"| {p.name} | {ver} | `{imp}` |")
        parts.append("")

    # ── Adding new dependencies ──────────────────────────────────
    parts.append("\n### Adding new dependencies (if needed)")
    parts.append(
        "If you need a package that's NOT in the tables above:\n\n"
        "Option A — emit a `requested_deps` entry in your output "
        "(structured, preferred):\n"
        "```\n"
        '"requested_deps": [\n'
        '  {"name": "pyjwt", "version": "^2.10", "purpose": "JWT validation",\n'
        '   "language": "python"}\n'
        "]\n"
        "```\n\n"
        "Option B — emit an edit to `requirements.txt` (or "
        "`package.json`) adding the package, in your normal edits / "
        "filled_files output.\n\n"
        "After your call returns, the orchestrator will:\n"
        "  1. `pip install -r requirements.txt` in the container\n"
        "  2. Restart the service container\n"
        "  3. Wait for /health to come back green\n"
        "  4. Re-run the validator\n\n"
        "You do NOT need to: run pip install yourself, restart the "
        "container, run pytest to verify, or add defensive try/except "
        "around the new import. The orchestrator handles all of that.\n"
        "If install fails, you'll see a DEP_INSTALL_FAILED finding "
        "next iteration with the pip error to fix."
    )

    # ── Live workspace files ─────────────────────────────────────
    if ctx.target_files_content or ctx.test_files_content or ctx.missing_paths:
        parts.append("\n### Live workspace state (your target + test files)")
        if ctx.missing_paths:
            parts.append("**Paths that don't exist on disk yet (CREATE them — "
                         "use whole-file content or new_files):**")
            for p in sorted(ctx.missing_paths):
                parts.append(f"  - `{p}`")
            parts.append("")
        for path, content in sorted(ctx.target_files_content.items()):
            parts.append(f"\n**`{path}`** (existing — EDIT, don't rewrite from scratch)")
            parts.append("```")
            parts.append(_truncate(content))
            parts.append("```")
        for path, content in sorted(ctx.test_files_content.items()):
            parts.append(f"\n**`{path}`** (existing test — EDIT, don't rewrite)")
            parts.append("```")
            parts.append(_truncate(content))
            parts.append("```")

    return "\n".join(parts)
