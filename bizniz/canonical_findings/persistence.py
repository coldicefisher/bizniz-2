"""JSON persistence for ``CanonicalReport`` so it survives process
restart + resume."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from bizniz.canonical_findings.types import CanonicalReport


def save_canonical_report(
    report: CanonicalReport, path: Path,
) -> None:
    """Write the report as pretty-printed JSON. Creates parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load_canonical_report(path: Path) -> Optional[CanonicalReport]:
    """Load a canonical report from disk. Returns None if the file
    doesn't exist or doesn't parse cleanly."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return CanonicalReport.model_validate(data)
    except Exception:
        return None
