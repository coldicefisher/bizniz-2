#!/usr/bin/env python3
"""
Focused code generation loop test.

Skips architect/engineer phases entirely. Uses pre-seeded DB with
architecture plan, issues, and Docker images. Goes straight into
the CodingOrchestrator loop for each issue.

Collects detailed metrics: time per issue, iterations, token counts,
tool calls, pass/fail, error details.

Usage:
    python3 examples/codegen_blast.py
"""

import json
import os
import shutil
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()


# ── Configuration ────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "blast_config.yaml"


def _load_blast_config(path=None):
    """Load blast config from YAML file."""
    import yaml
    if path:
        cfg_path = Path(path)
        if not cfg_path.is_absolute():
            cfg_path = Path(__file__).parent / cfg_path
    else:
        cfg_path = CONFIG_PATH
    with open(cfg_path) as f:
        return yaml.safe_load(f)


# ── Metrics ──────────────────────────────────────────────────────────────────

class IssueMetrics:
    def __init__(self, issue_id, title):
        self.issue_id = issue_id
        self.title = title
        self.start_time = None
        self.end_time = None
        self.elapsed_seconds = 0
        self.iterations = 0
        self.success = False
        self.skipped = False
        self.error = None
        self.strategy_used = None
        self.stall_count = 0
        self.agentic_debug_used = False
        self.tool_calls = 0
        self.log_lines = []

    def start(self):
        self.start_time = time.time()

    def finish(self, success, iterations=0, error=None, strategy=None):
        self.end_time = time.time()
        self.elapsed_seconds = self.end_time - self.start_time
        self.success = success
        self.iterations = iterations
        self.error = error
        self.strategy_used = strategy

    def log(self, msg):
        ts = time.time() - (self.start_time or time.time())
        line = f"[{ts:7.1f}s] {msg}"
        self.log_lines.append(line)
        print(f"  {line}")

    def to_dict(self):
        return {
            "issue_id": self.issue_id,
            "title": self.title,
            "success": self.success,
            "skipped": self.skipped,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "iterations": self.iterations,
            "strategy": self.strategy_used,
            "stall_count": self.stall_count,
            "agentic_debug": self.agentic_debug_used,
            "tool_calls": self.tool_calls,
            "error": self.error,
        }


class RunMetrics:
    def __init__(self):
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.start_time = time.time()
        self.issues = []
        self.total_failures = 0

    def add(self, m: IssueMetrics):
        self.issues.append(m)
        if not m.success:
            self.total_failures += 1

    def summary(self):
        elapsed = time.time() - self.start_time
        passed = sum(1 for m in self.issues if m.success)
        skipped = sum(1 for m in self.issues if m.skipped)
        failed = sum(1 for m in self.issues if not m.success and not m.skipped)
        total_iters = sum(m.iterations for m in self.issues)
        return {
            "run_id": self.run_id,
            "total_elapsed_seconds": round(elapsed, 1),
            "issues_attempted": len(self.issues),
            "issues_passed": passed,
            "issues_failed": failed,
            "issues_skipped": skipped,
            "total_iterations": total_iters,
            "issue_details": [m.to_dict() for m in self.issues],
        }

    def save(self, path):
        with open(path, "w") as f:
            json.dump(self.summary(), f, indent=2)
        print(f"\nMetrics saved to {path}")


# ── Logging interceptors ────────────────────────────────────────────────────

def make_status_callback(metrics: IssueMetrics):
    """Create a status callback that logs + counts tool calls."""
    def cb(msg: str):
        metrics.log(msg)
        # Count tool calls
        if any(x in msg for x in ["viewing", "listing", "searching", "running command"]):
            metrics.tool_calls += 1
        if "stall detected" in msg.lower():
            metrics.stall_count += 1
        if "AgenticDebugger" in msg:
            metrics.agentic_debug_used = True
    return cb


# ── Kahn's topological sort ──────────────────────────────────────────────────

def _topological_sort(issues):
    """
    Sort issues in topological order using Kahn's algorithm.
    Issues with no dependencies come first. Issues depending on earlier
    issues come later. Falls back to ID order for issues without deps.
    """
    id_to_issue = {iss["id"]: iss for iss in issues}
    valid_ids = set(id_to_issue.keys())

    # Build in-degree and adjacency (only for deps within this issue set)
    in_degree = {iss["id"]: 0 for iss in issues}
    dependents = defaultdict(list)

    for iss in issues:
        for dep_id in iss.get("depends_on", []):
            if dep_id in valid_ids:
                in_degree[iss["id"]] += 1
                dependents[dep_id].append(iss["id"])

    # Kahn's: collect zero-in-degree nodes layer by layer
    queue = deque(sorted(iid for iid, deg in in_degree.items() if deg == 0))
    ordered = []

    while queue:
        layer = sorted(queue)  # stable sort within layer by ID
        queue.clear()
        for iid in layer:
            ordered.append(id_to_issue[iid])
            for dep_id in dependents[iid]:
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    queue.append(dep_id)

    # Any remaining issues have cyclic deps — append them at the end
    ordered_ids = {iss["id"] for iss in ordered}
    for iss in issues:
        if iss["id"] not in ordered_ids:
            ordered.append(iss)

    return ordered


# ── Helpers ──────────────────────────────────────────────────────────────────

def _nuke_shadow_files(target_dir: Path):
    """Remove top-level .py files that shadow known packages."""
    _stdlib = set(sys.stdlib_module_names)
    _aliases = {"cv2", "PIL", "sklearn", "yaml", "bs4", "gi", "attr", "dotenv"}
    _common_pkgs = {
        "pydantic", "fastapi", "flask", "django", "sqlalchemy", "celery",
        "redis", "requests", "httpx", "uvicorn", "starlette", "pytest",
        "numpy", "pandas", "click", "rich", "boto3", "stripe",
    }
    _skip = {"conftest", "sitecustomize", "setup", "manage", "app", "main"}

    for py_file in target_dir.glob("*.py"):
        if py_file.name.startswith("__"):
            continue
        stem = py_file.stem
        stem_lower = stem.lower().replace("-", "_")
        if stem_lower in _skip:
            continue
        if stem in _stdlib or stem in _aliases or stem_lower in _common_pkgs:
            py_file.unlink()
            print(f"  [cleanup] Removed shadow file: {py_file.name}")


def _sanitize_requirements(target_dir: Path):
    """Remove invalid entries from requirements.txt."""
    import re as _re
    _stdlib = set(sys.stdlib_module_names)

    for req_file in target_dir.rglob("requirements.txt"):
        try:
            lines = req_file.read_text().splitlines()
        except Exception:
            continue
        clean = []
        seen = set()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                clean.append(line)
                continue
            pkg = _re.split(r"[><=!\[;]", stripped)[0].strip().lower().replace("-", "_")
            if pkg in _stdlib:
                print(f"  [cleanup] Removed stdlib '{stripped}' from {req_file}")
                continue
            if pkg in seen:
                continue
            if not _re.match(r"^[a-zA-Z]", stripped):
                print(f"  [cleanup] Removed invalid '{stripped}' from {req_file}")
                continue
            seen.add(pkg)
            clean.append(stripped)
        req_file.write_text("\n".join(clean) + "\n")


def _run_preflight_standalone(generated_files, workspace, language="python"):
    """Run preflight validation without Docker.

    Validates imports and creates auto-stubs. Writes any stubs/rewrites
    to the workspace on disk.
    """
    from bizniz.preflight.registry import get_validator

    validator = get_validator(language, workspace)
    if not validator:
        return None

    result = validator.validate(generated_files, [])

    # Write auto-stubs to disk
    for stub in result.stubs_created:
        workspace.write_file(path=stub.filepath, content=stub.content)

    # Write import rewrites to disk
    for rw in result.import_rewrites:
        if rw.filepath in generated_files:
            content = workspace.read_file(path=rw.filepath)
            if content:
                updated = content.replace(rw.old_import, rw.new_import)
                workspace.write_file(path=rw.filepath, content=updated)

    return result


# ── Phase 1: Frame (generate ALL code, no tests) ────────────────────────────

def _phase1_frame(issues, workspace, arch_context, make_client, env, model="gpt-4o-mini"):
    """Generate source code for all issues in topological order.

    No Docker, no tests. Writes real code to disk so later issues
    can import from earlier ones (instead of stubs).
    """
    from bizniz.agents.coder.coder import Coder

    print(f"\n{'=' * 60}")
    print("  PHASE 1: FRAME (generate all code, no tests)")
    print(f"{'=' * 60}\n")

    failed_issue_ids = set()
    phase1_metrics = []

    for i, issue in enumerate(issues):
        print(f"  [{i+1}/{len(issues)}] {issue['title']}")

        metrics = IssueMetrics(issue["id"], issue["title"])
        metrics.start()
        status_cb = make_status_callback(metrics)

        try:
            client = make_client(model)
            coder = Coder(
                client=client,
                environment=env,
                workspace=workspace,
                on_status_message=status_cb,
            )

            # Build problem statement — just the issue + hints.
            # Dependency code is already on disk; the agent discovers it via tools.
            problem_stmt = issue["description"]
            if issue.get("test_setup_hint"):
                problem_stmt += f"\n\nTEST SETUP HINT:\n{issue['test_setup_hint']}"

            # Generate source code only (no test_files)
            result = coder.generate_multi(
                issue_description=problem_stmt,
                target_files=issue["target_files"],
                architecture_context=arch_context,
                test_files=None,  # No tests in Phase 1
                on_status_message=status_cb,
            )

            # Run standalone preflight to fix imports and create auto-stubs
            generated = {ch.filepath: ch.code for ch in result.changes}
            pfr = _run_preflight_standalone(generated, workspace)
            if pfr:
                print(f"    {pfr.summary()}")

            metrics.finish(success=True, iterations=1, strategy="frame")
            print(f"    Framed in {metrics.elapsed_seconds:.1f}s "
                  f"({len(result.changes)} files)")
            phase1_metrics.append({"id": issue["id"], "title": issue["title"], "status": "framed"})

        except Exception as e:
            error_str = f"{type(e).__name__}: {str(e)[:200]}"
            failed_issue_ids.add(issue["id"])
            metrics.finish(success=False, error=error_str)
            print(f"    FAILED: {error_str}")
            phase1_metrics.append({"id": issue["id"], "title": issue["title"], "status": "failed", "error": error_str})

    _nuke_shadow_files(workspace._root)

    framed = sum(1 for m in phase1_metrics if m["status"] == "framed")
    print(f"\n  Phase 1 complete: {framed}/{len(issues)} issues framed")
    return failed_issue_ids, phase1_metrics


# ── Phase 2: Test (generate tests + run + repair, bottom-up) ────────────────

def _collect_dep_files(issue, issues):
    """Collect all source file paths from an issue's transitive dependencies.

    Returns a list of {"filepath": ..., "action": "modify"} dicts for
    dependency files that the repair loop is allowed to modify.
    """
    id_to_issue = {iss["id"]: iss for iss in issues}
    dep_ids = set(issue.get("depends_on", []))
    own_files = {tf["filepath"] for tf in issue["target_files"]}

    visited = set()
    dep_files = []
    queue = list(dep_ids)
    while queue:
        dep_id = queue.pop(0)
        if dep_id in visited:
            continue
        visited.add(dep_id)
        dep_issue = id_to_issue.get(dep_id)
        if not dep_issue:
            continue
        for tf in dep_issue["target_files"]:
            fp = tf["filepath"]
            if fp not in own_files:
                dep_files.append({"filepath": fp, "action": "modify"})
                own_files.add(fp)  # dedup
        for transitive_id in dep_issue.get("depends_on", []):
            if transitive_id not in visited:
                queue.append(transitive_id)

    return dep_files


def _find_issues_affected_by(modified_files, issues, passed_issue_ids):
    """Find already-passed issues whose source files were modified.

    Returns issue IDs that need re-testing because their own files
    or their dependency files were changed.
    """
    affected = []
    for iss in issues:
        if iss["id"] not in passed_issue_ids:
            continue
        own_files = {tf["filepath"] for tf in iss["target_files"]}
        if own_files & modified_files:
            affected.append(iss["id"])
    return affected


def _phase2_test(
    issues, workspace, arch_context, dep_edges, make_client, env,
    phase1_failed_ids, run_metrics, blast_cfg,
    snapshot_workspace_fn, restore_workspace_fn,
):
    """Cycle-based test strategy with response caching.

    Each cycle has three rounds:
      Round 1 (Shot):    single attempt per issue — quick first pass
      Round 2 (Grind):   up to N attempts per issue, bail on AI error / cached repeat / max
      Round 3 (Agentic): agentic debugger for remaining failures

    Cycles repeat until all issues pass or max_cycles reached.
    """
    import hashlib
    from bizniz.agents.coder.coder import Coder
    from bizniz.tester.tester import Tester
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
    from bizniz.orchestrator.strategy import CodingStrategy
    from bizniz.orchestrator.model_progression import ModelProgression

    max_cycles = blast_cfg["max_cycles"]
    shot_cfg = blast_cfg["shot_round"]
    grind_cfg = blast_cfg["grind_round"]
    agentic_cfg = blast_cfg["agentic_round"]

    passed_issue_ids: set = set()
    failed_issue_ids: set = set()
    skipped_issue_ids: set = set()
    completed_test_files: set = set()
    last_good_snapshot = "post_phase1"

    # Response cache: issue_id -> set of content hashes from prior attempts
    response_cache: dict[int, set[str]] = defaultdict(set)

    def _snapshot_issue_files(issue):
        """Hash the content of an issue's target + test files for cache comparison."""
        h = hashlib.sha256()
        for tf in sorted(issue["target_files"], key=lambda x: x["filepath"]):
            try:
                content = workspace.read_file(path=tf["filepath"])
                h.update(content.encode() if content else b"")
            except Exception:
                pass
        for tf in sorted(issue["test_files"]):
            try:
                content = workspace.read_file(path=tf)
                h.update(content.encode() if content else b"")
            except Exception:
                pass
        return h.hexdigest()

    def _run_issue(issue, round_cfg, label="Issue"):
        """Run the orchestrator for a single issue. Returns (success, metrics, result)."""
        idx = next((j for j, iss in enumerate(issues) if iss["id"] == issue["id"]), 0)

        issue_metrics = IssueMetrics(issue["id"], issue["title"])
        issue_metrics.start()

        print(f"\n{'─' * 60}")
        print(f"  {label} {idx+1}/{len(issues)}: [{issue['id']}] {issue['title']}")
        print(f"{'─' * 60}")

        status_cb = make_status_callback(issue_metrics)

        try:
            model = round_cfg["model"]
            max_iters = round_cfg["max_iters_per_attempt"]
            agentic = round_cfg.get("agentic_debug", False)
            print(f"  Model: {model} | Max iters: {max_iters} | Agentic: {agentic}")
            client = make_client(model)

            coder = Coder(
                client=client,
                environment=env,
                workspace=workspace,
                on_status_message=status_cb,
            )

            tester = Tester(
                client=client,
                environment=env,
                workspace=workspace,
                on_status_message=status_cb,
            )

            progression = ModelProgression(round_cfg["escalation"])

            orchestrator = CodingOrchestrator(
                coder=coder,
                tester=tester,
                test_environment=env,
                workspace=workspace,
                client=client,
                client_factory=make_client,
                model_progression=progression,
                max_iterations=max_iters,
                stall_threshold=round_cfg["stall_threshold"],
                agentic_debug_threshold=round_cfg["stall_threshold"],
                on_status_message=status_cb,
                enable_agentic_debug=agentic,
                stall_recovery=round_cfg["stall_recovery"],
            )

            # Build problem statement
            problem_stmt = issue["description"]
            if issue.get("test_setup_hint"):
                problem_stmt += f"\n\nTEST SETUP HINT:\n{issue['test_setup_hint']}"

            # Include dependency files as writable targets
            own_targets = [
                {"filepath": tf["filepath"], "action": "modify"}
                for tf in issue["target_files"]
            ]
            dep_targets = _collect_dep_files(issue, issues)
            target_files = own_targets + dep_targets
            if dep_targets:
                print(f"  Writable deps: {', '.join(tf['filepath'] for tf in dep_targets)}")

            result = orchestrator.run_multi(
                prompt=problem_stmt,
                target_files=target_files,
                test_files=issue["test_files"],
                architecture_context=arch_context,
                strategy=CodingStrategy.CODE_FIRST,
                dependency_edges=dep_edges,
                prior_test_files=completed_test_files or None,
            )

            issue_metrics.finish(
                success=result.success,
                iterations=result.iterations,
                strategy=result.strategy_used,
            )

            if not result.success:
                issue_metrics.finish(
                    success=False,
                    iterations=result.iterations,
                    error="Tests did not pass",
                    strategy=result.strategy_used,
                )

            return result.success, issue_metrics

        except Exception as e:
            error_name = type(e).__name__
            error_str = f"{error_name}: {str(e)[:200]}"

            if "no test files" in str(e).lower() or "no tests" in str(e).lower():
                issue_metrics.finish(success=False, error=error_str)
                issue_metrics.skipped = True
                print(f"\n  SKIPPED (no tests possible): {error_str}")
                return None, issue_metrics
            else:
                issue_metrics.finish(success=False, error=error_str)
                print(f"\n  CRASHED: {error_str}")
                return False, issue_metrics

    # ── Cycle loop ────────────────────────────────────────────────────────
    for cycle in range(1, max_cycles + 1):
        remaining = [iss for iss in issues if iss["id"] not in passed_issue_ids
                      and iss["id"] not in skipped_issue_ids]
        if not remaining:
            print(f"\n  All issues passed!")
            break

        print(f"\n{'=' * 60}")
        print(f"  CYCLE {cycle}/{max_cycles} — {len(remaining)} issues remaining")
        print(f"{'=' * 60}")

        # ── Round 1: Shot (single attempt per issue) ──────────────────
        print(f"\n  ── Round 1: Shot (model={shot_cfg['model']}, 1 attempt) ──")

        round1_newly_passed = []
        for issue in remaining:
            if issue["id"] in passed_issue_ids:
                continue

            success, issue_metrics = _run_issue(issue, shot_cfg, label="Shot")
            run_metrics.add(issue_metrics)
            post_hash = _snapshot_issue_files(issue)
            response_cache[issue["id"]].add(post_hash)

            if success:
                print(f"\n  PASSED in {issue_metrics.elapsed_seconds:.1f}s "
                      f"({issue_metrics.iterations} iters)")
                passed_issue_ids.add(issue["id"])
                failed_issue_ids.discard(issue["id"])
                completed_test_files.update(issue["test_files"])
                round1_newly_passed.append(issue["id"])
                snap = f"c{cycle}_shot_{issue['id']}"
                snapshot_workspace_fn(snap)
                last_good_snapshot = snap
            elif success is None:
                skipped_issue_ids.add(issue["id"])
            else:
                failed_issue_ids.add(issue["id"])

        # ── Regression check after Round 1 ────────────────────────────
        if round1_newly_passed:
            prior_passed = [
                iss for iss in issues
                if iss["id"] in passed_issue_ids and iss["id"] not in round1_newly_passed
            ]
            if prior_passed:
                print(f"\n  ── Regression check: {len(prior_passed)} prior issues ──")
                for iss in prior_passed:
                    success, re_metrics = _run_issue(iss, shot_cfg, label="RECHECK")
                    run_metrics.add(re_metrics)
                    if success:
                        print(f"  RECHECK OK: [{iss['id']}] {iss['title']}")
                    elif success is None:
                        pass
                    else:
                        print(f"  REGRESSION: [{iss['id']}] {iss['title']}")
                        passed_issue_ids.discard(iss["id"])
                        failed_issue_ids.add(iss["id"])
                        completed_test_files -= set(iss["test_files"])

        # ── Round 2: Grind (multi-attempt with bail conditions) ───────
        remaining_after_shot = [
            iss for iss in issues
            if iss["id"] in failed_issue_ids
        ]

        if remaining_after_shot:
            max_attempts = grind_cfg["max_attempts_per_issue"]
            print(f"\n  ── Round 2: Grind (model={grind_cfg['model']}, "
                  f"max_attempts={max_attempts}) ──")

            round2_newly_passed = []
            for issue in remaining_after_shot:
                for attempt in range(1, max_attempts + 1):
                    print(f"\n  [Attempt {attempt}/{max_attempts}]")

                    success, issue_metrics = _run_issue(issue, grind_cfg, label="Grind")
                    run_metrics.add(issue_metrics)
                    post_hash = _snapshot_issue_files(issue)

                    if success:
                        print(f"\n  PASSED in {issue_metrics.elapsed_seconds:.1f}s "
                              f"({issue_metrics.iterations} iters)")
                        passed_issue_ids.add(issue["id"])
                        failed_issue_ids.discard(issue["id"])
                        completed_test_files.update(issue["test_files"])
                        round2_newly_passed.append(issue["id"])
                        snap = f"c{cycle}_grind_{issue['id']}"
                        snapshot_workspace_fn(snap)
                        last_good_snapshot = snap
                        break
                    elif success is None:
                        skipped_issue_ids.add(issue["id"])
                        break
                    else:
                        # Bail condition 1: AI error (no output)
                        if issue_metrics.error and "BadAIResponse" in (issue_metrics.error or ""):
                            print(f"  ⏭  AI error — skipping to next issue")
                            break
                        # Bail condition 2: response is identical to a cached prior attempt
                        if post_hash in response_cache[issue["id"]]:
                            print(f"  ⏭  Cached repeat — skipping to next issue")
                            break
                        response_cache[issue["id"]].add(post_hash)

                if issue["id"] not in passed_issue_ids and issue["id"] not in skipped_issue_ids:
                    print(f"\n  Grind round exhausted for [{issue['id']}] {issue['title']}")

            # Regression check after Round 2
            if round2_newly_passed:
                prior_passed = [
                    iss for iss in issues
                    if iss["id"] in passed_issue_ids and iss["id"] not in round2_newly_passed
                ]
                if prior_passed:
                    print(f"\n  ── Regression check: {len(prior_passed)} prior issues ──")
                    for iss in prior_passed:
                        success, re_metrics = _run_issue(iss, grind_cfg, label="RECHECK")
                        run_metrics.add(re_metrics)
                        if success:
                            print(f"  RECHECK OK: [{iss['id']}] {iss['title']}")
                        elif success is None:
                            pass
                        else:
                            print(f"  REGRESSION: [{iss['id']}] {iss['title']}")
                            passed_issue_ids.discard(iss["id"])
                            failed_issue_ids.add(iss["id"])
                            completed_test_files -= set(iss["test_files"])

        # ── Round 3: Agentic debugger ─────────────────────────────────
        remaining_after_grind = [
            iss for iss in issues
            if iss["id"] in failed_issue_ids
        ]

        if remaining_after_grind:
            print(f"\n  ── Round 3: Agentic (model={agentic_cfg['model']}, "
                  f"max_iters={agentic_cfg['max_iters_per_attempt']}) ──")

            round3_newly_passed = []
            for issue in remaining_after_grind:
                success, issue_metrics = _run_issue(issue, agentic_cfg, label="Agentic")
                run_metrics.add(issue_metrics)

                if success:
                    print(f"\n  PASSED in {issue_metrics.elapsed_seconds:.1f}s")
                    passed_issue_ids.add(issue["id"])
                    failed_issue_ids.discard(issue["id"])
                    completed_test_files.update(issue["test_files"])
                    round3_newly_passed.append(issue["id"])
                    snap = f"c{cycle}_agentic_{issue['id']}"
                    snapshot_workspace_fn(snap)
                    last_good_snapshot = snap
                elif success is None:
                    skipped_issue_ids.add(issue["id"])
                else:
                    print(f"\n  FAILED in {issue_metrics.elapsed_seconds:.1f}s")
                    if agentic_cfg.get("rollback_on_fail", False):
                        restore_workspace_fn(last_good_snapshot)

            # Regression check after Round 3
            if round3_newly_passed:
                prior_passed = [
                    iss for iss in issues
                    if iss["id"] in passed_issue_ids and iss["id"] not in round3_newly_passed
                ]
                if prior_passed:
                    print(f"\n  ── Regression check: {len(prior_passed)} prior issues ──")
                    for iss in prior_passed:
                        success, re_metrics = _run_issue(iss, agentic_cfg, label="RECHECK")
                        run_metrics.add(re_metrics)
                        if success:
                            print(f"  RECHECK OK: [{iss['id']}] {iss['title']}")
                        elif success is None:
                            pass
                        else:
                            print(f"  REGRESSION: [{iss['id']}] {iss['title']}")
                            passed_issue_ids.discard(iss["id"])
                            failed_issue_ids.add(iss["id"])
                            completed_test_files -= set(iss["test_files"])

        # Cycle summary
        total_passed = len(passed_issue_ids)
        total_failed = len(failed_issue_ids)
        print(f"\n  Cycle {cycle} complete: "
              f"{total_passed} passed, {total_failed} failed, "
              f"{len(skipped_issue_ids)} skipped")

        if not failed_issue_ids:
            break

    return failed_issue_ids


# ── Main ─────────────────────────────────────────────────────────────────────

def main(config_path=None):
    from bizniz.config.bizniz_config import BiznizConfig
    from bizniz.workspace.local_workspace import LocalWorkspace
    from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
    from bizniz.environment.docker_jest_environment import DockerJestEnvironment
    from bizniz.clients.chatgpt.openai_chatgpt_client import OpenAIChat4GPTClient
    from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
    from bizniz.clients.claude.claude_client import ClaudeClient
    from bizniz.clients.gemini.gemini_client import GeminiClient
    from bizniz.engineer.engineer import Engineer
    from bizniz.engineer.types import ArchitecturePlan

    print("=" * 60)
    print("  Cycle-Based Code Generation Blast Test")
    print("=" * 60)

    # Load blast config: explicit arg > CLI arg > default
    cfg_path = config_path or (sys.argv[1] if len(sys.argv) > 1 else None)
    blast_cfg = _load_blast_config(cfg_path)
    PROJECT_ROOT = Path(blast_cfg["project_root"])
    WORKSPACE_DIR = PROJECT_ROOT / blast_cfg["workspace_subdir"]
    DOCKER_IMAGE = blast_cfg["docker_image"]
    PROBLEM_ID = blast_cfg["problem_id"]
    MODEL = blast_cfg["frame_model"]

    config = BiznizConfig.find_and_load()
    print(f"\n  Frame model: {MODEL}")
    print(f"  Shot round:    {blast_cfg['shot_round']['model']} (1 attempt)")
    print(f"  Grind round:   {blast_cfg['grind_round']['model']} "
          f"(max {blast_cfg['grind_round']['max_attempts_per_issue']} attempts)")
    print(f"  Agentic round: {blast_cfg['agentic_round']['model']} "
          f"(agentic, {blast_cfg['agentic_round']['max_iters_per_attempt']} iters)")
    print(f"  Max cycles: {blast_cfg['max_cycles']}")
    print(f"  Project: {PROJECT_ROOT}")
    print(f"  Docker image: {DOCKER_IMAGE}")

    # Setup workspace
    workspace = LocalWorkspace(root=WORKSPACE_DIR)

    # Get issues from DB
    import sqlite3
    db_path = WORKSPACE_DIR / ".bizniz" / "bizniz.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, title, description, target_files_json, test_files_json, "
        "depends_on_json, suggested_model, test_setup_hint "
        "FROM issues WHERE problem_id=? AND status='open' ORDER BY id",
        (PROBLEM_ID,)
    ).fetchall()
    conn.close()

    issues = []
    for row in rows:
        depends_on = []
        if row["depends_on_json"]:
            try:
                depends_on = json.loads(row["depends_on_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        issues.append({
            "id": row["id"],
            "title": row["title"],
            "description": row["description"],
            "target_files": json.loads(row["target_files_json"]) if row["target_files_json"] else [],
            "test_files": json.loads(row["test_files_json"]) if row["test_files_json"] else [],
            "depends_on": depends_on,
            "suggested_model": row["suggested_model"],
            "test_setup_hint": row["test_setup_hint"] if "test_setup_hint" in row.keys() else "",
        })

    # Resolve title-based dependencies to IDs
    title_to_id = {iss["title"]: iss["id"] for iss in issues}
    for iss in issues:
        resolved = []
        for dep in iss.get("depends_on", []):
            if isinstance(dep, str) and dep in title_to_id:
                resolved.append(title_to_id[dep])
            elif isinstance(dep, int):
                resolved.append(dep)
        iss["depends_on"] = resolved

    # Sort issues using Kahn's algorithm (topological order by dependencies)
    issues = _topological_sort(issues)

    print(f"\n  Issues to process: {len(issues)} (topological order)")
    for i, issue in enumerate(issues):
        targets = [tf["filepath"] for tf in issue["target_files"]]
        deps = issue.get("depends_on", [])
        dep_str = f" (depends on: {deps})" if deps else ""
        print(f"    {i+1}. [{issue['id']}] {issue['title']}{dep_str}")
        print(f"       Files: {', '.join(targets)}")
        print(f"       Tests: {', '.join(issue['test_files'])}")

    # Load architecture context
    plan_row = workspace.db.get_architecture_plan(PROBLEM_ID)
    arch_context = ""
    if plan_row:
        try:
            plan_data = json.loads(plan_row["plan_json"])
            plan_data.pop("db_id", None)
            plan_data.pop("problem_id", None)
            plan = ArchitecturePlan(
                db_id=plan_row["id"],
                problem_id=PROBLEM_ID,
                **plan_data,
            )
            arch_context = Engineer.format_architecture_context(plan)
            dep_edges = plan.dependencies or []
            print(f"\n  Architecture: {plan.package_name} ({len(plan.modules)} modules, {len(dep_edges)} dependency edges)")
        except Exception as e:
            print(f"\n  WARNING: Could not load architecture plan: {e}")
            dep_edges = []
    else:
        dep_edges = []

    # Nuke all source/test .py files so scaffold creates fresh stubs
    _protected_dirs = {".bizniz", ".snapshots", "__pycache__", "docs"}
    for py_file in WORKSPACE_DIR.rglob("*.py"):
        if any(part in _protected_dirs for part in py_file.parts):
            continue
        if py_file.name in ("setup.py", "conftest.py"):
            continue
        py_file.unlink()
    print(f"  [cleanup] Removed stale source files for fresh scaffold")

    # Run scaffold to create stub files before code gen
    if plan_row and plan:
        from bizniz.engineer.scaffold import scaffold_from_plan
        from bizniz.engineer.types import EngineeringIssue, TargetFile

        scaffold_issues = []
        for iss in issues:
            scaffold_issues.append(EngineeringIssue(
                db_id=iss["id"],
                title=iss["title"],
                description=iss["description"],
                target_files=[TargetFile(**tf) for tf in iss["target_files"]],
                test_files=iss["test_files"],
                test_setup_hint=iss.get("test_setup_hint", ""),
            ))

        import_map = scaffold_from_plan(
            workspace=workspace,
            plan=plan,
            issues=scaffold_issues,
            on_status_message=lambda msg: print(f"  {msg}"),
        )
        print(f"  Scaffold: {len(import_map)} stub files created")

        # Update issue dicts with flipped actions (create -> modify)
        for iss, eng_iss in zip(issues, scaffold_issues):
            iss["target_files"] = [
                {"filepath": tf.filepath, "action": tf.action}
                for tf in eng_iss.target_files
            ]

    # Client factory
    def make_client(model_name):
        if model_name.startswith("claude"):
            return ClaudeClient(
                api_key=os.environ.get("ANTHROPIC_API_KEY"),
                model_name=model_name,
            )
        if model_name.startswith("gemini"):
            return GeminiClient(
                api_key=os.environ.get("GEMINI_API_KEY"),
                model_name=model_name,
            )
        cfg = ChatGPTClientConfig(default_model=model_name)
        return OpenAIChat4GPTClient(
            config=cfg,
            api_key=os.environ["OPENAI_API_KEY"],
        )

    # Nuke all stale bizniz-pytest containers before starting
    import subprocess as _sp
    try:
        result = _sp.run(
            ["docker", "ps", "-a", "--filter", "name=bizniz-pytest-",
             "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=10,
        )
        container_ids = result.stdout.strip().split("\n")
        container_ids = [c for c in container_ids if c]
        if container_ids:
            _sp.run(
                ["docker", "rm", "-f"] + container_ids,
                capture_output=True, timeout=30,
            )
            print(f"  [cleanup] Removed {len(container_ids)} stale container(s)")
    except Exception:
        pass

    # Clean workspace before baseline snapshot
    _nuke_shadow_files(WORKSPACE_DIR)
    _sanitize_requirements(WORKSPACE_DIR)

    # Remove junk directories
    _expected_top_dirs = set()
    for iss in issues:
        for tf in iss["target_files"]:
            top = Path(tf["filepath"]).parts[0] if "/" in tf["filepath"] else None
            if top:
                _expected_top_dirs.add(top)
        for tf in iss["test_files"]:
            top = Path(tf).parts[0]
            _expected_top_dirs.add(top)
    _expected_top_dirs.update({".bizniz", ".snapshots", "__pycache__", "docs", "node_modules"})
    for item in WORKSPACE_DIR.iterdir():
        if item.is_dir() and item.name not in _expected_top_dirs:
            shutil.rmtree(item)
            print(f"  [cleanup] Removed unexpected directory: {item.name}")

    # Workspace snapshotting
    snapshot_dir = PROJECT_ROOT / ".snapshots"
    snapshot_dir.mkdir(exist_ok=True)

    # Also clean any existing snapshots
    if snapshot_dir.exists():
        for snap in snapshot_dir.iterdir():
            if snap.is_dir():
                _nuke_shadow_files(snap)
                _sanitize_requirements(snap)

    def snapshot_workspace(label: str):
        snap = snapshot_dir / label
        if snap.exists():
            shutil.rmtree(snap)
        shutil.copytree(WORKSPACE_DIR, snap, ignore=shutil.ignore_patterns(".bizniz", ".snapshots", "__pycache__", "*.pyc"))
        _nuke_shadow_files(snap)
        print(f"  [snapshot] Saved workspace state: {label}")

    def restore_workspace(label: str):
        snap = snapshot_dir / label
        if not snap.exists():
            print(f"  [snapshot] No snapshot '{label}' found — skipping restore")
            return
        for item in WORKSPACE_DIR.iterdir():
            if item.name in (".bizniz", ".snapshots"):
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        for item in snap.iterdir():
            dest = WORKSPACE_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        _nuke_shadow_files(WORKSPACE_DIR)
        print(f"  [snapshot] Restored workspace to: {label}")

    # Save initial clean state
    snapshot_workspace("baseline")

    # Setup Docker environment (needed for Phase 1 coder env.describe() and Phase 2)
    test_runner = blast_cfg.get("test_runner", "pytest")
    if test_runner == "jest":
        env = DockerJestEnvironment(
            workspace_root=WORKSPACE_DIR,
            image=DOCKER_IMAGE,
        )
    else:
        env = DockerPytestEnvironment(
            workspace_root=WORKSPACE_DIR,
            image=DOCKER_IMAGE,
        )

    run_metrics = RunMetrics()

    try:
        # ── Phase 1: Frame all code (no Docker, no tests) ────────────────
        phase1_failed_ids, phase1_metrics = _phase1_frame(
            issues=issues,
            workspace=workspace,
            arch_context=arch_context,
            make_client=make_client,
            env=env,
            model=MODEL,
        )

        # Snapshot after Phase 1 so Phase 2 can rollback to this state
        _nuke_shadow_files(WORKSPACE_DIR)
        snapshot_workspace("post_phase1")

        # ── Phase 2: Test bottom-up (Docker + repair loop) ───────────────
        _phase2_test(
            issues=issues,
            workspace=workspace,
            arch_context=arch_context,
            dep_edges=dep_edges,
            make_client=make_client,
            env=env,
            phase1_failed_ids=phase1_failed_ids,
            run_metrics=run_metrics,
            blast_cfg=blast_cfg,
            snapshot_workspace_fn=snapshot_workspace,
            restore_workspace_fn=restore_workspace,
        )

    finally:
        env.stop()
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
            print("  [snapshot] Cleaned up workspace snapshots")

    # Print summary
    summary = run_metrics.summary()
    print(f"\n{'=' * 60}")
    print(f"  RUN SUMMARY — {summary['run_id']}")
    print(f"{'=' * 60}")
    print(f"  Total time: {summary['total_elapsed_seconds']}s")
    print(f"  Issues attempted: {summary['issues_attempted']}")
    print(f"  Passed: {summary['issues_passed']}")
    print(f"  Failed: {summary['issues_failed']}")
    print(f"  Skipped: {summary.get('issues_skipped', 0)}")
    print(f"  Total iterations: {summary['total_iterations']}")

    # Phase 1 summary
    print(f"\n  Phase 1 (framing):")
    for m in phase1_metrics:
        status = {"framed": "+", "skipped": "-", "failed": "!"}[m["status"]]
        err = f" — {m.get('error', '')[:80]}" if m.get("error") else ""
        print(f"    {status} [{m['id']}] {m['title']}{err}")

    print(f"\n  Phase 2 (testing):")
    for m in summary["issue_details"]:
        if m.get("skipped"):
            status = "-"
        elif m["success"]:
            status = "+"
        else:
            status = "!"
        print(f"  {status} [{m['issue_id']}] {m['title']}")
        if m.get("skipped"):
            print(f"    {m.get('error', 'Skipped')}")
        else:
            print(f"    Time: {m['elapsed_seconds']}s | Iters: {m['iterations']} | "
                  f"Strategy: {m['strategy']} | Stalls: {m['stall_count']} | "
                  f"Tools: {m['tool_calls']} | Agentic: {m['agentic_debug']}")
            if m["error"]:
                print(f"    Error: {m['error'][:100]}")

    # Save metrics
    metrics_dir = PROJECT_ROOT / "docs"
    metrics_dir.mkdir(exist_ok=True)
    metrics_path = metrics_dir / f"run_{summary['run_id']}.json"
    run_metrics.save(str(metrics_path))

    # Also save full logs
    logs_path = metrics_dir / f"run_{summary['run_id']}_logs.txt"
    with open(logs_path, "w") as f:
        f.write("PHASE 1: FRAME\n")
        f.write("=" * 60 + "\n")
        for m in phase1_metrics:
            f.write(f"  [{m['id']}] {m['title']} — {m['status']}")
            if m.get("error"):
                f.write(f" — {m['error']}")
            f.write("\n")
        f.write("\nPHASE 2: TEST\n")
        f.write("=" * 60 + "\n")
        for m in run_metrics.issues:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"Issue [{m.issue_id}] {m.title}\n")
            f.write(f"Success: {m.success} | Time: {m.elapsed_seconds:.1f}s | Iterations: {m.iterations}\n")
            f.write(f"{'=' * 60}\n")
            for line in m.log_lines:
                f.write(line + "\n")
    print(f"Logs saved to {logs_path}")


if __name__ == "__main__":
    main()
