"""Tests for TypeScript pre-flight validator."""

import pytest
from unittest.mock import MagicMock

from bizniz.preflight.typescript_validator import TypeScriptPreflightValidator
from bizniz.workspace.base_workspace import BaseWorkspace


@pytest.fixture
def workspace():
    ws = MagicMock(spec=BaseWorkspace)
    ws.list_relative_files.return_value = []
    return ws


@pytest.fixture
def validator(workspace):
    return TypeScriptPreflightValidator(workspace)


class TestImportResolution:

    def test_node_builtin_passes(self, validator):
        files = {
            "app.ts": "import path from 'path';\nimport { readFile } from 'fs';\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_declared_dependency_passes(self, validator):
        files = {
            "app.ts": "import express from 'express';\n",
        }
        result = validator.validate(files, ["express"])
        assert result.passed

    def test_scoped_package_passes(self, validator):
        files = {
            "app.ts": "import { Test } from '@nestjs/testing';\n",
        }
        result = validator.validate(files, ["@nestjs/testing"])
        assert result.passed

    def test_missing_dependency_flagged(self, validator):
        files = {
            "app.ts": "import axios from 'axios';\n",
        }
        result = validator.validate(files, [])
        assert not result.passed
        assert result.issues[0].issue == "missing_dependency"

    def test_relative_import_resolves(self, validator):
        files = {
            "src/models.ts": "export class User {}\n",
            "src/app.ts": "import { User } from './models';\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_relative_import_with_extension(self, validator):
        files = {
            "src/models.ts": "export class User {}\n",
            "src/app.ts": "import { User } from './models.ts';\n",
        }
        result = validator.validate(files, [])
        # Exact .ts match passes since models.ts exists
        assert result.passed

    def test_missing_relative_import_creates_stub(self, validator):
        files = {
            "src/app.ts": "import { UserService } from './services/user';\n",
        }
        result = validator.validate(files, [])
        assert len(result.stubs_created) == 1
        assert result.stubs_created[0].filepath == "src/services/user.ts"
        assert "UserService" in result.stubs_created[0].content

    def test_index_file_resolves(self, validator):
        files = {
            "src/utils/index.ts": "export const helper = () => {};\n",
            "src/app.ts": "import { helper } from './utils';\n",
        }
        result = validator.validate(files, [])
        assert result.passed

    def test_tsx_files_checked(self, validator):
        files = {
            "src/App.tsx": "import React from 'react';\n",
        }
        result = validator.validate(files, ["react"])
        assert result.files_checked == 1
        assert result.passed


class TestStubGeneration:

    def test_class_export_stub(self, validator):
        files = {
            "src/app.ts": "import { UserModel, Config } from './types';\n",
        }
        result = validator.validate(files, [])
        stub = result.stubs_created[0]
        assert "export class UserModel" in stub.content

    def test_interface_stub_for_props(self, validator):
        files = {
            "src/App.tsx": "import { ButtonProps } from './types';\n",
        }
        result = validator.validate(files, [])
        stub = result.stubs_created[0]
        assert "export interface ButtonProps" in stub.content

    def test_const_export_stub(self, validator):
        files = {
            "src/app.ts": "import { apiUrl } from './config';\n",
        }
        result = validator.validate(files, [])
        stub = result.stubs_created[0]
        assert "export const apiUrl" in stub.content
