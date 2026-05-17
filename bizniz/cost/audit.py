"""Cost audit — reconcile our tracker estimates vs Google's actual billing.

Two data sources:

  1. Internal: per-call records in either
     - the project's sqlite DB (api_calls table), or
     - the run's costs.md file (parsed)

  2. External: Google's billing data, currently via CSV export from the
     Cloud Billing dashboard. (Future: BigQuery export or
     Cloud Billing API for automation.)

Compare → diff report by date and by model. The diff is what tells you
whether our pricing table is right and whether we missed cached-token
discounts.

CSV format expected (column names case-insensitive, common variants
accepted):

  date | usage start date | service description | sku | sku description |
  cost | total cost | quantity | tokens

We pick: a date column, a SKU/model identifier, and a cost column. The
parser is permissive about extras and column naming so a user can
download "Detailed daily costs" or "Daily totals" and we'll handle it.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# ── Internal: parse our tracker output ─────────────────────────────────


_COST_LINE_RE = re.compile(
    r"^\s+(\S+)\s+"                                     # agent
    r"(\S+)\s+"                                         # model
    r"in=\s*([\d,]+)\s+out=\s*([\d,]+)\s+"             # tokens
    r"\$([\d.]+)"                                       # cost
    r"(?:\s+cached_in=([\d,]+))?"                       # optional cached
)
_HEADER_RE = re.compile(
    r"^##\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+—\s+(.+?)\s*$"
)


@dataclass
class TrackerEntry:
    """One LLM-call record as scraped from costs.md (or sqlite)."""
    timestamp: datetime
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost: float
    phase: str = ""


def parse_costs_md(path: Path) -> List[TrackerEntry]:
    """Parse a per-run ``costs.md`` and return all per-call entries.

    Each ``## 2026-05-06 HH:MM:SS — phase=X`` block can carry a
    ``### Per-call breakdown`` fenced section with one line per LLM
    call. We pull those lines and tag them with the section's
    timestamp + phase label.
    """
    if not path.exists():
        return []
    text = path.read_text()
    entries: List[TrackerEntry] = []
    current_ts: Optional[datetime] = None
    current_phase: str = ""
    in_breakdown = False

    for line in text.splitlines():
        m_h = _HEADER_RE.match(line)
        if m_h:
            current_ts = datetime.fromisoformat(f"{m_h.group(1)}T{m_h.group(2)}")
            current_phase = m_h.group(3).strip()
            in_breakdown = False
            continue
        if line.strip().startswith("### Per-call breakdown"):
            in_breakdown = True
            continue
        if line.strip().startswith("---"):
            in_breakdown = False
            continue
        if not in_breakdown or current_ts is None:
            continue
        m_c = _COST_LINE_RE.match(line)
        if not m_c:
            continue
        entries.append(TrackerEntry(
            timestamp=current_ts,
            agent=m_c.group(1),
            model=m_c.group(2),
            input_tokens=int(m_c.group(3).replace(",", "")),
            output_tokens=int(m_c.group(4).replace(",", "")),
            cached_input_tokens=int((m_c.group(6) or "0").replace(",", "")),
            cost=float(m_c.group(5)),
            phase=current_phase,
        ))
    return entries


def parse_project_runs(project_root: Path) -> List[TrackerEntry]:
    """Scan ``<project>/<runs_root>/*/costs.md`` and aggregate all
    tracker entries across every recorded run.

    Honors the 2026-05-16 migration (item 8A): new runs live at
    ``.bizniz/runs/``; legacy ones at ``docs/runs/``.
    """
    from bizniz.driver.runs_paths import resolve_runs_root
    runs_root = resolve_runs_root(project_root)
    if not runs_root.exists():
        return []
    out: List[TrackerEntry] = []
    for job_dir in sorted(runs_root.iterdir()):
        out.extend(parse_costs_md(job_dir / "costs.md"))
    return out


# ── External: parse Google billing CSV ─────────────────────────────────


@dataclass
class GoogleBillingEntry:
    """One row from a Google Cloud Billing CSV export."""
    date: date
    sku: str
    cost: float


_DATE_COLS = (
    "usage start date", "date", "usage_start_date", "day", "usage start time",
)
_SKU_COLS = ("sku description", "sku", "service description", "service")
_COST_COLS = ("cost", "total cost", "subtotal", "amount", "total")


def _norm(s: str) -> str:
    return s.strip().lower().replace("_", " ")


def _pick_column(header: List[str], candidates: Tuple[str, ...]) -> Optional[int]:
    """Return the first column index whose normalized name matches any
    candidate. None if no match — caller decides whether to fall through
    or raise.
    """
    norm = {i: _norm(h) for i, h in enumerate(header)}
    for cand in candidates:
        for i, h in norm.items():
            if h == cand:
                return i
    # Substring match as fallback (e.g., "cost (usd)" matches "cost").
    for cand in candidates:
        for i, h in norm.items():
            if cand in h:
                return i
    return None


def parse_google_billing_csv(path: Path) -> List[GoogleBillingEntry]:
    """Parse a Google Cloud Billing CSV export.

    Tolerant of column naming — picks the first matching candidate
    from a list of common names. Skips rows that don't have all three
    of (date, sku, cost) parseable. Returns empty list for missing /
    unparseable file.
    """
    if not path.exists():
        return []
    out: List[GoogleBillingEntry] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return []
        date_idx = _pick_column(header, _DATE_COLS)
        sku_idx = _pick_column(header, _SKU_COLS)
        cost_idx = _pick_column(header, _COST_COLS)
        if date_idx is None or sku_idx is None or cost_idx is None:
            raise ValueError(
                f"could not find date/sku/cost columns in {path}. "
                f"Got header: {header}. Expected one of "
                f"date={_DATE_COLS}, sku={_SKU_COLS}, cost={_COST_COLS}."
            )
        for row in reader:
            if len(row) <= max(date_idx, sku_idx, cost_idx):
                continue
            try:
                d = _parse_date(row[date_idx])
                sku = row[sku_idx].strip()
                cost = float(row[cost_idx].replace(",", "").replace("$", ""))
            except Exception:
                continue
            if not sku or cost == 0:
                continue
            out.append(GoogleBillingEntry(date=d, sku=sku, cost=cost))
    return out


def _parse_date(s: str) -> date:
    """Parse a date in any of the formats Google's CSV emits."""
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Try ISO first 10 chars as last resort.
    try:
        return datetime.fromisoformat(s[:10]).date()
    except ValueError:
        raise ValueError(f"could not parse date: {s!r}")


# ── Compare + render ────────────────────────────────────────────────────


@dataclass
class AuditDiff:
    """Comparison output. Fields:

      - by_day: {date → (tracker_total, google_total)}
      - by_model: {model → (tracker_total, google_total)}
      - tracker_total / google_total: grand totals
    """
    by_day: Dict[date, Tuple[float, float]] = field(default_factory=dict)
    by_model: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    tracker_total: float = 0.0
    google_total: float = 0.0
    period_start: Optional[date] = None
    period_end: Optional[date] = None


def compare(
    tracker_entries: Iterable[TrackerEntry],
    google_entries: Iterable[GoogleBillingEntry],
    *,
    start: Optional[date] = None,
    end: Optional[date] = None,
    sku_to_model: Optional[Dict[str, str]] = None,
) -> AuditDiff:
    """Roll up tracker + google data and align by date and by
    model/SKU. ``start`` / ``end`` are inclusive filters on the date.

    ``sku_to_model``: optional explicit mapping from Google SKU
    descriptions to our internal model names. SKUs we don't recognize
    fall under "(unknown)" and only contribute to grand totals.
    """
    sku_map = sku_to_model or _DEFAULT_SKU_MAP

    diff = AuditDiff(period_start=start, period_end=end)

    tracker_days: Dict[date, float] = defaultdict(float)
    tracker_models: Dict[str, float] = defaultdict(float)
    for e in tracker_entries:
        d = e.timestamp.date()
        if start and d < start:
            continue
        if end and d > end:
            continue
        tracker_days[d] += e.cost
        tracker_models[e.model] += e.cost
        diff.tracker_total += e.cost

    google_days: Dict[date, float] = defaultdict(float)
    google_models: Dict[str, float] = defaultdict(float)
    for g in google_entries:
        if start and g.date < start:
            continue
        if end and g.date > end:
            continue
        model = _classify_sku(g.sku, sku_map)
        google_days[g.date] += g.cost
        google_models[model] += g.cost
        diff.google_total += g.cost

    all_days = set(tracker_days) | set(google_days)
    diff.by_day = {
        d: (tracker_days.get(d, 0.0), google_days.get(d, 0.0))
        for d in sorted(all_days)
    }
    all_models = set(tracker_models) | set(google_models)
    diff.by_model = {
        m: (tracker_models.get(m, 0.0), google_models.get(m, 0.0))
        for m in sorted(all_models)
    }
    return diff


# Default SKU → model map. SKU strings as Google's billing dashboard
# emits them. Add new entries when Google introduces SKU names we
# don't recognize.
_DEFAULT_SKU_MAP: Dict[str, str] = {
    "gemini 3 flash preview": "gemini-3-flash-preview",
    "gemini 3.1 flash lite preview": "gemini-3.1-flash-lite-preview",
    "gemini 3.1 pro preview": "gemini-3.1-pro-preview",
    "gemini 2.5 flash-lite": "gemini-2.5-flash-lite",
    "gemini 2.5 flash lite": "gemini-2.5-flash-lite",
    "gemini 2.5 flash": "gemini-2.5-flash",
    "gemini 2.5 pro": "gemini-2.5-pro",
}


def _classify_sku(sku: str, sku_map: Dict[str, str]) -> str:
    """Match a SKU string to a known model; '(unknown)' if no match."""
    norm = sku.lower()
    # Try exact-ish match (substring) on the longest-key-first basis
    # to avoid 'gemini 2.5 flash' eating 'gemini 2.5 flash-lite'.
    for key in sorted(sku_map, key=len, reverse=True):
        if key in norm:
            return sku_map[key]
    return f"(unknown: {sku[:60]})"


def render_diff(diff: AuditDiff) -> str:
    """Human-readable report. Markdown-flavored so it renders cleanly
    when written to docs/runs/<job>/audit.md or copy-pasted to a PR.
    """
    lines: List[str] = []
    period = ""
    if diff.period_start or diff.period_end:
        s = diff.period_start.isoformat() if diff.period_start else "earliest"
        e = diff.period_end.isoformat() if diff.period_end else "latest"
        period = f"  ({s} → {e})"
    lines.append(f"# Cost Audit{period}")
    lines.append("")
    grand_diff = diff.google_total - diff.tracker_total
    pct = (grand_diff / diff.tracker_total * 100.0) if diff.tracker_total else 0.0
    lines.append(
        f"**Tracker total:** ${diff.tracker_total:.4f}    "
        f"**Google total:** ${diff.google_total:.4f}    "
        f"**Diff:** ${grand_diff:+.4f}  ({pct:+.1f}%)"
    )
    lines.append("")

    lines.append("## By model")
    lines.append("")
    lines.append("| Model | Tracker | Google | Diff | Diff % |")
    lines.append("|---|---:|---:|---:|---:|")
    for model, (t, g) in diff.by_model.items():
        d = g - t
        p = (d / t * 100.0) if t else 0.0
        lines.append(
            f"| `{model}` | ${t:.4f} | ${g:.4f} | "
            f"${d:+.4f} | {p:+.1f}% |"
        )
    lines.append("")

    lines.append("## By day")
    lines.append("")
    lines.append("| Date | Tracker | Google | Diff | Diff % |")
    lines.append("|---|---:|---:|---:|---:|")
    for d, (t, g) in diff.by_day.items():
        delta = g - t
        p = (delta / t * 100.0) if t else 0.0
        lines.append(
            f"| {d.isoformat()} | ${t:.4f} | ${g:.4f} | "
            f"${delta:+.4f} | {p:+.1f}% |"
        )
    return "\n".join(lines)
