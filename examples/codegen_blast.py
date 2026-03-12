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

PROJECT_ROOT = Path("/home/jamey/bizniz_projects/pet_groomer")
WORKSPACE_DIR = PROJECT_ROOT / "backend"
DOCKER_IMAGE = "pet_groomer-backend:dev"
PROBLEM_ID = 8  # Issues with test_setup_hint + dependencies + model escalation
MAX_FAILURES = 3  # Stop after this many failed issues
MAX_ITERATIONS = 10  # Per-issue iteration cap


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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    from bizniz.config.bizniz_config import BiznizConfig
    from bizniz.workspace.local_workspace import LocalWorkspace
    from bizniz.environment.docker_pytest_environment import DockerPytestEnvironment
    from bizniz.clients.chatgpt.openai_chatgpt_client import OpenAIChat4GPTClient
    from bizniz.clients.chatgpt.chatgpt_client_config import ChatGPTClientConfig
    from bizniz.clients.claude.claude_client import ClaudeClient
    from bizniz.autocoder.autocoder import Autocoder
    from bizniz.autotester.autotester import Autotester
    from bizniz.orchestrator.coding_orchestrator import CodingOrchestrator
    from bizniz.orchestrator.strategy import CodingStrategy
    from bizniz.agentic_debugger.agentic_debugger import AgenticDebugger
    from bizniz.engineer.auto_engineer import AutoEngineer
    from bizniz.engineer.types import ArchitecturePlan
    from bizniz.orchestrator.model_progression import ModelProgression

    print("=" * 60)
    print("  Code Generation Blast Test")
    print("=" * 60)

    # Load config
    config = BiznizConfig.find_and_load()
    model = "gpt-4o-mini"
    print(f"\n  Model: {model}")
    print(f"  Project: {PROJECT_ROOT}")
    print(f"  Docker image: {DOCKER_IMAGE}")
    print(f"  Max failures before abort: {MAX_FAILURES}")
    print(f"  Max iterations per issue: {MAX_ITERATIONS}")

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
        "FROM issues WHERE problem_id=? ORDER BY id",
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
            arch_context = AutoEngineer.format_architecture_context(plan)
            print(f"\n  Architecture: {plan.package_name} ({len(plan.modules)} modules)")
        except Exception as e:
            print(f"\n  WARNING: Could not load architecture plan: {e}")

    print(f"\n{'=' * 60}")
    print("  Starting...\n")

    # Setup Docker environment
    env = DockerPytestEnvironment(
        workspace_root=WORKSPACE_DIR,
        image=DOCKER_IMAGE,
    )

    run_metrics = RunMetrics()
    failure_count = 0
    failed_issue_ids = set()  # Track failed IDs for dependency skip

    # Workspace snapshotting to prevent cascade failures
    snapshot_dir = PROJECT_ROOT / ".snapshots"
    snapshot_dir.mkdir(exist_ok=True)

    def _nuke_shadow_files(target_dir: Path):
        """Remove top-level .py files that shadow known packages.

        Checks stdlib, common aliases, and PyPI. Runs on workspace and
        snapshot dirs to prevent stale shadows from persisting across runs.
        """
        import urllib.request
        import urllib.error

        _stdlib = set(sys.stdlib_module_names)
        _aliases = {"cv2", "PIL", "sklearn", "yaml", "bs4", "gi", "attr", "dotenv"}

        for py_file in target_dir.glob("*.py"):
            if py_file.name.startswith("__"):
                continue
            stem = py_file.stem.lower().replace("-", "_")
            if stem in _stdlib or stem in _aliases:
                py_file.unlink()
                print(f"  [cleanup] Removed shadow file: {py_file.name}")
                continue
            # Quick PyPI check for anything else suspicious
            try:
                req = urllib.request.Request(
                    f"https://pypi.org/pypi/{stem}/json", method="HEAD"
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        py_file.unlink()
                        print(f"  [cleanup] Removed shadow file: {py_file.name} (exists on PyPI)")
            except Exception:
                pass  # not on PyPI or network error — keep it

    def _sanitize_requirements(target_dir: Path):
        """Remove invalid entries from requirements.txt.

        Strips stdlib modules, garbage entries, and duplicates that
        accumulate from bad LLM suggestions across runs.
        """
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
                # Extract bare package name
                pkg = _re.split(r"[><=!\[;]", stripped)[0].strip().lower().replace("-", "_")
                if pkg in _stdlib:
                    print(f"  [cleanup] Removed stdlib '{stripped}' from {req_file}")
                    continue
                if pkg in seen:
                    continue
                # Reject entries with no letters or that look malformed
                if not _re.match(r"^[a-zA-Z]", stripped):
                    print(f"  [cleanup] Removed invalid '{stripped}' from {req_file}")
                    continue
                seen.add(pkg)
                clean.append(stripped)
            req_file.write_text("\n".join(clean) + "\n")

    def snapshot_workspace(label: str):
        """Save a copy of the workspace after successful issue completion."""
        snap = snapshot_dir / label
        if snap.exists():
            shutil.rmtree(snap)
        shutil.copytree(WORKSPACE_DIR, snap, ignore=shutil.ignore_patterns(".bizniz", ".snapshots", "__pycache__", "*.pyc"))
        _nuke_shadow_files(snap)
        print(f"  [snapshot] Saved workspace state: {label}")

    def restore_workspace(label: str):
        """Restore workspace to a previous snapshot, preserving .bizniz DB."""
        snap = snapshot_dir / label
        if not snap.exists():
            print(f"  [snapshot] No snapshot '{label}' found — skipping restore")
            return
        # Remove current generated files but keep .bizniz
        for item in WORKSPACE_DIR.iterdir():
            if item.name in (".bizniz", ".snapshots"):
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        # Copy snapshot back
        for item in snap.iterdir():
            dest = WORKSPACE_DIR / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        _nuke_shadow_files(WORKSPACE_DIR)
        print(f"  [snapshot] Restored workspace to: {label}")

    # Clean workspace before baseline snapshot
    _nuke_shadow_files(WORKSPACE_DIR)
    _sanitize_requirements(WORKSPACE_DIR)
    # Also clean any existing snapshots
    if snapshot_dir.exists():
        for snap in snapshot_dir.iterdir():
            if snap.is_dir():
                _nuke_shadow_files(snap)
                _sanitize_requirements(snap)

    # Save initial clean state
    snapshot_workspace("baseline")
    last_good_snapshot = "baseline"

    try:
        for i, issue in enumerate(issues):
            # Skip issues whose dependencies failed
            blocked_by = [
                dep_id for dep_id in issue.get("depends_on", [])
                if dep_id in failed_issue_ids
            ]
            if blocked_by:
                issue_metrics = IssueMetrics(issue["id"], issue["title"])
                issue_metrics.skipped = True
                issue_metrics.elapsed_seconds = 0
                issue_metrics.error = f"Skipped: dependency {blocked_by} failed"
                # Propagate: this issue's dependents should also be skipped
                failed_issue_ids.add(issue["id"])
                run_metrics.add(issue_metrics)
                print(f"\n{'─' * 60}")
                print(f"  Issue {i+1}/{len(issues)}: [{issue['id']}] {issue['title']}")
                print(f"  ⏭ SKIPPED — blocked by failed dependency {blocked_by}")
                print(f"{'─' * 60}")
                continue

            issue_metrics = IssueMetrics(issue["id"], issue["title"])
            issue_metrics.start()

            print(f"\n{'─' * 60}")
            print(f"  Issue {i+1}/{len(issues)}: [{issue['id']}] {issue['title']}")
            print(f"{'─' * 60}")

            status_cb = make_status_callback(issue_metrics)

            try:
                # Create fresh client per issue
                client_config = ChatGPTClientConfig(default_model=model)
                client = OpenAIChat4GPTClient(
                    config=client_config,
                    api_key=os.environ["OPENAI_API_KEY"],
                )

                def make_client(model_name):
                    if model_name.startswith("claude"):
                        return ClaudeClient(
                            api_key=os.environ.get("ANTHROPIC_API_KEY"),
                            model_name=model_name,
                        )
                    cfg = ChatGPTClientConfig(default_model=model_name)
                    return OpenAIChat4GPTClient(
                        config=cfg,
                        api_key=os.environ["OPENAI_API_KEY"],
                    )

                autocoder = Autocoder(
                    client=client,
                    environment=env,
                    workspace=workspace,
                    on_status_message=status_cb,
                )

                autotester = Autotester(
                    client=client,
                    environment=env,
                    workspace=workspace,
                    on_status_message=status_cb,
                )

                # Model escalation on stall: matches bizniz.yaml repair_models
                progression = ModelProgression(["gpt-4o-mini", "gpt-4o", "gpt-5", "claude-sonnet", "claude-opus"])

                orchestrator = CodingOrchestrator(
                    autocoder=autocoder,
                    autotester=autotester,
                    test_environment=env,
                    workspace=workspace,
                    client=client,
                    client_factory=make_client,
                    model_progression=progression,
                    max_iterations=MAX_ITERATIONS,
                    stall_threshold=3,
                    agentic_debug_threshold=5,
                    on_status_message=status_cb,
                )

                # Build problem statement for orchestrator
                problem_stmt = f"{issue['description']}"
                if issue.get("test_setup_hint"):
                    problem_stmt += f"\n\nTEST SETUP HINT:\n{issue['test_setup_hint']}"

                result = orchestrator.run_multi(
                    prompt=problem_stmt,
                    target_files=issue["target_files"],
                    test_files=issue["test_files"],
                    architecture_context=arch_context,
                    strategy=CodingStrategy.CODE_FIRST,
                )

                issue_metrics.finish(
                    success=result.success,
                    iterations=result.iterations,
                    strategy=result.strategy_used,
                )

                if result.success:
                    print(f"\n  ✓ PASSED in {issue_metrics.elapsed_seconds:.1f}s "
                          f"({result.iterations} iterations)")
                    # Snapshot workspace after success to prevent cascade
                    snap_label = f"after_issue_{issue['id']}"
                    snapshot_workspace(snap_label)
                    last_good_snapshot = snap_label
                else:
                    failure_count += 1
                    failed_issue_ids.add(issue["id"])
                    issue_metrics.finish(
                        success=False,
                        iterations=result.iterations,
                        error="Tests did not pass",
                        strategy=result.strategy_used,
                    )
                    print(f"\n  ✗ FAILED in {issue_metrics.elapsed_seconds:.1f}s "
                          f"({result.iterations} iterations)")
                    # Rollback to last good state to prevent cascade
                    restore_workspace(last_good_snapshot)

            except Exception as e:
                error_name = type(e).__name__
                error_str = f"{error_name}: {str(e)[:200]}"

                # Autotester can't generate tests for non-code files (e.g. pyproject.toml)
                # — don't count these as real failures or block dependents
                if "no test files" in str(e).lower() or "no tests" in str(e).lower():
                    issue_metrics.finish(success=False, error=error_str)
                    issue_metrics.skipped = True
                    print(f"\n  ⚠ SKIPPED (no tests possible): {error_str}")
                else:
                    failure_count += 1
                    failed_issue_ids.add(issue["id"])
                    issue_metrics.finish(success=False, error=error_str)
                    print(f"\n  ✗ CRASHED: {error_str}")
                    # Rollback to last good state to prevent cascade
                    restore_workspace(last_good_snapshot)

            run_metrics.add(issue_metrics)

            # Check abort condition (skipped issues don't count)
            if failure_count >= MAX_FAILURES:
                # Skip remaining issues that depend on failed ones, but don't abort
                remaining = issues[i+1:]
                all_remaining_blocked = all(
                    any(d in failed_issue_ids for d in iss.get("depends_on", []))
                    for iss in remaining
                ) if remaining else False

                if not all_remaining_blocked:
                    print(f"\n{'=' * 60}")
                    print(f"  ABORTING: {failure_count} failures reached limit of {MAX_FAILURES}")
                    print(f"{'=' * 60}")
                    break

    finally:
        env.stop()
        # Clean up snapshots
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
    print()

    for m in summary["issue_details"]:
        if m.get("skipped"):
            status = "⏭"
        elif m["success"]:
            status = "✓"
        else:
            status = "✗"
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
