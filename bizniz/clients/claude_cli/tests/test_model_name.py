"""Tests for the ``claude-cli[:model]`` model-name parser."""
from __future__ import annotations

import pytest

from bizniz.clients.claude_cli.model_name import parse_claude_cli_model


class TestParseClaudeCliModel:
    def test_bare_name_returns_no_model_args(self):
        label, args = parse_claude_cli_model("claude-cli")
        assert label == "claude-cli"
        assert args == []

    def test_with_haiku_suffix_returns_model_flag(self):
        label, args = parse_claude_cli_model("claude-cli:claude-haiku-4-5")
        assert label == "claude-cli:claude-haiku-4-5"
        assert args == ["--model", "claude-haiku-4-5"]

    def test_with_opus_suffix_returns_model_flag(self):
        label, args = parse_claude_cli_model("claude-cli:claude-opus-4-7")
        assert label == "claude-cli:claude-opus-4-7"
        assert args == ["--model", "claude-opus-4-7"]

    def test_strips_whitespace_in_suffix(self):
        _, args = parse_claude_cli_model("claude-cli:  claude-haiku-4-5  ")
        assert args == ["--model", "claude-haiku-4-5"]

    def test_empty_suffix_is_treated_as_bare(self):
        label, args = parse_claude_cli_model("claude-cli:")
        assert label == "claude-cli:"
        assert args == []

    def test_non_cli_name_returns_no_args(self):
        # Anthropic API client routing — claude-opus-4-7 hits ClaudeClient,
        # not ClaudeCliClient. Parser must leave it alone.
        label, args = parse_claude_cli_model("claude-opus-4-7")
        assert label == "claude-opus-4-7"
        assert args == []

    def test_non_colon_suffix_is_opaque_label(self):
        # Names like ``claude-cli-foo`` were never wired to mean anything
        # — parser preserves them as opaque labels for forward-compat.
        label, args = parse_claude_cli_model("claude-cli-experimental")
        assert label == "claude-cli-experimental"
        assert args == []

    @pytest.mark.parametrize("model_id", [
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-7",
        "claude-sonnet-4-6",
    ])
    def test_known_anthropic_model_ids_pass_through(self, model_id):
        _, args = parse_claude_cli_model(f"claude-cli:{model_id}")
        assert args == ["--model", model_id]
