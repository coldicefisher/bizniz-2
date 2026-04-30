"""Unit tests for the AI template-gap fallback.

Mocks the AI client; never makes a real network call.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner import Provisioner
from bizniz.provisioner.ai_fallback import (
    AIFallbackResponse,
    AIFallbackTemplate,
    cache_path,
    generate_ai_fallback_response,
    save_cache,
)
from bizniz.provisioner.templates.base import TemplateContext


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    monkeypatch.setenv("BIZNIZ_TEMPLATE_CACHE_DIR", str(d))
    return d


def _service(**overrides) -> ServiceDefinition:
    base = dict(
        name="messaging", service_type="cache", framework="kafka",
        language="none", description="event bus",
        workspace_name="kafka", port=9092, depends_on=[], requirements=[],
        skeleton="none",
    )
    base.update(overrides)
    return ServiceDefinition(**base)


def _ai_response(payload: dict):
    """Build a fake (text, job_id, messages) tuple matching client.get_text."""
    return json.dumps(payload), "job-id", []


def _make_client(payload: dict) -> MagicMock:
    client = MagicMock()
    client.get_text.return_value = _ai_response(payload)
    return client


def _arch(*services, slug="proj") -> SystemArchitecture:
    return SystemArchitecture(
        project_name="Proj", project_slug=slug, description="d",
        services=list(services),
    )


# ── generate_ai_fallback_response ────────────────────────────────────────────


def test_generate_calls_client_and_caches(cache_dir):
    payload = {
        "dockerfile": "",
        "upstream_image": "confluentinc/cp-kafka:7.6.0",
        "env_vars": {"KAFKA_BROKER_ID": "1"},
        "infra_files": {},
        "notes": "Confluent's official Kafka image.",
    }
    client = _make_client(payload)

    response = generate_ai_fallback_response(
        client=client,
        framework="kafka",
        service_type="cache",
        description="event bus",
    )
    assert response.upstream_image == "confluentinc/cp-kafka:7.6.0"
    assert response.dockerfile == ""
    assert response.env_vars == {"KAFKA_BROKER_ID": "1"}
    assert client.get_text.call_count == 1

    # Cache file should now exist
    p = cache_path("kafka", "cache")
    assert p.exists()
    cached = json.loads(p.read_text())
    assert cached["upstream_image"] == "confluentinc/cp-kafka:7.6.0"
    assert "_cached_at" in cached


def test_generate_uses_cache_on_second_call(cache_dir):
    response = AIFallbackResponse(
        upstream_image="clickhouse/clickhouse-server:24.3",
        env_vars={"CLICKHOUSE_DB": "default"},
        notes="cached",
    )
    save_cache("clickhouse", "database", response)

    client = _make_client({"this": "should-never-be-called"})
    out = generate_ai_fallback_response(
        client=client,
        framework="clickhouse",
        service_type="database",
        description="OLAP store",
    )
    assert out.upstream_image == "clickhouse/clickhouse-server:24.3"
    client.get_text.assert_not_called()


def test_generate_skips_cache_when_use_cache_false(cache_dir):
    response = AIFallbackResponse(upstream_image="old:1.0", notes="stale")
    save_cache("kafka", "cache", response)

    payload = {
        "dockerfile": "", "upstream_image": "new:2.0",
        "env_vars": {}, "infra_files": {}, "notes": "fresh",
    }
    client = _make_client(payload)
    out = generate_ai_fallback_response(
        client=client, framework="kafka", service_type="cache",
        description="bus", use_cache=False,
    )
    assert out.upstream_image == "new:2.0"
    client.get_text.assert_called_once()


def test_generate_raises_when_neither_dockerfile_nor_image(cache_dir):
    payload = {
        "dockerfile": "", "upstream_image": "",
        "env_vars": {}, "infra_files": {}, "notes": "broken",
    }
    client = _make_client(payload)
    with pytest.raises(ValueError, match="neither dockerfile nor upstream_image"):
        generate_ai_fallback_response(
            client=client, framework="x", service_type="y", description="z",
        )


def test_generate_raises_on_empty_response(cache_dir):
    client = MagicMock()
    client.get_text.return_value = ("", "job", [])
    with pytest.raises(ValueError, match="empty"):
        generate_ai_fallback_response(
            client=client, framework="x", service_type="y", description="z",
        )


# ── AIFallbackTemplate.render ─────────────────────────────────────────────────


def test_render_with_upstream_image_uses_image_block(tmp_path):
    response = AIFallbackResponse(
        upstream_image="clickhouse/clickhouse-server:24.3",
        env_vars={"CLICKHOUSE_DB": "default"},
        notes="",
    )
    tmpl = AIFallbackTemplate(response, framework="clickhouse")
    ctx = TemplateContext(
        service=_service(name="ch", framework="clickhouse",
                         service_type="database", workspace_name="clickhouse",
                         port=8123, depends_on=[]),
        project_slug="proj",
        project_root=tmp_path,
    )
    out = tmpl.render(ctx)
    assert out.compose_service["image"] == "clickhouse/clickhouse-server:24.3"
    assert "build" not in out.compose_service
    assert out.compose_service["ports"] == ["8123:8123"]
    assert out.compose_service["networks"] == ["app-network"]
    assert out.env_vars == {"CLICKHOUSE_DB": "default"}
    # No Dockerfile written when using upstream_image
    assert not any(k.endswith("Dockerfile") for k in out.infra_files)


def test_render_with_dockerfile_writes_to_workspace_subdir(tmp_path):
    response = AIFallbackResponse(
        dockerfile="FROM custom:1\nRUN echo hi",
        env_vars={},
        infra_files={"config.yml": "key: value"},
        notes="",
    )
    tmpl = AIFallbackTemplate(response, framework="custom")
    ctx = TemplateContext(
        service=_service(name="custom", framework="custom",
                         service_type="cache", workspace_name="custom_ws",
                         port=7777, depends_on=["postgres"]),
        project_slug="proj",
        project_root=tmp_path,
    )
    out = tmpl.render(ctx)
    assert out.compose_service["build"] == {
        "context": "./custom_ws",
        "dockerfile": "Dockerfile",
    }
    assert out.compose_service["depends_on"] == ["postgres"]
    # Infra files prefixed with workspace_name
    assert "custom_ws/Dockerfile" in out.infra_files
    assert out.infra_files["custom_ws/Dockerfile"] == "FROM custom:1\nRUN echo hi"
    assert "custom_ws/config.yml" in out.infra_files


def test_render_does_not_emit_compose_level_ai_controlled_fields(tmp_path):
    """Even if the AI response somehow contained networks/healthcheck,
    the template should only emit Provisioner-controlled compose entries."""
    response = AIFallbackResponse(
        upstream_image="x:1", env_vars={}, infra_files={}, notes="",
    )
    tmpl = AIFallbackTemplate(response, framework="x")
    ctx = TemplateContext(
        service=_service(name="x", framework="x", service_type="cache",
                         workspace_name="x", port=1111, depends_on=[]),
        project_slug="proj",
        project_root=tmp_path,
    )
    out = tmpl.render(ctx)
    assert "healthcheck" not in out.compose_service
    # networks comes from Provisioner, not AI
    assert out.compose_service["networks"] == ["app-network"]


# ── Provisioner integration ──────────────────────────────────────────────────


def test_provisioner_without_fallback_skips_unknown_framework(cache_dir, tmp_path):
    arch = _arch(_service(name="bus", framework="kafka", service_type="cache",
                          workspace_name="bus", port=9092, depends_on=[]))
    p = Provisioner(project_parent=tmp_path, build_images=False)
    result = p.provision(arch, project_name="Proj")

    bus = next(s for s in result.services if s.name == "bus")
    assert bus.template_name is None
    assert bus.is_infrastructure is True


def test_provisioner_with_fallback_calls_ai_for_unknown_framework(cache_dir, tmp_path):
    payload = {
        "dockerfile": "",
        "upstream_image": "confluentinc/cp-kafka:7.6.0",
        "env_vars": {"KAFKA_BROKER_ID": "1"},
        "infra_files": {},
        "notes": "Confluent Kafka.",
    }
    client = _make_client(payload)
    factory = MagicMock(return_value=client)

    arch = _arch(_service(name="bus", framework="kafka", service_type="cache",
                          workspace_name="bus", port=9092, depends_on=[]))
    p = Provisioner(
        project_parent=tmp_path,
        build_images=False,
        ai_client_factory=factory,
        ai_fallback_enabled=True,
    )
    result = p.provision(arch, project_name="Proj")

    bus = next(s for s in result.services if s.name == "bus")
    assert bus.template_name == "ai_fallback:kafka"

    # Factory called with the configured model
    factory.assert_called_once_with("gemini-flash")
    client.get_text.assert_called_once()

    # Compose has the kafka entry
    compose = Path(result.compose_path).read_text()
    assert "bus:" in compose
    assert "confluentinc/cp-kafka:7.6.0" in compose


def test_provisioner_fallback_failure_falls_through_quietly(cache_dir, tmp_path):
    """If the AI call raises, Provisioner logs and treats it as no template."""
    client = MagicMock()
    client.get_text.side_effect = RuntimeError("API down")
    factory = MagicMock(return_value=client)

    arch = _arch(_service(name="bus", framework="kafka", service_type="cache",
                          workspace_name="bus", port=9092, depends_on=[]))
    p = Provisioner(
        project_parent=tmp_path, build_images=False,
        ai_client_factory=factory, ai_fallback_enabled=True,
    )
    statuses = []
    p._on_status_message = statuses.append

    result = p.provision(arch, project_name="Proj")
    bus = next(s for s in result.services if s.name == "bus")
    assert bus.template_name is None
    assert any("AI fallback for 'kafka' failed" in s for s in statuses)


def test_provisioner_requires_factory_when_fallback_enabled(tmp_path):
    from bizniz.provisioner.types import ProvisionerError
    with pytest.raises(ProvisionerError, match="ai_client_factory"):
        Provisioner(
            project_parent=tmp_path,
            build_images=False,
            ai_fallback_enabled=True,
        )


def test_provisioner_fallback_does_not_disturb_static_templates(cache_dir, tmp_path):
    """Even with fallback enabled, services that have static templates
    (postgres, redis, fusionauth) must NOT call the AI."""
    client = _make_client({"dockerfile": "", "upstream_image": "x:1",
                           "env_vars": {}, "infra_files": {}, "notes": ""})
    factory = MagicMock(return_value=client)

    arch = _arch(
        _service(name="db", framework="postgres", service_type="database",
                 workspace_name="postgres", port=5433, depends_on=[]),
        _service(name="cache", framework="redis", service_type="cache",
                 workspace_name="redis", port=6379, depends_on=[]),
    )
    p = Provisioner(
        project_parent=tmp_path, build_images=False,
        ai_client_factory=factory, ai_fallback_enabled=True,
    )
    p.provision(arch, project_name="Proj")
    factory.assert_not_called()
    client.get_text.assert_not_called()
