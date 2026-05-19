"""Domain models for the synthetic batch-fix test.

Intentional defects (each surfaces as a finding):
  - Recipe is missing the ``tags`` field that routes.py + tests
    expect. (1× mypy + 1× pytest + 1× QE)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Recipe:
    id: str
    title: str
    owner_id: str
    # BUG: ``tags`` field missing. routes.py + test_routes.py reference it.
