"""Unit tests for UXDesigner agent."""

import json
import struct
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bizniz.architect.types import ServiceDefinition
from bizniz.ux_designer.ux_designer import (
    UXDesigner,
    _strip_code_fences,
    _detect_design_system,
    run_ux_review,
)
from bizniz.ux_designer.prompts import EVALUATE_SCHEMA


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def frontend_service():
    return ServiceDefinition(
        name="frontend",
        service_type="frontend",
        framework="react",
        language="typescript",
        description="React frontend for pet grooming app",
        workspace_name="frontend",
        port=5173,
    )


@pytest.fixture
def mock_vision_client():
    client = MagicMock()
    client.get_text.return_value = (
        'const { test } = require("@playwright/test");\n'
        'test("screenshot home", async ({ page }) => {\n'
        '  await page.goto(process.env.FRONTEND_URL);\n'
        '  await page.screenshot({ path: "/workspace/screenshots/home.png" });\n'
        '});',
        "job-1",
        [{"role": "assistant", "content": "script"}],
    )
    client.get_text_with_images.return_value = (
        json.dumps({
            "overall_score": 7,
            "summary": "Clean layout, minor spacing issues",
            "issues": [
                {
                    "severity": "minor",
                    "category": "spacing",
                    "description": "Card margins too tight",
                    "fix_description": "Add mb-4 class to cards",
                    "screenshot": "home",
                    "target_file": "src/App.tsx",
                },
            ],
        }),
        "job-2",
        [{"role": "assistant", "content": "evaluation"}],
    )
    return client


@pytest.fixture
def mock_workspace(tmp_path):
    ws = MagicMock()
    ws.root = str(tmp_path)
    return ws


@pytest.fixture
def sample_png_bytes():
    """Minimal valid 1x1 PNG."""
    def _chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = zlib.compress(b"\x00\xff\x00\x00")
    idat = _chunk(b"IDAT", raw)
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


# ── Helper functions ─────────────────────────────────────────────


def test_strip_code_fences():
    assert _strip_code_fences("```javascript\ncode\n```") == "code"
    assert _strip_code_fences("```\ncode\n```") == "code"
    assert _strip_code_fences("plain code") == "plain code"


def test_detect_design_system():
    react = ServiceDefinition(
        name="fe", service_type="frontend", framework="react",
        language="typescript", description="", workspace_name="fe",
    )
    assert "Tailwind" in _detect_design_system(react)

    angular = ServiceDefinition(
        name="fe", service_type="frontend", framework="angular",
        language="typescript", description="", workspace_name="fe",
    )
    assert "Angular Material" in _detect_design_system(angular)


# ── Screenshot collection ─────────────────────────────────────────


def test_collect_screenshots(tmp_path, sample_png_bytes):
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir()
    (ss_dir / "home.png").write_bytes(sample_png_bytes)
    (ss_dir / "about.png").write_bytes(sample_png_bytes)
    (ss_dir / "empty.png").write_bytes(b"")  # should be skipped

    results = UXDesigner._collect_screenshots(ss_dir)
    assert len(results) == 2
    assert results[0]["name"] == "about"  # sorted alphabetically
    assert results[1]["name"] == "home"
    assert len(results[0]["bytes"]) > 50


def test_collect_screenshots_empty_dir(tmp_path):
    ss_dir = tmp_path / "screenshots"
    ss_dir.mkdir()
    assert UXDesigner._collect_screenshots(ss_dir) == []


def test_collect_screenshots_missing_dir(tmp_path):
    assert UXDesigner._collect_screenshots(tmp_path / "nope") == []


# ── Route discovery ──────────────────────────────────────────────


def test_discover_routes_from_files(tmp_path):
    routes_dir = tmp_path / "src" / "routes"
    routes_dir.mkdir(parents=True)
    (routes_dir / "Dashboard.tsx").write_text("export default function Dashboard() {}")
    (routes_dir / "Settings.tsx").write_text("export default function Settings() {}")

    ws = MagicMock()
    ws.root = str(tmp_path)
    svc = ServiceDefinition(
        name="fe", service_type="frontend", framework="react",
        language="typescript", description="", workspace_name="fe",
    )

    result = UXDesigner._discover_routes(ws, svc)
    assert "Dashboard" in result
    assert "Settings" in result


def test_discover_routes_from_app_tsx(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "App.tsx").write_text(
        'import { Routes } from "react-router";\n'
        '<Route path="/dashboard" />\n'
        '<Route path="/settings" />\n'
    )

    ws = MagicMock()
    ws.root = str(tmp_path)
    svc = ServiceDefinition(
        name="fe", service_type="frontend", framework="react",
        language="typescript", description="", workspace_name="fe",
    )

    result = UXDesigner._discover_routes(ws, svc)
    assert "/dashboard" in result
    assert "/settings" in result


# ── Evaluation ───────────────────────────────────────────────────


def test_evaluate_screenshots(mock_vision_client, frontend_service, sample_png_bytes):
    designer = UXDesigner(vision_client=mock_vision_client)

    screenshots = [
        {"name": "home", "bytes": sample_png_bytes, "mime_type": "image/png"},
    ]

    result = designer._evaluate_screenshots(
        screenshots=screenshots,
        service=frontend_service,
        problem_statement="Pet grooming app",
        design_system="Tailwind CSS",
    )

    assert result["overall_score"] == 7
    assert len(result["issues"]) == 1
    mock_vision_client.get_text_with_images.assert_called_once()


def test_evaluate_handles_invalid_json(mock_vision_client, frontend_service, sample_png_bytes):
    mock_vision_client.get_text_with_images.return_value = ("not json", "j", [])

    designer = UXDesigner(vision_client=mock_vision_client)
    result = designer._evaluate_screenshots(
        screenshots=[{"name": "home", "bytes": sample_png_bytes, "mime_type": "image/png"}],
        service=frontend_service,
        problem_statement="test",
        design_system="CSS",
    )

    assert result["overall_score"] == 5
    assert result["issues"] == []


# ── Full review (mocked subprocess) ─────────────────────────────


@patch("bizniz.ux_designer.ux_designer.subprocess")
def test_review_skips_when_no_screenshots(
    mock_subprocess, mock_vision_client, frontend_service, mock_workspace,
):
    mock_subprocess.run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

    # Fallback also fails
    designer = UXDesigner(vision_client=mock_vision_client)
    result = designer.review_frontend(
        service=frontend_service,
        workspace=mock_workspace,
        compose_path="/fake/docker-compose.yml",
        problem_statement="test",
    )

    assert result["screenshots_taken"] == 0


@patch("bizniz.ux_designer.ux_designer.subprocess")
def test_review_stops_when_score_acceptable(
    mock_subprocess, mock_vision_client, frontend_service, sample_png_bytes, tmp_path,
):
    # Use a real workspace path
    ws = MagicMock()
    ws.root = str(tmp_path)

    # Mock subprocess to "write" a screenshot the way the real script would,
    # so the post-clear collect step finds it.
    ss_dir = tmp_path / "screenshots"
    def _fake_run(*a, **kw):
        ss_dir.mkdir(parents=True, exist_ok=True)
        (ss_dir / "home.png").write_bytes(sample_png_bytes)
        return MagicMock(returncode=0, stdout="ok", stderr="")
    mock_subprocess.run.side_effect = _fake_run

    # Score of 7 >= acceptable (6)
    designer = UXDesigner(vision_client=mock_vision_client, acceptable_score=6)
    result = designer.review_frontend(
        service=frontend_service,
        workspace=ws,
        compose_path="/fake/docker-compose.yml",
        problem_statement="test",
    )

    assert result["final_score"] == 7
    assert result["fixes_applied"] == 0
    assert result["iterations"] == 1


# ── run_ux_review top-level function ──────────────────────────────


def test_run_ux_review_skips_no_frontends():
    from bizniz.architect.types import SystemArchitecture
    arch = SystemArchitecture(
        project_name="test",
        project_slug="test",
        services=[
            ServiceDefinition(
                name="backend", service_type="backend", framework="fastapi",
                language="python", description="API", workspace_name="backend", port=8000,
            ),
        ],
        description="test",
    )

    results = run_ux_review(
        architecture=arch,
        service_workspaces={},
        compose_path="/fake",
        problem_statement="test",
        vision_client=MagicMock(),
    )
    assert results == []
