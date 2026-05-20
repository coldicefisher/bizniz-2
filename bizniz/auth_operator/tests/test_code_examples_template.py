"""Tests for the CTX-3 skeleton-template path in generate_code_examples."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.auth_operator.code_examples import (
    _build_substitutions, _try_skeleton_template, generate_code_examples,
)


def _manifest():
    """Minimal AuthManifest stub. The function only reads specific
    fields; we don't need a full Pydantic instance."""
    m = MagicMock()
    m.primary_app_id = "fake-app-id-1234"
    m.tenant_id = "fake-tenant-id"
    m.issuer = "http://auth:9011"
    m.fa_url = "http://localhost:9011"
    m.signing_key.algorithm = "RS256"
    u = MagicMock(email="user@example.com", password="Password123!", roles=["user"])
    m.users = [u]
    return m


# ── Substitution helpers ────────────────────────────────────────────


class TestBuildSubstitutions:
    def test_all_expected_placeholders_present(self):
        subs = _build_substitutions(_manifest())
        for key in (
            "primary_app_id", "tenant_id", "issuer",
            "fa_url_host", "fa_url_container",
            "signing_algorithm",
            "test_user_email", "test_user_password",
        ):
            assert key in subs

    def test_test_user_picks_first(self):
        subs = _build_substitutions(_manifest())
        assert subs["test_user_email"] == "user@example.com"
        assert subs["test_user_password"] == "Password123!"

    def test_container_url_constant(self):
        # http://auth:9011 is the in-network URL; constant across projects.
        subs = _build_substitutions(_manifest())
        assert subs["fa_url_container"] == "http://auth:9011"


# ── Template loading + substitution ────────────────────────────────


class TestTrySkeletonTemplate:
    def test_returns_empty_when_no_skeleton_paths(self):
        result = _try_skeleton_template(
            manifest=_manifest(), skeleton_paths=[],
        )
        assert result == ""

    def test_returns_empty_when_template_file_missing(self, tmp_path):
        sk = tmp_path / "fake-skeleton"
        sk.mkdir()
        # No AUTH_CONTRACT_EXAMPLES.md.template inside.
        result = _try_skeleton_template(
            manifest=_manifest(), skeleton_paths=[sk],
        )
        assert result == ""

    def test_loads_and_substitutes_placeholders(self, tmp_path):
        sk = tmp_path / "skeleton"
        sk.mkdir()
        (sk / "AUTH_CONTRACT_EXAMPLES.md.template").write_text(
            "## Examples\n\nAPP={{primary_app_id}}\n"
            "URL={{fa_url_host}}\nUSER={{test_user_email}}\n"
        )
        result = _try_skeleton_template(
            manifest=_manifest(), skeleton_paths=[sk],
        )
        assert "APP=fake-app-id-1234" in result
        assert "URL=http://localhost:9011" in result
        assert "USER=user@example.com" in result
        # Unsubstituted placeholders gone.
        assert "{{primary_app_id}}" not in result

    def test_first_matching_skeleton_wins(self, tmp_path):
        sk1 = tmp_path / "skel1"
        sk1.mkdir()
        sk2 = tmp_path / "skel2"
        sk2.mkdir()
        (sk1 / "AUTH_CONTRACT_EXAMPLES.md.template").write_text(
            "from-skel1: {{primary_app_id}}"
        )
        (sk2 / "AUTH_CONTRACT_EXAMPLES.md.template").write_text(
            "from-skel2: {{primary_app_id}}"
        )
        result = _try_skeleton_template(
            manifest=_manifest(), skeleton_paths=[sk1, sk2],
        )
        assert "from-skel1" in result
        assert "from-skel2" not in result


# ── End-to-end: generate_code_examples prefers template ───────────


class TestGenerateCodeExamplesPrefersTemplate:
    def test_template_path_skips_llm_call(self, tmp_path):
        from unittest.mock import patch
        sk = tmp_path / "skeleton"
        sk.mkdir()
        (sk / "AUTH_CONTRACT_EXAMPLES.md.template").write_text(
            "## Examples\nAPP={{primary_app_id}}"
        )
        # Mock the LLM call — should NOT be reached.
        with patch(
            "bizniz.auth_operator.code_examples.call_with_retry"
        ) as mock_call:
            result = generate_code_examples(
                client=MagicMock(),
                manifest=_manifest(),
                languages=["python"],
                skeleton_paths=[sk],
            )
        assert "APP=fake-app-id-1234" in result
        mock_call.assert_not_called()

    def test_no_template_falls_through_to_llm(self, tmp_path):
        from unittest.mock import patch
        with patch(
            "bizniz.auth_operator.code_examples.call_with_retry",
            return_value={"markdown": "## Code samples\n(generated)\n"},
        ) as mock_call:
            result = generate_code_examples(
                client=MagicMock(),
                manifest=_manifest(),
                languages=["python"],
                skeleton_paths=[],  # no templates available
            )
        mock_call.assert_called_once()
        assert "Code samples" in result
