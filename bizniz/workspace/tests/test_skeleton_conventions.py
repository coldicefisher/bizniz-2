"""Tests for the SKELETON.md → system-prompt loader.

Regression coverage for the run-#3 bug: the FastAPI engineer wrote
``pet_groomer/routers/`` as a parallel package while the skeleton's
``app/main.py`` only mounted its own auth router, so the deployed
container served zero domain endpoints. SKELETON.md is the contract
the agents now read; this test locks in that the loader actually
returns its content as a system-prompt section, so the agents can't
silently miss it.
"""
from __future__ import annotations

from bizniz.workspace.local_workspace import LocalWorkspace
from bizniz.workspace.skeleton_conventions import load_skeleton_conventions


def test_returns_none_when_skeleton_md_missing(tmp_path):
    ws = LocalWorkspace(root=tmp_path)
    assert load_skeleton_conventions(ws) is None


def test_returns_none_for_empty_skeleton_md(tmp_path):
    ws = LocalWorkspace(root=tmp_path)
    (tmp_path / "SKELETON.md").write_text("   \n  \n")
    assert load_skeleton_conventions(ws) is None


def test_loads_and_wraps_skeleton_md(tmp_path):
    ws = LocalWorkspace(root=tmp_path)
    body = "Routers go in app/api/routes/. Do not edit app/main.py."
    (tmp_path / "SKELETON.md").write_text(body)
    out = load_skeleton_conventions(ws)
    assert out is not None
    assert body in out
    assert "HARD CONSTRAINT" in out  # header marker
    assert "Violation symptom" in out  # footer warns about the run-#3 failure mode
