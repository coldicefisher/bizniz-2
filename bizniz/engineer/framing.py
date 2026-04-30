"""
Phase 1 framing pass.

Cheaply generate baseline source for every issue in topological order,
with no tests and no Docker. After framing, every issue's target files
contain real working code instead of empty scaffold stubs, so:

  - Later issues can ``import`` from earlier ones via the actual API
    instead of speculating against stubs.
  - The Phase 2 test loop starts from a coherent codebase, so most
    layers pass on iteration 1 instead of needing repair cycles to
    glue mismatched files together.

This is the "quick pass" that ``examples/codegen_blast.py`` uses standalone;
this module is the engineer-side port called by ``AutoEngineer.run_layered``.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from bizniz.engineer.types import EngineeringIssue
from bizniz.preflight.registry import get_validator
from bizniz.workspace.base_workspace import BaseWorkspace

if TYPE_CHECKING:
    from bizniz.agents.autocoder.autocoder import Autocoder


def frame_issues(
    issues_topo: List[EngineeringIssue],
    autocoder: "Autocoder",
    workspace: BaseWorkspace,
    architecture_context: str,
    on_status_message: Optional[Callable[[str], None]] = None,
    language: str = "python",
) -> Dict[int, bool]:
    """
    Generate baseline source code for every issue, in the given topological
    order, without tests or Docker. Writes the generated files to the
    workspace and runs the language-specific preflight validator (auto-stubs
    missing local modules, rewrites broken imports) so each issue's output
    is consistent with what the next issue will see.

    Returns a dict of ``issue.db_id -> success`` (True if framed without
    raising; False if the autocoder failed for that issue).
    """

    def log(msg: str) -> None:
        if on_status_message:
            on_status_message(msg)

    log(f"AutoEngineer: Phase 1 framing — {len(issues_topo)} issue(s)...")

    results: Dict[int, bool] = {}
    validator = get_validator(language, workspace)

    for idx, issue in enumerate(issues_topo, 1):
        title_label = issue.title or f"issue #{issue.db_id}"
        log(f"AutoEngineer: framing [{idx}/{len(issues_topo)}] {title_label}")

        problem = issue.description or ""
        if issue.test_setup_hint:
            problem = f"{problem}\n\nTEST SETUP HINT:\n{issue.test_setup_hint}"

        target_files = [
            {"filepath": tf.filepath, "action": tf.action}
            for tf in issue.target_files
        ]

        try:
            result = autocoder.generate_multi(
                issue_description=problem,
                target_files=target_files,
                architecture_context=architecture_context,
                test_files=None,
                on_status_message=on_status_message,
            )
        except Exception as e:
            log(f"AutoEngineer: framing failed for {title_label}: {type(e).__name__}: {e}")
            results[issue.db_id] = False
            continue

        generated: Dict[str, str] = {}
        for change in result.changes:
            workspace.write_file(path=change.filepath, content=change.code)
            generated[change.filepath] = change.code

        if validator and generated:
            try:
                pf = validator.validate(generated, [])
                for stub in pf.stubs_created:
                    workspace.write_file(path=stub.filepath, content=stub.content)
                for rw in pf.import_rewrites:
                    if rw.filepath in generated:
                        existing = workspace.read_file(path=rw.filepath)
                        if existing:
                            workspace.write_file(
                                path=rw.filepath,
                                content=existing.replace(rw.old_import, rw.new_import),
                            )
            except Exception as e:
                # Preflight failure is non-fatal; the test loop will catch issues.
                log(f"AutoEngineer: preflight after framing skipped ({type(e).__name__}: {e})")

        results[issue.db_id] = True
        log(f"AutoEngineer: framed {title_label} — {len(generated)} file(s)")

    framed_ok = sum(1 for ok in results.values() if ok)
    log(f"AutoEngineer: Phase 1 framing complete — {framed_ok}/{len(issues_topo)} OK")
    return results


def flatten_layers_topo(layers) -> List[EngineeringIssue]:
    """Flatten a list of DependencyLayer objects into a single topological
    list of issues (Layer 0 first, Layer 1 next, etc.)."""
    out: List[EngineeringIssue] = []
    for layer in layers:
        out.extend(layer.issues)
    return out
