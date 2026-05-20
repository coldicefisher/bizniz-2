"""Stable fingerprint generator for canonical findings.

Same logical finding → same fingerprint across iters. The
ResolutionChecker can only reference findings by their fingerprint,
so stability is load-bearing.

Inputs:
  - ``source``: which reviewer produced it (quality_engineer, code_reviewer, etc.)
  - ``capability_id``: the EnrichedSpec capability the finding relates to
  - ``shape``: a source-specific tuple distinguishing this finding from
               others within the same source + capability
               (e.g., for QE missing scenarios: the scenario text;
                for CR flagged symbols: file + symbol + kind)

The output format is ``<source>:<capability_id_or_none>:<short_hash>``
where short_hash is the first 8 chars of a sha1 over the shape.
Short enough to be readable in logs; long enough to be unique within
a typical milestone (8 chars = 16^8 ≈ 4B possibilities; collisions
within ~100 findings are astronomically unlikely).
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional


def canonical_fingerprint(
    *,
    source: str,
    capability_id: Optional[str] = None,
    shape: Any,
) -> str:
    """Generate a stable fingerprint for a canonical finding.

    ``shape`` is hashed to produce the short_hash portion. Any
    JSON-serializable value works; we normalize to repr() so two
    callers passing the same logical content get the same hash even
    if they construct dicts in different orders.
    """
    cap = capability_id or "none"
    # repr() is order-stable for dicts in Python 3.7+ (insertion-ordered).
    # For maximum stability, sort dict keys before hashing.
    normalized = _normalize_for_hash(shape)
    h = hashlib.sha1(repr(normalized).encode("utf-8")).hexdigest()[:8]
    return f"{source}:{cap}:{h}"


def _normalize_for_hash(v: Any) -> Any:
    """Recursively normalize so dict key order doesn't affect the hash."""
    if isinstance(v, dict):
        return {k: _normalize_for_hash(v[k]) for k in sorted(v.keys())}
    if isinstance(v, (list, tuple)):
        return [_normalize_for_hash(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(_normalize_for_hash(x) for x in v)
    return v
