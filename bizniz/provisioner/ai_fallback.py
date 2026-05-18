"""AI fallback for infrastructure templates the Provisioner doesn't know.

When the architect requests an infrastructure framework (e.g. clickhouse,
kafka, dgraph) that has no static template registered, the Provisioner
can opt into asking an AI to fill the gap. The AI's job is small and
narrow: emit a Dockerfile (or pick an upstream image) plus env vars and
optional config files. **It does not control compose-level concerns** —
the response schema does not include ports, depends_on, or networks.
The Provisioner injects those from the architect's plan.

Responses are cached at ``~/.bizniz/template_cache/<framework>__<service_type>.json``
so subsequent runs against the same framework don't re-call the AI.

Opt-in only — `Provisioner(ai_fallback_enabled=True, ai_client_factory=...)`.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from typing import Callable, Dict, Optional

from pydantic import BaseModel, Field

from bizniz.architect.types import ServiceDefinition
from bizniz.core.client import BaseAIClient
from bizniz.core.types import ResponseFormat
from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
)


_LOG = logging.getLogger(__name__)


# Schema is JSON-Schema strict. Notably absent: port, depends_on, networks,
# healthcheck, EXPOSE — those are the architect's / provisioner's territory.
AIFallbackSchema = {
    "name": "ai_fallback_template",
    "strict": True,
    "schema": {
        "type": "object",
        "required": ["dockerfile", "upstream_image", "env_vars", "infra_files", "notes"],
        "additionalProperties": False,
        "properties": {
            "dockerfile": {
                "type": "string",
                "description": (
                    "Full Dockerfile content for this service. Empty string "
                    "if upstream_image is set instead. Do NOT include EXPOSE "
                    "directives — port mapping is controlled by the "
                    "Provisioner. Do NOT reference compose-level concepts "
                    "(networks, depends_on)."
                ),
            },
            "upstream_image": {
                "type": "string",
                "description": (
                    "Pre-built image to use directly instead of a Dockerfile "
                    "(e.g. 'clickhouse/clickhouse-server:24.3'). Empty string "
                    "if dockerfile is provided."
                ),
            },
            "env_vars": {
                "type": "object",
                "description": (
                    "Environment variables this service needs at runtime. "
                    "Keys are env var names; values are defaults."
                ),
                "additionalProperties": {"type": "string"},
            },
            "infra_files": {
                "type": "object",
                "description": (
                    "Additional config files written under "
                    "infra/development/<service>/. Map of relative path -> "
                    "file content. Use {} if none needed."
                ),
                "additionalProperties": {"type": "string"},
            },
            "notes": {
                "type": "string",
                "description": "Brief explanation of design choices.",
            },
        },
    },
}


_SYSTEM_PROMPT = """You are an infrastructure provisioner generating a starter Docker setup for a service the static template registry does not know about.

Your output drives a docker-compose deployment. Other parts of the system (the architect's plan) own:
  - host port mapping (do NOT specify EXPOSE or ports)
  - service dependencies (do NOT specify depends_on)
  - networks (do NOT reference any network)
  - health checks at the compose level (you may add HEALTHCHECK in Dockerfile only when essential)

Your output is ONE of:
  (A) `dockerfile`: a full Dockerfile when the framework needs a custom build (rare for off-the-shelf infra).
  (B) `upstream_image`: a published image like 'clickhouse/clickhouse-server:24.3' (preferred for well-known services).

Only one of dockerfile / upstream_image should be non-empty. Set the other to "".

Provide minimal, production-aware env vars (auth, default DB, paths). Use sensible defaults that work for local development."""


class AIFallbackResponse(BaseModel):
    dockerfile: str = ""
    upstream_image: str = ""
    env_vars: Dict[str, str] = Field(default_factory=dict)
    infra_files: Dict[str, str] = Field(default_factory=dict)
    notes: str = ""


# ── Cache ────────────────────────────────────────────────────────────────────


def default_cache_dir() -> Path:
    """Per-user cache. Override via ``BIZNIZ_TEMPLATE_CACHE_DIR``."""
    env = os.environ.get("BIZNIZ_TEMPLATE_CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".bizniz" / "template_cache"


def _cache_key(framework: str, service_type: str) -> str:
    """Filesystem-safe key. service_type matters because e.g. kafka can be
    a 'cache' or 'messaging' service with different sensible defaults."""
    safe_fw = framework.replace("/", "_").replace(":", "_")
    safe_st = service_type.replace("/", "_")
    return f"{safe_fw}__{safe_st}.json"


def cache_path(framework: str, service_type: str, cache_dir: Optional[Path] = None) -> Path:
    return (cache_dir or default_cache_dir()) / _cache_key(framework, service_type)


def load_cached(
    framework: str, service_type: str, cache_dir: Optional[Path] = None,
) -> Optional[AIFallbackResponse]:
    p = cache_path(framework, service_type, cache_dir)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        # Strip the cache-only metadata before parsing.
        raw.pop("_cached_at", None)
        return AIFallbackResponse(**raw)
    except Exception as e:
        _LOG.warning("ai_fallback: cache read failed for %s (%s) — re-generating", p, e)
        return None


def save_cache(
    framework: str,
    service_type: str,
    response: AIFallbackResponse,
    cache_dir: Optional[Path] = None,
) -> None:
    p = cache_path(framework, service_type, cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = response.model_dump()
    payload["_cached_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    p.write_text(json.dumps(payload, indent=2))


# ── Generation ───────────────────────────────────────────────────────────────


def generate_ai_fallback_response(
    client: BaseAIClient,
    framework: str,
    service_type: str,
    description: str,
    use_cache: bool = True,
    cache_dir: Optional[Path] = None,
) -> AIFallbackResponse:
    """Ask the AI for a fallback template. Uses cache when available.

    Raises whatever the underlying client raises on failure — caller
    catches and falls back to the prior behavior (no compose entry).
    """
    if use_cache:
        cached = load_cached(framework, service_type, cache_dir)
        if cached is not None:
            _LOG.info("ai_fallback: cache hit for %s/%s", framework, service_type)
            return cached

    user_prompt = (
        f"Generate a starter infrastructure setup for the following service.\n\n"
        f"Framework: {framework}\n"
        f"Service type: {service_type}\n"
        f"Description: {description}\n\n"
        f"Return either an `upstream_image` (preferred for well-known services) or "
        f"a full `dockerfile`. Set the other to an empty string. "
        f"Do not include EXPOSE, ports, depends_on, or networks."
    )

    text, _job_id, _msgs = client.get_text(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format=ResponseFormat.JSON_SCHEMA,
        schema=AIFallbackSchema,
        use_message_history=False,
    )
    if not text or not text.strip():
        raise ValueError("AI fallback returned empty response")

    data = json.loads(text)
    response = AIFallbackResponse(**data)

    # Sanity: at least one of dockerfile / upstream_image must be set.
    if not response.dockerfile and not response.upstream_image:
        raise ValueError(
            "AI fallback response set neither dockerfile nor upstream_image"
        )

    if use_cache:
        save_cache(framework, service_type, response, cache_dir)
    return response


# ── Template adapter ─────────────────────────────────────────────────────────


class AIFallbackTemplate(InfraTemplate):
    """Wraps an `AIFallbackResponse` so it plugs into the existing
    template-rendering flow.

    The Provisioner calls ``render(ctx)`` and gets back a normal
    ``TemplateOutput`` — same as postgres, redis, fusionauth. Compose
    port mapping, depends_on, networks are layered on by the Provisioner
    based on the architect's plan, NOT this template's response.
    """

    def __init__(self, response: AIFallbackResponse, framework: str):
        self.response = response
        self.name = f"ai_fallback:{framework}"

    def render(self, ctx: TemplateContext) -> TemplateOutput:
        from bizniz.architect.types import host_port_for
        svc = ctx.service
        container_port = svc.port
        host_port = host_port_for(svc)
        ws = svc.workspace_name

        compose_service: Dict = {}
        if self.response.upstream_image:
            compose_service["image"] = self.response.upstream_image
        else:
            # Dockerfile lives at infra/development/<workspace>/Dockerfile,
            # which is what compose paths are relative to.
            compose_service["build"] = {
                "context": f"./{ws}",
                "dockerfile": "Dockerfile",
            }

        # Provisioner-controlled fields. The AI does not get to specify
        # these — we layer them on from the architect's plan.
        if container_port is not None:
            # ``svc.port`` is the container port; ``host_port`` is the
            # host-side mapping (may have been remapped on collision).
            # For AI-fallback templates we don't know the framework's
            # canonical container port — assume container=svc.port.
            compose_service["ports"] = [f"{host_port}:{container_port}"]
        compose_service["networks"] = ["app-network"]
        if svc.depends_on:
            compose_service["depends_on"] = list(svc.depends_on)

        # Prefix every infra file with the workspace name so it lands
        # under infra/development/<ws>/ — same convention as the static
        # templates (postgres/init.sql, fusionauth/kickstart/...).
        infra_files: Dict[str, str] = {}
        for rel, content in self.response.infra_files.items():
            infra_files[f"{ws}/{rel}"] = content
        if self.response.dockerfile and not self.response.upstream_image:
            infra_files[f"{ws}/Dockerfile"] = self.response.dockerfile

        return TemplateOutput(
            compose_service=compose_service,
            compose_networks=["app-network"],
            infra_files=infra_files,
            env_vars=dict(self.response.env_vars),
        )


# Type used by Provisioner to inject a model-specific client without
# coupling Provisioner to BiznizConfig.
AIClientFactory = Callable[[str], BaseAIClient]
