# Decomposer — roll units up to parent issue for reporting

Filed 2026-05-15 mid-M5 of crm_v1. Sub-ticket of roadmap item 4.

## Problem

After item 4 v1 shipped, the dispatcher treats every unit as a
standalone CoderIssue. ``EngineerResult.completed_issue_ids`` now
contains UNIT ids (``BE-009-U1``, ``BE-009-U2``, ...) not parent
issue ids (``BE-009``). The parent issue concept lives only in
the description string of each unit.

This works functionally — ``QualityEngineer.review`` consumes
``spec_refs`` (which propagate from parent to every unit via the
``_unit_to_coder_issue`` shim), not issue ids, so capability-to-
coverage mapping is intact. But human readability suffers:

- Per-issue summary is missing. Operator can't easily see "BE-009
  is done" — they have to mentally aggregate ``BE-009-U1`` and
  ``BE-009-U2`` from the dispatch log.
- If 3 of 4 units in an issue pass and 1 fails, the parent issue's
  partial state isn't represented anywhere.
- The decomposer-aware downstream (refactorer in item 5, perf
  logging in item 8) may want to attribute work back to the
  feature, not the unit.

## Fix

Add **reporting-layer rollup**. Don't touch the dispatch / flat
unit-list architecture — that's correct. Just preserve parent issue
ids and group at report time.

### Acceptance

1. ``_unit_to_coder_issue`` shim stores ``parent_issue_id`` (new
   field on ``CoderIssue`` OR a separate dict on the dispatcher).
2. Dispatcher emits an end-of-dispatch per-issue summary log:
   ``MilestoneCodeDispatcher: decomposed 10 issues → 25 units → 24
   passed, 1 deferred (BE-005-U3 in BE-005)``.
3. Roll up unit-level outcomes to a per-parent-issue completion
   state in the returned ``EngineerResult``:
   - ``completed_issue_ids`` lists PARENT issue ids of issues
     whose units all passed.
   - ``deferred_issue_ids`` lists parent ids of issues with any
     failed/deferred unit.
   - Optional: new ``completed_units`` / ``deferred_units`` fields
     for per-unit visibility.

### Why this scope (not bigger)

- Dispatch is correct: unit-level dispatch + per-unit topo +
  per-unit test feedback. Don't touch.
- Resume granularity stays unit-level. The issue store keeps unit
  rows. Rollup is computed at report time from the unit rows.

### Risk

The QualityEngineer.review path currently reads
``completed_issue_ids``. If we change those to parent issue ids,
QE.review's capability mapping still works (via spec_refs) but
any direct lookup by issue id might break. Audit before changing.

Same for IssueStateStore: queries by issue_id need to know which
shape they're getting. Most callers iterate; the dispatcher itself
is the authority — keep the unit rows in the DB and add a derived
"parent issue done?" query for callers that need it.

## Estimated effort

~1 hour. Small change to ``_unit_to_coder_issue`` (add
``parent_issue_id`` field) + ``MilestoneCodeDispatcher.run`` rollup
logic at the end. Tests for the per-issue completion derivation
+ the new log line.

## Related

- ``docs/roadmap.md`` item 4 (parent ticket).
- ``bizniz/driver/milestone_code_dispatcher.py:_unit_to_coder_issue``
  — shim to extend.
- ``bizniz/engineer/types.py:EngineerResult`` — return shape.
- ``bizniz/state/issue_store.py`` — DB row shape (no change needed
  if we derive rollup at report time).
