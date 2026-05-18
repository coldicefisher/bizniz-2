"""Perf-test runner — CLI entry point.

Usage::

    python -m bizniz.perf_tests run <test-slug> [--runs N]
    python -m bizniz.perf_tests list
    python -m bizniz.perf_tests compare <test>/<run-A> <test>/<run-B>

The runner's job is *only* to:

1. Locate the test scenario (``bizniz.perf_tests.tests.<slug>``)
2. Set up a fresh per-run workspace under
   ``~/bizniz_perf_tests/<slug>/.runs/<run-id>/``
3. Invoke the test's ``run()`` function with that workspace
4. Capture timing + the scenario's return dict to ``result.json``
5. Tag the bizniz repo ``perf/<slug>/run-<N>``

The test scenario itself decides what to measure and how. The
runner is a thin harness — no opinion about Coder vs Decomposer
vs whatever. Future tests just drop a new file under
``bizniz/perf_tests/tests/<slug>.py`` exporting ``run(workspace,
fixture_root) -> dict``.

**No LLM calls live in this file.** All real work happens inside
the test scenarios.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PERF_ROOT = Path(os.environ.get(
    "BIZNIZ_PERF_TESTS_ROOT",
    str(Path.home() / "bizniz_perf_tests"),
))
FIXTURES_PKG = "bizniz.perf_tests.fixtures"
TESTS_PKG = "bizniz.perf_tests.tests"


# ── Discovery ────────────────────────────────────────────────────


def _discover_tests() -> List[str]:
    """Return the list of available test slugs by scanning
    ``bizniz.perf_tests.tests`` for ``.py`` files."""
    tests_dir = Path(__file__).parent / "tests"
    if not tests_dir.exists():
        return []
    return sorted(
        p.stem for p in tests_dir.glob("*.py")
        if not p.name.startswith("_")
    )


def _load_test(slug: str):
    """Import ``bizniz.perf_tests.tests.<slug>``. Returns the
    module. Raises ImportError on miss."""
    return importlib.import_module(f"{TESTS_PKG}.{slug}")


def _fixture_root(slug: str) -> Path:
    return Path(__file__).parent / "fixtures" / slug


# ── Run state ────────────────────────────────────────────────────


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _runs_dir(slug: str) -> Path:
    return PERF_ROOT / slug / ".runs"


def _existing_run_count(slug: str) -> int:
    """Return how many runs have already happened for this test
    (so the new tag gets a stable sequence number)."""
    rd = _runs_dir(slug)
    if not rd.exists():
        return 0
    return sum(
        1 for entry in rd.iterdir()
        if entry.is_dir() and (entry / "result.json").exists()
    )


# ── Git tagging ──────────────────────────────────────────────────


def _bizniz_repo_root() -> Path:
    return Path(__file__).parent.parent.parent


def _bizniz_git_rev() -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_bizniz_repo_root()),
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except OSError:
        pass
    return None


def _tag_run(slug: str, run_index: int, run_id: str) -> Optional[str]:
    """Tag the bizniz repo at HEAD: ``perf/<slug>/run-<N>``.

    Tag is annotated with the run_id so the message records which
    run dir on disk it corresponds to. Returns the tag name on
    success, None on any failure (never raises — perf tests must
    never break on git issues)."""
    tag = f"perf/{slug}/run-{run_index}"
    msg = f"perf test {slug} run {run_index} (id {run_id})"
    try:
        proc = subprocess.run(
            ["git", "tag", "-a", tag, "-m", msg],
            cwd=str(_bizniz_repo_root()),
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            return tag
    except OSError:
        pass
    return None


# ── Run loop ─────────────────────────────────────────────────────


def cmd_run(args) -> int:
    slug = args.slug
    if slug not in _discover_tests():
        sys.stderr.write(
            f"unknown test '{slug}'. available: "
            f"{', '.join(_discover_tests()) or '(none)'}\n"
        )
        return 2

    try:
        module = _load_test(slug)
    except ImportError as e:
        sys.stderr.write(f"failed to import test '{slug}': {e}\n")
        return 2
    if not hasattr(module, "run"):
        sys.stderr.write(
            f"test '{slug}' missing ``run(workspace, fixture_root)`` function\n"
        )
        return 2

    fixture_root = _fixture_root(slug)
    if not fixture_root.exists():
        sys.stderr.write(
            f"fixture missing for '{slug}': {fixture_root}\n"
        )
        return 2

    existing = _existing_run_count(slug)
    for i in range(args.runs):
        run_index = existing + i + 1
        run_id = _new_run_id()
        run_dir = _runs_dir(slug) / run_id
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        print(f"=== {slug} run #{run_index} (id={run_id}) ===")
        git_rev = _bizniz_git_rev()
        t0 = time.time()
        status = "ok"
        scenario_result: Dict[str, Any] = {}
        try:
            scenario_result = module.run(
                workspace=workspace, fixture_root=fixture_root,
            ) or {}
        except Exception as e:
            status = "errored"
            scenario_result = {
                "error": f"{type(e).__name__}: {e}",
                "traceback": traceback.format_exc(),
            }
        elapsed = time.time() - t0

        result = {
            "test_slug": slug,
            "run_index": run_index,
            "run_id": run_id,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "wall_clock_s": elapsed,
            "status": status,
            "git_rev": git_rev,
            "scenario_result": scenario_result,
        }
        (run_dir / "result.json").write_text(
            json.dumps(result, indent=2, default=str)
        )

        if not args.no_tag and status == "ok":
            tag = _tag_run(slug, run_index, run_id)
            if tag:
                print(f"    tagged: {tag}")
            else:
                print("    tag failed (non-fatal)")

        print(
            f"    wall: {elapsed:.1f}s  status: {status}  "
            f"result: {run_dir}/result.json"
        )

    return 0


def cmd_list(_args) -> int:
    tests = _discover_tests()
    if not tests:
        print("(no tests found under bizniz/perf_tests/tests/)")
        return 0
    print("Available perf tests:")
    for t in tests:
        print(f"  {t}")
        runs = _runs_dir(t)
        if runs.exists():
            n = _existing_run_count(t)
            print(f"    {n} prior run(s) under {runs}")
    return 0


def cmd_compare(args) -> int:
    """Cheap diff of two result.json files. Markdown-shaped output
    so it's drop-in for docs/perf_tests/<slug>.md."""

    def _load(path_spec: str) -> dict:
        # path_spec is "<test-slug>/<run-id>" — resolve to runs dir.
        parts = path_spec.split("/")
        if len(parts) != 2:
            sys.stderr.write(f"bad spec '{path_spec}'; expected <slug>/<run-id>\n")
            sys.exit(2)
        result_path = _runs_dir(parts[0]) / parts[1] / "result.json"
        if not result_path.exists():
            sys.stderr.write(f"no result.json at {result_path}\n")
            sys.exit(2)
        return json.loads(result_path.read_text())

    a = _load(args.baseline)
    b = _load(args.candidate)

    print(f"# perf compare: {args.baseline} → {args.candidate}\n")
    print(f"| | baseline | candidate | delta |")
    print(f"|---|---|---|---|")
    print(f"| wall_clock_s | {a['wall_clock_s']:.1f} | {b['wall_clock_s']:.1f} | "
          f"{b['wall_clock_s'] - a['wall_clock_s']:+.1f} |")
    print(f"| status | {a['status']} | {b['status']} | "
          f"{'⚠ changed' if a['status'] != b['status'] else 'same'} |")
    print(f"| git_rev | `{(a.get('git_rev') or 'n/a')[:8]}` | "
          f"`{(b.get('git_rev') or 'n/a')[:8]}` | |")

    print("\n## Scenario result deltas\n")
    ar = a.get("scenario_result") or {}
    br = b.get("scenario_result") or {}
    keys = sorted(set(ar.keys()) | set(br.keys()))
    for k in keys:
        av = ar.get(k)
        bv = br.get(k)
        marker = "" if av == bv else " ⚠"
        print(f"- **{k}**:{marker}")
        print(f"  - baseline: `{av}`")
        print(f"  - candidate: `{bv}`")

    return 0


# ── CLI ──────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bizniz.perf_tests")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="execute a test scenario")
    p_run.add_argument("slug")
    p_run.add_argument(
        "--runs", type=int, default=1,
        help="how many back-to-back runs (default 1). Useful for "
             "averaging LLM variance.",
    )
    p_run.add_argument(
        "--no-tag", action="store_true",
        help="skip git-tagging this run",
    )
    p_run.set_defaults(func=cmd_run)

    p_list = sub.add_parser("list", help="list available tests")
    p_list.set_defaults(func=cmd_list)

    p_cmp = sub.add_parser(
        "compare", help="markdown-diff two run results",
    )
    p_cmp.add_argument("baseline", help="<test-slug>/<run-id>")
    p_cmp.add_argument("candidate", help="<test-slug>/<run-id>")
    p_cmp.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
