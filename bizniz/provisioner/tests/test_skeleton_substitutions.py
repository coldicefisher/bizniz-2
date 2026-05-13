"""Tests for skeleton post-seed substitutions."""
from pathlib import Path

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.provisioner.skeleton_substitutions import apply_substitutions


def _arch(services):
    return SystemArchitecture(
        project_name="t", project_slug="t",
        services=services, description="",
    )


def _backend(name: str = "backend", framework: str = "fastapi") -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type="backend", framework=framework,
        language="python", description="", workspace_name=name, port=8000,
    )


def _frontend(name: str = "frontend") -> ServiceDefinition:
    return ServiceDefinition(
        name=name, service_type="frontend", framework="react",
        language="typescript", description="", workspace_name=name, port=5173,
    )


class TestReactViteProxySubstitution:
    def test_rewrites_default_target_to_actual_backend(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'export default defineConfig({\n'
            '  server: {\n'
            '    proxy: {\n'
            '      "/api": {\n'
            '        target: "http://api:8000",\n'
            '        changeOrigin: true,\n'
            '      },\n'
            '    },\n'
            '  },\n'
            '});\n'
        )
        arch = _arch([_backend("backend"), _frontend()])
        fe = _frontend()
        applied = apply_substitutions("react", ws, arch, fe)
        assert applied, "expected the vite proxy substitution to fire"
        new_content = (ws / "vite.config.ts").read_text()
        assert 'target: "http://backend:8000"' in new_content
        assert 'http://api:8000' not in new_content

    def test_uses_correct_backend_name_when_not_api(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'target: "http://api:8000",\n'
        )
        arch = _arch([_backend("core-service"), _frontend()])
        fe = _frontend()
        apply_substitutions("react", ws, arch, fe)
        assert 'http://core-service:8000' in (ws / "vite.config.ts").read_text()

    def test_skips_when_no_backend_in_arch(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'target: "http://api:8000",\n'
        )
        arch = _arch([_frontend()])  # frontend-only project
        fe = _frontend()
        applied = apply_substitutions("react", ws, arch, fe)
        # Substitution was tried but skipped (no backend service).
        assert applied == []
        # Original content unchanged.
        assert 'http://api:8000' in (ws / "vite.config.ts").read_text()

    def test_skips_when_file_missing(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        # No vite.config.ts at all.
        arch = _arch([_backend(), _frontend()])
        applied = apply_substitutions("react", ws, arch, _frontend())
        assert applied == []

    def test_skips_when_pattern_not_found(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        # vite.config.ts exists but doesn't have the default pattern
        # (e.g. someone already edited it).
        (ws / "vite.config.ts").write_text(
            'target: "http://something-else:9999",\n'
        )
        arch = _arch([_backend(), _frontend()])
        applied = apply_substitutions("react", ws, arch, _frontend())
        assert applied == []
        # Don't mangle a custom config.
        assert 'http://something-else:9999' in (ws / "vite.config.ts").read_text()

    def test_unknown_skeleton_is_noop(self, tmp_path):
        ws = tmp_path / "x"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'target: "http://api:8000",\n'
        )
        arch = _arch([_backend(), _frontend()])
        applied = apply_substitutions("totally-unknown", ws, arch, _frontend())
        assert applied == []

    def test_status_callback_logs_substitutions(self, tmp_path):
        ws = tmp_path / "frontend"
        ws.mkdir()
        (ws / "vite.config.ts").write_text(
            'target: "http://api:8000",\n'
        )
        msgs = []
        apply_substitutions(
            "react", ws, _arch([_backend(), _frontend()]),
            _frontend(),
            on_status=msgs.append,
        )
        assert any("applied skeleton substitution" in m for m in msgs)
        assert any("vite.config.ts" in m for m in msgs)
