"""Tests for the critical-docs gate + iterative document recovery
(D17, 2026-05-17).

Covers:

- ``_critical_docs_for`` returns the right list for a given
  architecture (always: architecture / infrastructure / auth;
  plus ``api/<svc>.md`` per backend).
- ``_missing_critical_docs`` flags absent + too-small files.
- ``_maybe_recover_document`` loops via ProgressTracker:
  converges when files appear, halts via hard-gate on stall.
- No-op when no DocumentRecovery is wired (legacy behavior).
- Defensive: dispatch exception breaks out cleanly, gate still
  fires if files still missing.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bizniz.architect.types import ServiceDefinition, SystemArchitecture
from bizniz.driver.gates import GatePolicy, GateViolation
from bizniz.driver.milestone_loop import MilestoneLoop
from bizniz.driver.state import MilestoneState, SubPhase
from bizniz.lib.agentic_phase_recovery import PhaseRecoveryResult


# ── Helpers ──────────────────────────────────────────────────────


def _arch(*services) -> SystemArchitecture:
    if not services:
        services = (
            ServiceDefinition(
                name="backend", service_type="backend",
                framework="fastapi", language="python",
                description="API", workspace_name="backend", port=8000,
            ),
        )
    return SystemArchitecture(
        project_name="X", project_slug="x",
        description="x", services=list(services),
    )


def _make_loop_skeleton(
    *,
    project_root: Path,
    document_recovery=None,
    stall_threshold: int = 3,
    gates: GatePolicy = None,
) -> MilestoneLoop:
    loop = MilestoneLoop.__new__(MilestoneLoop)
    loop._project_root = project_root
    loop._document_recovery = document_recovery
    loop._document_recovery_stall_threshold = stall_threshold
    loop._gates = gates or GatePolicy(mode="strict")
    loop._on_status = None
    return loop


def _milestone(name: str = "M1"):
    m = MagicMock()
    m.name = name
    return m


def _state(tmp_path):
    return MilestoneState(tmp_path / "runs" / "x" / "m1", 1)


def _write_critical(docs_root: Path, *paths: str, body: bytes = b"# Title\n\n" + b"x" * 200) -> None:
    for p in paths:
        full = docs_root / p
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(body)


# ── Critical-docs list ───────────────────────────────────────────


class TestCriticalDocsFor:
    def test_always_includes_three_root_docs(self, tmp_path):
        loop = _make_loop_skeleton(project_root=tmp_path)
        out = loop._critical_docs_for(_arch())
        assert "architecture.md" in out
        assert "infrastructure.md" in out
        assert "auth.md" in out

    def test_includes_api_md_per_backend(self, tmp_path):
        loop = _make_loop_skeleton(project_root=tmp_path)
        arch = _arch(
            ServiceDefinition(
                name="api", service_type="backend",
                framework="fastapi", language="python",
                description="x", workspace_name="api", port=8001,
            ),
            ServiceDefinition(
                name="worker", service_type="worker",
                framework="celery", language="python",
                description="x", workspace_name="worker", port=None,
            ),
            ServiceDefinition(
                name="admin", service_type="backend",
                framework="fastapi", language="python",
                description="x", workspace_name="admin", port=8002,
            ),
        )
        out = loop._critical_docs_for(arch)
        assert "api/api.md" in out
        assert "api/admin.md" in out
        assert "api/worker.md" not in out  # worker is not backend


# ── Missing-docs detection ───────────────────────────────────────


class TestMissingCriticalDocs:
    def test_all_missing_when_docs_dir_absent(self, tmp_path):
        loop = _make_loop_skeleton(project_root=tmp_path)
        missing = loop._missing_critical_docs(_arch())
        assert "architecture.md" in missing
        assert "infrastructure.md" in missing
        assert "auth.md" in missing
        assert "api/backend.md" in missing

    def test_present_with_content_passes(self, tmp_path):
        docs = tmp_path / "docs"
        _write_critical(
            docs, "architecture.md", "infrastructure.md", "auth.md",
            "api/backend.md",
        )
        loop = _make_loop_skeleton(project_root=tmp_path)
        assert loop._missing_critical_docs(_arch()) == []

    def test_too_small_files_count_as_missing(self, tmp_path):
        docs = tmp_path / "docs"
        # Below the 100-byte threshold.
        _write_critical(
            docs, "architecture.md", body=b"# TODO\n",
        )
        # Others fully absent.
        loop = _make_loop_skeleton(project_root=tmp_path)
        missing = loop._missing_critical_docs(_arch())
        # The too-small architecture.md IS in missing.
        assert "architecture.md" in missing


# ── _maybe_recover_document ──────────────────────────────────────


class TestMaybeRecoverDocument:
    def test_no_op_when_no_recovery_wired(self, tmp_path):
        loop = _make_loop_skeleton(
            project_root=tmp_path, document_recovery=None,
        )
        # No recovery means no gate even with all docs missing.
        loop._maybe_recover_document(
            milestone=_milestone(),
            architecture=_arch(),
            state=_state(tmp_path),
        )  # no raise

    def test_no_op_when_all_critical_present(self, tmp_path):
        docs = tmp_path / "docs"
        _write_critical(
            docs, "architecture.md", "infrastructure.md", "auth.md",
            "api/backend.md",
        )
        recovery = MagicMock()
        loop = _make_loop_skeleton(
            project_root=tmp_path, document_recovery=recovery,
        )
        loop._maybe_recover_document(
            milestone=_milestone(),
            architecture=_arch(),
            state=_state(tmp_path),
        )
        # Recovery never dispatched because nothing was missing.
        recovery.recover.assert_not_called()

    def test_converges_when_recovery_writes_files(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        recovery = MagicMock()

        def fake_recover(**kwargs):
            # Pretend the agent wrote the files.
            _write_critical(
                docs, "architecture.md", "infrastructure.md", "auth.md",
                "api/backend.md",
            )
            return PhaseRecoveryResult(attempted=True, succeeded=True)

        recovery.recover.side_effect = fake_recover

        loop = _make_loop_skeleton(
            project_root=tmp_path, document_recovery=recovery,
            stall_threshold=3,
        )
        # Should converge after 1 iteration without raising.
        loop._maybe_recover_document(
            milestone=_milestone(),
            architecture=_arch(),
            state=_state(tmp_path),
        )
        assert recovery.recover.call_count == 1
        # All critical docs now exist.
        assert loop._missing_critical_docs(_arch()) == []

    def test_hard_gates_after_stall(self, tmp_path):
        recovery = MagicMock()
        recovery.recover.return_value = PhaseRecoveryResult(
            attempted=True, succeeded=False, summary="couldn't",
        )

        loop = _make_loop_skeleton(
            project_root=tmp_path, document_recovery=recovery,
            stall_threshold=2,
        )

        with pytest.raises(GateViolation) as exc:
            loop._maybe_recover_document(
                milestone=_milestone(),
                architecture=_arch(),
                state=_state(tmp_path),
            )
        assert exc.value.gate_name == "document_critical_missing"
        # threshold=2 → 2 stalled iters before hard-gate.
        assert recovery.recover.call_count == 2

    def test_dispatch_exception_still_hard_gates(self, tmp_path):
        recovery = MagicMock()
        recovery.recover.side_effect = RuntimeError("boom")

        loop = _make_loop_skeleton(
            project_root=tmp_path, document_recovery=recovery,
        )
        with pytest.raises(GateViolation) as exc:
            loop._maybe_recover_document(
                milestone=_milestone(),
                architecture=_arch(),
                state=_state(tmp_path),
            )
        assert exc.value.gate_name == "document_critical_missing"

    def test_recovery_not_attempted_short_circuits(self, tmp_path):
        """Agent returned attempted=False (e.g. claude binary
        missing at runtime). Hard-gate fires because we still have
        missing docs and no way to write them."""
        recovery = MagicMock()
        recovery.recover.return_value = PhaseRecoveryResult(
            attempted=False, succeeded=False,
            summary="claude binary missing",
        )

        loop = _make_loop_skeleton(
            project_root=tmp_path, document_recovery=recovery,
        )
        with pytest.raises(GateViolation):
            loop._maybe_recover_document(
                milestone=_milestone(),
                architecture=_arch(),
                state=_state(tmp_path),
            )
        # Only one dispatch — the not-attempted path bails out.
        assert recovery.recover.call_count == 1
