"""
Provisioner templates — reusable handlers for common infrastructure services.

Each template knows how to:
  - emit a docker-compose service entry for the architecture's service
  - write any auxiliary config files (init.sql, kickstart.json, nginx.conf)
  - contribute environment variables to the project's .env

Templates are looked up by ``framework`` first (e.g. ``fusionauth``), then
falling back to ``service_type`` (e.g. ``database``). Add new ones by
registering with ``register()`` in this package's ``__init__``.
"""
from bizniz.provisioner.templates.base import (
    InfraTemplate,
    TemplateContext,
    TemplateOutput,
    register,
    lookup,
    all_templates,
)

# Register concrete templates so ``lookup()`` can find them.
from bizniz.provisioner.templates.postgres import PostgresTemplate
from bizniz.provisioner.templates.redis import RedisTemplate
from bizniz.provisioner.templates.fusionauth import FusionAuthTemplate
from bizniz.provisioner.templates.app_python import PythonAppTemplate
from bizniz.provisioner.templates.app_typescript import TypeScriptAppTemplate

register("postgres", PostgresTemplate())
register("redis", RedisTemplate())
register("fusionauth", FusionAuthTemplate())
# Generic app-service Dockerfile/requirements producers used when no skeleton.
register("__python_app__", PythonAppTemplate())
register("__typescript_app__", TypeScriptAppTemplate())

__all__ = [
    "InfraTemplate",
    "TemplateContext",
    "TemplateOutput",
    "register",
    "lookup",
    "all_templates",
    "PostgresTemplate",
    "RedisTemplate",
    "FusionAuthTemplate",
    "PythonAppTemplate",
    "TypeScriptAppTemplate",
]
