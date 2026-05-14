"""Tests for cost.audit."""
from datetime import date, datetime
from pathlib import Path

import pytest

from bizniz.cost.audit import (
    AuditDiff, GoogleBillingEntry, TrackerEntry,
    _classify_sku, _parse_date,
    compare, parse_costs_md, parse_google_billing_csv, render_diff,
)


# ── parse_costs_md ──────────────────────────────────────────────────────


SAMPLE_COSTS_MD = """\
# Cost log

## 2026-05-06 11:17:45 — phase=provision

CostSummary(calls=0, ...)

### Per-call breakdown
```
  (no calls)
```

---

## 2026-05-06 11:19:29 — phase=auth

CostSummary(calls=12, ...)

### Per-call breakdown
```
  auth_agent  gemini-3-flash-preview  in= 5,000 out=  300  $0.0034
  auth_agent  gemini-3-flash-preview  in= 8,000 out=  150  $0.0046  cached_in=4,000
```

---

## 2026-05-07 09:00:00 — milestone=1 phase=implement

CostSummary(calls=44, ...)

### Per-call breakdown
```
  engineer  gemini-3-flash-preview  in=10,000 out=  500  $0.0065
  engineer  gemini-3-flash-preview  in=15,000 out=  200  $0.0042  cached_in=8,000
```

---
"""


class TestParseCostsMd:
    def test_extracts_all_per_call_entries(self, tmp_path):
        path = tmp_path / "costs.md"
        path.write_text(SAMPLE_COSTS_MD)
        entries = parse_costs_md(path)
        assert len(entries) == 4

    def test_parses_dates_and_phases(self, tmp_path):
        path = tmp_path / "costs.md"
        path.write_text(SAMPLE_COSTS_MD)
        entries = parse_costs_md(path)
        # All entries from auth phase are 2026-05-06.
        auth = [e for e in entries if "auth" in e.phase]
        assert len(auth) == 2
        assert all(e.timestamp.date() == date(2026, 5, 6) for e in auth)
        # Implement entries are 2026-05-07.
        impl = [e for e in entries if "implement" in e.phase]
        assert all(e.timestamp.date() == date(2026, 5, 7) for e in impl)

    def test_extracts_cached_input(self, tmp_path):
        path = tmp_path / "costs.md"
        path.write_text(SAMPLE_COSTS_MD)
        entries = parse_costs_md(path)
        cached = [e for e in entries if e.cached_input_tokens > 0]
        assert len(cached) == 2
        assert {e.cached_input_tokens for e in cached} == {4000, 8000}

    def test_extracts_models_agents_costs(self, tmp_path):
        path = tmp_path / "costs.md"
        path.write_text(SAMPLE_COSTS_MD)
        entries = parse_costs_md(path)
        agents = {e.agent for e in entries}
        assert agents == {"auth_agent", "engineer"}
        models = {e.model for e in entries}
        assert models == {"gemini-3-flash-preview"}
        # Sum cost is float and reasonable.
        assert sum(e.cost for e in entries) == pytest.approx(0.0034 + 0.0046 + 0.0065 + 0.0042)

    def test_missing_file_returns_empty(self, tmp_path):
        assert parse_costs_md(tmp_path / "nope.md") == []

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "empty.md"
        path.write_text("")
        assert parse_costs_md(path) == []

    def test_no_breakdown_section_returns_empty(self, tmp_path):
        path = tmp_path / "x.md"
        path.write_text("# Cost log\n\n## 2026-05-06 11:17:45 — phase=foo\n\nCostSummary(...)\n\n---\n")
        assert parse_costs_md(path) == []


# ── parse_google_billing_csv ────────────────────────────────────────────


class TestParseGoogleCsv:
    def test_basic_csv(self, tmp_path):
        csv_path = tmp_path / "billing.csv"
        csv_path.write_text(
            "Date,SKU description,Cost\n"
            "2026-05-06,Gemini 3 Flash Preview Input,0.5000\n"
            "2026-05-07,Gemini 3 Flash Preview Output,1.5000\n"
        )
        rows = parse_google_billing_csv(csv_path)
        assert len(rows) == 2
        assert rows[0].date == date(2026, 5, 6)
        assert rows[0].cost == 0.5
        assert "Flash Preview Input" in rows[0].sku

    def test_csv_with_currency_symbols(self, tmp_path):
        csv_path = tmp_path / "billing.csv"
        csv_path.write_text(
            "Date,SKU,Cost\n"
            '2026-05-06,Gemini 2.5 Flash,"$1,234.56"\n'
        )
        rows = parse_google_billing_csv(csv_path)
        assert rows[0].cost == 1234.56

    def test_alternative_column_names(self, tmp_path):
        csv_path = tmp_path / "billing.csv"
        csv_path.write_text(
            "Usage Start Date,Service Description,Amount\n"
            "2026-05-06,Gemini 2.5 Pro,2.50\n"
        )
        rows = parse_google_billing_csv(csv_path)
        assert len(rows) == 1
        assert rows[0].date == date(2026, 5, 6)
        assert rows[0].cost == 2.50

    def test_skips_zero_cost_rows(self, tmp_path):
        csv_path = tmp_path / "billing.csv"
        csv_path.write_text(
            "Date,SKU,Cost\n"
            "2026-05-06,Gemini Free Tier,0.00\n"
            "2026-05-06,Gemini Paid,1.00\n"
        )
        rows = parse_google_billing_csv(csv_path)
        assert len(rows) == 1
        assert rows[0].cost == 1.00

    def test_missing_columns_raises(self, tmp_path):
        csv_path = tmp_path / "billing.csv"
        csv_path.write_text("ColA,ColB\nfoo,bar\n")
        with pytest.raises(ValueError, match="could not find"):
            parse_google_billing_csv(csv_path)

    def test_missing_file_returns_empty(self, tmp_path):
        rows = parse_google_billing_csv(tmp_path / "nope.csv")
        assert rows == []

    def test_handles_mm_dd_yyyy(self):
        assert _parse_date("05/06/2026") == date(2026, 5, 6)

    def test_handles_yyyy_mm_dd(self):
        assert _parse_date("2026-05-06") == date(2026, 5, 6)

    def test_unparseable_date_raises(self):
        with pytest.raises(ValueError):
            _parse_date("not a date")


# ── _classify_sku ───────────────────────────────────────────────────────


class TestClassifySku:
    def test_known_skus(self):
        m = {"gemini 3 flash preview": "gemini-3-flash-preview"}
        assert _classify_sku("Gemini 3 Flash Preview Input", m) == "gemini-3-flash-preview"

    def test_substring_match(self):
        m = {"gemini 2.5 flash": "gemini-2.5-flash",
             "gemini 2.5 flash-lite": "gemini-2.5-flash-lite"}
        # Longest-key-first: "Gemini 2.5 Flash-Lite" should match the longer key first.
        result = _classify_sku("Gemini 2.5 Flash-Lite Input", m)
        assert result == "gemini-2.5-flash-lite"

    def test_unknown_sku(self):
        result = _classify_sku("Some Unknown Service", {"foo": "bar"})
        assert result.startswith("(unknown:")


# ── compare + render ────────────────────────────────────────────────────


class TestCompare:
    def _t(self, day, model, cost):
        return TrackerEntry(
            timestamp=datetime.fromisoformat(f"{day}T12:00:00"),
            agent="engineer", model=model,
            input_tokens=1000, output_tokens=100,
            cached_input_tokens=0, cost=cost,
        )

    def _g(self, day, sku, cost):
        return GoogleBillingEntry(
            date=date.fromisoformat(day), sku=sku, cost=cost,
        )

    def test_sums_by_day_and_model(self):
        tracker = [
            self._t("2026-05-06", "gemini-3-flash-preview", 0.10),
            self._t("2026-05-06", "gemini-3-flash-preview", 0.20),
            self._t("2026-05-07", "gemini-2.5-flash", 0.05),
        ]
        google = [
            self._g("2026-05-06", "Gemini 3 Flash Preview Input", 0.32),
            self._g("2026-05-07", "Gemini 2.5 Flash Input", 0.06),
        ]
        diff = compare(tracker, google)
        assert diff.tracker_total == pytest.approx(0.35)
        assert diff.google_total == pytest.approx(0.38)
        # By day.
        assert diff.by_day[date(2026, 5, 6)] == pytest.approx((0.30, 0.32))
        assert diff.by_day[date(2026, 5, 7)] == pytest.approx((0.05, 0.06))
        # By model.
        assert diff.by_model["gemini-3-flash-preview"] == pytest.approx((0.30, 0.32))

    def test_period_filter(self):
        tracker = [
            self._t("2026-05-05", "x", 1.0),
            self._t("2026-05-06", "x", 2.0),
            self._t("2026-05-07", "x", 4.0),
        ]
        diff = compare(
            tracker, [],
            start=date(2026, 5, 6), end=date(2026, 5, 6),
        )
        assert diff.tracker_total == pytest.approx(2.0)

    def test_render_includes_grand_total_and_diff(self):
        tracker = [self._t("2026-05-06", "gemini-3-flash-preview", 0.50)]
        google = [self._g("2026-05-06", "Gemini 3 Flash Preview Input", 0.55)]
        diff = compare(tracker, google)
        out = render_diff(diff)
        assert "Tracker total" in out
        assert "Google total" in out
        assert "+10.0%" in out  # \$0.05 over \$0.50 = +10%
        assert "gemini-3-flash-preview" in out
        assert "2026-05-06" in out

    def test_render_handles_zero_tracker(self):
        # No tracker data — google-only — should not divide-by-zero.
        diff = compare([], [self._g("2026-05-06", "foo", 1.0)])
        out = render_diff(diff)
        assert "Cost Audit" in out
        # No assertion on % sign — depends on implementation, but we
        # verify no crash.

    def test_empty_inputs(self):
        diff = compare([], [])
        assert diff.tracker_total == 0.0
        assert diff.google_total == 0.0
        assert diff.by_day == {}
        assert diff.by_model == {}
