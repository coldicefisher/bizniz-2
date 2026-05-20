"""Tests for canonical findings types + fingerprint + persistence."""
from __future__ import annotations

import json

import pytest

from bizniz.canonical_findings import (
    CanonicalFinding,
    CanonicalReport,
    FindingResolution,
    ResolutionReport,
    canonical_fingerprint,
    load_canonical_report,
    save_canonical_report,
)


# ── Fingerprint ────────────────────────────────────────────────────


class TestFingerprint:
    def test_same_shape_same_fingerprint(self):
        fp1 = canonical_fingerprint(
            source="quality_engineer",
            capability_id="local_user_sync",
            shape={"scenario": "first_login", "priority": "important"},
        )
        fp2 = canonical_fingerprint(
            source="quality_engineer",
            capability_id="local_user_sync",
            shape={"scenario": "first_login", "priority": "important"},
        )
        assert fp1 == fp2

    def test_dict_key_order_doesnt_matter(self):
        fp1 = canonical_fingerprint(
            source="x", capability_id="y",
            shape={"a": 1, "b": 2},
        )
        fp2 = canonical_fingerprint(
            source="x", capability_id="y",
            shape={"b": 2, "a": 1},
        )
        assert fp1 == fp2

    def test_different_source_different_fingerprint(self):
        fp1 = canonical_fingerprint(
            source="quality_engineer", capability_id="c", shape={"x": 1},
        )
        fp2 = canonical_fingerprint(
            source="code_reviewer", capability_id="c", shape={"x": 1},
        )
        assert fp1 != fp2

    def test_format_is_source_capability_hash(self):
        fp = canonical_fingerprint(
            source="quality_engineer",
            capability_id="local_user_sync",
            shape={"scenario": "first_login"},
        )
        parts = fp.split(":")
        assert len(parts) == 3
        assert parts[0] == "quality_engineer"
        assert parts[1] == "local_user_sync"
        assert len(parts[2]) == 8  # short hash

    def test_no_capability_id_becomes_none(self):
        fp = canonical_fingerprint(
            source="static_ast", shape={"file": "x.py", "line": 5},
        )
        assert fp.startswith("static_ast:none:")


# ── CanonicalFinding helpers ──────────────────────────────────────


class TestFindingBlocker:
    def test_critical_unresolved_is_blocker(self):
        f = CanonicalFinding(
            id="x", source="quality_engineer",
            priority="critical", summary="s",
        )
        assert f.is_blocker() is True

    def test_resolved_is_not_blocker(self):
        f = CanonicalFinding(
            id="x", source="quality_engineer",
            priority="critical", summary="s", status="resolved",
        )
        assert f.is_blocker() is False

    def test_wont_fix_is_not_blocker(self):
        f = CanonicalFinding(
            id="x", source="quality_engineer",
            priority="critical", summary="s", status="wont_fix",
        )
        assert f.is_blocker() is False

    def test_nice_to_have_is_not_blocker(self):
        f = CanonicalFinding(
            id="x", source="quality_engineer",
            priority="nice_to_have", summary="s",
        )
        assert f.is_blocker() is False

    def test_regressed_critical_is_blocker(self):
        f = CanonicalFinding(
            id="x", source="quality_engineer",
            priority="critical", summary="s", status="regressed",
        )
        assert f.is_blocker() is True


# ── CanonicalReport queries ───────────────────────────────────────


def _make_report() -> CanonicalReport:
    return CanonicalReport(
        milestone_name="M1",
        findings=[
            CanonicalFinding(
                id="f1", source="quality_engineer", priority="critical",
                summary="missing login test", status="initial",
            ),
            CanonicalFinding(
                id="f2", source="code_reviewer", priority="critical",
                summary="hallucinated import", status="resolved",
            ),
            CanonicalFinding(
                id="f3", source="quality_engineer", priority="important",
                summary="missing edge case", status="still_present",
            ),
            CanonicalFinding(
                id="f4", source="quality_engineer", priority="nice_to_have",
                summary="optional",
            ),
        ],
    )


class TestReportQueries:
    def test_by_id_indexes_all(self):
        r = _make_report()
        assert set(r.by_id().keys()) == {"f1", "f2", "f3", "f4"}

    def test_unresolved_excludes_resolved_and_wontfix(self):
        r = _make_report()
        ids = [f.id for f in r.unresolved()]
        assert "f1" in ids and "f3" in ids and "f4" in ids
        assert "f2" not in ids  # resolved

    def test_blockers_only_critical_and_important_unresolved(self):
        r = _make_report()
        ids = [f.id for f in r.blockers()]
        assert ids == ["f1", "f3"]  # f4 is nice_to_have; f2 resolved

    def test_all_blockers_resolved_initially_false(self):
        r = _make_report()
        assert r.all_blockers_resolved() is False

    def test_all_blockers_resolved_when_all_critical_done(self):
        r = _make_report()
        for f in r.findings:
            if f.is_blocker():
                f.status = "resolved"
        assert r.all_blockers_resolved() is True


# ── Resolution application ────────────────────────────────────────


class TestApplyResolution:
    def test_initial_to_resolved_records_progress(self):
        r = _make_report()
        res = ResolutionReport(
            milestone_name="M1", iter_idx=2,
            resolutions=[
                FindingResolution(finding_id="f1", status="resolved"),
            ],
        )
        delta = r.apply_resolution(res, iter_idx=2)
        assert delta.newly_resolved_ids == ["f1"]
        assert delta.is_progress is True

    def test_resolved_to_still_present_is_regression(self):
        r = _make_report()
        # f2 was resolved; now resolution check says still_present.
        res = ResolutionReport(
            milestone_name="M1", iter_idx=3,
            resolutions=[
                FindingResolution(finding_id="f2", status="still_present"),
            ],
        )
        delta = r.apply_resolution(res, iter_idx=3)
        assert delta.regressed_ids == ["f2"]
        assert delta.is_regression is True
        # Status got flipped to "regressed".
        assert r.by_id()["f2"].status == "regressed"

    def test_unknown_id_recorded_but_no_invent(self):
        r = _make_report()
        res = ResolutionReport(
            milestone_name="M1", iter_idx=2,
            resolutions=[
                FindingResolution(finding_id="NEW-ID", status="still_present"),
            ],
        )
        delta = r.apply_resolution(res, iter_idx=2)
        assert delta.unknown_ids == ["NEW-ID"]
        # No new findings added — invariant.
        assert "NEW-ID" not in r.by_id()

    def test_no_change_status_skipped(self):
        r = _make_report()
        # f1 is initial; resolution check returns initial again.
        res = ResolutionReport(
            milestone_name="M1", iter_idx=2,
            resolutions=[
                FindingResolution(finding_id="f1", status="initial"),
            ],
        )
        delta = r.apply_resolution(res, iter_idx=2)
        assert delta.newly_resolved_ids == []
        assert delta.status_changed_ids == []


# ── Persistence ────────────────────────────────────────────────────


class TestPersistence:
    def test_round_trip(self, tmp_path):
        r = _make_report()
        path = tmp_path / "canonical.json"
        save_canonical_report(r, path)
        loaded = load_canonical_report(path)
        assert loaded is not None
        assert loaded.milestone_name == r.milestone_name
        assert len(loaded.findings) == len(r.findings)
        assert loaded.by_id()["f1"].summary == "missing login test"

    def test_load_missing_returns_none(self, tmp_path):
        assert load_canonical_report(tmp_path / "no.json") is None

    def test_load_corrupt_returns_none(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json at all{{{")
        assert load_canonical_report(path) is None

    def test_save_creates_parent_dirs(self, tmp_path):
        r = _make_report()
        path = tmp_path / "deep" / "nested" / "canonical.json"
        save_canonical_report(r, path)
        assert path.exists()
        # And loads back cleanly.
        loaded = load_canonical_report(path)
        assert loaded is not None
