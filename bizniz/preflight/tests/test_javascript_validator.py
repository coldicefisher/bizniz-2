"""Tests for JavaScript pre-flight validator."""

import pytest
from unittest.mock import MagicMock

from bizniz.preflight.javascript_validator import JavaScriptPreflightValidator
from bizniz.workspace.base_workspace import BaseWorkspace


@pytest.fixture
def workspace():
    ws = MagicMock(spec=BaseWorkspace)
    ws.list_relative_files.return_value = []
    return ws


@pytest.fixture
def validator(workspace):
    return JavaScriptPreflightValidator(workspace)


class TestImportResolution:

    def test_node_builtin_passes(self, validator):
        files = {
            "app.js": "const path = require('path');\nconst fs = require('fs');\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_declared_dependency_passes(self, validator):
        files = {
            "app.js": "const express = require('express');\n",
        }
        result = validator.validate(files, ["express"])
        assert result.passed

    def test_es_module_import(self, validator):
        files = {
            "app.mjs": "import express from 'express';\n",
        }
        result = validator.validate(files, ["express"])
        assert result.passed
        assert result.files_checked == 1

    def test_relative_require_resolves(self, validator):
        files = {
            "lib/utils.js": "module.exports = { helper: () => {} };\n",
            "app.js": "const utils = require('./lib/utils');\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_missing_relative_creates_stub(self, validator):
        files = {
            "app.js": "const db = require('./db');\n",
        }
        result = validator.validate(files, [])
        assert len(result.stubs_created) == 1
        assert result.stubs_created[0].filepath == "db.js"
        assert "module.exports" in result.stubs_created[0].content

    def test_missing_package_flagged(self, validator):
        files = {
            "app.js": "const axios = require('axios');\n",
        }
        result = validator.validate(files, [])
        assert not result.passed
        assert result.issues[0].issue == "missing_dependency"

    def test_dynamic_import(self, validator):
        files = {
            "app.js": "const mod = import('./lazy');\n",
        }
        result = validator.validate(files, [])
        # Missing relative creates stub
        assert len(result.stubs_created) == 1
