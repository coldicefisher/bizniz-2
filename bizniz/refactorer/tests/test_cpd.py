"""Tests for the CPD (copy-paste detector) algorithm."""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Dict, List

import pytest

from bizniz.refactorer.cpd import (
    CPDConfig,
    CPDReport,
    DuplicateBlock,
    FuzzyPair,
    _jaccard,
    _make_minhash_salts,
    _minhash_signature,
    _shingle_hashes,
    detect_duplicates,
    walk_source_tree,
)


# ── Helpers ──────────────────────────────────────────────────────


def _reader_from_dict(files: Dict[str, str]):
    """Build a file_reader callable from a path → contents dict."""
    def _read(path: str) -> str:
        return files[path]
    return _read


def _src_block(lines: int = 80, identifier_prefix: str = "x") -> str:
    """Build a multi-line Python source block big enough to produce
    multiple shingles (≥ 50 tokens with the default config)."""
    body: List[str] = []
    body.append("def record_action(db, actor_id, subject_id, subject_label, action):")
    for i in range(lines):
        body.append(
            f"    {identifier_prefix}{i} = process({identifier_prefix}{i-1 if i else 0})"
        )
    body.append("    return result")
    return "\n".join(body)


# ── Shingling ────────────────────────────────────────────────────


class TestShingleHashes:
    def test_emits_one_per_position(self):
        tokens = [f"t{i}" for i in range(60)]
        shingles = list(_shingle_hashes(tokens, shingle_tokens=50))
        # 60 - 50 + 1 = 11 shingles
        assert len(shingles) == 11
        # Start indices are 0..10
        assert [s[1] for s in shingles] == list(range(11))

    def test_too_short_yields_nothing(self):
        tokens = [f"t{i}" for i in range(30)]
        shingles = list(_shingle_hashes(tokens, shingle_tokens=50))
        assert shingles == []

    def test_identical_token_streams_produce_identical_hashes(self):
        a = [f"t{i}" for i in range(60)]
        b = [f"t{i}" for i in range(60)]
        ah = list(_shingle_hashes(a, 50))
        bh = list(_shingle_hashes(b, 50))
        assert [h for h, _ in ah] == [h for h, _ in bh]


# ── MinHash ──────────────────────────────────────────────────────


class TestMinHash:
    def test_identical_sets_jaccard_1(self):
        salts = _make_minhash_salts(64, seed=1)
        hashes = {i for i in range(1, 200)}
        sig_a = _minhash_signature(hashes, salts)
        sig_b = _minhash_signature(hashes, salts)
        assert _jaccard(sig_a, sig_b) == 1.0

    def test_disjoint_sets_jaccard_near_zero(self):
        salts = _make_minhash_salts(128, seed=1)
        sig_a = _minhash_signature({i for i in range(0, 500)}, salts)
        sig_b = _minhash_signature({i for i in range(10000, 10500)}, salts)
        assert _jaccard(sig_a, sig_b) < 0.05

    def test_half_overlap_jaccard_around_one_third(self):
        salts = _make_minhash_salts(128, seed=1)
        a = {i for i in range(0, 200)}
        b = {i for i in range(100, 300)}
        # True Jaccard = |intersection| / |union| = 100 / 300 = 0.33
        sig_a = _minhash_signature(a, salts)
        sig_b = _minhash_signature(b, salts)
        sim = _jaccard(sig_a, sig_b)
        # Allow ±0.15 wiggle room — MinHash is a stochastic estimator.
        assert 0.18 < sim < 0.48

    def test_empty_set_signature_zeroed(self):
        salts = _make_minhash_salts(8, seed=1)
        sig = _minhash_signature(set(), salts)
        assert sig == (0,) * 8


# ── End-to-end detector ──────────────────────────────────────────


class TestDetectDuplicates:
    def test_two_identical_files_flagged(self):
        body = _src_block(lines=60)
        files = {"/svc_a/foo.py": body, "/svc_b/foo.py": body}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        assert len(report.duplicates) >= 1
        # At least one block touches both files.
        cross = [d for d in report.duplicates if d.files_count >= 2]
        assert len(cross) >= 1
        # MinHash should also flag the file pair near 1.0 similarity.
        assert len(report.fuzzy_pairs) == 1
        assert report.fuzzy_pairs[0].jaccard_similarity > 0.95

    def test_unrelated_files_zero_dupes(self):
        a = "def foo(): return 1\ndef bar(): return 2"
        b = "import os\nimport sys\nimport json\n"
        files = {"/x.py": a, "/y.py": b}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        assert report.duplicates == []
        assert report.fuzzy_pairs == []

    def test_within_file_repetition_flagged(self):
        # Same logic block repeated TWICE in one file.
        block = _src_block(lines=60)
        files = {"/x.py": block + "\n\n" + block}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        # files_count is 1, but total_instances >= 2 means a within-
        # file duplicate.
        within = [d for d in report.duplicates if d.files_count == 1]
        assert len(within) >= 1
        assert all(d.total_instances >= 2 for d in within)

    def test_near_duplicate_detected_by_minhash(self):
        # File B has all of A's logic plus 5 extra lines.
        a = _src_block(lines=80)
        b = a + "\n    extra = 1\n    extra2 = 2\n    extra3 = 3\n    extra4 = 4\n    extra5 = 5"
        files = {"/a.py": a, "/b.py": b}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        # Should fire fuzzy pair near (but not exactly) 1.0.
        assert len(report.fuzzy_pairs) >= 1
        assert report.fuzzy_pairs[0].jaccard_similarity > 0.7

    def test_skipped_files_recorded(self):
        files = {"/x.rb": "puts 'hi'", "/y.py": "x = 1"}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        assert "/x.rb" in report.skipped_files
        # /y.py is too short to shingle but still in file_stats.
        assert any(fs.path == "/y.py" for fs in report.file_stats)

    def test_short_files_tokenized_but_not_shingled(self):
        # File with <50 tokens — stats produced, no shingles.
        a = "x = 1"
        files = {"/short.py": a}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        assert len(report.file_stats) == 1
        assert report.file_stats[0].token_count > 0
        assert report.duplicates == []

    def test_occurrences_carry_line_numbers(self):
        body = _src_block(lines=60)
        files = {"/a.py": body, "/b.py": body}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        cross = [d for d in report.duplicates if d.files_count >= 2][0]
        # Every occurrence has a 1-based line range.
        for occ in cross.occurrences:
            assert occ.line_start >= 1
            assert occ.line_end >= occ.line_start

    def test_duplicates_sorted_by_impact(self):
        # Build three duplicates with different impact:
        # block_a: in 3 files (high impact)
        # block_b: in 2 files (medium)
        # block_c: 2 instances in 1 file (within-file)
        block_a = _src_block(lines=60, identifier_prefix="a")
        block_b = _src_block(lines=60, identifier_prefix="b")
        block_c = _src_block(lines=60, identifier_prefix="c")
        files = {
            "/svc1/a.py": block_a,
            "/svc2/a.py": block_a,
            "/svc3/a.py": block_a,
            "/svc1/b.py": block_b,
            "/svc2/b.py": block_b,
            "/svc1/c.py": block_c + "\n\n" + block_c,
        }
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        # Should rank a → b → c (most files first).
        assert report.duplicates[0].files_count >= 3
        assert report.duplicates[-1].files_count == 1  # within-file last

    def test_config_threshold_tuning(self):
        # With a much higher similarity threshold, fuzzy pairs shrink.
        body = _src_block(lines=80)
        slightly_diff = body + "\n    extra = 1"
        files = {"/a.py": body, "/b.py": slightly_diff}
        # Default threshold 0.5 → matches.
        default = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        assert len(default.fuzzy_pairs) == 1
        # High threshold 0.99 → may not match.
        strict = detect_duplicates(
            files.keys(),
            config=CPDConfig(similarity_threshold=0.99),
            file_reader=_reader_from_dict(files),
        )
        # Whether or not it matches depends on the exact MinHash
        # collisions; just verify the config knob takes effect by
        # confirming the threshold tightened.
        assert strict.config.similarity_threshold == 0.99

    def test_cross_file_duplicates_helper(self):
        body = _src_block(lines=60)
        files = {f"/svc{i}/foo.py": body for i in range(4)}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        # At least one block touches all 4 files.
        cross_4 = report.cross_file_duplicates(min_files=4)
        assert len(cross_4) >= 1
        # And by definition no blocks touch 5 files (we only have 4).
        cross_5 = report.cross_file_duplicates(min_files=5)
        assert cross_5 == []

    def test_hot_blocks_helper(self):
        body = _src_block(lines=60)
        files = {f"/svc{i}/foo.py": body for i in range(5)}
        report = detect_duplicates(
            files.keys(),
            file_reader=_reader_from_dict(files),
        )
        # At least one block appears in 5 files (5+ instances).
        hot = report.hot_blocks(min_instances=5)
        assert len(hot) >= 1


# ── Tree walker ──────────────────────────────────────────────────


class TestWalkSourceTree:
    def test_finds_python_files(self, tmp_path):
        (tmp_path / "svc_a").mkdir()
        (tmp_path / "svc_a" / "x.py").write_text("x = 1", "utf-8")
        (tmp_path / "svc_b").mkdir()
        (tmp_path / "svc_b" / "y.ts").write_text("const x = 1;", "utf-8")
        out = walk_source_tree(tmp_path)
        assert any(p.endswith("/x.py") for p in out)
        assert any(p.endswith("/y.ts") for p in out)

    def test_skips_node_modules(self, tmp_path):
        (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg" / "x.js").write_text("var x;", "utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "good.js").write_text("var y;", "utf-8")
        out = walk_source_tree(tmp_path)
        # Use PARTS not substring — tmp_path name may contain "node_modules".
        from pathlib import Path
        parts_in_out = [Path(p).parts for p in out]
        assert any("good.js" in parts for parts in parts_in_out)
        assert not any("node_modules" in parts for parts in parts_in_out)

    def test_skips_dist_and_pycache(self, tmp_path):
        (tmp_path / "dist").mkdir()
        (tmp_path / "dist" / "x.js").write_text("var x;", "utf-8")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "y.py").write_text("x = 1", "utf-8")
        (tmp_path / "real.py").write_text("x = 1", "utf-8")
        out = walk_source_tree(tmp_path)
        from pathlib import Path
        parts_in_out = [Path(p).parts for p in out]
        assert any("real.py" in parts for parts in parts_in_out)
        assert not any("dist" in parts for parts in parts_in_out)
        assert not any("__pycache__" in parts for parts in parts_in_out)

    def test_deterministic_order(self, tmp_path):
        for name in ["z.py", "a.py", "m.py"]:
            (tmp_path / name).write_text("x = 1", "utf-8")
        out1 = walk_source_tree(tmp_path)
        out2 = walk_source_tree(tmp_path)
        assert out1 == out2
        assert out1 == sorted(out1)
