"""Tests for ``PIRunner`` + DAG builder + topological sort."""
from __future__ import annotations

import time
import threading

import pytest

from bizniz.coder.types import Issue
from bizniz.orchestrator.parallel_issue_runner import (
    PIRunner,
    build_dag,
    topological_levels,
)
from bizniz.per_issue_validator.types import ValidatedIssue


# ── Helpers ─────────────────────────────────────────────────────────


def _issue(
    iid: str,
    target_files: list = None,
    test_files: list = None,
    depends_on: list = None,
) -> Issue:
    return Issue(
        id=iid,
        title=iid,
        description=iid,
        service="backend",
        language="python",
        target_files=target_files or [],
        test_files=test_files or [],
        depends_on=depends_on or [],
        spec_refs=[],
    )


def _clean_runner(issue: Issue) -> ValidatedIssue:
    return ValidatedIssue(
        issue_id=issue.id, clean=True, files_written=issue.target_files,
    )


# ── DAG builder ─────────────────────────────────────────────────────


class TestBuildDAG:
    def test_no_overlap_no_deps_empty_edges(self):
        issues = [
            _issue("A", target_files=["a.py"]),
            _issue("B", target_files=["b.py"]),
            _issue("C", target_files=["c.py"]),
        ]
        deps = build_dag(issues)
        assert deps == {"A": set(), "B": set(), "C": set()}

    def test_file_overlap_creates_edge_in_list_order(self):
        # B and A both touch a.py; B is later in list → B depends on A.
        issues = [
            _issue("A", target_files=["a.py"]),
            _issue("B", target_files=["a.py", "b.py"]),
        ]
        deps = build_dag(issues)
        assert deps == {"A": set(), "B": {"A"}}

    def test_test_files_also_count_for_overlap(self):
        issues = [
            _issue("A", target_files=["conftest.py"]),
            _issue("B", test_files=["conftest.py"]),
        ]
        deps = build_dag(issues)
        assert deps["B"] == {"A"}

    def test_planner_depends_on_adds_edge(self):
        issues = [
            _issue("A", target_files=["a.py"]),
            _issue("B", target_files=["b.py"], depends_on=["A"]),
        ]
        deps = build_dag(issues)
        assert deps["B"] == {"A"}

    def test_union_with_no_remove_semantic(self):
        """File-overlap edge exists; planner depends_on adds another.
        Result is the union — file-overlap edges are NEVER removed."""
        issues = [
            _issue("A", target_files=["a.py"]),
            _issue("B", target_files=["a.py"], depends_on=["A"]),
        ]
        deps = build_dag(issues)
        assert deps["B"] == {"A"}  # one edge, not double-counted

    def test_self_edge_dropped(self):
        issues = [_issue("A", depends_on=["A"])]
        deps = build_dag(issues)
        assert deps["A"] == set()

    def test_unknown_dep_id_dropped(self):
        issues = [_issue("A", depends_on=["NONEXISTENT"])]
        deps = build_dag(issues)
        assert deps["A"] == set()


# ── Topological levels ─────────────────────────────────────────────


class TestTopologicalLevels:
    def test_no_deps_all_in_level_zero(self):
        issues = [
            _issue("A"), _issue("B"), _issue("C"),
        ]
        deps = build_dag(issues)
        levels, unsched = topological_levels(issues, deps)
        assert levels == [["A", "B", "C"]]
        assert unsched == []

    def test_linear_chain_one_per_level(self):
        # A → B → C (B depends on A, C depends on B).
        issues = [
            _issue("A", target_files=["a.py"]),
            _issue("B", target_files=["a.py", "b.py"]),
            _issue("C", target_files=["b.py", "c.py"]),
        ]
        deps = build_dag(issues)
        levels, unsched = topological_levels(issues, deps)
        assert levels == [["A"], ["B"], ["C"]]
        assert unsched == []

    def test_diamond_runs_middle_in_parallel(self):
        # A → B, A → C, B → D, C → D — B+C parallel in level 1.
        issues = [
            _issue("A"),
            _issue("B", depends_on=["A"]),
            _issue("C", depends_on=["A"]),
            _issue("D", depends_on=["B", "C"]),
        ]
        deps = build_dag(issues)
        levels, unsched = topological_levels(issues, deps)
        assert levels == [["A"], ["B", "C"], ["D"]]
        assert unsched == []

    def test_cycle_surfaces_as_unscheduled(self):
        # A depends on B, B depends on A — cycle.
        issues = [
            _issue("A", depends_on=["B"]),
            _issue("B", depends_on=["A"]),
        ]
        deps = build_dag(issues)
        levels, unsched = topological_levels(issues, deps)
        assert set(unsched) == {"A", "B"}
        assert levels == []


# ── Runner: serial path ────────────────────────────────────────────


class TestRunnerSerial:
    def test_single_issue_runs_clean(self):
        runner = PIRunner(max_parallel=6)
        result = runner.run(
            issues=[_issue("A", target_files=["a.py"])],
            issue_runner=_clean_runner,
        )
        assert result.clean_count == 1
        assert result.total_count == 1
        assert len(result.levels) == 1
        assert result.levels[0] == ["A"]

    def test_linear_chain_runs_in_dependency_order(self):
        seen_order: list = []
        lock = threading.Lock()

        def order_runner(issue: Issue) -> ValidatedIssue:
            with lock:
                seen_order.append(issue.id)
            return _clean_runner(issue)

        # Force linear chain via file overlap.
        issues = [
            _issue("A", target_files=["a.py"]),
            _issue("B", target_files=["a.py"]),
            _issue("C", target_files=["a.py"]),
        ]
        result = PIRunner(max_parallel=6).run(
            issues=issues, issue_runner=order_runner,
        )
        # Chain levels = 3 single-issue levels, in order A B C.
        assert seen_order == ["A", "B", "C"]
        assert result.clean_count == 3


# ── Runner: parallelism actually happens ───────────────────────────


class TestRunnerParallelism:
    def test_level_issues_run_concurrently(self):
        """3 issues with no deps, max_parallel=3 → all 3 start within a
        narrow window, then sleep — concurrent runtime should be ~0.2s,
        not 0.6s."""
        in_flight = []
        max_in_flight = [0]
        lock = threading.Lock()

        def slow_runner(issue: Issue) -> ValidatedIssue:
            with lock:
                in_flight.append(issue.id)
                if len(in_flight) > max_in_flight[0]:
                    max_in_flight[0] = len(in_flight)
            time.sleep(0.2)
            with lock:
                in_flight.remove(issue.id)
            return _clean_runner(issue)

        issues = [_issue(x) for x in ("A", "B", "C")]
        t0 = time.time()
        result = PIRunner(max_parallel=3).run(
            issues=issues, issue_runner=slow_runner,
        )
        wall = time.time() - t0
        # Parallel: should be ~0.2s, certainly < 0.5s.
        assert wall < 0.5, f"expected parallel run, got {wall:.2f}s"
        # All three observed in flight at once.
        assert max_in_flight[0] == 3
        assert result.clean_count == 3

    def test_max_parallel_caps_concurrency(self):
        """4 issues with max_parallel=2 — at most 2 should be in flight."""
        in_flight = []
        max_in_flight = [0]
        lock = threading.Lock()

        def slow_runner(issue: Issue) -> ValidatedIssue:
            with lock:
                in_flight.append(issue.id)
                if len(in_flight) > max_in_flight[0]:
                    max_in_flight[0] = len(in_flight)
            time.sleep(0.05)
            with lock:
                in_flight.remove(issue.id)
            return _clean_runner(issue)

        issues = [_issue(x) for x in ("A", "B", "C", "D")]
        PIRunner(max_parallel=2).run(
            issues=issues, issue_runner=slow_runner,
        )
        assert max_in_flight[0] == 2


# ── Runner: exception safety ───────────────────────────────────────


class TestRunnerExceptionSafety:
    def test_runner_exception_does_not_kill_level(self):
        def boom(issue: Issue) -> ValidatedIssue:
            if issue.id == "B":
                raise RuntimeError("boom")
            return _clean_runner(issue)

        result = PIRunner(max_parallel=3).run(
            issues=[_issue("A"), _issue("B"), _issue("C")],
            issue_runner=boom,
        )
        # All 3 land; B is broken with runner_exception halt_reason.
        assert result.total_count == 3
        b = next(v for v in result.validated if v.issue_id == "B")
        assert b.clean is False
        assert "runner_exception" in b.halt_reason
        # A and C are clean.
        a = next(v for v in result.validated if v.issue_id == "A")
        c = next(v for v in result.validated if v.issue_id == "C")
        assert a.clean is True
        assert c.clean is True


# ── Runner: cycles ──────────────────────────────────────────────────


class TestRunnerCycles:
    def test_cycle_issues_surfaced_as_broken_never_dispatched(self):
        issues = [
            _issue("A", depends_on=["B"]),
            _issue("B", depends_on=["A"]),
        ]
        result = PIRunner(max_parallel=6).run(
            issues=issues, issue_runner=_clean_runner,
        )
        assert result.total_count == 2
        assert result.failed_to_run == ["A", "B"] or result.failed_to_run == ["B", "A"]
        for v in result.validated:
            assert v.clean is False
            assert "dag_cycle" in v.halt_reason


# ── Constructor validation ─────────────────────────────────────────


class TestConstructor:
    def test_invalid_max_parallel_raises(self):
        with pytest.raises(ValueError, match="max_parallel"):
            PIRunner(max_parallel=0)
