"""Deterministic code documenters.

Each documenter walks a service's workspace and extracts the
machine-readable contract — exports, function signatures, class
fields, route declarations — into a stable JSON shape under
``docs/<service>/code/``.

Documenters are language/framework specific (because AST tooling is)
but their output shape is uniform so consumers (the coder, the next
engineer, the integration tester) read the same kind of artifact
regardless of source language.

NO AI calls. Pure mechanical extraction. Re-run after every engineer
completes a service; the artifact regenerates from source so it
never drifts.
"""

from bizniz.documenters.python_ast import PythonAstDocumenter
from bizniz.documenters.typescript_ast import (
    TypeScriptAstDocumenter,
    DocumenterError,
)
from bizniz.documenters.persist import write_service_docs, docs_dir_for

__all__ = [
    "PythonAstDocumenter",
    "TypeScriptAstDocumenter",
    "DocumenterError",
    "write_service_docs",
    "docs_dir_for",
]
