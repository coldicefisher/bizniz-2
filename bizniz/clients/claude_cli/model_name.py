"""Parse ``claude-cli[:<actual-model>]`` model names into CLI args.

The bizniz config layer routes models by name prefix. ``claude-cli``
is a single prefix that routes to the CLI subprocess client, but
historically that meant "whatever the CLI default is" — we couldn't
pin a specific Anthropic model per role.

This module extends the convention with an optional colon suffix:

    ``claude-cli``                         → CLI default model
    ``claude-cli:claude-haiku-4-5``        → ``--model claude-haiku-4-5``
    ``claude-cli:claude-opus-4-7``         → ``--model claude-opus-4-7``

Both ``ClaudeCliClient`` (single-call agents) and ``ClaudeCliCoder``
(tool-loop coder) read this so per-role config can pin Haiku for
execution roles and Opus for orchestration roles via the same
``coder_models`` / ``architect_model`` / etc. config fields.
"""
from __future__ import annotations

from typing import List, Tuple


_CLI_PREFIX = "claude-cli"


def parse_claude_cli_model(name: str) -> Tuple[str, List[str]]:
    """Split a model name into (telemetry label, extra CLI args).

    Returns:
        (label, args) where ``label`` is the unchanged name (used for
        cost-tracker stamping and logs) and ``args`` is a list to
        append to the ``claude --print`` invocation. Empty list when
        no suffix is present (defaults to the CLI's active model).

    Examples:
        >>> parse_claude_cli_model("claude-cli")
        ('claude-cli', [])
        >>> parse_claude_cli_model("claude-cli:claude-haiku-4-5")
        ('claude-cli:claude-haiku-4-5', ['--model', 'claude-haiku-4-5'])
    """
    if not name.startswith(_CLI_PREFIX):
        return (name, [])
    suffix = name[len(_CLI_PREFIX):]
    if not suffix:
        return (name, [])
    if not suffix.startswith(":"):
        # Forward-compat: a name like "claude-cli-something" is treated
        # as opaque label, no --model injection. Callers that want the
        # subprocess to pick a specific model must use the colon form.
        return (name, [])
    model = suffix[1:].strip()
    if not model:
        return (name, [])
    return (name, ["--model", model])
