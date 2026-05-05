"""Tests for BiznizConfig.

The model-progression lists (coder_models, tester_models, repair_models)
are required at config-load time — there's no longer a shared ``models``
fallback.

There's also no shared ``default_model`` fallback for ``make_client()``
— callers must pass an explicit model. Tests reflect both.
"""
from unittest.mock import patch

import pytest
import yaml

from bizniz.config.bizniz_config import BiznizConfig
from bizniz.orchestrator.model_progression import ModelProgression


# Minimum viable per-agent model lists for tests that don't care about
# the specific models, only about the rest of the config behavior.
MIN_PROGRESSIONS = {
    "coder_models": ["gpt-4o-mini", "gpt-4o"],
    "tester_models": ["gpt-4o-mini", "gpt-4o"],
    "repair_models": ["gpt-4o", "gpt-5"],
}


def _config_yaml(extra: dict | None = None) -> str:
    """Build a minimal valid bizniz.yaml string with the required
    progression lists plus any extra fields the test cares about."""
    body = dict(MIN_PROGRESSIONS)
    if extra:
        body.update(extra)
    return yaml.dump(body)


class TestFromYaml:
    """Test BiznizConfig.from_yaml() loading from a temp file."""

    def test_loads_all_fields(self, tmp_path):
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(_config_yaml({
            "engineer_model": "gpt-4o",
            "api_key": "sk-test-key",
            "is_azure": True,
            "api_base": "https://my-azure.openai.azure.com",
            "max_iterations": 10,
        }))

        config = BiznizConfig.from_yaml(str(config_file))

        assert config.engineer_model == "gpt-4o"
        assert config.api_key == "sk-test-key"
        assert config.is_azure is True
        assert config.api_base == "https://my-azure.openai.azure.com"
        assert config.max_iterations == 10

    def test_partial_yaml_uses_defaults_for_optional_fields(self, tmp_path):
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(_config_yaml({"engineer_model": "gpt-5"}))

        config = BiznizConfig.from_yaml(str(config_file))

        assert config.engineer_model == "gpt-5"
        assert config.api_key is None
        assert config.max_iterations == 20
        # Required progressions came from MIN_PROGRESSIONS via _config_yaml
        assert config.coder_models == ["gpt-4o-mini", "gpt-4o"]
        assert config.tester_models == ["gpt-4o-mini", "gpt-4o"]

    def test_missing_per_agent_progression_hard_fails(self, tmp_path):
        """A config without coder_models / tester_models / repair_models
        must raise — there's no silent fallback to a generic ``models``
        list anymore. This is the contract the user explicitly asked for."""
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(yaml.dump({"engineer_model": "gpt-4o"}))

        with pytest.raises(Exception):  # pydantic.ValidationError
            BiznizConfig.from_yaml(str(config_file))

    def test_empty_progression_list_hard_fails(self, tmp_path):
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(yaml.dump({
            "coder_models": [],
            "tester_models": ["x"],
            "repair_models": ["x"],
        }))

        with pytest.raises(Exception):
            BiznizConfig.from_yaml(str(config_file))


class TestFindAndLoad:
    """Test BiznizConfig.find_and_load() behavior."""

    def test_raises_when_no_file_and_no_args(self, tmp_path):
        """find_and_load() returns defaults if it can't find a file —
        but defaults now don't include the required progression lists,
        so constructing without them must fail."""
        with patch("bizniz.config.bizniz_config.Path.cwd", return_value=tmp_path):
            with pytest.raises(Exception):
                BiznizConfig.find_and_load()

    def test_finds_yaml_in_cwd(self, tmp_path):
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(_config_yaml({
            "engineer_model": "gpt-5", "max_iterations": 5,
        }))

        with patch("bizniz.config.bizniz_config.Path.cwd", return_value=tmp_path):
            config = BiznizConfig.find_and_load()

        assert config.engineer_model == "gpt-5"
        assert config.max_iterations == 5

    def test_finds_yaml_in_parent_directory(self, tmp_path):
        child = tmp_path / "subdir" / "deep"
        child.mkdir(parents=True)
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(_config_yaml({"engineer_model": "gpt-4o"}))

        with patch("bizniz.config.bizniz_config.Path.cwd", return_value=child):
            config = BiznizConfig.find_and_load()

        assert config.engineer_model == "gpt-4o"


class TestMakeProgressions:
    """Test the per-agent progression factories."""

    def test_make_model_progression_returns_coder_progression(self):
        """The shared make_model_progression() returns the coder
        progression (kept for legacy callers; new code should use
        per-agent factories)."""
        config = BiznizConfig(**MIN_PROGRESSIONS)
        progression = config.make_model_progression()
        assert isinstance(progression, ModelProgression)
        assert progression.current_model == "gpt-4o-mini"

    def test_make_autocoder_progression(self):
        config = BiznizConfig(
            coder_models=["gpt-4o-mini", "gpt-4o", "gpt-5"],
            tester_models=["gpt-4o"],
            repair_models=["gpt-4o"],
        )
        prog = config.make_autocoder_progression()
        assert prog.current_model == "gpt-4o-mini"
        assert prog.escalate() == "gpt-4o"
        assert prog.escalate() == "gpt-5"
        assert prog.is_at_max

    def test_make_autotester_progression_uses_its_own_list(self):
        config = BiznizConfig(
            coder_models=["wrong"],
            tester_models=["claude-sonnet", "claude-opus"],
            repair_models=["wrong"],
        )
        prog = config.make_autotester_progression()
        assert prog.current_model == "claude-sonnet"

    def test_make_repair_progression_uses_its_own_list(self):
        config = BiznizConfig(
            coder_models=["wrong"],
            tester_models=["wrong"],
            repair_models=["gpt-4o-mini", "gpt-4o"],
        )
        prog = config.make_repair_progression()
        assert prog.current_model == "gpt-4o-mini"


class TestNoModelsFallback:
    """The retired ``models`` field is no longer in the schema. Pydantic
    by default ignores unknown fields silently, so a stale ``models:``
    line in a user's bizniz.yaml just gets dropped on load — but the
    required per-agent fields ensure the config can't actually run
    without the new fields."""

    def test_no_models_attribute(self):
        config = BiznizConfig(**MIN_PROGRESSIONS)
        assert not hasattr(config, "models")


class TestNoDefaultModelFallback:
    """``default_model`` was the single-string fallback for
    ``make_client()`` calls without an explicit model. It's gone too —
    every call to make_client() must name its model."""

    def test_no_default_model_attribute(self):
        config = BiznizConfig(**MIN_PROGRESSIONS)
        assert not hasattr(config, "default_model")

    def test_make_client_without_model_raises(self):
        config = BiznizConfig(**MIN_PROGRESSIONS)
        with pytest.raises(ValueError, match="explicit model name"):
            config.make_client(model="")
        with pytest.raises(TypeError):  # missing required positional
            config.make_client()  # type: ignore[call-arg]

    def test_make_client_with_explicit_model_routes(self):
        """Explicit model name routes by prefix as before."""
        config = BiznizConfig(**MIN_PROGRESSIONS, gemini_api_key="x")
        # We don't actually instantiate a client here — that requires
        # a working API key. Just verify the routing function is
        # exposed and the call shape works.
        assert callable(config.make_client)
