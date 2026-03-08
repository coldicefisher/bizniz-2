"""Tests for BiznizConfig."""

import os
import tempfile
from unittest.mock import patch

import pytest
import yaml

from bizniz.config.bizniz_config import BiznizConfig
from bizniz.clients.chatgpt.chatgpt_client import ChatGPTClient
from bizniz.orchestrator.model_progression import ModelProgression


class TestFromYaml:
    """Test BiznizConfig.from_yaml() loading from a temp file."""

    def test_loads_all_fields(self, tmp_path):
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(yaml.dump({
            "default_model": "gpt-4o",
            "models": ["gpt-4o", "gpt-5"],
            "api_key": "sk-test-key",
            "is_azure": True,
            "api_base": "https://my-azure.openai.azure.com",
            "max_iterations": 10,
        }))

        config = BiznizConfig.from_yaml(str(config_file))

        assert config.default_model == "gpt-4o"
        assert config.models == ["gpt-4o", "gpt-5"]
        assert config.api_key == "sk-test-key"
        assert config.is_azure is True
        assert config.api_base == "https://my-azure.openai.azure.com"
        assert config.max_iterations == 10

    def test_partial_yaml_uses_defaults(self, tmp_path):
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(yaml.dump({"default_model": "gpt-5"}))

        config = BiznizConfig.from_yaml(str(config_file))

        assert config.default_model == "gpt-5"
        assert config.models == ["gpt-4o-mini", "gpt-4o", "gpt-5"]
        assert config.api_key is None
        assert config.max_iterations == 20

    def test_empty_yaml_uses_all_defaults(self, tmp_path):
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text("")

        config = BiznizConfig.from_yaml(str(config_file))

        assert config.default_model == "gpt-4o-mini"
        assert config.max_iterations == 20


class TestFindAndLoad:
    """Test BiznizConfig.find_and_load() defaults when no file exists."""

    def test_returns_defaults_when_no_file(self, tmp_path):
        """When no bizniz.yaml exists anywhere up the tree, return defaults."""
        with patch("bizniz.config.bizniz_config.Path.cwd", return_value=tmp_path):
            config = BiznizConfig.find_and_load()

        assert config.default_model == "gpt-4o-mini"
        assert config.models == ["gpt-4o-mini", "gpt-4o", "gpt-5"]
        assert config.max_iterations == 20

    def test_finds_yaml_in_cwd(self, tmp_path):
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(yaml.dump({"default_model": "gpt-5", "max_iterations": 5}))

        with patch("bizniz.config.bizniz_config.Path.cwd", return_value=tmp_path):
            config = BiznizConfig.find_and_load()

        assert config.default_model == "gpt-5"
        assert config.max_iterations == 5

    def test_finds_yaml_in_parent_directory(self, tmp_path):
        child = tmp_path / "subdir" / "deep"
        child.mkdir(parents=True)
        config_file = tmp_path / "bizniz.yaml"
        config_file.write_text(yaml.dump({"default_model": "gpt-4o"}))

        with patch("bizniz.config.bizniz_config.Path.cwd", return_value=child):
            config = BiznizConfig.find_and_load()

        assert config.default_model == "gpt-4o"


class TestMakeModelProgression:
    """Test make_model_progression() returns correct models."""

    def test_returns_model_progression_with_configured_models(self):
        config = BiznizConfig(models=["gpt-4o-mini", "gpt-4o"])
        progression = config.make_model_progression()

        assert isinstance(progression, ModelProgression)
        assert progression.current_model == "gpt-4o-mini"

    def test_default_models(self):
        config = BiznizConfig()
        progression = config.make_model_progression()

        assert progression.current_model == "gpt-4o-mini"
        # Escalate through all models
        assert progression.escalate() == "gpt-4o"
        assert progression.escalate() == "gpt-5"
        assert progression.is_at_max


class TestMakeClient:
    """Test make_client() creates a ChatGPTClient."""

    def test_creates_client_with_api_key_from_config(self):
        config = BiznizConfig(api_key="sk-test-123")
        client = config.make_client()

        assert isinstance(client, ChatGPTClient)

    def test_creates_client_with_api_key_from_env(self):
        config = BiznizConfig()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env-key"}):
            client = config.make_client()

        assert isinstance(client, ChatGPTClient)

    def test_creates_client_with_default_model(self):
        config = BiznizConfig(api_key="sk-test-456", default_model="gpt-4o")
        client = config.make_client()

        assert isinstance(client, ChatGPTClient)
