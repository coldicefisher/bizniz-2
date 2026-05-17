"""Tests for the extraction planner (Phase E)."""
from __future__ import annotations

from pathlib import Path
from typing import List

import pytest

from bizniz.refactorer.cpd import (
    CPDConfig, CPDReport, DuplicateBlock, ShingleOccurrence,
)
from bizniz.refactorer.extraction_planner import (
    ExtractionPlanReport,
    _is_excluded,
    _language_for_path,
    _service_for_path,
    plan_extractions,
)


def _dup(
    hash_: str = "deadbeef",
    paths: List[str] = None,
    token_count: int = 50,
) -> DuplicateBlock:
    paths = paths or ["/proj/svc_a/x.py", "/proj/svc_b/x.py"]
    occs = [
        ShingleOccurrence(
            path=p, start_token_idx=0,
            line_start=10, line_end=20,
        )
        for p in paths
    ]
    return DuplicateBlock(
        shingle_hash=hash_,
        token_count=token_count,
        occurrences=occs,
        files_count=len({p for p in paths}),
        total_instances=len(paths),
    )


def _cpd_report(*dups: DuplicateBlock) -> CPDReport:
    return CPDReport(
        config=CPDConfig(),
        duplicates=list(dups),
    )


# ── Path helpers ─────────────────────────────────────────────────


class TestServiceForPath:
    def test_first_dir_segment_under_root(self):
        root = Path("/proj")
        assert _service_for_path("/proj/backend/foo.py", root) == "backend"
        assert _service_for_path("/proj/frontend/bar.tsx", root) == "frontend"

    def test_skips_excluded_segments(self):
        root = Path("/proj")
        assert _service_for_path("/proj/__pycache__/x.py", root) == "unknown"

    def test_core_recognized(self):
        root = Path("/proj")
        assert _service_for_path("/proj/core/python/x.py", root) == "core"

    def test_without_root(self):
        # Without project_root, treat the path as absolute and pick
        # the first non-excluded segment.
        assert _service_for_path("/proj/backend/foo.py") in ("proj", "backend")


class TestIsExcluded:
    @pytest.mark.parametrize("path", [
        "/proj/.pkgs/cffi/x.py",
        "/proj/node_modules/lib/x.js",
        "/proj/dist/x.js",
        "/proj/tests/conftest.py",
        "/proj/backend/__pycache__/x.py",
    ])
    def test_excluded(self, path):
        assert _is_excluded(path) is True

    @pytest.mark.parametrize("path", [
        "/proj/backend/app/main.py",
        "/proj/frontend/src/index.tsx",
        "/proj/core/python/data_types/time_instant.py",
    ])
    def test_not_excluded(self, path):
        assert _is_excluded(path) is False


class TestLanguageForPath:
    def test_python(self):
        assert _language_for_path("/x/foo.py") == "python"

    def test_typescript(self):
        for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
            assert _language_for_path(f"/x/foo{ext}") == "typescript"

    def test_unknown(self):
        assert _language_for_path("/x/foo.rb") == "unknown"


# ── Planner ──────────────────────────────────────────────────────


class TestPlanExtractions:
    def test_cross_service_python_extract(self):
        dup = _dup(paths=["/proj/svc_a/foo.py", "/proj/svc_b/foo.py"])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        plans = report.extract_plans()
        assert len(plans) == 1
        p = plans[0]
        assert p.language == "python"
        assert set(p.services_involved) == {"svc_a", "svc_b"}
        assert p.disposition == "extract"

    def test_within_service_marked_manual_review(self):
        dup = _dup(paths=["/proj/svc_a/foo.py", "/proj/svc_a/bar.py"])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        manual = report.manual_review_plans()
        assert len(manual) == 1
        assert any(
            "within one service" in n for n in manual[0].notes
        )

    def test_excluded_paths_filtered(self):
        # Both copies in .pkgs/ — fully skipped.
        dup = _dup(paths=[
            "/proj/.pkgs/cffi/x.py", "/proj/.pkgs/werkzeug/y.py",
        ])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        assert report.plans == []
        assert report.skipped_duplicates_count == 1

    def test_partial_excluded_still_passes_with_remaining(self):
        # One copy in vendored, two in real services.
        dup = _dup(paths=[
            "/proj/.pkgs/cffi/x.py",
            "/proj/svc_a/foo.py",
            "/proj/svc_b/foo.py",
        ])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        plans = report.extract_plans()
        assert len(plans) == 1
        # Excluded file dropped from source_files.
        assert "/proj/.pkgs/cffi/x.py" not in plans[0].source_files

    def test_test_paths_excluded(self):
        dup = _dup(paths=[
            "/proj/svc_a/tests/conftest.py",
            "/proj/svc_b/tests/conftest.py",
        ])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        # tests/ is excluded → block fully filtered.
        assert report.plans == []

    def test_mixed_language_skipped(self):
        # CPD wouldn't normally produce this but the planner is
        # defensive — a duplicate spanning .py and .ts is skipped.
        dup = _dup(paths=[
            "/proj/svc_a/foo.py", "/proj/svc_b/foo.ts",
        ])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        assert report.plans == []

    def test_risk_score_increases_with_file_count(self):
        small = _dup("a", paths=[
            "/proj/svc_a/x.py", "/proj/svc_b/x.py",
        ])
        big = _dup("b", paths=[
            f"/proj/svc{i}/x.py" for i in range(10)
        ])
        report = plan_extractions(
            _cpd_report(small, big), project_root=Path("/proj"),
        )
        small_plan = next(p for p in report.plans if p.duplicate_hash == "a")
        big_plan = next(p for p in report.plans if p.duplicate_hash == "b")
        assert big_plan.risk_score > small_plan.risk_score

    def test_high_risk_flipped_to_manual_review(self):
        # Token count > 200 + many files → risk above threshold.
        dup = _dup(
            paths=[f"/proj/svc{i}/x.py" for i in range(12)],
            token_count=300,
        )
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        plan = report.plans[0]
        assert plan.disposition == "manual_review"
        assert any("Risk score" in n for n in plan.notes)

    def test_suggested_core_path_business_words(self):
        dup = _dup(paths=[
            "/proj/svc_a/company.py", "/proj/svc_b/company.py",
        ])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        plan = report.plans[0]
        assert "company" in plan.suggested_core_path.lower()
        assert plan.suggested_core_path.startswith("business/")

    def test_suggested_core_path_dto_words(self):
        dup = _dup(paths=[
            "/proj/svc_a/user_schema.py",
            "/proj/svc_b/user_schema.py",
        ])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        plan = report.plans[0]
        assert plan.suggested_core_path.startswith("dtos/")

    def test_typescript_language_detected(self):
        dup = _dup(paths=[
            "/proj/svc_a/util.ts", "/proj/svc_b/util.ts",
        ])
        report = plan_extractions(_cpd_report(dup), project_root=Path("/proj"))
        plan = report.plans[0]
        assert plan.language == "typescript"
        assert plan.suggested_core_path.endswith(".ts")

    def test_sort_by_disposition_then_risk(self):
        # 3 plans: 1 low-risk extract, 1 high-risk manual_review,
        # 1 medium-risk extract. Expected order: low risk extract,
        # medium risk extract, high risk manual_review.
        low = _dup("low", paths=[
            "/proj/svc_a/x.py", "/proj/svc_b/x.py",
        ])
        med = _dup("med", paths=[
            f"/proj/svc{i}/x.py" for i in range(6)
        ])
        high = _dup(
            "high",
            paths=[f"/proj/svc{i}/x.py" for i in range(12)],
            token_count=400,
        )
        report = plan_extractions(
            _cpd_report(low, med, high), project_root=Path("/proj"),
        )
        ids = [p.duplicate_hash for p in report.plans]
        # Manual reviews land last.
        assert ids.index("high") > ids.index("low")

    def test_total_considered_counted(self):
        dups = [_dup(str(i), paths=[
            f"/proj/svc_a/x{i}.py", f"/proj/svc_b/x{i}.py",
        ]) for i in range(5)]
        report = plan_extractions(_cpd_report(*dups), project_root=Path("/proj"))
        assert report.total_duplicates_considered == 5
