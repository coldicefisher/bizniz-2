"""Copy-paste detector for the Refactorer agent.

Ported from MUSE's ``cpd-analysis.py`` (v3, battle-tested on a real
Perl/Python codebase). Generalized to support multiple languages
via ``tokenizers.py``.

Two detection modes:

1. **Exact shingle matching** — identical 50-token sequences
   appearing in 2+ files (or 2+ positions in the same file) are
   flagged. Catches verbatim duplicates of business logic across
   services.
2. **MinHash file-pair similarity** — every file gets a 128-band
   MinHash signature; pairs with Jaccard similarity ≥ 0.5 are
   surfaced. Catches near-duplicates where 3-5 lines were edited
   (the case exact shingling misses).

The output (``CPDReport``) is the input to the Refactorer's
extraction planner. Each ``DuplicateBlock`` lists the files +
line ranges where one normalized token sequence appears more
than once — the planner decides what to extract to ``core/``.
"""
from __future__ import annotations

import collections
import hashlib
import random
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from bizniz.refactorer.tokenizers import (
    detect_language, tokenize_file, tokenize_text,
)


# Algorithm tuning. Defaults match the MUSE original; surface as
# CPDConfig so the Refactorer agent can tune them per project size.

DEFAULT_SHINGLE_TOKENS = 50          # ~10 lines of code
DEFAULT_MINHASH_BANDS = 128
DEFAULT_MINHASH_SEED = 42
DEFAULT_SIMILARITY_THRESHOLD = 0.5   # report file pairs at or above this


# ── Output schema ────────────────────────────────────────────────


class FileTokenStats(BaseModel):
    """Per-file token counts emitted from the tokenizer pass."""
    path: str
    total_lines: int = 0
    code_lines: int = 0
    token_count: int = 0


class ShingleOccurrence(BaseModel):
    """One occurrence of a shingle in a file."""
    path: str
    start_token_idx: int
    line_start: int
    line_end: int


class DuplicateBlock(BaseModel):
    """A normalized token sequence that appears 2+ times across the
    analyzed file set (cross-file OR within-file)."""
    shingle_hash: str    # hex of the hash value
    token_count: int     # always SHINGLE_TOKENS unless config tuned
    occurrences: List[ShingleOccurrence] = Field(default_factory=list)
    files_count: int = 0    # distinct files this shingle appears in
    total_instances: int = 0  # sum across all positions in all files


class FuzzyPair(BaseModel):
    """Two files whose MinHash signatures suggest high similarity
    (not necessarily exact matches — small edits OK)."""
    file_a: str
    file_b: str
    jaccard_similarity: float


class CPDReport(BaseModel):
    """End-to-end output of a CPD run."""
    config: "CPDConfig"
    file_stats: List[FileTokenStats] = Field(default_factory=list)
    duplicates: List[DuplicateBlock] = Field(default_factory=list)
    fuzzy_pairs: List[FuzzyPair] = Field(default_factory=list)
    total_tokens: int = 0
    total_code_lines: int = 0
    skipped_files: List[str] = Field(default_factory=list)

    def cross_file_duplicates(self, min_files: int = 2) -> List[DuplicateBlock]:
        """Shingles appearing in at least ``min_files`` distinct files."""
        return [d for d in self.duplicates if d.files_count >= min_files]

    def hot_blocks(
        self, min_instances: int = 3,
    ) -> List[DuplicateBlock]:
        """Shingles appearing N+ times anywhere (cross + within)."""
        return [d for d in self.duplicates if d.total_instances >= min_instances]


class CPDConfig(BaseModel):
    """Tunable algorithm parameters."""
    shingle_tokens: int = DEFAULT_SHINGLE_TOKENS
    minhash_bands: int = DEFAULT_MINHASH_BANDS
    minhash_seed: int = DEFAULT_MINHASH_SEED
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD


# Pydantic forward-ref resolution.
CPDReport.model_rebuild()


# ── Shingling ────────────────────────────────────────────────────


def _shingle_hashes(
    tokens: List[str], shingle_tokens: int,
) -> Iterable[Tuple[int, int]]:
    """Yield ``(hash, start_idx)`` for every shingle of length
    ``shingle_tokens`` in the token stream."""
    if len(tokens) < shingle_tokens:
        return
    for i in range(len(tokens) - shingle_tokens + 1):
        s = " ".join(tokens[i:i + shingle_tokens])
        h = int(hashlib.md5(s.encode()).hexdigest()[:16], 16)
        yield h, i


# ── MinHash ──────────────────────────────────────────────────────


def _make_minhash_salts(bands: int, seed: int) -> List[int]:
    r = random.Random(seed)
    return [r.getrandbits(64) for _ in range(bands)]


def _minhash_signature(
    shingle_hashes: Set[int], salts: List[int],
) -> Tuple[int, ...]:
    """Compute a MinHash signature over ``shingle_hashes`` using
    permutation-by-xor as a cheap independent-hash approximation."""
    if not shingle_hashes:
        return tuple(0 for _ in salts)
    sig: List[int] = []
    for salt in salts:
        sig.append(min(h ^ salt for h in shingle_hashes))
    return tuple(sig)


def _jaccard(
    sig_a: Tuple[int, ...], sig_b: Tuple[int, ...],
) -> float:
    if not sig_a:
        return 0.0
    eq = sum(1 for a, b in zip(sig_a, sig_b) if a == b)
    return eq / len(sig_a)


# ── Public detector ──────────────────────────────────────────────


def detect_duplicates(
    file_paths: Iterable[str],
    language: Optional[str] = None,
    config: Optional[CPDConfig] = None,
    file_reader: Optional[Callable[[str], str]] = None,
) -> CPDReport:
    """Run the CPD algorithm over ``file_paths``.

    ``language`` defaults to auto-detection per file (mixed-language
    runs are fine). ``config`` is the tunable params; defaults match
    the MUSE original. ``file_reader`` is injectable for tests —
    bypass disk I/O by passing a callable that maps path → text.

    Files shorter than ``shingle_tokens`` are tokenized for stats
    but skipped from shingle / minhash. Files with unknown language
    extensions land in ``skipped_files``.
    """
    cfg = config or CPDConfig()
    salts = _make_minhash_salts(cfg.minhash_bands, cfg.minhash_seed)

    file_stats: Dict[str, FileTokenStats] = {}
    per_file_tokens: Dict[str, List[str]] = {}
    per_file_token_lines: Dict[str, List[int]] = {}
    per_file_shingles: Dict[str, List[Tuple[int, int]]] = {}

    # Cross-cutting: which paths each hash appears in + total
    # instance count across all files.
    cross_files: Dict[int, Set[str]] = collections.defaultdict(set)
    total_instances: collections.Counter = collections.Counter()

    skipped: List[str] = []
    total_tokens = 0
    total_code_lines = 0

    for path in file_paths:
        lang = language or detect_language(path)
        if lang is None:
            skipped.append(path)
            continue
        if file_reader is not None:
            text = file_reader(path)
            toks, token_lines, total_lns, code_lns = tokenize_text(text, lang)
        else:
            try:
                toks, token_lines, total_lns, code_lns = tokenize_file(path, lang)
            except OSError:
                skipped.append(path)
                continue

        file_stats[path] = FileTokenStats(
            path=path,
            total_lines=total_lns,
            code_lines=code_lns,
            token_count=len(toks),
        )
        per_file_tokens[path] = toks
        per_file_token_lines[path] = token_lines
        total_tokens += len(toks)
        total_code_lines += code_lns

        if len(toks) < cfg.shingle_tokens:
            continue

        sh = list(_shingle_hashes(toks, cfg.shingle_tokens))
        per_file_shingles[path] = sh
        for h, _ in sh:
            cross_files[h].add(path)
            total_instances[h] += 1

    # ── Duplicate blocks (cross-file or within-file) ─────────────
    duplicates: List[DuplicateBlock] = []
    for h, paths in cross_files.items():
        instance_count = total_instances[h]
        if len(paths) < 2 and instance_count < 2:
            # Singleton — not a duplicate.
            continue
        occurrences: List[ShingleOccurrence] = []
        for path in paths:
            for hsh, start_idx in per_file_shingles.get(path, []):
                if hsh != h:
                    continue
                token_lines = per_file_token_lines[path]
                line_start = (
                    token_lines[start_idx]
                    if start_idx < len(token_lines) else 0
                )
                end_idx = min(
                    start_idx + cfg.shingle_tokens - 1,
                    len(token_lines) - 1,
                )
                line_end = (
                    token_lines[end_idx] if end_idx >= 0 else line_start
                )
                occurrences.append(ShingleOccurrence(
                    path=path,
                    start_token_idx=start_idx,
                    line_start=line_start,
                    line_end=line_end,
                ))
        duplicates.append(DuplicateBlock(
            shingle_hash=f"{h:x}",
            token_count=cfg.shingle_tokens,
            occurrences=occurrences,
            files_count=len(paths),
            total_instances=instance_count,
        ))
    # Sort by impact: most files first, then most instances.
    duplicates.sort(
        key=lambda d: (-d.files_count, -d.total_instances),
    )

    # ── MinHash pairwise similarity ──────────────────────────────
    file_sigs: Dict[str, Tuple[int, ...]] = {}
    for path, sh in per_file_shingles.items():
        unique_hashes = {h for h, _ in sh}
        file_sigs[path] = _minhash_signature(unique_hashes, salts)

    fuzzy_pairs: List[FuzzyPair] = []
    paths_sorted = sorted(file_sigs)
    for i in range(len(paths_sorted)):
        for j in range(i + 1, len(paths_sorted)):
            p1, p2 = paths_sorted[i], paths_sorted[j]
            sim = _jaccard(file_sigs[p1], file_sigs[p2])
            if sim >= cfg.similarity_threshold:
                fuzzy_pairs.append(FuzzyPair(
                    file_a=p1, file_b=p2, jaccard_similarity=sim,
                ))
    fuzzy_pairs.sort(key=lambda fp: -fp.jaccard_similarity)

    return CPDReport(
        config=cfg,
        file_stats=sorted(file_stats.values(), key=lambda fs: fs.path),
        duplicates=duplicates,
        fuzzy_pairs=fuzzy_pairs,
        total_tokens=total_tokens,
        total_code_lines=total_code_lines,
        skipped_files=skipped,
    )


# ── Helpers ──────────────────────────────────────────────────────


def walk_source_tree(
    root: Path,
    extensions: Iterable[str] = (".py", ".ts", ".tsx", ".js", ".jsx"),
    skip_dirs: Iterable[str] = (
        "node_modules", "dist", "build", ".bizniz",
        "__pycache__", ".venv", "venv", ".git",
    ),
) -> List[str]:
    """Walk a tree and return paths with the requested extensions,
    skipping hostile dirs. Deterministic alphabetical order so the
    CPD output is stable across runs."""
    root = Path(root).resolve()
    skip = set(skip_dirs)
    out: List[str] = []
    for ext in extensions:
        for p in sorted(root.rglob(f"*{ext}")):
            if any(seg in skip for seg in p.parts):
                continue
            if not p.is_file():
                continue
            out.append(str(p))
    return sorted(set(out))
